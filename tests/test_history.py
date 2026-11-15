from datetime import datetime,timedelta
import hashlib
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
