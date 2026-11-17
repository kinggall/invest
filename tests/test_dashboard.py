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
        self.assertEqual(status,200);self.assertIn('id="tabPaper"',raw.decode());self.assertIn('id="tabOI"',raw.decode());self.assertIn('id="tabReplay"',raw.decode());self.assertNotIn('id="start"',raw.decode())
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
        self.assertEqual(restarted.strategy()['symbol'],'069500')
        self.assertEqual(restarted.strategy()['mode'],'macd')
        with restarted.db() as db:
            old=json.loads(db.execute('SELECT value FROM settings WHERE id=2').fetchone()[0])
        self.assertEqual(old['symbol'],'005930')
        self.assertFalse(restarted.enabled)

    def test_previous_etf_settings_are_archived_and_orders_preserved(self):
        from macd_strategy import MacdEngine
        broker=Mock();broker.account={'CANO':'12345678','ACNT_PRDT_CD':'01'}
        engine=MacdEngine(self.app.state,broker)
        try:
            order=dict(id='old',symbol='472150',side='buy',quantity=2,limit_price=15000)
            engine.db.execute('INSERT INTO orders(run_id,scope,day,symbol,quantity,strategy,status,filled) VALUES (?,?,?,?,?,?,?,?)',
                ('old',engine.scope,'20261116','472150',2,json.dumps(order),'FILLED',2))
            with self.app.db() as db:
                previous=dict(self.app.strategy(),symbol='472150',id='tiger-macd-v1')
                db.execute('UPDATE settings SET value=? WHERE id=1',(json.dumps(previous),))
            restarted=d.Dashboard(self.app.home,self.app.state)
            self.assertEqual(restarted.strategy()['symbol'],'069500')
            self.assertFalse(restarted.enabled)
            with restarted.db() as db:
                self.assertEqual(json.loads(db.execute('SELECT value FROM settings WHERE id=4').fetchone()[0]),previous)
            self.assertEqual(restarted.previous_exposure(engine)['quantity'],2)
            with patch.object(d,'SharedToken'),patch.object(restarted,'client',return_value=broker),patch.object(restarted,'engine',return_value=engine):
                with self.assertRaisesRegex(TradingError,'LEGACY_TRADING_REMOVED'):restarted.action('start',{})
            self.assertFalse(restarted.enabled)
            broker.order.assert_not_called()
        finally:engine.db.close()

    def test_confirmation_is_read_only_and_records_sources_without_duplicates(self):
        self.app.snapshot=dict(symbol='069500',chart=[],flow={})
        self.app.record_confirmation();self.app.record_confirmation()
        with self.app.db() as db:
            rows=db.execute('SELECT payload FROM confirmation_samples').fetchall()
        self.assertEqual(len(rows),1)
        value=json.loads(rows[0][0])
        self.assertEqual(value['symbol'],'069500')
        self.assertEqual(value['execution'],'OBSERVE_ONLY')
        self.assertFalse(self.app.enabled)
        self.assertFalse(self.app.risk_active)

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

    def test_active_wall_worker_respects_rate_cooldown_before_retrying(self):
        from datetime import datetime
        from trader import KST,RateLimitError
        self.app.wall_enabled=True;clock=[100.0];turn=[0]
        def wait(seconds):
            turn[0]+=1
            if turn[0]>3:return True
            clock[0]=[100.,110.,131.][turn[0]-1]
            return False
        with patch.object(self.app.shutdown,'wait',side_effect=wait),patch.object(d.time,'monotonic',side_effect=lambda:clock[0]),patch.object(d,'datetime') as dates,patch.object(self.app,'tick',side_effect=[RateLimitError(),None]) as tick:
            dates.now.return_value=datetime(2026,11,16,10,30,tzinfo=KST)
            self.app.worker()
        self.assertEqual(tick.call_count,2)
        self.assertEqual(self.app.retry_after,130)
        self.assertTrue(self.app.wall_enabled)
        self.assertIn('EGW00201',self.app.wall_message)

    def test_oi_rate_error_keeps_observer_enabled_for_next_scheduled_attempt(self):
        self.app.oi_active=True
        provider=Mock();provider.collect.side_effect=TradingError('API_RATE_LIMIT [EGW00201]')
        with patch.object(d,'OIService',return_value=provider):self.app.run_oi(self.app.oi_config)
        self.assertTrue(self.app.oi_active)
        self.assertIn('30초',self.app.oi_job['message'])

    def test_offline_replay_never_constructs_a_quote_client_or_changes_wall_state(self):
        before=self.app.wall_status()['state']
        result=dict(ready_samples=0,mode='minute',date='2026-11-16')
        with patch.object(d,'replay',return_value=result) as run,patch.object(self.app,'client',side_effect=AssertionError('No network')):
            self.app.action('replay',dict(date='2026-11-16',mode='minute',capital=10000000))
            self.app.replay_thread.join(2)
        self.assertEqual(self.app.replay_result,result)
        self.assertEqual(self.app.replay_job['state'],'succeeded')
        self.assertIn('성과 판단 보류',self.app.replay_job['message'])
        self.assertEqual(self.app.wall_status()['state'],before)
        run.assert_called_once()

    def test_missing_replay_data_is_visible_and_retryable(self):
        with patch.object(d,'replay',side_effect=TradingError('과거 OI 기록이 없습니다')):
            self.app.action('replay',dict(date='2026-11-16'))
            self.app.replay_thread.join(2)
        self.assertEqual(self.app.replay_job['state'],'failed')
        self.assertIn('OI 기록',self.app.replay_job['message'])
        self.assertIsNone(self.app.replay_result)

    def test_wall_broker_starts_without_enabling_legacy_orders(self):
        self.app.oi_active=True
        with patch.object(d,'SharedToken'),patch.object(self.app,'client'),patch.object(d,'WallBroker'):
            self.app.action('wall_start',{})
        self.assertTrue(self.app.wall_enabled)
        self.assertFalse(self.app.enabled);self.assertFalse(self.app.risk_active)
        self.app.action('wall_stop',{})
        self.assertFalse(self.app.wall_enabled)
        restarted=d.Dashboard(self.app.home,self.app.state)
        self.assertFalse(restarted.wall_enabled)
        self.assertEqual(restarted.wall_status()['state']['capital'],10000000)

    def test_wall_start_does_not_double_start_an_oi_lookup_already_running(self):
        self.app.oi_job['state']='running'
        with patch.object(d,'SharedToken'),patch.object(self.app,'client'),patch.object(d.WallBroker,'start_check'),patch.object(d,'WallBroker') as broker,patch.object(self.app,'request_oi') as request:
            self.app.action('wall_start',{})
        self.assertTrue(self.app.oi_active);self.assertTrue(self.app.wall_enabled)
        request.assert_not_called()

    def test_wall_and_macd_order_execution_are_mutually_exclusive(self):
        self.app.wall_enabled=True
        with patch.object(d,'SharedToken') as token:
            with self.assertRaises(TradingError):self.app.action('start',{})
            token.assert_not_called()
        self.app.wall_enabled=False;self.app.enabled=True
        with self.assertRaises(TradingError):self.app.action('wall_start',{})

    def test_wall_stop_during_quote_io_prevents_paper_fills(self):
        from datetime import datetime
        from trader import KST
        self.app.wall_enabled=True
        client=Mock()
        def quote(symbol):
            self.assertEqual(symbol,'069500');self.app.wall_enabled=False;return 10000
        client.quote.side_effect=quote
        with patch.object(d,'datetime') as clock,patch.object(d,'WallBroker') as paper:
            clock.now.return_value=datetime(2026,11,16,10,30,tzinfo=KST)
            self.app.wall_tick({},client)
        paper.return_value.step.assert_not_called()
        client.order.assert_not_called()

    def test_futures_quote_worker_is_independent_of_trading_and_oi(self):
        snapshot=dict(session=dict(id='night:2026-11-16',mode='night'),bars=[])
        service=Mock();service.collect.return_value=snapshot
        self.app.enabled=True;self.app.risk_active=True;self.app.oi_active=True
        with patch.object(d,'FuturesChart',return_value=service),patch.object(self.app,'engine',side_effect=AssertionError('No order engine')):
            self.app.run_future()
        self.assertEqual(self.app.future_snapshot,snapshot)
        self.assertEqual(self.app.future_job['state'],'succeeded')
        self.assertTrue(self.app.enabled);self.assertTrue(self.app.risk_active);self.assertTrue(self.app.oi_active)
        service.close.assert_called_once()

    def test_futures_failure_does_not_stop_other_workers_or_relabel_old_data(self):
        self.app.enabled=True;self.app.risk_active=True;self.app.oi_active=True
        service=Mock();service.collect.side_effect=TradingError('FUTURES_SESSION_EMPTY')
        with patch.object(d,'FuturesChart',return_value=service):self.app.run_future()
        self.assertEqual(self.app.future_job['state'],'failed')
        self.assertIsNone(self.app.future_snapshot)
        self.assertTrue(self.app.enabled);self.assertTrue(self.app.risk_active);self.assertTrue(self.app.oi_active)

    def test_api_version_and_updated_source_are_reported(self):
        status=json.loads(self.request('/api/status')[1])
        self.assertEqual(status['api_version'],'2026-11-demo2')
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

    def test_removed_risk_tick_cannot_access_broker_even_if_old_flags_are_set(self):
        self.app.risk_active=True
        with patch.object(self.app,'engine') as engine:
            self.assertEqual(self.app.risk_tick()['status'],'REMOVED')
            engine.assert_not_called()


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

    def test_legacy_start_is_removed_even_when_token_and_position_are_valid(self):
        with patch.object(d,'SharedToken') as token,patch.object(self.app,'engine') as engine:
            with self.assertRaisesRegex(TradingError,'LEGACY_TRADING_REMOVED'):self.app.action('start',{})
            token.assert_not_called();engine.assert_not_called()
        self.assertFalse(self.app.enabled);self.assertFalse(self.app.risk_active)
        self.assertFalse(self.app.may_submit());self.assertFalse(self.app.may_risk_submit())


    def test_oi_lookup_is_independent_and_persists_only_observer_settings(self):
        with patch.object(d,'OIService') as provider,patch.object(self.app,'engine',side_effect=AssertionError('Trading engine must not be used')):
            provider.return_value.collect.return_value={'expiry':'202612','observed_at':'2026-11-16T10:00:00+09:00','cached':False}
            self.app.action('oi_start',{'expiry':'','radius_pct':5})
            self.app.oi_thread.join(2)
            self.assertEqual(self.app.oi_job['state'],'succeeded')
            self.assertTrue(self.app.oi_active)
            self.assertFalse(self.app.enabled);self.assertFalse(self.app.risk_active)
            self.app.action('oi_stop',{})
            self.assertFalse(self.app.oi_active)
        with self.app.db() as db:
            value=json.loads(db.execute('SELECT value FROM settings WHERE id=3').fetchone()[0])
        self.assertEqual(value,{'expiry':'','radius_pct':5})

    def test_oi_error_stops_only_observer_not_trading_or_protection(self):
        self.app.enabled=True;self.app.risk_active=True
        with patch.object(d,'OIService') as provider:
            provider.return_value.collect.side_effect=TradingError('OI_QUOTE_REJECTED')
            self.app.action('oi_start',{'expiry':'','radius_pct':5})
            self.app.oi_thread.join(2)
        self.assertEqual(self.app.oi_job['state'],'failed')
        self.assertFalse(self.app.oi_active)
        self.assertTrue(self.app.enabled);self.assertTrue(self.app.risk_active)

    def test_day_close_sweep_does_not_need_indicator_or_quote_workers(self):
        from datetime import datetime
        from trader import KST
        broker=Mock();broker.pending.return_value=[]
        broker.step.return_value=dict(state=dict(message='당일 청산 확인 완료'),quantity=0,pending_count=0)
        self.app.wall_enabled=True
        with patch.object(d,'datetime') as clock,patch.object(d,'WallBroker',return_value=broker),patch.object(self.app,'client'),patch.object(self.app,'tick',side_effect=AssertionError('No market dependency')):
            clock.now.return_value=datetime(2026,11,18,15,25,tzinfo=KST)
            self.app.sync_demo()
            self.assertEqual(broker.step.call_args.args,({}, {}, {}));self.assertTrue(broker.step.call_args.kwargs['force_exit'])
            self.assertTrue(self.app.demo_submission_allowed())
            self.app.wall_enabled=False;self.app.day_exit_armed=True
            self.app.sync_demo();self.assertEqual(broker.step.call_count,2)
            self.assertTrue(self.app.demo_submission_allowed())
            clock.now.return_value=datetime(2026,11,18,15,30,tzinfo=KST)
            self.assertFalse(self.app.demo_submission_allowed())

if __name__=='__main__':unittest.main()
