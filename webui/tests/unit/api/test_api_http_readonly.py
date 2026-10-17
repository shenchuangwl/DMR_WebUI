"""控制接口的只读部分：/state、/positions、/positions/external、/orders、/oplog、/alerts、/config、/operations，
以及操作类接口在没有装配操作服务时的答复（已实现的答 501「尚未装配」，接管类仍是 501 占位）。只读接口绝不改变任何状态。

报文约定：docs/plans/M1-控制接口只读部分.md 第 3 节；服务是真实实现 + 临时账本（见 api_kit.Kit）。
"""
from __future__ import annotations

import json
from decimal import Decimal

import pytest

from api_kit import T0, algo_info, order_info, position_info
from dmr_kernel.alerts.heartbeat import Heartbeat
from dmr_kernel.api.common import KERNEL_VERSION
from dmr_kernel.core.events import (
    AccountSnapshotTaken,
    OrdersSnapshotTaken,
    PositionSnapshotTaken,
    ReconcileCompleted,
    StreamStatusChanged,
)
from dmr_kernel.core.modelstate import allowed_operations
from dmr_kernel.core.types import (
    AlertLevel,
    AlgoType,
    Model,
    OpLock,
    OrderState,
    PositionSide,
    RunMode,
    Suppression,
)
from dmr_kernel.ledger import repo_orders
from dmr_kernel.models.m1_strategy import StrategyModel
from dmr_kernel.models.m2_takeover import TakeoverModel

D = Decimal
STATE_KEYS = ["env", "standin", "time_ms", "heartbeat_ms", "kernel_version", "model", "model3", "allowed", "button_meanings",
              "account", "counts", "list", "stream", "reconcile", "position_mode", "restrictions", "risk", "limits",
              "seat", "me"]
OPERATION_KEYS = ["switch", "switch_confirm", "switch_cancel", "start", "stop", "partial_close", "close_one",
                  "close_all", "abort", "takeover", "ignore", "restore", "cancel_takeover", "hand_stop_to_system"]
POSITION_KEYS = [
    "id", "symbol", "side", "generation", "status", "ownership", "is_mixed", "qty", "avg_price", "mark_price",
    "unrealized_pnl", "liquidation_price", "protection_state", "stop_price", "stop_note", "exit_ruleset",
    "signal_exit", "open_time_ms", "open_time_estimated", "exchange_qty", "consistent", "has_unconfirmed_orders",
    "closing", "pending_reason", "pending_flags", "origin_note", "leverage", "margin_mode", "has_stop_on_exchange",
    "version"]


@pytest.fixture
def sess(kit):
    return kit.login()


def state(sess) -> dict:
    response = sess.get("/state")
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------- /state：字段与来源

def test_state_has_exactly_the_agreed_top_level_fields_in_order(sess):
    assert list(state(sess)) == STATE_KEYS


def test_state_header_fields(kit, sess):
    body = state(sess)
    assert (body["env"], body["standin"], body["time_ms"], body["kernel_version"]) == (
        "standin", True, T0, KERNEL_VERSION)
    assert body["heartbeat_ms"] is None  # 还没有心跳文件


def test_state_in_a_non_standin_environment_says_so(open_kit):
    with open_kit(env="testnet") as kit:
        body = state(kit.login())
    assert (body["env"], body["standin"]) == ("testnet", False)


def test_heartbeat_comes_from_the_heartbeat_file(kit, sess):
    Heartbeat(kit.heartbeat_path, kit.clock, pid=4242).beat()
    kit.clock.advance(5_000)
    assert state(sess)["heartbeat_ms"] == T0


def test_a_broken_heartbeat_file_means_no_heartbeat(kit, sess):
    kit.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    kit.heartbeat_path.write_text("not json", encoding="utf-8")
    assert state(sess)["heartbeat_ms"] is None


def test_model_section_defaults(kit, sess):
    model = state(sess)["model"]
    assert list(model) == ["active", "gate", "version", "suppressions", "op_locks", "run_mode"]
    assert model == {"active": "m1", "gate": "stopped", "version": kit.modelstate.current().version,
                     "suppressions": [], "op_locks": [], "run_mode": {"code": "normal", "label": "正常"}}


def test_model_section_shows_suppressions_locks_and_run_mode_with_labels(kit, sess):
    kit.run(kit.modelstate.add_suppression(Suppression.RECOVERING, reason="测试"))
    kit.run(kit.modelstate.add_suppression(Suppression.CLOCK_SKEW, reason="测试"))
    kit.run(kit.modelstate.acquire_op_lock(OpLock.SWITCH_PREVIEW, owner="alice", expires_ms=T0 + 60_000))
    kit.run(kit.modelstate.set_run_mode(RunMode.POLLING_DEGRADED, reason="测试"))
    model = state(sess)["model"]
    assert model["version"] == kit.modelstate.current().version
    assert model["suppressions"] == [{"code": "recovering", "label": "恢复中"},
                                     {"code": "clock_skew", "label": "时钟偏差"}]
    assert model["op_locks"] == [{"code": "switch_preview", "label": "切换预览中", "owner": "alice",
                                  "expires_ms": T0 + 60_000}]
    assert model["run_mode"] == {"code": "polling_degraded", "label": "仅轮询降级"}


def test_allowed_is_the_modelstate_table_verbatim(kit, sess):
    kit.run(kit.modelstate.add_suppression(Suppression.RECOVERING, reason="测试"))
    allowed = state(sess)["allowed"]
    assert list(allowed) == OPERATION_KEYS
    expected = allowed_operations(kit.modelstate.current())
    for key, permission in expected.items():
        assert allowed[key] == {"allowed": permission.allowed, "reason": permission.reason,
                                "needs_live_read": permission.needs_live_read, "note": permission.note}
    assert allowed["start"]["allowed"] is False and "恢复中" in allowed["start"]["reason"]


def test_button_meanings_follow_the_active_model(kit, sess):
    assert state(sess)["button_meanings"] == StrategyModel().button_meanings()
    version = kit.modelstate.current().version
    kit.run(kit.modelstate.set_active_model(Model.M2, expected_version=version, operator="alice", op_id="op-1"))
    body = state(sess)
    assert body["model"]["active"] == "m2" and body["button_meanings"] == TakeoverModel().button_meanings()


def test_account_is_unknown_until_a_snapshot_arrives_then_strings(kit, sess):
    unknown = state(sess)["account"]
    assert unknown["known"] is False and unknown["reason"]
    assert (unknown["equity"], unknown["available"], unknown["wallet"], unknown["at_ms"]) == (None,) * 4
    kit.publish(AccountSnapshotTaken(equity=D("1234.50"), available=D("900"), wallet=D("1E+3"), at_ms=T0 + 9))
    assert state(sess)["account"] == {"known": True, "equity": "1234.50", "available": "900", "wallet": "1000",
                                      "at_ms": T0 + 9}


def test_counts_come_from_the_registry(kit, sess):
    kit.add_position()
    kit.add_position(symbol="ETHUSDT", side=PositionSide.SHORT, intent_id=2)
    kit.add_pending_position(symbol="XRPUSDT")
    counts = state(sess)["counts"]
    assert list(counts) == ["managed", "unprotected", "taking_over", "closing", "pending", "external"]
    assert counts == kit.registry.counts()
    assert counts["managed"] == 2 and counts["pending"] == 1


def _list_view(*, valid: bool, reason: str = "", members: int = 0, valid_until_ms: int | None = None,
               fetched_ms: int | None = T0):
    from dmr_kernel.listsync import ListMember, ListView

    rows = tuple(ListMember(symbol=f"C{i}USDT", direction=PositionSide.LONG, score=D(10 - i), first_selected_ms=None,
                            raw_code=f"C{i}") for i in range(members))
    return ListView(valid=valid, reason=reason, snapshot_id=7 if members or valid else None, fetched_ms=fetched_ms,
                    valid_until_ms=valid_until_ms, business_date="2026-10-17", status="ok", members=rows,
                    rejected=(("XYZ", "交易所查不到"),))


def test_list_without_a_list_sync_is_unknown_with_a_reason(sess):
    info = state(sess)["list"]
    assert info == {"known": False, "valid": False, "reason": "名单同步没有接入", "member_count": 0,
                    "valid_until_ms": None}
    assert "M4" not in info["reason"]


def test_list_comes_from_the_current_view_of_the_list_sync(kit, sess):
    view = _list_view(valid=True, members=3, valid_until_ms=T0 + 3_600_000)
    kit.ctx.list_view = lambda: view
    assert state(sess)["list"] == {"known": True, "valid": True, "reason": "", "member_count": 3,
                                   "valid_until_ms": T0 + 3_600_000}


def test_a_valid_but_empty_list_is_still_valid(kit, sess):
    kit.ctx.list_view = lambda: _list_view(valid=True, members=0, valid_until_ms=T0 + 60_000)
    info = state(sess)["list"]
    assert info["known"] is True and info["valid"] is True and info["member_count"] == 0


def test_an_invalid_list_reports_its_reason_and_the_members_it_still_carries(kit, sess):
    view = _list_view(valid=False, reason="HTTP 503", members=2, valid_until_ms=T0 + 60_000)
    kit.ctx.list_view = lambda: view
    assert state(sess)["list"] == {"known": True, "valid": False, "reason": "HTTP 503", "member_count": 2,
                                   "valid_until_ms": T0 + 60_000}


def test_a_list_sync_that_has_not_fetched_yet_says_so(kit, sess):
    kit.ctx.list_view = lambda: _list_view(valid=False, fetched_ms=None)
    info = state(sess)["list"]
    assert info["known"] is True and info["valid"] is False and info["member_count"] == 0
    assert info["reason"] == "还没有拉取过名单"


def test_the_list_entry_reads_the_view_at_every_request(kit, sess):
    views = [_list_view(valid=False, reason="valid=false"), _list_view(valid=True, members=1, valid_until_ms=T0 + 1)]
    kit.ctx.list_view = lambda: views[0]
    assert state(sess)["list"]["valid"] is False
    views.pop(0)
    assert state(sess)["list"]["valid"] is True and state(sess)["list"]["member_count"] == 1


def test_stream_is_unknown_until_a_status_arrives(kit, sess):
    unknown = state(sess)["stream"]
    assert unknown["known"] is False and unknown["state"] is None and unknown["last_frame_ms"] is None
    assert unknown["reason"]
    kit.publish(StreamStatusChanged(state="connected", reason="", at_ms=T0 + 3))
    assert state(sess)["stream"] == {"known": True, "state": "connected", "last_frame_ms": T0 + 3, "reason": ""}
    kit.publish(StreamStatusChanged(state="interrupted", reason="静默超时", at_ms=T0 + 90_000))
    stream = state(sess)["stream"]
    assert (stream["state"], stream["last_frame_ms"], stream["reason"]) == ("interrupted", T0 + 3, "静默超时")


def test_reconcile_is_unknown_until_each_kind_has_run(kit, sess):
    unknown = state(sess)["reconcile"]
    assert list(unknown) == ["positions", "orders"]
    for part in unknown.values():
        assert part["known"] is False and part["last_ms"] is None and part["status"] is None and part["reason"]
    kit.publish(ReconcileCompleted(kind="positions", status="ok", at_ms=T0 + 4))
    reconcile = state(sess)["reconcile"]
    assert reconcile["positions"] == {"known": True, "last_ms": T0 + 4, "status": "ok"}
    assert reconcile["orders"]["known"] is False
    kit.publish(ReconcileCompleted(kind="orders", status="mismatch", at_ms=T0 + 5))
    assert state(sess)["reconcile"]["orders"] == {"known": True, "last_ms": T0 + 5, "status": "mismatch"}


def test_position_mode_and_restrictions_are_unknown_until_the_assembly_reports_them(kit, sess):
    body = state(sess)
    assert body["position_mode"]["known"] is False and body["position_mode"]["dual_side"] is None
    assert body["restrictions"]["known"] is False and body["restrictions"]["restricted"] is None
    assert body["position_mode"]["reason"] and body["restrictions"]["reason"]
    kit.services.live.note_position_mode(True)
    kit.services.live.note_restrictions(False)
    body = state(sess)
    assert body["position_mode"] == {"known": True, "dual_side": True}
    assert body["restrictions"] == {"known": True, "restricted": False}


def test_risk_is_the_riskgate_status_verbatim(kit, sess):
    assert state(sess)["risk"] == json.loads(json.dumps(kit.riskgate.status()))


def test_limits_report_occupancy_and_the_cap(kit, sess):
    assert state(sess)["limits"] == {"used": 0, "reserved": 0, "cap": 30, "stale": True}  # 还没有任何快照
    kit.limits.update_positions([position_info("BTCUSDT"), position_info("ETHUSDT", PositionSide.SHORT)], T0)
    kit.limits.update_orders([], [], T0)
    assert state(sess)["limits"] == {"used": 2, "reserved": 0, "cap": 30, "stale": False}
    assert kit.limits.try_reserve(7, "XRPUSDT", PositionSide.LONG, degraded=False, now_ms=T0).allowed
    assert state(sess)["limits"]["reserved"] == 1


def test_limits_cap_drops_in_polling_degraded_mode(kit, sess):
    kit.run(kit.modelstate.set_run_mode(RunMode.POLLING_DEGRADED, reason="测试"))
    assert state(sess)["limits"]["cap"] == kit.config.get("max_symbols_degraded")


def test_seat_and_me_in_state(kit):
    alice, bob = kit.login("alice"), kit.login("bob")
    assert state(alice)["seat"] == {"holder_session_id": None, "holder_operator": None, "lease_until_ms": None,
                                    "mine": False}
    assert state(alice)["me"] == {"operator": "alice", "principal": "session", "reauth_valid_until_ms": None}
    alice.post("/seat/acquire")
    assert state(alice)["seat"]["mine"] is True
    seen_by_bob = state(bob)["seat"]
    assert seen_by_bob["holder_operator"] == "alice" and seen_by_bob["mine"] is False
    bob.post("/auth/reauth", json={"password": "correct-horse-battery"})
    assert state(bob)["me"]["reauth_valid_until_ms"] == T0 + 300_000


def test_state_for_the_cli_principal(kit):
    body = state(kit.cli())
    assert body["me"] == {"operator": "cli", "principal": "cli", "reauth_valid_until_ms": None}
    assert body["seat"]["mine"] is False


# ---------------------------------------------------------------- /positions

def test_positions_are_empty_at_first(sess):
    assert sess.get("/positions").json() == {"items": []}
    assert sess.get("/positions/external").json() == {"items": []}


def test_positions_lists_registered_ones_sorted_by_symbol_then_side(kit, sess):
    kit.add_position(symbol="ETHUSDT", side=PositionSide.SHORT, intent_id=1)
    kit.add_position(symbol="BTCUSDT", side=PositionSide.SHORT, intent_id=2)
    kit.add_position(symbol="BTCUSDT", side=PositionSide.LONG, intent_id=3)
    items = sess.get("/positions").json()["items"]
    assert [(i["symbol"], i["side"]) for i in items] == [("BTCUSDT", "LONG"), ("BTCUSDT", "SHORT"),
                                                         ("ETHUSDT", "SHORT")]
    assert all(list(item) == POSITION_KEYS for item in items)


def test_position_item_values(kit, sess):
    rec = kit.add_position(symbol="BTCUSDT", qty=D("0.5"), avg_price=D("60000"))
    (item,) = sess.get("/positions").json()["items"]
    assert (item["id"], item["status"], item["ownership"], item["qty"], item["avg_price"]) == (
        rec.id, "managed", "strategy", "0.5", "60000")
    assert (item["leverage"], item["margin_mode"], item["version"]) == (2, "isolated", rec.version)
    assert (item["mark_price"], item["unrealized_pnl"]) == (None, None)  # 还没有持仓快照
    assert (item["has_stop_on_exchange"], item["has_unconfirmed_orders"], item["closing"]) == (None, False, False)


def test_positions_exclude_pending_and_ignored_ones_and_external_lists_exactly_those(kit, sess):
    managed = kit.add_position()
    pending = kit.add_pending_position(symbol="ETHUSDT")
    ignored = kit.add_pending_position(symbol="XRPUSDT")
    kit.run(kit.registry.ignore(ignored.id, expected_version=None))
    assert [i["id"] for i in sess.get("/positions").json()["items"]] == [managed.id]
    external = sess.get("/positions/external").json()["items"]
    assert [(i["symbol"], i["status"]) for i in external] == [("ETHUSDT", "pending"), ("XRPUSDT", "ignored")]
    pending_item = external[0]
    assert pending_item["id"] == pending.id and pending_item["pending_reason"] == "out_of_scope"
    assert pending_item["pending_flags"] == ["manual"] and pending_item["origin_note"] == "来源不明"
    assert pending_item["ownership"] == "none" and pending_item["qty"] == "2"


def test_closing_positions_are_registered_ones_and_flagged(kit, sess):
    rec = kit.add_position()
    kit.run(kit.registry.set_closing(rec.id, True, cause="test"))
    (item,) = sess.get("/positions").json()["items"]
    assert item["status"] == "closing" and item["closing"] is True


def test_mark_price_and_pnl_come_from_the_latest_position_snapshot(kit, sess):
    kit.add_position(symbol="BTCUSDT", side=PositionSide.LONG, intent_id=1)
    kit.add_position(symbol="ETHUSDT", side=PositionSide.SHORT, intent_id=2)
    kit.publish(PositionSnapshotTaken(
        positions=(position_info("BTCUSDT", PositionSide.LONG, mark=D("61000"), pnl=D("500")),
                   position_info("ETHUSDT", PositionSide.SHORT, mark=D("3100.5"), pnl=D("-20.25"))),
        server_time_ms=T0, at_ms=T0))
    btc, eth = sess.get("/positions").json()["items"]
    assert (btc["mark_price"], btc["unrealized_pnl"]) == ("61000", "500")
    assert (eth["mark_price"], eth["unrealized_pnl"]) == ("3100.5", "-20.25")
    kit.publish(PositionSnapshotTaken(positions=(position_info("BTCUSDT", mark=D("62000"), pnl=D("1000")),),
                                      server_time_ms=T0 + 1, at_ms=T0 + 1))
    btc, eth = sess.get("/positions").json()["items"]
    assert btc["mark_price"] == "62000" and eth["mark_price"] is None  # 新快照里没有 ETH：不沿用旧值


def test_unconfirmed_orders_flag_follows_orders_whose_fate_is_unsettled(kit, sess):
    kit.add_position(symbol="BTCUSDT", side=PositionSide.LONG, intent_id=1)
    kit.add_position(symbol="ETHUSDT", side=PositionSide.LONG, intent_id=2)
    kit.add_order("DKA-1-1-o", symbol="BTCUSDT", position_side=PositionSide.LONG)  # 刚落盘还没发出
    btc, eth = sess.get("/positions").json()["items"]
    assert btc["has_unconfirmed_orders"] is True and eth["has_unconfirmed_orders"] is False


def test_stop_on_exchange_flag_follows_the_latest_orders_snapshot(kit, sess):
    kit.add_position(symbol="BTCUSDT", side=PositionSide.LONG, intent_id=1)
    kit.add_position(symbol="ETHUSDT", side=PositionSide.LONG, intent_id=2)
    kit.publish(OrdersSnapshotTaken(open_orders=(), algo_orders=(
        algo_info("BTCUSDT", position_side=PositionSide.LONG, algo_type=AlgoType.STOP_MARKET),
        algo_info("ETHUSDT", cid="DKA-1-2-t", aid="2002", position_side=PositionSide.LONG,
                  algo_type=AlgoType.TAKE_PROFIT_MARKET)), at_ms=T0))
    btc, eth = sess.get("/positions").json()["items"]
    assert btc["has_stop_on_exchange"] is True and eth["has_stop_on_exchange"] is False


# ---------------------------------------------------------------- /orders

def test_orders_are_empty_at_first(sess):
    assert sess.get("/orders").json() == {"system": [], "exchange_open": [], "exchange_algo": [], "snapshot_ms": None}


def test_orders_list_system_orders_that_have_not_reached_a_final_state(kit, sess):
    kit.add_order("DKA-1-1-o", symbol="BTCUSDT")
    kit.add_order("DKA-1-2-o", symbol="ETHUSDT", position_side=PositionSide.SHORT)
    done = kit.add_order("DKA-1-3-o", symbol="XRPUSDT")
    kit.ledger.write_sync(repo_orders.reject, done.client_order_id, now_ms=T0, error_code=-2010,
                          error_msg="余额不足", from_states=None, audit=False)
    system = sess.get("/orders").json()["system"]
    assert [o["client_order_id"] for o in system] == ["DKA-1-1-o", "DKA-1-2-o"]  # 终态的不列
    assert all(o["state"] == OrderState.PENDING_SEND.value for o in system)
    assert system[1]["position_side"] == "SHORT" and system[0]["qty"] == "0.01"
    assert list(system[0]) == ["client_order_id", "intent_id", "purpose", "symbol", "side", "position_side", "kind",
                               "order_type", "qty", "filled_qty", "avg_price", "trigger_price", "state",
                               "raw_status", "sent_ms", "updated_ms"]


def test_orders_include_the_latest_exchange_snapshot(kit, sess):
    o = order_info("BTCUSDT", cid="DKA-1-1-o", oid="1001", qty=D("0.5"), filled=D("0.1"), avg=D("60000.5"),
                   status="PARTIALLY_FILLED")
    a = algo_info("BTCUSDT", cid="DKA-1-1-s", aid="2001", trigger=D("58000"))
    kit.publish(OrdersSnapshotTaken(open_orders=(o,), algo_orders=(a,), at_ms=T0 + 40))
    body = sess.get("/orders").json()
    assert body["snapshot_ms"] == T0 + 40 and body["system"] == []
    (open_order,) = body["exchange_open"]
    assert (open_order["exchange_order_id"], open_order["qty"], open_order["filled_qty"], open_order["avg_price"],
            open_order["status"]) == ("1001", "0.5", "0.1", "60000.5", "PARTIALLY_FILLED")
    (algo,) = body["exchange_algo"]
    assert (algo["algo_id"], algo["trigger_price"], algo["algo_type"], algo["raw_status"]) == (
        "2001", "58000", "STOP_MARKET", "NEW")


# ---------------------------------------------------------------- /oplog

def test_oplog_is_newest_first_with_the_agreed_fields(kit, sess):
    first = kit.add_audit(action="config.set", operator="alice", target="config:leverage", before={"value": 2},
                          after={"value": 3}, request_id="r-1")
    second = kit.add_audit(action="position.close", operator="bob", target="position:1", result="failed")
    items = sess.get("/oplog").json()["items"]
    assert [i["id"] for i in items[:2]] == [second.id, first.id]
    assert list(items[0]) == ["id", "at_ms", "operator", "action", "target", "result", "request_id", "before",
                              "after"]
    assert items[1] == {"id": first.id, "at_ms": T0, "operator": "alice", "action": "config.set",
                        "target": "config:leverage", "result": "ok", "request_id": "r-1", "before": {"value": 2},
                        "after": {"value": 3}}
    assert items[0]["result"] == "failed" and items[0]["before"] is None and items[0]["request_id"] is None


def test_oplog_paging_with_limit_and_before_id(kit, sess):
    ids = [kit.add_audit(action=f"test.{i}").id for i in range(6)]
    first_page = sess.get("/oplog?limit=2").json()["items"]
    assert [i["id"] for i in first_page] == [ids[5], ids[4]]
    second_page = sess.get(f"/oplog?limit=2&before_id={first_page[-1]['id']}").json()["items"]
    assert [i["id"] for i in second_page] == [ids[3], ids[2]]


def test_oplog_accepts_an_empty_before_id_like_the_agreed_url(kit, sess):
    kit.add_audit(action="test.a")
    items = sess.get("/oplog?limit=100&before_id=").json()["items"]
    assert items and items[0]["action"] == "test.a"


def test_oplog_default_limit_is_one_hundred_and_one_thousand_is_the_cap(kit, sess):
    for i in range(105):
        kit.add_audit(action=f"test.{i}")
    assert len(sess.get("/oplog").json()["items"]) == 100
    assert len(sess.get("/oplog?limit=1000").json()["items"]) >= 105
    assert len(sess.get("/oplog?limit=1").json()["items"]) == 1


@pytest.mark.parametrize("query", ["limit=0", "limit=1001", "limit=-1", "limit=abc", "limit=1.5", "before_id=abc",
                                   "before_id=0", "before_id=-3", "before_id=1.5"])
def test_oplog_rejects_bad_parameters_with_422(sess, query):
    response = sess.get(f"/oplog?{query}")
    assert response.status_code == 422 and response.json()["code"] == "invalid"


# ---------------------------------------------------------------- /alerts

def test_alerts_are_newest_first_with_the_agreed_fields(kit, sess):
    assert sess.get("/alerts").json() == {"items": []}
    older = kit.add_alert(AlertLevel.WARNING, "reconcile", "对账不一致", at_ms=T0 + 1, delivered=False)
    newer = kit.add_alert(AlertLevel.CRITICAL, "risk", "风控触发", at_ms=T0 + 2)
    items = sess.get("/alerts").json()["items"]
    assert items == [
        {"id": newer.id, "at_ms": T0 + 2, "level": "critical", "kind": "risk", "message": "风控触发",
         "delivered": True},
        {"id": older.id, "at_ms": T0 + 1, "level": "warning", "kind": "reconcile", "message": "对账不一致",
         "delivered": False}]


def test_alerts_limit(kit, sess):
    for i in range(5):
        kit.add_alert(AlertLevel.INFO, "test", f"第 {i} 条", at_ms=T0 + i)
    assert [a["message"] for a in sess.get("/alerts?limit=2").json()["items"]] == ["第 4 条", "第 3 条"]
    for bad in ("0", "1001", "abc", "-1"):
        assert sess.get(f"/alerts?limit={bad}").status_code == 422


# ---------------------------------------------------------------- /config 与 /operations

def test_config_lists_every_item_as_described_by_the_store(kit, sess):
    body = sess.get("/config").json()
    assert list(body) == ["items"]
    expected = json.loads(json.dumps(kit.config.describe(), default=str))
    assert [i["key"] for i in body["items"]] == [i["key"] for i in expected]
    by_key = {i["key"]: i for i in body["items"]}
    assert by_key["seat_lease_s"]["value"] == 60 and by_key["seat_lease_s"]["unit"] == "秒"
    assert by_key["max_symbols"]["min"] == 1 and by_key["max_symbols"]["max"] == 35
    assert set(by_key["seat_lease_s"]) >= {"key", "group", "label", "value", "default", "min", "max", "choices",
                                           "unit", "apply", "affects_existing", "required_for_live",
                                           "requires_deploy_switch", "note"}


def test_config_amounts_are_strings(kit, sess):
    items = {i["key"]: i for i in sess.get("/config").json()["items"]}
    decimals = [i for i in items.values() if isinstance(i["default"], str) and i["default"].replace(".", "").isdigit()]
    assert decimals, "至少有一项金额类配置，值与默认值都是字符串"
    for item in decimals:
        assert isinstance(item["value"], (str, type(None)))


def test_operations_are_not_there_in_m1(sess):
    response = sess.get("/operations/op-123")
    assert response.status_code == 404 and response.json()["code"] == "not_found"


@pytest.mark.parametrize("path", ["/watchlist"])
def test_later_milestone_reads_answer_501(sess, path):
    response = sess.get(path)
    assert response.status_code == 501 and response.json()["code"] == "not_implemented"


# ---------------------------------------------------------------- 操作类接口：已实现的与仍是占位的

# M2 已实现的入口（routes_ops.real、routes_bulk）。这里的小应用没有装配操作服务（ApiContext.ops_wiring 为空、
# 没有 app.state.bulk_ops）：先鉴权，再答 501「尚未装配」，不看请求体。装配好的整套内核见 tests/integration/test_m2_flows.py
IMPLEMENTED_OPS = ["/ops/start", "/ops/stop", "/ops/partial_close/preview", "/ops/partial_close/confirm",
                   "/ops/close_one", "/ops/recover", "/ops/switch/preview", "/ops/switch/confirm", "/ops/switch/cancel",
                   "/ops/close_all/preview", "/ops/close_all/confirm", "/ops/abort"]
# 仍是占位的入口（接管类在 M3）：一律 501 not_implemented
PLACEHOLDER_OPS = ["/ops/takeover", "/ops/ignore", "/ops/restore", "/ops/cancel_takeover", "/ops/hand_stop_to_system"]
OPS = IMPLEMENTED_OPS + PLACEHOLDER_OPS


@pytest.mark.parametrize("path", PLACEHOLDER_OPS)
def test_placeholder_operation_endpoints_answer_501_for_sessions_and_the_cli(kit, path):
    for who in (kit.login(), kit.cli()):
        response = who.post(path, json={"op_id": "op-1"})
        assert response.status_code == 501
        body = response.json()
        assert body["code"] == "not_implemented" and "M2" in body["message"]


@pytest.mark.parametrize("path", IMPLEMENTED_OPS)
def test_implemented_operation_endpoints_answer_501_only_while_the_services_are_not_wired(kit, path):
    for who in (kit.login(), kit.cli()):
        response = who.post(path, json={"op_id": "op-1"})
        assert response.status_code == 501, response.text
        body = response.json()
        assert body["code"] == "not_implemented" and "装配" in body["message"]
    assert kit.request("POST", path, json={"op_id": "op-1"}).status_code == 401  # 鉴权在前


def test_only_the_paths_not_yet_implemented_keep_a_placeholder():
    """已实现的路径不再留着被遮蔽的 501 占位（挂载顺序因此不再影响它们）。"""
    from dmr_kernel.api import routes_ops

    assert sorted(routes_ops.OPS_PATHS) == sorted(PLACEHOLDER_OPS)
    assert routes_ops.DEV_PATHS == ("/dev/inject_signal",)


def test_unwired_operation_endpoints_do_not_even_look_at_the_body(kit):
    sess = kit.login()
    for path in ("/ops/start", "/ops/takeover", "/ops/close_all/confirm"):
        assert sess.post(path).status_code == 501
        assert sess.post(path, json=[1, 2, 3]).status_code == 501


# 配置修改（PUT /config/{key}）已由 T0.23 实现，测试见 tests/unit/api/test_api_config_admin.py。


def test_dev_endpoints_exist_only_in_the_standin_environment(kit):
    sess = kit.login()
    for path in ("/dev/inject_signal", "/dev/test_open"):  # 造仓已实现，但这里的小应用没有装配操作服务；注入信号仍是占位
        response = sess.post(path, json={"op_id": "op-1"})
        assert response.status_code == 501 and response.json()["code"] == "not_implemented"


def test_dev_endpoints_are_absent_outside_the_standin_environment(open_kit):
    with open_kit(env="testnet") as kit:
        sess = kit.login()
        for path in ("/dev/inject_signal", "/dev/test_open"):
            response = sess.post(path, json={"op_id": "op-1"})
            assert response.status_code == 404 and response.json()["code"] == "not_found"


def test_unknown_paths_and_wrong_methods_are_404_not_found_for_authenticated_callers(sess):
    for method, path in (("GET", "/no/such/path"), ("POST", "/state"), ("DELETE", "/positions"), ("GET", "/ops/start")):
        response = sess.kit.request(method, path, token=sess.token)
        assert response.status_code == 404 and response.json()["code"] == "not_found"


# ---------------------------------------------------------------- 只读接口绝不改变状态

READ_PATHS = ["/health", "/state", "/positions", "/positions/external", "/orders", "/oplog", "/oplog?limit=3",
              "/alerts", "/config", "/operations/x", "/auth/me", "/watchlist", "/trades", "/stats", "/candles?symbol=BTCUSDT&tf=15m&limit=10"]


def test_reading_changes_nothing_in_the_ledger_the_model_state_or_the_positions(kit):
    sess = kit.login()
    kit.add_position(symbol="BTCUSDT", intent_id=1)
    pending = kit.add_pending_position()
    kit.add_order("DKA-1-1-o")
    kit.add_alert(AlertLevel.INFO, "test", "一条告警")
    kit.add_audit(action="test.audit")
    kit.publish(AccountSnapshotTaken(equity=D("1000"), available=D("900"), wallet=D("950"), at_ms=T0),
                PositionSnapshotTaken(positions=(position_info(),), server_time_ms=T0, at_ms=T0),
                OrdersSnapshotTaken(open_orders=(order_info(),), algo_orders=(algo_info(),), at_ms=T0))
    sess.post("/seat/acquire")
    before_dump = kit.dump()
    before_counts = kit.row_counts()
    model_version = kit.modelstate.current().version
    versions = {rec.id: rec.version for rec in kit.registry.all_active()}
    for _ in range(3):
        kit.clock.advance(40_000)  # 时间在走：会话活动时间会被记，但不能改任何业务数据
        for path in READ_PATHS:
            assert kit.get(path, token=sess.token).status_code in (200, 404, 501), path
        assert kit.get("/state", token=kit.local_token).status_code == 200
        for path in PLACEHOLDER_OPS[:4]:  # 仍是占位的操作入口：答 501，也不改任何东西
            assert sess.post(path).status_code == 501
    assert kit.dump() == before_dump
    assert {k: v for k, v in kit.row_counts().items()} == before_counts
    assert kit.modelstate.current().version == model_version
    assert {rec.id: rec.version for rec in kit.registry.all_active()} == versions
    assert kit.registry.by_id(pending.id).version == versions[pending.id]


def test_reads_made_with_an_expired_lease_do_not_clean_it_up_either(kit):
    sess = kit.login()
    sess.post("/seat/acquire")
    kit.clock.advance(120_000)  # 租约早已到期，但没有人去清理
    before = kit.dump()
    assert sess.get("/state").json()["seat"]["holder_session_id"] is None
    assert kit.dump() == before  # 读的时候按到期算，不写表、不记审计


# ---------------------------------------------------------------- 意外错误

def test_an_unexpected_error_is_a_500_without_internal_details(open_kit, monkeypatch):
    with open_kit(raise_server_exceptions=False) as kit:
        sess = kit.login()

        def boom():
            raise RuntimeError("secret internal detail")

        monkeypatch.setattr(kit.registry, "counts", boom)
        response = sess.get("/state")
        assert response.status_code == 500
        assert response.json()["code"] == "internal_error" and "secret internal detail" not in response.text


@pytest.mark.parametrize("path", ["/trades", "/stats", "/candles?symbol=BTCUSDT&tf=15m&limit=10"])
def test_m5_reads_are_implemented_not_501(sess, path):
    assert sess.get(path).status_code == 200


def test_candles_without_parameters_is_a_422(sess):
    assert sess.get("/candles").status_code == 422
