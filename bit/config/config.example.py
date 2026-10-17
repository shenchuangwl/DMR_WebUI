# ============================================================
# BIT 日志配置示例
#
# 用法： cp bit/config/config.example.py bit/config/config.py
#
# ⚠️  bit/config/config.py 已被 .gitignore 忽略，请勿提交填好的版本。
#
# bit/utils/logger.py 从本模块读取 LOG_LEVEL / LOG_FILE；
# 缺少本文件会导致整条 BIT 策略链 ModuleNotFoundError。
# 交易参数请配置 bit/config/bit_config.py（见 bit_config.example.py）。
# ============================================================

LOG_LEVEL = 'INFO'
LOG_FILE = 'bit_trading_log.txt'
