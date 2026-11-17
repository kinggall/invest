"""Offline OI-strategy replay. Never fetches data, submits orders or edits source ledgers."""
from collections import Counter
from datetime import datetime,timedelta
import json
from pathlib import Path
import sqlite3
from oi import first_candidates,PRIMARY_RADIUS,PRIMARY_STRENGTH
from confirmation import assess,age,numeric
from instruments import TRADE_SYMBOL
from signals import aggregate,session_vwap,macd
from trader import KST,TradingError,integer
from wall_paper import WallPaper,RULES


def ensure_observations(db):
    db.execute('CREATE TABLE IF NOT EXISTS wall_observations (stamp TEXT PRIMARY KEY,payload TEXT)')


def capture(db,etf,oi,quote,now,manual=False):
    if not quote or not quote.get('price'):return
    # Two last bars suffice for the existing decision engine; no full-chart duplication.
    snap={k:etf.get(k) for k in ('symbol','price_time','bars','required_bars','periods','observed_at')}
    snap['chart']=(etf.get('chart') or [])[-2:]
    snap['flow']={k:v for k,v in (etf.get('flow') or {}).items() if k!='history'}
    context={k:oi.get(k) for k in ('expiry','observed_at','spot','put_wall','call_wall','coverage_complete','wall_selection','row_fresh_seconds')}
    frame=dict(version=1,at=now.isoformat(),etf=snap,oi=context,quote=quote,manual=bool(manual),recorded_rules=RULES)
    db.execute('INSERT OR IGNORE INTO wall_observations VALUES (?,?)',(frame['at'],json.dumps(frame,ensure_ascii=False)))


def source_connection(database):
    path=Path(database)
    if not path.is_file():raise TradingError('저장 자료 DB가 없습니다.')
    db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=10)
    db.row_factory=sqlite3.Row
    return db


def tables(db):return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def available_dates(database):
    if not Path(database).is_file():return []
    db=source_connection(database)
    try:
        found=set();names=tables(db)
        for table,col in [('wall_observations','stamp'),('oi_snapshots','observed_at')]:
            if table in names:found.update(r[0] for r in db.execute('SELECT DISTINCT substr('+col+',1,10) FROM '+table+' ORDER BY 1 DESC LIMIT 90'))
    finally:db.close()
    return sorted(found,reverse=True)[:90]



def reselect_snapshot(original):
    """Apply today's selector to that snapshot's past distribution, never today's OI."""
    if not isinstance(original.get('calls'),list) or not isinstance(original.get('puts'),list) or not numeric(original.get('spot')):
        return dict(original,replay_wall_selection='recorded_only')
    snapshot=dict(original);observed=datetime.fromisoformat(snapshot['observed_at']);radius=snapshot.get('radius_pct',5)
    for side,field in [('put','puts'),('call','calls')]:
        rows=[]
        for raw in snapshot[field]:
            elapsed=age(raw.get('received_at'),observed)
            rows.append(dict(raw,stale=elapsed is None or elapsed<0 or elapsed>(180 if snapshot.get('row_fresh_seconds')==180 else 600)))
        selected=first_candidates(rows,snapshot['spot'],side,radius,snapshot.get('basis'))
        snapshot[side+'_wall']=selected[0] if selected else None
    snapshot['wall_selection']=dict(mode='nearest_material_peak',primary_radius_pct=min(radius,PRIMARY_RADIUS),strength_ratio=PRIMARY_STRENGTH,major_radius_pct=radius)
    snapshot['replay_wall_selection']='recomputed_from_past_distribution'
    return snapshot


def load_sources(database,date,mode):
    start=date+'T00:00:00+09:00';end=(datetime.fromisoformat(start)+timedelta(days=1)).isoformat()
    db=source_connection(database)
    try:
        db.execute('BEGIN');names=tables(db)
        frames=[json.loads(r[0]) for r in db.execute('SELECT payload FROM wall_observations WHERE stamp>=? AND stamp<? ORDER BY stamp',(start,end))] if 'wall_observations' in names else []
        if mode=='recorded' or mode=='auto' and frames:
            if not frames:raise TradingError('선택일에 관측 입력 기록이 없습니다. 이 버전 실행 이후부터 저장됩니다. 저장 분봉 근사 모드를 선택할 수 있습니다.')
            return dict(mode='recorded',frames=frames)
        oi=[json.loads(r[0]) for r in db.execute('SELECT payload FROM oi_snapshots WHERE observed_at>=? AND observed_at<? ORDER BY observed_at,id',(start,end))] if 'oi_snapshots' in names else []
        if not oi:raise TradingError('선택일의 과거 OI 기록이 없습니다. 현재 벽을 과거에 소급 적용할 수 없습니다.')
        flows=[json.loads(r[0]) for r in db.execute("SELECT value FROM flow_samples WHERE market='KOSPI' AND stamp>=? AND stamp<? ORDER BY stamp",(start,end))] if 'flow_samples' in names else []
        current=[json.loads(r[0]) for r in db.execute('SELECT value FROM minutes WHERE symbol=? AND stamp>=? AND stamp<? ORDER BY stamp',(TRADE_SYMBOL,start,end))] if 'minutes' in names else []
        prior=[json.loads(r[0]) for r in db.execute('SELECT value FROM (SELECT stamp,value FROM minutes WHERE symbol=? AND stamp<? ORDER BY stamp DESC LIMIT 3000) ORDER BY stamp',(TRADE_SYMBOL,start))] if 'minutes' in names else []
        if not current:raise TradingError('선택일의 KODEX 200 원분봉이 없습니다. 다른 종목이나 선물 봉으로 대체하지 않습니다.')
        return dict(mode='minute',oi=[reselect_snapshot(s) for s in oi],flows=flows,current=current,prior=prior)
    finally:db.close()


def minute_frames(source,cfg):
    known=list(source['prior']);context={};flow={};oi_index=flow_index=0
    for row in source['current']:
        now=datetime.fromisoformat(row['time'])
        # Only observations that had already arrived by this minute may be used.
        while oi_index<len(source['oi']) and datetime.fromisoformat(source['oi'][oi_index]['observed_at'])<=now:
            context=source['oi'][oi_index];oi_index+=1
        while flow_index<len(source['flows']) and datetime.fromisoformat(source['flows'][flow_index]['observed_at'])<=now:
            flow=source['flows'][flow_index];flow_index+=1
        bars=session_vwap(known,aggregate(known,now))
        for i in range(len(bars)-1,0,-1):
            previous=datetime.fromisoformat(bars[i-1]['time']);stamp=datetime.fromisoformat(bars[i]['time'])
            if previous.date()==stamp.date() and stamp-previous!=timedelta(minutes=3):bars=bars[i:];break
        points=macd(bars,cfg['fast'],cfg['slow'],cfg['signal'])
        etf=dict(symbol=TRADE_SYMBOL,observed_at=now.isoformat(),price_time=known[-1]['time'] if known else None,
            bars=len(bars),required_bars=cfg['slow']+cfg['signal'],periods=[cfg['fast'],cfg['slow'],cfg['signal']],chart=points[-2:],flow=flow)
        # The upcoming minute's OPEN is a fill assumption, not a recorded REST quote.
        yield dict(at=now.isoformat(),etf=etf,oi=context,quote=dict(price=row['open'],observed_at=now.isoformat()),manual=False)
        known.append(row)


def replay(database,date,cfg,capital=10000000,mode='auto',progress=None):
    progress=progress or (lambda message:None)
    try:parsed=datetime.strptime(date,'%Y-%m-%d').date()
    except (ValueError,TypeError):raise TradingError('백테스트 날짜는 YYYY-MM-DD 형식으로 선택하세요.') from None
    if parsed>datetime.now(KST).date():raise TradingError('미래 날짜는 재생할 수 없습니다.')
    if mode not in ('auto','recorded','minute'):raise TradingError('REPLAY_MODE_INVALID')
    capital=integer(capital,'REPLAY_CAPITAL',100000)
    progress('저장된 OI·수급·ETF 자료를 읽는 중…')
    source=load_sources(database,date,mode)
    frames=source['frames'] if source['mode']=='recorded' else minute_frames(source,cfg)
    model=WallPaper(':memory:');model.reset(capital)
    counts=Counter();curve=[];chart={};seen_periods=set();first=last=None;ready=0;manual=0;warnings=[];oi_times=set();flow_times=set();ready_first=ready_last=None
    try:
        for i,frame in enumerate(frames):
            now=datetime.fromisoformat(frame['at']);etf=frame['etf'];oi=frame.get('oi') or {};quote=frame.get('quote') or {}
            if now.date()!=parsed:continue
            # No manual exit or live-ledger state is imported: this is a new strategy run.
            result=model.step(etf,oi,quote,now=now)
            counts[result['state']['status']]+=1;is_ready=assess(etf,oi,now)['status']!='WAITING';ready+=is_ready;manual+=bool(frame.get('manual'))
            if is_ready:ready_first=ready_first or now.isoformat();ready_last=now.isoformat()
            if oi.get('observed_at'):oi_times.add(oi['observed_at'])
            if (etf.get('flow') or {}).get('observed_at'):flow_times.add(etf['flow']['observed_at'])
            first=first or now.isoformat();last=now.isoformat()
            curve.append(dict(time=now.isoformat(),value=result['equity'],quantity=result['quantity']))
            if etf.get('periods'):seen_periods.add(tuple(etf['periods']))
            for bar in etf.get('chart') or []:
                if datetime.fromisoformat(bar['end'])<=now and bar['time'][:10]==date:chart.setdefault(bar['time'],bar)
            if i%100==0:progress(str(i+1)+'개 저장 표본 평가 중…')
        if not curve:raise TradingError('선택일에 평가할 관측 표본이 없습니다.')
        result=model.view();events=[json.loads(r[0]) for r in model.db.execute('SELECT payload FROM wall_paper_events ORDER BY rowid')]
        peak=capital;drawdown=0
        for point in curve:
            if point['value'] is not None:
                peak=max(peak,point['value']);drawdown=max(drawdown,(peak-point['value'])/peak*100)
        trades={}
        for event in events:
            key=event['id'].rsplit(':',1)[0];trade=trades.setdefault(key,dict(quantity=0,pnl=0))
            trade['quantity']+=event['quantity']*(1 if event['side']=='buy' else -1);trade['pnl']+=event['realized']
        completed=[t for t in trades.values() if t['quantity']==0]
        if source['mode']=='minute':warnings.append('1분 시가 가정의 근사 백테스트입니다. 실제 REST 현재가·10초 감시·봉중 급락/손절·원분봉의 당시 저장 시점을 재현하지 못합니다.')
        else:warnings.append('저장 관측 입력만 시간순 재생합니다. 입력 기록 전/앱 중지 구간은 재현하지 않으며 당시 저장 지표를 사용합니다. 현재 MACD 기간 변경을 소급 계산하지 않습니다.')
        if source['mode']=='minute':
            recomputed=sum(s.get('replay_wall_selection')=='recomputed_from_past_distribution' for s in source['oi'])
            warnings.append('당시 저장 OI 분포에 현재 1차 벽 규칙 재적용 '+str(recomputed)+'/'+str(len(source['oi']))+'건. 원분포가 없는 나머지는 기록된 선택 벽만 사용합니다.')
        warnings.append('현재 전략 코드로 초기 자금·미보유 상태에서 새로 실행합니다. 실제 가상 장부의 시작 보유나 수동 청산을 가져오지 않습니다.')
        if not ready:warnings.append('진입 판단에 필요한 자료가 동시에 충족된 표본이 없습니다. 거래 0건은 전략 성과의 증거가 아닙니다.')
        if result['quantity']:warnings.append('마지막 표본에도 미청산 보유가 있습니다. 마지막 관측 가격으로 평가했으며 임의로 장 마감 청산하지 않았습니다.')
        if first[11:16]>'09:00' or last[11:16]<'15:15':warnings.append('전 거래시간을 포함하지 않는 기록입니다. 표시된 첫/마지막 표본 구간만 평가했습니다.')
        return dict(date=date,mode=source['mode'],symbol=TRADE_SYMBOL,capital=capital,first=first,last=last,samples=len(curve),ready_samples=ready,status_counts=dict(counts),
            final_equity=result['equity'],net_pnl=result['net_pnl'],return_pct=result['net_pnl']/capital*100,realized=result['state']['realized'],unrealized=result['unrealized'],fees=result['state']['fees'],
            valuation_at=result['state'].get('mark_at'),oi_first=min(oi_times) if oi_times else None,oi_last=max(oi_times) if oi_times else None,flow_first=min(flow_times) if flow_times else None,flow_last=max(flow_times) if flow_times else None,ready_first=ready_first,ready_last=ready_last,
            ending_quantity=result['quantity'],position=result['state']['position'],max_drawdown_pct=drawdown,round_trips=len(completed),win_rate_pct=sum(t['pnl']>0 for t in completed)/len(completed)*100 if completed else None,
            buy_count=sum(e['side']=='buy' for e in events),sell_count=sum(e['side']=='sell' for e in events),events=events,curve=curve,chart=list(sorted(chart.values(),key=lambda b:b['time'])),
            periods=[list(p) for p in sorted(seen_periods)],manual_inputs_ignored=manual,warnings=warnings,rules=RULES)
    finally:model.close()
