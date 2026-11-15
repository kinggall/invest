import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dashboard as d
from trader import TradingError

class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.app=d.Dashboard(Path(self.tmp.name),Path(self.tmp.name)/'state.db')
        self.server=ThreadingHTTPServer(('127.0.0.1',0),d.handler_for(self.app))
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.close)
        self.host='127.0.0.1:'+str(self.server.server_port)

    def close(self):
        self.server.shutdown();self.server.server_close();self.thread.join()

    def request(self,path,body=None,secure=True):
        conn=http.client.HTTPConnection(self.host)
        headers={}
        if body is not None:
            headers={'Content-Type':'application/json','Origin':'http://'+self.host}
            if secure:headers['X-CSRF-Token']=self.app.csrf
        conn.request('GET' if body is None else 'POST',path,None if body is None else json.dumps(body),headers)
        response=conn.getresponse();status=response.status;raw=response.read();conn.close()
        return status,raw

    def test_page_and_unconfigured_status(self):
        status,raw=self.request('/')
        self.assertEqual(status,200);self.assertIn('주문·체결내역',raw.decode())
        with patch.dict('os.environ',{'KIS_DEMO_CANO':''}):
            status,raw=self.request('/api/status');data=json.loads(raw)
        self.assertEqual(status,200);self.assertFalse(data['enabled']);self.assertFalse(data['account_ready'])
        self.assertNotIn('app_secret',raw.decode());self.assertNotIn('Bearer',raw.decode())

    def test_external_or_missing_csrf_requests_cannot_start(self):
        status,_=self.request('/api/start',{},secure=False)
        self.assertEqual(status,403);self.assertFalse(self.app.enabled)
        conn=http.client.HTTPConnection(self.host)
        conn.request('POST','/api/start','{}',{'Origin':'https://example.com','X-CSRF-Token':self.app.csrf})
        response=conn.getresponse();self.assertEqual(response.status,403);response.read();conn.close()

    def test_save_persists_and_active_strategy_is_locked(self):
        value=self.app.strategy();value['quantity']=2;value['max_notional']=100000
        status,_=self.request('/api/strategy',value);self.assertEqual(status,200)
        self.assertEqual(self.app.strategy()['quantity'],2)
        self.app.enabled=True
        value['quantity']=1
        self.assertEqual(self.request('/api/strategy',value)[0],400)
        self.assertEqual(self.request('/api/stop',{})[0],200)
        self.assertFalse(self.app.enabled)
        restarted=d.Dashboard(self.app.home,self.app.state)
        self.assertFalse(restarted.enabled);self.assertEqual(restarted.strategy()['quantity'],2)

    def test_legacy_price_settings_migrate_without_erasing_records(self):
        with self.app.db() as db:
            db.execute('UPDATE settings SET value=? WHERE id=1', (json.dumps({'id':'old','symbol':'005930','quantity':1}),))
        restarted=d.Dashboard(self.app.home,self.app.state)
        self.assertEqual(restarted.strategy()['symbol'],'472150')
        self.assertEqual(restarted.strategy()['mode'],'macd')
        with restarted.db() as db:
            old=json.loads(db.execute('SELECT value FROM settings WHERE id=2').fetchone()[0])
        self.assertEqual(old['symbol'],'005930')
        self.assertFalse(restarted.enabled)

    def test_daily_chart_is_read_only_and_does_not_change_strategy(self):
        before=self.app.strategy()
        bars=[dict(time='2026-11-13',end='2026-11-13',open=100,high=110,low=90,close=105,volume=1000)]
        with patch.object(self.app,'engine') as factory:
            factory.return_value.market.daily.return_value=bars
            self.app.action('daily',{'date':'2026-11-13'})
            factory.return_value.client.order.assert_not_called()
            factory.return_value.evaluate.assert_not_called()
        self.assertEqual(self.app.history['last_date'],'2026-11-13')
        self.assertEqual(self.app.strategy(),before)
        self.assertFalse(self.app.enabled)

    def test_start_requires_existing_demo_token(self):
        with patch.object(d.SharedToken,'read',side_effect=TradingError('TOKEN_UNAVAILABLE')):
            self.assertEqual(self.request('/api/start',{})[0],400)
        self.assertFalse(self.app.enabled)

    def test_preview_does_not_execute_order(self):
        with patch.object(self.app,'tick',return_value={}) as tick:
            self.assertEqual(self.request('/api/preview',{})[0],200)
        tick.assert_called_once_with(False)

if __name__=='__main__':unittest.main()
