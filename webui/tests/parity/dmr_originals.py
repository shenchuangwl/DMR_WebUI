"""从 source_hashes.json 记录的行段编译出 DMR 原实现，给对拍测试调用；另有金标 CSV 的读取与格式换算。

- 执行的就是被哈希的那段原文：行号取自 source_hashes.json，test_source_hashes.py 保证它没有变。
- 每个原实现在自己的字典命名空间里执行，只给它需要的 pd/np、桩 self、桩 logger、静音的 print；
  它依赖的辅助函数（to_naive_utc 等）同样按名字从 DMR 源码取出编译，不经 import。
- 原实现里有「吞掉异常、只记日志」的分支：桩 logger 收到 error 时让测试失败，防止静默通过。
- 测试用例的数值（tests/offline/test_business_regressions.py、tests/test_security_fixes.py）只解析文本，不运行那两个文件。
"""
from __future__ import annotations

import ast
import json
import textwrap
import types
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np
import pandas as pd

import dmr_source as ds

ENTRIES: dict[str, dict[str, Any]] = {
    e["id"]: e
    for e in json.loads(Path(__file__).with_name("source_hashes.json").read_text(encoding="utf-8"))["segments"]
}

# 金标 CSV 的 time 列是新加坡时间（data_fetcher.save_data_to_csv：UTC → Asia/Singapore，格式 %Y-%m-%d %H:%M）；
# 新加坡自 1982 年起固定 UTC+8、无夏令时，这里用固定偏移换回 UTC，不依赖时区库。
SGT_OFFSET = pd.Timedelta(hours=8)
SHORT_GOLD_GLOB = "bit/data/bit_short_term/*_short_term_data.csv"
LONG_GOLD_GLOB = "bit/data/bit_long_term/*_long_term_data.csv"
# 原实现 _get_current_correction_config 读不到配置时的默认值（strategy_engine.py 第 178–179、188–189 行）
CORRECTION_DEFAULTS = {"correction_threshold": 0.0005, "correction_min_interval": 3600, "sequence_id": None,
                       "timeframe_combination": None, "is_matched_sequence": False, "description": "默认纠错配置"}


class StubLogger:
    """桩 logger：只把记录留在内存里。"""

    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def _log(self, level: str, msg: Any, *args: Any) -> None:
        self.records.append((level, str(msg) % args if args else str(msg)))

    def debug(self, msg, *args, **kwargs):
        self._log("debug", msg, *args)

    def info(self, msg, *args, **kwargs):
        self._log("info", msg, *args)

    def warning(self, msg, *args, **kwargs):
        self._log("warning", msg, *args)

    def error(self, msg, *args, **kwargs):
        self._log("error", msg, *args)

    def exception(self, msg, *args, **kwargs):
        self._log("error", msg, *args)

    def critical(self, msg, *args, **kwargs):
        self._log("error", msg, *args)

    def errors(self) -> list[str]:
        return [m for level, m in self.records if level == "error"]


def _quiet_print(*args: Any, **kwargs: Any) -> None:
    """原实现里的 print 只是调试输出：静音。"""


def source_of(ident: str) -> str:
    e = ENTRIES[ident]
    return ds.segment(e["path"], e["start"], e["end"])


def compile_recorded_def(ident: str, globals_: dict[str, Any]) -> Any:
    if ENTRIES[ident]["kind"] != "def":
        raise ValueError(f"{ident} 不是定义")
    return ds.compile_def(source_of(ident), globals_, label=ident)


def compile_recorded_segment(ident: str, *, params: str, returns: str, globals_: dict[str, Any]) -> Any:
    e = ENTRIES[ident]
    if e["kind"] != "segment":
        raise ValueError(f"{ident} 不是语句段")
    return ds.compile_segment(e["path"], e["start"], e["end"], params=params, returns=returns, globals_=globals_,
                              label=ident)


def _position(side: str | None) -> dict[str, Any] | None:
    """原实现的策略持仓记录：side 是小写的 'long' / 'short'。"""
    return None if side is None else {"side": side, "amount": 1.0}


# ---------------------------------------------------------------- DMR 指标


def calculate_dmr(n: int, *, long_term: bool = False) -> Callable[[pd.DataFrame], pd.DataFrame]:
    fn = compile_recorded_def("dmr.calculate_dmr_long" if long_term else "dmr.calculate_dmr_short", {"pd": pd})

    def run(df: pd.DataFrame) -> pd.DataFrame:
        stub = types.SimpleNamespace(dmr_period=n, logger=StubLogger())
        # calculate_log_dmr_* 读运行日志文件，不属于指标本身：换成原样返回
        stub.calculate_log_dmr_12 = stub.calculate_log_dmr_6 = lambda frame: frame
        out = fn(stub, df.copy())
        assert not stub.logger.errors(), stub.logger.errors()
        return out

    return run


# ---------------------------------------------------------------- 信号


def crossing() -> Callable[[Any, Any], Any]:
    fn = compile_recorded_segment("signal.crossing", params="self, previous_dmr, current_dmr",
                                  returns="crossover_signal", globals_={})
    return lambda prev, curr: fn(types.SimpleNamespace(logger=StubLogger()), prev, curr)


def long_trend() -> Callable[[pd.DataFrame | None], Any]:
    fn = compile_recorded_segment("signal.long_trend", params="self, long_df", returns="trend", globals_={})

    def run(long_df: pd.DataFrame | None) -> Any:
        stub = types.SimpleNamespace(logger=StubLogger(), long_dmr_period=6, last_long_dmr_value=None)
        return fn(stub, long_df)

    return run


def coordinate() -> Callable[[Any, Any, str | None], Any]:
    fn = compile_recorded_def("signal.coordinate", {})

    def run(short_signal: Any, trend: Any, position_side: str | None) -> Any:
        position = _position(position_side)
        stub = types.SimpleNamespace(logger=StubLogger(), get_strategy_position=lambda: position)
        return fn(stub, short_signal, trend)

    return run


def current_and_previous() -> Callable[[pd.DataFrame, int, Any], tuple[Any, Any]]:
    fn = compile_recorded_segment("signal.current_and_previous", params="self, df", returns="previous_dmr, current_dmr",
                                  globals_={})

    def run(df: pd.DataFrame, n: int, last_dmr_value: Any) -> tuple[Any, Any]:
        stub = types.SimpleNamespace(dmr_period=n, last_dmr_value=last_dmr_value, logger=StubLogger())
        return fn(stub, df)

    return run


def _correction_stub(position_side: str | None, enabled: bool) -> types.SimpleNamespace:
    position = _position(position_side)
    return types.SimpleNamespace(
        config={"signal_config": {"position_correction_enabled": enabled}},
        logger=StubLogger(),
        get_strategy_position=lambda: position,
        _get_current_correction_config=lambda: dict(CORRECTION_DEFAULTS),
    )


_FROZEN_TIME = types.SimpleNamespace(time=lambda: 1_000_000.0)


def correction() -> Callable[..., Any]:
    fn = compile_recorded_def("signal.correction", {"time": _FROZEN_TIME})

    def run(current_dmr: float, position_side: str | None, *, enabled: bool = True) -> Any:
        return fn(_correction_stub(position_side, enabled), current_dmr)

    return run


def signal_priority() -> Callable[..., Any]:
    """detect_signal 第 296–302 行：协调后的穿越信号，没有时改用纠错信号。"""
    seg = compile_recorded_segment("signal.priority", params="self, crossover_signal, long_term_trend, current_dmr",
                                   returns="signal", globals_={})
    coord = compile_recorded_def("signal.coordinate", {})
    corr = compile_recorded_def("signal.correction", {"time": _FROZEN_TIME})

    def run(crossover_signal: Any, trend: Any, current_dmr: float, position_side: str | None, *,
            correction_enabled: bool = True) -> Any:
        stub = _correction_stub(position_side, correction_enabled)
        stub._coordinate_dual_timeframe_signal = types.MethodType(coord, stub)
        stub._detect_position_correction_signal = types.MethodType(corr, stub)
        return seg(stub, crossover_signal, trend, current_dmr)

    return run


# ---------------------------------------------------------------- 止损


def _timeutil(name: str) -> Callable[..., Any]:
    return compile_recorded_def(f"timeutil.{name}", {"pd": pd, "datetime": datetime, "timezone": timezone})


def trailing_stop_manager() -> types.SimpleNamespace:
    """桩 self，挂上原实现的 calculate_atr、get_6kline_range、_ensure_datetime_column、calculate_stop_loss_price。

    不构造 TrailingStopManager（构造函数会建目录、读写持仓文件、发运行时警告）。
    """
    globals_ = {"pd": pd, "np": np, "print": _quiet_print,
                "to_naive_utc": _timeutil("to_naive_utc"), "index_to_naive_utc": _timeutil("index_to_naive_utc")}
    stub = types.SimpleNamespace()
    for ident, attr in (("stoploss.calculate_atr", "calculate_atr"),
                        ("stoploss.get_6kline_range", "get_6kline_range"),
                        ("stoploss.ensure_datetime_column", "_ensure_datetime_column"),
                        ("stoploss.calculate_stop_loss_price", "calculate_stop_loss_price")):
        setattr(stub, attr, types.MethodType(compile_recorded_def(ident, globals_), stub))
    return stub


def to_naive_utc() -> Callable[..., Any]:
    return _timeutil("to_naive_utc")


def is_better() -> Callable[[str, Any, Any], bool]:
    return compile_recorded_segment("stoploss.is_better", params="side, current_stop_loss, new_stop_loss",
                                    returns="should_update", globals_={})


def cycle_count() -> Callable[[pd.DataFrame], int]:
    return compile_recorded_segment("stoploss.cycle_count", params="after_entry", returns="completed_cycles",
                                    globals_={})


# ---------------------------------------------------------------- 取整、数量、盈亏


def round_down_decimal() -> Callable[..., Decimal]:
    fn = compile_recorded_def("rounding.round_down_decimal",
                              {"Decimal": Decimal, "ROUND_DOWN": ROUND_DOWN, "Optional": Optional})
    return lambda value, precision, step=None: fn(None, value, precision, step)


def decimal_to_str() -> Callable[[Decimal, int], str]:
    fn = compile_recorded_def("rounding.decimal_to_str", {"Decimal": Decimal, "ROUND_DOWN": ROUND_DOWN})
    return lambda value, precision: fn(None, value, precision)


def calculate_position_size() -> Callable[[Any, Any], Any]:
    fn = compile_recorded_def("sizing.calculate_position_size", {"print": _quiet_print})
    return lambda position_size, current_price: fn(types.SimpleNamespace(position_size=position_size), current_price)


def realized_pnl() -> Callable[..., Any]:
    return compile_recorded_def("pnl.realized_pnl", {})


def _literal_float(node: ast.AST) -> float:
    """float('nan')、-float('inf') 这类表达式的值（只认这几种写法）。"""
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_literal_float(node.operand)
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "float"
            and len(node.args) == 1):
        return float(ast.literal_eval(node.args[0]))
    raise ValueError(f"无法识别的数值表达式：{ast.dump(node)}")


def _is_call(node: ast.AST, name: str) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name


def precision_regression_cases() -> dict[str, Any]:
    """从 tests/offline/test_business_regressions.py 第 99–108 行的文本里找出取整用例的数值（不运行该文件）。"""
    tree = ast.parse(textwrap.dedent(source_of("rounding.regression_cases")))
    found: dict[str, Any] = {"tick": None, "step": None, "price": [], "quantity": [], "nonfinite": []}
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            try:
                d = ast.literal_eval(node)
            except ValueError:
                continue
            if d.get("filterType") == "PRICE_FILTER":
                found["tick"] = d["tickSize"]
            elif d.get("filterType") == "LOT_SIZE":
                found["step"] = d["stepSize"]
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "assertEqual":
            got, expected = node.args
            if isinstance(got, ast.Call) and isinstance(got.func, ast.Attribute):
                kind = {"format_price_str": "price", "format_quantity_str": "quantity"}.get(got.func.attr)
                if kind:
                    found[kind].append((ast.literal_eval(got.args[1]), ast.literal_eval(expected)))
        elif isinstance(node, ast.For) and isinstance(node.target, ast.Name) and node.target.id == "value":
            found["nonfinite"] = [_literal_float(elt) for elt in node.iter.elts]
    if not (found["tick"] and found["step"] and found["price"] and found["quantity"] and found["nonfinite"]):
        raise LookupError(f"没能从回归测试文本里找全用例：{found}")
    return found


def pnl_regression_cases() -> list[tuple[Any, ...]]:
    """从 tests/test_security_fixes.py 第 103–107 行的文本里找出盈亏用例（side, entry, exit, amount, 期望值或 None）。"""
    tree = ast.parse(textwrap.dedent(source_of("pnl.regression_cases")))
    cases: list[tuple[Any, ...]] = []
    for node in ast.walk(tree):
        if not (_is_call(node, "check") and len(node.args) == 2 and isinstance(node.args[1], ast.Compare)):
            continue
        cond = node.args[1]
        if isinstance(cond.ops[0], ast.Lt) and _is_call(cond.left, "abs"):
            inner = cond.left.args[0]
            if isinstance(inner, ast.BinOp) and _is_call(inner.left, "realized_pnl"):
                c = ast.literal_eval(inner.right)
                expected = -c if isinstance(inner.op, ast.Add) else c
                cases.append(tuple(ast.literal_eval(a) for a in inner.left.args) + (expected,))
        elif isinstance(cond.ops[0], ast.Is) and _is_call(cond.left, "realized_pnl"):
            cases.append(tuple(ast.literal_eval(a) for a in cond.left.args) + (ast.literal_eval(cond.comparators[0]),))
    if len(cases) < 3:
        raise LookupError(f"没能从回归测试文本里找全盈亏用例：{cases}")
    return cases


# ---------------------------------------------------------------- 金标 CSV 与格式换算


def gold_files() -> list[tuple[str, int, str]]:
    """(相对路径, DMR 周期, 金标列名)：短周期 DMR12、长周期 DMR6。"""
    short = sorted(p.relative_to(ds.DMR_ROOT).as_posix() for p in ds.DMR_ROOT.glob(SHORT_GOLD_GLOB))
    long_ = sorted(p.relative_to(ds.DMR_ROOT).as_posix() for p in ds.DMR_ROOT.glob(LONG_GOLD_GLOB))
    return [(rel, 12, "dmr_12") for rel in short] + [(rel, 6, "dmr_6") for rel in long_]


def gold_all_rows(rel: str) -> pd.DataFrame:
    """金标全部行（含获取时未收盘的末行），另加 open_time_ms（UTC 毫秒）。"""
    raw = ds.load_csv(rel)
    open_utc = pd.to_datetime(raw["time"], format="%Y-%m-%d %H:%M") - SGT_OFFSET
    out = raw.copy()
    out.insert(0, "open_time_ms", ((open_utc - pd.Timestamp("1970-01-01")) // pd.Timedelta(milliseconds=1)).astype("int64"))
    assert out["open_time_ms"].is_monotonic_increasing and out["open_time_ms"].is_unique, rel
    return out


def gold_closed(rel: str) -> pd.DataFrame:
    """金标去掉末行（获取时未收盘的那根），报告 5.3。"""
    return gold_all_rows(rel).iloc[:-1].reset_index(drop=True)


def original_frame(bars: pd.DataFrame) -> pd.DataFrame:
    """新接口的 bars（open_time_ms 列）换成原实现吃的格式：naive UTC 的 DatetimeIndex（与 ccxt K 线一致）。"""
    idx = pd.DatetimeIndex(pd.to_datetime(bars["open_time_ms"].to_numpy(), unit="ms"), name="timestamp")
    cols = [c for c in ("open", "high", "low", "close", "volume") if c in bars.columns]
    return pd.DataFrame({c: bars[c].to_numpy() for c in cols}, index=idx)


def index_ms(frame: pd.DataFrame) -> list[int]:
    """原实现返回的 K 线（DatetimeIndex）的开盘时间，UTC 毫秒。"""
    return [int((ts - pd.Timestamp("1970-01-01")) // pd.Timedelta(milliseconds=1)) for ts in frame.index]


def utc(ms: int) -> pd.Timestamp:
    """带 UTC 时区的时间（传给原实现的起算时间一律带时区）。"""
    return pd.Timestamp(ms, unit="ms", tz="UTC")
