# -*- coding: utf-8 -*-
"""
BIT 引擎完整决策周期驱动（最终验证）
- 真实 ccxt.binance 实例（假密钥），实例级拦截所有 fetch；全部端点仅由合成模拟交易所应答，无网络回退
  => 下单/条件单/撤单全部经过 ccxt 真实 create_order / cancel_order / fetch_positions 路由与签名，记录真实 URL 路径与参数
- 真实执行器 place_market_order/close_long/close_short、真实 position_manager、真实 TrailingStopManager、真实引擎
- 每周期：新建数据获取器/执行器/引擎 -> reconcile_state() -> run_strategy(df)，与 bit_*_main.py 一致
用法: python drive_bit.py <long|short> <plan> <out_dir>
"""
import os, sys, json, shutil, io, contextlib, traceback, inspect, urllib.parse, time, copy

ENGINE, PLAN, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
W = os.environ['BIT_EVIDENCE_ROOT']
os.makedirs(OUT, exist_ok=True)
os.chdir(OUT)

import ccxt
import numpy as np
import pandas as pd

from bit.config import feature_switches as FS
from bit.config.bit_long_term_config import LONG_TERM_CONFIG
from bit.config.bit_short_term_config import SHORT_TERM_CONFIG
from bit.utils.logger import setup_logger
from bit.strategy.bit_long_term.data_fetcher import LongTermDataFetcher
from bit.strategy.bit_short_term.data_fetcher import ShortTermDataFetcher
from bit.strategy.bit_long_term.order_executor import LongTermOrderExecutor
from bit.strategy.bit_short_term.order_executor import ShortTermOrderExecutor
from bit.strategy.bit_long_term.position_manager import LongTermPositionManager
from bit.strategy.bit_short_term.position_manager import ShortTermPositionManager
from bit.strategy.bit_long_term.risk_manager import LongTermRiskManager
from bit.strategy.bit_short_term.risk_manager import ShortTermRiskManager
from bit.strategy.bit_long_term.strategy_engine import LongTermDMRStrategy
from bit.strategy.bit_short_term.strategy_engine import ShortTermDMRStrategy

# Deterministic wholly synthetic OHLCV, aligned across periods.
KL={}
for tf,step,n in [('1h',3600000,640),('15m',900000,2560)]:
    x=np.arange(n)*step/3600000
    close=100+10*np.sin(x/8)+2*np.sin(x/2)
    KL[tf]=[[1704067200000+i*step,float(c),float(c+0.5),float(c-0.5),float(c),1000] for i,c in enumerate(close)]


def kdf(tf):
    d = pd.DataFrame(KL[tf], columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    d['ts'] = d['timestamp']
    d['timestamp'] = pd.to_datetime(d['timestamp'], unit='ms')
    d.set_index('timestamp', inplace=True)
    return d


DF1H, DF15 = kdf('1h'), kdf('15m')
BASE = DF1H if ENGINE == 'long' else DF15

# ------------------------------------------------------------------ 公开数据缓存
EX = ccxt.binance({'apiKey': 'DUMMYKEYDUMMYKEY', 'secret': 'DUMMYSECRETDUMMYSECRET', 'enableRateLimit': False,
                   'options': {'defaultType': 'future', 'recvWindow': 10000, 'fetchCurrencies': False}})
ORIG_FETCH = type(EX).fetch


class Sim:
    def __init__(self):
        self.equity = 10000.0  # 离线账户权益，供风控的 *_pct 阈值换算使用
        self.pos = {'LONG': 0.0, 'SHORT': 0.0}
        self.entry = {'LONG': 0.0, 'SHORT': 0.0}
        self.algo = {}
        self.next_algo = 4000000001
        self.next_order = 7000000001
        self.requests = []
        self.rules = []
        self.now_ms = 0
        self.price = 0.0
        self.events = []

    @staticmethod
    def ctx():
        names, sig, has_close = [], None, False
        for fr in inspect.stack():
            names.append(fr.function)
            if fr.function == 'execute_signal':
                sig = fr.frame.f_locals.get('signal')
                has_close = 'close_order' in fr.frame.f_locals
        return names, sig, has_close

    def kind_of(self, method, path):
        names, sig, has_close = self.ctx()
        if path.endswith('/algoOrder'):
            if method == 'POST':
                return 'algo_create'
            if '_retry_pending_cancels' in names:
                return 'algo_cancel_retry'
            if '_update_binance_stop_order' in names:
                return 'algo_cancel_update'
            if 'on_position_closed' in names:
                return 'algo_cancel_close'
            return 'algo_cancel_other'
        if path.endswith('/order') and method == 'POST':
            if 'open_long' in names or 'open_short' in names:
                return 'reverse_open' if has_close else 'open'
            if 'close_long' in names or 'close_short' in names:
                if 'execute_signal' in names:
                    return 'close_signal' if str(sig).startswith('CLOSE') else 'reverse_close'
                return 'trailing_close'
            return 'order_other'
        if path.endswith('/order') and method == 'DELETE':
            return 'order_cancel'
        return 'other'

    def err(self, code, msg, url, method, headers, body):
        resp = {'code': code, 'msg': msg}
        txt = json.dumps(resp)
        EX.handle_errors(400, 'Bad Request', url, method, {}, txt, resp, headers, body)
        raise ccxt.ExchangeError(EX.id + ' ' + txt)

    def apply_rules(self, kind):
        for r in self.rules:
            if r['kind'] == kind:
                if r.get('skip', 0) > 0:
                    r['skip'] -= 1
                    continue
                if r.get('times', 0) > 0:
                    r['times'] -= 1
                    r['hit'] = r.get('hit', 0) + 1
                    return r
        return None

    def handle(self, method, path, q, url, headers, body):
        kind = self.kind_of(method, path)
        rec = {'kind': kind, 'method': method, 'path': path,
               'params': {k: v for k, v in q.items() if k not in ('signature', 'timestamp', 'recvWindow')}}
        if method != 'GET' or kind != 'other':
            self.requests.append(rec)
        try:
            if method == 'GET' and path.endswith('/ticker/24hr'):
                return {'symbol': 'MAVIAUSDT', 'lastPrice': repr(self.price),
                        'closeTime': self.now_ms, 'openTime': self.now_ms - 86400000}
            if method == 'GET' and path.endswith('/leverageBracket'):
                return [{'symbol': 'MAVIAUSDT', 'brackets': [{'bracket': 1, 'initialLeverage': 20, 'notionalCap': 50000,
                                                             'notionalFloor': 0, 'maintMarginRatio': 0.02, 'cum': 0.0}]}]
            if method == 'GET' and path.endswith('/positionRisk'):
                out = []
                for ps in ('LONG', 'SHORT'):
                    if self.pos[ps] > 1e-12:
                        amt = self.pos[ps] if ps == 'LONG' else -self.pos[ps]
                        out.append({'symbol': 'MAVIAUSDT', 'positionSide': ps, 'positionAmt': repr(amt),
                                    'entryPrice': repr(self.entry[ps]), 'breakEvenPrice': '0', 'markPrice': repr(self.price),
                                    'unRealizedProfit': '0', 'liquidationPrice': '0', 'isolatedMargin': '0',
                                    'notional': repr(amt * self.price), 'marginAsset': 'USDT', 'isolatedWallet': '0',
                                    'initialMargin': '0', 'maintMargin': '0', 'positionInitialMargin': '0',
                                    'openOrderInitialMargin': '0', 'adl': 0, 'bidNotional': '0', 'askNotional': '0',
                                    'updateTime': self.now_ms})
                return out
            if method == 'GET' and path.startswith('/sapi/'):
                return []  # 带密钥 load_markets 时的保证金交易对等私有只读查询，与合约引擎无关
            if method == 'GET' and ('/fapi/v3/account' in path or '/fapi/v2/account' in path):
                # 持久化风控（common/risk_state.py）在每次风控检查时读取账户权益，
                # 用于把 *_pct 阈值换算成绝对金额。离线套件必须建模该只读端点，
                # 否则风控会因"权益不可得"而拒绝开仓，掩盖真正要验证的信号逻辑。
                return {'totalWalletBalance': repr(self.equity), 'totalMarginBalance': repr(self.equity),
                        'availableBalance': repr(self.equity), 'totalUnrealizedProfit': '0',
                        'assets': [{'asset': 'USDT', 'walletBalance': repr(self.equity),
                                    'availableBalance': repr(self.equity), 'marginBalance': repr(self.equity),
                                    'unrealizedProfit': '0', 'maxWithdrawAmount': repr(self.equity)}],
                        'positions': []}
            if method == 'GET' and '/fapi/v2/balance' in path:
                return [{'asset': 'USDT', 'balance': repr(self.equity), 'crossWalletBalance': repr(self.equity),
                         'availableBalance': repr(self.equity), 'maxWithdrawAmount': repr(self.equity)}]
            if path.endswith('/fapi/v1/order') and method == 'POST':
                r = self.apply_rules(kind)
                if r:
                    rec['result'] = f"INJECTED {r['code']}"
                    self.err(r['code'], r['msg'], url, method, headers, body)
                ps, side, qty = q.get('positionSide'), q.get('side'), float(q['quantity'])
                opening = (ps == 'LONG' and side == 'BUY') or (ps == 'SHORT' and side == 'SELL')
                if not opening and qty > self.pos[ps] + 1e-9:
                    rec['result'] = '-2022 reject'
                    self.err(-2022, 'ReduceOnly Order is rejected.', url, method, headers, body)
                if opening:
                    self.entry[ps] = self.price
                    self.pos[ps] += qty
                else:
                    self.pos[ps] = max(0.0, self.pos[ps] - qty)
                oid = self.next_order
                self.next_order += 1
                rec['result'] = f'FILLED id={oid}'
                return {'orderId': oid, 'symbol': 'MAVIAUSDT', 'status': 'FILLED', 'clientOrderId': q.get('newClientOrderId'),
                        'price': '0', 'avgPrice': repr(self.price), 'origQty': q['quantity'], 'executedQty': q['quantity'],
                        'cumQty': q['quantity'], 'cumQuote': repr(qty * self.price), 'timeInForce': 'GTC', 'type': 'MARKET',
                        'reduceOnly': False, 'closePosition': False, 'side': side, 'positionSide': ps, 'stopPrice': '0',
                        'workingType': 'CONTRACT_PRICE', 'priceProtect': False, 'origType': 'MARKET', 'priceMatch': 'NONE',
                        'selfTradePreventionMode': 'EXPIRE_MAKER', 'goodTillDate': 0, 'updateTime': self.now_ms}
            if path.endswith('/fapi/v1/algoOrder') and method == 'POST':
                r = self.apply_rules(kind)
                if r:
                    rec['result'] = f"INJECTED {r['code']}"
                    self.err(r['code'], r['msg'], url, method, headers, body)
                aid = self.next_algo
                self.next_algo += 1
                self.algo[str(aid)] = {'status': 'NEW', 'side': q.get('side'), 'positionSide': q.get('positionSide'),
                                       'triggerPrice': float(q.get('triggerPrice')), 'quantity': float(q.get('quantity'))}
                rec['result'] = f'NEW algoId={aid}'
                return {'algoId': aid, 'clientAlgoId': q.get('clientAlgoId'), 'algoType': 'CONDITIONAL',
                        'orderType': q.get('type'), 'symbol': 'MAVIAUSDT', 'side': q.get('side'),
                        'positionSide': q.get('positionSide'), 'timeInForce': 'GTC', 'quantity': q.get('quantity'),
                        'algoStatus': 'NEW', 'triggerPrice': q.get('triggerPrice'), 'price': '0', 'icebergQuantity': None,
                        'selfTradePreventionMode': 'EXPIRE_MAKER', 'workingType': 'CONTRACT_PRICE', 'priceMatch': 'NONE',
                        'closePosition': False, 'priceProtect': False, 'reduceOnly': False, 'activatePrice': '',
                        'callbackRate': '', 'createTime': self.now_ms, 'updateTime': self.now_ms, 'triggerTime': 0,
                        'goodTillDate': 0}
            if path.endswith('/fapi/v1/algoOrder') and method == 'DELETE':
                r = self.apply_rules(kind)
                if r:
                    rec['result'] = f"INJECTED {r['code']}"
                    self.err(r['code'], r['msg'], url, method, headers, body)
                aid = q.get('algoId')
                a = self.algo.get(str(aid))
                if a is None or a['status'] != 'NEW':
                    rec['result'] = f"-20124 (status={a and a['status']})"
                    self.err(-20124, 'Invalid algo id or it has been completed.', url, method, headers, body)
                a['status'] = 'CANCELED'
                rec['result'] = 'CANCELED'
                return {'algoId': int(aid), 'clientAlgoId': 'x', 'code': '200', 'msg': 'success'}
            if path.endswith('/fapi/v1/order') and method == 'DELETE':
                rec['result'] = '-2011'
                self.err(-2011, 'Unknown order sent.', url, method, headers, body)
            rec['result'] = 'UNHANDLED'
            raise ccxt.ExchangeError('UNHANDLED ' + method + ' ' + path)
        except Exception as e:
            rec.setdefault('result', 'EXC')
            rec['exception'] = f'{type(e).__name__}: {str(e)[:120]}'
            raise

    def exchange_stops(self, bars):
        """交易所侧条件单触发模拟（仅 exch_stop 计划启用）"""
        for aid, a in self.algo.items():
            if a['status'] != 'NEW':
                continue
            for ts, row in bars.iterrows():
                hit = (a['side'] == 'SELL' and row['low'] <= a['triggerPrice']) or (a['side'] == 'BUY' and row['high'] >= a['triggerPrice'])
                if hit:
                    ps = a['positionSide']
                    q = min(a['quantity'], self.pos[ps])
                    self.pos[ps] -= q
                    a['status'] = 'FINISHED'
                    self.events.append({'algoId': aid, 'bar': str(ts), 'trigger': a['triggerPrice'], 'closed_qty': q, 'positionSide': ps})
                    break


SIM = Sim()
ALL_RULES=[]


def fake_fetch(url, method='GET', headers=None, body=None):
    u = urllib.parse.urlsplit(url)
    q = dict(urllib.parse.parse_qsl(u.query))
    if body:
        q.update(dict(urllib.parse.parse_qsl(body)))
    return SIM.handle(method, u.path, q, url, headers, body)


EX.fetch = fake_fetch
# Synthetic market metadata; actual ccxt parser/order/signing, no fetch fallback.
EX.set_markets([{'id':'MAVIAUSDT','symbol':'MAVIA/USDT:USDT','base':'MAVIA','quote':'USDT','settle':'USDT','baseId':'MAVIA','quoteId':'USDT','settleId':'USDT','type':'swap','spot':False,'swap':True,'future':False,'option':False,'contract':True,'linear':True,'inverse':False,'active':True,'contractSize':1,'precision':{'amount':0.001,'price':0.001},'limits':{'amount':{'min':0.001,'max':1000000},'price':{'min':0.001,'max':1000000},'cost':{'min':5,'max':None}},'info':{'orderTypes':['MARKET','LIMIT','STOP_MARKET'],'filters':[{'filterType':'LOT_SIZE','stepSize':'0.001','minQty':'0.001','maxQty':'1000000'},{'filterType':'PRICE_FILTER','tickSize':'0.001','minPrice':'0.001','maxPrice':'1000000'}]}}])

# ------------------------------------------------------------------ 组件构造
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(
    sys.modules['bit.strategy.bit_long_term.strategy_engine'].__file__)))), 'data', 'bit_positions')


def clean_state():
    pref = ('bit_long_term', 'trailing_stop_MAVIA_USDT_1h') if ENGINE == 'long' else ('bit_short_term', 'trailing_stop_MAVIA_USDT_15m')
    for f in os.listdir(DATA_DIR):
        if f.startswith(pref):
            os.remove(os.path.join(DATA_DIR, f))


def make_fetcher(cls, cfg, tf_df, upto, name):
    f = object.__new__(cls)
    f.config = cfg
    f.logger = setup_logger(name=name, log_file=cfg['log_config']['log_file'])
    f.exchange = EX
    f.symbol, f.timeframe, f.dmr_period = cfg['symbol'], cfg['timeframe'], cfg['dmr_period']
    f.data_dir = os.path.join(OUT, 'csv')
    os.makedirs(f.data_dir, exist_ok=True)
    f.data_path = os.path.join(f.data_dir, f'{name}.csv')
    f.sync_time = lambda force=False: None
    sl = tf_df[tf_df.index <= upto].drop(columns=['ts']).copy()
    f.get_historical_data = lambda: sl.copy()
    return f


def make_executor(cls, cfg, fetcher, pm):
    ex = object.__new__(cls)
    ex.exchange, ex.position_manager, ex.data_fetcher, ex.config, ex.market_info = EX, pm, fetcher, cfg, {}
    return ex


def build(i):
    t = BASE.index[i]
    SIM.now_ms = int(BASE['ts'].iloc[i]) + 5000
    SIM.price = float(BASE['close'].iloc[i])
    if ENGINE == 'long':
        cfg = LONG_TERM_CONFIG
        fetcher = make_fetcher(LongTermDataFetcher, cfg, DF1H, t, 'LongTermDataFetcher')
        pm = LongTermPositionManager(EX)
        exe = make_executor(LongTermOrderExecutor, cfg, fetcher, pm)
        risk = LongTermRiskManager(EX)
        df = fetcher.get_and_save_data()
        eng = LongTermDMRStrategy(fetcher, exe, pm, risk)
    else:
        cfg = SHORT_TERM_CONFIG
        fetcher = make_fetcher(ShortTermDataFetcher, cfg, DF15, t, 'ShortTermDataFetcher')
        lfetcher = make_fetcher(LongTermDataFetcher, LONG_TERM_CONFIG, DF1H, t, 'LongTermDataFetcher')
        pm = ShortTermPositionManager(EX)
        exe = make_executor(ShortTermOrderExecutor, cfg, fetcher, pm)
        risk = ShortTermRiskManager(EX)
        df = fetcher.get_and_save_data()
        eng = ShortTermDMRStrategy(fetcher, exe, pm, risk, lfetcher)
    return eng, df


def disk_state(eng):
    s = {}
    try:
        s['strategy'] = json.load(open(eng.position_file)) if os.path.exists(eng.position_file) else None
    except Exception as e:
        s['strategy'] = f'unreadable {e}'
    if s['strategy'] and isinstance(s['strategy'], dict):
        s['strategy'] = {k: s['strategy'].get(k) for k in ('side', 'amount', 'entry_price')}
    m = eng.trailing_stop_manager
    d = json.load(open(m.position_file)) if os.path.exists(m.position_file) else {}
    s['tsm'] = {k: d.get(k) for k in ('has_position', 'side', 'position_amount', 'entry_time', 'current_stop_loss',
                                     'kline_cycle_count', 'binance_stop_order_id', 'pending_cancel_order_ids')}
    if ENGINE == 'short':
        s['dmr_state_exists'] = os.path.exists(eng.dmr_state_file)
    return s


LOG = open(os.path.join(OUT, f'{ENGINE}_{PLAN}_stdout.log'), 'w', encoding='utf-8')
TIMELINE = []
PREV_I = [None]
KEYS = ('检测到穿越信号', '纠错信号', '协调', '信号检测结果', '平仓', '开多', '开空', '失败', '反手', '止损触发', '移动止损平仓',
        '待撤销', '撤销', 'Traceback', 'Error', '模型', '接管', '首次执行', '判定为外部手动平仓', '已同步持仓', '运行失败', '无多仓', '无空仓')


def cycle(i, force_signal=None, pre=None, exch_stop=False, label=''):
    if exch_stop and PREV_I[0] is not None:
        SIM.exchange_stops(BASE.iloc[PREV_I[0]:i])
    PREV_I[0] = i
    n0, e0 = len(SIM.requests), len(SIM.events)
    buf = io.StringIO()
    exc = None
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            eng, df = build(i)
            if pre:
                pre(eng, df)
            eng.reconcile_state()
            if force_signal:
                eng.execute_signal(force_signal, df['close'].iloc[-1])
            else:
                eng.run_strategy(df)
        except Exception:
            exc = traceback.format_exc()
    txt = buf.getvalue()
    LOG.write(f'\n######## cycle i={i} t={BASE.index[i]} label={label} force={force_signal}\n{txt}\n')
    if exc:
        LOG.write('HARNESS EXC\n' + exc)
    lines = [l.split(' - ')[-1][:160] for l in txt.splitlines() if any(k in l for k in KEYS) and 'DataFrame' not in l]
    entry = {'i': i, 't': str(BASE.index[i]), 'label': label, 'force': force_signal,
             'close': float(BASE['close'].iloc[i]),
             'requests': SIM.requests[n0:], 'exchange_events': SIM.events[e0:],
             'sim_pos': dict(SIM.pos), 'state': disk_state(eng) if not exc else None,
             'traceback_in_log': txt.count('Traceback (most recent call last)'), 'harness_exc': exc, 'key_lines': lines}
    TIMELINE.append(entry)
    return entry


def dmr_series():
    if ENGINE == 'long':
        f = make_fetcher(LongTermDataFetcher, LONG_TERM_CONFIG, DF1H, DF1H.index[-1], 'probe_long')
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            d = f.calculate_dmr(f.get_historical_data())
        return d[f'dmr_{LONG_TERM_CONFIG["dmr_period"]}']
    f = make_fetcher(ShortTermDataFetcher, SHORT_TERM_CONFIG, DF15, DF15.index[-1], 'probe_short')
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        d = f.calculate_dmr(f.get_historical_data())
    return d[f'dmr_{SHORT_TERM_CONFIG["dmr_period"]}']


def set_model(n):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        FS.load_model_preset({1: FS.RunningModel.ORIGINAL, 2: FS.RunningModel.NEW, 3: FS.RunningModel.HYBRID}[n])


def arm_trailing(side_trigger=True):
    """夹具：把移动止损价设在当前价之上(多)/之下(空)使本周期触发，kline_cycle_count 置大避免重算"""
    def pre(eng, df):
        m = eng.trailing_stop_manager
        d = json.load(open(m.position_file))
        close = float(df['close'].iloc[-1])
        if d.get('side') == 'long':
            d['current_stop_loss'] = close * (1.01 if side_trigger else 0.5)
        else:
            d['current_stop_loss'] = close * (0.99 if side_trigger else 2.0)
        d['kline_cycle_count'] = 9999
        json.dump(d, open(m.position_file, 'w'), indent=2)
        m.position_data = d
    return pre


# ------------------------------------------------------------------ 计划
ERR_INTERNAL = (-1001, 'Internal error; unable to process your request. Please try again.')
ERR_MARGIN = (-2019, 'Margin is insufficient.')


def rule(kind, code_msg, times=1, skip=0):
    r={'kind': kind, 'code': code_msg[0], 'msg': code_msg[1], 'times': times, 'skip': skip}
    ALL_RULES.append(r)
    return r


def run_continuous(start, end, exch_stop=False):
    for i in range(start, end):
        cycle(i, exch_stop=exch_stop)


def main():
    clean_state()
    set_model(1)
    dmr = dmr_series()
    period_start = 60
    ups = [i for i in range(period_start, len(dmr)) if dmr.iloc[i - 1] <= 0 < dmr.iloc[i]]
    downs = [i for i in range(period_start, len(dmr)) if dmr.iloc[i - 1] >= 0 > dmr.iloc[i]]
    meta = {'engine': ENGINE, 'plan': PLAN, 'ccxt': ccxt.__version__, 'numpy': np.__version__, 'pandas': pd.__version__,
            'ups': ups[:10], 'downs': downs[:10], 'n_ups': len(ups), 'n_downs': len(downs)}
    n = len(BASE)
    if PLAN == 'normal':
        run_continuous(n - 400, n)
    elif PLAN == 'rev_close_fail':
        SIM.rules = [rule('reverse_close', ERR_INTERNAL)]
        run_continuous(n - 400, n)
    elif PLAN == 'rev_open_fail':
        SIM.rules = [rule('reverse_open', ERR_MARGIN)]
        run_continuous(n - 400, n)
    elif PLAN == 'close_signal_fail':
        SIM.rules = [rule('close_signal', ERR_INTERNAL)]
        run_continuous(n - 400, n)
    elif PLAN == 'trail_close_fail':
        SIM.rules = [rule('trailing_close', ERR_INTERNAL)]
        run_continuous(n - 400, n)
    elif PLAN == 'algo_cancel_fail':
        SIM.rules = [rule('algo_cancel_close', ERR_INTERNAL, times=1), rule('algo_cancel_update', ERR_INTERNAL, times=1),
                     rule('algo_cancel_retry', ERR_INTERNAL, times=1)]
        run_continuous(n - 400, n)
    elif PLAN == 'exch_stop':
        run_continuous(n - 400, n, exch_stop=True)
    elif PLAN == 'forced':
        forced(ups, downs)
    elif PLAN == 'model2':
        model2(ups, downs)
    for r in SIM.rules:
        r.setdefault('hit', 0)
    meta['rules'] = ALL_RULES
    meta['orphan_algo_new'] = {k: v for k, v in SIM.algo.items() if v['status'] == 'NEW'}
    meta['final_sim_pos'] = SIM.pos
    json.dump({'meta': meta, 'timeline': TIMELINE}, open(os.path.join(OUT, f'{ENGINE}_{PLAN}.json'), 'w'),
              ensure_ascii=False, indent=1, default=str)
    set_model(1)
    clean_state()


def first_after(lst, x):
    return next(v for v in lst if v > x)


def forced(ups, downs):
    """强制序列：用真实 execute_signal 开仓（夹具）+ 真实 run_strategy 周期"""
    n = len(BASE)
    d = [v for v in downs if v > n - 500][0]
    # F1 反手：开多 -> 反手平多失败 -> 下周期(非穿越) -> 反手平多成功+开空失败 -> 反手都成功
    cycle(d - 2, force_signal='LONG', label='F1 seed long via execute_signal')
    SIM.rules = [rule('reverse_close', ERR_INTERNAL)]
    cycle(d, force_signal='SHORT', label='F1 reverse SHORT: close fails')
    cycle(d + 1, label='F1 next cycle run_strategy (retry via correction/none)')
    SIM.rules = [rule('reverse_open', ERR_MARGIN)]
    if SIM.pos['LONG'] > 0:
        cycle(d + 2, force_signal='SHORT', label='F1 reverse SHORT: close ok, open fails')
    cycle(d + 3, label='F1 cycle after open failure')
    cycle(d + 4, force_signal='LONG', label='F1 seed long again')
    SIM.rules = []
    cycle(d + 5, force_signal='SHORT', label='F1 reverse SHORT: both succeed')
    cycle(d + 6, label='F1 cycle after reverse ok')
    cycle(d + 7, force_signal='CLOSE_SHORT' if ENGINE == 'short' else 'LONG', label='F1 close short (short: CLOSE_SHORT / long: reverse)')
    # F2 CLOSE_LONG 失败重试（仅短周期）
    if ENGINE == 'short':
        cycle(d + 10, force_signal='LONG', label='F2 seed long')
        SIM.rules = [rule('close_signal', ERR_INTERNAL)]
        cycle(d + 11, force_signal='CLOSE_LONG', label='F2 CLOSE_LONG fails')
        cycle(d + 12, force_signal='CLOSE_LONG', label='F2 CLOSE_LONG retry ok')
        cycle(d + 13, force_signal='SHORT', label='F2 seed short')
        SIM.rules = [rule('close_signal', ERR_INTERNAL)]
        cycle(d + 14, force_signal='CLOSE_SHORT', label='F2 CLOSE_SHORT fails')
        cycle(d + 15, force_signal='CLOSE_SHORT', label='F2 CLOSE_SHORT retry ok')
    # F3 移动止损触发：成功 / 失败后重试
    cycle(d + 20, force_signal='LONG', label='F3 seed long')
    cycle(d + 21, pre=arm_trailing(True), label='F3 trailing trigger close ok')
    cycle(d + 22, force_signal='SHORT', label='F3 seed short')
    SIM.rules = [rule('trailing_close', ERR_INTERNAL)]
    cycle(d + 23, pre=arm_trailing(True), label='F3 trailing trigger close FAILS')
    cycle(d + 24, pre=arm_trailing(True), label='F3 trailing trigger retry ok')
    # F4 reconcile 同步路径（移动止损文件丢失）后触发移动止损
    cycle(d + 30, force_signal='LONG', label='F4 seed long')
    def drop_tsm(eng, df):
        os.remove(eng.trailing_stop_manager.position_file)
        eng.trailing_stop_manager.position_data = eng.trailing_stop_manager._load_position_data()
    cycle(d + 31, pre=drop_tsm, label='F4 TSM file lost -> reconcile sync')
    cycle(d + 32, pre=arm_trailing(True), label='F4 trailing trigger after reconcile sync')
    cycle(d + 33, pre=arm_trailing(True), label='F4 trailing trigger again')
    # F5 仓位已被交易所外部平掉后反手（-2022）
    cycle(d + 40, force_signal='SHORT' if ENGINE == 'long' else 'SHORT', label='F5 seed short')
    def ext_close(eng, df):
        SIM.pos['SHORT'] = 0.0
    cycle(d + 41, pre=ext_close, force_signal='LONG', label='F5 short closed externally before cycle, LONG signal')
    def ext_close_after_reconcile(eng, df):
        pass
    cycle(d + 42, force_signal='SHORT', label='F5 seed short 2')
    orig = SIM.handle
    def partial(eng, df):
        SIM.pos['SHORT'] = SIM.pos['SHORT'] * 0.5
    cycle(d + 43, pre=partial, force_signal='LONG', label='F5 exchange short partially closed (qty mismatch), LONG signal')
    cycle(d + 44, label='F5 next cycle run_strategy')
    cycle(d + 45, force_signal='LONG', label='F5 LONG signal again')
    if ENGINE == 'short':
        # F6 撤单失败 -> 待撤销 -> 开仓/更新时重试
        SIM.pos = {'LONG': 0.0, 'SHORT': 0.0}
        clean_state()
        cycle(d + 50, force_signal='LONG', label='F6 seed long')
        def set_stop_order(eng, df):
            m = eng.trailing_stop_manager
            oid = m._create_binance_stop_order('long', float(df['close'].iloc[-1]) * 0.8, m.position_data['position_amount'])
            m.position_data['binance_stop_order_id'] = oid
            m._save_position_data()
        cycle(d + 51, pre=set_stop_order, label='F6 create exchange stop (algo POST)')
        SIM.rules = [rule('algo_cancel_close', ERR_INTERNAL), rule('algo_cancel_retry', ERR_INTERNAL, times=1)]
        cycle(d + 52, force_signal='CLOSE_LONG', label='F6 CLOSE_LONG ok, algo cancel FAILS -> pending')
        cycle(d + 53, force_signal='SHORT', label='F6 open short -> retry pending (fails once)')
        def update_stop(eng, df):
            m = eng.trailing_stop_manager
            oid = m._update_binance_stop_order('short', float(df['close'].iloc[-1]) * 1.2, m.position_data['position_amount'])
            m.position_data['binance_stop_order_id'] = oid
            m._save_position_data()
        cycle(d + 54, pre=update_stop, label='F6 update stop -> retry pending again (ok)')
        cycle(d + 55, pre=update_stop, label='F6 update stop again (old algo cancel with trigger=True)')
        cycle(d + 56, force_signal='CLOSE_SHORT', label='F6 CLOSE_SHORT -> cancel stop')
        # F7 旧版普通止损单ID（非 algo）撤单回退
        cycle(d + 60, force_signal='LONG', label='F7 seed long')
        def legacy_id(eng, df):
            m = eng.trailing_stop_manager
            m.position_data['binance_stop_order_id'] = '123456789'
            m._save_position_data()
        cycle(d + 61, pre=legacy_id, force_signal='CLOSE_LONG', label='F7 legacy normal stop id -> -20124 -> fallback DELETE /order -> -2011 treated ok')


def model2(ups, downs):
    n = len(BASE)
    d = [v for v in downs if v > n - 500][0]
    set_model(1)
    cycle(d - 2, force_signal='LONG', label='M model1 seed long')
    set_model(2)
    cycle(d - 1, label='M model2 run_strategy (has local position)')
    cycle(d, label='M model2 natural down-cross bar run_strategy')
    cycle(d + 1, force_signal='SHORT', label='M model2 execute_signal SHORT directly (reverse branch)')
    cycle(d + 2, label='M model2 run_strategy with no local position (claim path)')
    # 外部手动开仓 -> 接管
    def manual_long(eng, df):
        SIM.pos['LONG'] = 100.0
        SIM.entry['LONG'] = float(df['close'].iloc[-1])
    cycle(d + 3, pre=manual_long, label='M model2 manual long on exchange -> claim?')
    cycle(d + 4, label='M model2 next cycle')
    cycle(d + 5, pre=arm_trailing(True), label='M model2 trailing trigger (auto_close)')
    set_model(1)


if __name__ == '__main__':
    main()
