import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
project_root = str(Path(__file__).resolve().parents[3])
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from bit.config.bit_long_term_config import LONG_TERM_CONFIG
from common.risk_state import RiskLedger, evaluate, resolve_limits
from bit.utils.logger import setup_logger


class LongTermRiskManager:
    """BIT长期策略风控管理器（持久化硬风控）。

    此前这里的阈值被写死为 ``-inf``，且 ``update_pnl()`` 在生产代码中从未被调用，
    再加上策略对象每个调度周期都会重建，三重原因叠加使风控完全无法拦截任何交易。

    现在计数器由 :class:`common.risk_state.RiskLedger` 持久化（带文件锁与原子写），
    因此能跨周期重建与进程重启存活；阈值从配置的 ``risk_config`` 读取。

    单位：已实现盈亏以 USDT 计；``*_pct`` 阈值是账户权益的比例。
    """

    def __init__(self, exchange):
        self.exchange = exchange
        self.config = LONG_TERM_CONFIG
        self.limits = resolve_limits(self.config.get('risk_config', {}))
        self.ledger = RiskLedger('bit_long_term')
        self.max_daily_loss_pct = self.limits['max_daily_loss_pct']
        self.max_drawdown_pct = self.limits['max_drawdown_pct']
        self.max_daily_trades = self.limits['max_daily_trades']

        self.logger = setup_logger(
            name='LongTermRiskManager',
            log_file=self.config['log_config']['log_file']
        )


    # -- 兼容旧接口：这两个属性过去是实例变量，现在从持久化账本读取 --
    @property
    def daily_pnl(self):
        return float(self.ledger.snapshot().get('daily_pnl') or 0.0)

    @property
    def total_pnl(self):
        return float(self.ledger.snapshot().get('total_pnl') or 0.0)

    def check_risk_limits(self, intent='open'):
        """新增风险是否被允许。平仓等降风险动作(intent != 'open')一律放行。"""
        equity = self.get_account_balance()
        if equity > 0:
            self.ledger.note_equity(equity)
        decision = evaluate(self.ledger.snapshot(), self.limits, equity, intent=intent)
        if decision:
            self.logger.info("BIT长期策略风控检查通过 - %s" % decision.reason)
        else:
            self.logger.warning("BIT长期策略风控拦截开仓 - %s" % decision.reason)
        return bool(decision)

    def update_pnl(self, pnl, counts_as_trade=True):
        """记录一笔已实现盈亏（USDT）。必须在每次平仓后调用。"""
        equity = self.get_account_balance() or None
        state = self.ledger.record_trade(realized_pnl=pnl, counts_as_trade=counts_as_trade,
                                         equity=equity)
        self.logger.info("BIT长期策略已实现盈亏 %.6f 已记账 - 日累计 %.6f / 总累计 %.6f / 当日笔数 %d"
              % (float(pnl or 0.0), state['daily_pnl'], state['total_pnl'], state['daily_trades']))
        return state

    def reset_daily_pnl(self):
        """重置当日计数（账本亦会在 UTC 日期翻转时自动重置）。"""
        return self.ledger.reset_daily()

    def calculate_position_risk(self, position_size, current_price):
        """计算仓位风险"""
        position_value = position_size * current_price
        account_balance = self.get_account_balance()
        if account_balance > 0:
            return position_value / account_balance
        return 0

    def get_account_balance(self):
        """获取账户权益；不可得时返回 0，调用方据此拒绝开新仓。"""
        try:
            balance = self.exchange.fetch_balance()
            from common.balance_helper import extract_usdt_balance
            return float(extract_usdt_balance(balance)['total'])
        except Exception as e:
            self.logger.warning("BIT长期策略获取账户余额失败，风控将拒绝开新仓: %s" % e)
            return 0
