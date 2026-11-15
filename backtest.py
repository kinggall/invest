"""Long-only, cash-funded MACD backtest. Signals at close, fills at NEXT open."""
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from trader import TradingError, integer
from signals import vwap_entry
from risk import stop_percent


STRATEGIES = {
    'baseline': ('MACD 단독',dict(vwap_filter=False)),
    'vwap': ('MACD + VWAP 매수',dict(vwap_filter=True)),
    'vwap_hold': ('추세 유지 · VWAP 청산',dict(vwap_filter=True,exit_policy='vwap')),
    'swing_hold': ('추세 유지 · 눌림 저점 청산',dict(vwap_filter=True,exit_policy='swing')),
    'buy_hold': ('장 시작 단순보유',dict(benchmark=True)),
}


def rate(value, name, maximum=1000):
    if isinstance(value,bool):raise TradingError('INVALID_'+name)
    try:result=Decimal(str(value))
    except Exception:raise TradingError('INVALID_'+name) from None
    if not result.is_finite() or not 0<=result<=maximum:raise TradingError('INVALID_'+name)
    return result


def simulate(history, initial=10000000, fee_bps=1.5, slippage_bps=5, tax_bps=0, allocation=100, force_close=False, max_buys=10, vwap_filter=False, exit_policy='macd', benchmark=False, stop_loss_enabled=False, stop_loss_pct=1, stop_halt=True):
    stop_pct=stop_percent(stop_loss_pct)
    if not isinstance(stop_loss_enabled,bool) or not isinstance(stop_halt,bool):raise TradingError('INVALID_STOP_LOSS_SWITCH')
    initial=integer(initial,'CAPITAL');max_buys=integer(max_buys,'MAX_BUYS')
    fee=rate(fee_bps,'FEE');slip=rate(slippage_bps,'SLIPPAGE');tax=rate(tax_bps,'TAX')
    fraction=rate(allocation,'ALLOCATION',100)
    if fraction<=0:raise TradingError('INVALID_ALLOCATION')
    if exit_policy not in ('macd','vwap','swing'):raise TradingError('INVALID_EXIT_POLICY')
    if not isinstance(benchmark,bool):raise TradingError('INVALID_BENCHMARK')
    if benchmark and (vwap_filter or exit_policy!='macd'):raise TradingError('INVALID_BENCHMARK_RULES')
    if exit_policy!='macd':vwap_filter=True
    if not isinstance(vwap_filter,bool):raise TradingError('INVALID_VWAP_FILTER')
    if not isinstance(force_close,bool):raise TradingError('INVALID_FORCE_CLOSE')
    if history.get('missing_bars'):
        raise TradingError('분봉 누락으로 하루 손익을 계산하지 않습니다: '+', '.join(history['missing_bars'][:8]))
    bars=history.get('chart',[])
    if len(bars)<2:raise TradingError('백테스트용 확정 3분봉이 부족합니다.')
    if any(b['time'][:10]!=history['cutoff'] for b in bars):raise TradingError('BACKTEST_DAY_MISMATCH')
    if not benchmark and not any(b.get('signal') is not None for b in bars):raise TradingError('MACD 지표 준비용 과거 데이터가 부족합니다.')
    cash=initial;shares=0;basis=0;paid_fees=0;paid_tax=0;buys=0;slippage_cost=0
    filtered=unavailable=held_exits=exit_unavailable=stop_count=0
    halted=False
    last_swing=swing_stop=None
    orders=[];trades=[];equity=[];peak=initial;max_drawdown=0.0;entry=None
    def cost(amount,bps):return int((Decimal(amount)*bps/10000).to_integral_value(rounding=ROUND_CEILING))
    def fill_price(raw,side):
        value=Decimal(str(raw))*(1+(slip if side=='buy' else -slip)/10000)/5
        return int(value.to_integral_value(rounding=ROUND_CEILING if side=='buy' else ROUND_FLOOR))*5
    def liquidate(reference,bar,reason,trigger_level=None,signal_time=None,precision='open'):
        nonlocal cash,shares,basis,paid_fees,paid_tax,slippage_cost,entry,swing_stop
        price=fill_price(reference,'sell')
        if price<=0:raise TradingError('INVALID_FILL_PRICE')
        gross=shares*price;commission=cost(gross,fee);levy=cost(gross,tax)
        slip_cost=shares*(float(reference)-price);slippage_cost+=slip_cost
        proceeds=gross-commission-levy;cash+=proceeds;paid_fees+=commission;paid_tax+=levy
        trades.append(dict(entry=entry['time'],exit=bar['time'],quantity=shares,entry_price=entry['price'],exit_price=price,
            pnl=proceeds-basis,reason=reason,trigger_level=trigger_level,exit_precision=precision))
        orders.append(dict(side='sell',time=bar['time'],price=price,reference_open=float(reference),reference_kind='stop_level' if precision=='within_bar' else 'bar_open',
            quantity=shares,fee=commission,tax=levy,slippage_tick_cost=slip_cost,reason=reason,signal_time=signal_time,
            trigger_level=trigger_level,time_precision=precision))
        shares=0;basis=0;entry=None;swing_stop=None

    previous=None
    for index,bar in enumerate(bars):
        stamp=datetime.fromisoformat(bar['time'])
        opening=integer(bar['open'],'OPEN');closing=integer(bar['close'],'CLOSE')
        stopped_this_bar=False
        if stop_loss_enabled and shares:
            level=Decimal(entry['price'])*(1-Decimal(str(stop_pct))/100)
            if Decimal(opening)<=level:
                liquidate(opening,bar,str(stop_pct)+'% 로스컷 · 갭/시가',float(level))
                stop_count+=1;stopped_this_bar=True;halted=stop_halt
        side=None;reason='MACD';trigger_level=None
        if exit_policy=='swing' and index>=3:
            # A pivot low is known only after the right-hand bar has CLOSED.
            left,middle,right=bars[index-3:index]
            if middle['low']<left['low'] and middle['low']<=right['low']:
                last_swing=middle['low']
                if shares:swing_stop=max(swing_stop if swing_stop is not None else last_swing,last_swing)

        # An optional predeclared time exit is evaluated at the open, without looking at the close.
        if force_close and (stamp.hour,stamp.minute)>=(15,15) and shares:
            side='sell';reason='15:15 시간청산'
        elif previous and previous.get('end')==bar['time'] and (9,0)<=(stamp.hour,stamp.minute)<(15,20):
            side=previous.get('cross')
            if benchmark:
                side=None
            elif shares and exit_policy!='macd':
                reference=previous.get('vwap')
                valid=isinstance(reference,(int,float)) and not isinstance(reference,bool) and reference>0
                rising=previous.get('vwap_change') is not None and previous['vwap_change']>0
                supported=valid and previous['close']>reference and rising
                if not valid:exit_unavailable+=1
                broken=(valid and previous['close']<reference) if exit_policy=='vwap' else (swing_stop is not None and previous['close']<swing_stop)
                if broken:
                    side='sell'
                    reason='VWAP 종가 이탈' if exit_policy=='vwap' else '확인된 눌림 저점 이탈'
                    trigger_level=reference if exit_policy=='vwap' else swing_stop
                elif side=='sell' and supported:
                    side=None;held_exits+=1
                elif side=='sell':
                    reason='MACD 하향 · VWAP 상승 유지 조건 미충족'
        if benchmark and index==0:
            side='buy';reason='장 시작 단순보유'

        if side=='buy' and (stopped_this_bar or halted or (force_close and (stamp.hour,stamp.minute)>=(15,15))):side=None
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
                signal_time=previous['end'] if previous else None
                if exit_policy=='swing':
                    swing_stop=last_swing if last_swing is not None and last_swing<previous['close'] else previous['low']
                entry=dict(time=bar['time'],price=price,quantity=quantity,signal_time=signal_time)
                orders.append(dict(side='buy',time=bar['time'],price=price,reference_open=opening,quantity=quantity,
                    fee=commission,slippage_tick_cost=quantity*(price-opening),reason=reason,signal_time=signal_time))
        elif side=='sell' and shares:
            liquidate(opening,bar,reason,trigger_level,previous['end'] if previous and reason!='15:15 시간청산' else None)
        if stop_loss_enabled and shares:
            # A standing protective condition can trigger AFTER an opening buy, in the same bar.
            level=Decimal(entry['price'])*(1-Decimal(str(stop_pct))/100)
            if Decimal(str(bar['low']))<=level:
                reference=min(Decimal(opening),level)
                liquidate(reference,bar,str(stop_pct)+'% 로스컷 · 봉중',float(level),precision='within_bar')
                stop_count+=1;halted=stop_halt
        value=cash+shares*closing;peak=max(peak,value);max_drawdown=max(max_drawdown,(peak-value)/peak*100)
        equity.append(dict(time=bar['end'],value=value))
        previous=bar
    final=equity[-1]['value'];realized=sum(t['pnl'] for t in trades)
    strategy='buy_hold' if benchmark else ('vwap_hold' if exit_policy=='vwap' else 'swing_hold' if exit_policy=='swing' else 'vwap' if vwap_filter else 'baseline')
    return dict(strategy=strategy,stop_count=stop_count,trading_halted=halted,reference_price_pnl=final-initial+paid_fees+paid_tax+slippage_cost,total_cost=paid_fees+paid_tax+slippage_cost,initial_capital=initial,final_equity=final,net_pnl=final-initial,return_pct=(final/initial-1)*100,
        realized_pnl=realized,unrealized_pnl=shares*bars[-1]['close']-basis if shares else 0,
        ending_cash=cash,ending_shares=shares,fees=paid_fees,tax=paid_tax,slippage_cost=slippage_cost,valuation_at=bars[-1]['end'],max_drawdown_pct=max_drawdown,
        buy_count=buys,sell_count=len(trades),round_trips=len(trades),vwap_filtered_signals=filtered,vwap_unavailable_signals=unavailable,vwap_exit_unavailable_bars=exit_unavailable,macd_exits_held=held_exits,
        win_rate_pct=sum(t['pnl']>0 for t in trades)/len(trades)*100 if trades else None,
        orders=orders,trades=trades,equity=equity,date=history['cutoff'],warmup_date=history.get('warmup_date'),
        assumptions=dict(stop_loss_enabled=stop_loss_enabled,stop_loss_pct=stop_pct,stop_halt=stop_halt,stop_basis='실제 매수 체결가 대비 가격 손익. 수수료·매도비용 제외.',stop_execution='기존 보유의 시가 갭은 시가, 봉중 저가 이탈은 손절 기준가에 슬리피지·5원 호가단위 반영. OHLC에서 알 수 없는 봉중 갭·정확한 초 단위 시각은 재현하지 못함.',exit_policy=exit_policy,benchmark=benchmark,cost_decomposition='가격 차익은 동일 매매시각·수량 고정 역산입니다. 비용 0 재실행 결과가 아닙니다.',trend_exit=('상승 VWAP 위에서는 MACD 하향을 보류. '+('종가의 VWAP 이탈' if exit_policy=='vwap' else '종가의 확인된 눌림 저점 이탈')+'이면 다음 봉 시가에 청산. 상승 유지 조건 미충족이면 MACD 하향 청산.'),swing_confirmation='이전 저점보다 낮고 다음 봉 저점 이하인 저점을 다음 봉 마감 후 확인. 보유 중 기준은 낮추지 않음.',vwap_filter=vwap_filter,vwap_rule='매수 신호 확정봉 종가 > 같은 봉 종료시점 당일 VWAP. 매도 조건은 유지.',fee_bps=float(fee),slippage_bps=float(slip),tax_bps=float(tax),allocation_pct=float(fraction),
            force_close=force_close,max_buys=max_buys,execution='확정봉 신호 → 다음 3분봉 시가 ± 슬리피지, 5원 호가단위',
            sizing='가용 현금의 투자비중만큼 정수 주식 매수. 실시간 주문의 1주/금액상한 설정과 별도.',
            mark='미청산 보유분은 마지막 봉 종가로 평가. 미실현 손익에 향후 매도비용은 미반영.',
            liquidity='전량 즉시 체결 가정. 호가·거래량·부분체결·실시간 지정가 미체결은 재현하지 않음.',
            flow='코스피 수급은 매매 필터에 사용하지 않으며 과거 수급을 합성하지 않음.'))
