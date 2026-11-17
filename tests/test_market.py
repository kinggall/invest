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

    def test_time_series_preserves_decreasing_net_buy_and_three_minute_change(self):
        results=[]
        for seconds,value in [(0,123),(60,150),(120,100),(180,180)]:
            self.client.foreign=value
            results.append(self.engine.market.flow(SYMBOL,self.now+timedelta(seconds=seconds)))
        self.assertEqual(results[2]['foreign_change'],-50)
        self.assertEqual(results[-1]['foreign_change'],80)
        self.assertEqual(results[-1]['foreign_change_3m'],57)
        self.assertEqual(results[-1]['comparison_3m_seconds'],180)
        self.assertEqual(len(results[-1]['history']),4)
        self.assertEqual(results[-1]['history'][2]['foreign_change'],-50)
        self.assertNotIn('history',results[-1]['history'][0])
        cached=self.engine.market.flow(SYMBOL,self.now+timedelta(seconds=181))
        self.assertEqual(len(cached['history']),4)


class DailyClient:
    account=Client.account
    def __init__(self):
        self.calls=[];self.symbol=SYMBOL
        self.rows=[dict(stck_bsop_date=day,stck_oprc='10000',stck_hgpr='10100',stck_lwpr='9900',stck_clpr='10050',acml_vol='1234') for day in ['20261116','20261113','20261112','20261113']]
    def request(self,path,tr,params):
        self.calls.append((path,tr,params))
        return {'output1':{'stck_shrn_iscd':self.symbol},'output2':self.rows}

class DailyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.client=DailyClient();self.engine=MacdEngine(Path(self.tmp.name)/'state.db',self.client)
        self.addCleanup(self.engine.db.close)
        self.now=datetime(2026,11,16,10,tzinfo=KST)

    def test_cutoff_excludes_future_rows_sorts_and_deduplicates(self):
        bars=self.engine.market.daily(SYMBOL,'2026-11-13',self.now)
        self.assertEqual([b['time'] for b in bars],['2026-11-12','2026-11-13'])
        path,tr,params=self.client.calls[0]
        self.assertTrue(path.endswith('inquire-daily-itemchartprice'))
        self.assertEqual(tr,'FHKST03010100')
        self.assertEqual(params['FID_INPUT_DATE_2'],'20261113')
        self.assertEqual(params['FID_ORG_ADJ_PRC'],'0')
        self.assertEqual(bars[-1]['volume'],1234)

    def test_live_unclosed_daily_bar_is_excluded(self):
        bars=self.engine.market.daily(SYMBOL,'2026-11-16',self.now)
        self.assertEqual(bars[-1]['time'],'2026-11-13')
        self.assertEqual(self.client.calls[0][2]['FID_INPUT_DATE_2'],'20261115')

    def test_future_date_is_blocked_before_request(self):
        with self.assertRaises(TradingError):self.engine.market.daily(SYMBOL,'2026-11-17',self.now)
        self.assertEqual(self.client.calls,[])

    def test_wrong_symbol_is_rejected(self):
        self.client.symbol='005930'
        with self.assertRaisesRegex(TradingError,'SYMBOL_MISMATCH'):
            self.engine.market.daily(SYMBOL,'2026-11-13',self.now)

class MinuteBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.start=datetime(2026,11,16,9,tzinfo=KST);self.calls=[];self.fail_at=None
        owner=self
        class Broker:
            account=Client.account
            def request(self,path,tr,params):
                owner.calls.append(params['FID_INPUT_HOUR_1'])
                if owner.fail_at==len(owner.calls):raise TradingError('API_RATE_LIMIT [EGW00201]')
                cursor=datetime.strptime('20261116'+params['FID_INPUT_HOUR_1'],'%Y%m%d%H%M%S').replace(tzinfo=KST)
                rows=[]
                for i in range(30):
                    stamp=cursor-timedelta(minutes=i)
                    if stamp<owner.start:break
                    rows.append(dict(stck_bsop_date='20261116',stck_cntg_hour=stamp.strftime('%H%M%S'),stck_oprc='10000',stck_hgpr='10010',stck_lwpr='9990',stck_prpr='10000',cntg_vol='1'))
                return dict(output2=rows)
        self.engine=MacdEngine(Path(self.tmp.name)/'state.db',Broker());self.addCleanup(self.engine.db.close)
    def test_bootstrap_is_bounded_and_resumes_before_cached_earliest_minute(self):
        now=self.start+timedelta(hours=6)
        first=self.engine.market.minutes(SYMBOL,now)
        self.assertEqual(len(self.calls),4)
        first_oldest=min(r['time'] for r in first)
        self.calls.clear();second=self.engine.market.minutes(SYMBOL,now)
        self.assertEqual(len(self.calls),4)
        self.assertLess(min(r['time'] for r in second),first_oldest)
        self.assertLess(self.calls[1],datetime.fromisoformat(first_oldest).strftime('%H%M%S'))
    def test_failure_mid_bootstrap_keeps_partial_cache_and_next_attempt_backfills(self):
        now=self.start+timedelta(hours=6);self.fail_at=2
        with self.assertRaises(TradingError):self.engine.market.minutes(SYMBOL,now)
        first=self.engine.db.execute('SELECT min(stamp) FROM minutes WHERE symbol=?',(SYMBOL,)).fetchone()[0]
        self.fail_at=None;self.calls.clear()
        rows=self.engine.market.minutes(SYMBOL,now)
        self.assertLess(min(r['time'] for r in rows),first)
        self.assertEqual(len(self.calls),4)
    def test_complete_after_hours_cache_does_not_repeat_fourteen_pages(self):
        now=self.start.replace(hour=16)
        for _ in range(5):self.engine.market.minutes(SYMBOL,now)
        self.calls.clear();self.engine.market.minutes(SYMBOL,now)
        self.assertEqual(len(self.calls),1)
