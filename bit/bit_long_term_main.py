import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import time
import schedule
import threading
from datetime import datetime
from bit.strategy.bit_long_term.data_fetcher import LongTermDataFetcher
from bit.strategy.bit_long_term.strategy_engine import LongTermDMRStrategy
from bit.strategy.bit_long_term.order_executor import LongTermOrderExecutor
from bit.strategy.bit_long_term.position_manager import LongTermPositionManager
from bit.strategy.bit_long_term.risk_manager import LongTermRiskManager
from bit.config.bit_long_term_config import LONG_TERM_CONFIG
from bit.utils.logger import setup_logger

def run_long_term_strategy():
    """运行长周期策略（双时间框架DMR量化策略升级版）"""
    logger = setup_logger(
        name='LongTermMain',
        log_file=LONG_TERM_CONFIG['log_config']['log_file']
    )
    
    try:
        # 初始化各个模块
        data_fetcher = LongTermDataFetcher()
        position_manager = LongTermPositionManager(data_fetcher.exchange)
        order_executor = LongTermOrderExecutor(data_fetcher.exchange, position_manager)  # 修复：添加position_manager参数
        risk_manager = LongTermRiskManager(data_fetcher.exchange)
        
        # 获取并保存数据
        df = data_fetcher.get_and_save_data()
        if df is None:
            logger.error("长周期策略数据获取失败，跳过本次执行")
            return
        
        # 初始化策略引擎
        strategy = LongTermDMRStrategy(
            data_fetcher, order_executor, position_manager, risk_manager
        )
        
        # 启动时状态核对
        strategy.reconcile_state()
        
        # 运行策略，传入已获取和处理好的数据
        strategy.run_strategy(df)
        
        logger.info("长周期策略执行完成（双时间框架趋势判断版）")
        # 添加分隔线
        print("-" * 200)
        
    except Exception as e:
        logger.error(f"长周期策略执行失败: {e}")

def setup_long_term_schedule():
    """设置长周期策略调度"""
    timeframe = LONG_TERM_CONFIG['timeframe']

    if timeframe == '1m':
        # 每分钟执行
        schedule.every().minute.do(run_long_term_strategy)
    elif timeframe == '3m':
        # 每3分钟执行：0, 3, 6, 9, ..., 57分钟
        for minute in range(0, 60, 3):
            schedule.every().hour.at(f":{minute:02d}").do(run_long_term_strategy)
    elif timeframe == '5m':
        # 每5分钟执行：0, 5, 10, 15, ..., 55分钟
        for minute in range(0, 60, 5):
            schedule.every().hour.at(f":{minute:02d}").do(run_long_term_strategy)
    elif timeframe == '15m':
        # 每15分钟执行：0, 15, 30, 45分钟
        for minute in [0, 15, 30, 45]:
            schedule.every().hour.at(f":{minute:02d}").do(run_long_term_strategy)
    elif timeframe == '30m':
        # 每30分钟执行：0, 30分钟
        for minute in [0, 30]:
            schedule.every().hour.at(f":{minute:02d}").do(run_long_term_strategy)
    elif timeframe == '1h':
        # 每小时执行
        schedule.every().hour.at(":00").do(run_long_term_strategy)
    elif timeframe == '2h':
        # 每2小时执行：0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22点
        for hour in range(0, 24, 2):
            schedule.every().day.at(f"{hour:02d}:00").do(run_long_term_strategy)
    elif timeframe == '4h':
        # 每4小时执行：0, 4, 8, 12, 16, 20点
        for hour in [0, 4, 8, 12, 16, 20]:
            schedule.every().day.at(f"{hour:02d}:00").do(run_long_term_strategy)
    elif timeframe == '6h':
        # 每6小时执行：0, 6, 12, 18点
        for hour in [0, 6, 12, 18]:
            schedule.every().day.at(f"{hour:02d}:00").do(run_long_term_strategy)
    elif timeframe == '12h':
        # 每12小时执行：0, 12点
        for hour in [0, 12]:
            schedule.every().day.at(f"{hour:02d}:00").do(run_long_term_strategy)
    elif timeframe == '1d':
        # 每天执行：0点
        schedule.every().day.at("00:00").do(run_long_term_strategy)
    elif timeframe == '1w':
        # 每周执行：周一0点
        schedule.every().monday.at("00:00").do(run_long_term_strategy)
    else:
        # 默认情况：根据时间周期动态计算（覆盖8h、3d、1M等）
        from bit.config.bit_config import TIMEFRAME_MINUTES
        timeframe_minutes = TIMEFRAME_MINUTES.get(timeframe, 15)

        if timeframe_minutes < 60:
            # 小于1小时的时间周期，按分钟调度
            for minute in range(0, 60, timeframe_minutes):
                schedule.every().hour.at(f":{minute:02d}").do(run_long_term_strategy)
        elif timeframe_minutes < 1440:
            # 小于1天的时间周期，按小时调度（如8h）
            hours_interval = max(1, timeframe_minutes // 60)
            for hour in range(0, 24, hours_interval):
                schedule.every().day.at(f"{hour:02d}:00").do(run_long_term_strategy)
        elif timeframe_minutes < 10080:
            # 小于1周的时间周期，按天调度（如3d）
            days_interval = max(1, timeframe_minutes // 1440)
            schedule.every(days_interval).days.at("00:00").do(run_long_term_strategy)
        elif timeframe == '1M':
            # 月度调度：每日0点检查是否月初
            def run_monthly_guard():
                now = datetime.now()
                if now.day == 1:
                    run_long_term_strategy()
            schedule.every().day.at("00:00").do(run_monthly_guard)
        else:
            # 周期>=1周（默认每周一0点；如需N周可扩展）
            schedule.every().monday.at("00:00").do(run_long_term_strategy)

def run_scheduler():
    """运行调度器"""
    while True:
        schedule.run_pending()
        time.sleep(1)

def main():
    """长周期策略主程序"""
    logger = setup_logger(
        name='LongTermMain',
        log_file=LONG_TERM_CONFIG['log_config']['log_file']
    )
    
    logger.info("启动长周期DMR6策略系统（双时间框架DMR量化策略升级版）")
    
    # 设置调度
    setup_long_term_schedule()
    
    # 启动调度器线程
    scheduler_thread = threading.Thread(target=run_scheduler)
    scheduler_thread.daemon = True
    scheduler_thread.start()
    
    logger.info("长周期策略调度器已启动")
    
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        logger.info("长周期策略系统已停止")

if __name__ == "__main__":
    # 单实例保护：重复启动同一入口会让每个信号被执行两次（双倍下单，
    # 且两个进程的止损单 ID 会互相覆盖/撤销）。修复前全仓库没有任何此类保护。
    import sys as _sys
    from pathlib import Path as _Path
    _root = str(_Path(__file__).resolve().parents[2])
    if _root not in _sys.path:
        _sys.path.insert(0, _root)
    from common.atomic_state import single_instance

    try:
        with single_instance('bit_long_term'):
            main()
    except RuntimeError as _e:
        print('启动被拒绝: %s' % _e)
        raise SystemExit(3)
    