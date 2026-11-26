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


def latest(database,dates=None):
    if not Path(database).is_file():return []
    db=sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True)
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='price_archive_runs'").fetchone():return []
        if dates is not None:
            days=[d.isoformat() for d in dates]
            if not days:return []
            return [json.loads(r[0]) for r in db.execute('SELECT payload FROM price_archive_runs WHERE day IN ('+','.join('?' for _ in days)+') ORDER BY day DESC',days)]
        return [json.loads(r[0]) for r in db.execute('SELECT payload FROM price_archive_runs ORDER BY day DESC LIMIT 3')]
    finally:db.close()


def recovery_dates(start,end,now=None):
    now=now or datetime.now(KST)
    try:first=datetime.strptime(start,'%Y-%m-%d').date();last=datetime.strptime(end,'%Y-%m-%d').date()
    except (ValueError,TypeError):raise TradingError('복구 날짜는 YYYY-MM-DD 형식이어야 합니다.') from None
    if first>last or last>now.date() or (last-first).days>=31:raise TradingError('복구 범위는 미래 날짜 없이 최대 31일이며 시작일은 종료일 이전이어야 합니다.')
    dates=[last-timedelta(days=i) for i in range((last-first).days+1) if (last-timedelta(days=i)).weekday()<5]
    if not dates:raise TradingError('선택 범위에 평일이 없습니다.')
    return dates


def archive(database,home,now=None,client=None,force=False,dates=None,progress=None):
    now=now or datetime.now(KST);database=Path(database)
    requested=dates;progress=progress or (lambda message:None)
    with archive_lock(database):
        db=sqlite3.connect(database,timeout=10,isolation_level=None)
        try:
            prepare(db);reader=None
            if dates is None:
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
                progress(date.isoformat()+' · 실제 ETF 분봉 조회/누락 확인 중…')
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
                if record['error']:record['status']='PARTIAL' if record['saved'] else 'ERROR'
                else:record['status']='COMPLETE' if closed and not missing else 'PARTIAL' if closed else 'COLLECTING'
                db.execute('INSERT OR REPLACE INTO price_archive_runs VALUES (?,?,?)',(date.isoformat(),now.isoformat(),json.dumps(record,ensure_ascii=False)))
                progress(date.isoformat()+' · 저장 '+str(record['saved'])+'/'+str(record['expected'])+'분 · 누락 '+str(record['missing'])+'분 · '+record['status'])
        finally:db.close()
    return latest(database,requested)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--domino-home',type=Path,required=True)
    parser.add_argument('--state',type=Path,default=Path(__file__).parent/'runtime'/'orders.sqlite3')
    parser.add_argument('--from-date',help='복구 시작일 YYYY-MM-DD')
    parser.add_argument('--to-date',help='복구 종료일 YYYY-MM-DD')
    parser.add_argument('--force',action='store_true',help='오류 후 재시도 대기시간 무시')
    args=parser.parse_args()
    if bool(args.from_date)!=bool(args.to_date):parser.error('--from-date와 --to-date를 함께 지정하세요.')
    try:
        dates=recovery_dates(args.from_date,args.to_date) if args.from_date else None
        result=archive(args.state,args.domino_home,dates=dates,force=bool(dates) or args.force,progress=print)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        # Old unavailable days remain visible but do not trigger a retry storm.
        today=datetime.now(KST).date().isoformat()
        if dates:
            report=args.state.parent/('price-recovery-'+args.from_date+'-'+args.to_date+'.json')
            report.parent.mkdir(parents=True,exist_ok=True)
            temp=report.with_suffix('.tmp')
            temp.write_text(json.dumps(dict(results=result,note='실제 ETF 분봉 복구 결과 · 과거 OI/수급 기록은 생성하지 않음'),ensure_ascii=False,indent=2),encoding='utf-8')
            temp.chmod(0o600);os.replace(temp,report)
            print('복구 결과 파일: '+str(report))
        return 1 if any((dates is not None or r['day']==today) and r['status'] in ('ERROR','PARTIAL') for r in result) else 0
    except TradingError as exc:print('분봉 복구 실패 · '+str(exc));return 1
    except OSError:print('자동 분봉 저장 대기 · 실행 중복 또는 조회 연결을 확인하세요.');return 1


if __name__=='__main__':raise SystemExit(main())
