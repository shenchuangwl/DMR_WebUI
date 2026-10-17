"""命令行客户端测试的夹具（帮手在 cli_kit.py）。"""
from __future__ import annotations

import pytest

from cli_kit import Rig


@pytest.fixture
def rig(tmp_path) -> Rig:
    return Rig(tmp_path)
