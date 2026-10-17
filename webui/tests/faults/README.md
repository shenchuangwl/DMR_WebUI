# 故障注入验收（T0.27）

报告 4.11「故障注入的做法和通过标准」列的九种故障，全部在进程内替身交易所上跑通（不开套接字、禁网包装下运行）：

```
./run_tests.sh kernel tests/faults
```

## 怎么跑的

- `kit.py` 的 `Rig` 是一次「进程生命」：`tests/support/kernel.py` 装配完整内核（真实重启恢复、真实协调者/保护单管理/对账/入账），
  不起 uvicorn、不建 APScheduler。`Rig.step()` 把共享的 `ManualClock` 推进 1 秒，替身跑一轮（数据流推送、条件单触发、断线窗口
  到期），再按生产调度器的间隔调到点的作业（`stream_liveness` 5 秒、`reconcile_positions` 10 秒、`reconcile_orders`/`backfill_trades`/
  `guard_periodic` 60 秒、`resolve_unknown`/`expire_intents` 5 秒、`protection_tick` 2 秒）。**30 秒、90 秒这些标准都按这个注入时钟量**。
- 故障用替身的故障开关打开（`docs/设计约定.md` 7.5，`rig.standin.disp.switches.update(...)`）；「手动」动作走替身的用户接口
  （`/dev/manual/order`、`/dev/manual/cancel`、`/dev/price_override`，用户令牌）。
- 替身的「扣住不答」是真的扣住（真实时间）。`FaultStandin` 给内核的传输套 `CappedTransport`，把请求时限截到 0.2 秒，效果等同客户端超时。
- 「进程被杀」= `abandon`（不走关停、不撤任何单）或进程内崩溃点；「重启」= 同一账本、同一替身重新装配并跑真实恢复。
- 策略仓由测试专用入口（`source=TEST_ENTRY` 的 OPEN 意图）经协调者造；接管仓 = 用户手动开仓 → 持仓对账登记为待确认 → `begin_takeover`。
- 移动止损：`trail_up` 在开仓那根 K 线之后放 6 根逐根走高的已收盘 K 线、拉高现价、调 `bar_close_short`（生产路径
  `CandlesReady` → `trailing`，缺省 gap 模式先撤旧后挂新）；`watch_gap` 一秒一秒推进，量这一侧没有系统止损的最长连续时长。

## 通用判据（4.11 每种注入后都要满足）

| 编号 | 4.11 的标准 | 帮手 |
|---|---|---|
| G1 | 持仓登记与交易所一致 | `Rig.assert_registry_matches_exchange`（`assert_general` 含） |
| G2 | 同一个交易意图没有第二张订单 | `Rig.assert_one_order_per_intent`（按客户编号里的意图号，订单与条件单分开数；`assert_general` 含） |
| G3 | 从成交或接管登记到止损存在 ≤ 30 秒 | `run_until(rig.has_stop, limit_s=30)` + `fill_time_ms` |
| G4 | 移动止损更新的无保护间隙 ≤ `stop_gap_max_s`（缺省 30） | `trail_up` + `watch_gap` |
| G5 | 重启后 30 秒内给已登记而无止损的仓补上 | `restart()` 后 `run_until(has_stop, 30)` |
| G6 | 手动撤掉系统止损后 90 秒内重新存在 | `run_until(has_stop, 90)`，且是新的一张 |
| S1 | 「止损建单失败」：策略仓在 `protect_retry_window_s + close_one_timeout_s`（30 + 15）秒内被市价平掉 | |
| S2 | 「止损建单失败」：接管仓在 `protect_retry_window_s` 秒内转无保护、置抑制、发最高级告警，之后持续重试 | |

另外每个文件都在注入后再推进一段（通常 60–70 秒，让每个作业至少再跑一轮）后复查 G1、G2，并检查系统止损始终最多一张。

## 九种故障

| # | 故障 | 文件 | 做法 | 断言 | 标准 |
|---|---|---|---|---|---|
| 1 | 下单超时 | `test_order_timeout.py` | `order_timeout`：`accept_then_hang`（交易所照做、应答扣住）/ `drop`（没受理），`scope` 市价单或条件单；另在移动止损更新时让新止损的应答丢失 | 开仓单从不重发，回查到成交就登记并保护、确认没受理就结束；止损应答丢失时回查认领、不挂第二张，没受理就重挂；移动止损间隙 ≤ 30 秒（`drop` 实测约 23 秒） | G1 G2 G3 G4 |
| 2 | 止损建单失败 | `test_algo_create_fail.py` | `algo_create_fail`：`reject`（400 −2010）/ `unknown`（503），一直失败或只失败一两次 | 策略仓 45 秒内被市价平掉；接管仓 30 秒内转无保护 + `UNPROTECTED` 抑制 + 最高级 `protect_unprotected` 告警，之后持续重试、不平用户的仓，故障消失后挂上并解除抑制；只失败一次时窗口内挂上、不平仓；移动止损时新单与恢复旧单都被拒，间隙 ≤ 30 秒、仓不平 | S1 S2 G1 G2 G3 G4 |
| 3 | 持仓查询失败 | `test_position_query_fail.py` | `position_query_fail`：`error`（500）/ `hang`（扣住） | 开仓前读不到持仓就放弃（交易所无单）；读不到期间登记簿不变、止损一直在、不发平仓单；期间用户手动减仓，恢复后登记簿跟上 | G1 G2 |
| 4 | 进程被杀后重启 | `test_kill_restart.py` | 崩溃点（开仓单发出后、首个止损之前、平仓成交后清理之前）或任意时刻 `abandon`，停机 20 秒后重启 | 开仓单不重发、回查到成交并保护；无止损的仓重启后 30 秒内补上；已有止损的认领、不补第二张；平仓后残留止损被撤、登记为已平 | G1 G2 G5 |
| 5 | 部分成交 | `test_partial_fill.py` | `partial_fill`：先成交一部分，余量 `later`（稍后成交）/ `expire`（作废） | 开仓：从第一笔成交到止损 ≤ 30 秒，余量成交后登记簿跟上，作废时按实际成交量登记；平仓：余量成交后归零并清理止损，作废时剩下的仓仍登记、仍有止损；余量不另发单 | G1 G2 G3 |
| 6 | 停机期间止损触发 | `test_stop_triggered_while_down.py` | 内核被杀，停机期间 `price_override` 让价格越过止损，替身触发平仓，再重启 | 重启后记为止损出场的已平；交易所和登记簿都没有持仓；没有残留系统条件单；不补止损、不重复平仓；策略仓、接管仓各一例 | G1 G2 |
| 7 | 数据流断线 | `test_stream_drop.py` | `stream_drop`：`close`（断开并拒绝重连）/ `silent`（连着但不发帧） | 断线期间开的仓从成交到止损 ≤ 30 秒（持仓对账发现系统成交没入账时立即补拉）；断线期间手动减仓、手动撤止损，靠轮询跟上（登记簿一致、止损 90 秒内回来）；断线结束后数据流恢复 | G1 G2 G3 G6 |
| 8 | 限频被拒 | `test_rate_limit.py` | `rate_limit`：`http` 429/418，`scope` reads/writes/all | 开仓单被拒就是确定没下：意图结束、不重发、无残留，退避过去后照常开仓并保护；接管仓的第一张止损被拒，30 秒内重试挂上；读被拒后适配层对非保护类读退避，期间接管的仓仍在 30 秒内有止损（现价改用持仓里的标记价）；全部请求被拒期间不清登记、不撤不补止损；移动止损时撤旧单被拒，旧止损留着、无间隙 | G1 G2 G3 G4 |
| 9 | 手动撤掉系统的止损 | `test_manual_stop_cancel.py` | `/dev/manual/cancel`，替身推送条件单事件（`push_algo_events`）开/关两种；策略仓、接管仓；连撤三次；停机期间撤 | 90 秒内重新存在（新编号、原止损价；推送打开时 5 秒内）；撤掉不等于平仓、不发最高级告警；始终最多一张；停机期间撤掉的，重启后 30 秒内补上 | G1 G2 G5 G6 |

`test_baseline.py` 是对照组：不注入任何故障时止损立即存在、一分钟内没有误动作，移动止损上移且间隙为 0（说明帮手本身量得对）。

## 这一轮发现并修掉的缺陷（各有复现用例）

- **数据流断线时新开的仓没有止损**（`core/reconcile.py`）：开仓单 REST 应答已 FILLED、意图完成，但成交明细只能靠数据流或
  60 秒一次的补拉入账；持仓对账看到「交易所有、登记簿没有，且系统订单已成交而成交没入账」时只是「先不判」，于是 30 秒内
  登记簿没有这个仓、也就没有止损。修法：这种情况下先补拉该合约的成交、等事件总线交给登记簿，再复核（与数量不一致时的处理一致）。
  复现：`test_stream_drop.py::test_reconcile_backfills_a_filled_system_open_whose_fills_the_stream_never_delivered`。
- **限频退避期间接管仓挂不上止损**（`core/protection.py`）：429/418 之后适配层对非保护类读（含行情）退避到下一分钟甚至 120 秒，
  接管仓算止损要读现价，结果「读不到现价，暂不挂单」直到退避结束，30 秒内转成无保护。修法：行情读因限频（错误码 −1003）未知时，
  改读持仓（保护类读）里的标记价作为现价。复现：`test_rate_limit.py` 的两个接管仓用例。
- （上一轮）**止损建单结果未知时重试窗口用完仍不下结论**（`core/protection.py` 的 `_verdict_overdue`）：建单应答未知、协调者按签名
  时间窗回查要十几秒，窗口快用完时按「仍失败」下结论。由 `test_algo_create_fail.py` 的 `unknown` 两例覆盖。
