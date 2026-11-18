from datetime import datetime,timedelta
import hashlib
import json
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from macd_strategy import MacdEngine,DEFAULT,SYMBOL
from trader import KST,HISTORY_PATH,ReadOnlyHistoryClient,SharedToken,TradingError,transport,ORDER,REAL_BASE
from history import History

class Account:
    account={'CANO':'12345678','ACNT_PRDT_CD':'01'}

class HistorySource:
    def __init__(self):self.calls=[]
    def request(self,path,tr,params,post=False):
        self.calls.append((path,tr,params,post))
        date=datetime.strptime(params['FID_INPUT_DATE_1'],'%Y%m%d').replace(tzinfo=KST)
        cursor=datetime.strptime(params['FID_INPUT_HOUR_1'],'%H%M%S').time()
        rows=[]
        for i in range(390):
            stamp=date.replace(hour=9)+timedelta(minutes=i)
            if stamp.time()>cursor:continue
            price=15000+i
            rows.append(dict(stck_bsop_date=params['FID_INPUT_DATE_1'],stck_cntg_hour=stamp.strftime('%H%M%S'),
                stck_oprc=str(price),stck_hgpr=str(price+5),stck_lwpr=str(price-5),stck_prpr=str(price),cntg_vol='100'))
        return dict(rt_cd='0',output1={'stck_shrn_iscd':SYMBOL},output2=list(reversed(rows))[:120])

class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name)
        self.engine=MacdEngine(self.home/'state.db',Account())
        self.addCleanup(self.engine.db.close)
        self.source=HistorySource();self.history=History(self.engine,self.source)

    def test_paginated_historical_minutes_aggregate_and_use_prior_day_warmup(self):
        result=self.history.load('2026-11-13',DEFAULT,datetime(2026,11,15,tzinfo=KST))
        self.assertEqual(len(result['chart']),130)
        self.assertEqual(result['missing_bars'],[])
        self.assertEqual(result['warmup_date'],'2026-11-12')
        self.assertIsNotNone(result['chart'][0]['signal'])
        self.assertTrue(all(row['time'].startswith('2026-11-13') for row in result['chart']))
        self.assertTrue(all(not call[3] and call[0]==HISTORY_PATH for call in self.source.calls))
        before=len(self.source.calls)
        self.history.load('2026-11-13',DEFAULT,datetime(2026,11,15,tzinfo=KST))
        self.assertEqual(len(self.source.calls),before)

    def test_future_and_weekend_dates_rejected_without_network(self):
        for date in ['2026-11-16','2026-11-14']:
            with self.assertRaises(TradingError):self.history.load(date,DEFAULT,datetime(2026,11,15,tzinfo=KST))
        self.assertEqual(self.source.calls,[])

    def test_existing_real_cache_is_read_only_and_cannot_send_orders(self):
        config={'profiles':{'real':{'app_key':'key','app_secret':'secret'}}}
        (self.home/'kis_config.json').write_text(json.dumps(config))
        cached={'binding':hashlib.sha256(b'real\0key\0secret').hexdigest(),'token':'cached','expires':9999999999}
        path=self.home/'kis_token_real.json';path.write_text(json.dumps(cached));before=path.read_bytes()
        calls=[]
        def send(*args):calls.append(args);return {'rt_cd':'0'}
        with patch.dict('os.environ',{},clear=True):
            client=ReadOnlyHistoryClient(self.home,send,0)
            client.request(HISTORY_PATH,'FHKST03010230',{})
            with self.assertRaises(TradingError):client.request(ORDER,'TTTC0012U',{},True)
        self.assertEqual(path.read_bytes(),before)
        self.assertEqual(len(calls),1)
        self.assertFalse(calls[0][3])
        with self.assertRaisesRegex(TradingError,'REAL_ROUTE_BLOCKED'):
            transport(ORDER,{}, {},True,base=REAL_BASE)


    def test_current_day_rejection_uses_current_api_and_warmup_failure_is_nonfatal(self):
        from trader import INTRADAY_HISTORY_PATH
        original=self.source.request
        def request(path,tr,params,post=False):
            if path==HISTORY_PATH:raise TradingError('HISTORY_QUERY_REJECTED / CODE=OPSQ0008')
            self.assertEqual(path,INTRADAY_HISTORY_PATH);self.assertEqual(tr,'FHKST03010200')
            self.assertNotIn('FID_INPUT_DATE_1',params)
            result=original(HISTORY_PATH,'FHKST03010230',dict(params,FID_INPUT_DATE_1='20261118'))
            result['output2']=result['output2'][:30]
            for row in result['output2']:row.pop('stck_bsop_date')
            return result
        self.source.request=request
        result=self.history.load('2026-11-18',DEFAULT,datetime(2026,11,18,18,tzinfo=KST))
        self.assertEqual(len(result['chart']),130)
        self.assertEqual(result['missing_bars'],[])
        self.assertTrue(any('당일 분봉 API' in s for s in result['warnings']))
        self.assertTrue(any('지표 준비용' in s for s in result['warnings']))
        self.assertEqual(result['chart'][-1]['time'][11:16],'15:27')

    def test_past_date_failure_never_falls_back_to_today_prices(self):
        def request(path,tr,params,post=False):
            self.assertEqual(path,HISTORY_PATH)
            raise TradingError('HISTORY_QUERY_REJECTED / CODE=OPSQ0008')
        self.source.request=request
        with self.assertRaisesRegex(TradingError,'2026-11-13 15:30'):
            self.history.day(datetime(2026,11,13).date(),now=datetime(2026,11,18,18,tzinfo=KST))

    def test_optional_previous_session_failure_does_not_hide_current_day(self):
        original=self.source.request
        def request(path,tr,params,post=False):
            if params['FID_INPUT_DATE_1']=='20261112':raise TradingError('HISTORY_QUERY_REJECTED / CODE=OPSQ0008')
            return original(path,tr,params,post)
        self.source.request=request
        result=self.history.load('2026-11-13',DEFAULT,datetime(2026,11,18,18,tzinfo=KST))
        self.assertEqual(len(result['chart']),130)
        self.assertIsNone(result['warmup_date'])
        self.assertTrue(any('2026-11-12' in s for s in result['warnings']))



    def test_intraday_history_extension_remains_read_only(self):
        from trader import INTRADAY_HISTORY_PATH
        client=ReadOnlyHistoryClient(self.home)
        for path,tr,post in [(INTRADAY_HISTORY_PATH,'FHKST03010200',True),(INTRADAY_HISTORY_PATH,'wrong',False),('/oauth2/tokenP',None,False),(None,None,False)]:
            with self.assertRaisesRegex(TradingError,'READ_ONLY_HISTORY_ROUTE_BLOCKED'):
                client.request(path,tr,{},post=post)



    def test_rejected_1019_page_keeps_downloaded_afternoon_and_merges_local_morning(self):
        from trader import INTRADAY_HISTORY_PATH
        date=datetime(2026,11,18,18,tzinfo=KST)
        start=date.replace(hour=9,minute=0)
        for i in range(228):
            stamp=start+timedelta(minutes=i);price=15000+i
            row=dict(time=stamp.isoformat(),open=price,high=price+5,low=price-5,close=price,volume=100)
            self.engine.db.execute('INSERT OR REPLACE INTO minutes VALUES (?,?,?)',(SYMBOL,row['time'],json.dumps(row)))
        self.engine.db.commit()
        original=self.source.request;requests=[]
        def request(path,tr,params,post=False):
            requests.append((path,params['FID_INPUT_HOUR_1']))
            if params['FID_INPUT_HOUR_1']<='101900':raise TradingError('HISTORY_QUERY_REJECTED / CODE=OPSQ0008')
            query=dict(params,FID_INPUT_DATE_1='20261118')
            result=original(HISTORY_PATH,'FHKST03010230',query)
            result['output2']=[row for row in result['output2'] if row['stck_cntg_hour']>='102000']
            return result
        self.source.request=request
        rows=self.history.day(date.date(),now=date)
        self.assertEqual(len(rows),390)
        self.assertEqual(rows[0]['time'][11:16],'09:00')
        self.assertEqual(rows[-1]['time'][11:16],'15:29')
        self.assertIn((INTRADAY_HISTORY_PATH,'101900'),requests)
        self.assertTrue(any('추가 조회 실패' in w for w in self.history.warnings))
        self.assertFalse(self.history.last_api_success)
        # Closing the connection must not discard the afternoon checkpoints.
        import sqlite3
        check=sqlite3.connect(self.home/'state.db')
        self.assertTrue(check.execute("SELECT 1 FROM minutes WHERE stamp='2026-11-18T15:19:00+09:00'").fetchone());check.close()

    def test_first_page_rejection_uses_existing_partial_prices_with_warning(self):
        date=datetime(2026,11,18,18,tzinfo=KST)
        row=dict(time='2026-11-18T12:30:00+09:00',open=100,high=100,low=100,close=100,volume=1)
        self.engine.db.execute('INSERT INTO minutes VALUES (?,?,?)',(SYMBOL,row['time'],json.dumps(row)));self.engine.db.commit()
        def request(*args,**kwargs):raise TradingError('HISTORY_QUERY_REJECTED / CODE=OPSQ0008')
        self.source.request=request
        rows=self.history.day(date.date(),now=date)
        self.assertEqual(rows,[row])
        self.assertTrue(self.history.warnings)
        self.assertFalse(self.engine.db.execute('SELECT 1 FROM history_days').fetchone())

if __name__=='__main__':unittest.main()
