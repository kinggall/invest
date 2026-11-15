from datetime import datetime,timedelta
import unittest
from backtest import simulate
from trader import KST,TradingError


def sample():
    start=datetime(2026,11,13,9,tzinfo=KST)
    rows=[]
    for i in range(5):
        stamp=start+timedelta(minutes=i*3)
        rows.append(dict(time=stamp.isoformat(),end=(stamp+timedelta(minutes=3)).isoformat(),
            open=100 if i<2 else 150,close=100 if i==0 else 150,macd=2,signal=1,
            cross='buy' if i==0 else 'sell' if i==2 else None))
    return dict(cutoff='2026-11-13',chart=rows,missing_bars=[],warmup_date='2026-11-12')

class BacktestTests(unittest.TestCase):
    def test_known_round_trip_and_next_open_not_signal_close(self):
        result=simulate(sample(),1000,0,0)
        self.assertEqual(result['net_pnl'],500)
        self.assertEqual(result['final_equity'],1500)
        self.assertEqual(result['orders'][0]['time'],sample()['chart'][1]['time'])
        self.assertEqual(result['orders'][0]['price'],100)
        self.assertEqual(result['orders'][0]['quantity'],10)
        self.assertEqual(result['orders'][1]['time'],sample()['chart'][3]['time'])
        self.assertEqual(result['round_trips'],1)
        self.assertEqual(result['realized_pnl']+result['unrealized_pnl'],result['net_pnl'])

    def test_fees_slippage_and_whole_share_budget(self):
        free=simulate(sample(),1000,0,0)
        costly=simulate(sample(),1000,10,5,10)
        self.assertLess(costly['net_pnl'],free['net_pnl'])
        self.assertGreater(costly['fees'],0)
        self.assertGreater(costly['tax'],0)
        self.assertGreater(costly['slippage_cost'],0)
        self.assertGreaterEqual(costly['ending_cash'],0)
        self.assertTrue(all(order['price']%5==0 for order in costly['orders']))

    def test_unclosed_position_is_marked_and_not_reported_as_realized(self):
        data=sample();data['chart'][2]['cross']=None
        result=simulate(data,1000,0,0)
        self.assertEqual(result['realized_pnl'],0)
        self.assertEqual(result['unrealized_pnl'],500)
        self.assertEqual(result['ending_shares'],10)
        self.assertEqual(result['round_trips'],0)
        self.assertIsNone(result['win_rate_pct'])

    def test_final_bar_signal_cannot_fill_in_same_bar(self):
        data=sample()
        for row in data['chart']:row['cross']=None
        data['chart'][-1]['cross']='buy'
        self.assertEqual(simulate(data,1000,0,0)['orders'],[])

    def test_incomplete_data_and_invalid_costs_are_rejected(self):
        data=sample();data['missing_bars']=['09:12']
        with self.assertRaises(TradingError):simulate(data)
        for value in (-1,float('nan'),float('inf'),True):
            with self.assertRaises(TradingError):simulate(sample(),fee_bps=value)

    def test_close_equity_drawdown(self):
        data=sample();data['chart'][2]['close']=90
        self.assertAlmostEqual(simulate(data,1000,0,0)['max_drawdown_pct'],40)

    def test_predeclared_exit_uses_open_without_future_close(self):
        data=sample();data['chart'][2]['cross']=None
        last=data['chart'][-1];last['time']='2026-11-13T15:15:00+09:00';last['end']='2026-11-13T15:18:00+09:00';last['close']=9999
        result=simulate(data,1000,0,0,force_close=True)
        self.assertEqual(result['ending_shares'],0)
        self.assertEqual(result['trades'][-1]['exit_price'],150)
        self.assertEqual(result['net_pnl'],500)
