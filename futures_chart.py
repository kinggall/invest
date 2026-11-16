"""Read-only day/night KOSPI200 futures OHLCV. No synthetic ticks or orders."""
from datetime import datetime,timedelta
import json
import re
import sqlite3
from pathlib import Path
from trader import KST,TradingError,ReadOnlyDerivativesClient,DERIVATIVE_READS,FUTURES_MINUTES
from oi import load_future_master,nearest_month,number


def session_at(now):
    now=now.astimezone(KST);windows=[]
    for offset in range(8):
        day=(now-timedelta(days=offset)).replace(hour=0,minute=0,second=0,microsecond=0)
        if day.weekday()>=5:continue
        for mode,market,start,end in [('day','F',day.replace(hour=8,minute=45),day.replace(hour=15,minute=45)),
                                     ('night','CM',day.replace(hour=18),day+timedelta(days=1,hours=6))]:
            if start<=now:windows.append(dict(id=mode+':'+day.date().isoformat(),mode=mode,market=market,start=start.isoformat(),end=end.isoformat(),scheduled_open=now<end))
    return max(windows,key=lambda r:r['start'])


def stamp_for(day,hour,context):
    if not isinstance(day,str) or not re.fullmatch(r'\d{8}',day) or not isinstance(hour,str) or not re.fullmatch(r'\d{6}',hour):
        raise TradingError('FUTURES_MINUTE_TIME_INVALID')
    try:
        base=datetime.strptime(day,'%Y%m%d').replace(tzinfo=KST)
        h,m,s=int(hour[:2]),int(hour[2:4]),int(hour[4:])
        if h>29 or m>59 or s>59 or (context['mode']=='day' and h>=24):raise ValueError()
        stamp=base+timedelta(hours=h,minutes=m,seconds=s)
    except ValueError:raise TradingError('FUTURES_MINUTE_TIME_INVALID') from None
    # CM extended hours 24..29 are converted to the following calendar date.
    start=datetime.fromisoformat(context['start']);end=datetime.fromisoformat(context['end'])
    return stamp if start<=stamp<end and stamp.second==0 else None


def closed_bars(minutes,context,now):
    anchor=datetime.fromisoformat(context['start']);end=datetime.fromisoformat(context['end']);groups={}
    for row in minutes:
        stamp=datetime.fromisoformat(row['time'])
        if not anchor<=stamp<end:continue
        bucket=anchor+timedelta(minutes=int((stamp-anchor).total_seconds()//180)*3)
        groups.setdefault(bucket,{})[stamp]=row
    result=[]
    for start,items in sorted(groups.items()):
        stamps=[start+timedelta(minutes=i) for i in range(3)]
        if start+timedelta(minutes=3)>min(now,end) or any(s not in items for s in stamps):continue
        rows=[items[s] for s in stamps]
        result.append(dict(time=start.isoformat(),end=(start+timedelta(minutes=3)).isoformat(),open=rows[0]['open'],high=max(r['high'] for r in rows),
            low=min(r['low'] for r in rows),close=rows[-1]['close'],volume=sum(r['volume'] for r in rows)))
    return result


class FuturesChart:
    def __init__(self,home,database,client=None,progress=None):
        self.client=client or ReadOnlyDerivativesClient(home)
        self.progress=progress or (lambda message:None)
        self.db=sqlite3.connect(Path(database),timeout=10,isolation_level=None);self.db.row_factory=sqlite3.Row
        self.db.execute('CREATE TABLE IF NOT EXISTS futures_minutes (code TEXT,session TEXT,stamp TEXT,payload TEXT,PRIMARY KEY(code,session,stamp))')

    def close(self):self.db.close()

    def collect(self,now=None):
        now=now or datetime.now(KST);context=session_at(now)
        start=datetime.fromisoformat(context['start']);end=datetime.fromisoformat(context['end'])
        contracts=[r for r in load_future_master(now.date().isoformat()) if r['expiry']>=nearest_month(start)]
        if not contracts:raise TradingError('FUTURES_MASTER_MISSING')
        contract=contracts[0];code=contract['code']
        cached=self.db.execute('SELECT max(stamp) FROM futures_minutes WHERE code=? AND session=?',(code,context['id'])).fetchone()[0]
        cursor=min(now,end-timedelta(minutes=1)).replace(second=0,microsecond=0)
        received=0;warnings=[]
        for page in range(3):
            self.progress(('야간 CM' if context['mode']=='night' else '주간 F')+' '+code+' · 실제 1분봉 '+str(page+1)+'/3페이지')
            if context['mode']=='night':
                elapsed=(cursor.date()-start.date()).days
                hour=f'{cursor.hour+24*elapsed:02d}{cursor.minute:02d}00'
                query_day=start.strftime('%Y%m%d')
            else:hour=cursor.strftime('%H%M%S');query_day=cursor.strftime('%Y%m%d')
            data=self.client.request(FUTURES_MINUTES,DERIVATIVE_READS[FUTURES_MINUTES],dict(FID_COND_MRKT_DIV_CODE=context['market'],FID_INPUT_ISCD=code,
                FID_HOUR_CLS_CODE='60',FID_PW_DATA_INCU_YN='Y',FID_FAKE_TICK_INCU_YN='N',FID_INPUT_DATE_1=query_day,FID_INPUT_HOUR_1=hour))
            header=data.get('output1') or {}
            response_code=str(header.get('futs_shrn_iscd') or '').strip() if isinstance(header,dict) else ''
            if response_code and response_code not in (code,code+'000'):raise TradingError('FUTURES_SYMBOL_MISMATCH')
            rows=data.get('output2')
            if not isinstance(rows,list):raise TradingError('FUTURES_MINUTES_RESPONSE_INVALID')
            parsed={}
            for raw in rows:
                if not isinstance(raw,dict):raise TradingError('FUTURES_MINUTE_ROW_INVALID')
                stamp=stamp_for(raw.get('stck_bsop_date'),raw.get('stck_cntg_hour'),context)
                if stamp is None or stamp>cursor:continue
                row=dict(time=stamp.isoformat(),open=number(raw.get('futs_oprc'),'FUTURES_OPEN'),high=number(raw.get('futs_hgpr'),'FUTURES_HIGH'),
                    low=number(raw.get('futs_lwpr'),'FUTURES_LOW'),close=number(raw.get('futs_prpr'),'FUTURES_CLOSE'),volume=number(raw.get('cntg_vol'),'FUTURES_VOLUME',True))
                if min(row[k] for k in ('open','high','low','close'))<=0 or row['high']<max(row['open'],row['close'],row['low']) or row['low']>min(row['open'],row['close']):
                    raise TradingError('FUTURES_MINUTE_OHLC_INVALID')
                if stamp in parsed and parsed[stamp]!=row:raise TradingError('FUTURES_MINUTE_CONFLICT')
                parsed[stamp]=row
            if not parsed:break
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                self.db.executemany('INSERT OR REPLACE INTO futures_minutes VALUES (?,?,?,?)',[(code,context['id'],stamp.isoformat(),json.dumps(row)) for stamp,row in parsed.items()])
            received+=len(parsed);oldest=min(parsed)
            if oldest<=start or cached and oldest.isoformat()<=cached:break
            cursor=oldest-timedelta(minutes=1)
            if cursor<start:break
        minutes=[json.loads(r[0]) for r in self.db.execute('SELECT payload FROM futures_minutes WHERE code=? AND session=? AND stamp<=? ORDER BY stamp',(code,context['id'],now.isoformat()))]
        bars=closed_bars(minutes,context,now)
        if not bars:
            kind='야간 CM' if context['mode']=='night' else '주간 F'
            raise TradingError(kind+' 세션의 실제 확정 3분봉을 받지 못했습니다. 조회 권한/휴장/시각 형식을 확인하세요. 주간 값을 야간으로 대체하지 않습니다. [FUTURES_SESSION_EMPTY]')
        last_end=datetime.fromisoformat(bars[-1]['end']);age=(now-last_end).total_seconds()
        if context['scheduled_open'] and age>240:warnings.append('마지막 확정봉이 4분 이상 지연됐습니다. 실시간 수신 상태로 해석하지 마세요.')
        if not context['scheduled_open']:warnings.append('표준 거래시간 종료 구간입니다. 직전 세션 기록을 표시합니다.')
        if minutes and datetime.fromisoformat(minutes[0]['time'])>start:warnings.append('세션 전체가 아닌 수신된 최근 구간입니다. 미수신 봉은 생성하지 않습니다.')
        warnings.append('표준 거래시간 기준 자동 전환입니다. 휴장·임시 시간 변경은 실제 수신 시각을 확인하세요.')
        return dict(session=context,code=code,name=contract['name'],observed_at=now.isoformat(),last_bar_end=bars[-1]['end'],age_seconds=age,
            stale=context['scheduled_open'] and age>240,bars=bars[-140:],received_minutes=received,warnings=warnings,source='한투 실제 1분봉 → 확정 3분봉 · '+context['market'])
