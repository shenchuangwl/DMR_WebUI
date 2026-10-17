"""``stop``、``close-all``（内核存活时的命令行路径，T1.7 第一条）和 ``inventory``（T1.9）。

只经控制接口：停止调 /ops/stop，全部平仓调 /ops/close_all/preview + confirm 并轮询 /operations/{op_id}；范围固定「全部托管持仓」；
本机令牌，不要席位、不要口令；清点只读。传输一律是假的，不开套接字。
"""
from __future__ import annotations

import pytest

from cli_kit import UNREACHABLE, error_response, json_response, transport
from ops_kit import run_ops

STATE = {"env": "standin", "model": {"active": "m1", "gate": "running", "version": 7, "suppressions": [], "op_locks": []}}


def position(pid: int, symbol: str, side: str, **extra) -> dict:
    base = {"id": pid, "symbol": symbol, "side": side, "status": "managed", "ownership": "strategy", "qty": "1.5",
            "avg_price": "100", "mark_price": "101", "unrealized_pnl": "1.5", "protection_state": "protected",
            "stop_price": "95", "version": 3, "closing": False, "pending_reason": None, "origin_note": None,
            "has_unconfirmed_orders": False, "is_mixed": False}
    return {**base, **extra}


PREVIEW = {"scope": "all_managed", "model_version": 7,
           "included": [position(1, "BTCUSDT", "LONG"), position(2, "ETHUSDT", "SHORT")],
           "excluded": [{"position": position(3, "XRPUSDT", "LONG", status="pending"), "reason": "待确认的仓不平"}],
           "mixed_included": []}


def per(pid: int, symbol: str, side: str, result: str, detail: str = "") -> dict:
    return {"position_id": pid, "symbol": symbol, "side": side, "result": result, "detail": detail}


# ---------------------------------------------------------------- stop


def test_stop_reads_the_model_version_then_posts_stop_with_an_op_id(rig):
    fake = transport(json_response(200, STATE),
                     json_response(200, {"op_id": "x", "status": "succeeded",
                                         "result": {"gate": "stopped", "cancelled_open_orders": 2, "managed_remaining": 5}}))
    result, _, _ = run_ops(rig, ["stop"], fake)
    assert result.code == 0, result.text
    get, post = fake.sent
    assert (get.method, get.path) == ("GET", "/api/v1/state")
    assert (post.method, post.path) == ("POST", "/api/v1/ops/stop")
    body = post.json()
    assert body["model_version"] == 7 and 1 <= len(body["op_id"]) <= 64
    assert "password" not in body and "close_all" not in body
    assert post.headers["Authorization"] == f"Bearer {rig.token}"
    assert "已停止" in result.out and "2" in result.out and "5" in result.out
    assert rig.token not in result.text


def test_each_stop_uses_a_fresh_op_id(rig):
    ids = []
    for _ in range(2):
        fake = transport(json_response(200, STATE), json_response(200, {"op_id": "x", "status": "succeeded", "result": {}}))
        run_ops(rig, ["stop"], fake)
        ids.append(fake.sent[1].json()["op_id"])
    assert ids[0] != ids[1]


def test_stop_refused_by_the_kernel_prints_the_reason_and_fails(rig):
    fake = transport(json_response(200, STATE), error_response(409, "not_allowed", "已经是停止状态"))
    result, _, _ = run_ops(rig, ["stop"], fake)
    assert result.code == 1 and "已经是停止状态" in result.err


def test_stop_with_the_kernel_down_points_at_the_emergency_path(rig):
    result, _, _ = run_ops(rig, ["stop"], transport(UNREACHABLE("connect")))
    assert result.code == 3 and "emergency close" in result.err


def test_stop_does_not_resend_when_the_answer_is_lost(rig):
    fake = transport(json_response(200, STATE), UNREACHABLE("io"))
    result, _, _ = run_ops(rig, ["stop"], fake)
    assert result.code == 3 and len(fake.sent) == 2
    assert "不确定" in result.err


# ---------------------------------------------------------------- close-all


def test_close_all_previews_asks_confirms_and_polls_to_the_end(rig):
    fake = transport(
        json_response(200, STATE),
        json_response(200, PREVIEW),
        json_response(200, {"op_id": "x", "status": "running", "result": {"status": "running"}}),
        json_response(200, {"op_id": "x", "status": "running", "per_position": [per(1, "BTCUSDT", "LONG", "closed")]}),
        json_response(200, {"op_id": "x", "status": "succeeded",
                            "per_position": [per(1, "BTCUSDT", "LONG", "closed"), per(2, "ETHUSDT", "SHORT", "closed")],
                            "result": {"per_position": [per(1, "BTCUSDT", "LONG", "closed"),
                                                        per(2, "ETHUSDT", "SHORT", "closed")],
                                       "unknown_open_orders": [], "duration_ms": 1200}}))
    result, asked, clock = run_ops(rig, ["close-all"], fake, answers=["y"])
    assert result.code == 0, result.text
    paths = [(s.method, s.path) for s in fake.sent]
    assert paths[:3] == [("GET", "/api/v1/state"), ("POST", "/api/v1/ops/close_all/preview"),
                         ("POST", "/api/v1/ops/close_all/confirm")]
    assert all(p[1].startswith("/api/v1/operations/") for p in paths[3:]) and len(paths) == 5
    assert fake.sent[1].json() == {"scope": "all_managed"}
    confirm = fake.sent[2].json()
    assert confirm["scope"] == "all_managed" and confirm["model_version"] == 7
    assert "password" not in confirm
    assert fake.sent[3].path.endswith("/" + confirm["op_id"])
    assert len(asked) == 1
    assert "BTCUSDT" in result.out and "ETHUSDT" in result.out and "XRPUSDT" in result.out
    assert result.out.count("BTCUSDT") >= 2  # 预览里一次，逐仓结果里一次（同一条结果只打印一次）
    assert "已平" in result.out
    assert clock.sleeps  # 轮询之间睡了


def test_close_all_prints_each_per_position_result_only_once(rig):
    finished = {"op_id": "x", "status": "succeeded", "per_position": [per(1, "BTCUSDT", "LONG", "closed")]}
    fake = transport(json_response(200, STATE), json_response(200, PREVIEW),
                     json_response(200, {"status": "running"}),
                     json_response(200, {**finished, "status": "running"}), json_response(200, finished))
    result, _, _ = run_ops(rig, ["close-all"], fake, answers=["y"])
    lines = [ln for ln in result.out.splitlines() if "逐仓" in ln or ("BTCUSDT" in ln and "已平" in ln)]
    assert len([ln for ln in lines if "已平" in ln]) == 1


def test_close_all_declined_sends_no_confirm(rig):
    fake = transport(json_response(200, STATE), json_response(200, PREVIEW))
    result, _, _ = run_ops(rig, ["close-all"], fake, answers=["n"])
    assert result.code == 1 and len(fake.sent) == 2 and "已取消" in result.out


def test_close_all_eof_on_the_prompt_counts_as_no(rig):
    fake = transport(json_response(200, STATE), json_response(200, PREVIEW))
    result, _, _ = run_ops(rig, ["close-all"], fake, answers=[])
    assert result.code == 1 and len(fake.sent) == 2


def test_close_all_yes_flag_skips_the_prompt(rig):
    fake = transport(json_response(200, STATE), json_response(200, PREVIEW), json_response(200, {"status": "running"}),
                     json_response(200, {"status": "succeeded", "per_position": []}))
    result, asked, _ = run_ops(rig, ["close-all", "--yes"], fake)
    assert result.code == 0 and asked == []


def test_close_all_with_nothing_to_close_says_so_and_stops(rig):
    empty = {"scope": "all_managed", "model_version": 7, "included": [], "excluded": [], "mixed_included": []}
    fake = transport(json_response(200, STATE), json_response(200, empty))
    result, _, _ = run_ops(rig, ["close-all"], fake, answers=["y"])
    assert result.code == 0 and len(fake.sent) == 2 and "没有" in result.out


@pytest.mark.parametrize("final, code", [("partial", 1), ("failed", 1), ("unknown", 1), ("aborted", 1), ("succeeded", 0)])
def test_close_all_exit_code_follows_the_final_status(rig, final, code):
    fake = transport(json_response(200, STATE), json_response(200, PREVIEW), json_response(200, {"status": "running"}),
                     json_response(200, {"status": final, "per_position": [per(1, "BTCUSDT", "LONG", "failed", "超时")]}))
    result, _, _ = run_ops(rig, ["close-all", "--yes"], fake)
    assert result.code == code


def test_close_all_shows_failed_positions_with_the_detail(rig):
    fake = transport(json_response(200, STATE), json_response(200, PREVIEW), json_response(200, {"status": "running"}),
                     json_response(200, {"status": "partial", "per_position": [
                         per(1, "BTCUSDT", "LONG", "closed"), per(2, "ETHUSDT", "SHORT", "failed", "交易所拒绝")]}))
    result, _, _ = run_ops(rig, ["close-all", "--yes"], fake)
    assert "交易所拒绝" in result.out and "失败" in result.out


def test_close_all_gives_up_polling_after_the_deadline_without_resending(rig):
    running = {"status": "running", "per_position": []}
    fake = transport(json_response(200, STATE), json_response(200, PREVIEW), json_response(200, {"status": "running"}),
                     *[json_response(200, running)] * 400)
    result, _, clock = run_ops(rig, ["close-all", "--yes"], fake)
    assert result.code == 3 and "仍在进行" in result.err
    assert [s.path for s in fake.sent].count("/api/v1/ops/close_all/confirm") == 1
    assert clock.now >= 120


def test_close_all_confirm_refused_stale_version_tells_to_retry(rig):
    fake = transport(json_response(200, STATE), json_response(200, PREVIEW),
                     error_response(409, "stale_version", "模型状态已变化，请重新预览"))
    result, _, _ = run_ops(rig, ["close-all", "--yes"], fake)
    assert result.code == 1 and "重新预览" in result.err


def test_close_all_survives_a_lost_poll_answer_and_keeps_polling(rig):
    fake = transport(json_response(200, STATE), json_response(200, PREVIEW), json_response(200, {"status": "running"}),
                     UNREACHABLE("io"), json_response(200, {"status": "succeeded", "per_position": []}))
    result, _, _ = run_ops(rig, ["close-all", "--yes"], fake)
    assert result.code == 0


def test_close_all_with_the_kernel_down_points_at_the_emergency_path(rig):
    result, _, _ = run_ops(rig, ["close-all", "--yes"], transport(UNREACHABLE("connect")))
    assert result.code == 3 and "emergency close" in result.err


# ---------------------------------------------------------------- inventory


MANAGED = position(1, "BTCUSDT", "LONG")
EXTERNAL = position(5, "SOLUSDT", "SHORT", status="pending", ownership="none", pending_reason="external_found",
                    origin_note="用户在网页上手动开的", protection_state="unprotected", stop_price=None)
IGNORED = position(6, "DOGEUSDT", "LONG", status="ignored", ownership="none")
ORDERS = {"system": [{"client_order_id": "DKAx-m1-abc-e", "symbol": "BTCUSDT", "side": "BUY", "position_side": "LONG",
                      "purpose": "open", "kind": "order", "order_type": "MARKET", "qty": "1", "filled_qty": "0",
                      "state": "sent", "raw_status": "NEW"}],
          "exchange_open": [{"client_order_id": None, "exchange_order_id": "99", "symbol": "SOLUSDT", "side": "SELL",
                             "position_side": "SHORT", "order_type": "LIMIT", "qty": "2", "filled_qty": "0",
                             "status": "NEW"}],
          "exchange_algo": [{"client_algo_id": "DKAx-m1-abc-s", "algo_id": "7", "symbol": "BTCUSDT", "side": "SELL",
                             "position_side": "LONG", "algo_type": "STOP_MARKET", "trigger_price": "95"},
                            {"client_algo_id": None, "algo_id": "8", "symbol": "SOLUSDT", "side": "BUY",
                             "position_side": "SHORT", "algo_type": "STOP_MARKET", "trigger_price": "120"}],
          "snapshot_ms": 1}


def inventory_fake():
    return transport(json_response(200, {"items": [MANAGED]}), json_response(200, {"items": [EXTERNAL, IGNORED]}),
                     json_response(200, ORDERS))


def test_inventory_lists_all_positions_and_orders_through_the_control_interface(rig):
    fake = inventory_fake()
    result, _, _ = run_ops(rig, ["inventory"], fake)
    assert result.code == 0, result.text
    assert [(s.method, s.path) for s in fake.sent] == [
        ("GET", "/api/v1/positions"), ("GET", "/api/v1/positions/external"), ("GET", "/api/v1/orders")]
    for fragment in ("BTCUSDT", "SOLUSDT", "DOGEUSDT", "待确认", "托管", "外部"):
        assert fragment in result.out
    assert rig.token not in result.text


def test_inventory_offers_close_adopt_ignore_for_unconfirmed_and_only_lists(rig):
    result, _, _ = run_ops(rig, ["inventory"], inventory_fake())
    assert "平掉" in result.out and "接管" in result.out and "忽略" in result.out
    assert "M3" in result.out  # 接管在 M3 才可用
    assert "只列出" in result.out or "不会自动执行" in result.out


def test_inventory_marks_external_orders_and_system_orders_apart(rig):
    result, _, _ = run_ops(rig, ["inventory"], inventory_fake())
    assert "DKAx-m1-abc-e" in result.out
    assert "99" in result.out and "8" in result.out  # 没有系统编号的挂单与条件单也列出
    sol = [ln for ln in result.out.splitlines() if "SOLUSDT" in ln and ("LIMIT" in ln or "STOP" in ln)]
    assert sol and all("外部" in ln for ln in sol)


def test_inventory_never_posts(rig):
    fake = inventory_fake()
    run_ops(rig, ["inventory"], fake)
    assert all(s.method == "GET" for s in fake.sent)


def test_inventory_with_nothing_says_so(rig):
    fake = transport(json_response(200, {"items": []}), json_response(200, {"items": []}),
                     json_response(200, {"system": [], "exchange_open": [], "exchange_algo": [], "snapshot_ms": None}))
    result, _, _ = run_ops(rig, ["inventory"], fake)
    assert result.code == 0 and "没有持仓" in result.out and "没有挂单" in result.out


def test_inventory_orders_snapshot_missing_is_said_not_hidden(rig):
    fake = transport(json_response(200, {"items": [MANAGED]}), json_response(200, {"items": []}),
                     json_response(200, {"system": [], "exchange_open": [], "exchange_algo": [], "snapshot_ms": None}))
    result, _, _ = run_ops(rig, ["inventory"], fake)
    assert "快照" in result.out


def test_inventory_tolerates_odd_shapes(rig):
    fake = transport(json_response(200, {"items": [{"symbol": "BTCUSDT"}, "junk"]}), json_response(200, {}),
                     json_response(200, {"system": None}))
    result, _, _ = run_ops(rig, ["inventory"], fake)
    assert result.code == 0 and "BTCUSDT" in result.out


def test_inventory_with_the_kernel_down_exits_3(rig):
    result, _, _ = run_ops(rig, ["inventory"], transport(UNREACHABLE("connect")))
    assert result.code == 3
