from datetime import timedelta
import unittest
import test_wall_paper as fixtures
from trader import TradingError
from wall_paper import strategy_options


class BreakoutEntryTests(unittest.TestCase):
    def setUp(self):
        fixtures.PaperTests.setUp(self)
        self.book.options=strategy_options(dict(call_breakout_entry=True))
    def data(self,at,spot=798,call=800,**kw):
        return fixtures.PaperTests.data(self,at,spot=spot,call=call,**kw)
    def step(self,at,**kw):return self.book.step(*self.data(at,**kw),now=at)
    def arm(self):
        self.step(self.start)
        return self.step(self.start+timedelta(minutes=1),spot=801,call=820)
    def confirm(self,**kw):return self.step(self.start+timedelta(minutes=3),spot=802,call=830,**kw)

    def test_cross_is_not_immediate_entry_and_next_bar_buys_twenty_percent(self):
        armed=self.arm()
        self.assertEqual(armed['state']['status'],'CALL_BREAKOUT_ARMED');self.assertEqual(armed['quantity'],0)
        value=self.confirm();p=value['state']['position']
        self.assertEqual(value['events'][0]['reason'],'CALL_BREAKOUT_ENTRY')
        self.assertLessEqual(value['quantity']*value['average_entry'],2000000)
        self.assertGreater(value['quantity']*value['average_entry'],1900000)
        self.assertEqual((p['put'],p['call']),(800,820)) # 830 must not move the target.
        self.assertAlmostEqual(p['put_stop'],798.4)
        self.assertEqual(value['events'][0]['confirmation']['call_breakout_entry']['next_call'],820)

    def test_no_prior_reference_means_no_breakout_entry(self):
        value=self.step(self.start,spot=801,call=820)
        self.assertFalse(value['events'])
        self.assertIsNone(value['state']['breakout_armed'])

    def test_old_index_observation_cannot_confirm_new_bar(self):
        self.arm();at=self.start+timedelta(minutes=3)
        etf,oi,quote=self.data(at,spot=802,call=820)
        oi['observed_at']=(self.start+timedelta(minutes=1)).isoformat()
        value=self.book.step(etf,oi,quote,now=at)
        self.assertFalse(value['events']);self.assertEqual(value['state']['status'],'CALL_BREAKOUT_DATA_WAIT')
        self.assertEqual(self.confirm()['events'][0]['reason'],'CALL_BREAKOUT_ENTRY')

    def test_support_reentry_before_confirm_cancels(self):
        self.arm()
        value=self.step(self.start+timedelta(minutes=2),spot=799,call=820)
        self.assertFalse(value['events']);self.assertEqual(value['state']['status'],'CALL_BREAKOUT_CANCELLED')

    def test_expiry_and_missing_full_oi_cannot_confirm(self):
        self.arm();at=self.start+timedelta(minutes=3)
        value=self.step(at,spot=802,call=820,complete=False)
        self.assertFalse(value['events']);self.assertEqual(value['state']['status'],'CALL_BREAKOUT_DATA_WAIT')
        value=self.step(at+timedelta(minutes=3),spot=802,call=820)
        self.assertEqual(value['state']['status'],'CALL_BREAKOUT_EXPIRED');self.assertFalse(value['events'])

    def test_histogram_can_decrease_while_macd_stays_above_signal(self):
        self.arm();at=self.start+timedelta(minutes=3)
        etf,oi,quote=self.data(at,spot=802,call=820)
        etf['chart'][-2]['histogram']=3
        etf['chart'][-1].update(histogram=2,macd=5,signal=3)
        value=self.book.step(etf,oi,quote,now=at)
        self.assertEqual(value['events'][0]['reason'],'CALL_BREAKOUT_ENTRY')

    def test_below_vwap_rejects(self):
        self.arm();value=self.confirm(above=False)
        self.assertFalse(value['events']);self.assertEqual(value['state']['status'],'CALL_BREAKOUT_REJECTED')

    def test_broken_support_sells_entire_position_with_cooldown(self):
        self.arm();entered=self.confirm()
        value=self.step(self.start+timedelta(minutes=4),spot=798,call=820)
        self.assertEqual(value['quantity'],0)
        self.assertEqual(value['events'][-1]['reason'],'CALL_BREAKOUT_STOP')
        self.assertEqual(value['events'][-1]['quantity'],entered['quantity'])
        self.assertIsNotNone(value['state']['cooldown_until'])

    def test_disabled_by_default_and_invalid_option_rejected(self):
        self.assertFalse(strategy_options()['call_breakout_entry'])
        with self.assertRaises(TradingError):strategy_options(dict(call_breakout_entry=1))


    def test_quote_before_confirmation_close_cannot_be_used_as_fill(self):
        self.arm();at=self.start+timedelta(minutes=3)
        etf,oi,quote=self.data(at,spot=802,call=820)
        quote['observed_at']=at.replace(second=0).isoformat()
        # Exactly bar end is allowed; one second before is not.
        from datetime import datetime
        quote['observed_at']=(datetime.fromisoformat(etf['chart'][-1]['end'])-timedelta(seconds=1)).isoformat()
        value=self.book.step(etf,oi,quote,now=at)
        self.assertFalse(value['events']);self.assertEqual(value['state']['status'],'CALL_BREAKOUT_DATA_WAIT')

    def test_negative_macd_rejects_unless_macd_option_is_disabled(self):
        self.arm();at=self.start+timedelta(minutes=3)
        etf,oi,quote=self.data(at,spot=802,call=820)
        etf['chart'][-1].update(macd=-2,signal=-1,histogram=-1)
        rejected=self.book.step(etf,oi,quote,now=at)
        self.assertEqual(rejected['state']['status'],'CALL_BREAKOUT_REJECTED')
        self.assertFalse(rejected['events'])
        self.book.reset(10000000);self.book.options['macd_filter']=False
        self.arm()
        accepted=self.book.step(etf,oi,quote,now=at)
        self.assertEqual(accepted['events'][0]['reason'],'CALL_BREAKOUT_ENTRY')

    def test_price_gap_above_first_target_is_not_chased(self):
        self.arm()
        value=self.step(self.start+timedelta(minutes=3),spot=813,call=830)
        self.assertFalse(value['events']);self.assertEqual(value['state']['status'],'CALL_BREAKOUT_REJECTED')

    def test_future_confirmation_candle_does_not_allow_early_buy(self):
        self.arm();at=self.start+timedelta(minutes=2)
        etf,oi,quote=self.data(at,spot=802,call=820)
        etf['chart'][-1]['end']=(self.start+timedelta(minutes=3)).replace(second=0).isoformat()
        value=self.book.step(etf,oi,quote,now=at)
        self.assertFalse(value['events']);self.assertEqual(value['state']['status'],'CALL_BREAKOUT_WAIT')


    def test_delayed_post_close_index_can_confirm_before_next_bar(self):
        self.arm();at=self.start+timedelta(minutes=4,seconds=30)
        value=self.step(at,spot=802,call=820)
        self.assertEqual(value['events'][0]['reason'],'CALL_BREAKOUT_ENTRY')
        self.assertEqual(value['events'][0]['confirmation']['call_breakout_entry']['bar_end'],'2026-11-16T10:33:00+09:00')
