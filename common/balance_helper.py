"""Read USDT from an already fetched ccxt balance; never issue requests.

Binance's updateTime=0 filter can omit a funded asset from the unified
mapping. Recover only that omitted asset from the same response's info,
using ccxt's marginBalance/availableBalance/initialMargin semantics.
"""
from decimal import Decimal
import math


def extract_usdt_balance(balance):
    account = dict(balance.get('USDT') or {})
    for key in ('total', 'free', 'used'):
        if account.get(key) is None:
            account[key] = (balance.get(key) or {}).get('USDT')
    if not any(value is not None for value in account.values()):
        info = balance.get('info') or {}
        assets = info if isinstance(info, list) else info.get('assets', [])
        matches = [asset for asset in assets if asset.get('asset') == 'USDT']
        if len(matches) > 1:
            raise ValueError('Ambiguous USDT balance')
        if matches:
            asset = matches[0]
            account = {'total': asset.get('marginBalance', asset.get('balance')),
                       'free': asset.get('availableBalance'),
                       'used': asset.get('initialMargin')}
            if sum(value is not None for value in account.values()) < 2:
                raise ValueError('Incomplete raw USDT balance')
    values = {key: Decimal(str(value)) if value is not None else None
              for key, value in account.items()}
    if any(value is not None and not value.is_finite() for value in values.values()):
        raise ValueError('Non-finite USDT balance')
    total, free, used = (values.get(key) for key in ('total', 'free', 'used'))
    if total is None and free is not None and used is not None:
        total = free + used
    if free is None and total is not None and used is not None:
        free = total - used
    if used is None and total is not None and free is not None:
        used = total - free
    result = dict(zip(('total', 'free', 'used'), (float(x) if x is not None else 0.0 for x in (total, free, used))))
    if not all(math.isfinite(value) for value in result.values()):
        raise ValueError('USDT balance overflow')
    return result