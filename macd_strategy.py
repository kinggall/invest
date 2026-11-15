"""One ETF, closed-bar MACD crosses, fill-based positions, per-signal deduplication."""
from datetime import datetime, timedelta
import json
import re
import sqlite3
from trader import Engine, KST, TradingError, integer
from market import MarketData
from signals import aggregate, macd, session_vwap, vwap_entry

SYMBOL = '472150'
NAME = 'TIGER 배당커버드콜액티브'
DEFAULT = dict(id='tiger-macd-v1', mode='macd', symbol=SYMBOL, quantity=1, fast=12, slow=26,
               signal=9, max_notional=100000, limit_offset_bps=10, max_daily_buys=10, vwap_filter=False)


def validate(value):
    result = {key:value.get(key,default) for key,default in DEFAULT.items()}
    if result['symbol'] != SYMBOL or result['mode'] != 'macd' or not re.fullmatch(r'[A-Za-z0-9_-]{1,32}',result['id']):
        raise TradingError('고정 ETF 종목과 전략 ID를 확인하세요.')
    for key in ('quantity','fast','slow','signal','max_notional','max_daily_buys'):
        result[key]=integer(result[key],key.upper())
    if not isinstance(result['vwap_filter'],bool):
        raise TradingError('VWAP 필터는 켜기/끄기로 선택하세요.')
    result['limit_offset_bps']=integer(result['limit_offset_bps'],'OFFSET',0)
    if not (result['fast'] < result['slow'] <= 100 and result['signal'] <= 100 and result['limit_offset_bps'] <= 100):
        raise TradingError('MACD 기간 또는 지정가 허용폭을 확인하세요.')
    return result


class MacdEngine(Engine):
    def __init__(self, database, client, market_factory=MarketData):
        super().__init__(database,client)
        self.db.execute('''CREATE TABLE IF NOT EXISTS signals (
            event_id TEXT PRIMARY KEY, scope TEXT, bar_time TEXT, side TEXT,
            status TEXT, value TEXT)''')
        self.market=market_factory(self)

    def position(self):
        rows=self.db.execute('SELECT * FROM orders WHERE scope=? AND symbol=?',(self.scope,SYMBOL)).fetchall()
        quantity=0
        pending=False
        for row in rows:
            side=json.loads(row['strategy'])['side']
            quantity+=row['filled']*(1 if side=='buy' else -1)
            if row['status'] in ('UNKNOWN','SUBMITTED','PARTIAL'):
                pending=True
        if quantity < 0:raise TradingError('POSITION_RECONCILIATION_REQUIRED')
        return quantity,pending

    def evaluate(self, strategy, execute=False, now=None, allow_submit=None):
        cfg=validate(strategy)
        now=now or datetime.now(KST)
        # Sync first: positions are derived from confirmed fills, never order intentions.
        fills=self.sync()
        position,pending=self.position()
        flow=self.market.flow(SYMBOL,now)
        minutes=self.market.minutes(SYMBOL,now)
        bars=session_vwap(minutes,aggregate(minutes,now))
        # Reset warm-up after missing intraday bars instead of silently bridging a data hole.
        for index in range(len(bars)-1,0,-1):
            previous=datetime.fromisoformat(bars[index-1]['time'])
            current=datetime.fromisoformat(bars[index]['time'])
            if previous.date()==current.date() and current-previous != timedelta(minutes=3):
                bars=bars[index:]
                break
        points=macd(bars,cfg['fast'],cfg['slow'],cfg['signal'])
        needed=cfg['slow']+cfg['signal']
        last=points[-1] if points else None
        snapshot=dict(status='WARMUP', symbol=SYMBOL,name=NAME,position=position,pending=pending,
            flow=flow,chart=points[-120:],bars=len(bars),required_bars=needed,fills=fills,submitted=False)
        if not last or len(points)<needed:
            return snapshot
        stamp=datetime.fromisoformat(last['end'])
        snapshot.update(last_bar=last, status='NO_SIGNAL')
        side=last['cross']
        day=now.strftime('%Y%m%d')
        if side:
            event_id=self.scope+':'+cfg['id']+':'+last['time']+':'+side
            record=dict(strategy=cfg,bar=last,flow={k:v for k,v in flow.items() if k!='history'},position=position,observed_at=now.isoformat())
            self.db.execute('INSERT OR IGNORE INTO signals VALUES (?,?,?,?,?,?)',
                (event_id,self.scope,last['time'],side,'OBSERVED',json.dumps(record)))
            snapshot['event_id']=event_id
            snapshot['status']='BUY_SIGNAL' if side=='buy' else 'SELL_SIGNAL'
        # Only regular continuous KRX trading. Holidays require fresh bars below.
        if now.weekday()>=5 or not ((9,0) <= (now.hour,now.minute) < (15,20)):
            snapshot['status']='MARKET_CLOSED'
            return snapshot
        if stamp.date()!=now.date() or not 0 <= (now-stamp).total_seconds() <= 90:
            snapshot['status']='STALE_BAR'
            return snapshot
        # Do not bridge missing intraday bars when detecting a cross.
        previous=datetime.fromisoformat(points[-2]['end'])
        if previous.date()==stamp.date() and stamp-previous != timedelta(minutes=3):
            snapshot['status']='BAR_GAP'
            return snapshot
        if not side:return snapshot
        if pending:
            snapshot['status']='ORDER_PENDING'
            return snapshot
        if (side=='buy' and position>0) or (side=='sell' and position==0):
            snapshot['status']='POSITION_HOLD'
            return snapshot
        if side=='buy' and cfg['vwap_filter'] and not vwap_entry(last):
            snapshot['status']='VWAP_UNAVAILABLE' if last.get('vwap') is None else 'VWAP_FILTERED'
            record['entry_filter']=snapshot['status']
            self.db.execute("UPDATE signals SET value=? WHERE event_id=? AND status='OBSERVED'",(json.dumps(record),event_id))
            return snapshot
        quantity=cfg['quantity'] if side=='buy' else position
        count=sum(json.loads(row[0]).get('side')=='buy' for row in self.db.execute('SELECT strategy FROM orders WHERE scope=? AND day=? AND symbol=?',(self.scope,day,SYMBOL)))
        if side=='buy' and count>=cfg['max_daily_buys']:
            snapshot['status']='DAILY_LIMIT'
            return snapshot
        if not execute:return snapshot
        if allow_submit is not None and not allow_submit():
            snapshot['status']='STOPPED'
            return snapshot
        # Atomic same-bar reservation: preview does not consume a signal.
        existing=self.db.execute('SELECT status FROM signals WHERE event_id=?',(event_id,)).fetchone()
        if existing['status']!='OBSERVED':
            snapshot['status']='SIGNAL_PROCESSED'
            return snapshot
        quote=self.client.quote(SYMBOL)
        # KRX ETF tick size is 5 KRW. Favor execution within the configured limit band.
        scaled=quote*(10000+(cfg['limit_offset_bps'] if side=='buy' else -cfg['limit_offset_bps']))
        limit=((scaled+49999)//50000)*5 if side=='buy' else (scaled//50000)*5
        if limit<=0 or (side=='buy' and quantity*limit>cfg['max_notional']):
            snapshot['status']='MAX_NOTIONAL_EXCEEDED'
            return snapshot
        order_cfg=dict(id=cfg['id']+'-'+stamp.strftime('%Y%m%d%H%M')+'-'+side,
            symbol=SYMBOL,side=side,quantity=quantity,limit_price=limit,trigger_price=limit,
            max_notional=cfg['max_notional'],mode='macd',parent_id=cfg['id'],event_id=event_id)
        run_id=self.scope+':'+day+':'+order_cfg['id']
        self.db.execute('BEGIN IMMEDIATE')
        try:
            # Serialize outstanding-order and position checks across dashboard instances.
            current,pending=self.position()
            existing=self.db.execute('SELECT status FROM signals WHERE event_id=?',(event_id,)).fetchone()
            if pending or current!=position or existing['status']!='OBSERVED' or (allow_submit is not None and not allow_submit()):
                self.db.execute('ROLLBACK')
                snapshot['status']='ORDER_PENDING'
                return snapshot
            self.db.execute('INSERT INTO orders(run_id,scope,day,symbol,quantity,strategy,status) VALUES (?,?,?,?,?,?,?)',
                (run_id,self.scope,day,SYMBOL,quantity,json.dumps(order_cfg),'UNKNOWN'))
            self.db.execute("UPDATE signals SET status='RESERVED',value=? WHERE event_id=?",(json.dumps(record),event_id))
            self.db.execute('COMMIT')
        except Exception:
            self.db.execute('ROLLBACK')
            raise
        # Failures retain UNKNOWN and reservation; no automatic resubmission.
        number,branch=self.client.order(order_cfg)
        self.db.execute('UPDATE orders SET status=?,order_no=?,branch=? WHERE run_id=?',('SUBMITTED',number,branch,run_id))
        self.db.execute("UPDATE signals SET status='SUBMITTED' WHERE event_id=?",(event_id,))
        snapshot.update(status='SUBMITTED',order_no=number,submitted=True,pending=True)
        return snapshot
