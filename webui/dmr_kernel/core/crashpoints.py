"""测试用崩溃点（第 0a 层）：在指定的中断点模拟「进程被杀」。只在非实盘环境可用。

两种用法：
- 进程内（集成测试）：``arm(name)`` 之后，代码走到 ``hit(name)`` 时抛 ``CrashSignal``。
  测试装配会把它当作进程被杀：直接丢弃整个内核实例、不走关停流程，然后用同一个账本和同一个
  替身交易所重新装配一个内核，检验重启恢复（F9）。
- 进程级（回环端到端测试）：环境变量 ``DMR_CRASHPOINT=<name>`` 启动内核，走到该点时
  ``os._exit(137)``，效果等同 SIGKILL：不刷缓冲、不关连接、不释放任何东西。

中断点名称对应计划报告 6.2 的 8 个重启中断点。
"""
from __future__ import annotations

import os
import sys

KNOWN_CRASHPOINTS = {
    "open_sent": "1 开仓单已发出，回执未到",
    "open_filled_before_stop": "2 已开仓，初始止损未挂上",
    "stop_cancelled_before_new": "3 已撤旧止损，新止损未建成",
    "partial_close_sent": "4 部分平仓单已发出，回执未到",
    "close_filled_before_cleanup": "5 平仓单已成交，保护单未清理",
    "close_all_midway": "6 一键全平做到一半",
    "takeover_half_registered": "7 接管登记写了一半",
    "switch_midway": "8 模型切换进行到 F6 的第 4 至 6 步之间",
}


class CrashSignal(BaseException):
    """模拟进程被杀。继承 BaseException，避免被业务代码里的 ``except Exception`` 吞掉。"""

    def __init__(self, name: str):
        super().__init__(name)
        self.name = name


class CrashRegistry:
    def __init__(self, *, live: bool, process_exit: bool = False):
        self._live = live
        self._process_exit = process_exit
        self._armed: set[str] = set()

    def arm(self, name: str) -> None:
        if self._live:
            raise RuntimeError("实盘环境下不能设置崩溃点")
        if name not in KNOWN_CRASHPOINTS:
            raise ValueError(f"未知的崩溃点：{name!r}")
        self._armed.add(name)

    def disarm(self, name: str) -> None:
        self._armed.discard(name)

    @property
    def armed(self) -> frozenset[str]:
        return frozenset(self._armed)

    @property
    def live(self) -> bool:
        """是否实盘环境（实盘下不能设置崩溃点，测试专用入口也不可用）。"""
        return self._live

    def hit(self, name: str) -> None:
        if name not in self._armed:
            return
        self._armed.discard(name)
        if self._process_exit:
            sys.stderr.write(f"crashpoint {name} hit: os._exit(137)\n")
            sys.stderr.flush()
            os._exit(137)
        raise CrashSignal(name)
