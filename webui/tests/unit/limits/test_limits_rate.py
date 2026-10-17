"""订单频率：同一合约 10 分钟内的订单数必须 < max_orders_per_symbol_10m（note_order 记一笔，order_rate_ok 判定）。"""
from __future__ import annotations

T0 = 1_760_000_000_000
WINDOW = 600_000  # 10 分钟


def note_many(limits, symbol, count, *, start_ms=T0, step_ms=1000):
    for i in range(count):
        limits.note_order(symbol, start_ms + i * step_ms)


def test_a_symbol_without_any_order_is_ok(limits):
    assert limits.order_rate_ok("BTCUSDT", T0) is True


def test_the_count_must_stay_below_the_limit(limits, config):
    config["max_orders_per_symbol_10m"] = 5
    note_many(limits, "BTCUSDT", 4)
    assert limits.order_rate_ok("BTCUSDT", T0 + 4000) is True  # 4 < 5
    limits.note_order("BTCUSDT", T0 + 4000)
    assert limits.order_rate_ok("BTCUSDT", T0 + 4000) is False  # 5 不再 < 5


def test_the_default_limit_is_25(limits):
    note_many(limits, "BTCUSDT", 24)
    assert limits.order_rate_ok("BTCUSDT", T0 + 30_000) is True
    limits.note_order("BTCUSDT", T0 + 30_000)
    assert limits.order_rate_ok("BTCUSDT", T0 + 30_000) is False


def test_symbols_are_counted_separately(limits, config):
    config["max_orders_per_symbol_10m"] = 3
    note_many(limits, "BTCUSDT", 3)
    assert limits.order_rate_ok("BTCUSDT", T0 + 5000) is False
    assert limits.order_rate_ok("ETHUSDT", T0 + 5000) is True
    assert limits.order_rate_ok("币安人生USDT", T0 + 5000) is True


def test_the_window_slides_an_order_leaves_it_after_exactly_ten_minutes(limits, config):
    config["max_orders_per_symbol_10m"] = 2
    limits.note_order("BTCUSDT", T0)
    limits.note_order("BTCUSDT", T0 + 100_000)
    assert limits.order_rate_ok("BTCUSDT", T0 + 100_000) is False
    assert limits.order_rate_ok("BTCUSDT", T0 + WINDOW - 1) is False  # 第一笔还在 10 分钟内
    assert limits.order_rate_ok("BTCUSDT", T0 + WINDOW) is True  # 第一笔刚好满 10 分钟：出窗口
    assert limits.order_rate_ok("BTCUSDT", T0 + WINDOW + 100_000) is True


def test_only_orders_inside_the_window_are_counted(limits, config):
    config["max_orders_per_symbol_10m"] = 3
    note_many(limits, "BTCUSDT", 10, start_ms=T0, step_ms=1000)  # 一阵早先的下单
    far_later = T0 + 3 * WINDOW
    assert limits.order_rate_ok("BTCUSDT", far_later) is True
    note_many(limits, "BTCUSDT", 2, start_ms=far_later)
    assert limits.order_rate_ok("BTCUSDT", far_later + 2000) is True
    limits.note_order("BTCUSDT", far_later + 2000)
    assert limits.order_rate_ok("BTCUSDT", far_later + 2000) is False


def test_the_limit_is_read_from_the_config_on_every_call(limits, config):
    note_many(limits, "BTCUSDT", 10)
    config["max_orders_per_symbol_10m"] = 10
    assert limits.order_rate_ok("BTCUSDT", T0 + 20_000) is False
    config["max_orders_per_symbol_10m"] = 11
    assert limits.order_rate_ok("BTCUSDT", T0 + 20_000) is True


def test_notes_may_arrive_out_of_order(limits, config):
    config["max_orders_per_symbol_10m"] = 3
    limits.note_order("BTCUSDT", T0 + 5000)
    limits.note_order("BTCUSDT", T0 + 1000)
    limits.note_order("BTCUSDT", T0 + 3000)
    assert limits.order_rate_ok("BTCUSDT", T0 + 6000) is False  # 三笔都在窗口里
    assert limits.order_rate_ok("BTCUSDT", T0 + 1000 + WINDOW) is True  # 最早（T0+1000）那笔刚好出窗口：剩两笔 < 3
