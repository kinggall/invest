"""Observed monthly KOSPI200 option OI concentrations; never execution signals."""
from datetime import datetime,timedelta
import json
import math
import time
from pathlib import Path
import re
import sqlite3
import io
import urllib.request
import zipfile
from trader import KST,TradingError,ReadOnlyDerivativesClient,DERIVATIVE_READS,OI_OPTIONS,OI_EXPIRIES,OI_FUTURES,OI_INDEX,OI_PRICE



MAX_QUOTES=12
BATCH_SECONDS=15
FRESH_SECONDS=600
SAMPLING_VERSION=2

_MASTER_CACHE=None


def parse_index_master(text):
    result=[]
    for line in text.splitlines():
        cols=[c.strip() for c in line.split('|')]
        if len(cols)!=9 or cols[0] not in ('5','6') or cols[7]!='2001':continue
        side='call' if cols[0]=='5' else 'put'
        name=re.fullmatch(r'[CP]\s+(20\d{2}(?:0[1-9]|1[0-2]))\s+[0-9,.]+',cols[3])
        if not name or not re.fullmatch(('B01' if side=='call' else 'C01')+r'[A-Z0-9]{6}',cols[1]):continue
        try:strike=number(cols[5],'MASTER_STRIKE')
        except TradingError:continue
        if strike>0:result.append(dict(side=side,code=cols[1],expiry=name[1],strike=strike))
    if not result:raise TradingError('OI_MASTER_MONTHLY_MISSING')
    return result



def parse_future_master(text):
    result=[]
    for line in text.splitlines():
        cols=[c.strip() for c in line.split('|')]
        if len(cols)!=9 or cols[0]!='1' or cols[7]!='2001':continue
        match=re.fullmatch(r'F\s+(20\d{2}(?:0[1-9]|1[0-2]))',cols[3])
        if match and re.fullmatch(r'(?:A01|101)[A-Z0-9]{3}',cols[1]):
            result.append(dict(code=cols[1],name=cols[3],expiry=match[1]))
    return sorted(result,key=lambda r:(r['expiry'],r['code']))


def load_future_master(day):
    load_index_master(day)
    if not _MASTER_CACHE:raise TradingError('OI_FUTURE_MASTER_MISSING')
    return _MASTER_CACHE[2]


def load_index_master(day):
    """Public official product metadata only; no credentials or token headers."""
    global _MASTER_CACHE
    if _MASTER_CACHE and _MASTER_CACHE[0]==day:return _MASTER_CACHE[1]
    try:
        with urllib.request.urlopen('https://new.real.download.dws.co.kr/common/master/fo_idx_code_mts.mst.zip',timeout=15) as response:
            raw=response.read(4*1024*1024+1)
        if len(raw)>4*1024*1024:raise ValueError('size')
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            info=archive.getinfo('fo_idx_code_mts.mst')
            if info.file_size>8*1024*1024:raise ValueError('size')
            text=archive.read(info).decode('cp949')
            result=parse_index_master(text)
            futures=parse_future_master(text)
    except TradingError:raise
    except Exception:raise TradingError('OI_MASTER_UNAVAILABLE') from None
    _MASTER_CACHE=(day,result,futures)
    return result


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
    selected=[r for r in rows if not r.get('stale',False) and r['oi']>0 and ((spot*(1-radius/100)<=r['strike']<spot) if side=='put' else (spot<r['strike']<=spot*(1+radius/100)))]
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
        old=None
        sql='SELECT payload FROM oi_snapshots'+(' WHERE expiry=?' if cfg['expiry'] else '')+' ORDER BY id DESC LIMIT 1'
        cached=self.db.execute(sql,(cfg['expiry'],) if cfg['expiry'] else ()).fetchone()
        if cached:
            old=json.loads(cached[0]);age=(now-datetime.fromisoformat(old['observed_at'])).total_seconds()
            if old.get('sampling_version')==SAMPLING_VERSION and old.get('requested_expiry')==cfg['expiry'] and datetime.fromisoformat(old['observed_at']).date()==now.date() and old['expiry']>=nearest_month(now) and 0<=age<60 and old['radius_pct']==cfg['radius_pct']:
                return dict(old,cached=True,history=self.history(old['expiry'],now))
        warnings=[];master=None;incremental=False;skip_call=skip_put=0
        try:
            self.progress('공식 월물 종목 확인 중…')
            master=load_index_master(now.date().isoformat())
            months=sorted({r['expiry'] for r in master if r['expiry']>=nearest_month(now)})
            if not months:raise TradingError('OI_MASTER_MONTHLY_MISSING')
            expiry=cfg['expiry'] or months[0]
            if expiry not in months:raise TradingError('선택 월물이 공식 종목 목록에 없습니다.')
            expiries=[dict(code=m,label=m) for m in months]
            incremental=True;calls=[];puts=[]
            if old and old.get('expiry')==expiry and datetime.fromisoformat(old['observed_at']).date()==now.date():
                calls=[dict(r) for r in old['calls']];puts=[dict(r) for r in old['puts']]
        except TradingError:
            # Public metadata unavailable: retain the existing board-only path.
            warnings.append('공식 종목 목록을 확인하지 못해 전광판으로 대체 조회합니다. 응답이 느릴 수 있습니다.')
            master=None
            self.progress('옵션 월물 목록 조회 중…')
            listed=self.query(OI_EXPIRIES,dict(FID_COND_SCR_DIV_CODE='509',FID_COND_MRKT_DIV_CODE='',FID_COND_MRKT_CLS_CODE='')).get('output')
            if not isinstance(listed,list):raise TradingError('OI_EXPIRY_RESPONSE_INVALID')
            expiries=[]
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
            board_at=(now if fixed else datetime.now(KST)).isoformat()
            for row in calls+puts:row['received_at']=board_at
        spot=None;future=None
        self.progress('코스피200 지수와 선물 기준가격 조회 중…')
        try:
            out=self.query(OI_INDEX,dict(FID_COND_MRKT_DIV_CODE='U',FID_INPUT_ISCD='2001')).get('output')
            if isinstance(out,list):out=out[0] if len(out)==1 else None
            if not isinstance(out,dict):raise TradingError('OI_INDEX_RESPONSE_INVALID')
            spot=number(out.get('bstp_nmix_prpr'),'KOSPI200')
            if spot<=0:raise TradingError('OI_INDEX_PRICE_MISSING')
        except TradingError as exc:warnings.append(str(exc));spot=None
        if not incremental:
            try:
                out=self.query(OI_FUTURES,dict(FID_COND_MRKT_DIV_CODE='F',FID_COND_SCR_DIV_CODE='20503',FID_COND_MRKT_CLS_CODE='MKI')).get('output')
                if not isinstance(out,list):raise TradingError('OI_FUTURES_RESPONSE_INVALID')
                choices=[]
                skipped_futures=0
                for row in out:
                    code=str(row.get('futs_shrn_iscd','')).strip()
                    if not re.fullmatch(r'(?:101|A01)[A-Z0-9]{3}',code):continue
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
        if future is None:
            try:
                contracts=[r for r in load_future_master(now.date().isoformat()) if r['expiry']>=nearest_month(now)]
                if not contracts:raise TradingError('OI_FUTURE_MASTER_MISSING')
                contract=contracts[0]
                self.progress(contract['code']+' · 공식 최근월물 선물 현재가 직접 조회 중…')
                quoted=self.query(OI_PRICE,dict(FID_COND_MRKT_DIV_CODE='F',FID_INPUT_ISCD=contract['code'])).get('output1')
                if not isinstance(quoted,dict):raise TradingError('OI_FUTURE_QUOTE_INVALID')
                price=number(quoted.get('futs_prpr'),'FUTURE_PRICE')
                days=number(quoted.get('hts_rmnn_dynu'),'REMAINING_DAYS',True,True,True)
                if price<=0 or days is not None and days<0:raise TradingError('OI_FUTURE_PRICE_MISSING')
                future=dict(code=contract['code'],name=contract['name'],price=price,
                    volume=number(quoted.get('acml_vol'),'FUTURE_VOLUME',True,True),remaining_days=days,
                    oi=number(quoted.get('hts_otst_stpl_qty'),'FUTURE_OI',True,True),source='공식 최근월물 · 개별 조회')
                warnings.append('선물 전광판에서 표준선물을 확인하지 못해 공식 종목 파일의 최근월물 '+contract['code']+' 현재가를 직접 수신했습니다.')
            except TradingError as exc:warnings.append(str(exc)+' · 선물 현재가 직접 조회도 확인되지 않았습니다.')
        # Board responses can stop before ATM because each side is capped at 100.
        # Get missing contracts from the official same-month master, never infer OI.
        coverage_complete=None;individual_count=0;targets=[];attempted=0;budget_hit=False
        if spot is not None and (incremental or len(calls)>=100 or len(puts)>=100 or not candidates(calls,spot,'call',cfg['radius_pct'],None) or not candidates(puts,spot,'put',cfg['radius_pct'],None)):
            coverage_complete=False
            try:
                master=master if master is not None else load_index_master(now.date().isoformat())
                targets=[r for r in master if r['expiry']==expiry and abs(r['strike']/spot-1)*100<=cfg['radius_pct']+1e-9]
                if not targets:raise TradingError('OI_MASTER_NEARBY_MONTH_MISSING')
                collected={(r['side'],r['strike']):r for r in calls+puts}
                # Keep previous source times. Only returned quotes get a new timestamp.
                def age_of(contract):
                    item=collected.get((contract['side'],contract['strike']))
                    if not item or item['code']!=contract['code'] or not item.get('received_at'):return float('inf')
                    return max(0,(now-datetime.fromisoformat(item['received_at'])).total_seconds())
                nearest=sorted(targets,key=lambda r:(abs(r['strike']-spot),r['strike'],r['side']))
                watched=[]
                if old and old.get('expiry')==expiry:
                    for side in ('put','call'):
                        wall=old.get(side+'_wall')
                        if wall:watched.extend(r for r in targets if r['side']==side and r['code']==wall['code'])
                priorities=[]
                for item in watched+nearest:
                    if item not in priorities:priorities.append(item)
                    if len(priorities)>=4:break
                rotating=sorted([r for r in targets if r not in priorities],key=lambda r:(-age_of(r),abs(r['strike']-spot),r['strike'],r['side']))
                batch=(priorities+rotating)[:MAX_QUOTES]
                failures=0;consecutive=0;started=time.monotonic()
                for i,contract in enumerate(batch):
                    if time.monotonic()-started>=BATCH_SECONDS:budget_hit=True;break
                    attempted+=1
                    self.progress(expiry+' · 빠른 OI 갱신 '+str(i+1)+'/'+str(len(batch))+' · 나머지는 순환 수집')
                    try:
                        quoted=self.query(OI_PRICE,dict(FID_COND_MRKT_DIV_CODE='O',FID_INPUT_ISCD=contract['code']))
                        fields={}
                        for key in ('output1','output2','output3'):
                            output=quoted.get(key)
                            if isinstance(output,dict):fields.update({k:v for k,v in output.items() if v not in (None,'')})
                        if fields.get('acpr') not in (None,'') and number(fields['acpr'],'QUOTE_STRIKE')!=contract['strike']:
                            raise TradingError('OI_QUOTE_STRIKE_MISMATCH')
                        fields.update(acpr=contract['strike'],optn_shrn_iscd=contract['code'])
                        parsed,skipped=chain([fields],contract['side'])
                        if not parsed:raise TradingError('OI_QUOTE_MISSING')
                        parsed[0]['received_at']=(now if fixed else datetime.now(KST)).isoformat()
                        collected[(contract['side'],contract['strike'])]=parsed[0]
                        individual_count+=1;consecutive=0
                    except TradingError:
                        failures+=1;consecutive+=1
                        if consecutive>=3:break
                calls=sorted([r for r in collected.values() if r['side']=='call'],key=lambda r:r['strike'])
                puts=sorted([r for r in collected.values() if r['side']=='put'],key=lambda r:r['strike'])
                coverage_complete=all(age_of(r)<=FRESH_SECONDS for r in targets)
                warnings.append('분할 갱신 '+str(individual_count)+'/'+str(len(targets))+'종목 · 현재가/직전 후보 우선, 나머지는 오래된 순으로 순환합니다. 10분 초과 자료는 후보에서 제외합니다.')
                if failures:warnings.append('개별 OI 조회 실패 '+str(failures)+'건. 결측을 0으로 대체하지 않았습니다.')
                if budget_hit:warnings.append('이번 개별 조회의 15초 시작 예산에 도달해 나머지는 다음 갱신으로 넘겼습니다. 진행 중 요청의 응답 시간은 추가될 수 있습니다.')
            except TradingError as exc:warnings.append(str(exc)+' · 현재 지수 근방 OI 범위를 확인하지 못했습니다.')
        basis=future['price']-spot if future and spot is not None else None
        observed=now if fixed else datetime.now(KST)
        previous=self.db.execute('SELECT payload FROM oi_snapshots WHERE expiry=? AND observed_at>=? AND observed_at<? ORDER BY id DESC LIMIT 1',
            (expiry,observed.date().isoformat(),observed.isoformat())).fetchone()
        previous=json.loads(previous[0]) if previous else None
        previous_rows={(r['side'],r['strike'],r['code']):r for r in previous['calls']+previous['puts']} if previous else {}
        for item in calls+puts:
            stamp=item.get('received_at')
            age=(observed-datetime.fromisoformat(stamp)).total_seconds() if stamp else None
            item['age_seconds']=max(0,age) if age is not None else None
            item['stale']=age is None or age<0 or age>FRESH_SECONDS
            match=previous_rows.get((item['side'],item['strike'],item['code']))
            updated=bool(stamp and (not match or stamp!=match.get('received_at')))
            item['reused']=not updated
            item['observed_change']=None;item['comparison_at']=None;item['comparison_seconds']=None
            if updated and match and match.get('received_at'):
                item['observed_change']=item['oi']-match['oi']
                item['comparison_at']=match['received_at']
                item['comparison_seconds']=(datetime.fromisoformat(stamp)-datetime.fromisoformat(match['received_at'])).total_seconds()
        target_keys={(r['side'],r['strike'],r['code']) for r in targets}
        target_rows=[r for r in calls+puts if (r['side'],r['strike'],r['code']) in target_keys]
        sampling=dict(mode='incremental' if incremental else 'board',total=len(targets),received=len(target_rows),fresh=sum(not r['stale'] for r in target_rows),
            updated=individual_count,attempted=attempted,max_quotes=MAX_QUOTES,budget_seconds=BATCH_SECONDS,fresh_seconds=FRESH_SECONDS)
        if targets:coverage_complete=sampling['fresh']==sampling['total']
        put_candidates=candidates(puts,spot,'put',cfg['radius_pct'],basis)
        call_candidates=candidates(calls,spot,'call',cfg['radius_pct'],basis)
        if not incremental and (len(calls)>=100 or len(puts)>=100):warnings.append('API는 각 측 최대 100건입니다. 수신 범위 안의 후보이며 전체 행사가를 보장하지 않습니다.')
        if skip_call+skip_put:warnings.append('행사가/OI 필드가 없는 '+str(skip_call+skip_put)+'개 행을 제외했습니다.')
        if not put_candidates or not call_candidates:warnings.append('현재 지수의 지정 근방에서 양쪽 후보가 모두 확인되지는 않았습니다.')
        if observed.weekday()>=5 or not ((8,45)<=(observed.hour,observed.minute)<(15,45)):
            warnings.append('정규장 외 조회입니다. 직전 장 자료일 수 있으며 원자료 갱신시각은 확인되지 않았습니다.')
        if (observed-now).total_seconds()>30:warnings.append('조회에 30초 이상 걸렸습니다. 지수·선물·OI는 동시 체결 시각 자료가 아니며 환산선은 참고값입니다.')
        snapshot=dict(collection_started_at=now.isoformat(),collection_seconds=(observed-now).total_seconds(),expiry=expiry,expiries=expiries,radius_pct=cfg['radius_pct'],observed_at=observed.isoformat(),source_time=None,
            sampling_version=SAMPLING_VERSION,requested_expiry=cfg['expiry'],sampling=sampling,spot=spot,future=future,basis=basis,calls=calls,puts=puts,coverage_complete=coverage_complete,individual_count=individual_count,put_candidates=put_candidates,call_candidates=call_candidates,
            put_wall=put_candidates[0] if put_candidates else None,call_wall=call_candidates[0] if call_candidates else None,
            comparison_at=previous['observed_at'] if previous else None,
            comparison_seconds=(observed-datetime.fromisoformat(previous['observed_at'])).total_seconds() if previous else None,
            warnings=warnings,source='한투 실전 조회 · 일반 월물 · 주문 미연동')
        self.db.execute('INSERT INTO oi_snapshots(observed_at,expiry,payload) VALUES (?,?,?)',(observed.isoformat(),expiry,json.dumps(snapshot,ensure_ascii=False)))
        return dict(snapshot,cached=False,history=self.history(expiry,observed))
