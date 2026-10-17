"""
币安交易精度处理工具模块

该模块提供自动化的精度检测和格式化功能，确保提交给币安API的参数
符合各交易对的精度限制要求，避免-1111错误。

使用方法:
    from common.precision_helper import PrecisionHelper
    
    # 初始化（传入ccxt交易所实例）
    helper = PrecisionHelper(exchange)
    
    # 格式化价格和数量
    formatted_price = helper.format_price('PIPPIN/USDT', 0.3092297799018887)
    formatted_qty = helper.format_quantity('PIPPIN/USDT', 39.22060807630761)
"""

import math
from decimal import Decimal, ROUND_DOWN
from typing import Optional, Dict, Any


class PrecisionHelper:
    """
    币安交易精度处理工具类
    
    功能:
    1. 自动从交易所获取各交易对的精度信息
    2. 根据精度要求格式化价格和数量
    3. 支持缓存机制避免重复查询
    4. 提供降级方案（使用默认精度）
    """
    
    # 默认精度配置（当无法获取交易所信息时使用）
    DEFAULT_PRICE_PRECISION = 6   # 默认价格精度：6位小数
    DEFAULT_QTY_PRECISION = 2     # 默认数量精度：2位小数
    
    def __init__(self, exchange=None):
        """
        初始化精度处理器
        
        Args:
            exchange: ccxt交易所实例（可选）
        """
        self.exchange = exchange
        self._precision_cache: Dict[str, Dict[str, int]] = {}
        self._step_cache: Dict[str, Dict[str, Decimal]] = {}  # tickSize/stepSize，用于按步长整数倍取整
        self._initialized = False
        
    def set_exchange(self, exchange):
        """设置交易所实例"""
        self.exchange = exchange
        self._initialized = False
        self._precision_cache.clear()
        self._step_cache.clear()
        
    def _load_market_precision(self, symbol: str) -> Dict[str, int]:
        """
        从交易所加载交易对的精度信息
        
        Args:
            symbol: 交易对符号，如 'PIPPIN/USDT'
            
        Returns:
            包含price和quantity精度的字典
        """
        result = {
            'price': self.DEFAULT_PRICE_PRECISION,
            'quantity': self.DEFAULT_QTY_PRECISION
        }
        
        if not self.exchange:
            print(f"[PrecisionHelper] 警告：交易所实例未设置，尝试从API获取精度")
            return self._fetch_precision_from_api(symbol, result)
            
        try:
            # 确保市场信息已加载
            if not self.exchange.markets:
                self.exchange.load_markets()
            
            # 尝试多种symbol格式查找
            # 优先使用期货格式(:USDT后缀)，因为交易的是期货市场
            market = None
            symbol_variants = [
                f"{symbol}:USDT",                 # PIPPIN/USDT:USDT (期货格式) - 优先
                symbol,                           # PIPPIN/USDT (现货格式)
                symbol.replace('/', ''),          # PIPPINUSDT
            ]
            
            for variant in symbol_variants:
                if variant in self.exchange.markets:
                    market = self.exchange.markets[variant]
                    print(f"[PrecisionHelper] 找到交易对: {variant}")
                    break
            
            if market:
                precision = market.get('precision', {})
                
                # 获取价格精度
                price_precision = precision.get('price')
                if price_precision is not None:
                    # ccxt(TICK_SIZE 模式)返回的是步长(如 1e-07)，不是小数位数
                    if isinstance(price_precision, int) and price_precision >= 1:
                        result['price'] = price_precision
                    else:
                        result['price'] = self._get_precision_from_step(price_precision)
                        self._step_cache.setdefault(symbol, {})['price'] = Decimal(str(price_precision))
                    
                # 获取数量精度
                qty_precision = precision.get('amount')
                if qty_precision is not None:
                    if isinstance(qty_precision, int) and qty_precision >= 1:
                        result['quantity'] = qty_precision
                    else:
                        result['quantity'] = self._get_precision_from_step(qty_precision)
                        self._step_cache.setdefault(symbol, {})['quantity'] = Decimal(str(qty_precision))
                    
                # 从filters中获取更精确的信息（如果可用）
                filters = market.get('info', {}).get('filters', [])
                for f in filters:
                    filter_type = f.get('filterType')
                    if filter_type == 'PRICE_FILTER':
                        tick_size = f.get('tickSize', '0.00001')
                        result['price'] = self._get_precision_from_step(tick_size)
                        self._step_cache.setdefault(symbol, {})['price'] = Decimal(str(tick_size))
                    elif filter_type == 'LOT_SIZE':
                        step_size = f.get('stepSize', '0.01')
                        result['quantity'] = self._get_precision_from_step(step_size)
                        self._step_cache.setdefault(symbol, {})['quantity'] = Decimal(str(step_size))
                        
                print(f"[PrecisionHelper] {symbol} 精度信息: 价格={result['price']}位, 数量={result['quantity']}位")
            else:
                # 从ccxt markets中未找到，尝试直接从API获取
                print(f"[PrecisionHelper] ccxt markets中未找到 {symbol}，尝试从API获取...")
                result = self._fetch_precision_from_api(symbol, result)
                
        except Exception as e:
            print(f"[PrecisionHelper] 加载精度信息失败: {e}，尝试从API获取...")
            result = self._fetch_precision_from_api(symbol, result)
            
        return result
    
    def _fetch_precision_from_api(self, symbol: str, default_result: Dict[str, int]) -> Dict[str, int]:
        """
        直接从币安API获取交易对精度信息
        
        Args:
            symbol: 交易对符号
            default_result: 默认精度字典
            
        Returns:
            包含精度信息的字典
        """
        import requests
        
        try:
            # 格式化symbol为API格式
            api_symbol = symbol.split(':')[0].replace('/', '')  # 兼容 'X/USDT:USDT' 期货统一写法
            
            # 调用币安期货API获取交易规则
            url = 'https://fapi.binance.com/fapi/v1/exchangeInfo'
            response = requests.get(url, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                symbols = data.get('symbols', [])
                
                for sym_info in symbols:
                    if sym_info.get('symbol') == api_symbol:
                        filters = sym_info.get('filters', [])
                        
                        for f in filters:
                            filter_type = f.get('filterType')
                            if filter_type == 'PRICE_FILTER':
                                tick_size = f.get('tickSize', '0.00001')
                                default_result['price'] = self._get_precision_from_step(tick_size)
                                self._step_cache.setdefault(symbol, {})['price'] = Decimal(str(tick_size))
                            elif filter_type == 'LOT_SIZE':
                                step_size = f.get('stepSize', '0.01')
                                default_result['quantity'] = self._get_precision_from_step(step_size)
                                self._step_cache.setdefault(symbol, {})['quantity'] = Decimal(str(step_size))
                        
                        print(f"[PrecisionHelper] 从API获取 {symbol} 精度: 价格={default_result['price']}位, 数量={default_result['quantity']}位")
                        return default_result
                
                print(f"[PrecisionHelper] API中未找到 {api_symbol}，使用默认精度")
            else:
                print(f"[PrecisionHelper] API请求失败: {response.status_code}，使用默认精度")
                
        except Exception as e:
            print(f"[PrecisionHelper] 从API获取精度失败: {e}，使用默认精度")
            
        return default_result
        
    def _get_precision_from_step(self, step: str) -> int:
        """
        从stepSize/tickSize计算精度位数
        
        Args:
            step: 步进值字符串，如 '0.00001' 或 '1'
            
        Returns:
            精度位数
        """
        try:
            step_decimal = Decimal(str(step)).normalize()
            if step_decimal <= 0:
                return self.DEFAULT_PRICE_PRECISION
            if step_decimal >= 1:
                return 0
            # 计算小数位数：使用指数而非字符串解析，
            # 避免 Decimal('0.0000001') 转为科学计数法 '1E-7' 时被误判为0位
            return max(0, -step_decimal.as_tuple().exponent)
        except Exception:
            return self.DEFAULT_PRICE_PRECISION
            
    def get_precision(self, symbol: str) -> Dict[str, int]:
        """
        获取交易对的精度信息（带缓存）
        
        Args:
            symbol: 交易对符号
            
        Returns:
            包含price和quantity精度的字典
        """
        if symbol not in self._precision_cache:
            self._precision_cache[symbol] = self._load_market_precision(symbol)
        return self._precision_cache[symbol]
        
    def format_price(self, symbol: str, price: float) -> float:
        """
        格式化价格，确保符合交易对的精度要求
        
        Args:
            symbol: 交易对符号
            price: 原始价格
            
        Returns:
            格式化后的价格
        """
        precision = self.get_precision(symbol)
        return float(self._round_down_decimal(price, precision['price'], self._get_step(symbol, 'price')))
        
    def format_quantity(self, symbol: str, quantity: float) -> float:
        """
        格式化数量，确保符合交易对的精度要求
        
        Args:
            symbol: 交易对符号
            quantity: 原始数量
            
        Returns:
            格式化后的数量
        """
        precision = self.get_precision(symbol)
        return float(self._round_down_decimal(quantity, precision['quantity'], self._get_step(symbol, 'quantity')))
        
    def _round_down(self, value: float, precision: int) -> float:
        """
        向下取整到指定精度（避免因四舍五入导致超出持仓）
        
        Args:
            value: 原始值
            precision: 精度位数
            
        Returns:
            格式化后的值
        """
        return float(self._round_down_decimal(value, precision))

    def _get_step(self, symbol: str, kind: str) -> Optional[Decimal]:
        """获取交易对的 tickSize(kind='price') 或 stepSize(kind='quantity')，未知时返回 None"""
        return self._step_cache.get(symbol, {}).get(kind)

    def _round_down_decimal(self, value, precision: int, step: Optional[Decimal] = None) -> Decimal:
        """
        使用 Decimal 向下取整，避免 math.floor(value * 10^p) 的浮点误差（如 1.13 -> 1.12）

        Args:
            value: 原始值（支持 float / numpy.float64 / str）
            precision: 精度位数
            step: 步长(tickSize/stepSize)，提供时取整到步长的整数倍（如 tickSize=0.05）
        """
        value_decimal = Decimal(str(value))
        if not value_decimal.is_finite():
            # NaN/inf 直接报错，避免把 'NaN' 作为触发价/数量提交给交易所
            raise ValueError(f"非有限数值无法格式化: {value}")
        if step is not None and not step.is_finite():
            raise ValueError('非有限精度步长无法格式化')
        if step is not None and step > 0:
            return (value_decimal / step).to_integral_value(rounding=ROUND_DOWN) * step
        return value_decimal.quantize(Decimal(1).scaleb(-max(precision, 0)), rounding=ROUND_DOWN)

    def _decimal_to_str(self, value: Decimal, precision: int) -> str:
        """按精度位数输出定点字符串（不使用科学计数法）"""
        return format(value.quantize(Decimal(1).scaleb(-max(precision, 0)), rounding=ROUND_DOWN), 'f')
        
    def format_price_str(self, symbol: str, price: float) -> str:
        """
        格式化价格并返回字符串（用于API请求）
        
        Args:
            symbol: 交易对符号
            price: 原始价格
            
        Returns:
            格式化后的价格字符串
        """
        precision = self.get_precision(symbol)['price']
        formatted = self._round_down_decimal(price, precision, self._get_step(symbol, 'price'))
        return self._decimal_to_str(formatted, precision)
        
    def format_quantity_str(self, symbol: str, quantity: float) -> str:
        """
        格式化数量并返回字符串（用于API请求）
        
        Args:
            symbol: 交易对符号
            quantity: 原始数量
            
        Returns:
            格式化后的数量字符串
        """
        precision = self.get_precision(symbol)['quantity']
        formatted = self._round_down_decimal(quantity, precision, self._get_step(symbol, 'quantity'))
        return self._decimal_to_str(formatted, precision)


# 全局单例实例
_precision_helper_instance: Optional[PrecisionHelper] = None


def get_precision_helper(exchange=None) -> PrecisionHelper:
    """
    获取精度处理器的单例实例
    
    Args:
        exchange: ccxt交易所实例（首次调用时需要提供）
        
    Returns:
        PrecisionHelper实例
    """
    global _precision_helper_instance
    
    if _precision_helper_instance is None:
        _precision_helper_instance = PrecisionHelper(exchange)
    elif exchange is not None and _precision_helper_instance.exchange != exchange:
        _precision_helper_instance.set_exchange(exchange)
        
    return _precision_helper_instance
