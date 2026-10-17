# BIT final oracle repair — verified offline

## Verdict
- Long `model2 external claim`: **production wiring defect**. LongTermDMRStrategy supplied LongTermPositionManager to PositionClaimManager, which requires `has_position()` and `save_position()` (TrailingStopManager interface). The engine swallowed AttributeError. Wire the existing trailing manager, with the same disabled-trailing fail-fast prerequisite used by short strategy.
- Long `restart`: **downstream symptom**, not a separate restart defect: failed claim never created state. Claim and subsequent reconstruction now pass.
- Short `model2 direct execute_signal`: **production safety gap**. `run_strategy` blocked auto-open, but direct `execute_signal` could open both from flat and after closing the opposite side. Add the same pre-open guards already present in long strategy, after successful close/reset. Model2 still permits managing/closing old positions; no forced full-return before close.
- Short `external claim` and `restart`: **fixture omission**. MARKET_PRICE inference calls real CCXT `fetch_ticker`; simulator lacked GET `/fapi/v1/ticker/24hr`. Add a synthetic raw ticker response, still parsed by real CCXT. No claim success stub, and no production claim algorithm changes.

## Red → green evidence
- Original oracle reproduction: long and short child exits 1 (`repro-long.log`, `repro-short.log`). Original assertion sets were 45/53, failed 2/3.
- After ticker fixture correction and bidirectional/flat regression additions, BEFORE production edits: long failed claim/restart; short failed four auto-open checks (`fixture-red/{long,short}/oracles.json`). Short claim/restart already passed, proving fixture-only cause there.
- Minimal production fixes then passed intermediate oracles (`repair-green/`).
- Final persisted harness staged a FRESH allowlisted snapshot, then exercised real engines, executors, position managers, trailing managers and CCXT parser/signing against synthetic raw exchange responses.

## Final command
```bash
cd <项目根目录>
python3 tests/offline/run.py --out /tmp/dmr-astra-handoff/bit-final/persistent-verified-2
```
Use a new `--out` directory on reruns. Existing evidence is not overwritten.

## Verified results
- Process exit: 0.
- Long: **100/100** deterministic checks; short: **112/112**; **212/212 total**. `failed=[]`, `unhit_rules=[]`, `harness_errors=0` for both, read from JSON rather than inferred from driver exit.
- Assertions cover both directions: failed reversal close keeps old state, failed open after close leaves flat state, retry success, trailing close failure/retry, short close-only failure/retry, model2 direct reversal closes without reopening, flat no-open, external claim and reconstructed state, model2/3 SHORT claim side/size/entry exactness. Added checks for swallowed engine failures every cycle. Restart tests suppress unrelated fresh signals, not reconciliation or state loading.
- Additional entrypoint log summaries (not merely exit codes):

| Test | Passed | Failed |
|---|---:|---:|
| test_model_integration | 36 | 0 |
| test_model_switching | 30 | 0 |
| test_switch_model1_to_model2 | 25 | 0 |
| test_switch_model2_to_model1 | 24 | 0 |
| test_bit_position_lifecycle | 10 | 0 |
| test_time_handling | 34 | 0 |
| test_strategy_time_integration | 20 | 0 |

Additional checks total: **179**, all passed.

## Safety and limitations
- Guard probe actually returned `kernel connect EPERM` and `production config read denied`. Failed open of production config did not read its contents. Seccomp blocks connect/sendto/sendmsg/sendmmsg in children. All CCXT fetches terminate in simulator; no fallback, network or real account requests.
- Never read/copied real config/config.py or real secrets. Only source allowlist copied, bit_config/period configs/logger config generated with dummy constants. Source feature-switch logic retained, model state generated only in sandbox.
- Uses installed project venv dependencies, not clean installation. Fetcher/executor constructors bypassed to inject exchange/data dependencies; this is real business-component regression, not live startup/account/exchange integration certification.
- Natural uninterrupted periods are synthetic smoke checks, not profitability verification. Historical matrix18 was NOT reclassified as asserted coverage. No new claim about live deployments, exchange timing/race behavior, or unsupported simulator endpoints.
- First fresh persistence run failed because synthetic logger config was missing; fixed preparation to generate logging-only fixture. Evidence retained in `persistent-verified/`; final successful clean run is `persistent-verified-2/`.
- Repository has no .git; git status unavailable. Original owned sources backed up before V4A patches under `repair-before/`.

## Modified artifacts
Production only:
- `bit/strategy/bit_long_term/strategy_engine.py`: correct claim-manager dependency plus missing prerequisite guard.
- `bit/strategy/bit_short_term/strategy_engine.py`: LONG/SHORT pre-open safety guards.

Persisted under `tests/offline/`: `prepare.py`, `run.py`, `oracles.py`, `drive_bit_offline.py`, `no_network.py`, `guard/sitecustomize.py`, this `REPORT.md`. No real configuration artifacts included.

Final source hashes (verified equal to exercised sandbox source):
- `bit/strategy/bit_long_term/strategy_engine.py` SHA256 `18f83bb17469c8b8e696f18f0dc089de5c44a89927667ce842df671032e7dcdc`
- `bit/strategy/bit_short_term/strategy_engine.py` SHA256 `b98f451b876dfd135cdff88630bb4644c1165db8e706b0ea863cc1db24f84cb1`

Evidence root: `/tmp/dmr-astra-handoff/bit-final/`; final results: `persistent-verified-2/results.json`, oracle details: `persistent-verified-2/runs/{long,short}/oracles.json`, full logs adjacent.
