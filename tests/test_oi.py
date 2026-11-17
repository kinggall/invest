from datetime import datetime,timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from oi import OIService,candidates,first_candidates,monthly_expiry,nearest_month,parse_index_master,parse_future_master
from trader import KST,TradingError,ReadOnlyDerivativesClient,OI_EXPIRIES,OI_OPTIONS,OI_INDEX,OI_FUTURES,OI_PRICE,FUTURES_MINUTES,ORDER,REAL_BASE,transport

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
        futures=patch('oi.load_future_master',side_effect=TradingError('OI_MASTER_UNAVAILABLE'));futures.start();self.addCleanup(futures.stop)
        metadata=patch('oi.load_index_master',side_effect=TradingError('OI_MASTER_UNAVAILABLE'));metadata.start();self.addCleanup(metadata.stop)
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.quotes=Quotes();self.service=OIService(self.root,self.root/'state.db',self.quotes)
        self.addCleanup(self.service.close);self.now=datetime(2026,11,16,10,tzinfo=KST)

    def collect(self,**kwargs):return self.service.collect(dict(expiry='',radius_pct=5),now=kwargs.get('now',self.now))

    def test_monthly_parser_handles_padding_and_encoded_code(self):
        for row in [dict(mtrt_yymm_code='202612  ',mtrt_yymm='2026/12'),dict(mtrt_yymm_code='Z12',mtrt_yymm='202612'),dict(mtrt_yymm_code='Z12 ',mtrt_yymm='2026년 12월')]:
            self.assertEqual(monthly_expiry(row),'202612')
        for row in [dict(mtrt_yymm_code='W202612',mtrt_yymm='202612'),dict(mtrt_yymm_code='Z12',mtrt_yymm='202612 위클리'),dict(mtrt_yymm_code='202612',mtrt_yymm='미니'),dict(mtrt_yymm_code='Z11',mtrt_yymm='202612'),None]:
            self.assertIsNone(monthly_expiry(row))
        self.assertEqual(nearest_month(datetime(2026,12,16,tzinfo=KST)),'202701')

    def test_empty_month_list_uses_direct_monthly_chain_not_weekly(self):
        original=self.quotes.request
        def request(path,tr,params,post=False):
            if path==OI_EXPIRIES:return dict(output=[dict(mtrt_yymm_code='W20261116',mtrt_yymm='주간')])
            return original(path,tr,params,post)
        self.quotes.request=request
        result=self.collect()
        self.assertEqual(result['expiry'],'202612')
        self.assertTrue(any('직접 조회' in w for w in result['warnings']))
        self.assertEqual(next(c[2] for c in self.quotes.calls if c[0]==OI_OPTIONS)['FID_MTRT_CNT'],'202612')

    def test_empty_month_list_does_not_publish_an_empty_chain(self):
        original=self.quotes.request
        def request(path,tr,params,post=False):
            if path==OI_EXPIRIES:return dict(output=[])
            if path==OI_OPTIONS:return dict(output1=[],output2=[])
            return original(path,tr,params,post)
        self.quotes.request=request
        with self.assertRaisesRegex(TradingError,'콜·풋 OI 자료가 부족'):
            self.collect()
        self.assertEqual(self.service.db.execute('SELECT COUNT(*) FROM oi_snapshots').fetchone()[0],0)

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
        with patch('oi.load_future_master',side_effect=TradingError('OI_MASTER_UNAVAILABLE')):
            result=self.collect()
        self.assertEqual(result['put_wall']['strike'],97.5)
        self.assertIsNone(result['put_wall']['projected_future_level'])
        self.assertIsNone(result['future'])

    def test_official_future_master_rejects_mini_spreads_and_other_indices(self):
        text='\n'.join(['1|A01612|KRX|F 202612| |0|1|2001|KOSPI200','1|A01703|KRX|F 202703| |0|1|2001|KOSPI200','B|A05612|KRX|미니F 202612| |0|1|2001|KOSPI200','2|D0161201|KRX|SP 2612-2703| |0|1|2001|KOSPI200','3|A06612|KRX|코스닥150F 202612| |0|1|3003|KSQ150'])
        self.assertEqual([r['code'] for r in parse_future_master(text)],['A01612','A01703'])

    def test_board_missing_uses_exact_official_front_future_read_only_quote(self):
        self.quotes.no_future=True
        original=self.quotes.request
        def request(path,tr,params,post=False):
            if path==OI_PRICE:
                self.assertEqual(params,dict(FID_COND_MRKT_DIV_CODE='F',FID_INPUT_ISCD='A01612'))
                self.assertFalse(post)
                return dict(output1=dict(futs_prpr='102',hts_rmnn_dynu='23',acml_vol='100'))
            return original(path,tr,params,post)
        self.quotes.request=request
        with patch('oi.load_future_master',return_value=[dict(code='A01612',expiry='202612',name='F 202612')]):
            result=self.collect()
        self.assertEqual(result['future']['code'],'A01612')
        self.assertEqual(result['basis'],2)
        self.assertEqual(result['put_wall']['projected_future_level'],99.5)
        self.assertEqual(result['history'][0]['future_price'],102)

    def test_front_future_zero_quote_does_not_create_a_basis(self):
        self.quotes.no_future=True
        original=self.quotes.request
        def request(path,tr,params,post=False):
            if path==OI_PRICE:return dict(output1=dict(futs_prpr='0'))
            return original(path,tr,params,post)
        self.quotes.request=request
        with patch('oi.load_future_master',return_value=[dict(code='A01612',expiry='202612',name='F 202612')]):
            result=self.collect()
        self.assertIsNone(result['future']);self.assertIsNone(result['basis'])

    def test_candidates_exclude_atm_wrong_side_zero_and_far_away(self):
        rows=[{'strike':k,'oi':v} for k,v in [(90,9999),(95,0),(97.5,10),(100,99999),(102.5,88888)]]
        self.assertEqual([r['strike'] for r in candidates(rows,100,'put',5,None)],[97.5])

    def test_current_master_codes_keep_standard_futures_only(self):
        original=self.quotes.request
        def request(path,tr,params,post=False):
            if path==OI_FUTURES:
                return dict(output=[dict(futs_shrn_iscd=code,hts_kor_isnm='선물',futs_prpr='102',acml_vol=volume) for code,volume in [('A01612  ','100'),('A05612','99999'),('D0161201','99999'),('A06612','99999')]])
            return original(path,tr,params,post)
        self.quotes.request=request
        result=self.collect()
        self.assertEqual(result['future']['code'],'A01612')
        self.assertEqual(result['basis'],2)

    def test_master_parser_excludes_weekly_mini_and_other_indices(self):
        text='\n'.join([
            '5|B01612790|KRX|C 202612   790.0|2|00790.00| |2001|KOSPI200',
            '6|C01612790|KRX|P 202612   790.0|2|00790.00| |2001|KOSPI200',
            'D|B05612790|KRX|미니C 202612   790.0|2|00790.00| |2001|KOSPI200',
            'L|B09FLW790|KRX|위클리C 2611W3   790.0|2|00790.00| |2001|KOSPI200',
            '5|B01612790|KRX|C 202612   790.0|2|00790.00| |3003|KSQ150'])
        rows=parse_index_master(text)
        self.assertEqual(len(rows),2)
        self.assertEqual({r['side'] for r in rows},{'call','put'})
        self.assertTrue(all(r['expiry']=='202612' and r['strike']==790 for r in rows))

    def configure_truncated_board(self,fail=False):
        original=self.quotes.request
        def request(path,tr,params,post=False):
            if path==OI_OPTIONS:
                return dict(output1=[dict(acpr='10',hts_otst_stpl_qty='500',optn_shrn_iscd='farcall')],output2=[dict(acpr='10',hts_otst_stpl_qty='500',optn_shrn_iscd='farput')])
            if path==OI_PRICE:
                self.quotes.calls.append((path,tr,params,post))
                if fail:raise TradingError('OI_QUOTE_REJECTED')
                strike=102.5 if params['FID_INPUT_ISCD'].startswith('B') else 97.5
                return dict(output1=dict(hts_otst_stpl_qty='456',otst_stpl_qty_icdc='-10',acml_vol='21'),output2=dict(acpr=str(strike)))
            return original(path,tr,params,post)
        self.quotes.request=request
        return [dict(side='call',code='B01612102',expiry='202612',strike=102.5),dict(side='put',code='C01612097',expiry='202612',strike=97.5)]

    def test_truncated_board_gets_nearby_oi_by_verified_monthly_contract(self):
        master=self.configure_truncated_board()
        with patch('oi.load_index_master',return_value=master):result=self.collect()
        self.assertEqual(result['put_wall']['oi'],456)
        self.assertEqual(result['call_wall']['strike'],102.5)
        self.assertEqual(result['individual_count'],2)
        self.assertTrue(result['coverage_complete'])
        self.assertEqual([c[1] for c in self.quotes.calls if c[0]==OI_PRICE],['FHMIF10000000']*2)
        self.assertTrue(all(not c[3] for c in self.quotes.calls))

    def test_individual_quote_failure_keeps_coverage_incomplete_without_fake_zero(self):
        master=self.configure_truncated_board(fail=True)
        with patch('oi.load_index_master',return_value=master):result=self.collect()
        self.assertFalse(result['coverage_complete'])
        self.assertIsNone(result['put_wall']);self.assertIsNone(result['call_wall'])
        self.assertEqual(result['individual_count'],0)
        self.assertEqual(len(result['puts']),0)
        self.assertTrue(any('개별 OI 조회 실패' in w for w in result['warnings']))

    def configure_incremental(self):
        master=[dict(side=side,code=('B' if side=='call' else 'C')+'01612'+str(index).zfill(3),expiry='202612',strike=96+index*.5)
                for index in range(16) for side in ('call','put')]
        by_code={r['code']:r for r in master}
        original=self.quotes.request
        def request(path,tr,params,post=False):
            if path==OI_PRICE:
                self.quotes.calls.append((path,tr,params,post))
                row=by_code[params['FID_INPUT_ISCD']]
                return dict(output1=dict(hts_otst_stpl_qty=str(100+int(row['strike'])+self.quotes.extra),acpr=str(row['strike'])))
            return original(path,tr,params,post)
        self.quotes.request=request
        return master

    def test_full_pass_fetches_every_target_and_skips_slow_boards(self):
        master=self.configure_incremental()
        with patch('oi.load_index_master',return_value=master):result=self.collect()
        self.assertEqual(result['individual_count'],32)
        self.assertEqual(result['sampling']['total'],32)
        self.assertEqual(result['sampling']['fresh'],32)
        self.assertTrue(result['coverage_complete'])
        self.assertFalse(any(c[0] in [OI_OPTIONS,OI_EXPIRIES,OI_FUTURES] for c in self.quotes.calls))
        self.assertEqual(len([c for c in self.quotes.calls if c[0]==OI_PRICE]),32)
        self.assertIsNotNone(result['put_wall']);self.assertIsNotNone(result['call_wall'])

    def test_full_pass_refreshes_every_row_across_service_restarts(self):
        master=self.configure_incremental()
        with patch('oi.load_index_master',return_value=master):
            self.collect();self.service.close()
            self.service=OIService(self.root,self.root/'state.db',self.quotes);self.addCleanup(self.service.close)
            self.quotes.extra=10
            second=self.collect(now=self.now+timedelta(seconds=60))
        rows=second['calls']+second['puts']
        self.assertEqual(len(rows),32);self.assertTrue(second['coverage_complete'])
        self.assertTrue(all(not r['reused'] and r['received_at']==(self.now+timedelta(seconds=60)).isoformat() for r in rows))
        self.assertTrue(all(r['observed_change']==10 and r['comparison_seconds']==60 for r in rows))

    def test_forced_refresh_bypasses_recent_cache(self):
        master=self.configure_incremental()
        with patch('oi.load_index_master',return_value=master):
            self.collect();before=len(self.quotes.calls)
            result=self.service.collect(dict(expiry='',radius_pct=5),now=self.now+timedelta(seconds=30),force=True)
        self.assertFalse(result['cached']);self.assertEqual(result['individual_count'],32)
        self.assertGreaterEqual(len(self.quotes.calls)-before,32)
        self.assertTrue(all(r['received_at']==(self.now+timedelta(seconds=30)).isoformat() for r in result['calls']+result['puts']))

    def test_failed_refresh_does_not_restore_old_rows(self):
        master=self.configure_incremental()
        with patch('oi.load_index_master',return_value=master):
            self.collect();original=self.quotes.request
            failed_code=master[0]['code']
            def request(path,tr,params,post=False):
                if path==OI_PRICE and params['FID_INPUT_ISCD']==failed_code:raise TradingError('TIMEOUT')
                return original(path,tr,params,post)
            self.quotes.request=request
            result=self.collect(now=self.now+timedelta(seconds=700))
        rows=result['calls']+result['puts']
        self.assertEqual(len(rows),31);self.assertFalse(any(r['code']==failed_code for r in rows))
        self.assertTrue(all(not r['reused'] and not r['stale'] for r in rows));self.assertFalse(result['coverage_complete'])

    def test_full_pass_rechecks_spot_and_blocks_shifted_universe(self):
        master=self.configure_incremental();original=self.quotes.request;indices=[0]
        def request(path,tr,params,post=False):
            if path==OI_INDEX:
                indices[0]+=1
                return dict(output=dict(bstp_nmix_prpr='100' if indices[0]==1 else '200'))
            return original(path,tr,params,post)
        self.quotes.request=request
        stamp=self.now
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return stamp
        with patch('oi.load_index_master',return_value=master),patch('oi.datetime',Clock):
            result=self.service.collect(dict(expiry='',radius_pct=5),force=True)
        self.assertEqual(indices[0],2);self.assertEqual(result['spot'],200)
        self.assertFalse(result['coverage_complete']);self.assertIsNone(result['put_wall'])

    def test_incremental_day_change_does_not_reuse_other_scope(self):
        master=self.configure_incremental()
        other=[dict(r,expiry='202701',code=r['code'].replace('612','701')) for r in master]
        with patch('oi.load_index_master',return_value=master+other):
            self.collect()
            # Different month has no previous observation; quotes mapped for that month.
            tomorrow=self.collect(now=self.now+timedelta(days=1))
        self.assertEqual(tomorrow['sampling']['received'],32)
        self.assertTrue(all(not r['reused'] and r['observed_change'] is None for r in tomorrow['calls']+tomorrow['puts']))

    def test_incremental_expiry_change_does_not_reuse_other_month(self):
        master=self.configure_incremental()
        with patch('oi.load_index_master',return_value=master):self.collect()
        other=[dict(r,expiry='202701') for r in master]
        with patch('oi.load_index_master',return_value=master+other):
            result=self.service.collect(dict(expiry='202701',radius_pct=5),self.now+timedelta(seconds=60))
        self.assertEqual(result['expiry'],'202701')
        self.assertEqual(result['sampling']['received'],32)
        self.assertIsNone(result['comparison_at'])
        self.assertTrue(all(not r['reused'] and r['observed_change'] is None for r in result['calls']+result['puts']))

    def test_first_wall_is_near_material_peak_not_far_global_maximum(self):
        # Representative neighborhood matching the screenshot's material levels.
        puts=[dict(strike=k,oi=v) for k,v in [(750,4783),(760,600),(770,550),(772.5,20),(775,1350),(777.5,40),(780,1107),(782.5,20),(785,69)]]
        calls=[dict(strike=k,oi=v) for k,v in [(787.5,54),(790,75),(792.5,551),(795,134),(797.5,80),(800,2537),(802.5,180),(805,250),(807.5,220),(815,1500)]]
        self.assertEqual(candidates(puts,786.17,'put',5,None)[0]['strike'],750)
        self.assertEqual(first_candidates(puts,786.17,'put',5,None)[0]['strike'],780)
        self.assertEqual(first_candidates(calls,786.17,'call',5,None)[0]['strike'],800)
        self.assertEqual(first_candidates(puts,786.17,'put',5,None)[0]['selection'],'nearest_material_peak')

    def test_far_major_wall_does_not_automatically_become_a_first_wall(self):
        rows=[dict(strike=750,oi=4783)]
        self.assertEqual(candidates(rows,786.17,'put',5,None)[0]['strike'],750)
        self.assertEqual(first_candidates(rows,786.17,'put',5,None),[])

    def test_stale_first_peak_is_excluded_without_promoting_tiny_nearest_oi(self):
        rows=[dict(strike=775,oi=1350),dict(strike=777.5,oi=40),dict(strike=780,oi=1107,stale=True),dict(strike=782.5,oi=20),dict(strike=785,oi=69)]
        self.assertEqual(first_candidates(rows,786.17,'put',5,None)[0]['strike'],775)

    def test_equal_height_plateau_is_one_peak_and_flat_oi_is_not_a_wall(self):
        rows=[dict(strike=k,oi=v) for k,v in [(770,100),(775,1100),(777.5,1100),(780,50),(782.5,30)]]
        selected=first_candidates(rows,786.17,'put',5,None)
        self.assertEqual([r['strike'] for r in selected],[777.5])
        self.assertEqual(first_candidates([dict(strike=k,oi=100) for k in [775,777.5,780]],786.17,'put',5,None),[])

    def test_first_wall_respects_narrower_user_radius_and_correct_side(self):
        rows=[dict(strike=780,oi=1107),dict(strike=782.5,oi=20),dict(strike=785,oi=69)]
        self.assertEqual(first_candidates(rows,786.17,'put',1,None)[0]['strike'],780)
        self.assertEqual(first_candidates([dict(strike=750,oi=4783)],786.17,'put',1,None),[])
        self.assertEqual(first_candidates(rows,786.17,'call',5,None),[])

    def test_existing_real_token_is_unchanged_and_orders_are_blocked(self):
        (self.root/'kis_config.json').write_text(json.dumps({'profiles':{'real':{'app_key':'key','app_secret':'secret'}}}))
        path=self.root/'kis_token_real.json'
        path.write_text(json.dumps(dict(binding=hashlib.sha256(b'real\0key\0secret').hexdigest(),token='cached',expires=9999999999)))
        before=path.read_bytes();calls=[]
        def send(*args):calls.append(args);return {'rt_cd':'0','output':[]}
        with patch.dict('os.environ',{},clear=True):
            client=ReadOnlyDerivativesClient(self.root,send,0)
            client.request(OI_EXPIRIES,'FHPIO056104C0',{})
            client.request(OI_PRICE,'FHMIF10000000',{})
            client.request(FUTURES_MINUTES,'FHKIF03020200',{})
            with self.assertRaises(TradingError):client.request(FUTURES_MINUTES,'FHKIF03020200',{},True)
            with self.assertRaises(TradingError):client.request(OI_PRICE,'FHMIF10000000',{},True)
            with self.assertRaises(TradingError):client.request(ORDER,'TTTC0012U',{},True)
            with self.assertRaises(TradingError):client.request(OI_OPTIONS,'FHPIF05030100',{},True)
        self.assertEqual(len(calls),3);self.assertEqual(path.read_bytes(),before)
        with self.assertRaises(TradingError):transport(OI_OPTIONS,{}, {},True,base=REAL_BASE)
