from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from macd_strategy import MacdEngine, DEFAULT, SYMBOL
from signals import aggregate, macd
from trader import KST, TradingError

class Client:
    account={'CANO':'12345678','ACNT_PRDT_CD':'01'}
    def __init__(self):self.orders=[];self.completed=False;self.fail=False
    def quote(self,symbol):return 10000
    def order(self,strategy):
        self.orders.append(strategy)
        if self.fail:raise TradingError('REQUEST_UNCERTAIN')
        return str(len(self.orders)),'001'
    def fills(self,row):return ('FILLED',row['quantity']) if self.completed else ('PARTIAL',0)

class Data:
    def __init__(self,engine):self.rows=[];self.supply_error=False
    def minutes(self,symbol,now):return self.rows
    def flow(self,symbol,now):return dict(foreign=None if self.supply_error else 10,institution=-5,error='UNAVAILABLE' if self.supply_error else None,observed_at=now.isoformat())

class StrategyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.client=Client();self.engine=MacdEngine(Path(self.tmp.name)/'state.db',self.client,Data)
        self.addCleanup(self.engine.db.close)
        self.cfg=dict(DEFAULT,fast=3,slow=5,signal=3,max_notional=50000)
        self.start=datetime(2026,11,16,9,tzinfo=KST)
        closes=[10000+i*10 for i in range(20)]+[10200-i*20 for i in range(20)]+[9800+i*30 for i in range(20)]+[10400-i*20 for i in range(20)]+[10000+i*30 for i in range(20)]
        self.rows=[]
        for index,close in enumerate(closes):
            for minute in range(3):
                self.rows.append(dict(time=(self.start+timedelta(minutes=3*index+minute)).isoformat(),open=close,high=close+5,low=close-5,close=close,volume=1))
        points=macd(aggregate(self.rows,self.start+timedelta(minutes=300)),3,5,3)
        self.crosses=[(i,p['cross']) for i,p in enumerate(points) if p['cross']]
        self.buy1=next(i for i,side in self.crosses if side=='buy')
        self.sell=next(i for i,side in self.crosses if side=='sell' and i>self.buy1)
        self.buy2=next(i for i,side in self.crosses if side=='buy' and i>self.sell)

    def evaluate(self,index,execute=True,**changes):
        now=self.start+timedelta(minutes=(index+1)*3,seconds=5)
        self.engine.market.rows=self.rows[:(index+1)*3]
        return self.engine.evaluate(self.cfg,execute,now=now+timedelta(**changes))

    def test_buy_sell_reentry_same_day_uses_confirmed_position(self):
        self.assertTrue(self.evaluate(self.buy1)['submitted'])
        self.assertEqual(self.evaluate(self.buy1)['status'],'ORDER_PENDING')
        self.client.completed=True
        self.assertTrue(self.evaluate(self.sell)['submitted'])
        self.assertTrue(self.evaluate(self.buy2)['submitted'])
        self.assertEqual([r['side'] for r in self.client.orders],['buy','sell','buy'])
        self.assertEqual(len(set(r['id'] for r in self.client.orders)),3)

    def test_preview_does_not_consume_signal_and_records_supply(self):
        self.assertEqual(self.evaluate(self.buy1,False)['status'],'BUY_SIGNAL')
        self.assertEqual(self.client.orders,[])
        event=json.loads(self.engine.db.execute('SELECT value FROM signals').fetchone()[0])
        self.assertEqual(event['flow']['foreign'],10)
        self.assertTrue(self.evaluate(self.buy1)['submitted'])

    def test_unknown_order_blocks_retry_even_after_restart(self):
        self.client.fail=True
        with self.assertRaises(TradingError):self.evaluate(self.buy1)
        self.engine.db.close()
        self.engine=MacdEngine(Path(self.tmp.name)/'state.db',self.client,Data)
        self.addCleanup(self.engine.db.close)
        self.assertEqual(self.evaluate(self.buy1)['status'],'ORDER_PENDING')
        self.assertEqual(len(self.client.orders),1)

    def test_warmup_stale_and_weekend_cannot_submit(self):
        self.assertEqual(self.evaluate(2)['status'],'WARMUP')
        self.assertEqual(self.evaluate(self.buy1,seconds=100)['status'],'STALE_BAR')
        self.assertEqual(self.evaluate(self.buy1,days=6)['status'],'MARKET_CLOSED')
        self.assertEqual(self.client.orders,[])

    def test_flat_position_does_not_sell(self):
        sell=next(i for i,side in self.crosses if side=='sell')
        self.assertEqual(self.evaluate(sell)['status'],'POSITION_HOLD')
        self.assertEqual(self.client.orders,[])

    def test_supply_is_observation_only_and_missing_is_not_zero(self):
        self.engine.market.supply_error=True
        result=self.evaluate(self.buy1)
        self.assertTrue(result['submitted']);self.assertIsNone(result['flow']['foreign'])

    def test_notional_limit_and_daily_count(self):
        self.cfg['max_notional']=100
        self.assertEqual(self.evaluate(self.buy1)['status'],'MAX_NOTIONAL_EXCEEDED')
        self.cfg['max_notional']=50000;self.cfg['max_daily_buys']=1
        self.evaluate(self.buy1);self.client.completed=True
        self.assertTrue(self.evaluate(self.sell)['submitted'])
        self.assertEqual(self.evaluate(self.buy2)['status'],'DAILY_LIMIT')

    def test_missing_recent_bar_resets_warmup(self):
        index=self.buy1
        self.engine.market.rows=self.rows[:(index-2)*3]+self.rows[(index-1)*3:(index+1)*3]
        now=self.start+timedelta(minutes=(index+1)*3,seconds=5)
        result=self.engine.evaluate(self.cfg,True,now)
        self.assertEqual(result['status'],'WARMUP')
        self.assertEqual(self.client.orders,[])

    def test_partial_fill_blocks_exit_and_sell_ignores_entry_notional_cap(self):
        self.cfg['quantity']=2
        self.evaluate(self.buy1)
        self.client.fills=lambda row:('PARTIAL',1)
        result=self.evaluate(self.sell)
        self.assertEqual(result['position'],1)
        self.assertEqual(result['status'],'ORDER_PENDING')
        self.client.fills=lambda row:('FILLED',row['quantity'])
        self.cfg['max_notional']=1
        result=self.evaluate(self.sell)
        self.assertTrue(result['submitted'])
        self.assertEqual(self.client.orders[-1]['quantity'],2)

    def test_stopped_inflight_evaluation_cannot_submit(self):
        index=self.buy1
        self.engine.market.rows=self.rows[:(index+1)*3]
        now=self.start+timedelta(minutes=(index+1)*3,seconds=5)
        result=self.engine.evaluate(self.cfg,True,now,allow_submit=lambda:False)
        self.assertEqual(result['status'],'STOPPED')
        self.assertEqual(self.client.orders,[])
