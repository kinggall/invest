"""Demo-only minute history and explicitly timestamped market-wide KOSPI flow."""
from datetime import datetime, timedelta
import json
from trader import KST, TradingError, integer

MINUTES = '/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice'
DAILY = '/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice'
FLOW = '/uapi/domestic-stock/v1/quotations/inquire-investor-time-by-market'


class MarketData:
    def __init__(self, engine):
        self.engine = engine
        self.db, self.client = engine.db, engine.client
        self.db.execute('CREATE TABLE IF NOT EXISTS minutes (symbol TEXT, stamp TEXT, value TEXT, PRIMARY KEY(symbol,stamp))')
        self.db.execute('CREATE TABLE IF NOT EXISTS flows (symbol TEXT PRIMARY KEY, stamp TEXT, value TEXT)')

    def minutes(self, symbol, now):
        day = now.strftime('%Y%m%d')
        cursor = min(now, now.replace(hour=15, minute=30, second=0, microsecond=0))
        cached = self.db.execute('SELECT max(stamp) FROM minutes WHERE symbol=?', (symbol,)).fetchone()[0]
        # Initial bootstrapping is bounded to today's session; later polls refresh the last window.
        maximum = 14 if not cached or (now-datetime.fromisoformat(cached)).total_seconds() > 120 else 1
        seen = set()
        for _ in range(maximum):
            if cursor.hour < 9:
                break
            data = self.client.request(MINUTES, 'FHKST03010200', dict(FID_COND_MRKT_DIV_CODE='J',
                FID_INPUT_ISCD=symbol, FID_INPUT_HOUR_1=cursor.strftime('%H%M%S'),
                FID_PW_DATA_INCU_YN='Y', FID_ETC_CLS_CODE=''))
            rows = data.get('output2')
            if not isinstance(rows, list):
                raise TradingError('MINUTE_RESPONSE_INVALID')
            oldest = None
            for item in rows:
                if item.get('stck_bsop_date') != day:
                    continue
                stamp = datetime.strptime(day + item['stck_cntg_hour'], '%Y%m%d%H%M%S').replace(tzinfo=KST)
                if stamp > now or stamp in seen:
                    continue
                seen.add(stamp)
                oldest = min(oldest,stamp) if oldest else stamp
                row = dict(time=stamp.isoformat(), open=integer(item.get('stck_oprc'),'OPEN'),
                    high=integer(item.get('stck_hgpr'),'HIGH'), low=integer(item.get('stck_lwpr'),'LOW'),
                    close=integer(item.get('stck_prpr'),'CLOSE'), volume=integer(item.get('cntg_vol'),'VOLUME',0))
                if row['high'] < max(row['open'],row['close'],row['low']) or row['low'] > min(row['open'],row['close']):
                    raise TradingError('INVALID_OHLC')
                self.db.execute('INSERT OR REPLACE INTO minutes VALUES (?,?,?)', (symbol,stamp.isoformat(),json.dumps(row)))
            if oldest is None:
                break
            cursor = oldest - timedelta(minutes=1)
        return [json.loads(row[0]) for row in self.db.execute('SELECT value FROM (SELECT stamp,value FROM minutes WHERE symbol=? ORDER BY stamp DESC LIMIT 3000) ORDER BY stamp', (symbol,))]

    def daily(self, symbol, cutoff, now=None):
        now=now or datetime.now(KST)
        try:
            end=datetime.strptime(cutoff,'%Y-%m-%d').date()
        except (ValueError,TypeError):
            raise TradingError('기준일을 YYYY-MM-DD 형식으로 선택하세요.') from None
        if end>now.date():raise TradingError('미래 날짜는 조회할 수 없습니다.')
        if end==now.date() and (now.hour,now.minute)<(15,30):
            end-=timedelta(days=1)  # Complete daily candles only.
        start=end-timedelta(days=365)
        data=self.client.request(DAILY,'FHKST03010100',dict(FID_COND_MRKT_DIV_CODE='J',
            FID_INPUT_ISCD=symbol,FID_INPUT_DATE_1=start.strftime('%Y%m%d'),
            FID_INPUT_DATE_2=end.strftime('%Y%m%d'),FID_PERIOD_DIV_CODE='D',FID_ORG_ADJ_PRC='0'))
        if data.get('output1',{}).get('stck_shrn_iscd')!=symbol:
            raise TradingError('DAILY_SYMBOL_MISMATCH')
        raw=data.get('output2')
        if not isinstance(raw,list):raise TradingError('DAILY_RESPONSE_INVALID')
        bars={}
        for item in raw:
            stamp=item.get('stck_bsop_date','')
            if not stamp:continue
            try:day=datetime.strptime(stamp,'%Y%m%d').date()
            except (ValueError,TypeError):raise TradingError('DAILY_DATE_INVALID') from None
            if not start<=day<=end:continue
            bar=dict(time=day.isoformat(),end=day.isoformat(),
                open=integer(item.get('stck_oprc'),'OPEN'),high=integer(item.get('stck_hgpr'),'HIGH'),
                low=integer(item.get('stck_lwpr'),'LOW'),close=integer(item.get('stck_clpr'),'CLOSE'),
                volume=integer(item.get('acml_vol'),'VOLUME',0))
            if bar['high']<max(bar['open'],bar['close'],bar['low']) or bar['low']>min(bar['open'],bar['close']):
                raise TradingError('INVALID_DAILY_OHLC')
            if day in bars and bars[day]!=bar:raise TradingError('DAILY_DUPLICATE_CONFLICT')
            bars[day]=bar
        if not bars:raise TradingError('해당 기준일까지 일봉 자료가 없습니다.')
        return [bars[day] for day in sorted(bars)][-100:]

    def flow(self, symbol, now):
        # Market-wide KOSPI flow, not the ETF's own investor history.
        key = 'KOSPI'
        cached = self.db.execute('SELECT stamp,value FROM flows WHERE symbol=?',(key,)).fetchone()
        previous = json.loads(cached['value']) if cached else None
        if cached and 0 <= (now-datetime.fromisoformat(cached['stamp'])).total_seconds() < 60:
            return previous
        value = dict(kind='kospi_intraday', market='KOSPI', foreign=None, institution=None,
            foreign_change=None, institution_change=None, observed_at=now.isoformat(),
            source_time=None, unit='API 거래대금 원단위 (단위 미검증)',
            description='코스피 전체 누적 순매수 · 60초 조회. 조회시각은 원자료 갱신시각과 다릅니다.')
        try:
            data = self.client.request(FLOW, 'FHPTJ04030000', dict(FID_INPUT_ISCD='999',FID_INPUT_ISCD_2='S001'))
            rows = data.get('output')
            if isinstance(rows,dict): rows=[rows]
            if not isinstance(rows,list) or len(rows) != 1:
                raise TradingError('KOSPI_FLOW_RESPONSE_INVALID')
            row=rows[0]
            def signed(key):
                raw=row.get(key)
                if not isinstance(raw,(str,int)) or isinstance(raw,bool):raise TradingError('KOSPI_FLOW_VALUE_INVALID')
                return int(raw)
            value.update(foreign=signed('frgn_ntby_tr_pbmn'),institution=signed('orgn_ntby_tr_pbmn'))
            # Do not invent a source timestamp when the endpoint has none.
            if (previous and not previous.get('error') and previous['observed_at'][:10] == now.date().isoformat()):
                value['foreign_change']=value['foreign']-previous['foreign']
                value['institution_change']=value['institution']-previous['institution']
                value['comparison_at']=previous['observed_at']
        except TradingError as exc:
            value['error']=str(exc)
        self.db.execute('INSERT OR REPLACE INTO flows VALUES (?,?,?)',(key,now.isoformat(),json.dumps(value)))
        return value
