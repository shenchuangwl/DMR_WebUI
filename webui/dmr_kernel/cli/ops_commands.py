"""内核存活时的命令行操作（第 7 层 cli，T1.7 第一条路径）：``stop``、``close-all``。

只经控制接口，凭本机令牌（主体 ``cli``）：不占席位、不需要口令；每个改变状态的请求自带操作号（重复提交只执行一次）。

- ``stop``：``GET /state`` 取模型状态版本 -> ``POST /ops/stop``（不带「同时全部平仓」：要平仓用 ``close-all``）。
- ``close-all``：范围固定为「全部托管持仓」（``all_managed``）。``POST /ops/close_all/preview`` 列出会平与不平的仓，操作者确认
  （``--yes`` 跳过）后 ``POST /ops/close_all/confirm``，再轮询 ``GET /operations/{op_id}`` 逐仓打印结果，到终态为止。
  轮询超时只放弃等待、不重发：操作仍在内核里继续，可用 ``status`` 查看。
- 请求发出后没拿到完整的答复：不重发（结果不确定），提示用 ``status``/``inventory`` 核对。
"""
from __future__ import annotations

import secrets
from collections.abc import Mapping
from typing import Any

from dmr_kernel.cli.client import KernelError, KernelUnreachable
from dmr_kernel.cli.commands import (
    EXIT_FAILED,
    EXIT_OK,
    EXIT_UNAVAILABLE,
    CommandContext,
    _refusal_lines,
    _say,
    _unreachable_lines,
)

__all__ = ["POLL_INTERVAL_S", "POLL_TIMEOUT_S", "new_op_id", "run_close_all", "run_stop"]

POLL_INTERVAL_S = 1.0
POLL_TIMEOUT_S = 180.0  # 30 个仓 120 秒内给出结果（验收）；再留余量
TERMINAL = frozenset({"succeeded", "partial", "failed", "rejected", "aborted", "unknown"})
STATUS_LABEL = {"succeeded": "全部完成", "partial": "部分完成", "failed": "失败", "rejected": "被拒绝", "aborted": "已中止",
                "unknown": "结果未知", "running": "进行中", "accepted": "已受理"}
RESULT_LABEL = {"closed": "已平", "closed_externally": "已被外部平掉", "failed": "失败", "unknown": "结果未知", "residual": "有残余"}
YES = frozenset({"y", "yes", "是"})
UNCERTAIN = "请求已发出但没有拿到完整的答复，不确定内核是否已执行；没有重发。请用 status / inventory 核对后再决定。"


def new_op_id(kind: str) -> str:
    return f"cli-{kind}-{secrets.token_hex(8)}"  # 1–64 位 [A-Za-z0-9._:-]


def _m(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _fail_lines(ctx: CommandContext, exc: Exception, *, sent: bool) -> int:
    """sent=True：这是改变状态的请求，发出之后出了问题。"""
    if isinstance(exc, KernelUnreachable):
        if sent and exc.kind == "io":
            _say(ctx.err, [f"内核（{ctx.settings.kernel_host}:{ctx.settings.kernel_port}）没有给出完整的答复：{exc.detail}。", UNCERTAIN])
        else:
            _say(ctx.err, _unreachable_lines(ctx.settings, exc))
        return EXIT_UNAVAILABLE
    assert isinstance(exc, KernelError)
    _say(ctx.err, _refusal_lines(ctx.settings, exc))
    return EXIT_FAILED


def _model_version(ctx: CommandContext) -> int | None:
    state = ctx.client.get("/state")
    version = _m(state.get("model")).get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        _say(ctx.err, ["内核的状态里没有模型状态版本，无法安全地发出操作。请重试；仍不行请检查内核版本。"])
        return None
    return version


def _name(position: Mapping[str, Any]) -> str:
    side = {"LONG": "多", "SHORT": "空"}.get(str(position.get("side")), str(position.get("side", "?")))
    return f"{position.get('symbol', '?')} {side}"


# ---------------------------------------------------------------- stop


def run_stop(ctx: CommandContext) -> int:
    try:
        version = _model_version(ctx)
    except (KernelUnreachable, KernelError) as exc:
        return _fail_lines(ctx, exc, sent=False)
    if version is None:
        return EXIT_FAILED
    op_id = new_op_id("stop")
    try:
        reply = ctx.client.post("/ops/stop", {"op_id": op_id, "model_version": version})
    except (KernelUnreachable, KernelError) as exc:
        return _fail_lines(ctx, exc, sent=True)
    result = _m(_m(reply).get("result"))
    lines = [f"已停止：不再产生开仓意图（操作号 {op_id}）。"]
    cancelled, remaining = result.get("cancelled_open_orders"), result.get("managed_remaining")
    if cancelled is not None:
        lines.append(f"已撤销未成交的开仓单：{cancelled} 笔。")
    if remaining is not None:
        lines.append(f"仍在托管的持仓：{remaining} 个（止损照常更新）；要全部平掉请运行 close-all。")
    _say(ctx.out, lines)
    return EXIT_OK


# ---------------------------------------------------------------- close-all


def _per_position(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows = record.get("per_position")
    if not isinstance(rows, list):
        rows = _m(record.get("result")).get("per_position")
    return [row for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []


def _print_new_results(ctx: CommandContext, record: Mapping[str, Any], seen: set[tuple[Any, Any]]) -> None:
    for row in _per_position(record):
        key = (row.get("position_id"), row.get("result"))
        if key in seen:
            continue
        seen.add(key)
        label = RESULT_LABEL.get(str(row.get("result")), str(row.get("result")))
        detail = f"（{row['detail']}）" if row.get("detail") else ""
        print(f"  逐仓：{_name(row)} {label}{detail}", file=ctx.out)


def _show_preview(ctx: CommandContext, preview: Mapping[str, Any]) -> list[Any]:
    included = [p for p in (preview.get("included") or []) if isinstance(p, Mapping)]
    print(f"全部托管持仓平仓预览：将平 {len(included)} 个", file=ctx.out)
    mixed = {p.get("id") for p in (preview.get("mixed_included") or []) if isinstance(p, Mapping)}
    for p in included:
        note = "（混合仓）" if p.get("id") in mixed else ""
        print(f"  平：{_name(p)} 数量 {p.get('qty', '未知')}{note}", file=ctx.out)
    for item in preview.get("excluded") or []:
        item = _m(item)
        print(f"  不平：{_name(_m(item.get('position')))}（{item.get('reason', '未说明原因')}）", file=ctx.out)
    return included


def _confirmed(ctx: CommandContext, count: int) -> bool:
    try:
        answer = ctx.ask(f"确认市价平掉以上 {count} 个托管持仓？输入 y 确认，其他任何输入取消：")
    except EOFError:
        return False
    return answer.strip().lower() in YES


def run_close_all(ctx: CommandContext, *, yes: bool) -> int:
    try:
        version = _model_version(ctx)
        if version is None:
            return EXIT_FAILED
        preview = _m(ctx.client.post("/ops/close_all/preview", {"scope": "all_managed"}))
    except (KernelUnreachable, KernelError) as exc:
        return _fail_lines(ctx, exc, sent=False)
    included = _show_preview(ctx, preview)
    if not included:
        print("没有需要平掉的托管持仓。", file=ctx.out)
        return EXIT_OK
    if not yes and not _confirmed(ctx, len(included)):
        print("已取消：没有向内核发出平仓请求。", file=ctx.out)
        return EXIT_FAILED
    op_id = new_op_id("closeall")
    pv = preview.get("model_version")
    payload = {"op_id": op_id, "model_version": pv if isinstance(pv, int) and not isinstance(pv, bool) else version,
               "scope": "all_managed"}
    try:
        reply = _m(ctx.client.post("/ops/close_all/confirm", payload))
    except (KernelUnreachable, KernelError) as exc:
        return _fail_lines(ctx, exc, sent=True)
    print(f"已提交全部平仓（操作号 {op_id}），等待逐仓结果……", file=ctx.out)
    return _follow(ctx, op_id, reply)


def _follow(ctx: CommandContext, op_id: str, first: Mapping[str, Any]) -> int:
    seen: set[tuple[Any, Any]] = set()
    record: Mapping[str, Any] = first
    started = ctx.monotonic()
    while True:
        status = str(record.get("status"))
        if status != "running" and status in TERMINAL:
            _print_new_results(ctx, record, seen)
            return _finish(ctx, status, record)
        if status not in ("running", "accepted"):
            record = {}
        if ctx.monotonic() - started >= POLL_TIMEOUT_S:
            _say(ctx.err, [f"等了 {POLL_TIMEOUT_S:.0f} 秒，操作 {op_id} 仍在进行中。没有重发；内核会继续执行，"
                           "请用 status / inventory 查看进展。"])
            return EXIT_UNAVAILABLE
        ctx.sleep(POLL_INTERVAL_S)
        try:
            record = _m(ctx.client.get(f"/operations/{op_id}"))
        except KernelUnreachable:
            record = {"status": "running"}  # 轮询是只读的：丢了一次答复，接着问
            continue
        except KernelError as exc:
            return _fail_lines(ctx, exc, sent=False)
        _print_new_results(ctx, record, seen)


def _finish(ctx: CommandContext, status: str, record: Mapping[str, Any]) -> int:
    rows = _per_position(record)
    bad = [r for r in rows if r.get("result") != "closed" and r.get("result") != "closed_externally"]
    summary = f"全部平仓结束：{STATUS_LABEL.get(status, status)}（共 {len(rows)} 个持仓，其中 {len(bad)} 个未确认平掉）。"
    unknown_orders = _m(record.get("result")).get("unknown_open_orders")
    stream = ctx.out if status == "succeeded" else ctx.err
    lines = [summary]
    if unknown_orders:
        lines.append(f"另有 {len(unknown_orders)} 笔订单状态未知，请用 inventory 核对。")
    _say(stream, lines)
    return EXIT_OK if status == "succeeded" else EXIT_FAILED

