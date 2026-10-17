from config.testnet_config import TESTNET_API_KEY, TESTNET_API_SECRET, POSITION_SIZE
import config.config as _cfg
import ccxt
import time

# 账户设置改写开关：兼容未包含这些字段的旧版配置文件，缺省即关闭（不改写交易所账户设置）
APPLY_ACCOUNT_SETTINGS = getattr(_cfg, 'APPLY_ACCOUNT_SETTINGS', False)
APPLY_LEVERAGE = getattr(_cfg, 'APPLY_LEVERAGE', 10)
APPLY_MARGIN_TYPE = getattr(_cfg, 'APPLY_MARGIN_TYPE', 'CROSSED')

class TestnetOrderExecutor:
    def __init__(self):
        self.exchange = ccxt.binance({
            'apiKey': TESTNET_API_KEY,
            'secret': TESTNET_API_SECRET,
            'enableRateLimit': True,
            'options': {
                'defaultType': 'future',
                'adjustForTimeDifference': True,
                'testnet': True
            }
        })
        # 初始化时设置双向持仓模式
        try:
            self.exchange.fapiPrivatePostPositionSideDual({'dualSidePosition': True})
            print("Successfully set dual position mode")
        except Exception as e:
            print(f"Error setting position mode: {e}")

    def initialize_trading_config(self, symbol):
        """初始化交易配置（⚠️ 会改写测试网账户的杠杆与保证金模式）

        默认不执行，需显式设置 DMR_APPLY_ACCOUNT_SETTINGS=1 后才写入。
        """
        if not APPLY_ACCOUNT_SETTINGS:
            print(f"跳过测试网账户设置改写 {symbol}（未开启 DMR_APPLY_ACCOUNT_SETTINGS=1）")
            return
        try:
            # 设置杠杆倍数（默认10倍，可由 DMR_LEVERAGE 覆盖）
            self.exchange.fapiPrivatePostLeverage({
                'symbol': symbol.replace('/', ''),
                'leverage': APPLY_LEVERAGE
            })
            
            # 设置保证金模式为全仓模式
            self.exchange.fapiPrivatePostMarginType({
                'symbol': symbol.replace('/', ''),
                'marginType': APPLY_MARGIN_TYPE  # 默认 CROSSED，可由 DMR_MARGIN_TYPE 覆盖
            })
            print(f"Trading configuration initialized for {symbol}")
        except Exception as e:
            print(f"Error initializing trading config: {e}")

    def get_position_info(self, symbol):
        """获取当前持仓信息"""
        try:
            positions = self.exchange.fetch_positions([symbol])
            position_info = {}
            for pos in positions:
                # ccxt 统一持仓结构顶层没有 positionSide，需从 info 中读取
                position_side = str(pos.get('positionSide') or pos.get('info', {}).get('positionSide') or '').upper()
                if position_side == 'LONG':
                    position_info['long'] = float(pos.get('contracts') or 0)
                elif position_side == 'SHORT':
                    position_info['short'] = float(pos.get('contracts') or 0)
            return position_info
        except Exception as e:
            print(f"Error fetching positions: {e}")
            return {'long': 0, 'short': 0}

    def open_long(self, symbol, amount):
        """开多仓"""
        try:
            # 确保交易配置已初始化
            self.initialize_trading_config(symbol)
            
            # 获取最新价格并计算数量
            ticker = self.exchange.fetch_ticker(symbol)
            price = ticker['last']
            quantity = round(amount / price, 3)  # 将100USDT转换为对应的数量
            
            # 确保下单金额正确
            if amount != POSITION_SIZE:
                print(f"Warning: Order amount adjusted to {POSITION_SIZE} USDT")
                amount = POSITION_SIZE
                quantity = round(amount / price, 3)
            
            order = self.exchange.create_market_order(
                symbol=symbol,
                side='BUY',
                amount=quantity,
                params={
                    'positionSide': 'LONG',
                    'type': 'MARKET'
                }
            )
            print(f"Successfully opened long position: Amount={amount}USDT, Quantity={quantity}, Price={price}")
            return order
        except Exception as e:
            print(f"Error opening long position: {e}")
            return None

    def open_short(self, symbol, amount):
        """开空仓"""
        try:
            # 确保交易配置已初始化
            self.initialize_trading_config(symbol)
            
            # 获取最新价格并计算数量
            ticker = self.exchange.fetch_ticker(symbol)
            price = ticker['last']
            quantity = round(amount / price, 3)  # 将100USDT转换为对应的数量
            
            # 确保下单金额正确
            if amount != POSITION_SIZE:
                print(f"Warning: Order amount adjusted to {POSITION_SIZE} USDT")
                amount = POSITION_SIZE
                quantity = round(amount / price, 3)
            
            order = self.exchange.create_market_order(
                symbol=symbol,
                side='SELL',
                amount=quantity,
                params={
                    'positionSide': 'SHORT',
                    'type': 'MARKET'
                }
            )
            print(f"Successfully opened short position: Amount={amount}USDT, Quantity={quantity}, Price={price}")
            return order
        except Exception as e:
            print(f"Error opening short position: {e}")
            return None

    def close_position(self, symbol, position_side):
        """平掉指定方向的仓位"""
        try:
            positions = self.exchange.fetch_positions([symbol])
            for position in positions:
                exchange_position_side = str(position.get('positionSide') or position.get('info', {}).get('positionSide') or '').upper()
                if exchange_position_side == position_side and float(position.get('contracts') or 0) != 0:
                    side = 'SELL' if position_side == 'LONG' else 'BUY'
                    # 双向持仓模式下 positionSide 已表明平仓意图，不能同时传 reduceOnly（币安返回 -1106）
                    self.exchange.create_market_order(
                        symbol=symbol,
                        side=side,
                        amount=abs(float(position['contracts'])),
                        params={
                            'positionSide': position_side,
                            'type': 'MARKET'
                        }
                    )
            print(f"Successfully closed {position_side} position for {symbol}")
        except Exception as e:
            print(f"Error closing {position_side} position: {e}")

    def close_all_positions(self, symbol):
        """平掉所有仓位"""
        try:
            # 分别平掉多头和空头仓位
            self.close_position(symbol, 'LONG')
            time.sleep(1)  # 添加延时，避免请求过快
            self.close_position(symbol, 'SHORT')
            print(f"Successfully closed all positions for {symbol}")
        except Exception as e:
            print(f"Error closing positions: {e}")

    def execute(self, results):
        """执行策略结果"""
        # 这里可以根据策略结果执行具体的交易
        pass