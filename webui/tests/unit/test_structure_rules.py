"""结构规则的静态检查（计划报告 4.1 的四条结构规则与 4.3 的分层；docs/设计约定.md 第 5 节）。

检查项：
1. 分层：一个模块只能导入比自己低的层；同层不同组之间不得直接互相导入（0a 层只允许导入 core.types）。
2. 禁止的导入：任何 DMR 模块；三个包之间互不导入（内核、控制台、替身交易所各自独立）。
3. 库的使用范围：sqlalchemy/alembic/sqlite3 只在 ledger；ccxt 只在 exchange；apprise 只在 alerts；
   apscheduler 只在 core.scheduler；fastapi/starlette 只在 api。
4. 适配层写方法只在 core.coordinator 与 cli.emergency 调用；WriteCapability 只在装配层与应急入口构造。
5. 算法包没有任何 I/O：不导入 os/time/datetime/logging 等，不调用 open/print，不读时钟。
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

WEBUI = Path(__file__).resolve().parents[2]

# 分组前缀 -> 层。分组按「点分段」最长前缀匹配；core.* 与 models.* 每个模块各自成组。
LAYER_MAP: dict[str, object] = {
    "dmr_kernel": "pkg",
    "dmr_kernel.core": "pkg",
    "dmr_kernel.models": "pkg",
    "dmr_kernel.core.types": "0a",
    "dmr_kernel.core.clock": "0a",
    "dmr_kernel.core.crashpoints": "0a",
    "dmr_kernel.core.configdefs": "0a",
    "dmr_kernel.settings": "0a",
    "dmr_kernel.core.lifecycle": "0a",
    "dmr_kernel.algo": "0b",
    "dmr_kernel.core.events": "0b",
    "dmr_kernel.exchange": 1,
    "dmr_kernel.ledger": 1,
    "dmr_kernel.marketdata": 2,
    "dmr_kernel.listsync": 2,
    "dmr_kernel.alerts": 2,
    "dmr_kernel.core.config": 2,
    "dmr_kernel.core.modelstate": 3,
    "dmr_kernel.core.registry": 3,
    "dmr_kernel.core.intents": 3,
    "dmr_kernel.core.orders": 3,
    "dmr_kernel.core.ingest": 3,
    "dmr_kernel.core.riskgate": 3,
    "dmr_kernel.core.limits": 3,
    "dmr_kernel.core.coordinator": 4,
    "dmr_kernel.models.base": "5base",
    "dmr_kernel.core.protection": 5,
    "dmr_kernel.core.reconcile": 5,
    "dmr_kernel.core.guard": 5,
    "dmr_kernel.models.m1_strategy": 5,
    "dmr_kernel.models.m2_takeover": 5,
    "dmr_kernel.models.m3_reserved": 5,
    "dmr_kernel.core.recovery": 6,
    "dmr_kernel.ops": 6,
    "dmr_kernel.api": 7,
    "dmr_kernel.cli": 7,
    "dmr_kernel.core.scheduler": 7,
    "dmr_kernel.__main__": 8,
}
RANK = {"0a": 0.0, "0b": 0.5, 1: 1, 2: 2, 3: 3, 4: 4, "5base": 4.5, 5: 5, 6: 6, 7: 7, 8: 8}

DMR_TOP_LEVEL = {
    "common", "config", "bit", "strategy", "execution", "utils", "data", "risk_management",
    "monitoring", "src", "tests", "analyze_markets", "backtest_dmr_quadrant", "dmr_analysis",
    "dmr_quadrant_analyzer", "main", "first_short_term_main", "first_long_term_main",
    "short_term_main", "long_term_main",
}
BANNED_LIBS = {"backtrader", "binance", "aiosqlite"}
PACKAGES = ("dmr_kernel", "dmr_console", "dmr_standin")
LIB_SCOPE = {
    "sqlalchemy": ("dmr_kernel.ledger",),
    "alembic": ("dmr_kernel.ledger",),
    "sqlite3": ("dmr_kernel.ledger",),
    "ccxt": ("dmr_kernel.exchange",),
    "apprise": ("dmr_kernel.alerts",),
    "apscheduler": ("dmr_kernel.core.scheduler",),
    "fastapi": ("dmr_kernel.api",),
    "starlette": ("dmr_kernel.api",),
    "uvicorn": ("dmr_kernel.api", "dmr_kernel.core.scheduler", "dmr_kernel.__main__"),
    "nicegui": (),
}
WRITE_METHODS = {"create_market_order", "create_stop_order", "cancel_order", "cancel_algo_order",
                 "set_leverage", "set_margin_mode"}
WRITE_CALLERS = ("dmr_kernel.core.coordinator", "dmr_kernel.cli.emergency", "dmr_kernel.exchange")
CAPABILITY_BUILDERS = ("dmr_kernel.core.scheduler", "dmr_kernel.cli.emergency", "dmr_kernel.exchange.adapter")
ALGO_ALLOWED_IMPORTS = {"__future__", "math", "decimal", "dataclasses", "typing", "enum", "functools",
                        "itertools", "collections", "operator", "statistics", "numbers", "pandas", "numpy"}
ALGO_BANNED_CALLS = {"open", "print", "input", "exec", "eval", "compile", "__import__", "breakpoint"}
ALGO_BANNED_ATTRS = {"now", "utcnow", "today", "time", "monotonic", "perf_counter", "to_csv", "to_json",
                     "to_parquet", "to_pickle", "to_sql", "to_excel", "to_hdf", "to_feather", "to_html",
                     "to_clipboard", "urlopen"}


@dataclass(frozen=True)
class Source:
    text: str
    is_package: bool


def _in(mod: str, prefix: str) -> bool:
    return mod == prefix or mod.startswith(prefix + ".")


def group_of(mod: str) -> str | None:
    best = None
    for prefix in LAYER_MAP:
        if _in(mod, prefix) and (best is None or len(prefix) > len(best)):
            best = prefix
    if best in ("dmr_kernel", "dmr_kernel.core", "dmr_kernel.models") and mod != best:
        return None  # 包下面没有登记的模块
    return best


def _resolve(importer: str, is_package: bool, node: ast.ImportFrom, modules: set[str]) -> list[str]:
    if node.level:
        parts = importer.split(".")
        base_parts = parts if is_package else parts[:-1]
        base_parts = base_parts[: len(base_parts) - (node.level - 1)]
        base = ".".join(base_parts)
        target = f"{base}.{node.module}" if node.module else base
    else:
        target = node.module or ""
    out = []
    for alias in node.names:
        cand = f"{target}.{alias.name}"
        out.append(cand if cand in modules else target)
    return out


def imports_of(mod: str, src: Source, modules: set[str]) -> list[str]:
    tree = ast.parse(src.text)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.extend(_resolve(mod, src.is_package, node, modules))
    return found


def analyze(sources: dict[str, Source]) -> list[str]:
    modules = set(sources)
    problems: list[str] = []
    for mod, src in sorted(sources.items()):
        pkg = mod.split(".")[0]
        tree = ast.parse(src.text)
        imported = imports_of(mod, src, modules)
        if pkg == "dmr_kernel":
            g1 = group_of(mod)
            if g1 is None:
                problems.append(f"{mod}: 不在分层表里（先在 docs/设计约定.md 第 5 节登记）")
                continue
        for target in imported:
            top = target.split(".")[0]
            if top in DMR_TOP_LEVEL:
                problems.append(f"{mod}: 导入了 DMR 模块 {target}")
            if top in BANNED_LIBS:
                problems.append(f"{mod}: 导入了禁用的库 {target}")
            if top in PACKAGES and top != pkg:
                problems.append(f"{mod}: 跨包导入 {target}（内核、控制台、替身交易所互不导入）")
            if pkg == "dmr_kernel" and top in LIB_SCOPE:
                if not any(_in(mod, p) for p in LIB_SCOPE[top]):
                    problems.append(f"{mod}: 不允许导入 {top}（只限 {LIB_SCOPE[top] or '无'}）")
            if pkg == "dmr_kernel" and top == "dmr_kernel":
                g1, g2 = group_of(mod), group_of(target)
                if g2 is None:
                    problems.append(f"{mod}: 导入了未登记的模块 {target}")
                    continue
                l1, l2 = LAYER_MAP[g1], LAYER_MAP[g2]
                if l2 == "pkg" or g1 == g2:
                    continue
                if l1 == "pkg":
                    problems.append(f"{mod}: 包初始化文件不得导入内核模块 {target}")
                    continue
                if l1 == "0a" and g2 == "dmr_kernel.core.types":
                    continue
                if l2 == "5base" and g1.startswith("dmr_kernel.models."):
                    continue
                if RANK[l1] <= RANK[l2]:
                    problems.append(f"{mod}（层 {l1}）不得导入 {target}（层 {l2}）")
        if pkg == "dmr_kernel":
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    if node.func.attr in WRITE_METHODS and not any(_in(mod, p) for p in WRITE_CALLERS):
                        problems.append(f"{mod}: 调用了适配层写方法 {node.func.attr}（只有协调者与应急入口可以）")
                if isinstance(node, ast.Call):
                    name = node.func.id if isinstance(node.func, ast.Name) else (
                        node.func.attr if isinstance(node.func, ast.Attribute) else None)
                    if name == "WriteCapability" and not any(_in(mod, p) for p in CAPABILITY_BUILDERS):
                        problems.append(f"{mod}: 构造了 WriteCapability（只有装配层与应急入口可以）")
            if _in(mod, "dmr_kernel.algo"):
                for target in imported:
                    top = target.split(".")[0]
                    if top == "dmr_kernel":
                        if not (_in(target, "dmr_kernel.algo") or target == "dmr_kernel.core.types"):
                            problems.append(f"{mod}: 算法包只能导入 algo 与 core.types，不能导入 {target}")
                    elif top not in ALGO_ALLOWED_IMPORTS:
                        problems.append(f"{mod}: 算法包不得导入 {target}（算法包没有 I/O）")
                for node in ast.walk(tree):
                    if isinstance(node, ast.Call):
                        if isinstance(node.func, ast.Name) and node.func.id in ALGO_BANNED_CALLS:
                            problems.append(f"{mod}: 算法包不得调用 {node.func.id}()")
                        if isinstance(node.func, ast.Attribute) and (
                                node.func.attr in ALGO_BANNED_ATTRS or node.func.attr.startswith("read_")):
                            problems.append(f"{mod}: 算法包不得调用 .{node.func.attr}()（I/O 或读时钟）")
    return problems


def collect_sources() -> dict[str, Source]:
    out: dict[str, Source] = {}
    for pkg in PACKAGES:
        root = WEBUI / pkg
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            rel = path.relative_to(WEBUI).with_suffix("")
            parts = list(rel.parts)
            is_package = parts[-1] == "__init__"
            if is_package:
                parts = parts[:-1]
            out[".".join(parts)] = Source(path.read_text(encoding="utf-8"), is_package)
    return out


# ---------------------------------------------------------------- 真实代码


def test_real_code_obeys_structure_rules():
    problems = analyze(collect_sources())
    assert problems == [], "\n".join(problems)


# ---------------------------------------------------------------- 检查器自检（防止检查器失效而静默通过）


def _check(**mods: str) -> list[str]:
    srcs = {name.replace("__", "."): Source(text, False) for name, text in mods.items()}
    return analyze(srcs)


def test_checker_flags_upward_import():
    assert _check(dmr_kernel__exchange__x="from dmr_kernel.core import coordinator\n",
                  dmr_kernel__core__coordinator="")


def test_checker_flags_same_layer_cross_group_import():
    assert _check(dmr_kernel__core__registry="from dmr_kernel.core import intents\n",
                  dmr_kernel__core__intents="")


def test_checker_allows_downward_and_intra_group_imports():
    assert _check(dmr_kernel__core__coordinator="from dmr_kernel.core import intents\nfrom dmr_kernel.exchange import adapter\n",
                  dmr_kernel__core__intents="", dmr_kernel__exchange__adapter="",
                  dmr_kernel__exchange__standin="from . import adapter\n") == []


def test_checker_flags_dmr_and_cross_package_imports():
    assert _check(dmr_kernel__ledger__x="import common.atomic_state\n")
    assert _check(dmr_console__x="from dmr_kernel.core import types\n")
    assert _check(dmr_standin__x="import dmr_kernel\n")


def test_checker_flags_library_outside_scope():
    assert _check(dmr_kernel__core__registry="import sqlalchemy\n")
    assert _check(dmr_kernel__marketdata__x="import ccxt\n")


def test_checker_flags_write_calls_and_capabilities_outside_coordinator():
    assert _check(dmr_kernel__ops__close="async def f(a):\n    await a.create_market_order(None)\n")
    assert _check(dmr_kernel__api__x="from dmr_kernel.exchange.adapter import WriteCapability\nc = WriteCapability('x')\n",
                  dmr_kernel__exchange__adapter="")
    assert _check(dmr_kernel__core__coordinator="async def f(a):\n    await a.create_market_order(None)\n") == []


def test_checker_flags_io_in_algo():
    assert _check(dmr_kernel__algo__x="import time\n")
    assert _check(dmr_kernel__algo__x="def f():\n    return open('x')\n")
    assert _check(dmr_kernel__algo__x="import pandas as pd\ndef f():\n    return pd.Timestamp.now()\n")
    assert _check(dmr_kernel__algo__x="import pandas as pd\ndef f(p):\n    return pd.read_csv(p)\n")
    assert _check(dmr_kernel__algo__x="import pandas as pd\nimport numpy as np\nfrom dmr_kernel.core import types\n",
                  dmr_kernel__core__types="") == []


def test_checker_flags_unregistered_kernel_module():
    assert _check(dmr_kernel__core__mystery="")
