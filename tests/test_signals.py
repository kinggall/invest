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
