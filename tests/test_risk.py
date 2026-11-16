from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
from pathlib import Path
import tempfile
import threading
import unittest
from macd_strategy import MacdEngine,DEFAULT,SYMBOL
from trader import KST,TradingError

class Broker:
    account={'CANO':'12345678','ACNT_PRDT_CD':'01'}
    def __init__(self):self.price=14800;self.sent=[];self.replies={};self.fail=False;self.barrier=None;self.lock=threading.Lock()
    def quote(self,symbol):
        if self.barrier:self.barrier.wait(3)
        return self.price
    def fills(self,row):return self.replies.get(row['order_no'],(row['status'],row['filled'],row['filled_cash']))
    def order(self,cfg):
        with self.lock:self.sent.append(cfg);number='stop'+str(len(self.sent))
        if self.fail:raise TradingError('REQUEST_UNCERTAIN')
        return number,'001'

class RiskTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'state.db';self.broker=Broker();self.engine=MacdEngine(self.path,self.broker)
        self.addCleanup(self.engine.db.close)
        self.cfg=dict(DEFAULT,stop_loss_enabled=True,stop_loss_pct=1)
        self.now=datetime(2026,11,16,10,tzinfo=KST)

    def record(self,id='buy1',side='buy',qty=2,filled=2,cash=30000,status='FILLED',number=None):
        cfg=dict(id=id,symbol=SYMBOL,side=side,quantity=qty,limit_price=15000)
        self.engine.db.execute('INSERT INTO orders(run_id,scope,day,symbol,quantity,strategy,status,order_no,branch,filled,filled_cash) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (id,self.engine.scope,'20261116',SYMBOL,qty,json.dumps(cfg),status,number if number is not None else id,'001',filled,cash))

    def check(self,**kwargs):return self.engine.risk.check(self.cfg,True,self.now,**kwargs)

    def test_average_fill_price_trigger_and_market_order_deduplication(self):
        self.record(cash=30010);self.broker.price=14855
        watch=self.check();self.assertEqual(watch['status'],'WATCHING')
        self.assertEqual(watch['average_entry'],15005);self.assertAlmostEqual(watch['stop_price'],14854.95)
        self.broker.price=14850
        result=self.check();self.assertTrue(result['submitted'])
        self.assertEqual(self.broker.sent[0]['quantity'],2)
        self.assertEqual(self.broker.sent[0]['order_type'],'market')
        self.assertEqual(self.check()['status'],'WAIT_EXISTING_SELL')
        self.assertEqual(len(self.broker.sent),1)
        self.broker.replies['stop1']=('FILLED',2,29700)
        self.assertEqual(self.check()['status'],'COMPLETE')
        self.assertTrue(self.engine.risk.latched())
        self.engine.risk.reset();self.assertFalse(self.engine.risk.latched())

    def test_uncertain_order_blocks_retry_after_restart(self):
        self.record();self.broker.fail=True
        with self.assertRaisesRegex(TradingError,'UNCERTAIN'):self.check()
        other=MacdEngine(self.path,self.broker)
        self.addCleanup(other.db.close)
        self.broker.fail=False
        self.assertEqual(other.risk.check(self.cfg,True,self.now)['status'],'BLOCKED_UNKNOWN')
        self.assertEqual(len(self.broker.sent),1)

    def test_pending_sell_is_not_duplicated_and_missing_basis_is_not_invented(self):
        self.record();self.record('sell1','sell',2,0,None,'SUBMITTED')
        self.assertEqual(self.check()['status'],'WAIT_EXISTING_SELL')
        self.assertEqual(self.broker.sent,[])
        self.engine.db.execute("UPDATE orders SET status='CANCELLED' WHERE run_id='sell1'")
        self.engine.db.execute("UPDATE orders SET filled_cash=NULL WHERE run_id='buy1'")
        # Latch already owns the episode; test basis failure with a fresh, unlatched state.
        self.engine.db.execute('UPDATE risk_state SET latched=0')
        self.assertEqual(self.check()['status'],'BASIS_UNAVAILABLE')
        self.assertEqual(self.broker.sent,[])

    def test_partial_buy_remainders_are_liquidated_only_when_confirmed(self):
        self.record(qty=2,filled=1,cash=15000,status='PARTIAL')
        self.assertTrue(self.check()['submitted'])
        self.assertEqual(self.broker.sent[0]['quantity'],1)
        self.broker.replies['stop1']=('FILLED',1,14800)
        self.assertEqual(self.check()['status'],'WAIT_BUY_REMAINDER')
        self.broker.replies['buy1']=('FILLED',2,30000)
        self.broker.price=16000 # Latch still exits the remaining fill even after price recovery.
        self.assertTrue(self.check()['submitted'])
        self.assertEqual([o['quantity'] for o in self.broker.sent],[1,1])

    def test_disabled_observation_and_stop_callback_do_not_transmit(self):
        self.record()
        self.assertEqual(self.engine.risk.check(dict(self.cfg,stop_loss_enabled=False),True,self.now)['status'],'DISABLED')
        self.assertEqual(self.engine.risk.check(self.cfg,False,self.now)['status'],'WOULD_STOP')
        self.assertEqual(self.check(allow_submit=lambda:False)['status'],'STOPPED')
        self.assertFalse(self.engine.risk.latched());self.assertEqual(self.broker.sent,[])

    def test_concurrent_monitors_reserve_only_one_exit(self):
        self.record();self.broker.barrier=threading.Barrier(2)
        def run():
            engine=MacdEngine(self.path,self.broker)
            try:return engine.risk.check(self.cfg,True,self.now)['status']
            finally:engine.db.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:run(),range(2)))
        self.assertEqual(results.count('SUBMITTED'),1)
        self.assertEqual(len(self.broker.sent),1)

    def test_unrelated_symbol_does_not_block_protective_exit(self):
        self.record()
        cfg=json.dumps(dict(side='buy',limit_price=10000))
        self.engine.db.execute("INSERT INTO orders(run_id,scope,day,symbol,quantity,strategy,status,order_no,branch,filled) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ('other',self.engine.scope,'20261116','005930',1,cfg,'SUBMITTED','other','001',0))
        original=self.broker.fills
        def checked(row):
            if row['symbol']!='069500':raise AssertionError('Unrelated symbol queried')
            return original(row)
        self.broker.fills=checked
        self.assertTrue(self.check()['submitted'])

    def test_invalid_stop_percent_is_rejected(self):
        from risk import stop_percent
        for value in (0,-1,21,float('nan'),float('inf'),True,'1%'):
            with self.assertRaises(TradingError):stop_percent(value)
