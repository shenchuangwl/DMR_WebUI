"""
动态阶梯止盈管理器
基于Yang-Zhang波动率的4阶梯止盈系统

核心原理：
1. 使用Yang-Zhang综合法计算短周期波动率（最精确的波动率估计方法）
2. 阶梯单位 = 波动率 × 4
3. 在持仓建立后首次计算，一次性设定全部4个阶梯止盈价
4. 阶梯止盈价一经设定不再更新（区别于移动止盈的动态跟踪）
5. 与移动止盈机制独立运行，各自维护状态，互不干扰

阶梯止盈规则：
| 阶梯序号 | 止盈距离倍数 | 止盈仓位比例 |
|---------|-------------|-------------|
| 第1阶梯 | 1 × 阶梯单位 | 25% |
| 第2阶梯 | 2 × 阶梯单位 | 25% |
| 第3阶梯 | 3 × 阶梯单位 | 25% |
| 第4阶梯 | 4 × 阶梯单位 | 25% |
"""
import json
import os
import time
import hmac
import hashlib
import requests
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
import pandas as pd
import numpy as np

# 导入精度处理工具
from common.precision_helper import get_precision_helper


class LadderTakeProfitManager:
    """
    动态阶梯止盈管理器
    
    核心功能：
    1. 基于Yang-Zhang波动率计算阶梯止盈价格
    2. 4个阶梯各止盈25%仓位
    3. 阶梯止盈价一经设定不再更新
    4. 向币安交易所同步挂设止盈订单
    5. 与移动止盈机制独立运行
    
    算法规则：
    - 做多：止盈价 = 基准价格 + N × (波动率 × 4)，其中N=1,2,3,4
    - 做空：止盈价 = 基准价格 - N × (波动率 × 4)，其中N=1,2,3,4
    - 基准价格 = 首次计算时过去6根K线的最高/最低价
    """
    
    # 阶梯配置：每个阶梯止盈25%的当前剩余仓位
    LADDER_CONFIG = [
        {'level': 1, 'multiplier': 1, 'close_ratio': 0.25},  # 第1阶梯：1倍波动单位，平25%
        {'level': 2, 'multiplier': 2, 'close_ratio': 0.25},  # 第2阶梯：2倍波动单位，平25%
        {'level': 3, 'multiplier': 3, 'close_ratio': 0.25},  # 第3阶梯：3倍波动单位，平25%
        {'level': 4, 'multiplier': 4, 'close_ratio': 0.25},  # 第4阶梯：4倍波动单位，平25%
    ]
    
    # 波动率倍数（阶梯单位 = 波动率 × 此倍数）
    VOLATILITY_MULTIPLIER = 4
    
    # Yang-Zhang波动率计算窗口
    YZ_WINDOW = 26
    
    # 基准价格计算所用K线数
    KLINE_PERIOD = 6
    
    def __init__(self, symbol, timeframe, data_dir='data/first_positions', exchange=None):
        """
        初始化动态阶梯止盈管理器
        
        Args:
            symbol: 交易币种，如 'RVN/USDT'
            timeframe: 时间周期，如 '15m', '1h'
            data_dir: 数据存储目录
            exchange: ccxt交易所实例（用于设置币安止盈单）
        """
        self.symbol = symbol
        self.timeframe = timeframe
        self.data_dir = data_dir
        self.exchange = exchange
        
        # 确保数据目录存在
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        
        # 持仓追踪文件路径
        safe_symbol = symbol.replace('/', '_')
        self.position_file = os.path.join(
            data_dir, 
            f'ladder_take_profit_{safe_symbol}_{timeframe}.json'
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
                print(f"[LadderTakeProfit] 加载持仓数据失败: {e}")
        
        return self._get_initial_position_data()
    
    def _get_initial_position_data(self):
        """获取初始持仓数据结构"""
        return {
            'has_position': False,
            'side': None,  # 'long' or 'short'
            'entry_time': None,
            'entry_price': None,
            'initial_amount': None,  # 初始持仓数量
            'remaining_amount': None,  # 剩余持仓数量
            'base_price': None,  # 基准价格（6K线最高/最低价）
            'volatility': None,  # Yang-Zhang波动率
            'ladder_unit': None,  # 阶梯单位 = 波动率 × 4
            'ladders': [],  # 4个阶梯的详细信息
            'ladder_orders': [],  # 币安止盈订单ID列表
            'executed_ladders': [],  # 已执行的阶梯
            'initialized': False,  # 阶梯是否已初始化
            'last_update_time': None
        }
    
    def _save_position_data(self):
        """保存持仓追踪数据"""
        try:
            with open(self.position_file, 'w', encoding='utf-8') as f:
                json.dump(self.position_data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"[LadderTakeProfit] 保存持仓数据失败: {e}")
    
    def calculate_yang_zhang_volatility(self, df, window=26):
        """
        计算Yang-Zhang波动率
        综合考虑跳空波动和日内波动，是最精确的波动率估计方法
        
        Args:
            df: K线数据DataFrame，需包含 open, high, low, close 列
            window: 计算窗口，默认26根K线
            
        Returns:
            float: 周期波动率（未年化）
        """
        if df is None or len(df) < window + 1:
            print(f"[LadderTakeProfit] 数据不足，无法计算Yang-Zhang波动率（需要{window+1}根K线，当前{len(df) if df is not None else 0}根）")
            return None
        
        try:
            # 对数收益率
            log_oc = np.log(df['open'] / df['close'].shift(1))  # 跳空收益率
            log_co = np.log(df['close'] / df['open'])  # 日内收益率
            
            # Rogers-Satchell部分
            log_ho = np.log(df['high'] / df['open'])
            log_lo = np.log(df['low'] / df['open'])
            log_co_2 = np.log(df['close'] / df['open'])
            
            # 计算各部分方差
            # 隔夜方差（跳空）
            overnight_var = log_oc.rolling(window=window).var()
            
            # 开盘方差
            open_var = log_co.rolling(window=window).var()
            
            # Rogers-Satchell方差
            rs = log_ho * (log_ho - log_co_2) + log_lo * (log_lo - log_co_2)
            rs_var = rs.rolling(window=window).mean()
            
            # Yang-Zhang组合
            # k系数，通常取0.34
            k = 0.34 / (1.34 + (window + 1) / (window - 1))
            
            # 综合方差
            yz_var = overnight_var + k * open_var + (1 - k) * rs_var
            
            # 周期波动率
            period_vol = np.sqrt(yz_var)
            
            # 返回最新值
            current_vol = period_vol.iloc[-1]
            
            if pd.isna(current_vol):
                print(f"[LadderTakeProfit] Yang-Zhang波动率计算结果为NaN")
                return None
            
            print(f"[LadderTakeProfit] Yang-Zhang波动率计算完成: {current_vol:.8f} ({current_vol*100:.4f}%)")
            return current_vol
            
        except Exception as e:
            print(f"[LadderTakeProfit] 计算Yang-Zhang波动率失败: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def get_base_price(self, df, side, kline_period=6):
        """
        获取基准价格（过去N根K线的最高/最低价）
        
        Args:
            df: K线数据DataFrame
            side: 持仓方向 'long' 或 'short'
            kline_period: K线周期数，默认6根
            
        Returns:
            float: 基准价格
        """
        if df is None or len(df) < kline_period:
            print(f"[LadderTakeProfit] 数据不足，无法计算基准价格（需要{kline_period}根K线）")
            return None
        
        # 获取最近N根K线
        recent_klines = df.tail(kline_period)
        
        if side == 'long':
            # 做多：使用最高价作为基准
            base_price = recent_klines['high'].max()
            print(f"[LadderTakeProfit] 做多基准价格: {base_price:.6f} (过去{kline_period}根K线最高价)")
        else:
            # 做空：使用最低价作为基准
            base_price = recent_klines['low'].min()
            print(f"[LadderTakeProfit] 做空基准价格: {base_price:.6f} (过去{kline_period}根K线最低价)")
        
        return base_price
    
    def calculate_ladder_prices(self, base_price, volatility, side):
        """
        计算4个阶梯的止盈价格
        
        Args:
            base_price: 基准价格
            volatility: Yang-Zhang波动率
            side: 持仓方向 'long' 或 'short'
            
        Returns:
            list: 4个阶梯的详细信息
        """
        # 阶梯单位 = 波动率 × 4
        ladder_unit = volatility * self.VOLATILITY_MULTIPLIER * base_price
        
        ladders = []
        
        for config in self.LADDER_CONFIG:
            level = config['level']
            multiplier = config['multiplier']
            close_ratio = config['close_ratio']
            
            if side == 'long':
                # 做多：止盈价 = 基准价格 + N × 阶梯单位
                take_profit_price = base_price + multiplier * ladder_unit
            else:
                # 做空：止盈价 = 基准价格 - N × 阶梯单位
                take_profit_price = base_price - multiplier * ladder_unit
            
            ladder = {
                'level': level,
                'multiplier': multiplier,
                'close_ratio': close_ratio,
                'take_profit_price': take_profit_price,
                'distance_from_base': multiplier * ladder_unit,
                'distance_percent': (multiplier * ladder_unit / base_price) * 100,
                'status': 'pending',  # pending, triggered, executed
                'binance_order_id': None,
                'triggered_time': None,
                'executed_time': None,
                'executed_price': None,
                'executed_amount': None
            }
            ladders.append(ladder)
        
        return ladders, ladder_unit
    
    def on_position_opened(self, side, entry_price, entry_time=None, position_amount=None, df=None):
        """
        开仓时调用：记录持仓信息并初始化阶梯止盈
        
        Args:
            side: 持仓方向 'long' 或 'short'
            entry_price: 开仓价格
            entry_time: 开仓时间，默认为当前时间
            position_amount: 持仓数量
            df: K线数据DataFrame（用于计算波动率和基准价格）
        """
        if entry_time is None:
            entry_time = datetime.now().isoformat()
        
        self.position_data = {
            'has_position': True,
            'side': side,
            'entry_time': entry_time,
            'entry_price': entry_price,
            'initial_amount': position_amount,
            'remaining_amount': position_amount,
            'base_price': None,
            'volatility': None,
            'ladder_unit': None,
            'ladders': [],
            'ladder_orders': [],
            'executed_ladders': [],
            'initialized': False,
            'last_update_time': entry_time
        }
        
        self._save_position_data()
        
        print(f"[LadderTakeProfit] 开仓记录: {side} @ {entry_price}, 数量: {position_amount}, 时间: {entry_time}")
        
        # 如果有K线数据，立即初始化阶梯止盈
        if df is not None and len(df) >= self.YZ_WINDOW + 1:
            self.initialize_ladders(df)
    
    def initialize_ladders(self, df):
        """
        初始化阶梯止盈（仅在首次调用时执行）
        
        Args:
            df: K线数据DataFrame
            
        Returns:
            bool: 初始化是否成功
        """
        if not self.position_data['has_position']:
            print(f"[LadderTakeProfit] 无持仓，无法初始化阶梯止盈")
            return False
        
        if self.position_data['initialized']:
            print(f"[LadderTakeProfit] 阶梯止盈已初始化，无需重复初始化")
            return True
        
        side = self.position_data['side']
        initial_amount = self.position_data['initial_amount']
        
        # 1. 计算Yang-Zhang波动率
        volatility = self.calculate_yang_zhang_volatility(df, window=self.YZ_WINDOW)
        if volatility is None:
            print(f"[LadderTakeProfit] 波动率计算失败，阶梯止盈初始化失败")
            return False
        
        # 2. 获取基准价格
        base_price = self.get_base_price(df, side, kline_period=self.KLINE_PERIOD)
        if base_price is None:
            print(f"[LadderTakeProfit] 基准价格计算失败，阶梯止盈初始化失败")
            return False
        
        # 3. 计算4个阶梯的止盈价格
        ladders, ladder_unit = self.calculate_ladder_prices(base_price, volatility, side)
        
        # 4. 计算每个阶梯的止盈数量（递减计算）
        remaining = initial_amount
        for ladder in ladders:
            close_amount = remaining * ladder['close_ratio']
            ladder['close_amount'] = close_amount
            remaining = remaining - close_amount
        
        # 5. 更新持仓数据
        self.position_data['base_price'] = base_price
        self.position_data['volatility'] = volatility
        self.position_data['ladder_unit'] = ladder_unit
        self.position_data['ladders'] = ladders
        self.position_data['initialized'] = True
        self.position_data['last_update_time'] = datetime.now().isoformat()
        
        self._save_position_data()
        
        # 6. 打印阶梯止盈信息
        print(f"\n[LadderTakeProfit] ========== 阶梯止盈初始化完成 ==========")
        print(f"[LadderTakeProfit] 持仓方向: {side.upper()}")
        print(f"[LadderTakeProfit] 入场价格: {self.position_data['entry_price']:.6f}")
        print(f"[LadderTakeProfit] 基准价格: {base_price:.6f}")
        print(f"[LadderTakeProfit] Yang-Zhang波动率: {volatility:.8f} ({volatility*100:.4f}%)")
        print(f"[LadderTakeProfit] 阶梯单位: {ladder_unit:.6f} ({ladder_unit/base_price*100:.4f}%)")
        print(f"[LadderTakeProfit] 初始仓位: {initial_amount}")
        print(f"[LadderTakeProfit]")
        print(f"[LadderTakeProfit] 阶梯止盈价格:")
        
        for ladder in ladders:
            level = ladder['level']
            tp_price = ladder['take_profit_price']
            close_amount = ladder['close_amount']
            distance_pct = ladder['distance_percent']
            print(f"[LadderTakeProfit]   第{level}阶梯: {tp_price:.6f} (距基准+{distance_pct:.2f}%), 止盈数量: {close_amount:.4f}")
        
        print(f"[LadderTakeProfit] =============================================\n")
        
        # 7. 向币安挂设止盈订单
        if self.exchange:
            self._place_ladder_orders()
        else:
            print(f"[LadderTakeProfit] ⚠️  交易所实例未初始化，无法挂设币安止盈单")
        
        return True
    
    def _place_ladder_orders(self):
        """
        向币安交易所挂设4个阶梯止盈订单
        
        重要：为避免连续请求导致服务器超时，每个订单之间添加延迟
        参照移动止盈止损的实现方式，确保单个订单创建稳定性
        """
        if not self.exchange:
            print(f"[LadderTakeProfit] 错误：交易所实例未初始化，无法挂设止盈单")
            return
        
        side = self.position_data['side']
        ladders = self.position_data['ladders']
        
        # 订单间延迟时间（秒），避免连续请求导致服务器超时
        ORDER_DELAY_SECONDS = 2
        
        print(f"[LadderTakeProfit] 开始向币安挂设{len(ladders)}个阶梯止盈订单...")
        print(f"[LadderTakeProfit] 注意：每个订单间隔{ORDER_DELAY_SECONDS}秒，避免API超时")
        
        success_count = 0
        fail_count = 0
        
        for i, ladder in enumerate(ladders):
            if ladder['status'] != 'pending':
                continue
            
            # 第一个订单之后添加延迟，避免连续请求导致超时
            if i > 0:
                print(f"[LadderTakeProfit] 等待{ORDER_DELAY_SECONDS}秒后创建下一个订单...")
                time.sleep(ORDER_DELAY_SECONDS)
            
            level = ladder['level']
            tp_price = ladder['take_profit_price']
            close_amount = ladder['close_amount']
            
            order_id = self._create_binance_tp_order(side, tp_price, close_amount, level)
            
            if order_id:
                ladder['binance_order_id'] = order_id
                self.position_data['ladder_orders'].append(order_id)
                print(f"[LadderTakeProfit] ✓ 第{level}阶梯止盈单已挂设: 价格={tp_price:.6f}, 数量={close_amount:.4f}, 订单ID={order_id}")
                success_count += 1
            else:
                print(f"[LadderTakeProfit] ✗ 第{level}阶梯止盈单挂设失败")
                fail_count += 1
        
        print(f"[LadderTakeProfit] 阶梯止盈订单挂设完成: 成功={success_count}, 失败={fail_count}")
        self._save_position_data()
    
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
            print(f"[LadderTakeProfit] 获取API凭证失败: {e}")
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
    
    def _create_binance_algo_order(self, side, tp_price, amount, level, order_type='TAKE_PROFIT_MARKET', max_retries=3):
        """
        使用币安Algo Order API创建条件订单（止盈/止损）
        
        币安API升级后，TAKE_PROFIT_MARKET和STOP_MARKET等条件订单
        必须通过Algo Order API (/fapi/v1/algoOrder) 来创建。
        
        包含重试机制：遇到超时(-1007)或网络错误时自动重试，使用指数退避策略。
        
        API端点: POST /fapi/v1/algoOrder
        
        Args:
            side: 持仓方向 'long' 或 'short'
            tp_price: 止盈/止损触发价格
            amount: 订单数量
            level: 阶梯级别（用于日志标识）
            order_type: 订单类型 'TAKE_PROFIT_MARKET' 或 'STOP_MARKET'
            max_retries: 最大重试次数，默认3次
            
        Returns:
            str: algoId（订单ID），如果失败返回None
        """
        api_key, api_secret = self._get_api_credentials()
        if not api_key or not api_secret:
            print(f"[LadderTakeProfit] ✗ 无法获取API凭证，无法创建Algo订单")
            return None
        
        try:
            # 确定平仓方向和持仓方向
            order_side = 'SELL' if side == 'long' else 'BUY'
            position_side = 'LONG' if side == 'long' else 'SHORT'
            
            # 格式化交易对符号（去除斜杠）
            symbol = self.symbol.replace('/', '')
            
            # 使用精度处理工具格式化价格和数量
            precision_helper = get_precision_helper(self.exchange)
            formatted_price = precision_helper.format_price_str(self.symbol, tp_price)
            formatted_qty = precision_helper.format_quantity_str(self.symbol, amount)
            print(f"[LadderTakeProfit] 精度格式化: 价格 {tp_price} -> {formatted_price}, 数量 {amount} -> {formatted_qty}")
            
            # 构建请求参数
            timestamp = int(time.time() * 1000)
            
            params = {
                'symbol': symbol,
                'side': order_side,
                'positionSide': position_side,
                'algoType': 'CONDITIONAL',  # Algo订单类型：条件单
                'type': order_type,  # 订单类型
                'quantity': formatted_qty,
                'triggerPrice': formatted_price,  # Algo Order API使用triggerPrice
                'workingType': 'CONTRACT_PRICE',  # 使用合约价格作为触发条件
                'priceProtect': 'false',  # 价格保护
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
            
            print(f"[LadderTakeProfit] 正在通过Algo Order API创建第{level}阶梯止盈单...")
            print(f"[LadderTakeProfit]   API端点: POST {endpoint}")
            print(f"[LadderTakeProfit]   持仓方向: {side} (positionSide={position_side})")
            print(f"[LadderTakeProfit]   订单类型(type): {order_type}")
            print(f"[LadderTakeProfit]   止盈价格(stopPrice): {tp_price}")
            print(f"[LadderTakeProfit]   平仓数量: {amount}")
            print(f"[LadderTakeProfit]   平仓方向: {order_side}")
            # 调试：打印完整请求参数（不含签名）
            debug_params = {k: v for k, v in params.items() if k != 'signature'}
            print(f"[LadderTakeProfit]   请求参数: {debug_params}")
            
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
                        print(f"[LadderTakeProfit] 第{level}阶梯第{attempt + 1}次重试，等待{wait_time}秒...")
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
                        print(f"[LadderTakeProfit] ✓ 第{level}阶梯Algo止盈单创建成功！")
                        print(f"[LadderTakeProfit]   algoId: {algo_id}")
                        print(f"[LadderTakeProfit]   algoType: {result.get('algoType', 'N/A')}")
                        print(f"[LadderTakeProfit]   algoStatus: {algo_status}")
                        print(f"[LadderTakeProfit]   triggerPrice: {result.get('triggerPrice', 'N/A')}")
                        return algo_id
                    else:
                        error_code = result.get('code', 'N/A')
                        error_msg = result.get('msg', str(result))
                        
                        # 判断是否为可重试错误
                        # -1007: 服务器超时
                        # -1001: 服务器断开连接
                        # -1000: 未知错误
                        retryable_errors = [-1007, -1001, -1000]
                        
                        if error_code in retryable_errors and attempt < max_retries - 1:
                            print(f"[LadderTakeProfit] ⚠ 第{level}阶梯遇到可重试错误: {error_code} - {error_msg}")
                            last_error = f"错误码:{error_code}, {error_msg}"
                            continue  # 继续重试
                        
                        print(f"[LadderTakeProfit] ✗ 第{level}阶梯Algo止盈单创建失败")
                        print(f"[LadderTakeProfit]   HTTP状态码: {response.status_code}")
                        print(f"[LadderTakeProfit]   错误代码: {error_code}")
                        print(f"[LadderTakeProfit]   错误信息: {error_msg}")
                        return None
                        
                except requests.exceptions.Timeout as e:
                    last_error = f"请求超时: {e}"
                    if attempt < max_retries - 1:
                        print(f"[LadderTakeProfit] ⚠ 第{level}阶梯请求超时，准备重试...")
                        continue
                    print(f"[LadderTakeProfit] ✗ 第{level}阶梯请求超时，已达最大重试次数")
                    
                except requests.exceptions.RequestException as e:
                    last_error = f"网络错误: {e}"
                    if attempt < max_retries - 1:
                        print(f"[LadderTakeProfit] ⚠ 第{level}阶梯网络错误，准备重试...")
                        continue
                    print(f"[LadderTakeProfit] ✗ 第{level}阶梯网络请求失败: {e}")
            
            # 所有重试都失败
            if last_error:
                print(f"[LadderTakeProfit] ✗ 第{level}阶梯Algo止盈单创建失败（重试{max_retries}次后）: {last_error}")
            return None
        except Exception as e:
            print(f"[LadderTakeProfit] ✗ 创建第{level}阶梯Algo止盈单失败: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def _create_binance_tp_order(self, side, tp_price, amount, level):
        """
        创建币安市价止盈单
        
        优先使用Algo Order API，如果失败则尝试传统API（作为降级方案）
        
        Args:
            side: 持仓方向 'long' 或 'short'
            tp_price: 止盈触发价格
            amount: 止盈数量
            level: 阶梯级别
            
        Returns:
            str: 订单ID（algoId或orderId），如果失败返回None
        """
        if not self.exchange:
            return None
        
        # 方案一：使用Algo Order API（推荐，适配币安API升级）
        print(f"[LadderTakeProfit] 尝试使用Algo Order API创建止盈单...")
        algo_id = self._create_binance_algo_order(side, tp_price, amount, level, 'TAKE_PROFIT_MARKET')
        
        if algo_id:
            return algo_id
        
        # 方案二：降级到传统API（兼容旧版本）
        print(f"[LadderTakeProfit] Algo Order API失败，尝试使用传统API...")
        try:
            order_side = 'sell' if side == 'long' else 'buy'
            position_side = 'LONG' if side == 'long' else 'SHORT'
            
            params = {
                'stopPrice': float(tp_price),  # 转为原生float：ccxt 4.5.78 起不识别 numpy.float64
                'type': 'TAKE_PROFIT_MARKET',
                'positionSide': position_side
            }
            
            print(f"[LadderTakeProfit] 正在通过传统API创建第{level}阶梯币安止盈单...")
            print(f"[LadderTakeProfit]   持仓方向: {side} (positionSide={position_side})")
            print(f"[LadderTakeProfit]   止盈价格: {tp_price:.6f}")
            print(f"[LadderTakeProfit]   平仓数量: {amount:.4f}")
            
            order = self.exchange.create_order(
                symbol=self.symbol,
                type='TAKE_PROFIT_MARKET',
                side=order_side,
                amount=amount,
                params=params
            )
            
            order_id = order.get('id') or order.get('orderId')
            
            if order_id:
                print(f"[LadderTakeProfit] ✓ 第{level}阶梯传统止盈单创建成功！订单ID: {order_id}")
                return str(order_id)
            else:
                print(f"[LadderTakeProfit] ✗ 第{level}阶梯传统止盈单创建失败：无法获取订单ID")
                return None
                
        except Exception as e:
            print(f"[LadderTakeProfit] ✗ 传统API也失败: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def _cancel_binance_algo_order(self, algo_id, level=None):
        """
        取消币安Algo订单
        
        API端点: DELETE /fapi/v1/algoOrder
        
        Args:
            algo_id: Algo订单ID
            level: 阶梯级别（用于日志）
            
        Returns:
            bool: 是否成功取消
        """
        api_key, api_secret = self._get_api_credentials()
        if not api_key or not api_secret:
            print(f"[LadderTakeProfit] ✗ 无法获取API凭证，无法取消Algo订单")
            return False
        
        try:
            level_str = f"第{level}阶梯" if level else ""
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
            
            print(f"[LadderTakeProfit] 正在取消{level_str}Algo止盈单 ID: {algo_id}")
            
            response = requests.delete(
                f"{base_url}{endpoint}",
                headers=headers,
                params=params,
                timeout=10
            )
            
            result = response.json()
            
            if response.status_code == 200:
                print(f"[LadderTakeProfit] ✓ {level_str}Algo止盈单已取消: {algo_id}")
                return True
            else:
                error_code = result.get('code', 'N/A')
                error_msg = result.get('msg', str(result))
                
                # 检查是否订单已不存在或已执行
                if error_code in [-2011, -2013, -20124]:  # 订单不存在/已完成；-2014/-2015 为密钥或权限错误，不能视为已撤销
                    print(f"[LadderTakeProfit] {level_str}Algo止盈单不存在或已执行: {algo_id}")
                    return True
                
                print(f"[LadderTakeProfit] ✗ 取消{level_str}Algo止盈单失败")
                print(f"[LadderTakeProfit]   错误代码: {error_code}")
                print(f"[LadderTakeProfit]   错误信息: {error_msg}")
                return False
                
        except requests.exceptions.RequestException as e:
            print(f"[LadderTakeProfit] ✗ 网络请求失败: {e}")
            return False
        except Exception as e:
            print(f"[LadderTakeProfit] ✗ 取消{level_str}Algo止盈单异常: {e}")
            return False
    
    def _cancel_binance_tp_order(self, order_id, level=None):
        """
        取消币安止盈单（同时支持Algo订单和传统订单）
        
        Args:
            order_id: 订单ID（algoId或orderId）
            level: 阶梯级别（用于日志）
            
        Returns:
            bool: 是否成功取消
        """
        if not self.exchange:
            return False
        
        if not order_id:
            return True
        
        level_str = f"第{level}阶梯" if level else ""
        
        # 方案一：尝试使用Algo Order API取消
        if self._cancel_binance_algo_order(order_id, level):
            return True
        
        # 方案二：尝试使用传统API取消
        try:
            print(f"[LadderTakeProfit] Algo取消失败，尝试使用传统API取消{level_str}止盈单 ID: {order_id}")
            
            self.exchange.cancel_order(
                id=order_id,
                symbol=self.symbol
            )
            
            print(f"[LadderTakeProfit] ✓ {level_str}传统止盈单已取消: {order_id}")
            return True
            
        except Exception as e:
            error_msg = str(e).lower()
            if 'unknown order' in error_msg or 'order does not exist' in error_msg:
                print(f"[LadderTakeProfit] {level_str}止盈单不存在或已执行: {order_id}")
                return True
            
            print(f"[LadderTakeProfit] ✗ 取消{level_str}止盈单全部失败: {e}")
            return False
    
    def check_ladder_triggers(self, current_price):
        """
        检查阶梯止盈触发条件（本地检查，作为币安订单的补充）
        
        Args:
            current_price: 当前价格
            
        Returns:
            dict: {
                'triggered': 是否有阶梯被触发,
                'triggered_ladders': 被触发的阶梯列表,
                'all_executed': 是否所有阶梯都已执行,
                'remaining_amount': 剩余仓位,
                'message': 消息说明
            }
        """
        if not self.position_data['has_position']:
            return {
                'triggered': False,
                'triggered_ladders': [],
                'all_executed': False,
                'remaining_amount': 0,
                'message': '无持仓'
            }
        
        if not self.position_data['initialized']:
            return {
                'triggered': False,
                'triggered_ladders': [],
                'all_executed': False,
                'remaining_amount': self.position_data.get('remaining_amount', 0),
                'message': '阶梯止盈尚未初始化'
            }
        
        side = self.position_data['side']
        ladders = self.position_data['ladders']
        triggered_ladders = []
        
        for ladder in ladders:
            if ladder['status'] != 'pending':
                continue
            
            level = ladder['level']
            tp_price = ladder['take_profit_price']
            
            # 检查触发条件
            triggered = False
            if side == 'long':
                # 做多：当前价格 >= 止盈价格
                if current_price >= tp_price:
                    triggered = True
            else:
                # 做空：当前价格 <= 止盈价格
                if current_price <= tp_price:
                    triggered = True
            
            if triggered:
                ladder['status'] = 'triggered'
                ladder['triggered_time'] = datetime.now().isoformat()
                triggered_ladders.append(ladder)
                
                print(f"[LadderTakeProfit] 🎯 第{level}阶梯止盈触发！当前价格: {current_price:.6f}, 止盈价格: {tp_price:.6f}")
        
        # 更新已执行阶梯和剩余仓位
        if triggered_ladders:
            for ladder in triggered_ladders:
                self.position_data['executed_ladders'].append(ladder['level'])
                self.position_data['remaining_amount'] -= ladder['close_amount']
            
            self.position_data['last_update_time'] = datetime.now().isoformat()
            self._save_position_data()
        
        # 检查是否所有阶梯都已执行
        all_executed = all(l['status'] != 'pending' for l in ladders)
        
        return {
            'triggered': len(triggered_ladders) > 0,
            'triggered_ladders': triggered_ladders,
            'all_executed': all_executed,
            'remaining_amount': self.position_data['remaining_amount'],
            'message': f"已触发{len(triggered_ladders)}个阶梯" if triggered_ladders else "无阶梯触发"
        }
    
    def on_position_closed(self):
        """
        平仓时调用：清除持仓信息，并取消所有未执行的币安止盈单
        """
        print(f"[LadderTakeProfit] 平仓记录: {self.position_data['side']} @ {self.position_data['entry_price']}")
        print(f"[LadderTakeProfit] 已执行阶梯: {self.position_data['executed_ladders']}")
        
        # 取消所有未执行的币安止盈单
        for ladder in self.position_data.get('ladders', []):
            if ladder['status'] == 'pending' and ladder.get('binance_order_id'):
                self._cancel_binance_tp_order(ladder['binance_order_id'], ladder['level'])
        
        # 重置持仓数据
        self.position_data = self._get_initial_position_data()
        self._save_position_data()
        
        print(f"[LadderTakeProfit] ✓ 阶梯止盈状态已清理")
    
    def get_position_info(self):
        """获取当前持仓和阶梯止盈信息"""
        return self.position_data.copy()
    
    def has_position(self):
        """是否有持仓"""
        return self.position_data['has_position']
    
    def is_initialized(self):
        """阶梯止盈是否已初始化"""
        return self.position_data.get('initialized', False)
    
    def get_ladder_summary(self):
        """
        获取阶梯止盈摘要信息
        
        Returns:
            dict: 阶梯止盈摘要
        """
        if not self.position_data['has_position']:
            return {'status': 'no_position'}
        
        if not self.position_data['initialized']:
            return {
                'status': 'not_initialized',
                'side': self.position_data['side'],
                'entry_price': self.position_data['entry_price'],
                'initial_amount': self.position_data['initial_amount']
            }
        
        ladders = self.position_data['ladders']
        pending_count = sum(1 for l in ladders if l['status'] == 'pending')
        triggered_count = sum(1 for l in ladders if l['status'] == 'triggered')
        executed_count = sum(1 for l in ladders if l['status'] == 'executed')
        
        return {
            'status': 'active',
            'side': self.position_data['side'],
            'entry_price': self.position_data['entry_price'],
            'base_price': self.position_data['base_price'],
            'volatility': self.position_data['volatility'],
            'volatility_percent': self.position_data['volatility'] * 100 if self.position_data['volatility'] else None,
            'ladder_unit': self.position_data['ladder_unit'],
            'initial_amount': self.position_data['initial_amount'],
            'remaining_amount': self.position_data['remaining_amount'],
            'remaining_percent': (self.position_data['remaining_amount'] / self.position_data['initial_amount'] * 100) if self.position_data['initial_amount'] else None,
            'pending_ladders': pending_count,
            'triggered_ladders': triggered_count,
            'executed_ladders': executed_count,
            'ladder_details': ladders
        }
    
    def update(self, df, current_price):
        """
        更新阶梯止盈状态（主更新方法）
        
        Args:
            df: K线数据DataFrame
            current_price: 当前价格
            
        Returns:
            dict: {
                'initialized': 是否已初始化,
                'triggered': 是否有阶梯被触发,
                'triggered_ladders': 被触发的阶梯列表,
                'all_executed': 是否所有阶梯都已执行,
                'should_close_remaining': 是否应该平掉剩余仓位（所有阶梯执行完毕）,
                'remaining_amount': 剩余仓位,
                'message': 消息说明
            }
        """
        if not self.position_data['has_position']:
            return {
                'initialized': False,
                'triggered': False,
                'triggered_ladders': [],
                'all_executed': False,
                'should_close_remaining': False,
                'remaining_amount': 0,
                'message': '无持仓'
            }
        
        # 如果尚未初始化，尝试初始化
        if not self.position_data['initialized']:
            init_success = self.initialize_ladders(df)
            if not init_success:
                return {
                    'initialized': False,
                    'triggered': False,
                    'triggered_ladders': [],
                    'all_executed': False,
                    'should_close_remaining': False,
                    'remaining_amount': self.position_data.get('remaining_amount', 0),
                    'message': '阶梯止盈初始化失败，数据不足'
                }
        
        # 检查阶梯触发
        result = self.check_ladder_triggers(current_price)
        
        return {
            'initialized': True,
            'triggered': result['triggered'],
            'triggered_ladders': result['triggered_ladders'],
            'all_executed': result['all_executed'],
            'should_close_remaining': result['all_executed'],
            'remaining_amount': result['remaining_amount'],
            'message': result['message']
        }
