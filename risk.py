"""Client-side protective stops for confirmed demo fills. No real-order endpoint."""
from datetime import datetime
import json
import math
import uuid
from trader import KST, TradingError


def stop_percent(value):
    if isinstance(value,bool):raise TradingError('INVALID_STOP_LOSS_PERCENT')
    try:result=float(value)
    except (ValueError,TypeError):raise TradingError('INVALID_STOP_LOSS_PERCENT') from None
    if not math.isfinite(result) or not 0<result<=20:raise TradingError('INVALID_STOP_LOSS_PERCENT')
    return result


class RiskManager:
    def __init__(self,engine,symbol):
        self.engine=engine;self.db=engine.db;self.client=engine.client;self.scope=engine.scope;self.symbol=symbol
        self.db.execute('''CREATE TABLE IF NOT EXISTS risk_state (
            scope TEXT, symbol TEXT, latched INTEGER DEFAULT 0, episode TEXT,
            stop_price REAL, trigger_price REAL, entry_price REAL, sequence INTEGER DEFAULT 0,
            PRIMARY KEY(scope,symbol))''')

    def state(self):
        row=self.db.execute('SELECT * FROM risk_state WHERE scope=? AND symbol=?',(self.scope,self.symbol)).fetchone()
        return dict(row) if row else None

    def latched(self):
        state=self.state()
        return bool(state and state['latched'])

    def position(self):
        quantity=0;basis=0.0;unknown_basis=False;missing=[];pending=[]
        for row in self.db.execute('SELECT * FROM orders WHERE scope=? AND symbol=? ORDER BY rowid',(self.scope,self.symbol)):
            strategy=json.loads(row['strategy']);filled=row['filled']
            if strategy['side']=='buy' and filled:
                quantity+=filled
                if row['filled_cash'] is None:
                    unknown_basis=True;missing.append(row['run_id'])
                else:basis+=row['filled_cash']
            elif strategy['side']=='sell' and filled:
                if filled>quantity:raise TradingError('RISK_POSITION_RECONCILIATION_REQUIRED')
                basis=basis*(quantity-filled)/quantity if quantity else 0
                quantity-=filled
                if quantity==0:basis=0;unknown_basis=False;missing=[]
            if row['status'] in ('SUBMITTED','PARTIAL','UNKNOWN'):
                pending.append(dict(row,side=strategy['side']))
        return dict(quantity=quantity,average_entry=basis/quantity if quantity and not unknown_basis else None,
            missing_basis=missing,pending=pending)

    def reset(self):
        details=self.position()
        if details['quantity'] or details['pending']:
            raise TradingError('손절 청산·잔여 주문 확인 후 재개하세요.')
        self.db.execute('UPDATE risk_state SET latched=0 WHERE scope=? AND symbol=?',(self.scope,self.symbol))

    def check(self,cfg,execute=False,now=None,allow_submit=None):
        percent=stop_percent(cfg.get('stop_loss_pct',1))
        if not cfg.get('stop_loss_enabled',False):return dict(status='DISABLED',percent=percent)
        now=now or datetime.now(KST)
        self.engine.sync(symbol=self.symbol)
        details=self.position()
        # Only the current open cycle needs historical fill-price backfill.
        for run_id in details['missing_basis']:
            row=self.db.execute('SELECT * FROM orders WHERE run_id=?',(run_id,)).fetchone()
            self.engine.refresh_order(row)
        details=self.position();state=self.state()
        latched=bool(state and state['latched'])
        base=dict(percent=percent,quantity=details['quantity'],average_entry=details['average_entry'],
            latched=latched,observed_at=now.isoformat(timespec='seconds'),submitted=False)
        if not details['quantity'] and any(row['status']=='UNKNOWN' for row in details['pending']):
            return dict(base,status='BLOCKED_UNKNOWN',message='접수 불명 주문을 확인해야 합니다.')
        if not details['quantity']:
            base['status']='WAIT_BUY_REMAINDER' if details['pending'] else ('COMPLETE' if latched else 'NO_POSITION')
            return base
        if details['average_entry'] is None and not latched:
            return dict(base,status='BASIS_UNAVAILABLE',message='체결평단을 확인하지 못해 손절 감시를 준비할 수 없습니다.')
        threshold=state['stop_price'] if latched else details['average_entry']*(1-percent/100)
        base['stop_price']=threshold
        if now.weekday()>=5 or not ((9,0)<=(now.hour,now.minute)<(15,30)):
            return dict(base,status='MARKET_CLOSED')
        price=self.client.quote(self.symbol)
        base.update(price=price,price_return_pct=(price/details['average_entry']-1)*100 if details['average_entry'] else None)
        if not latched and price>threshold:return dict(base,status='WATCHING')
        if not execute:return dict(base,status='WOULD_STOP')
        if allow_submit is not None and not allow_submit():return dict(base,status='STOPPED')
        # Latch before transmission and re-read positions under the same DB write lock.
        self.db.execute('BEGIN IMMEDIATE')
        try:
            fresh=self.position();old=self.state()
            if allow_submit is not None and not allow_submit():
                self.db.execute('ROLLBACK');return dict(base,status='STOPPED')
            if not old or not old['latched']:
                episode=uuid.uuid4().hex[:16]
                self.db.execute('INSERT OR REPLACE INTO risk_state VALUES (?,?,?,?,?,?,?,?)',
                    (self.scope,self.symbol,1,episode,threshold,price,details['average_entry'],0))
                old=self.state()
            base.update(latched=True,episode=old['episode'])
            if any(row['status']=='UNKNOWN' for row in fresh['pending']):
                self.db.execute('COMMIT')
                return dict(base,status='BLOCKED_UNKNOWN',message='접수 불명 주문을 먼저 확인하세요. 중복 매도를 차단했습니다.')
            if any(row['side']=='sell' for row in fresh['pending']):
                self.db.execute('COMMIT')
                return dict(base,status='WAIT_EXISTING_SELL',message='기존 미체결 매도의 체결·취소 확인이 필요합니다. 손절 매도를 중복 전송하지 않습니다.')
            if fresh['quantity']<=0:
                self.db.execute('COMMIT');return dict(base,status='COMPLETE')
            sequence=old['sequence']+1
            run_id=self.scope+':risk:'+old['episode']+':'+str(sequence)
            strategy=dict(id='stop-'+old['episode']+'-'+str(sequence),symbol=self.symbol,side='sell',
                quantity=fresh['quantity'],limit_price=0,trigger_price=0,max_notional=0,order_type='market',
                mode='risk_stop',episode=old['episode'],entry_price=old['entry_price'],stop_price=old['stop_price'],trigger_price_observed=price)
            self.db.execute('INSERT INTO orders(run_id,scope,day,symbol,quantity,strategy,status) VALUES (?,?,?,?,?,?,?)',
                (run_id,self.scope,now.strftime('%Y%m%d'),self.symbol,fresh['quantity'],json.dumps(strategy),'UNKNOWN'))
            self.db.execute('UPDATE risk_state SET sequence=? WHERE scope=? AND symbol=?',(sequence,self.scope,self.symbol))
            self.db.execute('COMMIT')
        except Exception:
            self.db.execute('ROLLBACK');raise
        number,branch=self.client.order(strategy)
        self.db.execute('UPDATE orders SET status=?,order_no=?,branch=? WHERE run_id=?',('SUBMITTED',number,branch,run_id))
        return dict(base,status='SUBMITTED',submitted=True,order_no=number,
            message='손절 시장가 매도 전송. 매수 잔량이 있으면 한투에서 취소를 확인하세요.' if any(r['side']=='buy' for r in fresh['pending']) else '손절 시장가 매도 전송')
