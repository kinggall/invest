from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
import test_wall_paper
from trader import TradingError
from wall_paper import WallPaper
from wall_broker import WallBroker


class Demo:
    def __init__(self):
        self.account={'CANO':'12345678','ACNT_PRDT_CD':'01'}
        self.tokens=Mock();self.sent=[];self.matches={};self.cancelled=[];self.holdings=0;self.fail=False
        self.cash=100000000;self.limit=100000
    def balance(self,symbol):return dict(quantity=self.holdings,deposit=self.cash,observed_at='2026-11-16T10:30:00+09:00')
    def buying_power(self,symbol,price):return dict(cash=self.cash,quantity=self.limit)
    def order(self,strategy):
        self.sent.append(dict(strategy))
        if self.fail:raise TradingError('REQUEST_UNCERTAIN')
        return str(len(self.sent)),'001'
    def fills(self,row):return self.matches.get(row['order_no'])
    def cancel(self,row):self.cancelled.append(row['order_no'])


class BrokerTests(unittest.TestCase):
    data=test_wall_paper.PaperTests.data
    def setUp(self):
        from datetime import datetime
        from trader import KST
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'orders.sqlite3';self.client=Demo();self.book=WallBroker(self.path,self.client);self.addCleanup(self.book.close)
        self.start=datetime(2026,11,16,10,30,10,tzinfo=KST)
    def step(self,now,**kwargs):return self.book.step(*self.data(now,**kwargs),now=now)
    def enter(self):
        self.step(self.start);return self.step(self.start+timedelta(minutes=3))
    def fill(self,filled=None,price=10010,status='FILLED'):
        order=self.client.sent[-1];q=order['quantity'] if filled is None else filled
        number=str(len(self.client.sent));self.client.matches[number]=(status,q,q*price if q else None)
        self.client.holdings=q if order['side']=='buy' else max(0,self.client.holdings-q)
        return self.book.sync(self.start+timedelta(minutes=3,seconds=10))

    def test_signal_submits_once_without_fabricating_a_fill(self):
        view=self.enter();self.assertEqual(view['mode'],'KIS_DEMO');self.assertEqual(len(self.client.sent),1)
        self.assertEqual(view['quantity'],0);self.assertEqual(view['events'],[]);self.assertEqual(view['pending_count'],1)
        order=self.client.sent[0];self.assertEqual(order['symbol'],'069500');self.assertEqual(order['side'],'buy');self.assertEqual(order['order_type'],'limit')
        self.step(self.start+timedelta(minutes=3,seconds=10));self.assertEqual(len(self.client.sent),1)

    def test_partial_fills_are_incremental_idempotent_and_survive_restart(self):
        self.enter();v=self.fill(10,status='PARTIAL');self.assertEqual(v['quantity'],10);self.assertEqual(len(v['events']),1)
        self.book.sync(self.start+timedelta(minutes=3,seconds=15));self.assertEqual(len(self.book.view()['events']),1)
        other=WallBroker(self.path,self.client)
        try:
            q=self.client.sent[0]['quantity'];self.client.holdings=q;self.client.matches['1']=('FILLED',q,q*10015)
            v=other.sync(self.start+timedelta(minutes=3,seconds=20))
            self.assertEqual(v['quantity'],q);self.assertEqual(v['state']['position']['original_quantity'],q)
            self.assertEqual(sum(e['quantity'] for e in v['events']),q);self.assertEqual(v['pending_count'],0)
            self.assertAlmostEqual(v['average_entry'],10015)
            self.assertTrue(all(e['execution']=='KIS_DEMO_CONFIRMED' for e in v['events']))
        finally:other.close()

    def test_timeout_and_restart_never_resubmit(self):
        self.client.fail=True
        with self.assertRaisesRegex(TradingError,'UNCERTAIN'):self.enter()
        other=WallBroker(self.path,self.client)
        try:
            other.step(*self.data(self.start+timedelta(minutes=4)),now=self.start+timedelta(minutes=4))
            self.assertEqual(len(self.client.sent),1);self.assertEqual(other.view()['orders'][0]['status'],'UNKNOWN')
            with self.assertRaises(TradingError):other.start_check()
            with self.assertRaises(TradingError):other.reset(10000000)
        finally:other.close()

    def test_terminal_cancel_without_fill_never_creates_holdings(self):
        self.enter();self.fill(0,status='CANCELLED');v=self.book.view()
        self.assertEqual(v['quantity'],0);self.assertEqual(v['pending_count'],0);self.assertEqual(v['events'],[])

    def test_open_order_cancel_is_durable_and_never_repeated(self):
        self.enter();self.client.matches['1']=('SUBMITTED',0,None)
        now=self.start+timedelta(minutes=4)
        self.book.sync(now,allow_cancel=True);self.book.sync(now+timedelta(seconds=10),allow_cancel=True)
        self.assertEqual(self.client.cancelled,['1']);self.assertEqual(self.book.view()['pending_count'],1)
        self.fill(0,status='CANCELLED');self.assertEqual(self.book.view()['pending_count'],0)

    def test_paused_sync_only_reads_and_does_not_cancel(self):
        self.enter();self.client.matches['1']=('SUBMITTED',0,None)
        self.book.sync(self.start+timedelta(minutes=10));self.assertEqual(self.client.cancelled,[])

    def test_ledger_uses_actual_execution_price_and_stop_waits_for_fill(self):
        self.enter();v=self.fill(price=10020);q=v['quantity'];self.assertEqual(v['average_entry'],10020)
        v=self.step(self.start+timedelta(minutes=4),price=9800)
        self.assertEqual(len(self.client.sent),2);self.assertEqual(self.client.sent[-1]['order_type'],'market');self.assertEqual(v['quantity'],q)
        self.client.matches['2']=('FILLED',q,q*9790);self.client.holdings=0
        v=self.book.sync(self.start+timedelta(minutes=4,seconds=5));self.assertEqual(v['quantity'],0);self.assertEqual(v['pending_count'],0)
        self.assertLess(v['state']['realized'],0);self.assertEqual(v['events'][-1]['reason'],'ETF_STOP')

    def test_partial_profit_only_changes_stage_after_terminal_confirmation(self):
        self.enter();self.fill();initial=self.book.view()['quantity']
        v=self.step(self.start+timedelta(minutes=4),spot=791)
        self.assertEqual(self.client.sent[-1]['side'],'sell');self.assertFalse(v['state']['position']['tp1'])
        q=self.client.sent[-1]['quantity'];self.client.matches['2']=('PARTIAL',1,10100)
        self.book.sync(self.start+timedelta(minutes=4,seconds=5));self.assertFalse(self.book.view()['state']['position']['tp1'])
        self.client.matches['2']=('CANCELLED',1,10100)
        v=self.book.sync(self.start+timedelta(minutes=4,seconds=10));self.assertTrue(v['state']['position']['tp1']);self.assertEqual(v['quantity'],initial-1)

    def test_buying_power_and_account_scope_are_enforced(self):
        self.client.limit=3;self.enter();self.assertEqual(self.client.sent[0]['quantity'],3)
        other=Demo();other.account['CANO']='87654321'
        with self.assertRaisesRegex(TradingError,'ACCOUNT_CHANGED'):WallBroker(self.path,other)

    def test_external_balance_change_blocks_sell(self):
        self.enter();self.fill();self.client.holdings=0
        with self.assertRaisesRegex(TradingError,'BALANCE_MISMATCH'):self.step(self.start+timedelta(minutes=4),price=9800)
        self.assertEqual(len(self.client.sent),1)

    def test_paper_positions_are_not_imported(self):
        paper=WallPaper(self.path)
        try:
            paper.step(*self.data(self.start),now=self.start);paper.step(*self.data(self.start+timedelta(minutes=3)),now=self.start+timedelta(minutes=3))
            self.assertGreater(paper.view()['quantity'],0);self.assertEqual(self.book.view()['quantity'],0)
        finally:paper.close()

    def test_cli_reconciled_fills_are_applied_once_even_if_order_row_already_updated(self):
        self.enter();q=self.client.sent[-1]['quantity']
        self.book.db.execute("UPDATE orders SET filled=?,filled_cash=?,status='FILLED'",(q,q*10010))
        self.client.matches['1']=('FILLED',q,q*10010)
        v=self.book.sync(self.start+timedelta(minutes=4));self.assertEqual(v['quantity'],q);self.assertEqual(v['pending_count'],0)
        self.book.sync(self.start+timedelta(minutes=5));self.assertEqual(len(self.book.view()['events']),1)

    def test_missing_fill_price_blocks_new_orders(self):
        self.enter();self.client.matches['1']=('FILLED',2,None)
        with self.assertRaisesRegex(TradingError,'PRICE_UNAVAILABLE'):self.step(self.start+timedelta(minutes=4))
        self.assertEqual(len(self.client.sent),1);self.assertEqual(self.book.view()['quantity'],0)

    def test_pause_during_buying_power_query_prevents_order(self):
        self.step(self.start)
        now=self.start+timedelta(minutes=3)
        v=self.book.step(*self.data(now),now=now,may_submit=lambda:False)
        self.assertEqual(self.client.sent,[]);self.assertEqual(v['pending_count'],0)

    def test_two_connections_do_not_double_apply_a_fill(self):
        self.enter();q=self.client.sent[-1]['quantity'];self.client.matches['1']=('FILLED',q,q*10010)
        other=WallBroker(self.path,self.client)
        original=self.client.fills;fired=[False]
        def interleaved(row):
            if not fired[0]:
                fired[0]=True;other.sync(self.start+timedelta(minutes=4))
            return original(row)
        self.client.fills=interleaved
        try:
            self.book.sync(self.start+timedelta(minutes=4))
            self.assertEqual(self.book.view()['quantity'],q);self.assertEqual(len(self.book.view()['events']),1)
        finally:other.close()

    def test_actual_demo_adapter_routes_signal_order_and_fill_end_to_end(self):
        import trader as t
        calls=[];submitted={}
        def send(path,headers,payload,post):
            calls.append((path,headers['tr_id'],post))
            if path.endswith('inquire-balance'):
                return dict(rt_cd='0',output1=[dict(pdno='069500',hldg_qty=str(submitted.get('filled',0)))],output2=[dict(dnca_tot_amt='10000000')])
            if path.endswith('inquire-psbl-order'):
                return dict(rt_cd='0',output=dict(nrcvb_buy_amt='10000000',nrcvb_buy_qty='1000'))
            if path==t.ORDER:
                submitted.update(payload)
                return dict(rt_cd='0',output=dict(ODNO='000123',KRX_FWDG_ORD_ORGNO='001'))
            if path==t.FILLS:
                q=submitted['ORD_QTY'];submitted['filled']=int(q)
                return dict(rt_cd='0',output1=[dict(odno='000123',pdno='069500',ord_dt='20261116',ord_qty=q,ord_unpr=submitted['ORD_UNPR'],
                    sll_buy_dvsn_cd='02',tot_ccld_qty=q,rmn_qty='0',cncl_yn='N',tot_ccld_amt=str(int(q)*10005),avg_prvs='10005')])
            raise AssertionError(path)
        client=t.DemoClient(self.path.parent,'12345678','01',send,0);client.tokens.read=lambda:('key','secret','token')
        book=WallBroker(self.path.parent/'integration.sqlite3',client)
        try:
            book.step(*self.data(self.start),now=self.start)
            now=self.start+timedelta(minutes=3)
            result=book.step(*self.data(now),now=now)
            self.assertEqual(result['quantity'],0);self.assertEqual(result['orders'][0]['status'],'SUBMITTED')
            result=book.sync(now+timedelta(seconds=5))
            self.assertEqual(result['average_entry'],10005);self.assertEqual(result['quantity'],int(submitted['ORD_QTY']))
            self.assertEqual([tr for path,tr,post in calls if post],['VTTC0012U'])
            self.assertTrue(all(tr.startswith('V') for path,tr,post in calls))
        finally:book.close()

    def test_day_exit_ignores_missing_indicators_and_quotes_before_1519(self):
        self.enter();self.fill();q=self.book.view()['quantity']
        now=self.start.replace(hour=15,minute=18,second=0)
        v=self.book.step({}, {}, {},now=now)
        self.assertEqual(self.client.sent[-1]['side'],'sell');self.assertEqual(self.client.sent[-1]['order_type'],'market')
        self.assertEqual(self.client.sent[-1]['quantity'],q);self.assertEqual(v['orders'][0]['reason'],'DAY_EXIT')
        self.assertEqual(v['quantity'],q) # Submitted is not treated as filled.
        self.client.matches['2']=('FILLED',q,q*10000);self.client.holdings=0
        v=self.book.step({}, {}, {},now=now+timedelta(seconds=10))
        self.assertEqual(v['quantity'],0);self.assertEqual(v['pending_count'],0);self.assertEqual(v['state']['status'],'CLOSED_FLAT')

    def test_close_cancels_recent_buy_then_sells_only_confirmed_partial_fill(self):
        self.enter()
        row=self.book.db.execute('SELECT id,payload FROM broker_meta').fetchone();meta=json.loads(row['payload'])
        meta['time']=self.start.replace(hour=15,minute=14,second=55).isoformat()
        self.book.db.execute('UPDATE broker_meta SET payload=? WHERE id=?',(json.dumps(meta),row['id']))
        self.client.matches['1']=('PARTIAL',7,70070);self.client.holdings=7
        now=self.start.replace(hour=15,minute=15,second=0)
        self.book.step({}, {}, {},now=now)
        self.assertEqual(self.client.cancelled,['1']);self.assertEqual(len(self.client.sent),1)
        self.client.matches['1']=('CANCELLED',7,70070)
        self.book.step({}, {}, {},now=now+timedelta(seconds=10))
        self.assertEqual(len(self.client.sent),2);self.assertEqual(self.client.sent[-1]['quantity'],7)
        self.assertEqual(self.client.sent[-1]['side'],'sell')

    def test_full_market_exit_is_not_cancelled_during_closing_auction(self):
        self.enter();self.fill();q=self.book.view()['quantity']
        now=self.start.replace(hour=15,minute=19,second=0)
        self.book.step({}, {}, {},now=now)
        self.client.matches['2']=('PARTIAL',3,30000);self.client.holdings=q-3
        self.book.step({}, {}, {},now=now+timedelta(minutes=5))
        self.assertEqual(self.client.cancelled,[]);self.assertEqual(len(self.client.sent),2)

    def test_after_close_residual_is_alerted_without_out_of_session_order(self):
        self.enter();self.fill()
        v=self.book.step({}, {}, {},now=self.start.replace(hour=15,minute=30))
        self.assertEqual(len(self.client.sent),1);self.assertEqual(v['state']['status'],'CLOSE_FAILED')
        self.assertIn('즉시 확인',v['state']['message'])

    def test_close_unknown_order_alerts_and_does_not_blind_sell(self):
        self.client.fail=True
        with self.assertRaises(TradingError):self.enter()
        v=self.book.step({}, {}, {},now=self.start.replace(hour=15,minute=16))
        self.assertEqual(v['state']['status'],'CLOSE_BLOCKED');self.assertEqual(len(self.client.sent),1)

    def test_local_preflight_block_does_not_leave_an_unknown_order(self):
        from trader import OrderNotSent
        def stop_before_network(strategy):raise OrderNotSent('closed')
        self.client.order=stop_before_network
        with self.assertRaises(OrderNotSent):self.enter()
        v=self.book.view();self.assertEqual(v['pending_count'],0);self.assertEqual(v['quantity'],0)
        self.assertEqual(v['orders'][0]['status'],'CANCELLED')

    def test_1519_open_exit_is_late_and_1520_never_submits_to_auction(self):
        self.enter();self.fill();q=self.book.view()['quantity']
        now=self.start.replace(hour=15,minute=18,second=0)
        self.book.step({}, {}, {},now=now)
        self.client.matches['2']=('SUBMITTED',0,None)
        v=self.book.step({}, {}, {},now=now+timedelta(minutes=1))
        self.assertEqual(v['state']['status'],'CLOSE_DELAYED');self.assertEqual(self.client.cancelled,[])
        self.book.step({}, {}, {},now=now+timedelta(minutes=2))
        self.assertEqual(len(self.client.sent),2);self.assertEqual(self.client.cancelled,[])
        self.client.matches['2']=('FILLED',q,q*10000);self.client.holdings=0
        v=self.book.step({}, {}, {},now=now+timedelta(minutes=12))
        self.assertEqual(v['state']['status'],'CLOSED_FLAT');self.assertEqual(v['quantity'],0)

    def test_1520_unsent_holding_never_creates_new_auction_order(self):
        self.enter();self.fill()
        v=self.book.step({}, {}, {},now=self.start.replace(hour=15,minute=20))
        self.assertEqual(len(self.client.sent),1);self.assertEqual(v['state']['status'],'CLOSE_FAILED')
