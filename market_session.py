"""Explicit KRX equity special sessions. All times are Korea local time."""
from datetime import datetime,timedelta,timezone

KST=timezone(timedelta(hours=9))
SPECIAL={'2026-11-19':(60,'수능일 1시간 순연')}


def shift(day):
    key=day.date().isoformat() if isinstance(day,datetime) else day.isoformat() if hasattr(day,'isoformat') else str(day)
    return SPECIAL.get(key,(0,''))[0]


def market_time(day,hour,minute=0):
    total=hour*60+minute+shift(day)
    return total//60,total%60


def market_stamp(day,hour,minute=0):
    date=day.date() if isinstance(day,datetime) else day
    h,m=market_time(date,hour,minute)
    return datetime.combine(date,datetime.min.time(),tzinfo=KST).replace(hour=h,minute=m)


def order_window(now):
    return now.weekday()<5 and market_time(now,9,0)<=(now.hour,now.minute)<market_time(now,15,20)


def session_info(now):
    def label(h,m):return '%02d:%02d'%market_time(now,h,m)
    return dict(date=now.date().isoformat(),reason=SPECIAL.get(now.date().isoformat(),(0,'정규 거래일'))[1],
                open=label(9,0),liquidate=label(15,15),deadline=label(15,19),order_cutoff=label(15,20),close=label(15,30))
