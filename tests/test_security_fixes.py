"""Regression tests for the pre-release security fixes.

Run with the project venv, no network and no exchange credentials required:

    ./venv/bin/python tests/test_security_fixes.py

Each test pins a specific defect found in the pre-upload audit so it cannot
silently come back.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

RESULTS = []


def check(name, condition):
    RESULTS.append((name, bool(condition)))
    print(("  PASS  " if condition else "  FAIL  ") + name)
    return bool(condition)


def test_seccomp_guard_survives_optimize():
    """no_network.py must block connects even under python -O."""
    import socket
    import threading
    srv = socket.socket()
    srv.bind(('127.0.0.1', 0))
    srv.listen(5)
    port = srv.getsockname()[1]
    threading.Thread(target=lambda: srv.accept(), daemon=True).start()
    probe = (
        "import socket\ns=socket.socket()\n"
        "try:\n s.connect(('127.0.0.1',%d));print('CONNECTED')\n"
        "except PermissionError:print('BLOCKED')\n" % port
    )
    guard = str(ROOT / 'tests' / 'offline' / 'no_network.py')
    for flags, label in (([], '普通模式'), (['-O'], '-O 模式')):
        out = subprocess.run([sys.executable] + flags + [guard, sys.executable, '-c', probe],
                             capture_output=True, text=True, timeout=30)
        check('禁网守卫在%s下仍然阻断连接' % label, out.stdout.strip() == 'BLOCKED')
    srv.close()


def test_atomic_state():
    from common.atomic_state import (StateCorruptError, atomic_write_json,
                                     read_json, single_instance, state_path)
    p = state_path('regression_atomic.json')
    p.unlink(missing_ok=True)
    atomic_write_json(p, {'a': 1})
    check('状态文件权限为 0600', oct(os.stat(p).st_mode)[-3:] == '600')
    p.write_text('{"broken', encoding='utf-8')
    try:
        read_json(p, {})
        check('损坏状态文件必须抛错而非静默返回空', False)
    except StateCorruptError:
        check('损坏状态文件必须抛错而非静默返回空', True)
    code = ("import sys;sys.path.insert(0,%r)\n"
            "from common.atomic_state import single_instance\n"
            "try:\n    single_instance('regression_lock').__enter__();print('GOT')\n"
            "except RuntimeError:print('REFUSED')\n" % str(ROOT))
    with single_instance('regression_lock'):
        r = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
        check('单实例锁拒绝第二个进程', r.stdout.strip() == 'REFUSED')
    for f in p.parent.glob('*regression_*'):
        f.unlink(missing_ok=True)


def test_risk_gate_blocks():
    from common.risk_state import RiskLedger, evaluate, resolve_limits
    led = RiskLedger('regression_risk')
    led.path.unlink(missing_ok=True)
    limits = resolve_limits({'max_drawdown_pct': 0.10, 'max_daily_trades': 25})
    led.note_equity(1000.0)
    check('风控初始放行开仓', bool(evaluate(led.snapshot(), limits, 1000.0)))
    led.record_trade(realized_pnl=-60.0, equity=1000.0)
    check('日亏超限拒绝开仓', not evaluate(led.snapshot(), limits, 1000.0))
    check('日亏超限仍允许平仓', bool(evaluate(led.snapshot(), limits, 1000.0, intent='close')))
    check('账户权益不可得时拒绝开仓',
          not evaluate({'daily_pnl': 0, 'total_pnl': 0, 'daily_trades': 0}, limits, None))
    check('风控计数器跨进程持久化', abs(RiskLedger('regression_risk').snapshot()['daily_pnl'] + 60.0) < 1e-9)
    led.path.unlink(missing_ok=True)


def test_no_infinite_risk_thresholds():
    """No risk manager may hard-code -inf thresholds again."""
    offenders = []
    for path in ROOT.rglob('risk_manager.py'):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith(('A_PY/', 'A_TEST/', 'bit/B_PY/', 'bit/B_TEST/')):
            continue
        if "float('inf')" in path.read_text(encoding='utf-8'):
            offenders.append(rel)
    check('没有风控模块把阈值写死为 -inf（%s）' % (offenders or '无'), not offenders)


def test_realized_pnl_math():
    from common.pnl import extract_fill, realized_pnl
    check('做多亏损计算正确', abs(realized_pnl('long', 100, 90, 2) + 20.0) < 1e-9)
    check('做空盈利计算正确', abs(realized_pnl('short', 100, 90, 2) - 20.0) < 1e-9)
    check('数据缺失返回 None 而非 0', realized_pnl('long', 0, 90, 2) is None)
    check('优先采用交易所成交均价', extract_fill({'average': 101.5, 'filled': 3}) == (101.5, 3.0))


def test_timezone_alignment():
    import pandas as pd
    from common.timeutil import index_to_naive_utc, to_naive_utc
    check('tz-aware 时间被换算而非丢弃偏移',
          str(to_naive_utc('2026-09-17T20:00:00+08:00')) == '2026-09-17 12:00:00')
    idx = pd.DatetimeIndex(['2026-09-17 12:00:00'])
    check('naive UTC K线索引不被平移',
          str(index_to_naive_utc(idx)[0]) == '2026-09-17 12:00:00')
    for name in ('trailing_stop_manager.py', 'trailing_take_profit_manager.py'):
        text = (ROOT / 'common' / name).read_text(encoding='utf-8')
        check('%s 不再把K线平移到 Asia/Singapore' % name, 'Asia/Singapore' not in text)


def test_template_path_traversal():
    from config.config_templates import _template_path
    for bad in ('../escaped', '/tmp/evil', 'a/b', '..'):
        try:
            _template_path(bad)
            check('拒绝越界模板名 %r' % bad, False)
        except ValueError:
            check('拒绝越界模板名 %r' % bad, True)
    check('接受合法模板名', _template_path('ok_name').endswith('ok_name.py'))


def test_stop_order_create_before_cancel():
    """A failed new stop order must leave the old one in place."""
    import tempfile as tf
    from common.trailing_stop_manager import TrailingStopManager
    m = TrailingStopManager('BTC/USDT', '15m', data_dir=tf.mkdtemp(), exchange=object())
    m._save_position_data = lambda: None
    m.position_data = {'binance_stop_order_id': 'OLD', 'current_stop_loss': 100.0,
                       'position_amount': 1.0}
    seq = []
    m._create_binance_stop_order = lambda *a, **k: (seq.append('create'), None)[1]
    m._cancel_binance_stop_order = lambda oid: (seq.append('cancel'), True)[1]
    m._update_binance_stop_order('long', 95.0, 1.0)
    check('新止损单创建失败时不撤销旧单', 'cancel' not in seq)
    check('新止损单创建失败时保留旧单ID', m.position_data['binance_stop_order_id'] == 'OLD')
    seq.clear()
    m._create_binance_stop_order = lambda *a, **k: (seq.append('create'), 'NEW')[1]
    m._update_binance_stop_order('long', 95.0, 1.0)
    check('顺序为先创建后撤销', seq.index('create') < seq.index('cancel'))


def test_model_state_fails_safe():
    import importlib
    import bit.config.feature_switches as fs
    state = fs.MODEL_STATE_FILE
    backup = state.read_text(encoding='utf-8') if state.exists() else None
    try:
        state.write_text('{"truncated', encoding='utf-8')
        importlib.reload(fs)
        check('运行状态损坏时禁止自动开仓',
              fs.FEATURE_SWITCHES['order_execution']['auto_open'] is False)
        check('运行状态损坏时仍允许自动平仓',
              fs.FEATURE_SWITCHES['order_execution']['auto_close'] is True)
        state.write_text(json.dumps({'running_model': 'ORIGINAL'}), encoding='utf-8')
        importlib.reload(fs)
        check('字段缺失时 auto_open 默认 False',
              fs.FEATURE_SWITCHES['order_execution']['auto_open'] is False)
    finally:
        if backup is not None:
            state.write_text(backup, encoding='utf-8')
        else:
            state.unlink(missing_ok=True)
        importlib.reload(fs)


def test_env_loader():
    from utils.env_loader import load_env, parse_env
    v = parse_env('export A="x y"\n# c\nB=z # t\n')
    check('.env 解析 export 与引号', v['A'] == 'x y')
    check('.env 去除尾随注释', v['B'] == 'z')
    f = tempfile.NamedTemporaryFile('w', suffix='.env', delete=False)
    f.write('REG_TEST_VAR=fromfile\n')
    f.close()
    os.environ['REG_TEST_VAR'] = 'fromshell'
    load_env(f.name)
    check('真实环境变量优先于 .env 文件', os.environ['REG_TEST_VAR'] == 'fromshell')
    os.environ.pop('REG_TEST_VAR', None)


def test_no_tmp_state_paths():
    offenders = []
    for path in ROOT.rglob('strategy_position_manager.py'):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith(('A_PY/', 'A_TEST/', 'bit/B_PY/', 'bit/B_TEST/')):
            continue
        if '"/tmp/' in path.read_text(encoding='utf-8') or 'f"/tmp/' in path.read_text(encoding='utf-8'):
            offenders.append(rel)
    check('持仓状态不再写入可预测的 /tmp 路径（%s）' % (offenders or '无'), not offenders)


def main():
    for fn in sorted([f for n, f in globals().items() if n.startswith('test_')],
                     key=lambda f: f.__name__):
        print('\n[%s]' % fn.__name__)
        try:
            fn()
        except Exception as exc:
            check('%s 未抛异常' % fn.__name__, False)
            print('        %s: %s' % (type(exc).__name__, exc))
    passed = sum(1 for _, ok in RESULTS if ok)
    print('\n' + '=' * 60)
    print('总计 %d/%d 通过' % (passed, len(RESULTS)))
    print('=' * 60)
    return 0 if passed == len(RESULTS) else 1


if __name__ == '__main__':
    raise SystemExit(main())
