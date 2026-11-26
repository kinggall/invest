from datetime import datetime,timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from trader import KST,TradingError
from price_archive import archive,latest,expected_minutes
from test_history import HistorySource


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'orders.sqlite3';self.source=HistorySource()
        self.now=datetime(2026,11,18,16,tzinfo=KST)

    def test_closed_day_backfills_and_persists_all_minutes_without_order_tables(self):
        result=archive(self.path,Path(self.temp.name),now=self.now,client=self.source)
        today=result[0]
        self.assertEqual(today['status'],'COMPLETE');self.assertEqual(today['saved'],380)
        self.assertEqual(today['missing'],0);self.assertTrue(today['finalized'])
        db=sqlite3.connect(self.path);names={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")};db.close()
        self.assertNotIn('orders',names);self.assertNotIn('broker_meta',names)
        before=len(self.source.calls)
        archive(self.path,Path(self.temp.name),now=self.now+timedelta(hours=1),client=self.source)
        self.assertEqual(len(self.source.calls),before)
        self.assertTrue(all(not r[3] for r in self.source.calls))

    def test_partial_intraday_does_not_cache_day_and_postclose_refreshes(self):
        morning=self.now.replace(hour=12,minute=0)
        archive(self.path,Path(self.temp.name),now=morning,client=self.source)
        db=sqlite3.connect(self.path)
        self.assertFalse(db.execute("SELECT 1 FROM history_days WHERE day='2026-11-18'").fetchone())
        self.assertFalse(db.execute("SELECT 1 FROM minutes WHERE stamp>='2026-11-18T12:00:00+09:00'").fetchone());db.close()
        result=archive(self.path,Path(self.temp.name),now=self.now,client=self.source)
        self.assertEqual(result[0]['status'],'COMPLETE')
        self.assertEqual(result[0]['saved'],380)

    def test_missing_saved_minute_is_repaired_on_next_check(self):
        archive(self.path,Path(self.temp.name),now=self.now,client=self.source)
        db=sqlite3.connect(self.path);db.execute("DELETE FROM minutes WHERE stamp='2026-11-18T13:17:00+09:00'");db.commit();db.close()
        before=len(self.source.calls)
        result=archive(self.path,Path(self.temp.name),now=self.now+timedelta(minutes=16),client=self.source)
        self.assertEqual(result[0]['missing'],0);self.assertGreater(len(self.source.calls),before)

    def test_unavailable_prices_remain_missing_with_visible_status(self):
        source=HistorySource();source.request=lambda *a,**k:(_ for _ in ()).throw(TradingError('SHARED_REAL_TOKEN_UNAVAILABLE'))
        result=archive(self.path,Path(self.temp.name),now=self.now,client=source)
        self.assertEqual(result[0]['status'],'ERROR');self.assertEqual(result[0]['missing'],380)
        self.assertIn('SHARED_REAL_TOKEN_UNAVAILABLE',result[0]['error'])
        self.assertFalse(result[0]['finalized'])

    def test_next_morning_catches_yesterday_before_expiry_when_available(self):
        result=archive(self.path,Path(self.temp.name),now=self.now+timedelta(hours=16),client=self.source)
        self.assertEqual(result[0]['day'],'2026-11-18');self.assertEqual(result[0]['status'],'COMPLETE')

    def test_scheduled_task_is_installed_by_normal_launcher(self):
        root=Path(__file__).resolve().parents[1]
        self.assertIn('schedule-price-archive.ps1',(root/'start-dashboard.ps1').read_text())
        script=(root/'schedule-price-archive.ps1').read_text()
        for text in ('$slot -lt 30','15:35','15:50','16:10','-StartWhenAvailable','-AtLogOn','-MultipleInstances IgnoreNew','--domino-home','--state'):
            self.assertIn(text,script)

    def test_explicit_four_day_recovery_includes_monday_and_returns_all_days(self):
        from price_archive import recovery_dates
        now=datetime(2026,11,26,18,tzinfo=KST)
        dates=recovery_dates('2026-11-23','2026-11-26',now)
        result=archive(self.path,Path(self.temp.name),now=now,client=self.source,dates=dates,force=True)
        self.assertEqual([r['day'] for r in result],['2026-11-26','2026-11-25','2026-11-24','2026-11-23'])
        self.assertTrue(all(r['status']=='COMPLETE' and r['saved']==380 for r in result))
        self.assertEqual({call[2]['FID_INPUT_DATE_1'] for call in self.source.calls},{'20261123','20261124','20261125','20261126'})
        db=sqlite3.connect(self.path)
        self.assertTrue(db.execute("SELECT 1 FROM minutes WHERE stamp='2026-11-23T09:00:00+09:00'").fetchone());db.close()

    def test_one_unavailable_day_does_not_abandon_other_recovery_dates(self):
        from price_archive import recovery_dates
        now=datetime(2026,11,26,18,tzinfo=KST);original=self.source.request
        def query(path,tr,params,post=False):
            if params.get('FID_INPUT_DATE_1')=='20261124':raise TradingError('HISTORY_QUERY_REJECTED / CODE=OPSQ0008')
            return original(path,tr,params,post)
        self.source.request=query
        result=archive(self.path,Path(self.temp.name),now=now,client=self.source,dates=recovery_dates('2026-11-23','2026-11-26',now),force=True)
        by_date={r['day']:r for r in result}
        self.assertEqual(by_date['2026-11-24']['status'],'ERROR')
        self.assertEqual(by_date['2026-11-24']['missing'],380)
        self.assertEqual(by_date['2026-11-23']['status'],'COMPLETE')
        self.assertEqual(by_date['2026-11-26']['status'],'COMPLETE')

    def test_recovery_range_validation(self):
        from price_archive import recovery_dates
        now=datetime(2026,11,26,18,tzinfo=KST)
        for start,end in [('2026-11-27','2026-11-27'),('2026-11-26','2026-11-23'),('2026-10-01','2026-11-26'),('bad','2026-11-26')]:
            with self.assertRaises(TradingError):recovery_dates(start,end,now)
