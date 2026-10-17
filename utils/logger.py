import logging
from config.config import LOG_LEVEL, LOG_FILE

def setup_logger(name=None, log_file=None):
    """设置日志记录器
    
    Args:
        name: 日志记录器名称，默认为'TradingBot'
        log_file: 日志文件路径，默认使用配置文件中的LOG_FILE
    """
    logger_name = name if name else 'TradingBot'
    log_file_path = log_file if log_file else LOG_FILE
    
    logger = logging.getLogger(logger_name)
    logger.setLevel(LOG_LEVEL)
    
    # 清除现有的处理器，避免重复添加
    logger.handlers.clear()

    file_handler = logging.FileHandler(log_file_path)
    file_handler.setLevel(LOG_LEVEL)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(LOG_LEVEL)

    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger