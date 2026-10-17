from config.config import SYMBOL, POSITION_SIZE
import config.config as _cfg
import time
import sys
from pathlib import Path

# 账户设置改写开关：兼容未包含这些字段的旧版配置文件，缺省即关闭（不改写交易所账户设置）
APPLY_ACCOUNT_SETTINGS = getattr(_cfg, 'APPLY_ACCOUNT_SETTINGS', False)
APPLY_LEVERAGE = getattr(_cfg, 'APPLY_LEVERAGE', 10)
APPLY_MARGIN_TYPE = getattr(_cfg, 'APPLY_MARGIN_TYPE', 'CROSSED')

# 添加项目根目录到 Python 路径
project_root = str(Path(__file__).parent.parent)
if project_root not in sys.path:
    sys.path.append(project_root)

from data.data_fetcher import DataFetcher

class OrderExecutor:
    def __init__(self, exchange):
        # 初始化交易所对象
        self.exchange = exchange
        # 创建数据获取器用于时间同步
        self.data_fetcher = DataFetcher()
        # 获取交易对规则
        self.market_info = {}
        try:
            # 确保时间同步
            self.data_fetcher.sync_time(force=True)
            
            # # 添加时间戳参数
            # params = {
            #     'timestamp': self.data_fetcher.get_timestamp(),
            #     'recvWindow': 60000
            # }
            
            # markets = self.exchange.load_markets(params=params)
            # load_markets 方法不接受 timestamp 和 recvWindow 参数
            markets = self.exchange.load_markets()
            for symbol in [SYMBOL]:  # 可以扩展为多个交易对
                if symbol in markets:
                    self.market_info[symbol] = markets[symbol]
                    print(f"已加载{symbol}交易规则: 最小下单金额={markets[symbol].get('limits', {}).get('cost', {}).get('min', 'unknown')}")
        except Exception as e:
            print(f"加载市场信息失败: {e}")

    def place_market_order(self, side, amount):
        """
        下市价单
        :param side: 买入或卖出
        :param amount: 交易数量
        :return: 订单信息或None（如果下单失败）
        """
        try:
            # 确保时间同步
            self.data_fetcher.sync_time()
            
            # 添加时间戳参数
            params = {
                'timestamp': self.data_fetcher.get_timestamp(),
                'recvWindow': 60000
            }
            
            order = self.exchange.create_market_order(SYMBOL, side, amount, params=params)
            return order
        except Exception as e:
            print(f"Error placing market order: {e}")
            return None

    def place_limit_order(self, side, amount, price):
        """
        下限价单
        :param side: 买入或卖出
        :param amount: 交易数量
        :param price: 限价
        :return: 订单信息或None（如果下单失败）
        """
        try:
            # 确保时间同步
            self.data_fetcher.sync_time()
            
            # 添加时间戳参数
            params = {
                'timestamp': self.data_fetcher.get_timestamp(),
                'recvWindow': 60000
            }
            
            order = self.exchange.create_limit_order(SYMBOL, side, amount, price, params=params)
            return order
        except Exception as e:
            print(f"Error placing limit order: {e}")
            return None

    def initialize_trading_config(self, symbol):
        """初始化交易配置（改写账户杠杆与保证金模式）

        ⚠️ 高风险操作：本方法会改写交易所账户级别的设置，影响该账户下的
        其他持仓与风控。默认不执行，需显式开启：

            export DMR_APPLY_ACCOUNT_SETTINGS=1   # 确认后才写入
            export DMR_LEVERAGE=10                # 默认 10 倍
            export DMR_MARGIN_TYPE=CROSSED        # 默认全仓

        风险提示：10 倍杠杆的强平距离约为 5 倍的一半；CROSSED 全仓模式下
        单一仓位的亏损会波及整个账户余额。建议先在测试网验证。
        """
        if not APPLY_ACCOUNT_SETTINGS:
            print(f"跳过账户设置改写 {symbol}（未开启 DMR_APPLY_ACCOUNT_SETTINGS=1，"
                  f"将沿用账户当前的杠杆与保证金模式）")
            return

        print(f"⚠️ 即将改写账户设置 {symbol}: 杠杆={APPLY_LEVERAGE}倍, "
              f"保证金模式={APPLY_MARGIN_TYPE}（全仓爆仓将波及整个账户余额）")
        try:
            # 确保时间同步
            self.data_fetcher.sync_time(force=True)
            
            # 设置杠杆倍数（默认10倍，可由 DMR_LEVERAGE 覆盖）
            params = {
                'symbol': symbol.replace('/', ''),
                'leverage': APPLY_LEVERAGE,
                'timestamp': self.data_fetcher.get_timestamp(),
                'recvWindow': 60000
            }
            
            self.exchange.fapiPrivatePostLeverage(params)
            
            try:
                # 设置保证金模式为全仓模式
                params = {
                    'symbol': symbol.replace('/', ''),
                    'marginType': APPLY_MARGIN_TYPE,
                    'timestamp': self.data_fetcher.get_timestamp(),
                    'recvWindow': 60000
                }
                
                self.exchange.fapiPrivatePostMarginType(params)
            except Exception as e:
                # 如果是"No need to change margin type"错误，则忽略
                if "No need to change margin type" in str(e):
                    print(f"保证金类型已经设置为全仓模式，无需更改 {symbol}")
                else:
                    raise e
                    
            print(f"交易配置已初始化 {symbol}")
        except Exception as e:
            print(f"初始化交易配置错误: {e}")
    
    # 修改open_long方法中的价格设置部分
    def open_long(self, symbol, amount, price=None, order_type='LIMIT'):
        try:
            # 确保交易配置已初始化
            self.initialize_trading_config(symbol)
            
            # 确保时间同步
            self.data_fetcher.sync_time()
            
            # 添加时间戳参数
            base_params = {
                'timestamp': self.data_fetcher.get_timestamp(),
                'recvWindow': 60000
            }
            
            # 获取最新价格并计算数量
            ticker = self.exchange.fetch_ticker(symbol, params=base_params)
            market_price = ticker['last']
            
            # 确保下单金额至少为20 USDT
            actual_amount = max(amount, 20)
            quantity = round(actual_amount / market_price, 3)  # 将USDT转换为对应的数量
            
            # 确保下单金额正确
            if amount != POSITION_SIZE:
                print(f"Warning: Order amount adjusted to {POSITION_SIZE} USDT")
                amount = POSITION_SIZE
                quantity = round(amount / market_price, 3)
            
            # 合并参数
            order_params = {
                'positionSide': 'LONG',
                **base_params
            }
            
            # 根据订单类型执行不同的下单逻辑
            if order_type.upper() == 'MARKET':
                order = self.exchange.create_market_order(
                    symbol=symbol,
                    side='BUY',
                    amount=quantity,
                    params=order_params
                )
                print(f"成功开多仓（市价单）: 金额={actual_amount}USDT, 数量={quantity}")
            else:  # 默认使用限价单
                # 如果没有提供价格，则使用当前市场价并添加适当的滑点
                if price is None:
                    # 买入时，价格应略低于市场价（例如下降0.5%）以增加成交概率
                    price = market_price * 0.995  # 买入价格略低于市场价
                    print(f"No price provided, using adjusted market price: {price}")
                
                # 使用策略提供的价格，不再调整
                limit_price = round(price, 4)  # 增加精度，使用4位小数
                
                # 再次确认订单价值满足最低要求
                notional = quantity * limit_price
                if notional < 20:
                    # 如果订单价值仍然小于20 USDT，调整数量
                    quantity = round(20 / limit_price, 3) + 0.001  # 加一点余量确保满足要求
                    print(f"调整下单数量以满足最低订单价值要求: {quantity}")
                
                order = self.exchange.create_limit_order(
                    symbol=symbol,
                    side='BUY',
                    amount=quantity,
                    price=limit_price,
                    params=order_params
                )
                print(f"成功开多仓（限价单）: 金额={actual_amount}USDT, 数量={quantity}, 价格={limit_price}")
            
            return order
        except Exception as e:
            error_msg = str(e)
            if "insufficient balance" in error_msg.lower():
                print(f"开多仓失败: 余额不足，请确保账户有足够的USDT")
            elif "MIN_NOTIONAL" in error_msg or "notional" in error_msg:
                print(f"开多仓失败: 订单金额低于最小要求，请增加下单金额")
            elif "LOT_SIZE" in error_msg:
                print(f"开多仓失败: 下单数量不符合规则，请调整下单数量")
            elif "Timestamp for this request" in error_msg:
                print(f"开多仓失败: 时间戳错误，尝试重新同步时间")
                self.data_fetcher.sync_time(force=True)
            else:
                print(f"开多仓失败: {e}")
            return None

    # 修改open_short方法中的价格设置部分
    def open_short(self, symbol, amount, price=None, order_type='LIMIT'):
        try:
            # 确保交易配置已初始化
            self.initialize_trading_config(symbol)
            
            # 确保时间同步
            self.data_fetcher.sync_time()
            
            # 添加时间戳参数
            base_params = {
                'timestamp': self.data_fetcher.get_timestamp(),
                'recvWindow': 60000
            }
            
            # 获取最新价格并计算数量
            ticker = self.exchange.fetch_ticker(symbol, params=base_params)
            market_price = ticker['last']
            
            # 确保下单金额至少为20 USDT
            actual_amount = max(amount, 20)
            quantity = round(actual_amount / market_price, 3)  # 将USDT转换为对应的数量
            
            # 确保下单金额正确
            if amount != POSITION_SIZE:
                print(f"Warning: Order amount adjusted to {POSITION_SIZE} USDT")
                amount = POSITION_SIZE
                quantity = round(amount / market_price, 3)
            
            # 合并参数
            order_params = {
                'positionSide': 'SHORT',
                **base_params
            }
            
            # 根据订单类型执行不同的下单逻辑
            if order_type.upper() == 'MARKET':
                order = self.exchange.create_market_order(
                    symbol=symbol,
                    side='SELL',
                    amount=quantity,
                    params=order_params
                )
                print(f"成功开空仓（市价单）: 金额={actual_amount}USDT, 数量={quantity}")
            else:  # 默认使用限价单
                # 如果没有提供价格，则使用当前市场价并添加适当的滑点
                if price is None:
                    # 卖出时，价格应略高于市场价（例如上升0.5%）以增加成交概率
                    price = market_price * 1.005  # 卖出价格略高于市场价
                    print(f"No price provided, using adjusted market price: {price}")
                
                # 使用策略提供的价格，不再调整
                limit_price = round(price, 4)  # 增加精度，使用4位小数
                
                # 再次确认订单价值满足最低要求
                notional = quantity * limit_price
                if notional < 20:
                    # 如果订单价值仍然小于20 USDT，调整数量
                    quantity = round(20 / limit_price, 3) + 0.001  # 加一点余量确保满足要求
                    print(f"调整下单数量以满足最低订单价值要求: {quantity}")
                
                order = self.exchange.create_limit_order(
                    symbol=symbol,
                    side='SELL',
                    amount=quantity,
                    price=limit_price,
                    params=order_params
                )
                print(f"成功开空仓（限价单）: 金额={actual_amount}USDT, 数量={quantity}, 价格={limit_price}")
            
            return order
        except Exception as e:
            error_msg = str(e)
            if "insufficient balance" in error_msg.lower():
                print(f"开空仓失败: 余额不足，请确保账户有足够的USDT")
            elif "MIN_NOTIONAL" in error_msg or "notional" in error_msg:
                print(f"开空仓失败: 订单金额低于最小要求，请增加下单金额")
            elif "LOT_SIZE" in error_msg:
                print(f"开空仓失败: 下单数量不符合规则，请调整下单数量")
            elif "Timestamp for this request" in error_msg:
                print(f"开空仓失败: 时间戳错误，尝试重新同步时间")
                self.data_fetcher.sync_time(force=True)
            else:
                print(f"开空仓失败: {e}")
            return None

    def close_position(self, symbol, position_side, order_type='MARKET', price=None):
        """平掉指定方向的仓位（支持市价单和限价单）

        持仓模式以交易所返回的 positionSide 为准（ccxt 统一持仓结构中位于 info.positionSide）：
        - 双向持仓(LONG/SHORT)：只传 positionSide，不能同时传 reduceOnly（币安返回 -1106）
        - 单向持仓(BOTH)：只传 reduceOnly
        """
        try:
            # 确保时间同步
            self.data_fetcher.sync_time()
            
            # 添加时间戳参数
            base_params = {
                'timestamp': self.data_fetcher.get_timestamp(),
                'recvWindow': 60000
            }
            
            positions = self.exchange.fetch_positions([symbol], params=base_params)
            for position in positions:
                amount = abs(float(position.get('contracts') or 0))
                if amount == 0:
                    continue

                exchange_position_side = str(position.get('positionSide') or position.get('info', {}).get('positionSide') or '').upper()
                if exchange_position_side in ('LONG', 'SHORT'):
                    # 双向持仓模式：positionSide 已表明平仓意图
                    if exchange_position_side != position_side:
                        continue
                    order_params = {
                        'positionSide': position_side,
                        **base_params
                    }
                else:
                    # 单向持仓模式：根据持仓方向判断
                    if position.get('side') != position_side.lower():
                        continue
                    order_params = {
                        'reduceOnly': True,
                        **base_params
                    }

                side = 'SELL' if position_side == 'LONG' else 'BUY'

                if order_type.upper() == 'LIMIT' and price is not None:
                    # 使用限价单平仓，价格按交易对 tickSize 取精度
                    limit_price = float(self.exchange.price_to_precision(symbol, price))
                    self.exchange.create_limit_order(
                        symbol=symbol,
                        side=side,
                        amount=amount,
                        price=limit_price,
                        params=order_params
                    )
                    print(f"成功平仓 {position_side} 仓位 {symbol} (限价单, 价格={limit_price})")
                else:
                    # 使用市价单平仓
                    self.exchange.create_market_order(
                        symbol=symbol,
                        side=side,
                        amount=amount,
                        params=order_params
                    )
                    print(f"成功平仓 {position_side} 仓位 {symbol} (市价单)")
        except Exception as e:
            error_msg = str(e)
            if "Timestamp for this request" in error_msg:
                print(f"平仓失败: 时间戳错误，尝试重新同步时间")
                self.data_fetcher.sync_time(force=True)
            else:
                print(f"平仓 {position_side} 仓位失败: {e}")

    def close_all_positions(self, symbol, order_type='MARKET', price=None):
        """平掉所有仓位（支持市价单和限价单）"""
        try:
            # 分别平掉多头和空头仓位
            self.close_position(symbol, 'LONG', order_type, price)
            time.sleep(1)  # 添加延时，避免请求过快
            self.close_position(symbol, 'SHORT', order_type, price)
            print(f"Successfully closed all positions for {symbol} with {order_type.lower()} orders")
        except Exception as e:
            print(f"Error closing positions: {e}")