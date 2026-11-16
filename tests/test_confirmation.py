from copy import deepcopy
from datetime import datetime,timedelta
import unittest
from confirmation import assess
from instruments import TRADE_SYMBOL
from trader import KST

class ConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime(2026,11,16,10,30,10,tzinfo=KST)
        end=self.now.replace(second=0)
        self.snapshot=dict(symbol=TRADE_SYMBOL,bars=35,required_bars=35,
            chart=[dict(end=(end-timedelta(minutes=3)).isoformat(),histogram=3),dict(end=end.isoformat(),macd=8,signal=3,histogram=5,close=80000,vwap=79500,vwap_change=2)],
            flow=dict(kind='kospi_intraday',market='KOSPI',observed_at=end.isoformat(),comparison_3m_at=(end-timedelta(minutes=3)).isoformat(),comparison_3m_seconds=180,
                foreign=-10000,institution=-5000,foreign_change_3m=10,institution_change_3m=5,unit='API 단위 미검증'))
        wall=lambda strike:dict(strike=strike,oi=100,received_at=end.isoformat())
        self.oi=dict(observed_at=end.isoformat(),spot=791.56,put_wall=wall(775),call_wall=wall(800),coverage_complete=True)

    def test_uses_etf_bars_and_flow_changes_not_cumulative_signs(self):
        result=assess(self.snapshot,self.oi,self.now)
        self.assertEqual(result['status'],'SUPPORTIVE')
        self.assertEqual(result['execution'],'OBSERVE_ONLY')
        self.assertEqual(result['flow']['combined'],15)
        self.assertTrue(result['vwap']['above'])
        self.assertEqual(result['walls']['put'],775)

    def test_positive_cumulative_flow_with_negative_change_is_not_supportive(self):
        self.snapshot['flow'].update(foreign=100000,institution=100000,foreign_change_3m=-10,institution_change_3m=-5)
        result=assess(self.snapshot,self.oi,self.now)
        self.assertEqual(result['status'],'MIXED')
        self.assertFalse(result['flow']['positive'])

    def test_partial_oi_is_provisional_even_with_positive_indicators(self):
        self.oi['coverage_complete']=False
        result=assess(self.snapshot,self.oi,self.now)
        self.assertEqual(result['status'],'WAITING')
        self.assertIn('임시 후보',result['message'])
        self.assertEqual(result['macd']['state'],'상방 우위')

    def test_other_etf_and_stale_or_nonconsecutive_bars_are_not_accepted(self):
        for change in ('symbol','age','gap'):
            snapshot=deepcopy(self.snapshot)
            if change=='symbol':snapshot['symbol']='472150'
            if change=='age':snapshot['chart'][-1]['end']=(self.now-timedelta(seconds=91)).isoformat()
            if change=='gap':snapshot['chart'][0]['end']=(self.now.replace(second=0)-timedelta(minutes=6)).isoformat()
            self.assertEqual(assess(snapshot,self.oi,self.now)['macd']['state'],'UNKNOWN')

    def test_missing_flow_or_unknown_comparison_interval_is_not_zero(self):
        for change in ('missing','interval','day'):
            snapshot=deepcopy(self.snapshot)
            if change=='missing':snapshot['flow']['foreign_change_3m']=None
            if change=='interval':snapshot['flow']['comparison_3m_seconds']=1800
            if change=='day':snapshot['flow']['comparison_3m_at']=(self.now-timedelta(days=1)).isoformat()
            self.assertEqual(assess(snapshot,self.oi,self.now)['flow']['state'],'UNKNOWN')

    def test_partial_vwap_is_not_used_as_full_session_vwap(self):
        self.snapshot['chart'][-1].update(vwap=None,vwap_observed=79500)
        self.assertEqual(assess(self.snapshot,self.oi,self.now)['vwap']['state'],'UNKNOWN')

    def test_stale_oi_or_wall_cannot_be_used(self):
        for field in ('snapshot','wall'):
            oi=deepcopy(self.oi)
            if field=='snapshot':oi['observed_at']=(self.now-timedelta(seconds=151)).isoformat()
            else:oi['put_wall']['received_at']=(self.now-timedelta(seconds=601)).isoformat()
            self.assertEqual(assess(self.snapshot,oi,self.now)['walls']['state'],'UNKNOWN')

    def test_after_hours_data_is_reference_only(self):
        result=assess(self.snapshot,self.oi,self.now.replace(hour=0))
        self.assertEqual(result['status'],'WAITING')
        self.assertIn('장외',result['message'])
