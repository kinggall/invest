from datetime import timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import test_wall_broker as fixtures
from audit_trading import audit
from wall_broker import WallBroker
from wall_replay import capture,ensure_observations


class AuditTests(unittest.TestCase):
    def setUp(self):
        from datetime import datetime
        from trader import KST
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'orders.sqlite3';self.client=fixtures.Demo()
        self.book=WallBroker(self.path,self.client);self.addCleanup(self.book.close)
        self.start=datetime(2026,11,16,10,30,10,tzinfo=KST)
    data=fixtures.BrokerTests.data
    def enter(self):
        self.book.step(*self.data(self.start),now=self.start)
        at=self.start+timedelta(minutes=3);self.book.step(*self.data(at),now=at)
        q=self.client.sent[-1]['quantity'];self.client.holdings=q
        self.client.matches['1']=('FILLED',q,q*10020)
        self.book.sync(at+timedelta(seconds=5))
        return at

    def test_old_data_separates_recomputed_stop_from_proven_signal(self):
        at=self.enter()
        # Simulate the pre-audit version: no actual per-decision trace exists.
        self.book.db.execute('DELETE FROM broker_trace')
        import sqlite3
        db=sqlite3.connect(self.path);ensure_observations(db)
        now=at+timedelta(minutes=1)
        capture(db,*self.data(now,price=9800),now);db.commit();db.close()
        before={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.path,self.path.with_name('orders.demo.sqlite3'))}
        with patch('trader.transport',side_effect=AssertionError('No network')):
            report=audit(self.path,'2026-11-16')
        self.assertEqual(report['evidence']['sell_order_count'],0)
        self.assertFalse(report['evidence']['trace_available'])
        self.assertEqual(report['inference']['first_recomputed_exit']['reason'],'ETF_STOP')
        self.assertEqual(report['scheduled_close']['registered_close_orders'],0)
        self.assertTrue(report['scheduled_close']['required_if_holding'])
        for path,digest in before.items():self.assertEqual(hashlib.sha256(Path(path).read_bytes()).hexdigest(),digest)
        self.assertNotIn('CANO',json.dumps(report));self.assertNotIn('app_secret',json.dumps(report))

    def test_signal_blocked_before_order_reservation_is_proven(self):
        at=self.enter();self.client.holdings=0
        with self.assertRaises(Exception):self.book.step(*self.data(at+timedelta(minutes=1),price=9800),now=at+timedelta(minutes=1))
        self.book.record_error('계좌 사전 확인','DEMO_BALANCE_MISMATCH',at+timedelta(minutes=1))
        report=audit(self.path,'2026-11-16')
        self.assertEqual(report['evidence']['sell_order_count'],0)
        self.assertEqual(report['evidence']['proven_sell_signal_count'],1)
        self.assertIn('전송 전',report['evidence']['conclusion'])
        self.assertEqual(report['last_saved_error']['message'],'DEMO_BALANCE_MISMATCH')

    def test_unknown_submitted_sell_is_not_reported_as_no_order(self):
        at=self.enter();self.client.fail=True
        with self.assertRaises(Exception):self.book.step(*self.data(at+timedelta(minutes=1),price=9800),now=at+timedelta(minutes=1))
        report=audit(self.path,'2026-11-16')
        self.assertEqual(report['evidence']['sell_order_count'],1)
        self.assertEqual(report['orders'][-1]['status'],'UNKNOWN')
        self.assertIsNone(report['orders'][-1]['order_no'])

    def test_after_hours_error_does_not_prove_intraday_signal_coverage(self):
        self.enter();self.book.db.execute('DELETE FROM broker_trace')
        self.book.record_error('수동 조회','SHARED_DEMO_TOKEN_UNAVAILABLE',self.start.replace(hour=18))
        report=audit(self.path,'2026-11-16')
        self.assertFalse(report['evidence']['trace_available'])
        self.assertIn('확정 불가',report['evidence']['conclusion'])
