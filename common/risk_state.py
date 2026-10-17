"""Persistent risk ledger and the gate that decides whether new risk is allowed.

Why this exists
---------------
The previous risk managers could not block anything, for three independent
reasons, each sufficient on its own:

* ``max_daily_loss`` / ``max_drawdown`` were hard-coded to ``-inf`` in four
  managers, and every comparison was guarded by ``!= -float('inf')``.
* ``update_pnl()`` was never called anywhere in production, so ``daily_pnl`` and
  ``total_pnl`` stayed at 0 forever.
* Strategy objects (and therefore the risk manager) are rebuilt on every
  scheduling cycle, so even a correct in-memory counter would reset each cycle.

The ledger below is persisted, locked and reset on date rollover, so the counters
survive both the per-cycle rebuild and a process restart.

Units
-----
Realised PnL is recorded in quote currency (USDT). Limits expressed as ``*_pct``
are fractions of a reference equity value. The old code compared a USDT PnL
against a 0.03 fraction, which is why the thresholds were meaningless.
"""
from datetime import datetime, timezone

from common.atomic_state import atomic_write_json, file_lock, read_json, state_path

DEFAULT_LIMITS = {
    'max_daily_loss_pct': 0.05,
    'max_drawdown_pct': 0.10,
    'max_daily_trades': 25,
}


def _today():
    return datetime.now(timezone.utc).strftime('%Y-%m-%d')


def _empty(strategy_name):
    return {
        'strategy': strategy_name,
        'date': _today(),
        'daily_pnl': 0.0,
        'total_pnl': 0.0,
        'daily_trades': 0,
        'peak_equity': 0.0,
        'day_start_equity': 0.0,
        'updated_at': None,
    }


class RiskLedger:
    """Durable per-strategy PnL and trade counters."""

    def __init__(self, strategy_name):
        self.strategy_name = str(strategy_name)
        self._path = state_path('risk_ledger_%s.json' % self.strategy_name)

    @property
    def path(self):
        return self._path

    def _load_locked(self):
        data = read_json(self._path, None)
        if not isinstance(data, dict):
            return _empty(self.strategy_name)
        base = _empty(self.strategy_name)
        base.update({k: v for k, v in data.items() if k in base})
        if base.get('date') != _today():
            # Date rollover: the daily counters restart, cumulative ones do not.
            base['date'] = _today()
            base['daily_pnl'] = 0.0
            base['daily_trades'] = 0
            base['day_start_equity'] = 0.0
        return base

    def snapshot(self):
        """Current counters. Never raises for a missing file."""
        with file_lock('risk_ledger_%s' % self.strategy_name):
            return self._load_locked()

    def record_trade(self, realized_pnl=0.0, counts_as_trade=True, equity=None):
        """Record one completed trade under an exclusive lock."""
        with file_lock('risk_ledger_%s' % self.strategy_name):
            state = self._load_locked()
            pnl = float(realized_pnl or 0.0)
            state['daily_pnl'] = float(state['daily_pnl']) + pnl
            state['total_pnl'] = float(state['total_pnl']) + pnl
            if counts_as_trade:
                state['daily_trades'] = int(state['daily_trades']) + 1
            if equity is not None:
                equity = float(equity)
                state['peak_equity'] = max(float(state.get('peak_equity') or 0.0), equity)
                if not state.get('day_start_equity'):
                    state['day_start_equity'] = equity
            state['updated_at'] = datetime.now(timezone.utc).isoformat()
            atomic_write_json(self._path, state)
            return state

    def note_equity(self, equity):
        """Record the latest account equity without counting a trade."""
        if equity is None:
            return self.snapshot()
        return self.record_trade(realized_pnl=0.0, counts_as_trade=False, equity=equity)

    def reset_daily(self):
        with file_lock('risk_ledger_%s' % self.strategy_name):
            state = self._load_locked()
            state['daily_pnl'] = 0.0
            state['daily_trades'] = 0
            state['date'] = _today()
            state['updated_at'] = datetime.now(timezone.utc).isoformat()
            atomic_write_json(self._path, state)
            return state


class RiskDecision:
    """Outcome of a gate evaluation. Truthy when new risk is allowed."""

    __slots__ = ('allowed', 'reason', 'detail')

    def __init__(self, allowed, reason='', detail=None):
        self.allowed = bool(allowed)
        self.reason = reason
        self.detail = detail or {}

    def __bool__(self):
        return self.allowed

    def __repr__(self):
        return 'RiskDecision(allowed=%r, reason=%r)' % (self.allowed, self.reason)


def resolve_limits(risk_config):
    """Merge a strategy ``risk_config`` dict over the safe defaults."""
    limits = dict(DEFAULT_LIMITS)
    for key in ('max_daily_loss_pct', 'max_drawdown_pct', 'max_daily_trades'):
        value = (risk_config or {}).get(key)
        if value is not None:
            limits[key] = value
    return limits


def evaluate(snapshot, limits, equity_reference, intent='open'):
    """Decide whether ``intent`` may proceed.

    ``intent='close'`` (or any risk-reducing action) is always allowed: blocking
    an exit would strand a position that the limits are trying to protect.

    When ``equity_reference`` is unknown, percentage limits cannot be evaluated,
    so opening new risk is refused rather than silently permitted.
    """
    if intent != 'open':
        return RiskDecision(True, 'risk-reducing action is always permitted')

    trades = int(snapshot.get('daily_trades') or 0)
    max_trades = limits.get('max_daily_trades')
    if max_trades is not None and trades >= int(max_trades):
        return RiskDecision(False, 'daily trade limit reached: %d >= %s' % (trades, max_trades),
                            {'daily_trades': trades})

    if not equity_reference or float(equity_reference) <= 0:
        return RiskDecision(False,
                            'account equity unavailable, cannot evaluate percentage limits; '
                            'refusing to open new risk',
                            {'equity_reference': equity_reference})

    equity_reference = float(equity_reference)
    daily_pnl = float(snapshot.get('daily_pnl') or 0.0)
    total_pnl = float(snapshot.get('total_pnl') or 0.0)

    daily_cap = float(limits['max_daily_loss_pct']) * equity_reference
    if daily_pnl < 0 and abs(daily_pnl) >= daily_cap:
        return RiskDecision(False,
                            'daily loss limit breached: %.6f >= %.6f (%.2f%% of %.6f)'
                            % (abs(daily_pnl), daily_cap, float(limits['max_daily_loss_pct']) * 100,
                               equity_reference),
                            {'daily_pnl': daily_pnl, 'cap': daily_cap})

    peak = float(snapshot.get('peak_equity') or 0.0) or equity_reference
    drawdown_cap = float(limits['max_drawdown_pct']) * peak
    if total_pnl < 0 and abs(total_pnl) >= drawdown_cap:
        return RiskDecision(False,
                            'max drawdown breached: %.6f >= %.6f (%.2f%% of peak %.6f)'
                            % (abs(total_pnl), drawdown_cap, float(limits['max_drawdown_pct']) * 100, peak),
                            {'total_pnl': total_pnl, 'cap': drawdown_cap})

    return RiskDecision(True, 'within limits',
                        {'daily_pnl': daily_pnl, 'total_pnl': total_pnl, 'daily_trades': trades})
