"""
移动止损管理器
基于6K线法则和ATR的移动止损系统
"""
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
import pandas as pd
import numpy as np
import sys

# 添加项目根目录到 Python 路径
project_root = str(Path(__file__).parent.parent)
if project_root not in sys.path:
    sys.path.append(project_root)

from bit.common.time_utils import TimeInfo, TimeSource, TimePriorityManager, parse_time_to_timestamp


class TrailingStopManager:
    """
    移动止损管理器
    
    核心功能：
    1. 基于6K线法则计算止损价格
    2. 结合ATR动态调整止损位
    3. 实现移动止损价格更新机制
    4. 自动触发止损平仓
    """
    
    def __init__(self, symbol, timeframe, data_dir='bit/data/bit_positions', exchange=None):
        """
        初始化移动止损管理器
        
        Args:
            symbol: 交易币种，如 'SPK/USDT'
            timeframe: 时间周期，如 '5m', '1m'
            data_dir: 数据存储目录
            exchange: ccxt交易所实例（用于设置币安止损单）
        """
        self.symbol = symbol
        self.timeframe = timeframe
        self.data_dir = data_dir
        self.exchange = exchange  # 币安交易所实例
        if exchange is None:
            # 没有 exchange 就无法创建交易所侧 STOP_MARKET，止损只剩本地判断：
            # 进程退出、被 kill 或断网期间仓位完全没有保护。必须让人看见。
            import warnings
            message = (
                '%s %s: TrailingStopManager 未注入 exchange，将无法创建交易所侧止损单；'
                '止损仅在本进程存活时有效。' % (self.__class__.__name__, symbol))
            warnings.warn(message, RuntimeWarning, stacklevel=2)
            print('⚠️  ' + message)
        
        # 确保数据目录存在
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        
        # 持仓追踪文件路径
        safe_symbol = symbol.replace('/', '_')
        self.position_file = os.path.join(
            data_dir, 
            f'trailing_stop_{safe_symbol}_{timeframe}.json'
        )
        
        # 加载或初始化持仓追踪数据
        self.position_data = self._load_position_data()
        
    def _load_position_data(self):
        """加载持仓追踪数据"""
        if os.path.exists(self.position_file):
            try:
                with open(self.position_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    
                    # 验证和修正持仓方向数据
                    if data.get('has_position') and data.get('side'):
                        side = str(data['side']).lower()
                        # 确保 side 值只能是 'long' 或 'short'
                        if side not in ['long', 'short']:
                            print(f"[TrailingStop] 警告：检测到无效的持仓方向 '{data['side']}'，重置为空仓")
                            data['has_position'] = False
                            data['side'] = None
                        else:
                            data['side'] = side  # 统一为小写
                    
                    return data
            except Exception as e:
                print(f"[TrailingStop] 加载持仓数据失败: {e}")
        
        return {
            'has_position': False,
            'side': None,  # 'long' or 'short'
            'entry_time': None,
            'entry_price': None,
            'current_stop_loss': None,
            'stop_loss_history': [],
            'kline_cycle_count': 0,  # 已完成的6K线周期数
            'last_update_time': None,
            'binance_stop_order_id': None,  # 币安止损单ID
            'position_amount': None  # 持仓数量
        }
    
    def _save_position_data(self):
        """保存持仓追踪数据"""
        try:
            with open(self.position_file, 'w', encoding='utf-8') as f:
                json.dump(self.position_data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"[TrailingStop] 保存持仓数据失败: {e}")
    
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
        entry_datetime = pd.to_datetime(entry_time)
        # 【关键修复】如果entry_datetime带有时区，移除时区信息
        if hasattr(entry_datetime, 'tz') and entry_datetime.tz is not None:
            entry_datetime = entry_datetime.tz_localize(None)
        
        # 【关键修复】找到开仓时间之后的第一根完整K线（不包含开仓时刻所在的那根K线）
        # 使用 > 而不是 >= ，因为开仓时刻所在的K线是不完整的
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
    
    def calculate_stop_loss_price(self, df, entry_time, side, cycle_number=0, atr_period=14):
        """
        计算止损价格（6K线法则 + ATR）
        
        Args:
            df: K线数据DataFrame
            entry_time: 开仓时间
            side: 持仓方向 'long' 或 'short'
            cycle_number: 第几个6K线周期
            atr_period: ATR计算周期
            
        Returns:
            dict: {
                'stop_loss': 止损价格,
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
            # 做多：找6根K线中的最低价
            min_idx = kline_6['low'].idxmin()
            extreme_price = kline_6.loc[min_idx, 'low']
            atr_value = atr_series.loc[min_idx] if min_idx in atr_series.index else atr_series.iloc[-1]
            
            # 止损价格 = 最低价 - ATR
            stop_loss = extreme_price - atr_value
            
        else:  # side == 'short'
            # 做空：找6根K线中的最高价
            max_idx = kline_6['high'].idxmax()
            extreme_price = kline_6.loc[max_idx, 'high']
            atr_value = atr_series.loc[max_idx] if max_idx in atr_series.index else atr_series.iloc[-1]
            
            # 止损价格 = 最高价 + ATR
            stop_loss = extreme_price + atr_value
        
        return {
            'stop_loss': stop_loss,
            'extreme_price': extreme_price,
            'atr_value': atr_value,
            'kline_data': kline_6
        }
    
    def on_position_opened(self, side, entry_price, entry_time=None, position_amount=None, time_info=None):
        """
        开仓时调用：记录持仓信息
        
        Args:
            side: 持仓方向 'long' 或 'short'
            entry_price: 开仓价格
            entry_time: 开仓时间，默认为当前时间（兼容旧接口）
            position_amount: 持仓数量
            time_info: TimeInfo对象，包含时间来源标记（优先使用）
        """
        # 时间优先级处理
        if time_info is not None:
            # 优先使用TimeInfo对象
            final_time_info = time_info
            entry_time_str = time_info.iso_format
            time_source = time_info.source.value
            print(f"[TrailingStop] 使用TimeInfo: {time_info}")
        elif entry_time is not None:
            # 兼容旧接口，将entry_time转换为TimeInfo
            timestamp = parse_time_to_timestamp(entry_time)
            final_time_info = TimeInfo(
                timestamp=timestamp,
                source=TimeSource.UNKNOWN,
                description="从旧接口转换"
            )
            entry_time_str = final_time_info.iso_format
            time_source = TimeSource.UNKNOWN.value
            print(f"[TrailingStop] 兼容旧接口，转换entry_time为TimeInfo")
        else:
            # 都为None，使用当前系统时间
            final_time_info = TimePriorityManager.create_system_time("开仓时间未提供，使用系统时间")
            entry_time_str = final_time_info.iso_format
            time_source = TimeSource.SYSTEM_TIME.value
            print(f"[TrailingStop] 无时间信息，使用系统时间")
        
        # 开仓前先重试撤销之前平仓时未撤销成功的止损单
        pending_cancel_ids = self._retry_pending_cancels()

        self.position_data = {
            'has_position': True,
            'side': side,
            'entry_time': entry_time_str,
            'entry_price': entry_price,
            'current_stop_loss': None,
            'stop_loss_history': [],
            'kline_cycle_count': 0,
            'last_update_time': entry_time_str,
            'binance_stop_order_id': None,
            'position_amount': position_amount,
            'pending_cancel_order_ids': pending_cancel_ids,
            # 新增：记录时间来源信息
            'time_source': time_source,
            'time_priority': final_time_info.priority,
            'time_description': final_time_info.description
        }
        
        self._save_position_data()
        
        print(f"[TrailingStop] 开仓记录: {side} @ {entry_price}, 数量: {position_amount}")
        print(f"[TrailingStop] 开仓时间: {entry_time_str} (来源: {time_source}, 优先级: {final_time_info.priority})")
        print(f"[TrailingStop] 时间描述: {final_time_info.description}")
    
    def on_position_closed(self):
        """
        平仓时调用：清除持仓信息，并取消币安止损单
        """
        print(f"[TrailingStop] 平仓记录: {self.position_data['side']} @ {self.position_data['entry_price']}")
        print(f"[TrailingStop] 最终止损价格: {self.position_data['current_stop_loss']}")
        print(f"[TrailingStop] 止损更新历史: {self.position_data['stop_loss_history']}")
        
        # 取消币安止损单（如果存在）；撤单失败时记录为待撤销订单，后续开仓或更新止损时重试，
        # 避免交易所残留无人跟踪的条件单（同方向再次开仓时可能被意外触发）
        pending_cancel_ids = list(self.position_data.get('pending_cancel_order_ids') or [])
        stop_order_id = self.position_data.get('binance_stop_order_id')
        if stop_order_id and not self._cancel_binance_stop_order(stop_order_id):
            pending_cancel_ids.append(stop_order_id)
            print(f"[TrailingStop] ⚠️ 止损单撤销失败，已记录为待撤销订单: {stop_order_id}")
        
        self.position_data = {
            'has_position': False,
            'side': None,
            'entry_time': None,
            'entry_price': None,
            'current_stop_loss': None,
            'stop_loss_history': [],
            'kline_cycle_count': 0,
            'last_update_time': None,
            'binance_stop_order_id': None,
            'position_amount': None,
            'pending_cancel_order_ids': pending_cancel_ids
        }
        
        self._save_position_data()
    
    def update_trailing_stop(self, df, current_price, atr_period=14):
        """
        更新移动止损价格
        
        Args:
            df: 最新的K线数据DataFrame
            current_price: 当前价格
            atr_period: ATR计算周期
            
        Returns:
            dict: {
                'stop_loss_triggered': 是否触发止损,
                'should_close': 是否应该平仓,
                'stop_loss_updated': 止损价格是否更新,
                'current_stop_loss': 当前止损价格,
                'message': 消息说明
            }
        """
        # 如果没有持仓，直接返回
        if not self.position_data['has_position']:
            return {
                'stop_loss_triggered': False,
                'should_close': False,
                'stop_loss_updated': False,
                'current_stop_loss': None,
                'message': '无持仓'
            }
        
        side = self.position_data['side']
        entry_time = self.position_data['entry_time']
        current_stop_loss = self.position_data['current_stop_loss']
        
        # 计算当前应该检查第几个6K线周期
        # 自动检测时间列名并转换为datetime
        df = self._ensure_datetime_column(df)
        
        # 检查datetime列是否存在
        if 'datetime' not in df.columns:
            return {
                'stop_loss_triggered': False,
                'should_close': False,
                'stop_loss_updated': False,
                'current_stop_loss': current_stop_loss,
                'message': '数据缺少时间列'
            }
        
        # 【关键修复】确保entry_time转换为无时区的datetime，并找到开仓后的第一根完整K线
        entry_datetime = pd.to_datetime(entry_time)
        # 如果entry_datetime带有时区，移除时区信息
        if hasattr(entry_datetime, 'tz') and entry_datetime.tz is not None:
            entry_datetime = entry_datetime.tz_localize(None)
        
        # 【关键修复】找到开仓时间之后的第一根完整K线（不包含开仓时刻所在的那根K线）
        # 使用 > 而不是 >= ，因为开仓时刻所在的K线是不完整的
        after_entry = df[df['datetime'] > entry_datetime]
        
        # 【调试日志】输出详细的时间信息
        print(f"[TrailingStop] 开仓时间: {entry_datetime} (type: {type(entry_datetime)})")
        if len(df) > 0:
            print(f"[TrailingStop] DataFrame时间范围: {df['datetime'].min()} 至 {df['datetime'].max()}")
            print(f"[TrailingStop] DataFrame最后时间类型: {type(df['datetime'].iloc[-1])}")
            # 显示开仓前后的K线时间，帮助诊断
            print(f"[TrailingStop] DataFrame最后5根K线时间: {list(df['datetime'].tail(5))}")
        print(f"[TrailingStop] 开仓后K线数量: {len(after_entry)}")
        if len(after_entry) > 0:
            print(f"[TrailingStop] 开仓后第一根K线时间: {after_entry['datetime'].iloc[0]}")
            print(f"[TrailingStop] 开仓后最后一根K线时间: {after_entry['datetime'].iloc[-1]}")
        
        if len(after_entry) == 0:
            return {
                'stop_loss_triggered': False,
                'should_close': False,
                'stop_loss_updated': False,
                'current_stop_loss': current_stop_loss,
                'message': '无最新K线数据'
            }
        
        # 计算已完成的6K线周期数
        completed_klines = len(after_entry)
        completed_cycles = completed_klines // 6
        
        # 【调试日志】输出周期计算信息
        print(f"[TrailingStop] 完整K线数: {completed_klines}, 完整周期数: {completed_cycles}")
        
        # 检查是否有新的完整6K线周期
        if completed_cycles > self.position_data['kline_cycle_count']:
            # 有新的6K线周期，计算新的止损价格
            result = self.calculate_stop_loss_price(
                df, entry_time, side, completed_cycles - 1, atr_period
            )
            
            if result is not None:
                new_stop_loss = result['stop_loss']
                
                # 判断是否应该更新止损价格
                should_update = False
                
                if current_stop_loss is None:
                    # 第一次设置止损价格
                    should_update = True
                    message = f'初始止损价格设置: {new_stop_loss:.6f}'
                else:
                    if side == 'long':
                        # 做多：新止损价格必须高于旧止损价格
                        if new_stop_loss > current_stop_loss:
                            should_update = True
                            message = f'做多止损上移: {current_stop_loss:.6f} -> {new_stop_loss:.6f}'
                        else:
                            message = f'做多止损未上移(新:{new_stop_loss:.6f} <= 旧:{current_stop_loss:.6f})'
                    else:  # short
                        # 做空：新止损价格必须低于旧止损价格
                        if new_stop_loss < current_stop_loss:
                            should_update = True
                            message = f'做空止损下移: {current_stop_loss:.6f} -> {new_stop_loss:.6f}'
                        else:
                            message = f'做空止损未下移(新:{new_stop_loss:.6f} >= 旧:{current_stop_loss:.6f})'
                
                # 【关键修复】无论止损价格是否更新，都要更新周期计数器和记录检查历史
                # 这样可以确保每个周期只检查一次，避免重复计算
                self.position_data['kline_cycle_count'] = completed_cycles
                self.position_data['last_update_time'] = datetime.now().isoformat()
                
                # 记录每个周期的检查结果（包括未更新的情况）
                self.position_data['stop_loss_history'].append({
                    'cycle': completed_cycles,
                    'stop_loss': new_stop_loss,
                    'applied': should_update,  # 标记是否实际应用了这个止损价格
                    'time': datetime.now().isoformat(),
                    'extreme_price': result['extreme_price'],
                    'atr_value': result['atr_value']
                })
                
                if should_update:
                    # 实际更新止损价格
                    self.position_data['current_stop_loss'] = new_stop_loss
                    
                    self._save_position_data()
                    
                    print(f"[TrailingStop] {message}")
                    print(f"[TrailingStop] 第{completed_cycles}个6K线周期完成")
                    print(f"[TrailingStop] 极值价格: {result['extreme_price']:.6f}, ATR: {result['atr_value']:.6f}")
                    
                    # 【关键功能】更新币安止损单
                    position_amount = self.position_data.get('position_amount')
                    if position_amount and self.exchange:
                        binance_order_id = self._update_binance_stop_order(side, new_stop_loss, position_amount)
                        if binance_order_id:
                            print(f"[TrailingStop] ✓ 币安止损单已更新为新价格: {new_stop_loss:.6f}")
                        else:
                            print(f"[TrailingStop] ⚠️  币安止损单更新失败，但本地止损价已更新")
                    else:
                        if not self.exchange:
                            print(f"[TrailingStop] ⚠️  交易所实例未初始化，无法设置币安止损单")
                        else:
                            print(f"[TrailingStop] ⚠️  持仓数量未知，无法设置币安止损单")
                    
                    current_stop_loss = new_stop_loss
                else:
                    # 止损价格未更新，但周期已检查
                    self._save_position_data()
                    
                    print(f"[TrailingStop] {message}")
                    print(f"[TrailingStop] 第{completed_cycles}个6K线周期完成（止损价格保持不变）")
                    print(f"[TrailingStop] 极值价格: {result['extreme_price']:.6f}, ATR: {result['atr_value']:.6f}")
        
        # 检查是否触发止损
        stop_loss_triggered = False
        should_close = False
        
        if current_stop_loss is not None:
            if side == 'long':
                # 做多：当前价格跌破止损价格
                if current_price <= current_stop_loss:
                    stop_loss_triggered = True
                    should_close = True
                    message = f'做多止损触发！当前价格 {current_price:.6f} <= 止损价格 {current_stop_loss:.6f}'
                    print(f"[TrailingStop] ⚠️  {message}")
            else:  # short
                # 做空：当前价格突破止损价格
                if current_price >= current_stop_loss:
                    stop_loss_triggered = True
                    should_close = True
                    message = f'做空止损触发！当前价格 {current_price:.6f} >= 止损价格 {current_stop_loss:.6f}'
                    print(f"[TrailingStop] ⚠️  {message}")
        
        return {
            'stop_loss_triggered': stop_loss_triggered,
            'should_close': should_close,
            'stop_loss_updated': completed_cycles > self.position_data['kline_cycle_count'],
            'current_stop_loss': current_stop_loss,
            'message': message if 'message' in locals() else '止损监控中',
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
    
    def save_position(self, position_data):
        """
        保存持仓数据（兼容 PositionClaimManager 接口）
        
        Args:
            position_data: 持仓数据字典，包含:
                - side: 'LONG' 或 'SHORT'（持仓方向）
                - amount: 持仓数量
                - entry_price: 开仓价格
                - entry_time: 开仓时间（时间戳或ISO格式字符串）
                - time_info: TimeInfo对象（可选）
        """
        # 修正：转换持仓方向格式 'LONG' -> 'long', 'SHORT' -> 'short'
        # 统一处理大小写以提高健壮性
        side_upper = str(position_data['side']).upper()
        side = 'long' if side_upper == 'LONG' else 'short'
        
        # 获取TimeInfo对象（如果有）
        time_info = position_data.get('time_info')
        
        if time_info is not None:
            # 使用TimeInfo对象
            print(f"[TrailingStop] 接管持仓：使用TimeInfo对象 (来源: {time_info.source.value})")
            self.on_position_opened(
                side=side,
                entry_price=position_data['entry_price'],
                position_amount=position_data['amount'],
                time_info=time_info
            )
        else:
            # 兼容旧接口：转换时间格式
            entry_time = position_data.get('entry_time')
            if isinstance(entry_time, (int, float)):
                entry_time = datetime.fromtimestamp(entry_time).isoformat()
            
            print(f"[TrailingStop] 接管持仓：使用旧接口时间")
            self.on_position_opened(
                side=side,
                entry_price=position_data['entry_price'],
                entry_time=entry_time,
                position_amount=position_data['amount']
            )
    
    def _ensure_datetime_column(self, df):
        """
        确保DataFrame有datetime列
        自动检测时间列名和格式，包括索引中的时间戳
        
        Args:
            df: K线数据DataFrame
            
        Returns:
            DataFrame: 包含datetime列的DataFrame（副本，避免修改原数据）
        """
        if df is None or df.empty:
            return df
        
        # 如果已经有datetime列，直接返回
        if 'datetime' in df.columns:
            # 确保datetime列是datetime类型，并移除时区信息
            if not pd.api.types.is_datetime64_any_dtype(df['datetime']):
                df = df.copy()  # 创建副本避免警告
                df['datetime'] = pd.to_datetime(df['datetime'])
            # 【关键修复】移除时区信息，统一使用无时区的datetime
            if hasattr(df['datetime'].dtype, 'tz') and df['datetime'].dtype.tz is not None:
                df = df.copy()
                df['datetime'] = df['datetime'].dt.tz_localize(None)
            return df
        
        # 创建副本以避免 SettingWithCopyWarning
        df = df.copy()
        
        # 【关键修复】首先检查索引是否是时间类型
        if pd.api.types.is_datetime64_any_dtype(df.index):
            # 索引本身就是datetime类型，直接使用
            # 【关键修复】先转换为本地时区（Asia/Singapore），再移除时区信息
            if hasattr(df.index, 'tz') and df.index.tz is not None:
                # 如果有时区信息，转换为新加坡时区后再移除
                df['datetime'] = df.index.tz_convert('Asia/Singapore').tz_localize(None)
                print(f"[TrailingStop] 从索引提取datetime列（{df.index.tz} -> Asia/Singapore -> 无时区）")
            else:
                # 如果没有时区信息，假定为UTC时间，先本地化为UTC，再转换为新加坡时区
                df['datetime'] = df.index.tz_localize('UTC').tz_convert('Asia/Singapore').tz_localize(None)
                print(f"[TrailingStop] 从索引提取datetime列（UTC -> Asia/Singapore -> 无时区）")
            return df
        elif df.index.name in ['time', 'timestamp'] or isinstance(df.index, pd.DatetimeIndex):
            # 索引名称表明它是时间，或者是DatetimeIndex
            try:
                df['datetime'] = pd.to_datetime(df.index)
                print(f"[TrailingStop] 从索引提取datetime列（索引名: {df.index.name}）")
                return df
            except Exception as e:
                print(f"[TrailingStop] 无法从索引转换datetime: {e}")
        
        # 检测时间列名：优先使用 'time'，其次 'timestamp'
        time_col = None
        if 'time' in df.columns:
            time_col = 'time'
        elif 'timestamp' in df.columns:
            time_col = 'timestamp'
        else:
            # 没有找到时间列，尝试从索引获取
            print(f"[TrailingStop] 警告：未找到时间列，尝试使用索引")
            try:
                df['datetime'] = pd.to_datetime(df.index)
                return df
            except Exception as e:
                print(f"[TrailingStop] 无法从索引创建datetime列: {e}")
                return df
        
        # 检测时间格式并转换
        try:
            # 尝试作为字符串解析（适用于 '2025-10-20 15:18' 格式）
            df['datetime'] = pd.to_datetime(df[time_col])
        except:
            try:
                # 尝试作为毫秒时间戳解析
                df['datetime'] = pd.to_datetime(df[time_col], unit='ms')
            except:
                try:
                    # 尝试作为秒时间戳解析
                    df['datetime'] = pd.to_datetime(df[time_col], unit='s')
                except Exception as e:
                    print(f"[TrailingStop] 无法解析时间列 '{time_col}': {e}")
                    return df
        
        return df
    
    def _create_binance_stop_order(self, side, stop_price, amount):
        """
        创建币安市价止损单（STOP_MARKET订单）
        
        Args:
            side: 持仓方向 'long' 或 'short'
            stop_price: 止损触发价格
            amount: 持仓数量
            
        Returns:
            str: 订单ID，如果失败返回None
        """
        if not self.exchange:
            print(f"[TrailingStop] 错误：交易所实例未初始化，无法创建止损单")
            return None
        
        try:
            # 币安止损单参数
            # - 做多止损：当价格跌破stop_price时，以市价卖出平仓
            # - 做空止损：当价格涨破stop_price时，以市价买入平仓
            order_side = 'sell' if side == 'long' else 'buy'  # 平仓方向
            
            # 【关键修复】添加positionSide参数，支持双向持仓模式
            # 做多持仓：positionSide='LONG'
            # 做空持仓：positionSide='SHORT'
            position_side = 'LONG' if side == 'long' else 'SHORT'
            
            # 创建STOP_MARKET订单
            # 注意：双向持仓模式下，positionSide已经明确了平仓意图，不需要reduceOnly参数
            params = {
                'stopPrice': float(stop_price),  # 触发价格（转为原生float：ccxt 4.5.78 起不识别 numpy.float64）
                'type': 'STOP_MARKET',          # 止损市价单
                'positionSide': position_side   # 【关键】指定持仓方向（双向持仓模式必需）
            }
            
            print(f"[TrailingStop] 正在创建币安止损单...")
            print(f"[TrailingStop]   持仓方向: {side} (positionSide={position_side})")
            print(f"[TrailingStop]   止损价格: {stop_price:.6f}")
            print(f"[TrailingStop]   平仓数量: {amount}")
            print(f"[TrailingStop]   平仓方向: {order_side}")
            
            order = self.exchange.create_order(
                symbol=self.symbol,
                type='STOP_MARKET',
                side=order_side,
                amount=float(amount),
                params=params
            )
            
            order_id = order.get('id') or order.get('orderId')
            
            if order_id:
                print(f"[TrailingStop] ✓ 币安止损单创建成功！")
                print(f"[TrailingStop]   订单ID: {order_id}")
                print(f"[TrailingStop]   止损价: {stop_price:.6f}")
                print(f"[TrailingStop]   状态: {order.get('status', 'N/A')}")
                return str(order_id)
            else:
                print(f"[TrailingStop] ✗ 币安止损单创建失败：无法获取订单ID")
                return None
                
        except Exception as e:
            print(f"[TrailingStop] ✗ 创建币安止损单失败: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    @staticmethod
    def _is_missing_order_error(error):
        # Prefer Binance's numeric code over possibly misleading message text.
        import re
        message = str(error).lower()
        match = re.search(r'\bcode[\"\s\x27:]*(-\d+)\b', message)
        if match is None:
            match = re.search(r'(?<!\d)(-\d{4,5})(?!\d)', message)
        if match is not None:
            return int(match.group(1)) in (-2011, -2013, -20124)
        return any(text in message for text in ('unknown order', 'order does not exist', 'invalid algo id'))

    def _cancel_binance_stop_order(self, order_id):
        """
        取消币安止损单
        
        Args:
            order_id: 订单ID
            
        Returns:
            bool: 是否成功取消
        """
        if not self.exchange:
            print(f"[TrailingStop] 错误：交易所实例未初始化，无法取消止损单")
            return False
        
        if not order_id:
            return True  # 没有订单ID，视为成功
        
        try:
            print(f"[TrailingStop] 正在取消币安止损单 ID: {order_id}")
            
            # STOP_MARKET 止损单在币安已被路由为 Algo 条件单（返回 algoId），
            # 必须以 trigger=True 走 DELETE /fapi/v1/algoOrder 撤销，普通撤单接口撤不掉，会导致止损单叠加
            try:
                result = self.exchange.cancel_order(
                    id=order_id,
                    symbol=self.symbol,
                    params={'trigger': True}
                )
            except Exception as algo_error:
                # 兼容旧版本创建的普通止损单：Algo 撤单报"不存在"时再尝试普通撤单
                if self._is_missing_order_error(algo_error):
                    result = self.exchange.cancel_order(
                        id=order_id,
                        symbol=self.symbol
                    )
                else:
                    raise
            
            print(f"[TrailingStop] ✓ 币安止损单已取消: {order_id}")
            return True
            
        except Exception as e:
            # 如果订单已经被执行或不存在，也视为成功
            if self._is_missing_order_error(e):
                print(f"[TrailingStop] 止损单不存在或已执行: {order_id}")
                return True
            
            print(f"[TrailingStop] ✗ 取消币安止损单失败: {e}")
            return False
    
    def _retry_pending_cancels(self):
        """
        重试撤销之前平仓时撤单失败的止损单

        Returns:
            list: 仍未撤销成功的订单ID
        """
        remaining_ids = []
        for order_id in list(self.position_data.get('pending_cancel_order_ids') or []):
            if self._cancel_binance_stop_order(order_id):
                print(f"[TrailingStop] ✓ 待撤销止损单已撤销: {order_id}")
            else:
                remaining_ids.append(order_id)
        return remaining_ids

    def _update_binance_stop_order(self, side, new_stop_price, amount):
        """
        更新币安止损单（先取消旧的，再创建新的）
        
        Args:
            side: 持仓方向 'long' 或 'short'
            new_stop_price: 新的止损价格
            amount: 持仓数量
            
        Returns:
            str: 新订单ID，如果失败返回None
        """
        old_order_id = self.position_data.get('binance_stop_order_id')
        
        # 先取消旧的止损单
        if old_order_id:
            print(f"[TrailingStop] 移动止损触发：需要更新币安止损单")
            print(f"[TrailingStop]   旧止损价: {self.position_data.get('current_stop_loss', 'N/A')}")
            print(f"[TrailingStop]   新止损价: {new_stop_price:.6f}")
            
            if not self._cancel_binance_stop_order(old_order_id):
                print(f"[TrailingStop] ✗ 取消旧止损单失败，中止更新")
                return None
        
        # 重试撤销之前未撤销成功的止损单
        if self.position_data.get('pending_cancel_order_ids'):
            self.position_data['pending_cancel_order_ids'] = self._retry_pending_cancels()

        # 创建新的止损单
        new_order_id = self._create_binance_stop_order(side, new_stop_price, amount)
        
        if new_order_id:
            self.position_data['binance_stop_order_id'] = new_order_id
            self._save_position_data()
        
        return new_order_id
