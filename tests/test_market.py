from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from macd_strategy import MacdEngine, SYMBOL
from trader import KST, TradingError
from market import FLOW

class Client:
    account={'CANO':'12345678','ACNT_PRDT_CD':'01'}
    def __init__(self):self.calls=[];self.foreign=123;self.fail=False
    def request(self,path,tr,params):
        self.calls.append((path,tr,params))
        if self.fail:raise TradingError('QUERY_FAILED')
        return {'output':[{'frgn_ntby_tr_pbmn':str(self.foreign),'orgn_ntby_tr_pbmn':'-12'}]}

class FlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.client=Client();self.engine=MacdEngine(Path(self.tmp.name)/'state.db',self.client)
        self.addCleanup(self.engine.db.close)
        self.now=datetime(2026,11,16,10,tzinfo=KST)

    def test_kospi_market_scope_and_60_second_sampling(self):
        first=self.engine.market.flow(SYMBOL,self.now)
        self.engine.market.flow(SYMBOL,self.now+timedelta(seconds=59))
        self.assertEqual(len(self.client.calls),1)
        self.assertEqual(self.client.calls[0],(FLOW,'FHPTJ04030000',{'FID_INPUT_ISCD':'999','FID_INPUT_ISCD_2':'S001'}))
        self.client.foreign=150
        second=self.engine.market.flow(SYMBOL,self.now+timedelta(seconds=60))
        self.assertEqual(len(self.client.calls),2)
        self.assertEqual(second['foreign_change'],27)
        self.assertIsNone(first['source_time'])
        self.assertIsNone(first['foreign_change'])

    def test_failed_flow_is_missing_and_next_day_has_no_intraday_delta(self):
        self.engine.market.flow(SYMBOL,self.now)
        tomorrow=self.engine.market.flow(SYMBOL,self.now+timedelta(days=1))
        self.assertIsNone(tomorrow['foreign_change'])
        self.client.fail=True
        result=self.engine.market.flow(SYMBOL,self.now+timedelta(days=1,minutes=1))
        self.assertIsNone(result['foreign'])
        self.assertEqual(result['error'],'QUERY_FAILED')
