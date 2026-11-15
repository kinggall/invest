"""Closed KRX 3-minute bars and SMA-seeded MACD; no execution side effects."""
from datetime import datetime, timedelta
import math
from trader import KST, TradingError


def aggregate(minutes, now):
    buckets = {}
    for row in minutes:
        stamp = datetime.fromisoformat(row['time']).astimezone(KST)
        if stamp.second or stamp.hour < 9 or stamp.hour > 15 or (stamp.hour == 15 and stamp.minute >= 30):
            continue
        start = stamp.replace(minute=stamp.minute - stamp.minute % 3, second=0, microsecond=0)
        if start + timedelta(minutes=3) > now:
            continue
        buckets.setdefault(start, {})[stamp] = row
    result = []
    for start, items in sorted(buckets.items()):
        stamps = [start + timedelta(minutes=i) for i in range(3)]
        if any(stamp not in items for stamp in stamps):
            continue  # Never invent missing OHLCV or evaluate an unfinished bar.
        rows = [items[stamp] for stamp in stamps]
        result.append(dict(time=start.isoformat(), end=(start+timedelta(minutes=3)).isoformat(),
            open=rows[0]['open'], high=max(r['high'] for r in rows), low=min(r['low'] for r in rows),
            close=rows[-1]['close'], volume=sum(r['volume'] for r in rows)))
    return result


def ema(values, period):
    if period < 1:
        raise TradingError('INVALID_EMA_PERIOD')
    result = [None] * len(values)
    if len(values) < period:
        return result
    value = sum(values[:period]) / period
    result[period-1] = value
    alpha = 2 / (period+1)
    for index in range(period, len(values)):
        value += alpha * (values[index] - value)
        result[index] = value
    return result


def macd(bars, fast=12, slow=26, signal=9):
    if not (1 <= fast < slow <= 100 and 1 <= signal <= 100):
        raise TradingError('INVALID_MACD_PERIODS')
    closes = [float(row['close']) for row in bars]
    if any(not math.isfinite(v) or v <= 0 for v in closes):
        raise TradingError('INVALID_CLOSE')
    fast_line, slow_line = ema(closes, fast), ema(closes, slow)
    line = [None if s is None else f-s for f,s in zip(fast_line,slow_line)]
    signal_values = ema(line[slow-1:], signal)
    signal_line = [None]*(slow-1) + signal_values if len(line) >= slow else [None]*len(line)
    output = []
    for i, bar in enumerate(bars):
        m, s = line[i], signal_line[i]
        cross = None
        if i and s is not None and signal_line[i-1] is not None:
            previous = line[i-1] - signal_line[i-1]
            current = m-s
            if previous <= 0 and current > 0: cross = 'buy'
            elif previous >= 0 and current < 0: cross = 'sell'
        output.append(dict(bar, macd=m, signal=s, histogram=None if s is None else m-s, cross=cross))
    return output


def session_vwap(minutes, bars):
    """Causal day-reset VWAP from minute HLC3*volume; gaps invalidate the day's reference."""
    unique = {}
    for row in minutes:
        stamp = datetime.fromisoformat(row['time']).astimezone(KST)
        if stamp.second or not ((9,0) <= (stamp.hour,stamp.minute) < (15,30)):
            continue
        if stamp in unique and any(unique[stamp][k] != row[k] for k in ('high','low','close','volume')):
            raise TradingError('VWAP_MINUTE_CONFLICT')
        unique[stamp] = row
    cumulative = {}
    day = None
    total_value = total_volume = 0.0
    complete = True
    expected = None
    for stamp, row in sorted(unique.items()):
        if stamp.date() != day:
            missing = 0
            missing_times = []
            day = stamp.date()
            total_value = total_volume = 0.0
            complete = True
            expected = stamp.replace(hour=9,minute=0,second=0,microsecond=0)
        if stamp != expected:
            complete = False
            count=max(0,int((stamp-expected).total_seconds()//60))
            missing+=count
            for index in range(min(count,5-len(missing_times))):
                missing_times.append((expected+timedelta(minutes=index)).strftime('%H:%M'))
        expected = stamp + timedelta(minutes=1)
        try:
            high,low,close,volume = (float(row[k]) for k in ('high','low','close','volume'))
        except (TypeError,ValueError):
            raise TradingError('VWAP_INPUT_INVALID') from None
        if any(not math.isfinite(v) for v in (high,low,close,volume)) or min(high,low,close) <= 0 or volume < 0:
            raise TradingError('VWAP_INPUT_INVALID')
        total_value += ((high+low+close)/3)*volume
        total_volume += volume
        observed=total_value/total_volume if total_volume>0 else None
        cumulative[expected] = dict(value=observed if complete else None,observed=observed,
            quality='no_volume' if observed is None else ('complete' if complete else 'partial'),
            missing=missing,missing_times=list(missing_times),volume=total_volume)
    result = []
    previous = None
    previous_day = None
    for bar in bars:
        end = datetime.fromisoformat(bar['end']).astimezone(KST)
        state=cumulative.get(end,{})
        value = state.get('value')
        if end.date() != previous_day:
            previous = None
        change = value-previous if value is not None and previous is not None else None
        result.append(dict(bar,vwap=value,vwap_change=change,
            vwap_observed=state.get('observed'),vwap_quality=state.get('quality','missing'),
            vwap_missing_minutes=state.get('missing',0),vwap_missing_times=state.get('missing_times',[]),
            vwap_cumulative_volume=state.get('volume',0),
            vwap_distance_pct=(bar['close']/value-1)*100 if value else None,
            vwap_source='당일 1분봉 HLC3×거래량 근사 · 09:00 초기화'))
        previous,previous_day = value,end.date()
    return result


def vwap_entry(bar):
    value = bar.get('vwap')
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) and value>0 and bar['close']>value
