"""操作执行器（控制器上的 actions）：op_id 复用、失联时不换号、错误翻译、成功后刷新状态、席位调用、不泄露口令。"""
from __future__ import annotations

import logging

import pytest
from console_kit import GOOD_TOKEN, FakeKernelClient, ManualClock, lost, make_state

from dmr_console import operations as O
from dmr_console.client import KernelError
from dmr_console.live import LiveController
from dmr_console.session import ConsoleSession


def make_controller(api=None):
    api = api or FakeKernelClient()
    clock = ManualClock()
    session = ConsoleSession(browser_id="b1", token=GOOD_TOKEN, operator="admin", session_id="s1", idle_timeout_min=30.0,
                             opened_ms=clock.now_ms(), last_seen_ms=clock.now_ms())
    return LiveController(api, session, clock, operations_enabled=True), api


async def test_a_successful_call_returns_the_result_and_refreshes_the_state():
    controller, api = make_controller()
    api.set("POST /ops/start", {"op_id": "x", "status": "succeeded", "result": {"gate": "running"}})
    outcome = await controller.actions.call("start", "/ops/start", lambda op: O.start_payload(op, 12))
    assert outcome.ok and outcome.status == "succeeded" and outcome.result == {"gate": "running"}
    assert api.posts_to("/ops/start")[0]["model_version"] == 12
    assert api.calls_of("state")  # 成功后立刻重新拉状态，按钮据此更新


async def test_a_kernel_refusal_is_reported_with_code_and_extra_and_the_next_click_gets_a_fresh_op_id():
    controller, api = make_controller()
    api.set("POST /ops/start", KernelError(409, "not_allowed", "自检不过", {"checks": [{"name": "对账", "ok": False}]}))
    first = await controller.actions.call("start", "/ops/start", lambda op: O.start_payload(op, 1))
    assert not first.ok and first.code == "not_allowed" and first.message == "自检不过" and not first.unreachable
    assert first.extra["checks"][0]["name"] == "对账"
    await controller.actions.call("start", "/ops/start", lambda op: O.start_payload(op, 1))
    ids = [b["op_id"] for b in api.posts_to("/ops/start")]
    assert ids[0] != ids[1]


async def test_a_retry_after_losing_the_kernel_reuses_the_same_op_id():
    controller, api = make_controller()
    api.set("POST /ops/stop", lost("timeout"))
    first = await controller.actions.call("stop", "/ops/stop", lambda op: O.stop_payload(op, 1))
    assert not first.ok and first.unreachable and "超时" in first.message
    api.heal()
    second = await controller.actions.call("stop", "/ops/stop", lambda op: O.stop_payload(op, 1))
    assert second.ok
    ids = [b["op_id"] for b in api.posts_to("/ops/stop")]
    assert ids[0] == ids[1]
    third = await controller.actions.call("stop", "/ops/stop", lambda op: O.stop_payload(op, 1))
    assert api.posts_to("/ops/stop")[2]["op_id"] != ids[0] and third.ok  # 已成功，下一次点击换新号


async def test_calls_without_an_op_id_do_not_consume_one():
    controller, api = make_controller()
    outcome = await controller.actions.call("close_all.preview", "/ops/close_all/preview",
                                            lambda _: O.close_all_preview_payload("model"), with_op_id=False)
    assert outcome.ok and api.posts_to("/ops/close_all/preview") == [{"scope": "model"}]


async def test_an_expired_session_marks_the_controller_invalid():
    controller, api = make_controller()
    api.set("POST /ops/start", KernelError(401, "unauthorized", "会话无效"))
    outcome = await controller.actions.call("start", "/ops/start", lambda op: O.start_payload(op, 1))
    assert outcome.code == "unauthorized" and controller.session_invalid


async def test_seat_calls_use_the_seat_endpoints_and_only_force_carries_a_password():
    controller, api = make_controller()
    assert (await controller.actions.seat("acquire")).ok
    assert (await controller.actions.seat("force", password="pw")).ok
    assert [p for p, _ in api.posts] == ["/seat/acquire", "/seat/force"]
    assert api.posts[0][1] is None and api.posts[1][1] == {"password": "pw"}
    with pytest.raises(ValueError):
        await controller.actions.seat("steal")


async def test_a_seat_taken_refusal_carries_the_holder():
    controller, api = make_controller()
    api.set("POST /seat/acquire", KernelError(409, "seat_taken", "席位被占", {"holder_operator": "bob"}))
    outcome = await controller.actions.seat("acquire")
    assert outcome.code == "seat_taken" and outcome.extra["holder_operator"] == "bob"


async def test_each_call_leaves_one_log_line_without_the_password(caplog):
    controller, api = make_controller()
    with caplog.at_level(logging.INFO, logger="dmr_console.actions"):
        await controller.actions.call("close_all", "/ops/close_all/confirm",
                                      lambda op: O.close_all_confirm_payload(op, 1, "model", "SECRET-PW"))
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "/ops/close_all/confirm" in text and "SECRET-PW" not in text
    assert len(controller.actions.history) == 1 and "SECRET-PW" not in controller.actions.history[0]


async def test_operation_progress_is_read_from_the_operations_endpoint():
    controller, api = make_controller()
    api.set("GET /operations", {"op_id": "a", "status": "running", "per_position": [{"position_id": 1}]})
    outcome = await controller.actions.operation("a")
    assert outcome.ok and outcome.result["per_position"] == [{"position_id": 1}] and outcome.status == "running"
    api.set("GET /operations", KernelError(404, "not_found", "没有这个操作"))
    assert (await controller.actions.operation("a")).code == "not_found"


async def test_the_seat_is_renewed_at_half_the_lease_and_not_before():
    controller, api = make_controller()
    now = controller.actions._clock.now_ms()
    controller.state = mine()
    controller.state["seat"]["lease_until_ms"] = now + 60_000
    assert await controller.actions.renew_if_due() is None  # 刚看到持有：从现在起算
    controller.actions._clock.advance(29)
    assert await controller.actions.renew_if_due() is None
    assert api.posts_to("/seat/renew") == [] or api.posts == []
    controller.actions._clock.advance(2)
    outcome = await controller.actions.renew_if_due()
    assert outcome is not None and outcome.ok
    assert [p for p, _ in api.posts] == ["/seat/renew"]
    assert await controller.actions.renew_if_due() is None  # 刚续过


async def test_nothing_is_renewed_without_holding_the_seat():
    controller, api = make_controller()
    controller.state = make_state()
    controller.actions._clock.advance(1000)
    assert await controller.actions.renew_if_due() is None and api.posts == []


def mine():
    return make_state(seat={"holder_session_id": "s1", "holder_operator": "admin", "lease_until_ms": None, "mine": True})


async def test_set_config_sends_the_value_with_a_fresh_op_id_and_returns_the_kernel_result():
    controller, api = make_controller()
    api.set("PUT /config/takeover_symbols", {"key": "takeover_symbols", "old": [], "new": ["BTCUSDT"],
                                              "applies": "只影响之后新出现的仓", "affects_existing": False})
    outcome = await controller.actions.set_config("takeover_symbols", ["BTCUSDT"])
    assert outcome.ok and outcome.result["new"] == ["BTCUSDT"] and outcome.result["applies"] == "只影响之后新出现的仓"
    body = api.puts_to("takeover_symbols")[0]
    assert body["value"] == ["BTCUSDT"] and body["op_id"].startswith("c-")
    await controller.actions.set_config("takeover_symbols", ["ETHUSDT"])
    assert api.puts_to("takeover_symbols")[0]["op_id"] != api.puts_to("takeover_symbols")[1]["op_id"]


async def test_set_config_refusal_keeps_code_and_message_and_a_lost_kernel_keeps_the_op_id():
    controller, api = make_controller()
    api.set("PUT /config/leverage", KernelError(422, "invalid", "杠杆 不能大于 20"))
    refused = await controller.actions.set_config("leverage", 99)
    assert not refused.ok and refused.code == "invalid" and "不能大于" in refused.text
    api.set("PUT /config/leverage", lost("timeout"))
    unknown = await controller.actions.set_config("leverage", 5)
    assert unknown.unreachable
    api.heal()
    await controller.actions.set_config("leverage", 5)
    ids = [b["op_id"] for b in api.puts_to("leverage")]
    assert ids[1] == ids[2] != ids[0]


async def test_set_config_with_a_dead_session_invalidates_the_controller():
    controller, api = make_controller()
    api.valid_tokens.clear()
    outcome = await controller.actions.set_config("leverage", 5)
    assert not outcome.ok and controller.session_invalid


async def test_fetch_config_returns_items_or_an_error_text():
    controller, api = make_controller()
    api.set("config", [{"key": "leverage", "group": "资金"}])
    items, error = await controller.actions.fetch_config()
    assert items == [{"key": "leverage", "group": "资金"}] and error == ""
    api.set("config", lost("connect"))
    items, error = await controller.actions.fetch_config()
    assert items is None and "无法连接" in error


async def test_two_tabs_clicking_the_same_slot_at_once_send_different_op_ids():
    """同一浏览器的两个标签页共用一个控制器：第一个请求还在途时，第二个点击不能复用同一个操作号（否则内核按幂等重放，
    把第一个的成功结果原样回给第二个标签页）；第二个用新号，由内核自己判（例如持仓版本已过期而被拒并写明原因）。"""
    import asyncio

    controller, api = make_controller()
    gate = asyncio.Event()

    async def slow(body):
        await gate.wait()
        return {"op_id": body["op_id"], "status": "succeeded", "result": {}}

    api.set("POST /ops/partial_close/confirm", slow)
    build = lambda op: O.partial_confirm_payload(op, {"position_id": 7, "position_version": 3, "live_qty": "1", "close_qty": "0.5"})
    first = asyncio.ensure_future(controller.actions.call("partial:7", "/ops/partial_close/confirm", build))
    await asyncio.sleep(0)
    second = asyncio.ensure_future(controller.actions.call("partial:7", "/ops/partial_close/confirm", build))
    await asyncio.sleep(0)
    gate.set()
    await asyncio.gather(first, second)
    ids = [b["op_id"] for b in api.posts_to("/ops/partial_close/confirm")]
    assert len(ids) == 2 and ids[0] != ids[1]
