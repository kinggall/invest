from datetime import datetime,timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from instruments import TRADE_SYMBOL
from trader import KST,TradingError
from wall_paper import WallPaper
from wall_replay import capture,ensure_observations,available_dates,replay,minute_frames,reselect_snapshot
from macd_strategy import DEFAULT

class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'source.db';self.book=WallPaper(self.path);self.addCleanup(self.book.close)
        ensure_observations(self.book.db)
        self.start=datetime(2026,11,16,10,30,10,tzinfo=KST)
    def frame(self,now,spot=776,price=10000):
        end=now.replace(minute=now.minute//3*3,second=0,microsecond=0)
        etf=dict(symbol=TRADE_SYMBOL,price_time=now.replace(second=0).isoformat(),bars=35,required_bars=35,periods=[12,26,9],
            chart=[dict(time=(end-timedelta(minutes=6)).isoformat(),end=(end-timedelta(minutes=3)).isoformat(),high=9990,low=9970,open=9980,close=9985,volume=100,macd=2,signal=1,histogram=1),
                   dict(time=(end-timedelta(minutes=3)).isoformat(),end=end.isoformat(),high=10020,low=9990,open=10000,close=10010,volume=100,macd=8,signal=3,histogram=5,vwap=9000,vwap_change=1)],
            flow=dict(kind='kospi_intraday',market='KOSPI',observed_at=now.isoformat(),comparison_3m_at=(now-timedelta(minutes=3)).isoformat(),comparison_3m_seconds=180,foreign_change_3m=10,institution_change_3m=5))
        oi=dict(expiry='202612',observed_at=now.isoformat(),spot=spot,coverage_complete=True,put_wall=dict(strike=775,oi=100,received_at=now.isoformat()),call_wall=dict(strike=800,oi=100,received_at=now.isoformat()))
        return etf,oi,dict(price=price,observed_at=now.isoformat())
    def test_recorded_replay_matches_fresh_engine_without_changing_source_or_network(self):
        reference=WallPaper(':memory:');self.addCleanup(reference.close)
        for offset,spot,price in [(0,776,10000),(3,776,10000),(4,790,10100),(5,795,10200),(6,790,9800)]:
            now=self.start+timedelta(minutes=offset);etf,oi,quote=self.frame(now,spot,price)
            capture(self.book.db,etf,oi,quote,now)
            expected=reference.step(etf,oi,quote,now)
        before=hashlib.sha256(self.path.read_bytes()).hexdigest()
        with patch('trader.transport',side_effect=AssertionError('No network')):
            result=replay(self.path,'2026-11-16',DEFAULT)
        self.assertEqual(result['mode'],'recorded')
        self.assertEqual(result['net_pnl'],expected['net_pnl'])
        self.assertEqual([e['reason'] for e in result['events']],[e['reason'] for e in expected['events']])
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(),before)
        self.assertEqual(self.book.view()['events'],[])
        self.assertEqual(available_dates(self.path),['2026-11-16'])
    def test_observation_mode_does_not_import_live_position_or_force_final_liquidation(self):
        for offset in (0,3):
            now=self.start+timedelta(minutes=offset);capture(self.book.db,*self.frame(now),now,manual=True)
        result=replay(self.path,'2026-11-16',DEFAULT,mode='recorded')
        self.assertGreater(result['ending_quantity'],0)
        self.assertEqual(result['sell_count'],0)
        self.assertTrue(any('미청산' in w for w in result['warnings']))
        self.assertEqual(result['manual_inputs_ignored'],2)
    def test_no_oi_is_a_visible_error_not_current_wall_backfill(self):
        with self.assertRaisesRegex(TradingError,'과거 OI 기록이 없습니다'):
            replay(self.path,'2026-11-16',DEFAULT,mode='minute')
        with self.assertRaisesRegex(TradingError,'관측 입력 기록이 없습니다'):
            replay(self.path,'2026-11-16',DEFAULT,mode='recorded')
    def test_missing_indicators_produce_zero_readiness_warning_not_a_performance_claim(self):
        now=self.start;etf,oi,quote=self.frame(now);etf['flow']={}
        capture(self.book.db,etf,oi,quote,now)
        result=replay(self.path,'2026-11-16',DEFAULT)
        self.assertEqual(result['ready_samples'],0);self.assertEqual(result['buy_count'],0)
        self.assertTrue(any('성과의 증거가 아닙니다' in w for w in result['warnings']))
    def test_capital_changes_are_isolated(self):
        for offset in (0,3):
            now=self.start+timedelta(minutes=offset);capture(self.book.db,*self.frame(now),now)
        low=replay(self.path,'2026-11-16',DEFAULT,capital=1000000)
        high=replay(self.path,'2026-11-16',DEFAULT,capital=10000000)
        self.assertGreater(high['ending_quantity'],low['ending_quantity'])
        self.assertEqual(self.book.view()['state']['capital'],10000000)
    def test_minute_replay_never_uses_future_oi_or_current_minute_close(self):
        start=self.start.replace(hour=9,minute=0,second=0)
        rows=[dict(time=(start+timedelta(minutes=i)).isoformat(),open=10000,high=10010,low=9990,close=10000,volume=1) for i in range(9)]
        rows[3].update(close=12000,high=12000)
        observed=(start+timedelta(minutes=5,seconds=10)).isoformat()
        source=dict(prior=[],current=rows,flows=[],oi=[dict(observed_at=observed,spot=776)])
        frames=list(minute_frames(source,DEFAULT))
        self.assertEqual(frames[3]['quote']['price'],10000)
        self.assertEqual(frames[3]['etf']['chart'][-1]['close'],10000)
        self.assertEqual(frames[5]['oi'],{})
        self.assertEqual(frames[6]['oi']['observed_at'],observed)
        for frame in frames:
            self.assertTrue(all(bar['end']<=frame['at'] for bar in frame['etf']['chart']))
    def test_minute_mode_reports_sparse_oi_and_cannot_invent_flow(self):
        db=self.book.db
        db.execute('CREATE TABLE oi_snapshots (id INTEGER PRIMARY KEY,observed_at TEXT,expiry TEXT,payload TEXT)')
        db.execute('CREATE TABLE minutes (symbol TEXT,stamp TEXT,value TEXT)')
        etf,oi,quote=self.frame(self.start)
        db.execute('INSERT INTO oi_snapshots VALUES (1,?,?,?)',(oi['observed_at'],'202612',json.dumps(oi)))
        start=self.start.replace(hour=10,minute=30,second=0)
        for i in range(8):
            row=dict(time=(start+timedelta(minutes=i)).isoformat(),open=10000,high=10010,low=9990,close=10000,volume=1)
            db.execute('INSERT INTO minutes VALUES (?,?,?)',(TRADE_SYMBOL,row['time'],json.dumps(row)))
        result=replay(self.path,'2026-11-16',DEFAULT,mode='minute')
        self.assertEqual(result['mode'],'minute');self.assertEqual(result['ready_samples'],0)
        self.assertEqual(result['buy_count'],0)
        self.assertTrue(any('근사 백테스트' in w for w in result['warnings']))
    def test_current_selector_uses_past_distribution_without_rewriting_source(self):
        now=self.start;etf,old,quote=self.frame(now,spot=786.17)
        old['put_wall']['strike']=750;old['radius_pct']=5
        def row(strike,oi):return dict(strike=strike,oi=oi,received_at=now.isoformat())
        old['puts']=[row(k,v) for k,v in [(750,4783),(775,1350),(777.5,40),(780,1107),(782.5,20),(785,69)]]
        old['calls']=[row(k,v) for k,v in [(787.5,54),(790,75),(792.5,551),(795,134),(797.5,80),(800,2537),(805,200)]]
        selected=reselect_snapshot(old)
        self.assertEqual(selected['put_wall']['strike'],780)
        self.assertEqual(selected['call_wall']['strike'],800)
        self.assertEqual(old['put_wall']['strike'],750)
        old['puts'][3]['received_at']=None
        self.assertNotEqual((reselect_snapshot(old)['put_wall'] or {}).get('strike'),780)

    def test_future_date_is_rejected_before_data_access(self):
        with self.assertRaisesRegex(TradingError,'미래 날짜'):replay(self.path,'2099-01-01',DEFAULT)

    def test_macd_exclusion_is_compared_on_same_frames_without_mutating_source(self):
        for offset in (0,3):
            now=self.start+timedelta(minutes=offset);etf,oi,quote=self.frame(now)
            etf['chart'][-1].update(macd=-1,signal=3,histogram=-4)
            etf['chart'][-2]['histogram']=-2
            capture(self.book.db,etf,oi,quote,now)
        before=hashlib.sha256(self.path.read_bytes()).hexdigest()
        result=replay(self.path,'2026-11-16',DEFAULT,options=dict(macd_filter=False))
        self.assertEqual(result['comparison'][0]['buy_count'],0)
        self.assertEqual(result['comparison'][1]['buy_count'],1)
        self.assertFalse(result['options']['macd_filter'])
        self.assertEqual(result['events'][0]['rules']['macd_filter'],False)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(),before)
        self.assertEqual(self.book.options['macd_filter'],True)

    def test_disabled_macd_does_not_require_macd_warmup_but_keeps_price_freshness(self):
        for offset in (0,3):
            now=self.start+timedelta(minutes=offset);etf,oi,quote=self.frame(now)
            etf['bars']=2
            for row in etf['chart']:
                for key in ('macd','signal','histogram'):row.pop(key,None)
            capture(self.book.db,etf,oi,quote,now)
        result=replay(self.path,'2026-11-16',DEFAULT,options=dict(macd_filter=False))
        self.assertEqual(result['buy_count'],1);self.assertEqual(result['comparison'][0]['ready_samples'],0)
        etf,oi,quote=self.frame(self.start)
        etf['chart'][-1]['end']=(self.start-timedelta(minutes=10)).isoformat()
        from confirmation import assess
        self.assertEqual(assess(etf,oi,self.start,require_macd=False)['status'],'WAITING')

    def test_two_bar_window_can_enter_on_second_confirmation(self):
        for offset in (0,3,6):
            now=self.start+timedelta(minutes=offset);etf,oi,quote=self.frame(now)
            if offset==3:etf['chart'][-1].update(close=9980,low=9970)
            if offset==6:
                etf['chart'][-2].update(high=10020,histogram=5)
                etf['chart'][-1].update(close=10030,high=10040,macd=9,signal=1,histogram=8)
            capture(self.book.db,etf,oi,quote,now)
        result=replay(self.path,'2026-11-16',DEFAULT,options=dict(rebound_bars=2))
        self.assertEqual(result['comparison'][0]['buy_count'],0);self.assertEqual(result['buy_count'],1)
        self.assertEqual(result['events'][0]['time'],(self.start+timedelta(minutes=6)).isoformat())

    def test_vwap_size_switch_changes_budget_without_changing_live_defaults(self):
        for offset in (0,3):
            now=self.start+timedelta(minutes=offset);etf,oi,quote=self.frame(now)
            etf['chart'][-1]['vwap']=11000
            capture(self.book.db,etf,oi,quote,now)
        result=replay(self.path,'2026-11-16',DEFAULT,options=dict(vwap_sizing=False))
        baseline=result['comparison'][0]
        self.assertGreater(result['ending_quantity'],baseline['ending_quantity'])
        self.assertEqual(self.book.options['vwap_sizing'],True)

    def test_replay_option_validation(self):
        from wall_paper import strategy_options
        for bad in (True,{'macd_filter':'false'},{'rebound_bars':4},{'rebound_bars':True},{'unknown':True}):
            with self.assertRaises(TradingError):strategy_options(bad)

    def seed_tail_prices(self,stopped=False):
        for offset in (0,3):
            now=self.start+timedelta(minutes=offset);capture(self.book.db,*self.frame(now),now)
        self.book.db.execute('CREATE TABLE minutes (symbol TEXT,stamp TEXT,value TEXT)')
        for stamp,opening,closing in [('10:33',20000,20000),('12:48',9800 if stopped else 10000,9000),('15:15',10050,10050),('15:19',10040,10040)]:
            at='2026-11-16T'+stamp+':00+09:00'
            row=dict(time=at,open=opening,close=closing,high=max(opening,closing),low=min(opening,closing),volume=1)
            self.book.db.execute('INSERT INTO minutes VALUES (?,?,?)',(TRADE_SYMBOL,at,json.dumps(row)))

    def test_missing_afternoon_prices_extend_mark_and_close_without_inventing_oi(self):
        self.seed_tail_prices()
        before=hashlib.sha256(self.path.read_bytes()).hexdigest()
        result=replay(self.path,'2026-11-16',DEFAULT,supplement=True)
        self.assertEqual(result['mode'],'hybrid')
        self.assertEqual(result['last'],'2026-11-16T15:19:00+09:00')
        self.assertEqual(result['ending_quantity'],0)
        self.assertEqual([e['reason'] for e in result['events']],['REBOUND_ENTRY','DAY_EXIT'])
        self.assertEqual(result['events'][1]['reference_price'],10050)
        self.assertEqual(result['supplemented_samples'],3)
        self.assertEqual(result['buy_count'],1)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(),before)

    def test_afternoon_open_below_etf_stop_exits_without_oi_or_future_close(self):
        self.seed_tail_prices(stopped=True)
        result=replay(self.path,'2026-11-16',DEFAULT,supplement=True)
        self.assertEqual(result['events'][-1]['reason'],'ETF_STOP')
        self.assertEqual(result['events'][-1]['reference_price'],9800)
        self.assertEqual(result['events'][-1]['time'],'2026-11-16T12:48:00+09:00')
        self.assertEqual(result['buy_count'],1)

    def test_existing_recorded_minute_has_precedence_over_downloaded_open(self):
        self.seed_tail_prices()
        from wall_replay import supplement_recorded,load_sources
        source=supplement_recorded(self.path,'2026-11-16',load_sources(self.path,'2026-11-16','recorded'),DEFAULT)
        at=self.start+timedelta(minutes=3)
        rows=[f for f in source['frames'] if f['at'][:16]==at.isoformat()[:16]]
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['quote']['price'],10000)
        added=[f for f in source['frames'] if f.get('price_supplement')]
        self.assertTrue(all(not f['oi'] and not f['etf']['flow'] for f in added))


    def test_etf_profit_take_compares_same_samples_and_preserves_source(self):
        for offset,price in [(0,10000),(3,10000),(4,10120),(5,10000)]:
            now=self.start+timedelta(minutes=offset);capture(self.book.db,*self.frame(now,price=price),now)
        before=hashlib.sha256(self.path.read_bytes()).hexdigest()
        result=replay(self.path,'2026-11-16',DEFAULT,options=dict(etf_profit_take=True))
        self.assertEqual(result['comparison'][0]['samples'],result['comparison'][1]['samples'])
        self.assertEqual([e['reason'] for e in result['events']],['REBOUND_ENTRY','ETF_PROFIT_1PCT','PROFIT_PROTECT'])
        self.assertGreater(result['comparison'][0]['ending_quantity'],0)
        self.assertEqual(result['ending_quantity'],0)
        self.assertFalse(self.book.options['etf_profit_take'])
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(),before)
