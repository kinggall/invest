"""Observed monthly KOSPI200 option OI concentrations; never execution signals."""
from datetime import datetime,timedelta
import json
import math
from pathlib import Path
import re
import sqlite3
from trader import KST,TradingError,ReadOnlyDerivativesClient,DERIVATIVE_READS,OI_OPTIONS,OI_EXPIRIES,OI_FUTURES,OI_INDEX


def number(raw,label,integer=False,optional=False,signed=False):
    if optional and raw in (None,''):return None
    if isinstance(raw,bool):raise TradingError('OI_INVALID_'+label)
    try:value=float(raw)
    except (ValueError,TypeError):raise TradingError('OI_INVALID_'+label) from None
    if not math.isfinite(value) or (not signed and value<0) or (integer and value!=int(value)):
        raise TradingError('OI_INVALID_'+label)
    return int(value) if integer else value


def settings(value):
    expiry=value.get('expiry','')
    if not isinstance(expiry,str) or expiry and not re.fullmatch(r'20\d{2}(?:0[1-9]|1[0-2])',expiry):
        raise TradingError('만기는 월물 YYYYMM 형식으로 선택하세요.')
    radius=number(value.get('radius_pct',5),'RADIUS')
    if not 1<=radius<=15:raise TradingError('OI 탐색 범위는 지수 대비 1~15%로 설정하세요.')
    return dict(expiry=expiry,radius_pct=radius)



def monthly_expiry(item):
    """Use the display month separately from the provider's encoded maturity code."""
    if not isinstance(item,dict):return None
    code=str(item.get('mtrt_yymm_code') or '').strip()
    label=str(item.get('mtrt_yymm') or '').strip()
    # Never reinterpret a weekly/mini label or a weekly identifier as monthly.
    if re.search(r'주간|위클리|미니|weekly|mini',label,re.I):return None
    pattern=r'20\d{2}(?:0[1-9]|1[0-2])'
    if re.fullmatch(pattern,code):return code
    normalized=re.sub(r'[\s/년월.-]','',label)
    if re.fullmatch(pattern,normalized) and re.fullmatch(r'[A-Z](?:0[1-9]|1[0-2])',code) and code[-2:]==normalized[-2:]:
        return normalized
    return None


def nearest_month(now):
    first=now.date().replace(day=1)
    expiry_day=first+timedelta(days=(3-first.weekday())%7+7)
    if now.date()>expiry_day:first=(first+timedelta(days=32)).replace(day=1)
    return first.strftime('%Y%m')


def chain(rows,side):
    if not isinstance(rows,list):raise TradingError('OI_CHAIN_RESPONSE_INVALID')
    result={};skipped=0
    for row in rows:
        if not isinstance(row,dict):raise TradingError('OI_CHAIN_ROW_INVALID')
        if row.get('acpr') in (None,'') or row.get('hts_otst_stpl_qty') in (None,''):
            skipped+=1;continue
        strike=number(row['acpr'],'STRIKE')
        if strike==0:skipped+=1;continue
        item=dict(side=side,strike=strike,code=str(row.get('optn_shrn_iscd',''))[:32],
            oi=number(row['hts_otst_stpl_qty'],'OPEN_INTEREST',True),
            api_change=number(row.get('otst_stpl_qty_icdc'),'OI_CHANGE',True,True,True),
            volume=number(row.get('acml_vol'),'VOLUME',True,True),observed_change=None)
        if strike in result and item!=result[strike]:raise TradingError('OI_DUPLICATE_STRIKE')
        result[strike]=item
    return sorted(result.values(),key=lambda r:r['strike']),skipped


def candidates(rows,spot,side,radius,basis):
    if spot is None:return []
    selected=[r for r in rows if r['oi']>0 and ((spot*(1-radius/100)<=r['strike']<spot) if side=='put' else (spot<r['strike']<=spot*(1+radius/100)))]
    total=sum(r['oi'] for r in selected)
    ranked=sorted(selected,key=lambda r:(-r['oi'],abs(r['strike']-spot),r['strike']))[:3]
    return [dict(r,local_share_pct=r['oi']/total*100,distance_pct=(r['strike']/spot-1)*100,
                 projected_future_level=r['strike']+basis if basis is not None else None) for r in ranked]


class OIService:
    def __init__(self,home,database,client=None,progress=None):
        self.client=client or ReadOnlyDerivativesClient(home)
        self.progress=progress or (lambda message:None)
        path=Path(database);path.parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(path,timeout=10,isolation_level=None);self.db.row_factory=sqlite3.Row
        self.db.execute('CREATE TABLE IF NOT EXISTS oi_snapshots (id INTEGER PRIMARY KEY,observed_at TEXT,expiry TEXT,payload TEXT)')
        self.db.execute('CREATE INDEX IF NOT EXISTS oi_expiry_time ON oi_snapshots(expiry,observed_at)')
        path.chmod(0o600)

    def close(self):self.db.close()

    def query(self,path,params):return self.client.request(path,DERIVATIVE_READS[path],params)

    def history(self,expiry,now):
        rows=self.db.execute('SELECT payload FROM (SELECT id,payload FROM oi_snapshots WHERE expiry=? AND observed_at>=? AND observed_at<=? ORDER BY id DESC LIMIT 60) ORDER BY id',
            (expiry,now.date().isoformat(),now.isoformat())).fetchall()
        result=[]
        for row in rows:
            value=json.loads(row[0]);future=value.get('future') or {}
            result.append(dict(observed_at=value['observed_at'],expiry=expiry,future_code=future.get('code'),
                future_price=future.get('price'),spot=value.get('spot'),basis=value.get('basis'),
                put_strike=(value.get('put_wall') or {}).get('strike'),call_strike=(value.get('call_wall') or {}).get('strike'),
                put_oi=(value.get('put_wall') or {}).get('oi'),call_oi=(value.get('call_wall') or {}).get('oi'),
                put_future=(value.get('put_wall') or {}).get('projected_future_level'),call_future=(value.get('call_wall') or {}).get('projected_future_level')))
        return result

    def collect(self,value,now=None):
        cfg=settings(value);fixed=now is not None;now=now or datetime.now(KST)
        sql='SELECT payload FROM oi_snapshots'+(' WHERE expiry=?' if cfg['expiry'] else '')+' ORDER BY id DESC LIMIT 1'
        cached=self.db.execute(sql,(cfg['expiry'],) if cfg['expiry'] else ()).fetchone()
        if cached:
            old=json.loads(cached[0]);age=(now-datetime.fromisoformat(old['observed_at'])).total_seconds()
            if 0<=age<60 and old['radius_pct']==cfg['radius_pct']:
                return dict(old,cached=True,history=self.history(old['expiry'],now))
        self.progress('옵션 월물 목록 조회 중…')
        listed=self.query(OI_EXPIRIES,dict(FID_COND_SCR_DIV_CODE='509',FID_COND_MRKT_DIV_CODE='',FID_COND_MRKT_CLS_CODE='')).get('output')
        if not isinstance(listed,list):raise TradingError('OI_EXPIRY_RESPONSE_INVALID')
        expiries=[];warnings=[]
        minimum=nearest_month(now)
        for item in listed:
            code=monthly_expiry(item)
            if code and code>=minimum:
                expiries.append(dict(code=code,label=code))
        expiries=sorted({r['code']:r for r in expiries}.values(),key=lambda r:r['code'])
        if not expiries:
            # The call/put endpoint officially accepts YYYYMM directly. Validate
            # actual nonempty chains before publishing any inferred month to UI.
            expiry=cfg['expiry'] or minimum
            if expiry<minimum:raise TradingError('선택 월물이 표준 만기일을 지났습니다. 자동 월물로 다시 조회하세요.')
            expiries=[dict(code=expiry,label=expiry)]
            warnings.append('월물 목록에서 유효한 만기를 확인하지 못해 '+expiry+' 일반 월물을 직접 조회했습니다. 실제 콜·풋 자료가 수신된 경우에만 표시합니다. 목록 수신 '+str(len(listed))+'행.')
        else:
            expiry=cfg['expiry'] or expiries[0]['code']
            if expiry not in {r['code'] for r in expiries}:raise TradingError('선택 만기가 현재 옵션 월물 목록에 없습니다. 자동 월물로 다시 조회하세요.')
        self.progress(expiry+' · 콜/풋 미결제약정 조회 중…')
        data=self.query(OI_OPTIONS,dict(FID_COND_MRKT_DIV_CODE='O',FID_COND_SCR_DIV_CODE='20503',FID_MRKT_CLS_CODE='CO',
            FID_MTRT_CNT=expiry,FID_MRKT_CLS_CODE1='PO',FID_COND_MRKT_CLS_CODE=''))
        calls,skip_call=chain(data.get('output1'),'call');puts,skip_put=chain(data.get('output2'),'put')
        if not calls or not puts:raise TradingError('콜·풋 OI 자료가 부족합니다. 실전 조회 권한과 선택 만기를 확인하세요.')
        spot=None;future=None
        self.progress('코스피200 지수와 선물 기준가격 조회 중…')
        try:
            out=self.query(OI_INDEX,dict(FID_COND_MRKT_DIV_CODE='U',FID_INPUT_ISCD='2001')).get('output')
            if isinstance(out,list):out=out[0] if len(out)==1 else None
            if not isinstance(out,dict):raise TradingError('OI_INDEX_RESPONSE_INVALID')
            spot=number(out.get('bstp_nmix_prpr'),'KOSPI200')
            if spot<=0:raise TradingError('OI_INDEX_PRICE_MISSING')
        except TradingError as exc:warnings.append(str(exc));spot=None
        try:
            out=self.query(OI_FUTURES,dict(FID_COND_MRKT_DIV_CODE='F',FID_COND_SCR_DIV_CODE='20503',FID_COND_MRKT_CLS_CODE='MKI')).get('output')
            if not isinstance(out,list):raise TradingError('OI_FUTURES_RESPONSE_INVALID')
            choices=[]
            skipped_futures=0
            for row in out:
                code=str(row.get('futs_shrn_iscd',''))
                if not re.fullmatch(r'101[A-Z0-9]{3,9}',code):continue
                try:
                    price=number(row.get('futs_prpr'),'FUTURE_PRICE')
                    days=number(row.get('hts_rmnn_dynu'),'REMAINING_DAYS',True,True,True)
                    if price<=0 or days is not None and days<0:continue
                    choices.append(dict(code=code,name=str(row.get('hts_kor_isnm',code))[:80],price=price,
                        volume=number(row.get('acml_vol'),'FUTURE_VOLUME',True,True),remaining_days=days,
                        oi=number(row.get('hts_otst_stpl_qty'),'FUTURE_OI',True,True)))
                except TradingError:skipped_futures+=1
            if skipped_futures:warnings.append('가격/수량이 확인되지 않는 선물 '+str(skipped_futures)+'개 행을 제외했습니다.')
            if not choices:raise TradingError('OI_KOSPI200_FUTURE_MISSING')
            future=sorted(choices,key=lambda r:(-(r['volume'] or 0),r['remaining_days'] if r['remaining_days'] is not None else 9999,r['code']))[0]
        except TradingError as exc:warnings.append(str(exc))
        basis=future['price']-spot if future and spot is not None else None
        observed=now if fixed else datetime.now(KST)
        previous=self.db.execute('SELECT payload FROM oi_snapshots WHERE expiry=? AND observed_at>=? AND observed_at<? ORDER BY id DESC LIMIT 1',
            (expiry,observed.date().isoformat(),observed.isoformat())).fetchone()
        previous=json.loads(previous[0]) if previous else None
        if previous:
            old={(r['side'],r['strike'],r['code']):r for r in previous['calls']+previous['puts']}
            for item in calls+puts:
                match=old.get((item['side'],item['strike'],item['code']))
                if match:item['observed_change']=item['oi']-match['oi']
        put_candidates=candidates(puts,spot,'put',cfg['radius_pct'],basis)
        call_candidates=candidates(calls,spot,'call',cfg['radius_pct'],basis)
        if len(calls)>=100 or len(puts)>=100:warnings.append('API는 각 측 최대 100건입니다. 수신 범위 안의 후보이며 전체 행사가를 보장하지 않습니다.')
        if skip_call+skip_put:warnings.append('행사가/OI 필드가 없는 '+str(skip_call+skip_put)+'개 행을 제외했습니다.')
        if not put_candidates or not call_candidates:warnings.append('현재 지수의 지정 근방에서 양쪽 후보가 모두 확인되지는 않았습니다.')
        if observed.weekday()>=5 or not ((8,45)<=(observed.hour,observed.minute)<(15,45)):
            warnings.append('정규장 외 조회입니다. 직전 장 자료일 수 있으며 원자료 갱신시각은 확인되지 않았습니다.')
        if (observed-now).total_seconds()>30:warnings.append('조회에 30초 이상 걸렸습니다. 지수·선물·OI는 동시 체결 시각 자료가 아니며 환산선은 참고값입니다.')
        snapshot=dict(collection_started_at=now.isoformat(),collection_seconds=(observed-now).total_seconds(),expiry=expiry,expiries=expiries,radius_pct=cfg['radius_pct'],observed_at=observed.isoformat(),source_time=None,
            spot=spot,future=future,basis=basis,calls=calls,puts=puts,put_candidates=put_candidates,call_candidates=call_candidates,
            put_wall=put_candidates[0] if put_candidates else None,call_wall=call_candidates[0] if call_candidates else None,
            comparison_at=previous['observed_at'] if previous else None,
            comparison_seconds=(observed-datetime.fromisoformat(previous['observed_at'])).total_seconds() if previous else None,
            warnings=warnings,source='한투 실전 조회 · 일반 월물 · 주문 미연동')
        self.db.execute('INSERT INTO oi_snapshots(observed_at,expiry,payload) VALUES (?,?,?)',(observed.isoformat(),expiry,json.dumps(snapshot,ensure_ascii=False)))
        return dict(snapshot,cached=False,history=self.history(expiry,observed))
