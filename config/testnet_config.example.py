# ============================================================
# Binance 测试网配置示例
#
# 用法： cp config/testnet_config.example.py config/testnet_config.py
#
# ⚠️  测试网凭证与实盘凭证是两套，切勿混用；
#     用实盘凭证跑"测试"会产生真实委托。
# ============================================================
import os

TESTNET_API_KEY = os.environ.get('BINANCE_TESTNET_API_KEY', '')
TESTNET_API_SECRET = os.environ.get('BINANCE_TESTNET_API_SECRET', '')

POSITION_SIZE = 10                 # USDT
STOP_LOSS_PERCENTAGE = 0.02        # 2%
TAKE_PROFIT_PERCENTAGE = 0.05      # 5%
