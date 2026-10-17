"""统一操作的弹窗：启动、停止、部分平仓（含选仓）、平掉此仓、一键全平、模型切换。

所有逻辑（报文、版本、操作号）在 ``dmr_console.operations`` 与 ``Actions``；这里只管摆元素、收输入、把结果写成文字。
弹窗里的按钮登记在外壳的「失联即禁用」名单里；弹窗关闭时元素与名单一起清掉。口令框的值只在点击时读一次，不存、不记。
内核给的文字一律按纯文本渲染。
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from nicegui import ui

from dmr_console import labels as L
from dmr_console import operations as O
from dmr_console.actions import OpOutcome
from dmr_console.pages.shell import Shell

log = logging.getLogger(__name__)

NO_STATE = "尚未取得内核状态，请稍后再试"
INTERNAL = "界面内部错误，请查看控制台日志"
RESULT_COLUMNS = [
    {"name": "symbol", "label": "合约", "field": "symbol", "align": "left"},
    {"name": "side", "label": "方向", "field": "side", "align": "left"},
    {"name": "result", "label": "结果", "field": "result", "align": "left"},
    {"name": "detail", "label": "说明", "field": "detail", "align": "left"},
]
DONE_STATUSES = frozenset({"succeeded", "failed", "aborted", "cancelled", "completed"})


class _Dialog:
    """一个弹窗的壳：卡片、按钮登记、忙碌标志、关闭清理。"""

    def __init__(self, owner: "OpDialogs", name: str, title: str, *, on_close: Callable[[], Awaitable[None] | None] | None = None) -> None:
        self.owner = owner
        self.shell = owner.shell
        self.busy = False
        self._buttons: list[ui.button] = []
        self._on_close = on_close
        self._closed = False
        with self.shell.body:
            self.dialog = ui.dialog().props("persistent")
        with self.dialog:
            self.card = ui.card().classes("dmr-dialog").mark(f"dlg-{name}")
        with self.card:
            ui.label(title).classes("dmr-h2")

    def button(self, text: str, handler: Callable[[], Awaitable[None]], marker: str, *,
               ready: Callable[[], bool] = lambda: True, guard: bool = True) -> ui.button:
        """``guard=False``：只读的预览按钮，可以连点换范围（以最后一次点击的结果为准），不占「忙碌」。"""
        async def run() -> None:
            if (guard and self.busy) or self._closed:
                return
            if guard:
                self.busy = True
            try:
                await handler()
            except Exception:  # noqa: BLE001 - 一个弹窗出错不能拖垮页面
                log.exception("弹窗操作出错")
            finally:
                if guard:
                    self.busy = False

        button = self.shell.utility_button(text, run, marker, extra_enabled=lambda: not self.busy and ready())
        button.enabled = ready() and not self.shell.controller.link.status.lost  # 第一次刷新之前也不能多点
        self._buttons.append(button)
        return button

    def close_button(self, text: str = "关闭", marker: str = "dlg-close") -> ui.button:
        return ui.button(text, on_click=self.close).props("flat no-caps").mark(marker)

    def open(self) -> None:
        self.dialog.open()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.shell.forget_buttons(self._buttons)
        self.dialog.close()
        self.dialog.delete()
        if self._on_close is not None:
            result = self._on_close()
            if result is not None:
                import asyncio
                asyncio.ensure_future(result)


def _lines(items: list[str], empty: str = "（无）") -> str:
    return "\n".join(items) if items else empty


class OpDialogs:
    def __init__(self, shell: Shell) -> None:
        self.shell = shell
        self.controller = shell.controller
        self.actions = shell.controller.actions

    # ------------------------------------------------------------ 共用

    def _state(self) -> Any:
        return self.controller.state

    def _model_version(self) -> int | None:
        return O.model_version(self._state())

    def _is_live(self) -> bool:
        state = self._state()
        return isinstance(state, dict) and state.get("env") == "mainnet_live"

    @staticmethod
    def _message(marker: str) -> ui.label:
        return ui.label("").classes("dmr-reason dmr-msg").mark(marker)

    @staticmethod
    def _fail(label: ui.label, outcome: OpOutcome) -> None:
        label.set_text(outcome.text)

    def _meaning(self, key: str) -> str:
        state = self._state()
        meanings = state.get("button_meanings") if isinstance(state, dict) else None
        text = meanings.get(key) if isinstance(meanings, dict) else None
        return text if isinstance(text, str) else ""

    # ------------------------------------------------------------ 启动

    def open_start(self) -> None:
        version = self._model_version()
        dlg = _Dialog(self, "start", L.OPERATION_LABELS["start"])
        with dlg.card:
            ui.label(self._meaning("start") or "打开开仓闸").classes("dmr-hint")
            ui.label("启动前内核会逐项自检，全部通过才开闸。").classes("dmr-hint")
            password = ui.input("口令（实盘启动需要）", password=True).props("autocomplete=off").mark("start-password")
            checks = ui.label("").classes("dmr-checks").style("white-space: pre-line").mark("start-checks")
            message = self._message("start-message")

            async def confirm() -> None:
                if version is None:
                    message.set_text(NO_STATE)
                    return
                outcome = await self.actions.call("start", "/ops/start", lambda op: O.start_payload(op, version, password=password.value))
                rows = O.check_rows(outcome.result if outcome.ok else outcome.extra)
                checks.set_text("\n".join(f"{mark} · {name}" + (f"：{detail}" if detail else "") for mark, name, detail in rows))
                message.classes(add="dmr-refusal" if not outcome.ok else "", remove="" if not outcome.ok else "dmr-refusal")
                if outcome.ok:
                    password.set_value("")
                    message.set_text("已启动：开仓闸已打开" if outcome.result.get("gate") in (None, "running") else
                                     f"启动请求已受理：{outcome.result.get('gate')}")
                else:
                    message.set_text(outcome.text)

            with ui.row():
                dlg.button("确认启动", confirm, "start-confirm")
                dlg.close_button()
        dlg.open()

    # ------------------------------------------------------------ 停止

    def open_stop(self) -> None:
        version = self._model_version()
        dlg = _Dialog(self, "stop", L.OPERATION_LABELS["stop"])
        with dlg.card:
            ui.label(self._meaning("stop") or "关上开仓闸，撤掉未成交的开仓单").classes("dmr-hint")
            close_all = ui.checkbox("同时全部平仓（需要口令）").mark("stop-close-all")
            password = ui.input("口令", password=True).props("autocomplete=off").mark("stop-password")
            password.bind_visibility_from(close_all, "value")
            message = self._message("stop-message")

            async def confirm() -> None:
                if version is None:
                    message.set_text(NO_STATE)
                    return
                wants_all = bool(close_all.value)
                outcome = await self.actions.call(
                    "stop", "/ops/stop",
                    lambda op: O.stop_payload(op, version, close_all=wants_all, password=password.value if wants_all else None))
                if not outcome.ok:
                    message.set_text(outcome.text)
                    return
                text = "已停止：开仓闸已关闭"
                if outcome.result.get("close_all_op_id"):
                    text += f"；已另起一键全部平仓（操作号 {outcome.result['close_all_op_id']}）"
                message.set_text(text)

            with ui.row():
                dlg.button("确认停止", confirm, "stop-confirm")
                dlg.close_button()
        dlg.open()

    # ------------------------------------------------------------ 部分平仓

    def open_pick_partial(self) -> None:
        dlg = _Dialog(self, "pick-partial", "部分平仓：选择持仓")
        positions = [p for p in (self.controller.positions or []) if isinstance(p, dict) and p.get("status") == "managed"]
        with dlg.card:
            ui.label(self._meaning("partial_close") or "对任何一个托管持仓部分平仓").classes("dmr-hint")
            if not positions:
                ui.label("没有托管持仓").classes("dmr-empty").mark("pick-partial-empty")
            for pos in positions:
                def pick(pos=pos) -> None:
                    dlg.close()
                    self.open_partial(pos)

                ui.button(self._position_line(pos), on_click=pick).props("flat no-caps").mark(f"pick-partial-{pos.get('id')}")
            dlg.close_button()
        dlg.open()

    @staticmethod
    def _position_line(pos: dict) -> str:
        return f"{pos.get('symbol', '')} {L.label_of(L.SIDE_LABELS, pos.get('side'))} · 数量 {pos.get('qty', '')}"

    def open_partial(self, pos: dict) -> None:
        pid = pos.get("id")
        slot = f"partial_close:{pid}"
        dlg = _Dialog(self, "partial", f"部分平仓 · {self._position_line(pos)}")
        preview: dict[str, Any] = {"value": None}
        with dlg.card:
            ui.label("选择比例，先预览实际平仓数量，再确认。").classes("dmr-hint")
            summary = ui.label("").classes("dmr-preview").style("white-space: pre-line").mark("partial-preview")
            message = self._message("partial-message")

            async def run_preview(**kwargs: Any) -> None:
                self.actions.ids.settle(slot)  # 换了参数：之前未决的操作号不能再复用
                preview["value"] = None
                preview["seq"] = seq = preview.get("seq", 0) + 1
                outcome = await self.actions.call(f"{slot}:preview", "/ops/partial_close/preview",
                                                  lambda _: O.partial_preview_payload(pid, **kwargs), with_op_id=False)
                if preview["seq"] != seq:
                    return
                if not outcome.ok:
                    summary.set_text("")
                    message.set_text(outcome.text)
                    return
                preview["value"] = outcome.result
                r = outcome.result
                lines = [f"实际平仓数量：{r.get('close_qty')}（持仓 {r.get('live_qty')}）",
                         f"预计剩余数量：{r.get('remaining_qty')}，剩余名义金额 {r.get('remaining_notional')}",
                         f"标记价 {r.get('mark_price')}，最小名义金额 {r.get('min_notional')}"]
                lines += [str(w) for w in (r.get("warnings") or [])]
                summary.set_text("\n".join(lines))
                message.set_text("")

            with ui.row():
                for ratio in O.PARTIAL_RATIOS:
                    percent = int(ratio * 100)
                    dlg.button(f"{percent}%", lambda ratio=ratio: run_preview(ratio=ratio), f"partial-ratio-{percent}", guard=False)
            with ui.row().classes("items-center"):
                custom = ui.input("自定义百分比（0–100）").props("dense").mark("partial-custom")

                async def go_custom() -> None:
                    ratio = O.parse_custom_percent(custom.value)
                    if ratio is None:
                        message.set_text("百分比必须大于 0 小于 100（例如 30）")
                        return
                    await run_preview(ratio=ratio)

                dlg.button("预览", go_custom, "partial-custom-go")

            async def confirm() -> None:
                current = preview["value"]
                if current is None:
                    message.set_text("请先预览")
                    return
                outcome = await self.actions.call(slot, "/ops/partial_close/confirm", lambda op: O.partial_confirm_payload(op, current))
                if outcome.ok:
                    preview["value"] = None
                    r = outcome.result
                    message.set_text(f"已提交部分平仓：{r.get('outcome') or r.get('state') or ''}".rstrip("：") + "")
                    return
                if outcome.code in ("stale_version", "not_allowed"):
                    preview["value"] = None  # 作废，必须重新预览
                message.set_text(outcome.text)

            with ui.row():
                dlg.button("确认部分平仓", confirm, "partial-confirm", ready=lambda: preview["value"] is not None)
                dlg.close_button()
        dlg.open()

    # ------------------------------------------------------------ 平掉此仓

    def open_close_one(self, pos: dict) -> None:
        pid = pos.get("id")
        version = O.position_version(pos)
        dlg = _Dialog(self, "close-one", f"平掉此仓 · {self._position_line(pos)}")
        with dlg.card:
            ui.label("按市价平掉这个持仓的全部数量；只需确认一次，不需要口令。").classes("dmr-hint")
            message = self._message("closeone-message")

            async def confirm() -> None:
                outcome = await self.actions.call(f"close_one:{pid}", "/ops/close_one", lambda op: O.close_one_payload(op, pid, version))
                if outcome.ok:
                    message.set_text(f"已提交平仓：{outcome.result.get('outcome') or outcome.result.get('state') or ''}")
                else:
                    message.set_text(outcome.text)

            with ui.row():
                dlg.button("确认平掉此仓", confirm, "closeone-confirm")
                dlg.close_button()
        dlg.open()

    # ------------------------------------------------------------ 接管类（接管、忽略、恢复托管、取消托管、交给系统管理止损）

    TAKEOVER_DIALOGS = {
        "takeover": ("takeover", "按接管规则登记这个仓并挂上初始止损；只在模型 2 生效时可用。"),
        "ignore": ("ignore", "标记为外部（已忽略）：系统只读，不会动它，有效到它归零。"),
        "restore": ("restore", "按成交记录找回原来的归属；查不到就登记为手动接管。"),
        "hand_stop_to_system": ("hand-stop", "先挂系统止损，再撤掉你自己挂的止损；之后由系统管理这个仓的止损。"),
    }

    @staticmethod
    def _result_line(prefix: str, result: dict) -> str:
        parts = [prefix]
        for label, table, field in (("状态", L.POSITION_STATUS_LABELS, "status"), ("归属", L.OWNERSHIP_LABELS, "ownership"),
                                    ("保护", L.PROTECTION_LABELS, "protection_state")):
            if result.get(field) is not None:
                parts.append(f"{label} {L.label_of(table, result.get(field))}")
        return "；".join(parts)

    def open_position_op(self, key: str, pos: dict) -> None:
        name, hint = self.TAKEOVER_DIALOGS[key]
        pid, version = pos.get("id"), O.position_version(pos)
        dlg = _Dialog(self, name, f"{L.OPERATION_LABELS[key]} · {self._position_line(pos)}")
        with dlg.card:
            ui.label(hint).classes("dmr-hint")
            message = self._message(f"{name}-message")

            async def confirm() -> None:
                outcome = await self.actions.call(f"{key}:{pid}", O.TAKEOVER_PATHS[key],
                                                  lambda op: O.position_op_payload(op, pid, version))
                if outcome.ok:
                    message.set_text(self._result_line(f"已完成：{L.OPERATION_LABELS[key]}", outcome.result))
                else:
                    message.set_text(outcome.text)

            with ui.row():
                dlg.button(f"确认{L.OPERATION_LABELS[key]}", confirm, f"{name}-confirm")
                dlg.close_button()
        dlg.open()

    def open_cancel_takeover(self, pos: dict) -> None:
        pid, version = pos.get("id"), O.position_version(pos)
        dlg = _Dialog(self, "cancel-takeover", f"取消托管 · {self._position_line(pos)}")
        with dlg.card:
            ui.label("二选一：保留止损并移交，或撤掉系统止损。两种方式之后这个仓都变成外部仓（已忽略）。").classes("dmr-hint")
            mode = ui.radio(O.CANCEL_MODE_LABELS, value="handover").mark("cancel-takeover-mode")
            message = self._message("cancel-takeover-message")

            async def confirm() -> None:
                chosen = mode.value
                outcome = await self.actions.call(f"cancel_takeover:{pid}", O.TAKEOVER_PATHS["cancel_takeover"],
                                                  lambda op: O.cancel_takeover_payload(op, pid, version, chosen))
                if outcome.ok:
                    message.set_text(self._result_line("已取消托管", outcome.result))
                else:
                    message.set_text(outcome.text)

            with ui.row():
                dlg.button("确认取消托管", confirm, "cancel-takeover-confirm")
                dlg.close_button()
        dlg.open()

    # ------------------------------------------------------------ 一键全部平仓

    def open_close_all(self) -> None:
        dlg = _Dialog(self, "close-all", L.OPERATION_LABELS["close_all"])
        view: dict[str, Any] = {"preview": None, "scope": None, "op_id": None, "running": False, "timer": None}
        with dlg.card:
            ui.label(self._meaning("close_all") or "默认平策略仓").classes("dmr-hint")
            ui.label("先选范围并预览，再输入口令确认。外部持仓与待确认持仓永不包含。").classes("dmr-hint")
            with ui.row():
                for scope in O.CLOSE_ALL_SCOPES:
                    dlg.button(O.SCOPE_LABELS[scope], lambda scope=scope: preview(scope), f"ca-scope-{scope}", guard=False)
            included = ui.label("").style("white-space: pre-line").mark("ca-included")
            excluded = ui.label("").classes("dmr-hint").style("white-space: pre-line").mark("ca-excluded")
            mixed = ui.label("").classes("dmr-warn-text").style("white-space: pre-line").mark("ca-mixed")
            password = ui.input("口令", password=True).props("autocomplete=off").mark("ca-password")
            progress = ui.label("").mark("ca-progress")
            table = ui.table(columns=RESULT_COLUMNS, rows=[], row_key="symbol").props("dense flat bordered").classes("w-full").mark("ca-results")
            message = self._message("ca-message")

            async def preview(scope: str) -> None:
                view["seq"] = seq = view.get("seq", 0) + 1
                outcome = await self.actions.call("close_all.preview", "/ops/close_all/preview",
                                                  lambda _: O.close_all_preview_payload(scope), with_op_id=False)
                if view["seq"] != seq:
                    return  # 已经点了别的范围：以最后一次为准
                if not outcome.ok:
                    view["preview"] = None
                    message.set_text(outcome.text)
                    return
                data = O.close_all_view(outcome.result)
                view.update(preview=data, scope=scope)
                included.set_text(f"将平掉（{len(data.included)}）：\n" + _lines(data.included))
                excluded.set_text(f"不在范围内（{len(data.excluded)}）：\n" + _lines(data.excluded))
                mixed.set_text(("其中混合仓（单独标出）：\n" + _lines(data.mixed)) if data.mixed else "")
                message.set_text("")

            def show_rows(items: Any) -> None:
                rows = O.per_position_rows(items)
                for i, row in enumerate(rows):
                    row["key"] = f"{row['symbol']}-{row['side']}-{i}"
                table.rows = rows
                table.row_key = "key"
                table.update()

            async def poll() -> None:
                if not view["running"] or view["op_id"] is None:
                    return
                outcome = await self.actions.operation(view["op_id"])
                if not outcome.ok:
                    progress.set_text(f"查询进度失败：{outcome.text}")
                    return
                items = outcome.result.get("per_position")
                show_rows(items)
                total = len(view["preview"].included) if view["preview"] else 0
                done = len(items) if isinstance(items, list) else 0
                if outcome.status in DONE_STATUSES:
                    finish(f"完成：{outcome.status}，逐仓结果共 {done} 条")
                else:
                    progress.set_text(f"进行中：已有结果 {done} / {total}")

            def finish(text: str) -> None:
                view["running"] = False
                if view["timer"] is not None:
                    view["timer"].cancel()
                progress.set_text(text)

            async def confirm() -> None:
                data = view["preview"]
                if data is None:
                    message.set_text("请先选择范围并预览")
                    return
                version = data.model_version if data.model_version is not None else self._model_version()
                outcome = await self.actions.call("close_all", "/ops/close_all/confirm",
                                                  lambda op: O.close_all_confirm_payload(op, version, view["scope"], password.value))
                if not outcome.ok:
                    message.set_text(outcome.text)
                    return
                password.set_value("")
                message.set_text("")
                view["op_id"] = outcome.op_id
                items = outcome.result.get("per_position")
                if outcome.result.get("status") == "running" or outcome.status == "running":
                    view["running"] = True
                    progress.set_text("进行中：已提交，等待逐仓结果")
                    view["timer"] = ui.timer(self.shell.console.config.tick_s, poll)
                else:
                    show_rows(items)
                    finish(f"完成：{outcome.status or outcome.result.get('status', '')}")

            async def abort() -> None:
                if view["op_id"] is None:
                    return
                outcome = await self.actions.call("abort", "/ops/abort", lambda op: O.abort_payload(op, view["op_id"]))
                message.set_text("已请求中止：不再发新单，操作锁释放，模型回到已停止" if outcome.ok else outcome.text)

            with ui.row():
                dlg.button("确认全部平仓", confirm, "ca-confirm",
                           ready=lambda: view["preview"] is not None and bool(password.value) and not view["running"])
                dlg.button("中止", abort, "ca-abort", ready=lambda: bool(view["running"]))
                dlg.close_button()
        dlg._on_close = lambda: finish_on_close(view)
        dlg.open()

    # ------------------------------------------------------------ 模型切换

    def open_switch(self) -> None:
        state = {"preview": None, "mode": None, "target": None, "running": False, "op_id": None, "timer": None, "done": False}

        async def cancel_preview() -> None:
            if state["preview"] is not None and not state["done"]:
                state["preview"] = None
                await self.actions.call("switch.cancel", "/ops/switch/cancel", O.switch_cancel_payload)

        dlg = _Dialog(self, "switch", L.OPERATION_LABELS["switch"], on_close=lambda: (finish_on_close(state), cancel_preview())[1])
        with dlg.card:
            ui.label("选择目标模型 → 预览 → 选择处理方式并输入口令 → 确认。预览期间会暂停开仓信号。").classes("dmr-hint")
            with ui.row():
                for target in O.SWITCH_TARGETS:
                    dlg.button(L.MODEL_LABELS.get(target, target), lambda t=target: preview(t), f"sw-target-{target}")
            stage = ui.column().classes("w-full gap-2")
            message = self._message("sw-message")
            dlg.close_button()

        async def preview(target: str) -> None:
            outcome = await self.actions.call("switch.preview", "/ops/switch/preview", lambda op: O.switch_preview_payload(op, target))
            if not outcome.ok:
                message.set_text(outcome.text)
                return
            data = O.switch_view(outcome.result, target=target)
            state.update(preview=data, target=target, mode=None)
            message.set_text("")
            build_stage(data)

        def build_stage(data: O.SwitchView) -> None:
            stage.clear()
            with stage:
                lines = [f"目标：{L.MODEL_LABELS.get(data.target, data.target)}",
                         f"各归属持仓数：{data.counts_text}", f"未完成订单：{data.open_orders} 个", *data.notes]
                ui.label("\n".join(lines)).style("white-space: pre-line").mark("sw-summary")
                with ui.row():
                    for mode in O.SWITCH_MODES:
                        dlg.button(O.MODE_LABELS[mode], lambda m=mode: choose(m), f"sw-mode-{mode}")
                chosen = ui.label("请选择处理方式").classes("dmr-hint").mark("sw-mode-label")
                state["chosen_label"] = chosen
                password = ui.input("口令", password=True).props("autocomplete=off").mark("sw-password")
                state["password"] = password
                progress = ui.label("").mark("sw-progress")
                state["progress"] = progress
                with ui.row():
                    dlg.button("确认切换", confirm, "sw-confirm",
                               ready=lambda: state["mode"] is not None and bool(password.value) and not state["running"] and not state["done"])
                    dlg.button("取消切换", cancel, "sw-cancel", ready=lambda: not state["running"])

        async def choose(mode: str) -> None:
            state["mode"] = mode
            state["chosen_label"].set_text(f"已选择：{O.MODE_LABELS[mode]}")

        async def cancel() -> None:
            await cancel_preview()
            dlg.close()

        def describe(result: dict) -> str:
            if result.get("status") == "failed":
                active = result.get("active_model")
                return f"切换失败，留在旧模型{('（' + L.label_of(L.MODEL_LABELS, active) + '）') if active else ''}：{result.get('reason', '')}"
            active = result.get("active_model")
            return f"已切换到 {L.label_of(L.MODEL_LABELS, active)}；开仓闸为已停止，需要时请重新启动"

        async def poll() -> None:
            if not state["running"] or state["op_id"] is None:
                return
            outcome = await self.actions.operation(state["op_id"])
            if outcome.ok and outcome.status in DONE_STATUSES:
                state["running"] = False
                state["done"] = True
                if state["timer"] is not None:
                    state["timer"].cancel()
                message.set_text(describe(outcome.result.get("result") if isinstance(outcome.result.get("result"), dict) else outcome.result))
            elif not outcome.ok:
                state["progress"].set_text(f"查询进度失败：{outcome.text}")

        async def confirm() -> None:
            data = state["preview"]
            if data is None or state["mode"] is None:
                return
            version = data.model_version if data.model_version is not None else self._model_version()
            outcome = await self.actions.call("switch.confirm", "/ops/switch/confirm",
                                              lambda op: O.switch_confirm_payload(op, version, state["mode"], state["password"].value))
            if not outcome.ok:
                message.set_text(outcome.text)
                return
            state["password"].set_value("")
            if outcome.result.get("status") == "running":
                state.update(running=True, op_id=outcome.op_id)
                state["progress"].set_text("切换进行中：先全部平仓，再停旧模型")
                state["timer"] = ui.timer(self.shell.console.config.tick_s, poll)
            else:
                state["done"] = True
                message.set_text(describe(outcome.result))

        dlg.open()


def finish_on_close(view: dict) -> None:
    """弹窗关闭时停掉进度定时器。"""
    timer = view.get("timer")
    if timer is not None:
        timer.cancel()
        view["timer"] = None
    view["running"] = False
