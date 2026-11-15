import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import Mock
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

    def test_historical_chart_is_read_only_and_does_not_change_strategy(self):
        before=self.app.strategy()
        with patch.object(self.app,'engine') as factory, patch.object(d,'History') as reader:
            reader.return_value.load.return_value={'cutoff':'2026-11-13','chart':[]}
            self.app.action('historical',{'date':'2026-11-13'})
            self.app.lookup_thread.join(2)
            self.assertFalse(self.app.lookup_thread.is_alive())
            factory.return_value.client.order.assert_not_called()
            factory.return_value.evaluate.assert_not_called()
        self.assertEqual(self.app.history['cutoff'],'2026-11-13')
        self.assertEqual(self.app.strategy(),before)
        self.assertFalse(self.app.enabled)

    def test_backtest_never_calls_live_evaluation_or_order(self):
        before=self.app.strategy()
        with patch.object(self.app,'engine') as factory, patch.object(d,'History') as reader, patch.object(d,'simulate') as simulator:
            reader.return_value.load.return_value={'cutoff':'2026-11-13','chart':[]}
            simulator.return_value={'net_pnl':123,'date':'2026-11-13'}
            self.app.action('backtest',{'date':'2026-11-13','capital':10000000})
            self.app.lookup_thread.join(2)
            self.assertFalse(self.app.lookup_thread.is_alive())
            factory.return_value.client.order.assert_not_called()
            factory.return_value.evaluate.assert_not_called()
            self.assertEqual(simulator.call_args.kwargs['initial'],10000000)
            self.assertEqual([call.kwargs.get('vwap_filter',False) for call in simulator.call_args_list],[False,True,True,True,False])
            self.assertEqual(self.app.backtest['comparison']['selected'],'baseline')
        self.assertEqual(self.app.backtest['net_pnl'],123)
        self.assertEqual(self.app.strategy(),before)
        self.assertFalse(self.app.enabled)

    def test_start_requires_existing_demo_token(self):
        with patch.object(d.SharedToken,'read',side_effect=TradingError('TOKEN_UNAVAILABLE')):
            self.assertEqual(self.request('/api/start',{})[0],400)
        self.assertFalse(self.app.enabled)

    def test_preview_does_not_execute_order(self):
        with patch.object(self.app,'tick',return_value={}) as tick:
            self.assertEqual(self.request('/api/preview',{})[0],200)
            self.app.lookup_thread.join(2)
        tick.assert_called_once_with(False)

    def test_slow_lookup_keeps_status_and_stop_responsive(self):
        entered=threading.Event();release=threading.Event()
        def slow(*args):
            entered.set()
            release.wait(3)
            return {'cutoff':'2026-11-13','chart':[]}
        with patch.object(self.app,'engine') as factory, patch.object(d,'History') as reader:
            reader.return_value.load.side_effect=slow
            try:
                status,raw=self.request('/api/historical',{'date':'2026-11-13'})
                self.assertEqual(status,200)
                self.assertTrue(entered.wait(1))
                data=json.loads(self.request('/api/status')[1])
                self.assertEqual(data['lookup_job']['state'],'running')
                self.assertEqual(self.request('/api/stop',{})[0],200)
                self.assertEqual(self.request('/api/historical',{'date':'2026-11-13'})[0],400)
            finally:
                release.set();self.app.lookup_thread.join(2)
        self.assertEqual(self.app.lookup_job['state'],'succeeded')

    def test_missing_real_token_is_visible_as_failed_job_without_secrets(self):
        with patch.dict('os.environ',{'KIS_DEMO_CANO':'12345678','KIS_DEMO_PRODUCT':'01'}):
            self.assertEqual(self.request('/api/historical',{'date':'2026-11-13'})[0],200)
            self.app.lookup_thread.join(2)
            status,raw=self.request('/api/status')
        data=json.loads(raw)
        self.assertEqual(status,200)
        self.assertEqual(data['lookup_job']['state'],'failed')
        self.assertIn('SHARED_REAL_TOKEN_UNAVAILABLE',data['lookup_job']['message'])
        self.assertIn('check --environment real',data['lookup_job']['message'])
        self.assertIsNone(data['history'])
        self.assertNotIn('12345678',raw.decode())

    def test_unexpected_error_is_sanitized_and_retry_is_possible(self):
        with patch.object(self.app,'engine'), patch.object(d,'History') as reader:
            reader.return_value.load.side_effect=ValueError('private-secret')
            self.app.action('historical',{'date':'2026-11-13'})
            self.app.lookup_thread.join(2)
            self.assertEqual(self.app.lookup_job['state'],'failed')
            self.assertNotIn('private-secret',self.app.lookup_job['message'])
            reader.return_value.load.side_effect=None
            reader.return_value.load.return_value={'cutoff':'2026-11-13','chart':[]}
            self.app.action('historical',{'date':'2026-11-13'})
            self.app.lookup_thread.join(2)
        self.assertEqual(self.app.lookup_job['state'],'succeeded')

    def test_api_version_and_updated_source_are_reported(self):
        status=json.loads(self.request('/api/status')[1])
        self.assertEqual(status['api_version'],'2026-11-risk4')
        self.assertFalse(status['restart_required'])
        self.app.enabled=True
        with patch.object(d,'source_fingerprint',return_value='changed-source'):
            status=json.loads(self.request('/api/status')[1])
            self.assertTrue(status['restart_required'])
            self.assertFalse(self.app.may_submit())

    def test_stop_turns_off_both_entry_and_protective_monitor(self):
        self.app.enabled=False;self.app.risk_active=True
        self.app.action('stop',{})
        self.assertFalse(self.app.enabled);self.assertFalse(self.app.risk_active)
        self.assertEqual(self.app.risk_snapshot['status'],'DISARMED')

    def test_risk_monitor_does_not_wait_on_history_io_lock(self):
        from concurrent.futures import ThreadPoolExecutor
        self.app.risk_active=True
        with patch.object(self.app,'engine') as factory:
            factory.return_value.risk.check.return_value={'status':'WATCHING'}
            self.app.io_lock.acquire()
            try:
                with ThreadPoolExecutor(max_workers=1) as pool:pool.submit(self.app.risk_tick).result(timeout=2)
            finally:self.app.io_lock.release()
        self.assertEqual(self.app.risk_snapshot['status'],'WATCHING')

    def test_stop_backtest_includes_same_cost_without_stop_controls(self):
        with patch.object(self.app,'engine'),patch.object(d,'History') as reader,patch.object(d,'simulate') as simulator:
            reader.return_value.load.return_value={'cutoff':'2026-11-13','chart':[]}
            simulator.side_effect=lambda data,**options:dict(net_pnl=-10 if options['stop_loss_enabled'] else -20,stop_count=1 if options['stop_loss_enabled'] else 0)
            self.app.action('backtest',{'date':'2026-11-13','stop_loss_enabled':True,'stop_loss_pct':1,'fee_bps':2,'strategy':'vwap_hold'})
            self.app.lookup_thread.join(2)
            self.assertEqual(self.app.lookup_job['state'],'succeeded')
            self.assertEqual(len(simulator.call_args_list),10)
            self.assertEqual(self.app.backtest['without_stop']['vwap_hold']['net_pnl'],-20)
            self.assertEqual(self.app.backtest['comparison']['vwap_hold']['net_pnl'],-10)
            self.assertTrue(all(call.kwargs['fee_bps']==2 for call in simulator.call_args_list))

    def test_explicit_start_can_resume_latched_exit_without_new_entries(self):
        cfg=self.app.strategy();cfg['stop_loss_enabled']=True
        self.app.action('strategy',cfg)
        with patch.object(d.SharedToken,'read',return_value=('k','s','t')),patch.object(self.app,'client'),patch.object(self.app,'engine') as factory:
            factory.return_value.risk.latched.return_value=True
            factory.return_value.risk.position.return_value={'quantity':1,'pending':[]}
            self.app.action('start',{})
            self.assertFalse(self.app.enabled);self.assertTrue(self.app.risk_active)
            factory.return_value.risk.reset.assert_not_called()
            self.app.action('stop',{})
            factory.return_value.risk.position.return_value={'quantity':0,'pending':[]}
            self.app.action('start',{})
            factory.return_value.risk.reset.assert_called_once()
            self.assertTrue(self.app.enabled);self.assertTrue(self.app.risk_active)

if __name__=='__main__':unittest.main()
