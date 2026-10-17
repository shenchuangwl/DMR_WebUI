import time
import schedule
import threading
from datetime import datetime
from strategy.first_short_term.data_fetcher import ShortTermDataFetcher
from strategy.first_short_term.strategy_engine import ShortTermDMRStrategy
from strategy.first_short_term.order_executor import ShortTermOrderExecutor
from strategy.first_short_term.position_manager import ShortTermPositionManager
from strategy.first_short_term.risk_manager import ShortTermRiskManager
from strategy.first_long_term.data_fetcher import LongTermDataFetcher  # 双时间框架升级版
from config.first_short_term_config import SHORT_TERM_CONFIG
from utils.logger import setup_logger

def run_short_term_strategy():
    """运行短周期策略（双时间框架DMR量化策略升级版）"""
    logger = setup_logger(
        name='ShortTermMain',
        log_file=SHORT_TERM_CONFIG['log_config']['log_file']
    )

    try:
        # 初始化各个模块
        data_fetcher = ShortTermDataFetcher()
        position_manager = ShortTermPositionManager(data_fetcher.exchange)
        order_executor = ShortTermOrderExecutor(data_fetcher.exchange, position_manager)
        risk_manager = ShortTermRiskManager(data_fetcher.exchange)

        # 双时间框架升级版：初始化长期数据获取器
        long_term_data_fetcher = LongTermDataFetcher()

        # 获取并保存数据
        df = data_fetcher.get_and_save_data()
        if df is None:
            logger.error("短周期策略数据获取失败，跳过本次执行")
            return

        # 初始化策略引擎（双时间框架升级版）
        strategy = ShortTermDMRStrategy(
            data_fetcher, order_executor, position_manager, risk_manager, long_term_data_fetcher
        )

        # 启动时状态核对
        strategy.reconcile_state()

        # 运行策略，传入已获取和处理好的数据
        strategy.run_strategy(df)

        logger.info("短周期策略执行完成（双时间框架协调版）")
        # 添加分隔线
        print("-" * 200)

    except Exception as e:
        logger.error(f"短周期策略执行失败: {e}")

def setup_short_term_schedule():
    """设置短周期策略调度"""
    timeframe = SHORT_TERM_CONFIG['timeframe']

    if timeframe == '1m':
        # 每分钟执行
        schedule.every().minute.do(run_short_term_strategy)
    elif timeframe == '3m':
        # 每3分钟执行：0, 3, 6, 9, ..., 57分钟
        for minute in range(0, 60, 3):
            schedule.every().hour.at(f":{minute:02d}").do(run_short_term_strategy)
    elif timeframe == '5m':
        # 每5分钟执行：0, 5, 10, 15, ..., 55分钟
        for minute in range(0, 60, 5):
            schedule.every().hour.at(f":{minute:02d}").do(run_short_term_strategy)
    elif timeframe == '15m':
        # 每15分钟执行：0, 15, 30, 45分钟
        for minute in [0, 15, 30, 45]:
            schedule.every().hour.at(f":{minute:02d}").do(run_short_term_strategy)
    elif timeframe == '30m':
        # 每30分钟执行：0, 30分钟
        for minute in [0, 30]:
            schedule.every().hour.at(f":{minute:02d}").do(run_short_term_strategy)
    elif timeframe == '1h':
        # 每小时执行
        schedule.every().hour.at(":00").do(run_short_term_strategy)
    elif timeframe == '2h':
        # 每2小时执行：0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22点
        for hour in range(0, 24, 2):
            schedule.every().day.at(f"{hour:02d}:00").do(run_short_term_strategy)
    elif timeframe == '4h':
        # 每4小时执行：0, 4, 8, 12, 16, 20点
        for hour in [0, 4, 8, 12, 16, 20]:
            schedule.every().day.at(f"{hour:02d}:00").do(run_short_term_strategy)
    elif timeframe == '6h':
        # 每6小时执行：0, 6, 12, 18点
        for hour in [0, 6, 12, 18]:
            schedule.every().day.at(f"{hour:02d}:00").do(run_short_term_strategy)
    elif timeframe == '12h':
        # 每12小时执行：0, 12点
        for hour in [0, 12]:
            schedule.every().day.at(f"{hour:02d}:00").do(run_short_term_strategy)
    elif timeframe == '1d':
        # 每天执行：0点
        schedule.every().day.at("00:00").do(run_short_term_strategy)
    elif timeframe == '1w':
        # 每周执行：周一0点
        schedule.every().monday.at("00:00").do(run_short_term_strategy)
    else:
        # 默认情况：根据时间周期动态计算（覆盖8h、3d、1M等）
        from config.first_config import TIMEFRAME_MINUTES
        timeframe_minutes = TIMEFRAME_MINUTES.get(timeframe, 5)

        if timeframe_minutes < 60:
            # 小于1小时的时间周期，按分钟调度
            for minute in range(0, 60, timeframe_minutes):
                schedule.every().hour.at(f":{minute:02d}").do(run_short_term_strategy)
        elif timeframe_minutes < 1440:
            # 小于1天的时间周期，按小时调度（如8h）
            hours_interval = max(1, timeframe_minutes // 60)
            for hour in range(0, 24, hours_interval):
                schedule.every().day.at(f"{hour:02d}:00").do(run_short_term_strategy)
        elif timeframe_minutes < 10080:
            # 小于1周的时间周期，按天调度（如3d）
            days_interval = max(1, timeframe_minutes // 1440)
            schedule.every(days_interval).days.at("00:00").do(run_short_term_strategy)
        elif timeframe == '1M':
            # 月度调度：每日0点检查是否月初
            def run_monthly_guard():
                now = datetime.now()
                if now.day == 1:
                    run_short_term_strategy()
            schedule.every().day.at("00:00").do(run_monthly_guard)
        else:
            # 周期>=1周（默认每周一0点；如需N周可扩展）
            schedule.every().monday.at("00:00").do(run_short_term_strategy)

def run_scheduler():
    """运行调度器"""
    while True:
        schedule.run_pending()
        time.sleep(1)

def main():
    """短周期策略主程序"""
    logger = setup_logger(
        name='ShortTermMain',
        log_file=SHORT_TERM_CONFIG['log_config']['log_file']
    )
    
    logger.info("启动短周期DMR12策略系统（双时间框架DMR量化策略升级版）")
    
    # 设置调度
    setup_short_term_schedule()
    
    # 启动调度器线程
    scheduler_thread = threading.Thread(target=run_scheduler)
    scheduler_thread.daemon = True
    scheduler_thread.start()
    
    logger.info("短周期策略调度器已启动")
    
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        logger.info("短周期策略系统已停止")

if __name__ == "__main__":
    # 单实例保护：重复启动同一入口会让每个信号被执行两次（双倍下单，
    # 且两个进程的止损单 ID 会互相覆盖/撤销）。修复前全仓库没有任何此类保护。
    import sys as _sys
    from pathlib import Path as _Path
    _root = str(_Path(__file__).resolve().parents[1])
    if _root not in _sys.path:
        _sys.path.insert(0, _root)
    from common.atomic_state import single_instance

    try:
        with single_instance('first_short_term'):
            main()
    except RuntimeError as _e:
        print('启动被拒绝: %s' % _e)
        raise SystemExit(3)
    