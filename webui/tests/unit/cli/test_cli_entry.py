"""命令行的入口与结构：``python -m dmr_kernel.cli``；命令行只经控制接口说话，不打开数据库，也不依赖任何第三方库。"""
from __future__ import annotations

import ast
import runpy
import sys
from pathlib import Path

import pytest

import dmr_kernel.cli as cli_package

CLI_DIR = Path(cli_package.__file__).resolve().parent
ALLOWED_KERNEL_MODULES = {"dmr_kernel.settings"}  # 命令行只需要知道内核地址与 var 目录在哪


def cli_modules() -> list[Path]:
    return sorted(CLI_DIR.glob("*.py"))


def imported_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:  # 相对导入：同包内
                names.add("dmr_kernel.cli" + (f".{base}" if base else ""))
            else:
                names.add(base)
                names.update(f"{base}.{alias.name}" for alias in node.names)
    return names


def test_the_expected_modules_exist():
    assert {"__init__", "__main__", "client", "commands", "main", "render"} <= {p.stem for p in cli_modules()}


@pytest.mark.parametrize("path", cli_modules(), ids=lambda p: p.name)
def test_the_command_line_imports_only_the_standard_library_and_the_settings(path):
    for name in imported_names(path):
        top = name.split(".")[0]
        if top == "dmr_kernel":
            allowed = any(name == mod or name.startswith(mod + ".") for mod in ALLOWED_KERNEL_MODULES)
            assert allowed or name == "dmr_kernel" or name.startswith("dmr_kernel.cli"), (
                f"{path.name} 导入了 {name}：命令行只经控制接口说话，不碰账本、适配层或内核内部")
        else:
            assert top in sys.stdlib_module_names or top == "__future__", f"{path.name} 导入了第三方库 {name}"


@pytest.mark.parametrize("path", cli_modules(), ids=lambda p: p.name)
def test_the_command_line_never_opens_the_database(path):
    source = path.read_text(encoding="utf-8")
    for forbidden in ("sqlite3", "sqlalchemy", ".sqlite", "ledger"):
        assert forbidden not in source, f"{path.name} 提到了 {forbidden}"


def test_python_dash_m_runs_the_command_line(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["python -m dmr_kernel.cli", "--help"])
    with pytest.raises(SystemExit) as stopped:
        runpy.run_module("dmr_kernel.cli", run_name="__main__")
    assert stopped.value.code == 0
    shown = capsys.readouterr().out
    assert "status" in shown and "create-operator" in shown
