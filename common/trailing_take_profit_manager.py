"""
移动止盈管理器
基于6K线法则和4倍ATR的移动止盈系统
"""
import json
import os
import time
import hmac
import hashlib
import requests
from datetime import datetime, timedelta

from common.timeutil import index_to_naive_utc, to_naive_utc, utc_now_iso
from pathlib import Path
from urllib.parse import urlencode
import pandas as pd
import numpy as np

# 导入精度处理工具
from common.precision_helper import get_precision_helper


class TrailingTakeProfitManager:
    """
    移动止盈管理器
    
    核心功能：
    1. 基于6K线法则计算止盈价格
    2. 结合4倍ATR动态调整止盈位
    3. 实现移动止盈价格更新机制
    4. 自动触发止盈平仓
    
    算法规则：
    - 做多：止盈价 = 过去6根K线最高价 + 4×ATR（只能上移）
    - 做空：止盈价 = 过去6根K线最低价 - 4×ATR（只能下移）
    """
    
    def __init__(self, symbol, timeframe, data_dir='data/first_positions', exchange=None):
        """
        初始化移动止盈管理器
        
        Args:
            symbol: 交易币种，如 'SPK/USDT'
            timeframe: 时间周期，如 '5m', '1m'
            data_dir: 数据存储目录
            exchange: ccxt交易所实例（用于设置币安止盈单）
        """
        self.symbol = symbol
        self.timeframe = timeframe
        self.data_dir = data_dir
        self.exchange = exchange  # 币安交易所实例
        
        # ATR倍数设置为4倍（止盈比止损更远）
        self.atr_multiplier = 4
        
        # 确保数据目录存在
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        
        # 持仓追踪文件路径
        safe_symbol = symbol.replace('/', '_')
        self.position_file = os.path.join(
            data_dir, 
            f'trailing_take_profit_{safe_symbol}_{timeframe}.json'
        )
        
        # 加载或初始化持仓追踪数据
        self.position_data = self._load_position_data()
        
    def _load_position_data(self):
        """加载持仓追踪数据"""
        if os.path.exists(self.position_file):
            try:
                with open(self.position_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                print(f"[TrailingTakeProfit] 加载持仓数据失败: {e}")
        
        return {
            'has_position': False,
            'side': None,  # 'long' or 'short'
            'entry_time': None,
            'entry_price': None,
            'current_take_profit': None,
            'take_profit_history': [],
            'kline_cycle_count': 0,  # 已完成的6K线周期数
            'last_update_time': None,
            'binance_tp_order_id': None,  # 币安止盈单ID
            'position_amount': None  # 持仓数量
        }
    
    def _save_position_data(self):
        """保存持仓追踪数据"""
        try:
            with open(self.position_file, 'w', encoding='utf-8') as f:
                json.dump(self.position_data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"[TrailingTakeProfit] 保存持仓数据失败: {e}")
    
    def calculate_atr(self, df, period=14):
        """
        计算ATR指标
        
        Args:
            df: K线数据DataFrame，需包含 high, low, close 列
            period: ATR周期，默认14
            
        Returns:
            Series: ATR值序列
        """
        if df is None or len(df) < period:
            return None
        
        # 计算真实波幅 (True Range)
        high = df['high']
        low = df['low']
        close = df['close']
        
        # TR = max(high - low, abs(high - prev_close), abs(low - prev_close))
        tr1 = high - low
        tr2 = abs(high - close.shift(1))
        tr3 = abs(low - close.shift(1))
        
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        
        # ATR = MA(TR, period)
        atr = tr.rolling(window=period).mean()
        
        return atr
    
    def get_6kline_range(self, df, entry_time, cycle_number=0):
        """
        获取6K线范围的数据
        
        Args:
            df: K线数据DataFrame
            entry_time: 开仓时间
            cycle_number: 第几个6K线周期（0为初始周期）
            
        Returns:
            DataFrame: 6根K线的数据，如果数据不足返回None
        """
        if df is None or len(df) == 0:
            return None
        
        # 自动检测时间列名并转换为datetime
        df = self._ensure_datetime_column(df)
        
        # 检查datetime列是否存在
        if 'datetime' not in df.columns:
            return None
        
        # 找到开仓时间对应的K线索引
        # 统一换算为 naive UTC。旧持仓记录里的 entry_time 是"本机本地时间"，
        # to_naive_utc 会按本机时区换算，因此升级后在途持仓仍然正确。
        entry_datetime = to_naive_utc(entry_time)
        
        # 找到开仓时间之后的第一根完整K线（不包含开仓时刻所在的那根K线）
        after_entry = df[df['datetime'] > entry_datetime]
        
        if len(after_entry) == 0:
            return None
        
        # 计算起始索引：每个周期是6根K线
        start_idx = cycle_number * 6
        end_idx = start_idx + 6
        
        # 获取对应周期的6根K线
        if len(after_entry) < end_idx:
            return None  # 数据不足
        
        return after_entry.iloc[start_idx:end_idx]
    
    def calculate_take_profit_price(self, df, entry_time, side, cycle_number=0, atr_period=14):
        """
        计算止盈价格（6K线法则 + 4倍ATR）
        
        Args:
            df: K线数据DataFrame
            entry_time: 开仓时间
            side: 持仓方向 'long' 或 'short'
            cycle_number: 第几个6K线周期
            atr_period: ATR计算周期
            
        Returns:
            dict: {
                'take_profit': 止盈价格,
                'extreme_price': 极值价格（最高价或最低价）,
                'atr_value': ATR值,
                'kline_data': 6根K线数据
            }
        """
        # 获取6根K线数据
        kline_6 = self.get_6kline_range(df, entry_time, cycle_number)
        
        if kline_6 is None:
            return None
        
        # 计算ATR
        atr_series = self.calculate_atr(df, period=atr_period)
        
        if atr_series is None:
            return None
        
        if side == 'long':
            # 做多：找6根K线中的最高价
            max_idx = kline_6['high'].idxmax()
            extreme_price = kline_6.loc[max_idx, 'high']
            atr_value = atr_series.loc[max_idx] if max_idx in atr_series.index else atr_series.iloc[-1]
            
            # 止盈价格 = 最高价 + 4×ATR
            take_profit = extreme_price + (self.atr_multiplier * atr_value)
            
        else:  # side == 'short'
            # 做空：找6根K线中的最低价
            min_idx = kline_6['low'].idxmin()
            extreme_price = kline_6.loc[min_idx, 'low']
            atr_value = atr_series.loc[min_idx] if min_idx in atr_series.index else atr_series.iloc[-1]
            
            # 止盈价格 = 最低价 - 4×ATR
            take_profit = extreme_price - (self.atr_multiplier * atr_value)
        
        return {
            'take_profit': take_profit,
            'extreme_price': extreme_price,
            'atr_value': atr_value,
            'kline_data': kline_6
        }
    
    def on_position_opened(self, side, entry_price, entry_time=None, position_amount=None):
        """
        开仓时调用：记录持仓信息
        
        Args:
            side: 持仓方向 'long' 或 'short'
            entry_price: 开仓价格
            entry_time: 开仓时间，默认为当前时间
            position_amount: 持仓数量
        """
        if entry_time is None:
            entry_time = utc_now_iso()  # 带显式 UTC 偏移，避免与 K 线时间口径不一致
        
        self.position_data = {
            'has_position': True,
            'side': side,
            'entry_time': entry_time,
            'entry_price': entry_price,
            'current_take_profit': None,
            'take_profit_history': [],
            'kline_cycle_count': 0,
            'last_update_time': entry_time,
            'binance_tp_order_id': None,
            'position_amount': position_amount
        }
        
        self._save_position_data()
        
        print(f"[TrailingTakeProfit] 开仓记录: {side} @ {entry_price}, 数量: {position_amount}, 时间: {entry_time}")
    
    def on_position_closed(self):
        """
        平仓时调用：清除持仓信息，并取消币安止盈单
        """
        print(f"[TrailingTakeProfit] 平仓记录: {self.position_data['side']} @ {self.position_data['entry_price']}")
        print(f"[TrailingTakeProfit] 最终止盈价格: {self.position_data['current_take_profit']}")
        print(f"[TrailingTakeProfit] 止盈更新历史: {self.position_data['take_profit_history']}")
        
        # 取消币安止盈单（如果存在）
        if self.position_data.get('binance_tp_order_id'):
            self._cancel_binance_tp_order(self.position_data['binance_tp_order_id'])
        
        self.position_data = {
            'has_position': False,
            'side': None,
            'entry_time': None,
            'entry_price': None,
            'current_take_profit': None,
            'take_profit_history': [],
            'kline_cycle_count': 0,
            'last_update_time': None,
            'binance_tp_order_id': None,
            'position_amount': None
        }
        
        self._save_position_data()
    
    def update_trailing_take_profit(self, df, current_price, atr_period=14):
        """
        更新移动止盈价格
        
        Args:
            df: 最新的K线数据DataFrame
            current_price: 当前价格
            atr_period: ATR计算周期
            
        Returns:
            dict: {
                'take_profit_triggered': 是否触发止盈,
                'should_close': 是否应该平仓,
                'take_profit_updated': 止盈价格是否更新,
                'current_take_profit': 当前止盈价格,
                'message': 消息说明
            }
        """
        # 如果没有持仓，直接返回
        if not self.position_data['has_position']:
            return {
                'take_profit_triggered': False,
                'should_close': False,
                'take_profit_updated': False,
                'current_take_profit': None,
                'message': '无持仓'
            }
        
        side = self.position_data['side']
        entry_time = self.position_data['entry_time']
        current_take_profit = self.position_data['current_take_profit']
        
        # 计算当前应该检查第几个6K线周期
        df = self._ensure_datetime_column(df)
        
        # 检查datetime列是否存在
        if 'datetime' not in df.columns:
            return {
                'take_profit_triggered': False,
                'should_close': False,
                'take_profit_updated': False,
                'current_take_profit': current_take_profit,
                'message': '数据缺少时间列'
            }
        
        # 确保entry_time转换为无时区的datetime，并找到开仓后的第一根完整K线
        entry_datetime = pd.to_datetime(entry_time)
        if hasattr(entry_datetime, 'tz') and entry_datetime.tz is not None:
            entry_datetime = entry_datetime.tz_localize(None)
        
        # 找到开仓时间之后的第一根完整K线
        after_entry = df[df['datetime'] > entry_datetime]
        
        # 【调试日志】输出详细的时间信息
        print(f"[TrailingTakeProfit] 开仓时间: {entry_datetime} (type: {type(entry_datetime)})")
        if len(df) > 0:
            print(f"[TrailingTakeProfit] DataFrame时间范围: {df['datetime'].min()} 至 {df['datetime'].max()}")
            print(f"[TrailingTakeProfit] DataFrame最后时间类型: {type(df['datetime'].iloc[-1])}")
        print(f"[TrailingTakeProfit] 开仓后K线数量: {len(after_entry)}")
        if len(after_entry) > 0:
            print(f"[TrailingTakeProfit] 开仓后第一根K线时间: {after_entry['datetime'].iloc[0]}")
            print(f"[TrailingTakeProfit] 开仓后最后一根K线时间: {after_entry['datetime'].iloc[-1]}")
        
        if len(after_entry) == 0:
            return {
                'take_profit_triggered': False,
                'should_close': False,
                'take_profit_updated': False,
                'current_take_profit': current_take_profit,
                'message': '无最新K线数据'
            }
        
        # 计算已完成的6K线周期数
        completed_klines = len(after_entry)
        completed_cycles = completed_klines // 6
        
        # 【调试日志】输出周期计算信息
        print(f"[TrailingTakeProfit] 完整K线数: {completed_klines}, 完整周期数: {completed_cycles}")
        
        # 检查是否有新的完整6K线周期
        if completed_cycles > self.position_data['kline_cycle_count']:
            # 有新的6K线周期，计算新的止盈价格
            result = self.calculate_take_profit_price(
                df, entry_time, side, completed_cycles - 1, atr_period
            )
            
            if result is not None:
                new_take_profit = result['take_profit']
                
                # 判断是否应该更新止盈价格
                should_update = False
                
                if current_take_profit is None:
                    # 第一次设置止盈价格
                    should_update = True
                    message = f'初始止盈价格设置: {new_take_profit:.6f}'
                else:
                    if side == 'long':
                        # 做多：新止盈价格必须高于旧止盈价格
                        if new_take_profit > current_take_profit:
                            should_update = True
                            message = f'做多止盈上移: {current_take_profit:.6f} -> {new_take_profit:.6f}'
                        else:
                            message = f'做多止盈未上移(新:{new_take_profit:.6f} <= 旧:{current_take_profit:.6f})'
                    else:  # short
                        # 做空：新止盈价格必须低于旧止盈价格
                        if new_take_profit < current_take_profit:
                            should_update = True
                            message = f'做空止盈下移: {current_take_profit:.6f} -> {new_take_profit:.6f}'
                        else:
                            message = f'做空止盈未下移(新:{new_take_profit:.6f} >= 旧:{current_take_profit:.6f})'
                
                # 【关键】无论止盈价格是否更新，都要更新周期计数器和记录检查历史
                self.position_data['kline_cycle_count'] = completed_cycles
                self.position_data['last_update_time'] = datetime.now().isoformat()
                
                # 记录每个周期的检查结果（包括未更新的情况）
                self.position_data['take_profit_history'].append({
                    'cycle': completed_cycles,
                    'take_profit': new_take_profit,
                    'applied': should_update,  # 标记是否实际应用了这个止盈价格
                    'time': datetime.now().isoformat(),
                    'extreme_price': result['extreme_price'],
                    'atr_value': result['atr_value']
                })
                
                if should_update:
                    # 实际更新止盈价格
                    self.position_data['current_take_profit'] = new_take_profit
                    
                    self._save_position_data()
                    
                    print(f"[TrailingTakeProfit] {message}")
                    print(f"[TrailingTakeProfit] 第{completed_cycles}个6K线周期完成")
                    print(f"[TrailingTakeProfit] 极值价格: {result['extreme_price']:.6f}, ATR: {result['atr_value']:.6f} (4倍ATR: {result['atr_value']*4:.6f})")
                    
                    # 【关键功能】更新币安止盈单
                    position_amount = self.position_data.get('position_amount')
                    if position_amount and self.exchange:
                        binance_order_id = self._update_binance_tp_order(side, new_take_profit, position_amount)
                        if binance_order_id:
                            print(f"[TrailingTakeProfit] ✓ 币安止盈单已更新为新价格: {new_take_profit:.6f}")
                        else:
                            print(f"[TrailingTakeProfit] ⚠️  币安止盈单更新失败，但本地止盈价已更新")
                    else:
                        if not self.exchange:
                            print(f"[TrailingTakeProfit] ⚠️  交易所实例未初始化，无法设置币安止盈单")
                        else:
                            print(f"[TrailingTakeProfit] ⚠️  持仓数量未知，无法设置币安止盈单")
                    
                    current_take_profit = new_take_profit
                else:
                    # 止盈价格未更新，但周期已检查
                    self._save_position_data()
                    
                    print(f"[TrailingTakeProfit] {message}")
                    print(f"[TrailingTakeProfit] 第{completed_cycles}个6K线周期完成（止盈价格保持不变）")
                    print(f"[TrailingTakeProfit] 极值价格: {result['extreme_price']:.6f}, ATR: {result['atr_value']:.6f} (4倍ATR: {result['atr_value']*4:.6f})")
        
        # 检查是否触发止盈
        take_profit_triggered = False
        should_close = False
        
        if current_take_profit is not None:
            if side == 'long':
                # 做多：当前价格涨破止盈价格
                if current_price >= current_take_profit:
                    take_profit_triggered = True
                    should_close = True
                    message = f'做多止盈触发！当前价格 {current_price:.6f} >= 止盈价格 {current_take_profit:.6f}'
                    print(f"[TrailingTakeProfit] ⚠️  {message}")
            else:  # short
                # 做空：当前价格跌破止盈价格
                if current_price <= current_take_profit:
                    take_profit_triggered = True
                    should_close = True
                    message = f'做空止盈触发！当前价格 {current_price:.6f} <= 止盈价格 {current_take_profit:.6f}'
                    print(f"[TrailingTakeProfit] ⚠️  {message}")
        
        return {
            'take_profit_triggered': take_profit_triggered,
            'should_close': should_close,
            'take_profit_updated': completed_cycles > self.position_data['kline_cycle_count'],
            'current_take_profit': current_take_profit,
            'message': message if 'message' in locals() else '止盈监控中',
            'kline_cycles': completed_cycles,
            'entry_price': self.position_data['entry_price'],
            'side': side
        }
    
    def get_position_info(self):
        """获取当前持仓信息"""
        return self.position_data.copy()
    
    def has_position(self):
        """是否有持仓"""
        return self.position_data['has_position']
    
    def _ensure_datetime_column(self, df):
        """
        确保DataFrame有datetime列
        自动检测时间列名和格式，包括索引中的时间戳
        """
        if df is None or df.empty:
            return df
        
        # 如果已经有datetime列，直接返回
        if 'datetime' in df.columns:
            if not pd.api.types.is_datetime64_any_dtype(df['datetime']):
                df = df.copy()
                df['datetime'] = pd.to_datetime(df['datetime'])
            if hasattr(df['datetime'].dtype, 'tz') and df['datetime'].dtype.tz is not None:
                df = df.copy()
                df['datetime'] = df['datetime'].dt.tz_localize(None)
            return df
        
        df = df.copy()
        
        # 检查索引是否是时间类型
        if pd.api.types.is_datetime64_any_dtype(df.index):
            # 统一为 naive UTC。此前这里把 naive(本就是 UTC 的)索引强行 +8 小时
            # 转成新加坡时间，与 entry_time 的本机本地时间口径不一致。
            df['datetime'] = index_to_naive_utc(df.index)
            print(f"[TrailingTakeProfit] 从索引提取datetime列（统一为 naive UTC，原时区: {getattr(df.index, 'tz', None)}）")
            return df
        elif df.index.name in ['time', 'timestamp'] or isinstance(df.index, pd.DatetimeIndex):
            try:
                df['datetime'] = pd.to_datetime(df.index)
                print(f"[TrailingTakeProfit] 从索引提取datetime列（索引名: {df.index.name}）")
                return df
            except Exception as e:
                print(f"[TrailingTakeProfit] 无法从索引转换datetime: {e}")
        
        # 检测时间列名
        time_col = None
        if 'time' in df.columns:
            time_col = 'time'
        elif 'timestamp' in df.columns:
            time_col = 'timestamp'
        else:
            print(f"[TrailingTakeProfit] 警告：未找到时间列，尝试使用索引")
            try:
                df['datetime'] = pd.to_datetime(df.index)
                return df
            except Exception as e:
                print(f"[TrailingTakeProfit] 无法从索引创建datetime列: {e}")
                return df
        
        # 检测时间格式并转换
        try:
            df['datetime'] = pd.to_datetime(df[time_col])
        except:
            try:
                df['datetime'] = pd.to_datetime(df[time_col], unit='ms')
            except:
                try:
                    df['datetime'] = pd.to_datetime(df[time_col], unit='s')
                except Exception as e:
                    print(f"[TrailingTakeProfit] 无法解析时间列 '{time_col}': {e}")
                    return df
        
        return df
    
    def _get_api_credentials(self):
        """
        从exchange实例获取API凭证
        
        Returns:
            tuple: (api_key, api_secret) 或 (None, None)
        """
        try:
            if hasattr(self.exchange, 'apiKey') and hasattr(self.exchange, 'secret'):
                return self.exchange.apiKey, self.exchange.secret
            return None, None
        except Exception as e:
            print(f"[TrailingTakeProfit] 获取API凭证失败: {e}")
            return None, None
    
    def _generate_signature(self, params, api_secret):
        """
        生成币安API签名
        
        Args:
            params: 请求参数字典
            api_secret: API密钥
            
        Returns:
            str: HMAC SHA256签名
        """
        query_string = urlencode(params)
        signature = hmac.new(                                                                                                                                         
            api_secret.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        return signature
    
    def _create_binance_algo_tp_order(self, side, tp_price, amount, max_retries=3):
        """
        使用币安Algo Order API创建止盈单
        
        币安API升级后，TAKE_PROFIT_MARKET等条件订单必须通过Algo Order API来创建。
        
        包含重试机制：遇到超时(-1007)或网络错误时自动重试，使用指数退避策略。
        
        API端点: POST /fapi/v1/algoOrder
        
        Args:
            side: 持仓方向 'long' 或 'short'
            tp_price: 止盈触发价格
            amount: 持仓数量
            max_retries: 最大重试次数，默认3次
            
        Returns:
            str: algoId（订单ID），如果失败返回None
        """
        api_key, api_secret = self._get_api_credentials()
        if not api_key or not api_secret:
            print(f"[TrailingTakeProfit] ✗ 无法获取API凭证，无法创建Algo止盈单")
            return None
        
        try:
            # 确定平仓方向和持仓方向
            order_side = 'SELL' if side == 'long' else 'BUY'
            position_side = 'LONG' if side == 'long' else 'SHORT'
            
            # 格式化交易对符号
            symbol = self.symbol.replace('/', '')
            
            # 使用精度处理工具格式化价格和数量
            precision_helper = get_precision_helper(self.exchange)
            formatted_price = precision_helper.format_price_str(self.symbol, tp_price)
            formatted_qty = precision_helper.format_quantity_str(self.symbol, amount)
            print(f"[TrailingTakeProfit] 精度格式化: 价格 {tp_price} -> {formatted_price}, 数量 {amount} -> {formatted_qty}")
            
            # 构建请求参数
            timestamp = int(time.time() * 1000)
            
            params = {
                'symbol': symbol,
                'side': order_side,
                'positionSide': position_side,
                'algoType': 'CONDITIONAL',  # Algo订单类型：条件单
                'type': 'TAKE_PROFIT_MARKET',  # 订单类型
                'quantity': formatted_qty,
                'triggerPrice': formatted_price,  # Algo Order API使用triggerPrice
                'workingType': 'CONTRACT_PRICE',
                'priceProtect': 'false',
                'timestamp': timestamp,
                'recvWindow': 5000
            }
            
            # 生成签名
            signature = self._generate_signature(params, api_secret)
            params['signature'] = signature
            
            # 发送请求
            base_url = 'https://fapi.binance.com'
            endpoint = '/fapi/v1/algoOrder'
            headers = {
                'X-MBX-APIKEY': api_key,
                'Content-Type': 'application/x-www-form-urlencoded'
            }
            
            print(f"[TrailingTakeProfit] 正在通过Algo Order API创建止盈单...")
            print(f"[TrailingTakeProfit]   API端点: POST {endpoint}")
            print(f"[TrailingTakeProfit]   持仓方向: {side} (positionSide={position_side})")
            print(f"[TrailingTakeProfit]   订单类型(type): TAKE_PROFIT_MARKET")
            print(f"[TrailingTakeProfit]   止盈价格(stopPrice): {tp_price}")
            print(f"[TrailingTakeProfit]   平仓数量: {amount}")
            print(f"[TrailingTakeProfit]   平仓方向: {order_side}")
            # 调试：打印完整请求参数（不含签名）
            debug_params = {k: v for k, v in params.items() if k != 'signature'}
            print(f"[TrailingTakeProfit]   请求参数: {debug_params}")
            
            # 重试机制：遇到超时或可重试错误时自动重试
            last_error = None
            for attempt in range(max_retries):
                try:
                    # 每次重试需要更新时间戳和签名
                    if attempt > 0:
                        params['timestamp'] = int(time.time() * 1000)
                        params.pop('signature', None)
                        signature = self._generate_signature(params, api_secret)
                        params['signature'] = signature
                        wait_time = 2 ** attempt  # 指数退避: 2, 4, 8秒
                        print(f"[TrailingTakeProfit] 第{attempt + 1}次重试，等待{wait_time}秒...")
                        time.sleep(wait_time)
                    
                    response = requests.post(
                        f"{base_url}{endpoint}",
                        headers=headers,
                        data=urlencode(params),
                        timeout=30  # 增加超时时间到30秒
                    )
                    
                    result = response.json()
                    
                    if response.status_code == 200 and 'algoId' in result:
                        algo_id = str(result['algoId'])
                        algo_status = result.get('algoStatus', 'UNKNOWN')
                        print(f"[TrailingTakeProfit] ✓ Algo止盈单创建成功！")
                        print(f"[TrailingTakeProfit]   algoId: {algo_id}")
                        print(f"[TrailingTakeProfit]   algoType: {result.get('algoType', 'N/A')}")
                        print(f"[TrailingTakeProfit]   algoStatus: {algo_status}")
                        print(f"[TrailingTakeProfit]   triggerPrice: {result.get('triggerPrice', 'N/A')}")
                        return algo_id
                    else:
                        error_code = result.get('code', 'N/A')
                        error_msg = result.get('msg', str(result))
                        
                        # 判断是否为可重试错误
                        retryable_errors = [-1007, -1001, -1000]
                        
                        if error_code in retryable_errors and attempt < max_retries - 1:
                            print(f"[TrailingTakeProfit] ⚠ 遇到可重试错误: {error_code} - {error_msg}")
                            last_error = f"错误码:{error_code}, {error_msg}"
                            continue
                        
                        print(f"[TrailingTakeProfit] ✗ Algo止盈单创建失败")
                        print(f"[TrailingTakeProfit]   HTTP状态码: {response.status_code}")
                        print(f"[TrailingTakeProfit]   错误代码: {error_code}")
                        print(f"[TrailingTakeProfit]   错误信息: {error_msg}")
                        return None
                        
                except requests.exceptions.Timeout as e:
                    last_error = f"请求超时: {e}"
                    if attempt < max_retries - 1:
                        print(f"[TrailingTakeProfit] ⚠ 请求超时，准备重试...")
                        continue
                    print(f"[TrailingTakeProfit] ✗ 请求超时，已达最大重试次数")
                    
                except requests.exceptions.RequestException as e:
                    last_error = f"网络错误: {e}"
                    if attempt < max_retries - 1:
                        print(f"[TrailingTakeProfit] ⚠ 网络错误，准备重试...")
                        continue
                    print(f"[TrailingTakeProfit] ✗ 网络请求失败: {e}")
            
            # 所有重试都失败
            if last_error:
                print(f"[TrailingTakeProfit] ✗ Algo止盈单创建失败（重试{max_retries}次后）: {last_error}")
            return None
        except Exception as e:
            print(f"[TrailingTakeProfit] ✗ 创建Algo止盈单失败: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def _create_binance_tp_order(self, side, tp_price, amount):
        """
        创建币安市价止盈单
        
        优先使用Algo Order API，如果失败则尝试传统API（作为降级方案）
        
        Args:
            side: 持仓方向 'long' 或 'short'
            tp_price: 止盈触发价格
            amount: 持仓数量
            
        Returns:
            str: 订单ID（algoId或orderId），如果失败返回None
        """
        if not self.exchange:
            print(f"[TrailingTakeProfit] 错误：交易所实例未初始化，无法创建止盈单")
            return None
        
        # 方案一：使用Algo Order API（推荐，适配币安API升级）
        print(f"[TrailingTakeProfit] 尝试使用Algo Order API创建止盈单...")
        algo_id = self._create_binance_algo_tp_order(side, tp_price, amount)
        
        if algo_id:
            return algo_id
        
        # 方案二：降级到传统API（兼容旧版本）
        print(f"[TrailingTakeProfit] Algo Order API失败，尝试使用传统API...")
        try:
            order_side = 'sell' if side == 'long' else 'buy'
            position_side = 'LONG' if side == 'long' else 'SHORT'
            
            params = {
                'stopPrice': float(tp_price),  # 转为原生float：ccxt 4.5.78 起不识别 numpy.float64
                'type': 'TAKE_PROFIT_MARKET',
                'positionSide': position_side
            }
            
            print(f"[TrailingTakeProfit] 正在通过传统API创建币安止盈单...")
            print(f"[TrailingTakeProfit]   持仓方向: {side} (positionSide={position_side})")
            print(f"[TrailingTakeProfit]   止盈价格: {tp_price:.6f}")
            print(f"[TrailingTakeProfit]   平仓数量: {amount}")
            print(f"[TrailingTakeProfit]   平仓方向: {order_side}")
            
            order = self.exchange.create_order(
                symbol=self.symbol,
                type='TAKE_PROFIT_MARKET',
                side=order_side,
                amount=amount,
                params=params
            )
            
            order_id = order.get('id') or order.get('orderId')
            
            if order_id:
                print(f"[TrailingTakeProfit] ✓ 传统止盈单创建成功！")
                print(f"[TrailingTakeProfit]   订单ID: {order_id}")
                print(f"[TrailingTakeProfit]   止盈价: {tp_price:.6f}")
                print(f"[TrailingTakeProfit]   状态: {order.get('status', 'N/A')}")
                return str(order_id)
            else:
                print(f"[TrailingTakeProfit] ✗ 传统止盈单创建失败：无法获取订单ID")
                return None
                
        except Exception as e:
            print(f"[TrailingTakeProfit] ✗ 传统API也失败: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def _cancel_binance_algo_tp_order(self, algo_id):
        """
        取消币安Algo止盈单
        
        API端点: DELETE /fapi/v1/algoOrder
        
        Args:
            algo_id: Algo订单ID
            
        Returns:
            bool: 是否成功取消
        """
        api_key, api_secret = self._get_api_credentials()
        if not api_key or not api_secret:
            print(f"[TrailingTakeProfit] ✗ 无法获取API凭证，无法取消Algo止盈单")
            return False
        
        try:
            symbol = self.symbol.replace('/', '')
            timestamp = int(time.time() * 1000)
            
            params = {
                'symbol': symbol,
                'algoId': algo_id,
                'timestamp': timestamp,
                'recvWindow': 5000
            }
            
            signature = self._generate_signature(params, api_secret)
            params['signature'] = signature
            
            base_url = 'https://fapi.binance.com'
            endpoint = '/fapi/v1/algoOrder'
            headers = {
                'X-MBX-APIKEY': api_key
            }
            
            print(f"[TrailingTakeProfit] 正在取消Algo止盈单 ID: {algo_id}")
            
            response = requests.delete(
                f"{base_url}{endpoint}",
                headers=headers,
                params=params,
                timeout=10
            )
            
            result = response.json()
            
            if response.status_code == 200:
                print(f"[TrailingTakeProfit] ✓ Algo止盈单已取消: {algo_id}")
                return True
            else:
                error_code = result.get('code', 'N/A')
                error_msg = result.get('msg', str(result))
                
                # 检查是否订单已不存在或已执行
                if error_code in [-2011, -2013, -20124]:  # 订单不存在/已完成；-2014/-2015 为密钥或权限错误，不能视为已撤销
                    print(f"[TrailingTakeProfit] Algo止盈单不存在或已执行: {algo_id}")
                    return True
                
                print(f"[TrailingTakeProfit] ✗ 取消Algo止盈单失败")
                print(f"[TrailingTakeProfit]   错误代码: {error_code}")
                print(f"[TrailingTakeProfit]   错误信息: {error_msg}")
                return False
                
        except requests.exceptions.RequestException as e:
            print(f"[TrailingTakeProfit] ✗ 网络请求失败: {e}")
            return False
        except Exception as e:
            print(f"[TrailingTakeProfit] ✗ 取消Algo止盈单异常: {e}")
            return False
    
    def _cancel_binance_tp_order(self, order_id):
        """
        取消币安止盈单（同时支持Algo订单和传统订单）
        
        Args:
            order_id: 订单ID（algoId或orderId）
            
        Returns:
            bool: 是否成功取消
        """
        if not self.exchange:
            print(f"[TrailingTakeProfit] 错误：交易所实例未初始化，无法取消止盈单")
            return False
        
        if not order_id:
            return True  # 没有订单ID，视为成功
        
        # 方案一：尝试使用Algo Order API取消
        if self._cancel_binance_algo_tp_order(order_id):
            return True
        
        # 方案二：尝试使用传统API取消
        try:
            print(f"[TrailingTakeProfit] Algo取消失败，尝试使用传统API取消止盈单 ID: {order_id}")
            
            result = self.exchange.cancel_order(
                id=order_id,
                symbol=self.symbol
            )
            
            print(f"[TrailingTakeProfit] ✓ 传统止盈单已取消: {order_id}")
            return True
            
        except Exception as e:
            error_msg = str(e).lower()
            if 'unknown order' in error_msg or 'order does not exist' in error_msg:
                print(f"[TrailingTakeProfit] 止盈单不存在或已执行: {order_id}")
                return True
            
            print(f"[TrailingTakeProfit] ✗ 取消止盈单全部失败: {e}")
            return False
    
    def _update_binance_tp_order(self, side, new_tp_price, amount):
        """
        更新币安止盈单（先取消旧的，再创建新的）
        
        Args:
            side: 持仓方向 'long' 或 'short'
            new_tp_price: 新的止盈价格
            amount: 持仓数量
            
        Returns:
            str: 新订单ID，如果失败返回None
        """
        old_order_id = self.position_data.get('binance_tp_order_id')
        
        # 先取消旧的止盈单
        if old_order_id:
            print(f"[TrailingTakeProfit] 移动止盈触发：需要更新币安止盈单")
            print(f"[TrailingTakeProfit]   旧止盈价: {self.position_data.get('current_take_profit', 'N/A')}")
            print(f"[TrailingTakeProfit]   新止盈价: {new_tp_price:.6f}")
            
            if not self._cancel_binance_tp_order(old_order_id):
                print(f"[TrailingTakeProfit] ✗ 取消旧止盈单失败，中止更新")
                return None
        
        # 创建新的止盈单
        new_order_id = self._create_binance_tp_order(side, new_tp_price, amount)
        
        if new_order_id:
            self.position_data['binance_tp_order_id'] = new_order_id
            self._save_position_data()
        
        return new_order_id
