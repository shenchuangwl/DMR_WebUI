"""ops.config_ops：修改单项配置（经操作记录去重，调用 ConfigStore.set，写操作日志）。

覆盖：返回结构与中文生效说明、操作记录与审计的顺序、同一操作号只执行一次（含值之后又被别人改过、并发）、
校验失败给出中文原因并记为被拒绝、跨字段校验（A、B 取值）、部署级开关、未知键、没有变化、内部错误、
上次没做完的操作号重做、不同类型占用的操作号、调用方被取消时操作照样做完。
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from dmr_kernel.core import configdefs as cd
from dmr_kernel.core.config import ConfigStore
from dmr_kernel.core.configdefs import APPLY_MODES, DEFINITIONS, ConfigError
from dmr_kernel.core.events import ConfigChanged, OperationProgress
from dmr_kernel.core.types import Environment, OperationStatus, OperationType
from dmr_kernel.ledger import repo_audit, repo_config
from dmr_kernel.ops.config_ops import ConfigOps
from dmr_kernel.ops.operations import Operations
from tests.support.ledger import START_MS

D = Decimal
RESULT_KEYS = {"key", "old", "new", "applies", "affects_existing"}


class Rig:
    def __init__(self, ledger, bus, clock, store: ConfigStore) -> None:
        self.ledger, self.bus, self.clock, self.store = ledger, bus, clock, store
        self.operations = Operations(ledger, clock, bus)
        self.config_ops = ConfigOps(self.operations, store)

    async def set(self, op_id: str, key: str, value, *, operator: str = "alice", session_id: int | None = 7):
        return await self.config_ops.set_config(op_id, key, value, operator=operator, session_id=session_id)

    async def audit(self) -> list:
        """从旧到新。"""
        return list(reversed(await self.ledger.read(repo_audit.recent)))

    async def audit_actions(self) -> list[str]:
        return [row.action for row in await self.audit()]

    async def op(self, op_id: str):
        return await self.operations.get(op_id)

    async def stored(self, key: str):
        return (await self.ledger.read(repo_config.get_item, key)).value


async def make_rig(ledger, bus, clock, *, deploy_switch: bool = False, env=Environment.STANDIN) -> Rig:
    store = ConfigStore(ledger, bus, clock, env, deploy_switch=deploy_switch)
    await store.load()
    return Rig(ledger, bus, clock, store)


@pytest.fixture
def rig_factory(ledger, bus, clock):
    async def make(**options) -> Rig:
        return await make_rig(ledger, bus, clock, **options)

    return make


# ====================================================================== 成功的修改


async def test_a_change_returns_key_old_new_and_when_it_takes_effect(rig_factory):
    rig = await rig_factory()
    result = await rig.set("op-1", "leverage", 3)
    assert result == {"key": "leverage", "old": 2, "new": 3, "applies": "下一个信号起生效（不影响已有持仓）",
                      "affects_existing": False}
    assert set(result) == RESULT_KEYS
    assert rig.store.get("leverage") == 3
    assert await rig.stored("leverage") == 3  # 落了盘


@pytest.mark.parametrize("key, value", [
    ("leverage", 4),                         # 下一个信号起生效
    ("stop_trigger_price_type", "MARK_PRICE"),  # 下一次挂止损起生效，且影响已有持仓
    ("takeover_symbols", ["BTCUSDT"]),       # 只影响之后新出现的仓
    ("timeframe_short", "5m"),               # 内核重启后生效
    ("seat_lease_s", 90),                    # 立即生效
    ("require_direction_match", False),      # 布尔
    ("margin_mode", "cross"),                # 选项
])
async def test_applies_and_affects_existing_come_from_the_definition(rig_factory, key, value):
    rig = await rig_factory()
    result = await rig.set("op-1", key, value)
    defn = DEFINITIONS[key]
    assert result["applies"] == APPLY_MODES[defn.apply]
    assert result["affects_existing"] is defn.affects_existing
    assert result["key"] == key and result["new"] == rig.store.get(key) and result["old"] != result["new"]
    assert any("一" <= ch <= "鿿" for ch in result["applies"])  # 中文说明


async def test_decimal_values_come_back_as_decimals_and_text_input_is_accepted(rig_factory):
    rig = await rig_factory()
    result = await rig.set("op-1", "trade_notional_usdt", "25.5")
    assert result["old"] == D("10") and result["new"] == D("25.5") and isinstance(result["new"], D)
    assert rig.store.get("trade_notional_usdt") == D("25.5")


async def test_symbol_lists_are_normalised_by_the_config_store(rig_factory):
    rig = await rig_factory()
    result = await rig.set("op-1", "takeover_symbols", "btcusdt, ethusdt btcusdt")
    assert result["old"] == [] and result["new"] == ["BTCUSDT", "ETHUSDT"]


async def test_a_change_is_recorded_as_a_config_operation(rig_factory, clock):
    rig = await rig_factory()
    clock.advance(1_500)
    result = await rig.set("op-1", "leverage", 3, operator="alice", session_id=7)
    record = await rig.op("op-1")
    assert record.op_type is OperationType.CONFIG and record.scope == "leverage"
    assert record.operator == "alice" and record.session_id == 7
    assert record.params == {"key": "leverage", "value": 3}
    assert record.status is OperationStatus.SUCCEEDED
    assert record.result == result  # 整数、布尔、文本的 JSON 与原值一样
    assert record.started_ms == record.ended_ms == START_MS + 1_500


async def test_a_decimal_result_is_stored_in_its_json_form(rig_factory):
    rig = await rig_factory()
    await rig.set("op-1", "trade_notional_usdt", D("25"))
    record = await rig.op("op-1")
    assert record.params == {"key": "trade_notional_usdt", "value": "25"}
    assert record.result["old"] == "10" and record.result["new"] == "25"


async def test_the_command_line_has_no_session(rig_factory):
    rig = await rig_factory()
    await rig.set("op-1", "leverage", 3, operator="cli", session_id=None)
    record = await rig.op("op-1")
    assert record.operator == "cli" and record.session_id is None
    assert {row.operator for row in await rig.audit()} == {"cli"}


async def test_the_operation_log_shows_begin_the_change_and_finish_in_order(rig_factory):
    rig = await rig_factory()
    await rig.set("op-1", "leverage", 3)
    rows = await rig.audit()
    assert [row.action for row in rows] == ["operation.begin", "config.set", "operation.finish"]
    assert {row.operator for row in rows} == {"alice"}
    _, changed, ended = rows
    assert changed.target == "leverage" and changed.before == {"value": 2} and changed.after == {"value": 3}
    assert ended.result == "succeeded" and ended.after["status"] == "succeeded"
    assert ended.after["result"]["new"] == 3


async def test_a_change_publishes_one_config_changed_and_the_operation_progress(rig_factory, event_log):
    log = event_log(ConfigChanged, OperationProgress)
    rig = await rig_factory()
    await rig.set("op-1", "leverage", 3)
    events = await log.settle()
    assert [type(e).__name__ for e in events if isinstance(e, ConfigChanged)] == ["ConfigChanged"]
    assert [e.key for e in events if isinstance(e, ConfigChanged)] == ["leverage"]
    assert [(e.op_id, e.status) for e in events if isinstance(e, OperationProgress)] == [
        ("op-1", "running"), ("op-1", "succeeded")]


async def test_setting_the_value_it_already_has_succeeds_without_touching_the_config(rig_factory, event_log):
    log = event_log(ConfigChanged)
    rig = await rig_factory()
    result = await rig.set("op-1", "leverage", 2)
    assert result["old"] == result["new"] == 2
    assert (await rig.op("op-1")).status is OperationStatus.SUCCEEDED
    assert await rig.audit_actions() == ["operation.begin", "operation.finish"]  # 没有 config.set
    assert await log.settle() == []


# ====================================================================== 同一操作号只执行一次


async def test_a_repeated_op_id_returns_the_first_result_and_executes_nothing(rig_factory, event_log):
    log = event_log(ConfigChanged)
    rig = await rig_factory()
    first = await rig.set("op-1", "leverage", 3)
    audit_before = await rig.audit()
    second = await rig.set("op-1", "leverage", 5)  # 同一个操作号、不同的值：第一次的结果为准
    assert second == first and second["new"] == 3
    assert rig.store.get("leverage") == 3 and await rig.stored("leverage") == 3
    assert await rig.audit() == audit_before
    assert len(await log.settle()) == 1


async def test_a_replay_is_not_undone_by_what_happened_after_the_first_run(rig_factory):
    rig = await rig_factory()
    first = await rig.set("op-1", "leverage", 3)
    await rig.set("op-2", "leverage", 4)
    replay = await rig.set("op-1", "leverage", 3)
    assert replay == first == {"key": "leverage", "old": 2, "new": 3, "applies": first["applies"],
                               "affects_existing": False}
    assert rig.store.get("leverage") == 4  # 重放没有把值改回 3


async def test_a_replay_has_the_same_python_types_as_the_first_answer(rig_factory):
    rig = await rig_factory()
    first = await rig.set("op-1", "trade_notional_usdt", "25")
    replay = await rig.set("op-1", "trade_notional_usdt", "25")
    assert replay == first
    assert isinstance(replay["old"], D) and isinstance(replay["new"], D)


async def test_a_replay_survives_a_new_config_store_and_ops_instance(rig_factory, ledger, bus, clock):
    rig = await rig_factory()
    first = await rig.set("op-1", "leverage", 3)
    fresh = await make_rig(ledger, bus, clock)  # 内核重启：对象全是新的，账本还在
    assert await fresh.set("op-1", "leverage", 9) == first
    assert fresh.store.get("leverage") == 3


async def test_concurrent_submissions_of_one_op_id_execute_once(rig_factory, event_log):
    log = event_log(ConfigChanged)
    rig = await rig_factory()
    results = await asyncio.gather(*(rig.set("op-1", "leverage", 3 + i, operator=f"u{i}") for i in range(6)))
    assert all(result == results[0] for result in results)
    assert rig.store.get("leverage") == results[0]["new"]
    assert (await rig.audit_actions()).count("config.set") == 1
    assert len(await log.settle()) == 1


async def test_different_op_ids_change_different_items_independently(rig_factory):
    rig = await rig_factory()
    changes = [("leverage", 5), ("seat_lease_s", 120), ("cooldown_bars", 8), ("intent_ttl_s", 90)]
    await asyncio.gather(*(rig.set(f"op-{i}", key, value) for i, (key, value) in enumerate(changes)))
    assert all(rig.store.get(key) == value for key, value in changes)
    assert (await rig.audit_actions()).count("config.set") == 4


# ====================================================================== 校验失败：明确的中文原因

BAD_VALUES = [
    ("leverage", 21, "杠杆"),
    ("leverage", 0, "杠杆"),
    ("leverage", True, "需要整数"),
    ("leverage", "abc", "需要整数"),
    ("leverage", 2.5, "需要整数"),
    ("leverage", None, "不能为空"),
    ("margin_mode", "weird", "只能取"),
    ("trade_notional_usdt", "0.5", "不能小于"),
    ("trade_notional_usdt", "10001", "不能大于"),
    ("trade_notional_usdt", "abc", "无法解析"),
    ("trade_notional_usdt", float("nan"), "无法解析"),
    ("trade_notional_usdt", "Infinity", "无法解析"),
    ("correction_signal", True, "只能为关"),
    ("takeover_symbols", ["bad symbol!"], "合约代码格式不对"),
    ("require_direction_match", "maybe", "需要是或否"),
]


@pytest.mark.parametrize("key, value, fragment", BAD_VALUES)
async def test_a_bad_value_is_rejected_with_a_chinese_reason_and_changes_nothing(rig_factory, key, value, fragment):
    rig = await rig_factory()
    before = rig.store.snapshot()
    with pytest.raises(ConfigError, match=fragment) as caught:
        await rig.set("op-1", key, value)
    message = str(caught.value)
    assert any("一" <= ch <= "鿿" for ch in message)
    assert rig.store.snapshot() == before
    record = await rig.op("op-1")
    assert record.status is OperationStatus.REJECTED and record.result == {"message": message}
    assert record.scope == key and record.operator == "alice"
    rows = await rig.audit()
    assert [row.action for row in rows] == ["operation.begin", "operation.finish"]  # 没有 config.set
    assert rows[-1].result == "rejected"


async def test_a_rejected_operation_replays_as_the_same_rejection(rig_factory):
    rig = await rig_factory()
    with pytest.raises(ConfigError) as first:
        await rig.set("op-1", "leverage", 99)
    audit_before = await rig.audit()
    for value in (99, 3):  # 第二次给了合法的值，也不重新执行：第一次的结果为准
        with pytest.raises(ConfigError) as again:
            await rig.set("op-1", "leverage", value)
        assert str(again.value) == str(first.value)
    assert rig.store.get("leverage") == 2
    assert await rig.audit() == audit_before


async def test_a_non_finite_float_is_recorded_safely(rig_factory, raw):
    """HTTP 的 JSON 解析会放行 NaN 字样：操作记录里要能存下，不能让写事务崩掉。"""
    rig = await rig_factory()
    with pytest.raises(ConfigError):
        await rig.set("op-1", "trade_notional_usdt", float("nan"))
    record = await rig.op("op-1")
    assert record.params == {"key": "trade_notional_usdt", "value": "nan"}
    assert raw.query("SELECT COUNT(*) FROM operation") == [(1,)]


async def test_a_value_that_is_not_a_plain_json_type_is_recorded_as_text(rig_factory):
    rig = await rig_factory()
    with pytest.raises(ConfigError):
        await rig.set("op-1", "leverage", {"a": (1, 2), "b": object})
    params = (await rig.op("op-1")).params
    assert params["key"] == "leverage" and params["value"]["a"] == [1, 2]
    assert isinstance(params["value"]["b"], str)


# ---------------------------------------------------------------- 跨字段：A、B 取值校验生效

CROSS_FIELD = [
    ("init_stop_min_pct", "6", "A < B"),            # A 不小于 B（B=5）
    ("init_stop_max_pct", "0.5", "A < B"),          # B 不大于 A（A=1）
    ("leverage", 20, "强平距离"),                   # 20 倍杠杆下 B=5 超过估算的强平距离
    ("takeover_min_notional", "300", "下限 < 上限"),
    ("takeover_max_notional", "5", "下限 < 上限"),
    ("max_symbols", 5, "降级时的上限不能大于"),
    ("timeframe_short", "4h", "短周期必须小于长周期"),
]


@pytest.mark.parametrize("key, value, fragment", CROSS_FIELD)
async def test_a_value_that_conflicts_with_other_items_is_rejected(rig_factory, key, value, fragment):
    rig = await rig_factory()
    before = rig.store.snapshot()
    with pytest.raises(ConfigError, match=fragment) as caught:
        await rig.set("op-1", key, value)
    assert "与其它配置冲突" in str(caught.value)
    assert rig.store.snapshot() == before
    assert (await rig.op("op-1")).status is OperationStatus.REJECTED


async def test_the_stop_band_checks_use_the_values_after_earlier_changes(rig_factory):
    rig = await rig_factory()
    await rig.set("op-1", "init_stop_max_pct", "8")
    await rig.set("op-2", "init_stop_min_pct", "6")  # 先把 B 抬到 8，A=6 才成立
    with pytest.raises(ConfigError, match="A < B"):
        await rig.set("op-3", "init_stop_max_pct", "6")  # B 回到 6，不再大于 A
    assert (rig.store.get("init_stop_min_pct"), rig.store.get("init_stop_max_pct")) == (D("6"), D("8"))


# ---------------------------------------------------------------- 部署级开关

DEPLOY_KEYS = [(key, value) for key, value in [
    ("m3_form", "ai_proposal_sealed"), ("m3_ai_tier", "shadow"), ("m3_exec_tier", "paper"),
    ("m3_master_switch", True), ("m3_max_notional_usdt", "100"), ("m3_max_symbols", 3)]]


def test_the_deploy_switch_list_covers_every_item_that_needs_it():
    assert {key for key, _ in DEPLOY_KEYS} == {k for k, d in DEFINITIONS.items() if d.requires_deploy_switch}


@pytest.mark.parametrize("key, value", DEPLOY_KEYS)
async def test_model3_tiers_are_refused_while_the_deploy_switch_is_off(rig_factory, key, value):
    rig = await rig_factory(deploy_switch=False)
    before = rig.store.snapshot()
    with pytest.raises(ConfigError, match="部署级开关"):
        await rig.set("op-1", key, value)
    assert rig.store.snapshot() == before
    assert (await rig.op("op-1")).status is OperationStatus.REJECTED
    assert (await rig.audit_actions()).count("config.set") == 0


@pytest.mark.parametrize("key", [key for key, _ in DEPLOY_KEYS])
async def test_even_the_current_value_is_refused_while_the_deploy_switch_is_off(rig_factory, key):
    rig = await rig_factory(deploy_switch=False)
    with pytest.raises(ConfigError, match="部署级开关"):
        await rig.set("op-1", key, rig.store.get(key))


@pytest.mark.parametrize("key, value", DEPLOY_KEYS)
async def test_model3_tiers_can_be_changed_when_the_deploy_switch_is_on(rig_factory, key, value):
    rig = await rig_factory(deploy_switch=True)
    result = await rig.set("op-1", key, value)
    assert result["key"] == key and result["new"] == rig.store.get(key)
    assert (await rig.op("op-1")).status is OperationStatus.SUCCEEDED


async def test_the_deploy_switch_does_not_stand_in_the_way_of_ordinary_items(rig_factory):
    rig = await rig_factory(deploy_switch=False)
    assert (await rig.set("op-1", "leverage", 3))["new"] == 3


# ====================================================================== 未知键与参数


async def test_an_unknown_key_is_refused_before_anything_is_recorded(rig_factory, raw):
    rig = await rig_factory()
    with pytest.raises(ConfigError, match="未知的配置项"):
        await rig.set("op-1", "no_such_item", 1)
    assert raw.query("SELECT COUNT(*) FROM operation") == [(0,)]
    assert await rig.audit() == []
    # 操作号没有被占用：改用正确的键照样能执行
    assert (await rig.set("op-1", "leverage", 3))["new"] == 3


async def test_a_very_long_unknown_key_is_not_echoed_in_full(rig_factory):
    rig = await rig_factory()
    with pytest.raises(ConfigError) as caught:
        await rig.set("op-1", "x" * 5000, 1)
    assert len(str(caught.value)) < 200


@pytest.mark.parametrize("op_id", ["", "has space", "x" * 65, None])
async def test_a_malformed_op_id_is_a_programming_error(rig_factory, raw, op_id):
    rig = await rig_factory()
    with pytest.raises(ValueError, match="操作号"):
        await rig.set(op_id, "leverage", 3)
    assert raw.query("SELECT COUNT(*) FROM operation") == [(0,)]


@pytest.mark.parametrize("operator", ["", "   ", None])
async def test_a_change_must_name_who_made_it(rig_factory, raw, operator):
    rig = await rig_factory()
    with pytest.raises(ValueError, match="操作人"):
        await rig.set("op-1", "leverage", 3, operator=operator)
    assert rig.store.get("leverage") == 2
    assert raw.query("SELECT COUNT(*) FROM operation") == [(0,)]


@pytest.mark.parametrize("key", [None, 5, ["leverage"]])
async def test_the_key_must_be_text(rig_factory, key):
    rig = await rig_factory()
    with pytest.raises(ValueError, match="配置项"):
        await rig.set("op-1", key, 3)


# ====================================================================== 操作号被别的操作占着

async def test_an_op_id_already_used_by_another_kind_of_operation_is_refused(rig_factory):
    rig = await rig_factory()
    stopped, _ = await rig.operations.begin("op-1", OperationType.STOP, operator="alice", session_id=7)
    with pytest.raises(ConfigError, match="另一种操作"):
        await rig.set("op-1", "leverage", 3)
    assert rig.store.get("leverage") == 2
    assert await rig.op("op-1") == stopped  # 别人的记录原封不动


async def test_a_previous_attempt_that_did_not_finish_is_carried_out_again_with_its_own_parameters(rig_factory):
    """内核在登记之后、做完之前退出：同一个操作号再来，按第一次登记的参数做完（不是这一次带来的参数）。"""
    rig = await rig_factory()
    await rig.operations.begin("op-1", OperationType.CONFIG, operator="alice", session_id=7, scope="leverage",
                               params={"key": "leverage", "value": 3})
    assert rig.store.get("leverage") == 2
    result = await rig.set("op-1", "leverage", 9, operator="alice")
    assert result["new"] == 3 and rig.store.get("leverage") == 3
    record = await rig.op("op-1")
    assert record.status is OperationStatus.SUCCEEDED and record.result == result
    assert (await rig.audit_actions()).count("operation.begin") == 1  # 登记只记一次
    assert (await rig.audit_actions()).count("operation.finish") == 1


async def test_an_unfinished_attempt_whose_value_is_now_invalid_ends_as_rejected(rig_factory):
    rig = await rig_factory()
    await rig.operations.begin("op-1", OperationType.CONFIG, operator="alice", session_id=7, scope="leverage",
                               params={"key": "leverage", "value": 99})
    with pytest.raises(ConfigError, match="杠杆"):
        await rig.set("op-1", "leverage", 3)
    assert (await rig.op("op-1")).status is OperationStatus.REJECTED
    assert rig.store.get("leverage") == 2


# ====================================================================== 内部错误

async def test_an_internal_failure_is_recorded_and_not_swallowed(rig_factory, monkeypatch):
    rig = await rig_factory()

    async def broken(*args, **kwargs):
        raise RuntimeError("磁盘满了 /secret/path")

    monkeypatch.setattr(rig.store, "set", broken)
    with pytest.raises(RuntimeError):
        await rig.set("op-1", "leverage", 3)
    record = await rig.op("op-1")
    assert record.status is OperationStatus.FAILED
    assert "RuntimeError" in record.result["message"] and "/secret/path" not in record.result["message"]
    # 同一个操作号再来：不重做，明确告诉调用方换新的操作号
    monkeypatch.undo()
    with pytest.raises(ConfigError, match="换一个新的操作号"):
        await rig.set("op-1", "leverage", 3)
    assert rig.store.get("leverage") == 2
    assert (await rig.set("op-2", "leverage", 3))["new"] == 3


# ====================================================================== 调用方被取消

@pytest.mark.timeout_s(20)
async def test_a_cancelled_caller_does_not_leave_a_half_done_operation(rig_factory):
    rig = await rig_factory()
    entered, release = asyncio.Event(), asyncio.Event()
    real_set = rig.store.set

    async def slow_set(key, value, *, operator):
        entered.set()
        await release.wait()
        return await real_set(key, value, operator=operator)

    rig.store.set = slow_set  # type: ignore[method-assign]
    caller = asyncio.ensure_future(rig.set("op-1", "leverage", 3))
    await asyncio.wait_for(entered.wait(), 5)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    release.set()
    for _ in range(200):  # 操作在后台做完：最多等 2 秒
        record = await rig.op("op-1")
        if record.status is not OperationStatus.RUNNING:
            break
        await asyncio.sleep(0.01)
    assert record.status is OperationStatus.SUCCEEDED and rig.store.get("leverage") == 3
    assert (await rig.audit_actions()) == ["operation.begin", "config.set", "operation.finish"]
    # 重放拿到第一次的结果
    assert (await rig.set("op-1", "leverage", 3))["new"] == 3


@pytest.mark.timeout_s(20)
async def test_a_cancelled_caller_whose_change_is_then_rejected_leaves_no_unretrieved_error(rig_factory, caplog):
    rig = await rig_factory()
    entered, release = asyncio.Event(), asyncio.Event()
    real_set = rig.store.set

    async def slow_set(key, value, *, operator):
        entered.set()
        await release.wait()
        return await real_set(key, value, operator=operator)

    rig.store.set = slow_set  # type: ignore[method-assign]
    caller = asyncio.ensure_future(rig.set("op-1", "leverage", 99))
    await asyncio.wait_for(entered.wait(), 5)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    release.set()
    for _ in range(200):
        record = await rig.op("op-1")
        if record.status is not OperationStatus.RUNNING:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    assert record.status is OperationStatus.REJECTED
    assert not [r for r in caplog.records if r.levelname == "ERROR"]
