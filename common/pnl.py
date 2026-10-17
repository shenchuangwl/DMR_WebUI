"""Realised-PnL accounting for the risk ledger.

The engines previously logged a percentage move at close time but never fed a
number back into the risk manager, so the risk counters stayed at zero forever.
These helpers turn a close into a quote-currency (USDT) number, preferring the
exchange's reported fill over the locally requested price.
"""


def extract_fill(order, fallback_price=None, fallback_amount=None):
    """Return ``(avg_price, filled_amount)`` from a ccxt order dict.

    Falls back to the requested price/amount when the venue did not report a
    fill, so accounting degrades rather than silently recording zero.
    """
    price = None
    amount = None
    if isinstance(order, dict):
        for key in ('average', 'price'):
            value = order.get(key)
            if value:
                try:
                    price = float(value)
                except (TypeError, ValueError):
                    price = None
                if price:
                    break
        for key in ('filled', 'amount'):
            value = order.get(key)
            if value:
                try:
                    amount = float(value)
                except (TypeError, ValueError):
                    amount = None
                if amount:
                    break
        info = order.get('info') if isinstance(order.get('info'), dict) else {}
        if not price:
            for key in ('avgPrice', 'averagePrice'):
                try:
                    candidate = float(info.get(key) or 0)
                except (TypeError, ValueError):
                    candidate = 0.0
                if candidate:
                    price = candidate
                    break
        if not amount:
            try:
                candidate = float(info.get('executedQty') or 0)
            except (TypeError, ValueError):
                candidate = 0.0
            if candidate:
                amount = candidate
    if not price and fallback_price:
        try:
            price = float(fallback_price)
        except (TypeError, ValueError):
            price = None
    if not amount and fallback_amount:
        try:
            amount = float(fallback_amount)
        except (TypeError, ValueError):
            amount = None
    return price, amount


def realized_pnl(side, entry_price, exit_price, amount):
    """Linear USDT PnL for a futures position. Returns ``None`` when unknown.

    ``None`` is deliberate: it means "cannot be computed", which the caller must
    not confuse with a break-even 0.0.
    """
    try:
        entry_price = float(entry_price or 0)
        exit_price = float(exit_price or 0)
        amount = float(amount or 0)
    except (TypeError, ValueError):
        return None
    if entry_price <= 0 or exit_price <= 0 or amount <= 0:
        return None
    if str(side).lower() in ('long', 'buy'):
        return (exit_price - entry_price) * amount
    return (entry_price - exit_price) * amount
