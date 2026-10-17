"""源码段哈希夹具：逐条复算 source_hashes.json 记录的 DMR 源码段，DMR 源码一变就报出来（报告 5.3 第 4 条）。

另外检查：按名字定位的定义行号与记录一致；算法文件头写明了来源路径、行号与 sha256（与记录一致，没有过期的哈希），
并保留 DMR 的 Apache-2.0 版权声明（版权人照 DMR_WebUI/NOTICE 的写法）。
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

import dmr_source as ds

ENTRIES = json.loads(Path(__file__).with_name("source_hashes.json").read_text(encoding="utf-8"))["segments"]
DEFS = [e for e in ENTRIES if e["kind"] == "def"]
ALGO_MODULES = ("dmr", "signal", "stoploss", "sizing", "rounding", "pnl", "riskeval")
NOTICE = ("Copyright 2026 shenchuangwl", "Licensed under the Apache License, Version 2.0", "DMR_WebUI/LICENSE")
HEX64 = re.compile(r"\b[0-9a-f]{64}\b")


def _ids(entries):
    return [e["id"] for e in entries]


def _algo_docstring(rel: str) -> str:
    path = ds.WEBUI_ROOT / rel
    assert path.is_file(), f"算法文件不存在：{rel}"
    doc = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8")))
    assert doc, f"{rel} 没有文件头说明"
    return doc


def test_entries_are_well_formed():
    ids = _ids(ENTRIES)
    assert len(ids) == len(set(ids)), "id 重复"
    for e in ENTRIES:
        assert e["kind"] in ("def", "segment"), e["id"]
        assert isinstance(e["start"], int) and isinstance(e["end"], int) and 1 <= e["start"] <= e["end"], e["id"]
        assert re.fullmatch(r"[0-9a-f]{64}", e["sha256"]), e["id"]
        assert e["note"], e["id"]
        if e["kind"] == "def":
            assert e["name"], e["id"]
        if e["algo"] is not None:
            assert e["algo"] in {f"dmr_kernel/algo/{m}.py" for m in ALGO_MODULES}, e["id"]


@pytest.mark.parametrize("entry", ENTRIES, ids=_ids(ENTRIES))
def test_segment_hash_matches_dmr_source(entry):
    text = ds.segment(entry["path"], entry["start"], entry["end"])
    assert ds.sha256(text) == entry["sha256"], (
        f"DMR 源码段变了：{entry['path']}:{entry['start']}-{entry['end']}（{entry['note']}）。"
        f"先重新核对 {entry['algo']} 的誊写与对拍，再更新 source_hashes.json 与文件头。")


@pytest.mark.parametrize("entry", DEFS, ids=_ids(DEFS))
def test_def_is_located_at_recorded_lines(entry):
    assert ds.locate_def(entry["path"], entry["name"], entry["cls"]) == (entry["start"], entry["end"])


@pytest.mark.parametrize("entry", [e for e in ENTRIES if e["algo"]], ids=_ids([e for e in ENTRIES if e["algo"]]))
def test_algo_header_cites_path_lines_and_hash(entry):
    doc = _algo_docstring(entry["algo"])
    assert f"{entry['path']}:{entry['start']}-{entry['end']}" in doc
    assert entry["sha256"] in doc


@pytest.mark.parametrize("module", ALGO_MODULES)
def test_algo_header_has_no_stale_hash_and_keeps_apache_notice(module):
    rel = f"dmr_kernel/algo/{module}.py"
    doc = _algo_docstring(rel)
    recorded = {e["sha256"] for e in ENTRIES if e["algo"] == rel}
    assert recorded, f"{rel} 没有登记任何来源段"
    assert set(HEX64.findall(doc)) == recorded, "文件头里的哈希与 source_hashes.json 不一致"
    for line in NOTICE:
        assert line in doc
