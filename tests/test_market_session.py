from datetime import datetime,timedelta
import unittest
from market_session import KST,market_time,order_window,session_info
from price_archive import expected_minutes
from futures_chart import session_at
from signals import session_vwap,aggregate
import test_wall_broker as broker_fixtures
from wall_broker import WallBroker


class SessionTests(unittest.TestCase):
    def test_exam_day_order_window_and_closing_times(self):
        day=datetime(2026,11,19,tzinfo=KST)
        self.assertFalse(order_window(day.replace(hour=9,minute=59)))
        self.assertTrue(order_window(day.replace(hour=10)))
        self.assertTrue(order_window(day.replace(hour=16,minute=19,second=59)))
        self.assertFalse(order_window(day.replace(hour=16,minute=20)))
        s=session_info(day)
        self.assertEqual((s['open'],s['liquidate'],s['deadline'],s['close']),('10:00','16:15','16:19','16:30'))
        self.assertEqual(session_info(day-timedelta(days=1))['open'],'09:00')

    def test_exam_day_expected_prices_and_future_session(self):
        now=datetime(2026,11,19,17,tzinfo=KST)
        expected=expected_minutes(now.date(),now)
        self.assertEqual(len(expected),380)
        self.assertIn('T10:00:',expected[0]);self.assertIn('T16:19:',expected[-1])
        future=session_at(now.replace(hour=10))
        self.assertIn('T09:45:',future['start']);self.assertIn('T16:45:',future['end'])

    def test_vwap_accepts_delayed_open_and_late_hour(self):
        start=datetime(2026,11,19,10,tzinfo=KST)
        rows=[dict(time=(start+timedelta(minutes=i)).isoformat(),open=100,high=100,low=100,close=100,volume=1) for i in range(380)]
        result=session_vwap(rows,aggregate(rows,start+timedelta(hours=7)))
        self.assertEqual(result[0]['time'],start.isoformat())
        self.assertIsNotNone(result[-1]['vwap'])
        self.assertTrue(any(b['time'][11:13]=='16' for b in result))


class OvernightExitTests(unittest.TestCase):
    setUp=broker_fixtures.BrokerTests.setUp
    data=broker_fixtures.BrokerTests.data
    step=broker_fixtures.BrokerTests.step
    enter=broker_fixtures.BrokerTests.enter
    fill=broker_fixtures.BrokerTests.fill

    def test_exam_day_cannot_sell_before_ten_and_prior_day_exits_at_ten(self):
        self.enter();self.fill()
        p=self.book.load();p['position']['entry_day']='2026-11-18';self.book.save(p)
        before=datetime(2026,11,19,9,59,tzinfo=KST)
        quote=lambda at:dict(symbol='069500',price=10010,observed_at=at.isoformat())
        self.book.step({}, {},quote(before),now=before,protect_only=True)
        self.assertEqual(len(self.client.sent),1)
        opening=before.replace(hour=10,minute=0)
        result=self.book.step({}, {},quote(opening),now=opening,protect_only=True)
        self.assertEqual(result['orders'][0]['reason'],'OVERNIGHT_EXIT')
        self.assertEqual(self.client.sent[-1]['side'],'sell')
        self.assertEqual(self.client.sent[-1]['order_type'],'market')

    def test_exam_day_closing_waits_until_sixteen_fifteen(self):
        self.enter();self.fill()
        p=self.book.load();p['position']['entry_day']='2026-11-19';self.book.save(p)
        early=datetime(2026,11,19,15,15,tzinfo=KST)
        self.book.step({}, {},dict(symbol='069500',price=10010,observed_at=early.isoformat()),now=early,protect_only=True)
        self.assertEqual(len(self.client.sent),1)
        closing=early.replace(hour=16)
        result=self.book.step({}, {},{},now=closing,protect_only=True)
        self.assertEqual(result['orders'][0]['reason'],'DAY_EXIT')
