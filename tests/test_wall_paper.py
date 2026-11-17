from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta
from pathlib import Path
import tempfile
import unittest
from wall_paper import WallPaper
from trader import KST,TradingError


class PaperTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'state.db';self.book=WallPaper(self.path);self.addCleanup(self.book.close)
        self.start=datetime(2026,11,16,10,30,10,tzinfo=KST)
    def data(self,now,spot=776,price=10000,above=True,complete=True,call=800):
        end=now.replace(minute=now.minute-now.minute%3,second=0,microsecond=0)
        rows=[dict(time=(end-timedelta(minutes=6)).isoformat(),end=(end-timedelta(minutes=3)).isoformat(),high=9990,histogram=1),
              dict(time=(end-timedelta(minutes=3)).isoformat(),end=end.isoformat(),high=10020,close=10010,macd=8,signal=3,histogram=5,vwap=9000 if above else 11000,vwap_change=1)]
        snapshot=dict(symbol='069500',price_time=now.replace(second=0).isoformat(),bars=35,required_bars=35,chart=rows,
            flow=dict(kind='kospi_intraday',market='KOSPI',observed_at=now.isoformat(),comparison_3m_at=(now-timedelta(minutes=3)).isoformat(),comparison_3m_seconds=180,
                      foreign_change_3m=10,institution_change_3m=5))
        wall=lambda strike:dict(strike=strike,oi=100,received_at=now.isoformat())
        oi=dict(expiry='202612',observed_at=now.isoformat(),spot=spot,put_wall=wall(775),call_wall=wall(call),coverage_complete=complete)
        quote=dict(price=price,observed_at=now.isoformat())
        return snapshot,oi,quote
    def step(self,now,**kwargs):return self.book.step(*self.data(now,**kwargs),now=now)
    def enter(self,above=True):
        self.step(self.start,above=above)
        return self.step(self.start+timedelta(minutes=3),above=above)

    def test_touch_does_not_fill_until_a_later_confirmed_rebound(self):
        first=self.step(self.start)
        self.assertEqual(first['state']['status'],'ARMED');self.assertEqual(first['events'],[])
        same=self.step(self.start+timedelta(seconds=10))
        self.assertEqual(same['quantity'],0)
        entered=self.step(self.start+timedelta(minutes=3))
        self.assertGreater(entered['quantity'],0)
        self.assertEqual(entered['events'][0]['reason'],'REBOUND_ENTRY')
        self.assertEqual(entered['events'][0]['execution'],'LOCAL_PAPER_ONLY')
        self.assertLessEqual(entered['capital'] if 'capital' in entered else entered['state']['capital'],10000000)
        self.assertEqual((entered['state']['position']['target1'],entered['state']['position']['target2']),(790,795))
        self.assertEqual(entered['state']['position']['put_stop'],775*.997)
        self.assertLessEqual(10000000-entered['state']['cash'],4000000)

    def test_below_vwap_uses_half_initial_allocation(self):
        result=self.enter(False)
        self.assertGreater(result['quantity'],0)
        self.assertLessEqual(10000000-result['state']['cash'],2000000)

    def test_missing_data_and_partial_oi_cannot_enter(self):
        result=self.step(self.start,complete=False)
        self.assertEqual(result['quantity'],0);self.assertIsNone(result['state']['armed'])
        etf,oi,quote=self.data(self.start);etf['flow']['foreign_change_3m']=None
        result=self.book.step(etf,oi,quote,self.start)
        self.assertEqual(result['state']['status'],'DATA_WAIT')

    def test_failed_or_missed_first_confirmation_is_not_backfilled_later(self):
        self.step(self.start)
        now=self.start+timedelta(minutes=6)
        result=self.step(now)
        self.assertEqual(result['quantity'],0)
        self.assertEqual(result['state']['status'],'ARM_EXPIRED')
        self.assertEqual(result['events'],[])

    def test_rebound_requires_both_close_above_previous_high_and_improving_histogram(self):
        for kind in ('price','momentum'):
            self.book.reset(10000000);self.step(self.start)
            now=self.start+timedelta(minutes=3);etf,oi,quote=self.data(now)
            if kind=='price':etf['chart'][-1]['close']=9900
            else:etf['chart'][-1]['histogram']=0
            result=self.book.step(etf,oi,quote,now)
            self.assertEqual(result['quantity'],0)
            self.assertEqual(result['events'],[])

    def test_quote_before_signal_end_is_not_used_for_a_retroactive_fill(self):
        self.step(self.start)
        now=self.start+timedelta(minutes=3);etf,oi,quote=self.data(now)
        quote['observed_at']=now.replace(second=0).isoformat()
        quote['observed_at']=(datetime.fromisoformat(quote['observed_at'])-timedelta(seconds=1)).isoformat()
        result=self.book.step(etf,oi,quote,now)
        self.assertEqual(result['quantity'],0)

    def test_partial_profit_targets_are_once_only_and_do_not_follow_new_walls(self):
        entry=self.enter();initial=entry['quantity']
        first=self.step(self.start+timedelta(minutes=4),spot=790,price=10100)
        self.assertEqual(first['quantity'],initial-int(initial*.3))
        same=self.step(self.start+timedelta(minutes=4,seconds=10),spot=790,price=10100)
        self.assertEqual(len(first['events']),len(same['events']))
        second=self.step(self.start+timedelta(minutes=5),spot=795,price=10200,call=825)
        self.assertEqual(second['quantity'],initial-2*int(initial*.3))
        self.assertEqual(second['state']['position']['call'],800)
        self.assertGreaterEqual(second['state']['position']['stop_price'],entry['average_entry'])

    def breakout(self):
        entry=self.enter()
        self.step(self.start+timedelta(minutes=4),spot=790,price=10100)
        self.step(self.start+timedelta(minutes=5),spot=795,price=10200)
        first=self.step(self.start+timedelta(minutes=6),spot=801,price=10300,call=825)
        self.assertEqual(first['state']['position']['breakout_count'],1)
        repeated=self.step(self.start+timedelta(minutes=6,seconds=10),spot=801,price=10300,call=825)
        self.assertEqual(repeated['state']['position']['breakout_count'],1)
        added=self.step(self.start+timedelta(minutes=9),spot=802,price=10300,call=825)
        return entry,added

    def test_breakout_requires_two_separate_bar_confirmations_and_adds_only_once(self):
        entry,added=self.breakout()
        self.assertTrue(added['state']['position']['added'])
        self.assertEqual([e['reason'] for e in added['events']].count('BREAKOUT_ADD'),1)
        again=self.step(self.start+timedelta(minutes=12),spot=803,price=10300,call=825)
        self.assertEqual([e['reason'] for e in again['events']].count('BREAKOUT_ADD'),1)
        self.assertGreaterEqual(again['state']['cash'],0)
        self.assertLess(again['quantity']*10300,10000000)

    def test_breakout_failure_sells_only_additional_lot_and_keeps_stop_floor(self):
        entry,added=self.breakout();p=added['state']['position'];base=sum(l['quantity'] for l in p['lots'] if l['kind']=='base');stop=p['stop_price']
        result=self.step(self.start+timedelta(minutes=10),spot=798,price=10250)
        self.assertEqual(result['quantity'],base)
        self.assertEqual(result['events'][-1]['reason'],'BREAKOUT_FAIL')
        self.assertEqual(result['state']['position']['stop_price'],stop)
        self.assertTrue(result['state']['position']['added'])
        self.assertIsNotNone(result['state']['cooldown_until'])

    def test_one_percent_stop_is_evaluated_without_oi_or_indicators(self):
        self.enter();now=self.start+timedelta(minutes=4)
        etf,oi,quote=self.data(now,price=9800);etf['chart']=[];etf['flow']={}
        result=self.book.step(etf,{},quote,now)
        self.assertEqual(result['quantity'],0)
        self.assertEqual(result['events'][-1]['reason'],'ETF_STOP')
        self.assertLess(result['net_pnl'],0)
        self.assertEqual(result['state']['losses']['202612:775'],1)

    def test_put_stop_uses_frozen_entry_wall_even_if_current_wall_moves(self):
        self.enter();now=self.start+timedelta(minutes=4);etf,oi,quote=self.data(now,spot=772,price=10000)
        oi['put_wall']['strike']=750
        result=self.book.step(etf,oi,quote,now)
        self.assertEqual(result['quantity'],0);self.assertEqual(result['events'][-1]['reason'],'PUT_STOP')

    def test_cooldown_and_two_stop_limit(self):
        self.enter();first_stop=self.start+timedelta(minutes=4);self.step(first_stop,price=9800)
        self.assertEqual(self.step(first_stop+timedelta(minutes=5))['state']['status'],'COOLDOWN')
        rearm=self.start+timedelta(minutes=21);self.step(rearm)
        second=self.step(rearm+timedelta(minutes=3));self.assertGreater(second['quantity'],0)
        self.step(rearm+timedelta(minutes=4),price=9800)
        result=self.step(rearm+timedelta(minutes=21))
        self.assertEqual(result['state']['status'],'PUT_HALTED')
        self.assertEqual(result['state']['losses']['202612:775'],2)

    def test_end_of_day_and_restart_do_not_create_overnight_entries(self):
        entry=self.enter();self.book.close();self.book=WallPaper(self.path);self.addCleanup(self.book.close)
        now=self.start.replace(hour=15,minute=15)
        result=self.step(now,price=10100)
        self.assertEqual(result['events'][-1]['reason'],'DAY_EXIT');self.assertEqual(result['quantity'],0)
        self.assertEqual(self.step(now+timedelta(seconds=10))['state']['status'],'ENTRY_CUTOFF')
        self.assertAlmostEqual(result['net_pnl'],result['state']['realized'],places=2)

    def test_stale_price_and_after_hours_never_fill(self):
        self.enter();now=self.start+timedelta(minutes=4);etf,oi,quote=self.data(now,price=9800)
        etf['price_time']=(now-timedelta(minutes=3)).isoformat()
        result=self.book.step(etf,oi,quote,now);self.assertGreater(result['quantity'],0)
        result=self.step(now.replace(hour=18),price=9800)
        self.assertGreater(result['quantity'],0);self.assertEqual(result['state']['status'],'CLOSED')

    def test_reset_does_not_erase_history_and_cannot_discard_open_position(self):
        self.enter()
        with self.assertRaises(TradingError):self.book.reset(20000000)
        now=self.start+timedelta(minutes=4);result=self.book.step(*self.data(now),now=now,force_exit=True)
        self.assertEqual(result['quantity'],0)
        old_run=result['state']['run_id'];self.book.reset(20000000)
        self.assertNotEqual(self.book.view()['state']['run_id'],old_run)
        self.assertEqual(self.book.view()['state']['capital'],20000000)
        self.assertEqual(self.book.db.execute('SELECT count(*) FROM wall_paper_events WHERE run_id=?',(old_run,)).fetchone()[0],2)

    def test_new_first_wall_selection_never_rewrites_an_open_trade_box(self):
        for offset in (0,3):
            now=self.start+timedelta(minutes=offset)
            etf,oi,quote=self.data(now,spot=751)
            oi['put_wall']['strike']=750
            oi['wall_selection']=dict(mode='old_largest')
            before=self.book.step(etf,oi,quote,now)
        self.assertEqual(before['state']['position']['put'],750)
        now=self.start+timedelta(minutes=4);etf,oi,quote=self.data(now,spot=786)
        oi['put_wall']['strike']=780
        oi['wall_selection']=dict(mode='nearest_material_peak')
        after=self.book.step(etf,oi,quote,now)
        self.assertEqual(after['state']['position']['put'],750)
        self.assertEqual(after['state']['position']['put_stop'],750*.997)
        self.assertEqual(after['state']['position']['wall_selection']['mode'],'old_largest')

    def test_concurrent_rebound_evaluations_fill_only_once(self):
        self.step(self.start);now=self.start+timedelta(minutes=3)
        def run(_):
            book=WallPaper(self.path)
            try:return book.step(*self.data(now),now=now)
            finally:book.close()
        with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(run,range(2)))
        result=self.book.view()
        self.assertEqual(len(result['events']),1)
        self.assertEqual(result['events'][0]['reason'],'REBOUND_ENTRY')
