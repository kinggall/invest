"""Read-only OI context and ETF three-minute confirmation; never an order signal."""
from datetime import datetime
import math
from instruments import TRADE_SYMBOL
from trader import KST


def numeric(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)


def age(stamp,now):
    try:
        parsed=datetime.fromisoformat(stamp)
        if parsed.tzinfo is None:return None
        return (now-parsed).total_seconds()
    except (TypeError,ValueError):return None


def assess(snapshot=None,oi=None,now=None):
    now=now or datetime.now(KST);snapshot=snapshot or {};oi=oi or {}
    result=dict(observed_at=now.isoformat(),symbol=TRADE_SYMBOL,basis='KODEX 200 ETF 확정 3분봉',execution='OBSERVE_ONLY',
        macd=dict(state='UNKNOWN'),vwap=dict(state='UNKNOWN'),flow=dict(state='UNKNOWN'),walls=dict(state='UNKNOWN'),
        status='WAITING',message='OI 조회와 ETF 지표·수급 조회가 필요합니다.',reasons=[])
    regular=now.weekday()<5 and (9,0)<=(now.hour,now.minute)<(15,20)
    if not regular:result['reasons'].append('장외 또는 연속매매 종료 · 직전 자료 참고용')
    points=snapshot.get('chart') or []
    last=points[-1] if points else snapshot.get('last_bar') or {}
    previous=points[-2] if len(points)>1 else {}
    bar_age=age(last.get('end'),now)
    source_ok=snapshot.get('symbol')==TRADE_SYMBOL and bar_age is not None and 0<=bar_age<=90
    previous_age=age(previous.get('end'),now)
    contiguous=previous_age is not None and bar_age is not None and abs(previous_age-bar_age-180)<.001
    warmed=snapshot.get('bars',0)>=snapshot.get('required_bars',35)
    if source_ok and warmed and contiguous and all(numeric(r.get('histogram')) for r in (last,previous)) and all(numeric(last.get(k)) for k in ('macd','signal')):
        improving=last['histogram']>previous['histogram']
        above=last['macd']>last['signal']
        state='상향 교차' if last.get('cross')=='buy' else '하향 교차' if last.get('cross')=='sell' else '상방 우위' if above else '하방 우위'
        result['macd']=dict(periods=snapshot.get('periods'),state=state,improving=improving,above_signal=above,histogram=last['histogram'],previous_histogram=previous['histogram'],bar_end=last['end'])
    else:result['reasons'].append('ETF MACD 준비/최신 확정봉 확인 필요')
    if source_ok and numeric(last.get('vwap')) and last['vwap']>0 and numeric(last.get('close')) and numeric(last.get('vwap_change')):
        above=last['close']>last['vwap'];rising=last['vwap_change']>0
        result['vwap']=dict(state='VWAP 위' if above else 'VWAP 아래/동일',above=above,rising=rising,value=last['vwap'],price=last['close'],distance_pct=(last['close']/last['vwap']-1)*100,bar_end=last['end'])
    else:result['reasons'].append('완전한 당일 ETF VWAP 확인 필요')
    flow=snapshot.get('flow') or {};flow_age=age(flow.get('observed_at'),now)
    interval=flow.get('comparison_3m_seconds');anchor=flow.get('comparison_3m_at')
    actual_interval=age(anchor,datetime.fromisoformat(flow['observed_at'])) if flow.get('observed_at') and flow_age is not None else None
    flow_ok=(not flow.get('error') and flow.get('kind')=='kospi_intraday' and flow.get('market')=='KOSPI' and flow_age is not None and 0<=flow_age<=120
        and flow.get('observed_at','')[:10]==now.date().isoformat() and anchor and anchor[:10]==now.date().isoformat()
        and numeric(interval) and 180<=interval<=240 and actual_interval is not None and abs(actual_interval-interval)<1
        and all(numeric(flow.get(k)) for k in ('foreign_change_3m','institution_change_3m')))
    if flow_ok:
        foreign=flow['foreign_change_3m'];institution=flow['institution_change_3m'];combined=foreign+institution
        result['flow']=dict(state='합산 순매수 증가' if combined>0 else '합산 순매수 감소' if combined<0 else '합산 변화 없음',foreign=foreign,institution=institution,combined=combined,
            foreign_1m=flow.get('foreign_change'),institution_1m=flow.get('institution_change'),interval_seconds=interval,positive=combined>0,both_positive=foreign>0 and institution>0,
            observed_at=flow['observed_at'],unit=flow.get('unit','API 원단위 · 단위 미검증'))
    else:result['reasons'].append('코스피 수급의 같은 날 약 3분 변화 확인 필요')
    oi_age=age(oi.get('observed_at'),now)
    put=oi.get('put_wall');call=oi.get('call_wall');spot=oi.get('spot')
    valid_walls=oi_age is not None and 0<=oi_age<=150 and numeric(spot) and spot>0 and put and call
    if valid_walls:
        valid_walls=(numeric(put.get('strike')) and numeric(call.get('strike')) and put['strike']<spot<call['strike'])
        for wall in (put,call):
            elapsed=age(wall.get('received_at'),now)
            valid_walls=valid_walls and numeric(wall.get('oi')) and wall['oi']>0 and elapsed is not None and 0<=elapsed<=(180 if oi.get('row_fresh_seconds')==180 else 600)
    if valid_walls:
        complete=oi.get('coverage_complete') is True
        result['walls']=dict(state='범위 확인' if complete else '일부 수신 · 임시 후보',complete=complete,selection=oi.get('wall_selection'),spot=spot,put=put['strike'],call=call['strike'],
            put_distance_pct=(spot/put['strike']-1)*100,call_distance_pct=(call['strike']/spot-1)*100,observed_at=oi['observed_at'])
        if not complete:result['reasons'].append('OI 범위 수집 미완료 · 임시 후보')
    else:result['reasons'].append('최근 수신된 양쪽 OI 후보 확인 필요')
    if not result['reasons']:
        supportive=result['macd']['improving'] and result['vwap']['above'] and result['vwap']['rising'] and result['flow']['positive']
        result.update(status='SUPPORTIVE' if supportive else 'MIXED',message='상승 보조조건 일치 · 매수 주문 신호 아님' if supportive else '보조조건 혼재 · 항목별 확인')
    else:result['message']='판단 보류 · '+ ' / '.join(result['reasons'])
    return result
