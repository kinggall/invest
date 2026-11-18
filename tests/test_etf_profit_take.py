from datetime import timedelta
import hashlib
import unittest
import test_wall_paper as fixtures
from wall_paper import WallPaper,average,strategy_options
from trader import TradingError


class ProfitTakeTests(unittest.TestCase):
    data=fixtures.PaperTests.data
    def setUp(self):
        fixtures.PaperTests.setUp(self)
        self.book.options=strategy_options(dict(etf_profit_take=True))
    def step(self,now,**kwargs):return self.book.step(*self.data(now,**kwargs),now=now)
    def enter(self,above=True):
        self.step(self.start,above=above)
        return self.step(self.start+timedelta(minutes=3),above=above)

    def test_one_percent_sells_half_once_and_protects_remaining_at_average(self):
        entered=self.enter();q=entered['quantity'];basis=entered['average_entry']
        at=self.start+timedelta(minutes=4)
        etf,oi,quote=self.data(at,price=basis*1.01)
        taken=self.book.step(etf,{},quote,now=at) # No OI is necessary for the ETF trigger.
        self.assertEqual(taken['events'][-1]['reason'],'ETF_PROFIT_1PCT')
        self.assertEqual(taken['events'][-1]['quantity'],q//2)
        self.assertEqual(taken['quantity'],q-q//2)
        self.assertTrue(taken['state']['position']['tp1'])
        self.assertEqual(taken['state']['position']['stop_price'],basis)
        self.step(at+timedelta(seconds=10),price=basis*1.02)
        self.assertEqual(sum(e['reason']=='ETF_PROFIT_1PCT' for e in self.book.view()['events']),1)
        closed=self.step(at+timedelta(minutes=1),price=basis)
        self.assertEqual(closed['events'][-1]['reason'],'PROFIT_PROTECT');self.assertEqual(closed['quantity'],0)

    def test_below_target_does_not_take_profit(self):
        entered=self.enter()
        value=self.step(self.start+timedelta(minutes=4),price=entered['average_entry']*1.0099)
        self.assertEqual(len(value['events']),1)
        self.assertFalse(value['state']['position']['protected'])

    def test_prior_index_scaleout_is_included_in_half_target(self):
        entered=self.enter();q=entered['quantity'];basis=entered['average_entry']
        first=self.step(self.start+timedelta(minutes=4),spot=790,price=basis)
        previous=first['events'][-1]['quantity']
        self.assertEqual(first['events'][-1]['reason'],'TAKE_PROFIT_1')
        taken=self.step(self.start+timedelta(minutes=5),price=basis*1.01)
        self.assertEqual(taken['events'][-1]['quantity'],q//2-previous)
        self.assertEqual(taken['quantity'],q-q//2)

    def test_single_share_is_sold_in_full(self):
        self.book.reset(100000)
        entered=self.enter(above=False)
        self.assertEqual(entered['quantity'],1)
        value=self.step(self.start+timedelta(minutes=4),price=entered['average_entry']*1.01)
        self.assertEqual(value['quantity'],0);self.assertEqual(value['events'][-1]['quantity'],1)

    def test_live_defaults_and_option_validation(self):
        self.assertFalse(strategy_options()['etf_profit_take'])
        with self.assertRaises(TradingError):strategy_options(dict(etf_profit_take='true'))


    def test_additional_lot_keeps_remaining_average_protection(self):
        entered=self.enter();at=self.start+timedelta(minutes=4)
        self.step(at,price=entered['average_entry']*1.01)
        state=self.book.load();p=state['position']
        self.book.event(state,p,'BREAKOUT_ADD','buy',10,10300,801,at+timedelta(minutes=3),{})
        self.assertGreaterEqual(p['stop_price'],average(p))
