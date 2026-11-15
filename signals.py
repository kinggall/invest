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
