"""Long-only, cash-funded MACD backtest. Signals at close, fills at NEXT open."""
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from trader import TradingError, integer
from signals import vwap_entry


def rate(value, name, maximum=1000):
    if isinstance(value,bool):raise TradingError('INVALID_'+name)
    try:result=Decimal(str(value))
    except Exception:raise TradingError('INVALID_'+name) from None
    if not result.is_finite() or not 0<=result<=maximum:raise TradingError('INVALID_'+name)
    return result


def simulate(history, initial=10000000, fee_bps=1.5, slippage_bps=5, tax_bps=0, allocation=100, force_close=False, max_buys=10, vwap_filter=False):
    initial=integer(initial,'CAPITAL');max_buys=integer(max_buys,'MAX_BUYS')
    fee=rate(fee_bps,'FEE');slip=rate(slippage_bps,'SLIPPAGE');tax=rate(tax_bps,'TAX')
    fraction=rate(allocation,'ALLOCATION',100)
    if fraction<=0:raise TradingError('INVALID_ALLOCATION')
    if not isinstance(vwap_filter,bool):raise TradingError('INVALID_VWAP_FILTER')
    if not isinstance(force_close,bool):raise TradingError('INVALID_FORCE_CLOSE')
    if history.get('missing_bars'):
        raise TradingError('분봉 누락으로 하루 손익을 계산하지 않습니다: '+', '.join(history['missing_bars'][:8]))
    bars=history.get('chart',[])
    if len(bars)<2:raise TradingError('백테스트용 확정 3분봉이 부족합니다.')
    if any(b['time'][:10]!=history['cutoff'] for b in bars):raise TradingError('BACKTEST_DAY_MISMATCH')
    if not any(b.get('signal') is not None for b in bars):raise TradingError('MACD 지표 준비용 과거 데이터가 부족합니다.')
    cash=initial;shares=0;basis=0;paid_fees=0;paid_tax=0;buys=0;slippage_cost=0
    filtered=unavailable=0
    orders=[];trades=[];equity=[];peak=initial;max_drawdown=0.0;entry=None
    def cost(amount,bps):return int((Decimal(amount)*bps/10000).to_integral_value(rounding=ROUND_CEILING))
    def fill_price(raw,side):
        value=Decimal(integer(raw,'PRICE'))*(1+(slip if side=='buy' else -slip)/10000)/5
        return int(value.to_integral_value(rounding=ROUND_CEILING if side=='buy' else ROUND_FLOOR))*5
    previous=None
    for bar in bars:
        stamp=datetime.fromisoformat(bar['time'])
        opening=integer(bar['open'],'OPEN');closing=integer(bar['close'],'CLOSE')
        side=None;reason='MACD'
        # An optional predeclared time exit is evaluated at the open, without looking at the close.
        if force_close and (stamp.hour,stamp.minute)>=(15,15) and shares:
            side='sell';reason='15:15 시간청산'
        elif previous and previous.get('end')==bar['time'] and (9,0)<=(stamp.hour,stamp.minute)<(15,20):
            side=previous.get('cross')
        if force_close and (stamp.hour,stamp.minute)>=(15,15) and side=='buy':side=None
        if side=='buy' and shares==0 and buys<max_buys and vwap_filter and not vwap_entry(previous):
            if previous.get('vwap') is None:unavailable+=1
            else:filtered+=1
            side=None
        if side=='buy' and shares==0 and buys<max_buys:
            price=fill_price(opening,'buy')
            if price<=0:raise TradingError('INVALID_FILL_PRICE')
            budget=int(Decimal(cash)*fraction/100)
            quantity=int(Decimal(budget)/(Decimal(price)*(1+fee/10000)))
            while quantity and quantity*price+cost(quantity*price,fee)>budget:quantity-=1
            if quantity:
                gross=quantity*price;commission=cost(gross,fee)
                slippage_cost+=quantity*(price-opening)
                cash-=gross+commission;shares=quantity;basis=gross+commission;paid_fees+=commission;buys+=1
                entry=dict(time=bar['time'],price=price,quantity=quantity,signal_time=previous['end'])
                orders.append(dict(side='buy',time=bar['time'],price=price,quantity=quantity,fee=commission,reason=reason,signal_time=previous['end']))
        elif side=='sell' and shares:
            price=fill_price(opening,'sell')
            if price<=0:raise TradingError('INVALID_FILL_PRICE')
            gross=shares*price;commission=cost(gross,fee);levy=cost(gross,tax)
            slippage_cost+=shares*(opening-price)
            proceeds=gross-commission-levy;cash+=proceeds;paid_fees+=commission;paid_tax+=levy
            trades.append(dict(entry=entry['time'],exit=bar['time'],quantity=shares,entry_price=entry['price'],exit_price=price,pnl=proceeds-basis,reason=reason))
            orders.append(dict(side='sell',time=bar['time'],price=price,quantity=shares,fee=commission,tax=levy,reason=reason,signal_time=previous['end'] if reason=='MACD' else None))
            shares=0;basis=0;entry=None
        value=cash+shares*closing;peak=max(peak,value);max_drawdown=max(max_drawdown,(peak-value)/peak*100)
        equity.append(dict(time=bar['end'],value=value))
        previous=bar
    final=equity[-1]['value'];realized=sum(t['pnl'] for t in trades)
    return dict(initial_capital=initial,final_equity=final,net_pnl=final-initial,return_pct=(final/initial-1)*100,
        realized_pnl=realized,unrealized_pnl=shares*bars[-1]['close']-basis if shares else 0,
        ending_cash=cash,ending_shares=shares,fees=paid_fees,tax=paid_tax,slippage_cost=slippage_cost,valuation_at=bars[-1]['end'],max_drawdown_pct=max_drawdown,
        buy_count=buys,sell_count=len(trades),round_trips=len(trades),vwap_filtered_signals=filtered,vwap_unavailable_signals=unavailable,
        win_rate_pct=sum(t['pnl']>0 for t in trades)/len(trades)*100 if trades else None,
        orders=orders,trades=trades,equity=equity,date=history['cutoff'],warmup_date=history.get('warmup_date'),
        assumptions=dict(vwap_filter=vwap_filter,vwap_rule='매수 신호 확정봉 종가 > 같은 봉 종료시점 당일 VWAP. 매도 조건은 유지.',fee_bps=float(fee),slippage_bps=float(slip),tax_bps=float(tax),allocation_pct=float(fraction),
            force_close=force_close,max_buys=max_buys,execution='확정봉 신호 → 다음 3분봉 시가 ± 슬리피지, 5원 호가단위',
            sizing='가용 현금의 투자비중만큼 정수 주식 매수. 실시간 주문의 1주/금액상한 설정과 별도.',
            mark='미청산 보유분은 마지막 봉 종가로 평가. 미실현 손익에 향후 매도비용은 미반영.',
            liquidity='전량 즉시 체결 가정. 호가·거래량·부분체결·실시간 지정가 미체결은 재현하지 않음.',
            flow='코스피 수급은 매매 필터에 사용하지 않으며 과거 수급을 합성하지 않음.'))
