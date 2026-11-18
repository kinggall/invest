from market_session import market_time, market_stamp
"""Automatically archive actual ETF minutes before API retention expires. GET only."""
import argparse
from contextlib import contextmanager
from datetime import datetime,timedelta
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from history import History
from instruments import TRADE_SYMBOL
from trader import KST, ReadOnlyHistoryClient, TradingError


def expected_minutes(date,now):
    start=market_stamp(date,9)
    end=min(market_stamp(date,15,19),now.replace(second=0,microsecond=0)-timedelta(minutes=1))
    return [(start+timedelta(minutes=i)).isoformat() for i in range(max(0,int((end-start).total_seconds()/60)+1))]


@contextmanager
def archive_lock(database):
    path=Path(database).with_suffix('.archive.lock');path.parent.mkdir(parents=True,exist_ok=True)
    with open(path,'a+b') as stream:
        if path.stat().st_size==0:stream.write(b'0');stream.flush()
        stream.seek(0)
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:yield
        finally:
            stream.seek(0)
            if os.name=='nt':msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(stream.fileno(),fcntl.LOCK_UN)


def prepare(db):
    db.execute('CREATE TABLE IF NOT EXISTS minutes (symbol TEXT,stamp TEXT,value TEXT,PRIMARY KEY(symbol,stamp))')
    db.execute('CREATE TABLE IF NOT EXISTS price_archive_runs (day TEXT PRIMARY KEY,checked_at TEXT,payload TEXT)')


def latest(database):
    if not Path(database).is_file():return []
    db=sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True)
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='price_archive_runs'").fetchone():return []
        return [json.loads(r[0]) for r in db.execute('SELECT payload FROM price_archive_runs ORDER BY day DESC LIMIT 3')]
    finally:db.close()


def archive(database,home,now=None,client=None,force=False):
    now=now or datetime.now(KST);database=Path(database)
    with archive_lock(database):
        db=sqlite3.connect(database,timeout=10,isolation_level=None)
        try:
            prepare(db);reader=None
            dates=[];date=now.date()
            for _ in range(7):
                if date.weekday()<5 and (date<now.date() or (now.hour,now.minute)>=market_time(now,9,2)):dates.append(date)
                if len(dates)>=3:break
                date-=timedelta(days=1)
            for date in dates:
                wanted=expected_minutes(date,now)
                stamps={r[0] for r in db.execute('SELECT stamp FROM minutes WHERE symbol=? AND substr(stamp,1,10)=?',(TRADE_SYMBOL,date.isoformat()))}
                missing=sorted(set(wanted)-stamps)
                closed=date<now.date() or (now.hour,now.minute)>=market_time(now,15,31)
                previous=db.execute('SELECT checked_at,payload FROM price_archive_runs WHERE day=?',(date.isoformat(),)).fetchone()
                # Current data catch-up every 15min, old failed days hourly.
                # A partial 15:15 run must not defer the first post-close sweep.
                delay=timedelta(minutes=15 if date==now.date() else 60)
                previous_at=datetime.fromisoformat(previous[0]) if previous else None
                postclose=bool(previous_at and closed and date==now.date() and (previous_at.hour,previous_at.minute)<market_time(now,15,31))
                if previous and not force and not postclose and now-datetime.fromisoformat(previous[0])<delay:continue
                record=dict(day=date.isoformat(),checked_at=now.isoformat(),expected=len(wanted),status='COMPLETE' if closed and not missing else 'COLLECTING',source='KIS real ETF minute GET',error=None)
                finalized=bool(previous and json.loads(previous[1]).get('finalized'))
                fetched=False
                if missing or closed and not finalized:
                    try:
                        if reader is None:reader=History(SimpleNamespace(db=db),client=client or ReadOnlyHistoryClient(home))
                        reader.day(date,refresh=True,now=now)
                        fetched=reader.last_api_success
                        record['warnings']=list(reader.warnings)
                        if not fetched:record['error']=reader.warnings[-1] if reader.warnings else 'PRICE_ARCHIVE_PARTIAL_FETCH'
                    except Exception as exc:
                        # Only a controlled error type/code reaches the status page.
                        record.update(status='ERROR',error=str(exc) if isinstance(exc,TradingError) else 'PRICE_ARCHIVE_FETCH_FAILED')
                stamps={r[0] for r in db.execute('SELECT stamp FROM minutes WHERE symbol=? AND substr(stamp,1,10)=?',(TRADE_SYMBOL,date.isoformat()))}
                missing=sorted(set(wanted)-stamps)
                record.update(saved=len(set(wanted)&stamps),missing=len(missing),first_missing=missing[:8],finalized=bool(closed and not missing and (fetched or finalized)))
                if not record['error']:record['status']='COMPLETE' if closed and not missing else 'PARTIAL' if closed else 'COLLECTING'
                db.execute('INSERT OR REPLACE INTO price_archive_runs VALUES (?,?,?)',(date.isoformat(),now.isoformat(),json.dumps(record,ensure_ascii=False)))
        finally:db.close()
    return latest(database)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--domino-home',type=Path,required=True)
    parser.add_argument('--state',type=Path,default=Path(__file__).parent/'runtime'/'orders.sqlite3')
    args=parser.parse_args()
    try:
        result=archive(args.state,args.domino_home)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        # Old unavailable days remain visible but do not trigger a retry storm.
        today=datetime.now(KST).date().isoformat()
        return 1 if any(r['day']==today and r['status'] in ('ERROR','PARTIAL') for r in result) else 0
    except (OSError,TradingError):print('자동 분봉 저장 대기 · 실행 중복 또는 조회 연결을 확인하세요.');return 1


if __name__=='__main__':raise SystemExit(main())
