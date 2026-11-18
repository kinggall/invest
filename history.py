"""Historical minute retrieval using existing real tokens; never trading or issuing tokens."""
from datetime import datetime, timedelta
import json
from trader import KST, TradingError, ReadOnlyHistoryClient, HISTORY_PATH, INTRADAY_HISTORY_PATH, integer, is_rate_limit
from signals import aggregate, macd, session_vwap
from macd_strategy import SYMBOL


class History:
    def __init__(self, engine, client=None, progress=None):
        self.progress=progress or (lambda message:None)
        self.db=engine.db
        self.warnings=[]
        self.client=client or ReadOnlyHistoryClient(engine.client.tokens.home)
        self.db.execute('CREATE TABLE IF NOT EXISTS history_days (symbol TEXT,day TEXT,source TEXT,observed TEXT,PRIMARY KEY(symbol,day))')

    def cached(self,date):
        return [json.loads(r[0]) for r in self.db.execute('SELECT value FROM minutes WHERE symbol=? AND stamp>=? AND stamp<? ORDER BY stamp',
            (SYMBOL,date.isoformat(),(date+timedelta(days=1)).isoformat()))]

    def save_minutes(self,rows):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            for row in rows:
                self.db.execute('INSERT OR REPLACE INTO minutes VALUES (?,?,?)',(SYMBOL,row['time'],json.dumps(row)))
            self.db.execute('COMMIT')
        except Exception:self.db.execute('ROLLBACK');raise

    def day(self,date,refresh=False,now=None):
        self.last_api_success=False
        try:return self.download_day(date,refresh,now)
        except TradingError as exc:
            cached=self.cached(date)
            if not cached:raise
            self.warnings.append(date.isoformat()+' 분봉 추가 조회 실패 · 받은 자료와 기존 저장 '+str(len(cached))+'분으로 계속: '+str(exc))
            return cached

    def download_day(self, date, refresh=False, now=None):
        now=now or datetime.now(KST)
        row=self.db.execute('SELECT source FROM history_days WHERE symbol=? AND day=?',(SYMBOL,date.isoformat())).fetchone()
        if row and not refresh:
            self.progress(date.isoformat()+' · 저장된 과거 분봉 읽는 중…')
            cached=[json.loads(r[0]) for r in self.db.execute('SELECT value FROM minutes WHERE symbol=? AND stamp>=? AND stamp<? ORDER BY stamp',
                (SYMBOL,date.isoformat(),(date+timedelta(days=1)).isoformat()))]
            start=datetime.combine(date,datetime.min.time(),tzinfo=KST).replace(hour=9)
            required={(start+timedelta(minutes=i)).isoformat() for i in range(380)}
            if required<={r['time'] for r in cached}:return cached
            self.progress('저장 분봉 누락 발견 · 한투에서 다시 조회하는 중…')
        cursor=datetime.combine(date,datetime.min.time(),tzinfo=KST).replace(hour=15,minute=30)
        if date==now.date() and (now.hour,now.minute)<(15,31):
            cursor=min(cursor,now.replace(second=0,microsecond=0)-timedelta(minutes=1))
        rows={};intraday=False
        for page in range(24):
            self.progress(date.isoformat()+' · '+('당일' if intraday else '과거')+' 1분봉 '+str(page+1)+'페이지 조회 중…')
            params=dict(FID_COND_MRKT_DIV_CODE='J',FID_INPUT_ISCD=SYMBOL,FID_INPUT_HOUR_1=cursor.strftime('%H%M%S'))
            try:
                if intraday:
                    data=self.client.request(INTRADAY_HISTORY_PATH,'FHKST03010200',dict(params,FID_PW_DATA_INCU_YN='Y',FID_ETC_CLS_CODE=''))
                else:
                    data=self.client.request(HISTORY_PATH,'FHKST03010230',dict(params,FID_INPUT_DATE_1=date.strftime('%Y%m%d'),FID_PW_DATA_INCU_YN='N',FID_FAKE_TICK_INCU_YN=''))
            except TradingError as exc:
                if str(exc).startswith('SHARED_') or is_rate_limit(exc):raise
                if 'OPSQ0008' in str(exc) and not intraday and date==now.date():
                    intraday=True
                    self.warnings.append(date.isoformat()+' 일별 분봉 요청 거절 · 당일 분봉 API로 보완')
                    continue
                if 'OPSQ0008' in str(exc) and rows and cursor.hour==9 and cursor.minute<=1:
                    self.warnings.append(date.isoformat()+' 장 시작 경계 조회 거절 · 이미 받은 분봉은 보존')
                    break
                raise TradingError(date.isoformat()+' '+cursor.strftime('%H:%M')+' '+('당일' if intraday else '일별')+' 분봉 조회 실패 · '+str(exc)) from None
            code=data.get('output1',{}).get('stck_shrn_iscd')
            if code and code!=SYMBOL:raise TradingError('HISTORY_SYMBOL_MISMATCH')
            batch=data.get('output2')
            if not isinstance(batch,list):raise TradingError('HISTORY_RESPONSE_INVALID')
            oldest=None
            for item in batch:
                item_date=item.get('stck_bsop_date') or (date.strftime('%Y%m%d') if intraday else '')
                if item_date!=date.strftime('%Y%m%d'):continue
                try:stamp=datetime.strptime(item_date+item['stck_cntg_hour'],'%Y%m%d%H%M%S').replace(tzinfo=KST)
                except (ValueError,TypeError,KeyError):raise TradingError('HISTORY_TIME_INVALID') from None
                if not (9<=stamp.hour<16) or stamp>cursor:continue
                if stamp.second:raise TradingError('HISTORY_NOT_MINUTE_DATA')
                oldest=min(oldest,stamp) if oldest else stamp
                row=dict(time=stamp.isoformat(),open=integer(item.get('stck_oprc'),'OPEN'),
                    high=integer(item.get('stck_hgpr'),'HIGH'),low=integer(item.get('stck_lwpr'),'LOW'),
                    close=integer(item.get('stck_prpr'),'CLOSE'),volume=integer(item.get('cntg_vol'),'VOLUME',0))
                if row['high']<max(row['open'],row['close'],row['low']) or row['low']>min(row['open'],row['close']):
                    raise TradingError('HISTORY_INVALID_OHLC')
                if stamp in rows and rows[stamp]!=row:raise TradingError('HISTORY_CONFLICT')
                rows[stamp]=row
            self.save_minutes(rows.values())
            if oldest is None or oldest.hour==9 and oldest.minute==0:
                break
            following=oldest-timedelta(minutes=1)
            if following>=cursor:raise TradingError('HISTORY_PAGINATION_STALLED')
            cursor=following
            if cursor.hour<9:break
        else:
            raise TradingError('HISTORY_PAGINATION_LIMIT')
        self.last_api_success=True
        # Pages are durable even when a later page fails. Mark a complete day only
        # when this retrieval finished and all expected regular-session minutes exist.
        merged=self.cached(date)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            for stamp,row in rows.items():
                self.db.execute('INSERT OR REPLACE INTO minutes VALUES (?,?,?)',(SYMBOL,stamp.isoformat(),json.dumps(row)))
            complete_day=date<now.date() or date==now.date() and (now.hour,now.minute)>=(15,31)
            start=datetime.combine(date,datetime.min.time(),tzinfo=KST).replace(hour=9)
            complete_minutes={(start+timedelta(minutes=i)) for i in range(380)}
            if merged and complete_day and {d.isoformat() for d in complete_minutes}<={r['time'] for r in merged}:
                self.db.execute('INSERT OR REPLACE INTO history_days VALUES (?,?,?,?)',
                    (SYMBOL,date.isoformat(),'KIS real historical GET',datetime.now(KST).isoformat()))
            self.db.execute('COMMIT')
        except Exception:
            self.db.execute('ROLLBACK');raise
        return merged

    def load(self, cutoff, config, now=None):
        now=now or datetime.now(KST)
        try:date=datetime.strptime(cutoff,'%Y-%m-%d').date()
        except (ValueError,TypeError):raise TradingError('조회일을 YYYY-MM-DD 형식으로 선택하세요.') from None
        if date>now.date() or date==now.date() and (now.hour,now.minute)<(15,30):
            raise TradingError('장 종료 후 확정된 거래일만 조회하세요.')
        if date.weekday()>=5:raise TradingError('주말이 아닌 거래일을 선택하세요.')
        self.progress(cutoff+' · 과거 분봉 조회 준비 중…')
        raw=self.day(date,now=now)
        if not raw:raise TradingError('해당 날짜의 과거 분봉이 없습니다. 서버 보관 범위와 실전 조회 권한을 확인하세요.')
        self.progress('MACD 준비용 이전 거래일 자료 확인 중…')
        warmup=[]
        # A prior trading session supplies EMA warm-up, never contributes P&L.
        for offset in range(1,8):
            previous=date-timedelta(days=offset)
            if previous.weekday()>=5:continue
            try:prior=self.day(previous,now=now)
            except TradingError as exc:
                self.warnings.append('지표 준비용 '+previous.isoformat()+' 조회 실패 · 당일 자료로 계속: '+str(exc))
                prior=[json.loads(r[0]) for r in self.db.execute('SELECT value FROM minutes WHERE symbol=? AND substr(stamp,1,10)=? ORDER BY stamp',(SYMBOL,previous.isoformat()))]
                if prior:warmup=prior
                break
            if prior:
                warmup=prior;break
        self.progress('확정 3분봉 합성 및 MACD 계산 중…')
        bars=session_vwap(warmup+raw,aggregate(warmup+raw,datetime.combine(date+timedelta(days=1),datetime.min.time(),tzinfo=KST)))
        # Reset calculation at missing intraday bars, matching the live strategy.
        chart=[];segment=[]
        for bar in bars:
            if segment:
                last=datetime.fromisoformat(segment[-1]['time']);current=datetime.fromisoformat(bar['time'])
                if last.date()==current.date() and current-last!=timedelta(minutes=3):
                    chart.extend(macd(segment,config['fast'],config['slow'],config['signal']));segment=[]
            segment.append(bar)
        chart.extend(macd(segment,config['fast'],config['slow'],config['signal']))
        selected=[bar for bar in chart if bar['time'][:10]==cutoff]
        if not selected:raise TradingError('완성된 3분봉이 없습니다. 누락된 분봉은 합성하지 않습니다.')
        expected=datetime.combine(date,datetime.min.time(),tzinfo=KST).replace(hour=9)
        present={bar['time'] for bar in selected}
        # Continuous full bars through 15:15; auction-transition bars are optional.
        missing=[]
        for i in range(126):
            stamp=expected+timedelta(minutes=3*i)
            if stamp.isoformat() not in present:missing.append(stamp.strftime('%H:%M'))
        return dict(cutoff=cutoff,last_date=selected[-1]['time'],symbol=SYMBOL,chart=selected,
            timeframe='3m',source='한투 과거/당일 분봉 · 기존 실전 토큰 읽기 전용',warnings=list(self.warnings),
            warmup_date=warmup[0]['time'][:10] if warmup else None,missing_bars=missing,
            periods=[config['fast'],config['slow'],config['signal']],observed_at=now.isoformat())
