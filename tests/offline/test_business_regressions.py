"""Offline business regressions. No application imports/config or network.
Run: venv/bin/python tests/offline/test_business_regressions.py
Classes are compiled from the current source AST, without import-time side effects.
"""
import ast
import copy
import importlib.util
import json
import logging
import math
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
import inspect
import unittest
from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from typing import Optional, Dict, Any
from unittest.mock import Mock

ROOT = Path(os.environ.get('DMR_SOURCE_ROOT', Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(ROOT))

def blocked(*args, **kwargs):
    raise AssertionError('Network prohibited in offline regressions')
socket.socket.connect = blocked
socket.create_connection = blocked

def load(relative, name):
    """Load actual class/function definitions only, never config or credentials."""
    import pandas as pd
    tree = ast.parse((ROOT / relative).read_text())
    nodes = [n for n in tree.body if isinstance(n, (ast.ClassDef, ast.FunctionDef))]
    ns = dict(pd=pd, math=math, Decimal=Decimal, ROUND_DOWN=ROUND_DOWN, Optional=Optional,
              Dict=Dict, Any=Any, datetime=datetime, os=os, json=json, logging=logging)
    helper = ROOT / 'common/balance_helper.py'
    if helper.exists():
        spec = importlib.util.spec_from_file_location('offline_balance_helper', helper)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        ns['extract_usdt_balance'] = module.extract_usdt_balance
    exec(compile(ast.Module(body=nodes, type_ignores=[]), relative, 'exec'), ns)
    return ns[name]

FETCHERS = [(f'strategy/{s}/data_fetcher.py', 'ShortTermDataFetcher' if 'short' in s else 'LongTermDataFetcher') for s in ('short_term', 'long_term', 'first_short_term', 'first_long_term')]
FETCHERS += [(f'bit/strategy/{s}/data_fetcher.py', 'ShortTermDataFetcher' if 'short' in s else 'LongTermDataFetcher') for s in ('bit_short_term', 'bit_long_term')]

class BusinessTests(unittest.TestCase):
    def test_balance_filtered_nonzero_raw_assets(self):
        # This is the exact parser shape produced by ccxt 4.5.78.
        import ccxt
        ex = ccxt.binance()
        raw = {'assets': [{'asset': 'USDT', 'updateTime': 0,
                           'marginBalance': '125.50', 'availableBalance': '100', 'initialMargin': '25.50'}]}
        balance = ex.parse_balance_custom(raw, 'linear')
        self.assertNotIn('USDT', balance)
        for path, name in FETCHERS:
            with self.subTest(path=path):
                obj = object.__new__(load(path, name))
                obj.exchange = Mock(fetch_balance=Mock(return_value=copy.deepcopy(balance)))
                obj.logger = Mock()
                self.assertEqual(obj.get_account_balance(), {'total_usdt': 125.5, 'free_usdt': 100.0, 'used_usdt': 25.5})
                obj.exchange.fetch_balance.assert_called_once_with()

    def test_balance_missing_and_valid_unified(self):
        for path, name in FETCHERS:
            for balance, expect in [({}, {'total_usdt': 0, 'free_usdt': 0, 'used_usdt': 0}),
                ({'USDT': {'total': 12, 'free': 9, 'used': 3}}, {'total_usdt': 12, 'free_usdt': 9, 'used_usdt': 3})]:
                with self.subTest(path=path, balance=balance):
                    obj = object.__new__(load(path, name)); obj.exchange=Mock(fetch_balance=Mock(return_value=balance)); obj.logger=Mock()
                    self.assertEqual(obj.get_account_balance(), expect)

    def test_all_balance_consumers_recover_raw_assets(self):
        balance={'info':{'assets':[{'asset':'USDT','updateTime':0,'marginBalance':'125.5','availableBalance':'100','initialMargin':'25.5'}]},'total':{},'used':{},'free':{}}
        paths=[p.replace('data_fetcher.py','risk_manager.py') for p, _ in FETCHERS]+['data/data_fetcher.py']
        for path in paths:
            with self.subTest(path=path):
                tree=ast.parse((ROOT/path).read_text())
                name=next(n.name for n in tree.body if isinstance(n,ast.ClassDef))
                obj=object.__new__(load(path,name)); obj.exchange=Mock(fetch_balance=Mock(return_value=copy.deepcopy(balance)))
                obj.logger=Mock(); obj.sync_time=Mock(); obj.get_timestamp=Mock(return_value=1)
                got=obj.get_account_balance()
                self.assertEqual(got['total_usdt'] if isinstance(got,dict) else got,125.5)

    def test_balance_raw_validation_and_precedence(self):
        fn=load('common/balance_helper.py','extract_usdt_balance')
        for assets, expected in [([{'asset':'USDT','balance':'12','availableBalance':'9'}],{'total':12,'free':9,'used':3}),
                                  ([{'asset':'USDT','marginBalance':'0','availableBalance':'0','initialMargin':'0'}],{'total':0,'free':0,'used':0}),
                                  ([],{'total':0,'free':0,'used':0})]:
            self.assertEqual(fn({'info':assets}),expected)
        self.assertEqual(fn({'USDT':{'total':0,'free':0,'used':0},'info':[{'asset':'USDT','balance':'99','availableBalance':'99'}]})['total'],0)
        for asset in [{'asset':'USDT','balance':'nan','availableBalance':'1'}, {'asset':'USDT','balance':'12'},
                      {'asset':'USDT','balance':'Infinity','availableBalance':'1'}]:
            with self.assertRaises(ValueError):fn({'info':[asset]})

    def test_precision_decimal_and_nonfinite(self):
        cls=load('common/precision_helper.py', 'PrecisionHelper')
        h=cls(Mock(markets={'X/USDT:USDT': {'precision': {}, 'info': {'filters': [
            {'filterType':'PRICE_FILTER','tickSize':'0.05'}, {'filterType':'LOT_SIZE','stepSize':'0.0000001'}]}}}))
        self.assertEqual(h.format_price_str('X/USDT',100.33),'100.30')
        self.assertEqual(h.format_quantity_str('X/USDT',0.0002345678),'0.0002345')
        for method in ['format_price','format_quantity','format_price_str','format_quantity_str']:
            for value in [float('nan'),float('inf'),-float('inf')]:
                with self.subTest(method=method,value=value), self.assertRaises((ValueError,ArithmeticError)):
                    getattr(h,method)('X/USDT',value)

    def test_first_exit_quantity_direction_failure(self):
        for period in ('first_short_term', 'first_long_term'):
            name = 'ShortTermDMRStrategy' if 'short' in period else 'LongTermDMRStrategy'
            cls = load(f'strategy/{period}/strategy_engine.py', name)
            for side in ('long', 'short'):
                for qty in (100, 0, None):
                    with self.subTest(period=period, side=side, qty=qty):
                        eng = object.__new__(cls)
                        eng.strategy_position = {'side': side, 'amount': qty}
                        eng.position_manager = Mock(get_current_position=Mock(return_value={'contracts': 80, 'side': side}))
                        eng.order_executor = Mock()
                        # _close_position_for_exit 现在会经 _record_close_pnl 回写风控账本，
                        # object.__new__ 跳过了 __init__，这两个属性必须由夹具补齐。
                        eng.logger = Mock()
                        eng.risk_manager = Mock()
                        eng.ladder_tp_manager = Mock(has_position=Mock(return_value=True), get_position_info=Mock(return_value={'remaining_amount': 30})) if 'short' in period else None
                        method = getattr(eng.order_executor, 'close_' + side)
                        method.return_value = {'id': 'fake'}
                        eng._close_position_for_exit(side, {'position_amount': 100})
                        expected = 30 if 'short' in period else 80
                        method.assert_called_once_with(expected, {'side': side, 'amount': expected})
                        method.return_value = None
                        with self.assertRaises(RuntimeError):
                            eng._close_position_for_exit(side, {'position_amount': 100})
                        method.reset_mock()
                        with self.assertRaises(RuntimeError):
                            eng._close_position_for_exit('short' if side == 'long' else 'long', {'position_amount': 100})
                        method.assert_not_called()

    def test_bit_cancel_retry_and_trigger(self):
        cls = load('bit/common/trailing_stop_manager.py', 'TrailingStopManager')
        eng = object.__new__(cls)
        eng.symbol = 'X/USDT'
        eng._save_position_data = Mock()
        eng.position_data = {'has_position': True, 'side': 'long', 'entry_price': 100,
                             'current_stop_loss': 90, 'stop_loss_history': [], 'binance_stop_order_id': 'old', 'position_amount': 1}
        eng.exchange = Mock()
        eng.exchange.cancel_order.side_effect = RuntimeError('network failure')
        eng.on_position_closed()
        self.assertEqual(eng.position_data['pending_cancel_order_ids'], ['old'])
        eng.exchange.cancel_order.side_effect = None
        eng.exchange.create_order.return_value = {'id': 'new'}
        eng._update_binance_stop_order('long', 90, 1)
        self.assertEqual(eng.position_data['pending_cancel_order_ids'], [])
        self.assertEqual(eng.position_data['binance_stop_order_id'], 'new')
        eng.exchange.cancel_order.assert_called_with(id='old', symbol='X/USDT', params={'trigger': True})

    def test_bit_cancel_error_codes_not_message_fragments(self):
        cls=load('bit/common/trailing_stop_manager.py','TrailingStopManager')
        for code in (-2011,-2013,-20124,-2014,-2015,-1021):
            with self.subTest(code=code):
                eng=object.__new__(cls); eng.symbol='X/USDT'; eng.exchange=Mock()
                # Numeric code must decide, even if message is misleading/localized.
                message='unknown order' if code in (-2014,-2015) else 'fixture'
                eng.exchange.cancel_order.side_effect=RuntimeError(json.dumps({'code':code,'msg':message}))
                self.assertEqual(eng._cancel_binance_stop_order('old'),code in (-2011,-2013,-20124))

    def test_precision_rejects_nonfinite_step(self):
        cls=load('common/precision_helper.py','PrecisionHelper')
        for step in (Decimal('NaN'),Decimal('Infinity'),Decimal('-Infinity')):
            with self.subTest(step=step), self.assertRaises(ValueError):
                cls()._round_down_decimal(1,2,step)

    def test_bit_tuple_failure_keeps_position(self):
        for period in ('bit_short_term', 'bit_long_term'):
            path=f'bit/strategy/{period}/strategy_engine.py'
            tree=ast.parse((ROOT/path).read_text())
            name=next(n.name for n in tree.body if isinstance(n, ast.ClassDef) and any(isinstance(m,ast.FunctionDef) and m.name=='execute_signal' for m in n.body))
            for side, signal in [('long','SHORT'),('short','LONG')]:
                for close_ok, open_ok in [(False,True),(True,False),(True,True)]:
                    with self.subTest(period=period,side=side,close_ok=close_ok,open_ok=open_ok):
                        eng=object.__new__(load(path,name)); eng.logger=Mock(); eng.config={'strategy_id':'offline'}
                        eng.auto_open_enabled=True; eng.strategy_position={'side':side,'amount':100}
                        eng.position_manager=Mock(calculate_position_size=Mock(return_value=100),get_current_position=Mock(return_value=None))
                        eng.get_strategy_position=lambda:eng.strategy_position
                        eng._reset_position_state=Mock(); eng._update_strategy_position=Mock()
                        eng.order_executor=Mock()
                        for s in ('long','short'):
                            getattr(eng.order_executor,'close_'+s).return_value=({'id':'close'},'time') if close_ok else (None,None)
                            getattr(eng.order_executor,'open_'+s).return_value=({'id':'open'},'time') if open_ok else (None,None)
                        eng.execute_signal(signal,1)
                        self.assertEqual(eng._reset_position_state.call_count,int(close_ok))
                        self.assertEqual(eng._update_strategy_position.call_count,int(close_ok and open_ok))
                        self.assertEqual(getattr(eng.order_executor,'open_'+signal.lower()).call_count,int(close_ok))

    def test_main_and_first_open_failure_state(self):
        for period in ('short_term','long_term','first_short_term','first_long_term'):
            name='ShortTermDMRStrategy' if 'short' in period else 'LongTermDMRStrategy'
            for signal in ('LONG','SHORT'):
                for ok in (False,True):
                    with self.subTest(period=period,signal=signal,ok=ok):
                        eng=object.__new__(load(f'strategy/{period}/strategy_engine.py',name))
                        eng.logger=Mock(); eng.config={'strategy_id':'offline'}; eng.strategy_position=None
                        eng.get_strategy_position=lambda:eng.strategy_position
                        eng.position_manager=Mock(calculate_position_size=Mock(return_value=100),get_current_position=Mock(return_value=None))
                        eng._update_strategy_position=Mock(); eng.order_executor=Mock()
                        getattr(eng.order_executor,'open_'+signal.lower()).return_value={'id':'fake'} if ok else None
                        eng.execute_signal(signal,1)
                        self.assertEqual(eng._update_strategy_position.call_count,int(ok))

    def test_first_reverse_failure_and_persistent_reset(self):
        for period in ('first_short_term','first_long_term'):
            name='ShortTermDMRStrategy' if 'short' in period else 'LongTermDMRStrategy'
            cls=load(f'strategy/{period}/strategy_engine.py',name)
            for close_ok in (False,True):
                with tempfile.TemporaryDirectory() as folder, self.subTest(period=period,close_ok=close_ok):
                    eng=object.__new__(cls); eng.logger=Mock(); eng.config={'strategy_id':'offline'}
                    eng.strategy_position={'side':'short','amount':100}
                    eng.position_file=str(Path(folder)/'position.json')
                    Path(eng.position_file).write_text(json.dumps(eng.strategy_position))
                    eng.position_manager=Mock(calculate_position_size=Mock(return_value=100),get_current_position=Mock(return_value=None))
                    eng.order_executor=Mock(close_short=Mock(return_value={'id':'close'} if close_ok else None),open_long=Mock(return_value=None))
                    eng.trailing_stop_enabled=False; eng.trailing_tp_enabled=False; eng.ladder_tp_enabled=False
                    eng._reset_dmr_state=Mock()
                    eng.execute_signal('LONG',1)
                    self.assertEqual(Path(eng.position_file).exists(),not close_ok)
                    self.assertEqual(eng.strategy_position is None,close_ok)
                    self.assertEqual(eng.order_executor.open_long.call_count,int(close_ok))

    def test_close_position_hedge_and_oneway_parameters(self):
        for path,name in [('execution/order_executor.py','OrderExecutor'),('execution/testnet_order_executor.py','TestnetOrderExecutor')]:
            for side in ('LONG','SHORT'):
                with self.subTest(path=path,side=side):
                    obj=object.__new__(load(path,name)); obj.data_fetcher=Mock(get_timestamp=Mock(return_value=1))
                    obj.exchange=Mock(fetch_positions=Mock(return_value=[{'contracts':12,'side':side.lower(),'info':{'positionSide':side}}]))
                    obj.close_position('X/USDT',side)
                    kw=obj.exchange.create_market_order.call_args.kwargs
                    self.assertEqual(kw['amount'],12)
                    self.assertEqual(kw['params']['positionSide'],side)
                    self.assertNotIn('reduceOnly',kw['params'])
                    self.assertEqual(kw['side'],'SELL' if side=='LONG' else 'BUY')
                    if name=='OrderExecutor':
                        obj.exchange.reset_mock()
                        obj.exchange.fetch_positions.return_value=[{'contracts':12,'side':side.lower(),'info':{'positionSide':'BOTH'}}]
                        obj.close_position('X/USDT',side)
                        kw=obj.exchange.create_market_order.call_args.kwargs
                        self.assertTrue(kw['params']['reduceOnly']); self.assertNotIn('positionSide',kw['params'])

    def test_common_cancel_error_classification(self):
        for path,name in [('common/trailing_stop_manager.py','TrailingStopManager'),('common/trailing_take_profit_manager.py','TrailingTakeProfitManager'),('common/ladder_take_profit_manager.py','LadderTakeProfitManager')]:
            cls=load(path,name)
            method=next(n for n in dir(cls) if n.startswith('_cancel') and 'algo' in n)
            for code in (-2011,-2013,-20124,-2014,-2015,-1021):
                with self.subTest(path=path,code=code):
                    obj=object.__new__(cls); obj.symbol='X/USDT'
                    obj._get_api_credentials=lambda:('offline','offline')
                    obj._generate_signature=lambda *a:'not-a-signature'
                    fn=getattr(obj,method)
                    fn.__func__.__globals__.update(time=time,requests=Mock(delete=Mock(return_value=Mock(status_code=400,json=Mock(return_value={'code':code,'msg':'fixture'})))))
                    self.assertEqual(fn('fake'),code in (-2011,-2013,-20124))

    def test_numpy_stop_price_fallback(self):
        import numpy as np
        for path,name in [('common/trailing_stop_manager.py','TrailingStopManager'),('common/trailing_take_profit_manager.py','TrailingTakeProfitManager'),('common/ladder_take_profit_manager.py','LadderTakeProfitManager'),('bit/common/trailing_stop_manager.py','TrailingStopManager')]:
            cls=load(path,name)
            tree=ast.parse((ROOT/path).read_text())
            methods=[n for c in tree.body if isinstance(c,ast.ClassDef) for n in c.body if isinstance(n,ast.FunctionDef)]
            target=next(n.name for n in methods if n.name.startswith('_create') and 'algo' not in n.name and 'self.exchange.create_order(' in ast.unparse(n))
            obj=object.__new__(cls); obj.symbol='X/USDT'; obj.exchange=Mock(create_order=Mock(return_value={'id':'fake'})); obj.position_data={}
            for n in dir(cls):
                if n.startswith('_create') and 'algo' in n:setattr(obj,n,lambda *a,**k:None)
            kwargs={}
            for n in inspect.signature(getattr(obj,target)).parameters:
                if 'price' in n:kwargs[n]=np.float64(100)
                elif n in ('amount','quantity','qty'):kwargs[n]=np.float64(1)
                elif n=='side':kwargs[n]='long'
                elif 'level' in n:kwargs[n]=1
            getattr(obj,target)(**kwargs)
            with self.subTest(path=path):
                kw=obj.exchange.create_order.call_args.kwargs
                self.assertIs(type(kw['params']['stopPrice']),float)

    def test_precision_api_fallback_unified_symbol(self):
        from unittest.mock import patch
        cls=load('common/precision_helper.py','PrecisionHelper'); h=cls()
        response=Mock(status_code=200,json=Mock(return_value={'symbols':[{'symbol':'XUSDT','filters':[{'filterType':'PRICE_FILTER','tickSize':'0.05'},{'filterType':'LOT_SIZE','stepSize':'0.001'}]}]}))
        with patch('requests.get',return_value=response) as request:
            self.assertEqual(h._fetch_precision_from_api('X/USDT:USDT',{'price':6,'quantity':2}),{'price':2,'quantity':3})
            request.assert_called_once()

if __name__ == '__main__':
    unittest.main(verbosity=2)
