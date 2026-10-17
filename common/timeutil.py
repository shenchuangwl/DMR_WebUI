"""One timezone convention for everything the stop-loss logic compares.

The bug this replaces
---------------------
``entry_time`` was written with ``datetime.now()`` -- a *naive* timestamp in the
host's local zone -- while the K-line index (naive UTC, as ccxt returns it) was
run through ``tz_localize('UTC').tz_convert('Asia/Singapore').tz_localize(None)``,
i.e. shifted +8h. The two were then compared directly in
``df['datetime'] > entry_datetime``, so the comparison was only correct when the
host happened to run in UTC+8.

On a UTC host the entry timestamp lands 8 hours *before* the real entry, so
``after_entry`` immediately contains candles from before the position was opened
and the stop anchor is computed from pre-entry bars.

Convention now: every timestamp used for comparison is **naive UTC**.
"""
from datetime import datetime, timezone

import pandas as pd


def utc_now():
    """Timezone-aware 'now' in UTC."""
    return datetime.now(timezone.utc)


def utc_now_iso():
    """ISO-8601 'now' carrying an explicit UTC offset, so it is unambiguous."""
    return utc_now().isoformat()


def to_naive_utc(value, assume_naive_is_local=True):
    """Normalise ``value`` to a naive-UTC ``pd.Timestamp``.

    A tz-aware input is *converted* to UTC (the old code called
    ``tz_localize(None)``, which discards the offset instead of applying it).

    A naive input is ambiguous. Records written before this fix used the host's
    local clock, so ``assume_naive_is_local=True`` interprets them that way and
    converts, keeping positions opened by the old code correct across the
    upgrade. Pass ``False`` for feeds that are known to be UTC already (such as
    ccxt K-line indexes).
    """
    if value is None:
        return None
    stamp = pd.to_datetime(value)
    if getattr(stamp, 'tzinfo', None) is not None or getattr(stamp, 'tz', None) is not None:
        return stamp.tz_convert('UTC').tz_localize(None)
    if assume_naive_is_local:
        local_zone = datetime.now().astimezone().tzinfo
        return stamp.tz_localize(local_zone).tz_convert('UTC').tz_localize(None)
    return stamp


def index_to_naive_utc(index):
    """Normalise a pandas DatetimeIndex to naive UTC.

    ccxt returns millisecond epochs, so a naive index is already UTC and must
    not be shifted.
    """
    if getattr(index, 'tz', None) is not None:
        return index.tz_convert('UTC').tz_localize(None)
    return index
