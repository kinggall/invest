from datetime import datetime,timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from oi import OIService,candidates
from trader import KST,TradingError,ReadOnlyDerivativesClient,OI_EXPIRIES,OI_OPTIONS,OI_INDEX,OI_FUTURES,ORDER,REAL_BASE,transport

class Quotes:
    def __init__(self):self.calls=[];self.extra=0;self.no_spot=False;self.no_future=False
    def request(self,path,tr,params,post=False):
        self.calls.append((path,tr,params,post))
        if path==OI_EXPIRIES:return dict(output=[{'mtrt_yymm_code':m,'mtrt_yymm':m} for m in ['202611','202612','202701','W20261116']])
        if path==OI_INDEX:
            if self.no_spot:raise TradingError('INDEX_UNAVAILABLE')
            return dict(output={'bstp_nmix_prpr':'100'})
        if path==OI_FUTURES:
            if self.no_future:raise TradingError('FUTURE_UNAVAILABLE')
            return dict(output=[dict(futs_shrn_iscd='101Z12',hts_kor_isnm='코스피200 선물',futs_prpr='102',acml_vol='1000',hts_rmnn_dynu='24',hts_otst_stpl_qty='3000'),
                dict(futs_shrn_iscd='101Z03',hts_kor_isnm='코스피200 다음월물',futs_prpr='105',acml_vol='500',hts_rmnn_dynu='100',hts_otst_stpl_qty='5000')])
        if path==OI_OPTIONS:
            def row(strike,oi,side):return dict(acpr=str(strike),hts_otst_stpl_qty=str(oi+(self.extra if strike in [97.5,102.5] else 0)),optn_shrn_iscd=side+str(strike),otst_stpl_qty_icdc='8',acml_vol='123')
            return dict(output1=[row(k,v,'C') for k,v in [(95,9999),(100,4000),(102.5,200),(105,100),(110,99999)]],
                        output2=[row(k,v,'P') for k,v in [(90,99999),(95,100),(97.5,300),(100,4000),(102.5,9000)]])
        raise AssertionError(path)

class OITests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.quotes=Quotes();self.service=OIService(self.root,self.root/'state.db',self.quotes)
        self.addCleanup(self.service.close);self.now=datetime(2026,11,16,10,tzinfo=KST)

    def collect(self,**kwargs):return self.service.collect(dict(expiry='',radius_pct=5),now=kwargs.get('now',self.now))

    def test_monthly_same_expiry_bounded_candidates_and_basis(self):
        result=self.collect()
        self.assertEqual(result['expiry'],'202612') # November's standard monthly expiry has passed.
        self.assertEqual(result['put_wall']['strike'],97.5)
        self.assertEqual(result['call_wall']['strike'],102.5)
        self.assertEqual(result['put_wall']['projected_future_level'],99.5)
        self.assertEqual(result['call_wall']['projected_future_level'],104.5)
        self.assertEqual(result['future']['code'],'101Z12')
        self.assertEqual(result['basis'],2)
        self.assertAlmostEqual(result['put_wall']['local_share_pct'],75)
        params=next(c[2] for c in self.quotes.calls if c[0]==OI_OPTIONS)
        self.assertEqual(params['FID_MTRT_CNT'],'202612')
        self.assertTrue(all(not call[3] for call in self.quotes.calls))
        self.assertIsNone(result['source_time'])

    def test_cache_does_not_create_fake_observations(self):
        first=self.collect();calls=len(self.quotes.calls)
        cached=self.collect(now=self.now+timedelta(seconds=59))
        self.assertTrue(cached['cached']);self.assertEqual(len(self.quotes.calls),calls)
        self.assertEqual(len(cached['history']),1)
        self.assertEqual(cached['observed_at'],first['observed_at'])

    def test_observed_change_is_distinct_from_api_change_and_expiry_resets(self):
        self.collect();self.quotes.extra=10
        current=self.collect(now=self.now+timedelta(seconds=60))
        self.assertEqual(current['put_wall']['observed_change'],10)
        self.assertEqual(current['put_wall']['api_change'],8)
        self.assertEqual(current['comparison_seconds'],60)
        self.assertEqual(len(current['history']),2)
        other=self.service.collect(dict(expiry='202701',radius_pct=5),self.now+timedelta(seconds=120))
        self.assertIsNone(other['put_wall']['observed_change'])
        self.assertIsNone(other['comparison_at'])

    def test_missing_spot_does_not_invent_walls(self):
        self.quotes.no_spot=True
        result=self.collect()
        self.assertIsNone(result['spot']);self.assertIsNone(result['basis'])
        self.assertIsNone(result['put_wall']);self.assertIsNone(result['call_wall'])
        self.assertTrue(result['calls']);self.assertIn('INDEX_UNAVAILABLE',result['warnings'])

    def test_missing_futures_preserves_index_strikes_without_projection(self):
        self.quotes.no_future=True
        result=self.collect()
        self.assertEqual(result['put_wall']['strike'],97.5)
        self.assertIsNone(result['put_wall']['projected_future_level'])
        self.assertIsNone(result['future'])

    def test_candidates_exclude_atm_wrong_side_zero_and_far_away(self):
        rows=[{'strike':k,'oi':v} for k,v in [(90,9999),(95,0),(97.5,10),(100,99999),(102.5,88888)]]
        self.assertEqual([r['strike'] for r in candidates(rows,100,'put',5,None)],[97.5])

    def test_existing_real_token_is_unchanged_and_orders_are_blocked(self):
        (self.root/'kis_config.json').write_text(json.dumps({'profiles':{'real':{'app_key':'key','app_secret':'secret'}}}))
        path=self.root/'kis_token_real.json'
        path.write_text(json.dumps(dict(binding=hashlib.sha256(b'real\0key\0secret').hexdigest(),token='cached',expires=9999999999)))
        before=path.read_bytes();calls=[]
        def send(*args):calls.append(args);return {'rt_cd':'0','output':[]}
        with patch.dict('os.environ',{},clear=True):
            client=ReadOnlyDerivativesClient(self.root,send,0)
            client.request(OI_EXPIRIES,'FHPIO056104C0',{})
            with self.assertRaises(TradingError):client.request(ORDER,'TTTC0012U',{},True)
            with self.assertRaises(TradingError):client.request(OI_OPTIONS,'FHPIF05030100',{},True)
        self.assertEqual(len(calls),1);self.assertEqual(path.read_bytes(),before)
        with self.assertRaises(TradingError):transport(OI_OPTIONS,{}, {},True,base=REAL_BASE)
