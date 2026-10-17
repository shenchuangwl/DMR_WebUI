"""对拍基础设施自检：只读打开 DMR 的源码与 CSV，按 ast 取源码，在隔离的字典命名空间里执行。

防止基础设施失效而静默通过：越界路径、密钥文件、运行期状态目录一律拒绝；执行原实现不经 import 系统、
不写字节码、不在 DMR 目录产生任何文件。
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

import pandas as pd
import pytest

import dmr_source as ds

TIMEUTIL = "common/timeutil.py"


def test_roots_point_at_dmr_and_webui():
    assert (ds.WEBUI_ROOT / "run_tests.sh").is_file()
    assert ds.DMR_ROOT == ds.WEBUI_ROOT.parent
    assert (ds.DMR_ROOT / TIMEUTIL).is_file()


def test_read_text_reads_dmr_source():
    assert "def to_naive_utc(" in ds.read_text(TIMEUTIL)


@pytest.mark.parametrize("rel", [
    "../outside.py",  # DMR_WebUI 之外
    "/etc/hostname",  # 绝对路径
    "webui/dmr_kernel/core/types.py",  # webui 自己不算 DMR 源码
    ".env",  # 密钥
    "config/.env.py",
    "data/runtime_state/state.py",  # 运行期状态目录：替身环境下一律不打开
    "LICENSE",  # 只允许 .py 与 .csv
])
def test_dmr_path_refuses_anything_but_dmr_sources_and_csv(rel):
    with pytest.raises((ValueError, PermissionError)):
        ds.dmr_path(rel)


def test_segment_is_an_inclusive_one_based_line_range():
    lines = ds.read_text(TIMEUTIL).splitlines(keepends=True)
    assert ds.segment(TIMEUTIL, 33, 34) == lines[32] + lines[33]
    for start, end in [(0, 3), (5, 4), (1, len(lines) + 1)]:
        with pytest.raises(ValueError):
            ds.segment(TIMEUTIL, start, end)


def test_sha256_is_hex_digest_of_utf8_text():
    assert ds.sha256("") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    # UTF-8 编码后再算：「中」= e4 b8 ad
    assert ds.sha256("中") == "a567bdaa11367f260f0708391f4d10766b5962f565d9e432f17981a3584fe1e2"


def test_extract_and_locate_def_for_function_method_and_class():
    src = ds.extract_def(TIMEUTIL, "to_naive_utc")
    assert src.startswith("def to_naive_utc(")
    start, end = ds.locate_def(TIMEUTIL, "to_naive_utc")
    assert ds.segment(TIMEUTIL, start, start).lstrip().startswith("def to_naive_utc(")
    assert end > start
    method = ds.extract_def("common/precision_helper.py", "_round_down_decimal", cls="PrecisionHelper")
    assert method.lstrip().startswith("def _round_down_decimal(")
    klass = ds.extract_def("common/risk_state.py", "RiskDecision")
    assert klass.startswith("class RiskDecision")
    with pytest.raises(LookupError):
        ds.extract_def(TIMEUTIL, "no_such_function")
    with pytest.raises(LookupError):
        ds.extract_def("common/precision_helper.py", "_round_down_decimal", cls="NoSuchClass")


def test_compile_def_executes_in_a_new_dict_without_import_or_files():
    common_before = sorted(p.name for p in (ds.DMR_ROOT / "common").iterdir())
    modules_before = set(sys.modules)
    ns = {"pd": pd, "datetime": datetime, "timezone": timezone}
    fn = ds.compile_def(ds.extract_def(TIMEUTIL, "to_naive_utc"), ns, label="timeutil.to_naive_utc")
    assert fn.__code__.co_filename == "<dmr:timeutil.to_naive_utc>"
    assert fn.__globals__ is not ns and "to_naive_utc" not in ns
    assert str(fn("2026-09-17T20:00:00+08:00")) == "2026-09-17 12:00:00"
    assert ds.dmr_module_violations(set(sys.modules) - modules_before) == []
    assert sorted(p.name for p in (ds.DMR_ROOT / "common").iterdir()) == common_before


def test_compile_def_does_not_inherit_future_flags_of_the_caller():
    # 本文件有 from __future__ import annotations；原实现必须按它自己的语义编译（注解在定义时求值）
    with pytest.raises(NameError):
        ds.compile_def("def f(x: NotDefinedAnywhere):\n    return x\n", {}, label="probe")


def test_compile_segment_wraps_statement_lines_into_a_function():
    # common/timeutil.py 的 to_naive_utc 里 `if value is None: return None` 两行之后的语句段
    start, _ = ds.locate_def(TIMEUTIL, "to_naive_utc")
    text = ds.read_text(TIMEUTIL).splitlines()
    line = next(i for i in range(start, start + 30) if text[i - 1].strip().startswith("stamp = pd.to_datetime"))
    fn = ds.compile_segment(TIMEUTIL, line, line, params="value", returns="stamp", globals_={"pd": pd},
                            label="timeutil.stamp")
    assert fn("2026-09-17 12:00") == pd.Timestamp("2026-09-17 12:00")
    assert fn.__code__.co_filename == "<dmr:timeutil.stamp>"


def test_dmr_module_violations_flags_modules_loaded_from_dmr_tree():
    class Fake:
        __file__ = str(ds.DMR_ROOT / "common" / "timeutil.py")

    class Ours:
        __file__ = str(ds.WEBUI_ROOT / "dmr_kernel" / "core" / "types.py")

    sys.modules["_probe_dmr_mod"] = Fake  # type: ignore[assignment]
    sys.modules["_probe_our_mod"] = Ours  # type: ignore[assignment]
    try:
        assert ds.dmr_module_violations({"_probe_dmr_mod", "_probe_our_mod"}) == ["_probe_dmr_mod"]
    finally:
        del sys.modules["_probe_dmr_mod"], sys.modules["_probe_our_mod"]


def test_no_dmr_module_is_imported_in_this_process():
    assert ds.dmr_module_violations(set(sys.modules)) == []


def test_load_csv_reads_gold_csv_read_only():
    df = ds.load_csv("bit/data/bit_short_term/CYS_USDT_short_term_data.csv")
    assert list(df.columns)[:6] == ["time", "open", "high", "low", "close", "volume"]
    assert len(df) == 1000
