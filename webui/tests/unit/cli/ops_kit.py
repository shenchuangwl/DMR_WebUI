"""T1.7/T1.9 命令行测试的帮手：把可注入的传输、提问、睡眠、单调时钟都接到 main 上（cli_kit.Rig 只接了传输）。"""
from __future__ import annotations

from datetime import timezone
from io import StringIO

from cli_kit import Result, Rig
from dmr_kernel.cli.main import main


class Clockwork:
    """假的睡眠与单调时钟：睡多久时钟就走多久，测试不真的等。"""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


def run_ops(rig: Rig, argv: list[str], fake, *, answers: list[str] | None = None, clock: Clockwork | None = None,
            with_token: bool = True) -> tuple[Result, list[str], Clockwork]:
    """返回（结果, 被问过的问题, 假时钟）。answers 用完后再问就当作输入结束（EOFError）。"""
    if with_token and not rig.token_path.exists():
        rig.write_token()
    clock = clock or Clockwork()
    asked: list[str] = []
    pending = list(answers or [])

    def ask(question: str) -> str:
        asked.append(question)
        if not pending:
            raise EOFError
        return pending.pop(0)

    out, err = StringIO(), StringIO()
    code = main(argv, env=dict(rig.env), transport_factory=lambda settings: fake, stdout=out, stderr=err,
                tz=timezone.utc, ask=ask, sleep=clock.sleep, monotonic=clock.monotonic)
    return Result(code, out.getvalue(), err.getvalue()), asked, clock
