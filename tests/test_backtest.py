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

    def test_vwap_filter_uses_signal_bar_not_future_entry_bar(self):
        data=sample()
        for row in data['chart']:row['vwap']=1
        data['chart'][0]['vwap']=110
        blocked=simulate(data,1000,0,0,vwap_filter=True)
        self.assertEqual(blocked['buy_count'],0)
        self.assertEqual(blocked['vwap_filtered_signals'],1)
        self.assertEqual(simulate(data,1000,0,0,vwap_filter=False)['net_pnl'],500)
        data['chart'][0]['vwap']=90
        data['chart'][1]['vwap']=200
        allowed=simulate(data,1000,0,0,vwap_filter=True)
        self.assertEqual(allowed['net_pnl'],500)

    def test_vwap_missing_blocks_entries_but_does_not_block_exit(self):
        data=sample()
        self.assertEqual(simulate(data,1000,0,0,vwap_filter=True)['vwap_unavailable_signals'],1)
        for row in data['chart']:row['vwap']=200
        data['chart'][0]['vwap']=90
        result=simulate(data,1000,0,0,vwap_filter=True)
        self.assertEqual(result['sell_count'],1)
        self.assertEqual(result['net_pnl'],500)

class TrendExitTests(unittest.TestCase):
    def history(self,prices,lows=None,crosses=None,refs=None):
        start=datetime(2026,11,11,9,tzinfo=KST)
        rows=[]
        for index,price in enumerate(prices):
            stamp=start+timedelta(minutes=3*index)
            rows.append(dict(time=stamp.isoformat(),end=(stamp+timedelta(minutes=3)).isoformat(),
                open=price,high=price+1,low=lows[index] if lows else price-1,close=price,volume=100,
                macd=1,signal=0,cross=(crosses or {}).get(index),
                vwap=refs[index] if refs else 90+index,vwap_change=1 if index else None))
        return dict(cutoff='2026-11-11',chart=rows,missing_bars=[],warmup_date='2026-11-10')

    def test_vwap_trend_holds_macd_exit_and_sells_after_closed_break(self):
        data=self.history([100,110,120,115,130,105,110],crosses={0:'buy',2:'sell'},refs=[90,95,100,101,102,110,109])
        old=simulate(data,1000,0,0,vwap_filter=True)
        trend=simulate(data,1000,0,0,exit_policy='vwap')
        self.assertEqual(old['trades'][0]['exit'],data['chart'][3]['time'])
        self.assertEqual(trend['trades'][0]['exit'],data['chart'][6]['time'])
        self.assertEqual(trend['trades'][0]['reason'],'VWAP 종가 이탈')
        self.assertEqual(trend['macd_exits_held'],1)
        self.assertEqual(trend['trades'][0]['trigger_level'],110)

    def test_non_rising_vwap_does_not_protect_macd_exit(self):
        data=self.history([100,110,120,115,130],crosses={0:'buy',2:'sell'})
        data['chart'][2]['vwap_change']=0
        run=simulate(data,1000,0,0,exit_policy='vwap')
        self.assertEqual(run['trades'][0]['exit'],data['chart'][3]['time'])
        self.assertIn('조건 미충족',run['trades'][0]['reason'])

    def test_confirmed_swing_stop_is_trailing_and_not_lowered(self):
        data=self.history([105,110,115,112,120,104,110,102,101],
            lows=[100,101,105,103,106,101,102,100,99],crosses={0:'buy',2:'sell'})
        run=simulate(data,1000,0,0,exit_policy='swing')
        self.assertEqual(run['trades'][0]['exit'],data['chart'][8]['time'])
        self.assertEqual(run['trades'][0]['trigger_level'],103)
        self.assertEqual(run['trades'][0]['reason'],'확인된 눌림 저점 이탈')
        self.assertEqual(run['macd_exits_held'],1)
        prefix=dict(data,chart=data['chart'][:5])
        earlier=simulate(prefix,1000,0,0,exit_policy='swing')
        self.assertEqual(earlier['orders'],run['orders'][:1])

    def test_missing_vwap_cannot_silently_protect_exit(self):
        data=self.history([100,110,120,115],crosses={0:'buy',2:'sell'})
        data['chart'][2]['vwap']=None
        run=simulate(data,1000,0,0,exit_policy='vwap')
        self.assertEqual(run['sell_count'],1)
        self.assertGreater(run['vwap_exit_unavailable_bars'],0)

    def test_buy_hold_enters_at_first_open_and_obeys_same_forced_exit(self):
        data=self.history([100,110,120,150])
        for bar in data['chart']:bar['signal']=None
        data['chart'][-1].update(time='2026-11-11T15:15:00+09:00',end='2026-11-11T15:18:00+09:00')
        run=simulate(data,1000,0,0,benchmark=True,force_close=True)
        self.assertEqual(run['buy_count'],1)
        self.assertEqual(run['sell_count'],1)
        self.assertEqual(run['orders'][0]['time'],data['chart'][0]['time'])
        self.assertEqual(run['orders'][0]['signal_time'],None)
        self.assertEqual(run['trades'][0]['reason'],'15:15 시간청산')
        self.assertEqual(run['net_pnl'],500)

    def test_screenshot_trade_cost_decomposition_is_reconciled(self):
        rows=[(14715,14800),(14895,14910),(14970,14960),(15055,15030),(15110,15065),(15110,15070),(15120,15045)]
        prices=[];crosses={}
        for buy,sell in rows:
            index=len(prices);prices.extend([buy-10,buy-10,sell+10,sell+10])
            crosses[index]='buy';crosses[index+2]='sell'
        result=simulate(self.history(prices,crosses=crosses))
        self.assertEqual(result['net_pnl'],-82298)
        self.assertEqual(result['fees'],21028)
        self.assertEqual(result['slippage_cost'],93500)
        self.assertEqual(result['reference_price_pnl'],32230)
        self.assertEqual(result['total_cost'],114528)
        self.assertEqual(result['reference_price_pnl']-result['total_cost'],result['net_pnl'])
