from datetime import datetime, timedelta
import unittest
from signals import aggregate, ema, macd
from trader import KST

class IndicatorTests(unittest.TestCase):
    def test_sma_seeded_ema_known_values(self):
        self.assertEqual(ema([2,4,6,8,10],3),[None,None,4,6,8])

    def test_linear_trend_known_macd_and_no_spurious_crosses(self):
        points=macd([dict(close=i) for i in range(1,21)],3,5,2)
        self.assertEqual(points[4]['macd'],1)
        self.assertEqual(points[5]['signal'],1)
        self.assertTrue(all(p['cross'] is None for p in points))

    def test_warmup_and_causal_values(self):
        bars=[dict(close=100+i%7) for i in range(100)]
        full=macd(bars)
        self.assertIsNone(full[32]['signal'])
        self.assertIsNotNone(full[33]['signal'])
        self.assertEqual(full[:40],macd(bars[:40]))

    def test_closed_bars_only_deduplicate_and_skip_missing_minutes(self):
        start=datetime(2026,11,16,9,tzinfo=KST)
        rows=[dict(time=(start+timedelta(minutes=i)).isoformat(),open=10+i,high=12+i,low=9+i,close=11+i,volume=i+1) for i in range(6)]
        result=aggregate(rows+[rows[0]],start+timedelta(minutes=5))
        self.assertEqual(len(result),1)
        self.assertEqual((result[0]['open'],result[0]['close'],result[0]['volume']),(10,13,6))
        self.assertEqual(len(aggregate(rows,start+timedelta(minutes=6))),2)
        self.assertEqual(len(aggregate(rows[:1]+rows[2:],start+timedelta(minutes=6))),1)

class VWAPTests(unittest.TestCase):
    def rows(self):
        start=datetime(2026,11,16,9,tzinfo=KST)
        prices=[100,200,300,400,500,600]
        volumes=[1,2,1,1,1,1]
        return [dict(time=(start+timedelta(minutes=i)).isoformat(),open=p,high=p,low=p,close=p,volume=volumes[i]) for i,p in enumerate(prices)]

    def test_known_volume_weighted_values_and_no_future_leak(self):
        from signals import session_vwap
        rows=self.rows();now=datetime(2026,11,16,9,6,tzinfo=KST)
        points=session_vwap(rows,aggregate(rows,now))
        self.assertEqual(points[0]['vwap'],200)
        self.assertAlmostEqual(points[1]['vwap'],2300/7)
        self.assertAlmostEqual(points[0]['vwap_distance_pct'],50)
        future=[dict(row) for row in rows]
        for row in future[3:]:row.update(high=9999,low=9999,close=9999,volume=999)
        self.assertEqual(session_vwap(future,aggregate(future,now))[0]['vwap'],200)

    def test_new_day_resets_and_zero_volume_is_missing(self):
        from signals import session_vwap
        rows=self.rows()
        tomorrow=[dict(row,time=(datetime.fromisoformat(row['time'])+timedelta(days=1)).isoformat(),high=1000,low=1000,close=1000,open=1000) for row in rows[:3]]
        now=datetime(2026,11,17,9,6,tzinfo=KST)
        points=session_vwap(rows+tomorrow,aggregate(rows+tomorrow,now))
        self.assertEqual(points[-1]['vwap'],1000)
        self.assertIsNone(points[-1]['vwap_change'])
        for row in rows:row['volume']=0
        self.assertTrue(all(p['vwap'] is None for p in session_vwap(rows,aggregate(rows,now))))

    def test_missing_opening_minute_invalidates_reference(self):
        from signals import session_vwap
        rows=self.rows()[1:]
        points=session_vwap(rows,aggregate(rows,datetime(2026,11,16,9,6,tzinfo=KST)))
        self.assertEqual(len(points),1)
        self.assertIsNone(points[0]['vwap'])
        self.assertGreater(points[0]['vwap_observed'],0)
        self.assertEqual(points[0]['vwap_quality'],'partial')
        self.assertEqual(points[0]['vwap_missing_times'],['09:00'])

    def test_one_missing_minute_keeps_display_estimate_but_not_trading_reference(self):
        from signals import session_vwap,vwap_entry
        rows=self.rows()
        extra=[dict(row,time=(datetime.fromisoformat(row['time'])+timedelta(minutes=6)).isoformat()) for row in rows]
        raw=rows+extra
        raw=[row for row in raw if row['time'][11:16]!='09:04']
        points=session_vwap(raw,aggregate(raw,datetime(2026,11,16,9,12,tzinfo=KST)))
        self.assertEqual(points[0]['vwap_quality'],'complete')
        self.assertEqual(points[-1]['vwap_quality'],'partial')
        self.assertIsNone(points[-1]['vwap'])
        self.assertGreater(points[-1]['vwap_observed'],0)
        self.assertEqual(points[-1]['vwap_missing_minutes'],1)
        self.assertEqual(points[-1]['vwap_missing_times'],['09:04'])
        self.assertFalse(vwap_entry(points[-1]))

    def test_zero_volume_explains_why_no_line_is_available(self):
        from signals import session_vwap
        rows=self.rows()
        for row in rows:row['volume']=0
        points=session_vwap(rows,aggregate(rows,datetime(2026,11,16,9,6,tzinfo=KST)))
        self.assertEqual(points[-1]['vwap_quality'],'no_volume')
        self.assertEqual(points[-1]['vwap_cumulative_volume'],0)
        self.assertIsNone(points[-1]['vwap_observed'])
