import pandas as pd
from strategy.multi_strategy import MultiStrategy
# 导入新的DMR四象限量化策略，替换原有策略
from strategy.DMRQuadrantStrategy import DMRQuadrantStrategy
from utils.logger import setup_logger
import os
import sys
import time
from pathlib import Path
from datetime import datetime, timedelta
from data.data_fetcher import DataFetcher
from execution.order_executor import OrderExecutor
# 导入部分
from config.config import (
    SYMBOL, TIMEFRAME_SHORT, TIMEFRAME_LONG,
    DMR_STRATEGY_CONFIG, SYSTEM_CONFIG
)
# 其他部分中的变量替换
# 将所有 TIMEFRAME_1H 替换为 TIMEFRAME_SHORT
# 将所有 TIMEFRAME_4H 替换为 TIMEFRAME_LONG
import schedule
import threading

# 添加项目根目录到 Python 路径
project_root = str(Path(__file__).parent)
if project_root not in sys.path:
    sys.path.append(project_root)

def update_data(fetcher, symbol, filename):
    """更新市场数据"""
    # 强制同步时间
    fetcher.sync_time(force=True)
    
    # 修正：使用TIMEFRAME_SHORT替代TIMEFRAME_1H
    df = fetcher.get_historical_data(symbol, TIMEFRAME_SHORT)
    
    if df is not None:
        fetcher.save_data_to_csv(df, filename)
        return df
    return None

def log_account_info(logger, fetcher, symbol):
    """记录账户信息和交易数据"""
    try:
        # 强制同步时间
        fetcher.sync_time(force=True)
        
        # 获取账户余额
        balance_info = fetcher.get_account_balance()
        if balance_info:
            logger.info(f"账户余额 - 总计USDT: {balance_info['total_usdt']}, 可用USDT: {balance_info['free_usdt']}, 已用USDT: {balance_info['used_usdt']}")
        
        # 获取当前持仓
        positions = fetcher.get_positions(symbol)
        if positions:
            for pos in positions:
                side = pos['side']
                size = pos['contracts']
                entry_price = pos['entryPrice']
                unrealized_pnl = pos['unrealizedPnl']
                logger.info(f"当前持仓 - {symbol} {side}: 数量={size}, 入场价={entry_price}, 未实现盈亏={unrealized_pnl}")
        else:
            logger.info(f"当前无{symbol}持仓")
        
        # 获取未完成订单
        open_orders = fetcher.get_open_orders(symbol)
        if open_orders:
            logger.info(f"未完成订单数量: {len(open_orders)}")
            for order in open_orders:
                logger.info(f"订单ID: {order['id']}, 类型: {order['type']}, 方向: {order['side']}, 价格: {order['price']}, 数量: {order['amount']}")
        else:
            logger.info(f"当前无未完成订单")
        
        # 获取最近交易
        recent_trades = fetcher.get_recent_trades(symbol, limit=5)
        if recent_trades:
            logger.info(f"最近5笔交易:")
            for trade in recent_trades:
                trade_time = pd.to_datetime(trade['timestamp'], unit='ms')
                logger.info(f"交易时间: {trade_time}, ID: {trade['id']}, 方向: {trade['side']}, 价格: {trade['price']}, 数量: {trade['amount']}, 成本: {trade['cost']}")
    except Exception as e:
        logger.error(f"记录账户信息失败: {e}")

def check_and_execute_strategy(logger, fetcher, order_executor, multi_strategy, data_path, executed_signals):
    """检查并执行策略"""
    try:
        # 同步时间并获取服务器时间
        fetcher.sync_time(force=True)
        current_time = fetcher.get_server_time()
        
        # 更新市场数据
        df = update_data(fetcher, SYMBOL, data_path)
        if df is None:
            logger.error("获取市场数据失败")
            return
            
        logger.info(f"在 {current_time} 更新了市场数据")
        
        # 创建并执行策略 - 使用配置文件中的参数
        dmr_strategy = DMRQuadrantStrategy(df, order_executor, params=DMR_STRATEGY_CONFIG)
        multi_strategy.add_strategy(dmr_strategy)
        
        try:
            # 获取当前信号但不立即执行
            dmr_strategy.calculate_dmr()
            dmr_strategy.resample_data()
            dmr_strategy.generate_signals()
            
            # 获取最新信号 - 修正变量名
            latest_4h_signal = dmr_strategy.df_4h['signal_4h'].iloc[-1] if len(dmr_strategy.df_4h) > 0 else 0
            latest_1h_signal = dmr_strategy.df_1h['signal_1h'].iloc[-1] if len(dmr_strategy.df_1h) > 0 else 0
            
            # 获取当前市场状态
            market_state = dmr_strategy.get_market_state()
            logger.info(f"当前市场状态: {market_state}")
            # 正确显示：长周期显示DMR12，短周期显示DMR26
            logger.info(f"长周期({TIMEFRAME_LONG}) DMR12: {dmr_strategy.df_4h['dmr_avg12'].iloc[-1] if len(dmr_strategy.df_4h) > 0 else 0:.6f}")
            logger.info(f"短周期({TIMEFRAME_SHORT}) DMR26: {dmr_strategy.df_1h['dmr_avg26'].iloc[-1] if len(dmr_strategy.df_1h) > 0 else 0:.6f}")
            
            # 检查信号是否已执行过 - 修正变量名
            is_long_new_signal = latest_4h_signal != 0 and latest_4h_signal != executed_signals[TIMEFRAME_LONG]
            is_short_new_signal = latest_1h_signal != 0 and latest_1h_signal != executed_signals[TIMEFRAME_SHORT]
            
            # 只执行新信号
            if is_long_new_signal or is_short_new_signal:
                # 再次同步时间，确保执行交易时时间戳正确
                fetcher.sync_time(force=True)
                
                multi_strategy.execute_strategies()
                # 更新已执行的信号
                if is_long_new_signal:
                    executed_signals[TIMEFRAME_LONG] = latest_4h_signal
                    logger.info(f"执行了新的长周期信号: {latest_4h_signal}")
                if is_short_new_signal:
                    executed_signals[TIMEFRAME_SHORT] = latest_1h_signal
                    logger.info(f"执行了新的短周期信号: {latest_1h_signal}")
                logger.info("K线收盘时间，检查并执行新的交易信号")
            else:
                logger.info("K线收盘时间，但没有新的交易信号")
        except Exception as e:
            logger.error(f"策略执行错误: {e}")
            
        # 清空策略列表，准备下一轮
        multi_strategy.strategies.clear()
        
    except Exception as e:
        logger.error(f"执行策略检查时发生错误: {e}")

def run_scheduler():
    """运行调度器"""
    while True:
        schedule.run_pending()
        time.sleep(SYSTEM_CONFIG['scheduler_sleep'])

def main():
    """主函数"""
    try:
        logger = setup_logger()
        
        # 初始化数据获取器和交易执行器
        fetcher = DataFetcher()
        
        # 程序启动时立即同步时间
        if SYSTEM_CONFIG['force_sync_on_startup']:
            logger.info("程序启动，正在同步时间...")
            fetcher.sync_time(force=True)
        
        order_executor = OrderExecutor(fetcher.exchange)
        multi_strategy = MultiStrategy(order_executor)
        
        # 设置交易对和数据文件路径
        data_dir = os.path.join(project_root, 'data')
        os.makedirs(data_dir, exist_ok=True)
        base_currency = SYMBOL.split('/')[0]
        data_path = f'{data_dir}/{base_currency}_USDT_data.csv'
        
        logger.info(f"Starting live trading bot with {SYMBOL} DMR Quadrant strategy")
        
        # 在程序启动时立即记录账户信息
        logger.info("=" * 50)
        logger.info("程序启动 - 初始账户信息和持仓数据")
        logger.info("-" * 50)
        log_account_info(logger, fetcher, SYMBOL)
        logger.info("=" * 50)
        
        # 创建一个信号记录字典，用于跟踪已执行的信号
        executed_signals = {TIMEFRAME_LONG: None, TIMEFRAME_SHORT: None}
        
        # 修正调度逻辑 - 短周期调度
        if TIMEFRAME_SHORT == '1h':
            schedule.every().hour.at(":00").do(
                check_and_execute_strategy, 
                logger, fetcher, order_executor, multi_strategy, data_path, executed_signals
            )
        elif TIMEFRAME_SHORT == '5m':
            for minute in range(0, 60, 5):
                schedule.every().hour.at(f":{minute:02d}").do(
                    check_and_execute_strategy,
                    logger, fetcher, order_executor, multi_strategy, data_path, executed_signals
                )
        elif TIMEFRAME_SHORT == '15m':
            for minute in [0, 15, 30, 45]:
                schedule.every().hour.at(f":{minute:02d}").do(
                    check_and_execute_strategy,
                    logger, fetcher, order_executor, multi_strategy, data_path, executed_signals
                )
        elif TIMEFRAME_SHORT == '30m':
            for minute in [0, 30]:
                schedule.every().hour.at(f":{minute:02d}").do(
                    check_and_execute_strategy,
                    logger, fetcher, order_executor, multi_strategy, data_path, executed_signals
                )
        
        # 修正调度逻辑 - 长周期调度
        if TIMEFRAME_LONG == '4h':
            for hour in [0, 4, 8, 12, 16, 20]:
                schedule.every().day.at(f"{hour:02d}:00").do(
                    check_and_execute_strategy,
                    logger, fetcher, order_executor, multi_strategy, data_path, executed_signals
                )
        elif TIMEFRAME_LONG == '15m':
            for minute in [0, 15, 30, 45]:
                schedule.every().hour.at(f":{minute:02d}").do(
                    check_and_execute_strategy,
                    logger, fetcher, order_executor, multi_strategy, data_path, executed_signals
                )
        elif TIMEFRAME_LONG == '30m':
            for minute in [0, 30]:
                schedule.every().hour.at(f":{minute:02d}").do(
                    check_and_execute_strategy,
                    logger, fetcher, order_executor, multi_strategy, data_path, executed_signals
                )
        elif TIMEFRAME_LONG == '1h':
            schedule.every().hour.at(":00").do(
                check_and_execute_strategy,
                logger, fetcher, order_executor, multi_strategy, data_path, executed_signals
            )
        elif TIMEFRAME_LONG == '2h':
            for hour in range(0, 24, 2):
                schedule.every().day.at(f"{hour:02d}:00").do(
                    check_and_execute_strategy,
                    logger, fetcher, order_executor, multi_strategy, data_path, executed_signals
                )
        
        # 设置定时任务 - 每天0点记录账户信息
        schedule.every().day.at("00:00").do(
            lambda: log_account_info(logger, fetcher, SYMBOL)
        )
        
        # 设置定时任务 - 每小时同步一次时间
        schedule.every().hour.do(
            lambda: fetcher.sync_time(force=True)
        )
        
        # 启动调度器线程
        scheduler_thread = threading.Thread(target=run_scheduler)
        scheduler_thread.daemon = True
        scheduler_thread.start()
        
        # 主线程保持运行
        logger.info("定时任务已设置，系统正在运行...")
        while True:
            time.sleep(SYSTEM_CONFIG['main_loop_sleep'])
            
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")
    finally:
        logger.info("Bot stopped")

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
        with single_instance('main'):
            main()
    except RuntimeError as _e:
        print('启动被拒绝: %s' % _e)
        raise SystemExit(3)


