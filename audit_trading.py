"""Read-only trading audit; optional existing-token market GET, never orders or ledger repairs."""
import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta
import json
import os
import sqlite3
from types import SimpleNamespace
from pathlib import Path
from trader import TradingError, KST
from instruments import TRADE_SYMBOL
from wall_paper import WallPaper, quantity, average
from wall_broker import Intent
from wall_replay import source_connection, tables


class Probe(WallPaper):
    def event(self,*args):raise Intent(*args)


def audit(database,day):
    parsed=datetime.strptime(day,'%Y-%m-%d').date();day=parsed.isoformat()
    lower=parsed.isoformat();upper=(parsed+timedelta(days=1)).isoformat()
    database=Path(database);broker=database.with_name(database.stem+'.demo.sqlite3')
    report=dict(date=day,symbol=TRADE_SYMBOL,method='로컬 기록만 분석',orders=[],trace=[],observations={},inference=None)
    report['method']='로컬 기록 읽기 전용 · API 호출/주문/장부 변경 없음'
    if not broker.is_file():raise TradingError('모의 주문 장부 파일이 없습니다.')
    db=source_connection(broker)
    try:
        db.execute('BEGIN');names=tables(db)
        if not {'orders','broker_meta','wall_paper_state'}<=names:raise TradingError('모의 장부 형식이 올바르지 않습니다.')
        state=json.loads(db.execute('SELECT payload FROM wall_paper_state WHERE id=1').fetchone()[0])
        order_rows=db.execute('SELECT o.*,m.payload AS meta FROM orders o JOIN broker_meta m ON m.id=o.run_id ORDER BY o.rowid').fetchall()
        fills=[json.loads(r[0]) for r in db.execute('SELECT payload FROM wall_paper_events ORDER BY rowid')]
        if 'broker_trace' in names:
            report['trace']=[dict(at=r['stamp'],kind=r['kind'],details=json.loads(r['payload'])) for r in
                db.execute('SELECT * FROM broker_trace WHERE stamp>=? AND stamp<? ORDER BY id',(lower,upper))]
        if 'broker_settings' in names:
            row=db.execute("SELECT payload FROM broker_settings WHERE id='last_error'").fetchone()
            report['last_saved_error']=json.loads(row[0]) if row else None
            report['last_error_is_for_selected_day']=bool(report['last_saved_error'] and report['last_saved_error'].get('at','')[:10]==day)
    finally:db.close()
    for row in order_rows:
        if row['day']!=day.replace('-',''):continue
        meta=json.loads(row['meta']);strategy=json.loads(row['strategy'])
        report['orders'].append(dict(at=meta['time'],side=strategy['side'],reason=meta['reason'],quantity=row['quantity'],filled=row['filled'],
            status=row['status'],order_no=row['order_no'] or None,cancel_requested=bool(meta.get('cancel_requested'))))
    frames=[];minutes=[]
    if database.is_file():
        db=source_connection(database)
        try:
            db.execute('BEGIN');names=tables(db)
            if 'wall_observations' in names:
                frames=[json.loads(r[0]) for r in db.execute('SELECT payload FROM wall_observations WHERE stamp>=? AND stamp<? ORDER BY stamp',(lower,upper))]
            if 'minutes' in names:
                minutes=[json.loads(r[0]) for r in db.execute('SELECT value FROM minutes WHERE symbol=? AND stamp>=? AND stamp<? ORDER BY stamp',(TRADE_SYMBOL,lower,upper))]
        finally:db.close()
    report['observations']=dict(count=len(frames),first=frames[0]['at'] if frames else None,last=frames[-1]['at'] if frames else None,
        last_saved_mark=state.get('mark_at'),note='마감 청산은 별도 경로이므로 관측 입력 부재만으로 마감 평가 여부를 단정하지 않습니다.')
    p=state['position'];report['current_ledger_position']=dict(quantity=quantity(p),average=average(p),entry_day=p.get('entry_day') if p else None,
        stop_price=p.get('stop_price') if p else None,put_stop=p.get('put_stop') if p else None,target1=p.get('target1') if p else None,target2=p.get('target2') if p else None)
    sells=[r for r in report['orders'] if r['side']=='sell']
    proven=[t for t in report['trace'] if t['kind']=='SIGNAL' and t['details'].get('side')=='sell']
    evaluated=[t for t in report['trace'] if t['kind']=='EVALUATION_STARTED' and '09:00'<=t['at'][11:16]<'15:20']
    report['trace_coverage']=dict(count=len(evaluated),first=evaluated[0]['at'] if evaluated else None,last=evaluated[-1]['at'] if evaluated else None)
    report['evidence']=dict(sell_order_count=len(sells),proven_sell_signal_count=len(proven),trace_available=bool(evaluated),
        conclusion='매도 주문 기록 있음 · 접수/체결 상태를 확인해야 합니다.' if sells else
                   '매도 판단 기록은 있으나 주문 기록 없음 · 전송 전 차단/오류 확인 필요' if proven else
                   '매도 주문 기록 없음 · 기존 판단 로그가 없어 매도 신호 유무는 확정 불가' if not evaluated else
                   '저장된 판단 로그에 매도 신호 없음 · 기록된 시간 구간에 한한 결과')
    report['scheduled_close']=dict(required_if_holding=True,starts_at=day+'T15:15:00+09:00',target=day+'T15:19:00+09:00',
        registered_close_orders=sum(r['reason']=='DAY_EXIT' for r in sells),note='보유 중이면 별도 익절/손절 신호가 없어도 15:15 청산 조건이 존재합니다.')
    # Restrict retrospective reconstruction to the exact simple case we can prove:
    # one confirmed entry fill, still open, without any later fill changing its basis.
    related=[r for r in order_rows if p and json.loads(r['meta'])['position']['id']==p['id']]
    entry=next((r for r in related if json.loads(r['meta'])['reason']=='REBOUND_ENTRY'),None)
    entry_events=[f for f in fills if entry and f['id'].startswith(entry['run_id']+':')]
    related_fills=[f for f in fills if any(f['id'].startswith(r['run_id']+':') for r in related)]
    can_recheck=bool(p and p.get('entry_day')==day and len(entry_events)==1 and len(related_fills)==1)
    if can_recheck:
        at=entry_events[0]['time'];base=deepcopy(state);base.update(day=day,armed=None,cooldown_until=None)
        model=Probe(':memory:');signals=[];counts=Counter();used=[]
        try:
            for frame in frames:
                if frame['at']<at:continue
                now=datetime.fromisoformat(frame['at']);used.append(frame['at'])
                try:
                    decision=model.advance(deepcopy(base),frame.get('etf') or {},frame.get('oi') or {},frame.get('quote') or {},now,False)
                    counts[decision['status']]+=1
                except Intent as signal:
                    value=signal.value
                    if value['side']=='sell':signals.append(dict(at=frame['at'],reason=value['reason'],price=value['reference'],quantity=value['quantity']))
        finally:model.close()
        report['inference']=dict(evidence='실제 미청산 보유를 각 저장 입력에서 독립 재평가 · 당시 실행된 판단의 증거는 아니며 가상 매도를 누적하지 않음',
            entry_fill_at=at,samples_after_entry=len(used),first=used[0] if used else None,last=used[-1] if used else None,
            first_recomputed_exit=signals[0] if signals else None,recomputed_reasons=dict(Counter(s['reason'] for s in signals)),statuses=dict(counts))
        breach=next((r for r in minutes if r.get('time','')>=at and isinstance(r.get('low'),(int,float)) and r['low']<=p['stop_price']),None)
        report['minute_price_evidence']=dict(first_low_below_stop=dict(time=breach['time'],low=breach['low'],stop=p['stop_price']) if breach else None,
            note='분봉 저가 이탈은 시장 가격 참고입니다. 실제 REST 현재가 수신이나 당시 매도 판단·전송을 증명하지 않습니다.')
    else:report['inference_note']='단일 매수·추가 체결 없음·당일 미청산 사례가 아니어서 현재 보유로 과거를 재구성하지 않았습니다.'
    return report


def fetch_price_evidence(report,home,client=None):
    from history import History
    from trader import ReadOnlyHistoryClient
    memory=sqlite3.connect(':memory:',isolation_level=None)
    try:
        memory.execute('CREATE TABLE minutes (symbol TEXT,stamp TEXT,value TEXT,PRIMARY KEY(symbol,stamp))')
        reader=History(SimpleNamespace(db=memory),client=client or ReadOnlyHistoryClient(home),progress=lambda message:None)
        rows=reader.day(datetime.strptime(report['date'],'%Y-%m-%d').date())
    finally:memory.close()
    entry=(report.get('inference') or {}).get('entry_fill_at');stop=report['current_ledger_position'].get('stop_price')
    after=[r for r in rows if entry and r['time']>=entry]
    low=next((r for r in after if stop and r['low']<=stop),None)
    close=next((r for r in after if stop and r['close']<=stop),None)
    def sample(row):return {k:row[k] for k in ('time','low','close')} if row else None
    report['downloaded_price_evidence']=dict(source='한투 실전 시세 GET · KODEX 200 실제 1분봉',samples=len(rows),first=rows[0]['time'] if rows else None,last=rows[-1]['time'] if rows else None,
        stop_price=stop,first_low_below_stop=sample(low),first_close_below_stop=sample(close),
        note='단일 진입 이후 실제 시장 가격의 손절선 이탈 확인입니다. 당시 프로그램 수신·매도 판단을 증명하지 않습니다. 선물 가격을 현물 지수 손절선과 직접 비교하지 않습니다.' if entry else '단일 진입 평단을 확인할 수 없어 손절선 비교를 보류했습니다.')
    report['method']='로컬 장부는 읽기 전용 · 실제 1분봉은 기존 실전 토큰으로 GET · 주문/토큰 발급 없음'
    return report


def local_home(value=None):
    if value:return Path(value)
    if os.environ.get('DOMINO_HOME'):return Path(os.environ['DOMINO_HOME'])
    if os.environ.get('LOCALAPPDATA'):
        path=Path(os.environ['LOCALAPPDATA'])/'invest'/'settings.json'
        if path.is_file():return Path(json.loads(path.read_text(encoding='utf-8-sig'))['domino_home'])
    raise TradingError('DOMINO_HOME_REQUIRED')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--date',default=datetime.now(KST).date().isoformat())
    parser.add_argument('--state',type=Path,default=Path(__file__).parent/'runtime'/'orders.sqlite3')
    parser.add_argument('--fetch-prices',action='store_true',help='Read actual ETF minute prices with the existing real token; never orders')
    parser.add_argument('--domino-home',type=Path)
    args=parser.parse_args()
    try:
        result=audit(args.state,args.date)
        if args.fetch_prices:result=fetch_price_evidence(result,local_home(args.domino_home))
        target=args.state.parent/('audit-'+args.date+'.json')
        target.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');target.chmod(0o600)
        print(json.dumps({k:result[k] for k in ('evidence','observations','current_ledger_position','inference')},ensure_ascii=False,indent=2))
        if result.get('downloaded_price_evidence'):print(json.dumps(result['downloaded_price_evidence'],ensure_ascii=False,indent=2))
        print('진단 파일: '+str(target))
    except TradingError as exc:raise SystemExit('진단/시세 조회 실패: '+str(exc))
    except (ValueError,OSError):raise SystemExit('진단 실패: 날짜와 runtime 모의 장부 파일을 확인하세요.')


if __name__=='__main__':main()
