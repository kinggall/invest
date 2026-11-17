"""OI strategy execution on KIS DEMO only. Local paper history is never imported."""
from copy import deepcopy
from datetime import datetime, timedelta
import json
import math
from pathlib import Path
import uuid
from wall_paper import WallPaper, RULES, quantity, average
from instruments import TRADE_SYMBOL
from trader import Engine, TradingError, OrderNotSent, KST


class Intent(Exception):
    def __init__(self,state,position,reason,side,amount,reference,spot,now,confirmation):
        self.value=dict(state=deepcopy(state),position=deepcopy(position),reason=reason,side=side,
                        quantity=amount,reference=reference,spot=spot,time=now.isoformat(),confirmation=confirmation)


class WallBroker(WallPaper):
    def __init__(self,database,client=None):
        self.source=Path(database)
        super().__init__(self.source.with_name(self.source.stem+'.demo.sqlite3'))
        self.source.with_name(self.source.stem+'.demo.sqlite3').chmod(0o600)
        self.client=client
        self.may_submit=None
        self.db.execute('CREATE TABLE IF NOT EXISTS broker_meta (id TEXT PRIMARY KEY,payload TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS broker_settings (id TEXT PRIMARY KEY,payload TEXT)')
        # Reuse the durable order ledger and the existing CLI reconciliation path.
        if client is not None:
            engine=Engine(self.source.with_name(self.source.stem+'.demo.sqlite3'),client)
            self.scope=engine.scope;engine.db.close()
            bound=self.db.execute("SELECT payload FROM broker_settings WHERE id='scope'").fetchone()
            if bound and bound[0]!=self.scope:
                if self.db.execute('SELECT 1 FROM orders LIMIT 1').fetchone() or quantity(self.load()['position']):
                    self.close();raise TradingError('DEMO_ACCOUNT_CHANGED: 이전 모의계좌 장부를 확인하세요. 자동 이관하지 않습니다.')
                self.db.execute("UPDATE broker_settings SET payload=? WHERE id='scope'",(self.scope,))
            self.db.execute("INSERT OR IGNORE INTO broker_settings VALUES ('scope',?)",(self.scope,))
        elif not self.db.execute("SELECT 1 FROM sqlite_master WHERE name='orders'").fetchone():
            # Local views can be rendered before account configuration; no network.
            self.db.execute('CREATE TABLE orders (run_id TEXT PRIMARY KEY,scope TEXT,day TEXT,symbol TEXT,quantity INTEGER,strategy TEXT,status TEXT,order_no TEXT,branch TEXT,filled INTEGER DEFAULT 0,filled_cash REAL)')

    def event(self,state,p,reason,side,q,reference,spot,now,confirmation):
        # Stop before the inherited simulator can debit money or fabricate a fill.
        raise Intent(state,p,reason,side,q,reference,spot,now,confirmation)

    def pending(self):
        return self.db.execute("SELECT o.*,m.payload AS meta FROM orders o JOIN broker_meta m ON m.id=o.run_id WHERE o.status NOT IN ('FILLED','CANCELLED') OR json_extract(m.payload,'$.settled') IS NOT 1 ORDER BY o.rowid").fetchall()

    def view(self):
        result=super().view();result['state']['message']=result['state']['message'].replace('가상','모의').replace('실제 주문 없음','한투 모의계좌 주문 대기');result['mode']='KIS_DEMO';result['orders']=[]
        for row in self.db.execute('SELECT o.*,m.payload AS meta FROM orders o JOIN broker_meta m ON m.id=o.run_id ORDER BY o.rowid DESC LIMIT 100'):
            meta=json.loads(row['meta']);strategy=json.loads(row['strategy'])
            result['orders'].append(dict(id=row['run_id'],strategy_id=strategy['id'],day=row['day'],order_no=row['order_no'],status=row['status'],side=strategy['side'],
                quantity=row['quantity'],filled=row['filled'],price=strategy['limit_price'],reason=meta['reason'],time=meta['time'],cancel_requested=bool(meta.get('cancel_requested'))))
        result['pending_count']=len(self.pending())
        row=self.db.execute("SELECT payload FROM broker_settings WHERE id='balance'").fetchone()
        result['account_balance']=json.loads(row[0]) if row else None
        return result

    def reset(self,capital):
        if self.pending():raise TradingError('미체결/접수 확인 필요 주문이 있어 전략 예산을 초기화할 수 없습니다.')
        super().reset(capital)

    def check_balance(self):
        value=self.client.balance(TRADE_SYMBOL)
        if value['quantity']<quantity(self.load()['position']):
            raise TradingError('DEMO_BALANCE_MISMATCH: 전략 보유보다 한투 잔고가 적습니다. 수동 거래/계좌 초기화를 확인하세요.')
        self.db.execute("INSERT OR REPLACE INTO broker_settings VALUES ('balance',?)",(json.dumps(value),))
        return value

    def start_check(self):
        self.client.tokens.read()
        self.check_balance()
        if any(not row['order_no'] for row in self.pending()):
            raise TradingError('접수 확인 필요 주문이 있습니다. 주문번호를 연결한 뒤 재개하세요.')
        if self.source.exists():
            import sqlite3
            db=sqlite3.connect(self.source)
            try:
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='orders'").fetchone():
                    if db.execute("SELECT 1 FROM orders WHERE scope=? AND symbol=? AND status IN ('UNKNOWN','SUBMITTED','PARTIAL') LIMIT 1",(self.scope,TRADE_SYMBOL)).fetchone():
                        raise TradingError('이전 전략의 미체결/접수 확인 필요 모의주문을 먼저 확인하세요.')
            finally:db.close()

    def sync(self,now=None,allow_cancel=False):
        now=now or datetime.now(KST);changed=False
        for row in self.pending():
            meta=json.loads(row['meta'])
            if not row['order_no']:continue
            match=self.client.fills(row)
            if match is None:continue
            status,filled,amount=match
            if filled<row['filled'] or filled>row['quantity']:raise TradingError('DEMO_FILL_REGRESSION')
            if filled and (amount is None or not math.isfinite(amount) or amount<=0):
                raise TradingError('DEMO_FILL_PRICE_UNAVAILABLE: 체결금액 확인 전 추가 주문 보류')
            if filled==row['filled'] and row['filled_cash'] is not None and amount is not None and abs(amount-row['filled_cash'])>.01:
                raise TradingError('DEMO_FILL_AMOUNT_CHANGED')
            if filled>row['filled'] and row['filled_cash'] is not None and amount<=row['filled_cash']:
                raise TradingError('DEMO_FILL_AMOUNT_REGRESSION')
            self.db.execute('BEGIN IMMEDIATE')
            try:
                row=self.db.execute('SELECT o.*,m.payload AS meta FROM orders o JOIN broker_meta m ON m.id=o.run_id WHERE o.run_id=?',(row['run_id'],)).fetchone()
                meta=json.loads(row['meta'])
                if meta.get('settled'):
                    self.db.execute('COMMIT');continue
                if filled<row['filled']:raise TradingError('DEMO_FILL_REGRESSION')
                state=self.load();p=state['position'] or deepcopy(meta['position'])
                applied=int(meta.get('applied_quantity',0));applied_cash=float(meta.get('applied_cash',0))
                if filled<applied:raise TradingError('DEMO_APPLIED_FILL_REGRESSION')
                delta=filled-applied
                if delta:
                    changed=True
                    cash=amount-applied_cash;price=cash/delta
                    if price<=0:raise TradingError('DEMO_FILL_PRICE_INVALID')
                    fee=round(cash*RULES['fee_bps']/10000,2);realized=0
                    if meta['side']=='buy':
                        p['lots'].append(dict(kind='add' if meta['reason']=='BREAKOUT_ADD' else 'base',quantity=delta,price=price,buy_fee_per_share=fee/delta))
                        state['cash']=round(state['cash']-cash-fee,2)
                        p['stop_price']=max(p.get('stop_price',0),average(p)*.99)
                        if meta['reason']=='REBOUND_ENTRY':p['original_quantity']=filled
                    else:
                        if delta>quantity(p):raise TradingError('DEMO_SELL_EXCEEDS_OWNED')
                        left=delta;cost=0
                        for lot in p['lots']:
                            if meta['reason']=='BREAKOUT_FAIL' and lot['kind']!='add':continue
                            take=min(left,lot['quantity']);lot['quantity']-=take;left-=take;cost+=take*(lot['price']+lot['buy_fee_per_share'])
                        if left:raise TradingError('DEMO_ADD_LOT_MISMATCH')
                        realized=round(cash-fee-cost,2);state['cash']=round(state['cash']+cash-fee,2);state['realized']=round(state['realized']+realized,2)
                    state['fees']=round(state['fees']+fee,2);state['position']=p if quantity(p) else None
                    event=dict(id=row['run_id']+':'+str(filled),time=now.isoformat(),side=meta['side'],quantity=delta,price=price,
                        reference_price=meta['reference'],fee=fee,realized=realized,reason=meta['reason'],execution='KIS_DEMO_CONFIRMED',order_no=row['order_no'],fee_estimated=True)
                    self.db.execute('INSERT INTO wall_paper_events VALUES (?,?,?,?)',(event['id'],state['run_id'],now.isoformat(),json.dumps(event)))
                meta['applied_quantity']=filled;meta['applied_cash']=amount or 0
                terminal=status in ('FILLED','CANCELLED')
                if terminal:
                    meta['settled']=True
                    if filled:
                        if meta['reason']=='BREAKOUT_ADD':p['added']=True
                        if meta['reason'].startswith('TAKE_PROFIT_'):
                            number=meta['reason'][-1];p['tp'+number]=True
                            if number=='2':p['protected']=True;p['stop_price']=max(p['stop_price'],average(p))
                        if meta['reason'] in ('ETF_STOP','PUT_STOP','BREAKOUT_FAIL'):
                            state['cooldown_until']=(now+timedelta(minutes=15)).isoformat()
                            if meta['reason']!='BREAKOUT_FAIL':
                                key=p['expiry']+':'+str(p['put']);state['losses'][key]=state['losses'].get(key,0)+1
                    state['armed']=None
                state.update(status=status,message='한투 모의주문 '+status+' · 체결 '+str(filled)+'/'+str(row['quantity']),updated_at=now.isoformat())
                self.save(state)
                self.db.execute('UPDATE orders SET status=?,filled=?,filled_cash=? WHERE run_id=?',(status,filled,amount,row['run_id']))
                self.db.execute('UPDATE broker_meta SET payload=? WHERE id=?',(json.dumps(meta),row['run_id']))
                self.db.execute('COMMIT')
            except Exception:self.db.execute('ROLLBACK');raise
            # A durable cancellation intent prevents duplicate POSTs after a crash.
            closing=(now.hour,now.minute)>=(15,15)
            strategy=json.loads(row['strategy'])
            full_market_exit=(strategy['side']=='sell' and strategy.get('order_type')=='market'
                              and row['quantity']-filled==quantity(self.load()['position']))
            cancel_due=(closing and not full_market_exit) or (not closing and (now-datetime.fromisoformat(meta['time'])).total_seconds()>=30)
            if allow_cancel and not terminal and not meta.get('cancel_requested') and cancel_due:
                self.db.execute('BEGIN IMMEDIATE')
                try:
                    meta=json.loads(self.db.execute('SELECT payload FROM broker_meta WHERE id=?',(row['run_id'],)).fetchone()[0])
                    cancel=not meta.get('cancel_requested') and not meta.get('settled')
                    if cancel:
                        meta['cancel_requested']=True
                        self.db.execute('UPDATE broker_meta SET payload=? WHERE id=?',(json.dumps(meta),row['run_id']))
                    self.db.execute('COMMIT')
                except Exception:self.db.execute('ROLLBACK');raise
                if cancel:
                    self.client.before_order=self.may_submit
                    try:self.client.cancel(row)
                    except OrderNotSent:
                        meta['cancel_requested']=False
                        self.db.execute('UPDATE broker_meta SET payload=? WHERE id=?',(json.dumps(meta),row['run_id']))
                        raise
        if changed:
            balance=self.client.balance(TRADE_SYMBOL)
            self.db.execute("INSERT OR REPLACE INTO broker_settings VALUES ('balance',?)",(json.dumps(balance),))
        return self.view()

    def step(self,snapshot,oi,quote,now=None,force_exit=False,may_submit=None):
        if self.client is None:raise TradingError('DEMO_CLIENT_REQUIRED')
        now=now or datetime.now(KST)
        self.may_submit=may_submit
        self.sync(now,allow_cancel=(now.weekday()<5 and (9,0)<=(now.hour,now.minute)<(15,20) and (may_submit is None or may_submit())))
        regular=now.weekday()<5 and (9,0)<=(now.hour,now.minute)<(15,20)
        closing=now.weekday()<5 and (now.hour,now.minute)>=(15,15)
        pending=self.pending()
        if closing and not regular:
            state=self.load();unresolved=bool(quantity(state['position']) or pending)
            state.update(status='CLOSE_FAILED' if unresolved else 'CLOSED_FLAT',
                message='당일 청산 미완료 · 잔여 보유/미확인 주문을 한투 모의계좌에서 즉시 확인하세요' if unresolved else '당일 청산 확인 완료 · 보유 0 / 미체결 0',updated_at=now.isoformat())
            self.save(state);return self.view()
        if pending:
            blocked=closing and any(not row['order_no'] for row in pending)
            late=closing and (now.hour,now.minute)>=(15,19)
            state=self.load();state.update(status='CLOSE_BLOCKED' if blocked else 'CLOSE_DELAYED' if late else 'ORDER_PENDING',
                message='당일 청산 차단 · 접수 불명 주문을 한투에서 즉시 확인하세요' if blocked else '15:19 청산 기한 초과 · 미체결 즉시 확인 · 잔여 청산 진행' if late else '당일 청산 진행 · 기존 주문 취소/체결 확인 중' if closing else '미체결/접수 확인 필요 주문 대기 · 새 주문 차단');self.save(state)
            return self.view()
        self.db.execute('BEGIN IMMEDIATE')
        intent=None
        try:
            state=self.load()
            try:
                if regular and (closing or force_exit):
                    # Closing must not depend on ETF warm-up, OI, flow or REST quotes.
                    position=state['position'];amount=quantity(position)
                    if amount:
                        reference=state.get('last_price') or average(position)
                        raise Intent(state,position,'DAY_EXIT' if closing else 'MANUAL_EXIT','sell',amount,reference,None,now,{})
                    state.update(armed=None,status='CLOSED_FLAT' if closing else 'FLAT',
                        message='당일 청산 확인 완료 · 보유 0 / 미체결 0' if closing else '보유 없음',updated_at=now.isoformat())
                else:state=self.advance(state,snapshot or {},oi or {},quote or {},now,force_exit)
            except Intent as signal:
                intent=signal.value;state=intent['state']
            self.save(state);self.db.execute('COMMIT')
        except Exception:self.db.execute('ROLLBACK');raise
        if intent is None:return self.view()
        balance=self.check_balance()
        amount=intent['quantity'];reference=intent['reference'];side=intent['side'];reason=intent['reason']
        protective=side=='sell' and reason in ('MANUAL_EXIT','OVERNIGHT_EXIT','DAY_EXIT','ETF_STOP','PUT_STOP','PROFIT_PROTECT','BREAKOUT_FAIL')
        # Aggressive bounded limits for entry/targets; market only for protective exits.
        price=(math.ceil(reference*1.001/5) if side=='buy' else math.floor(reference*.999/5))*5
        if side=='buy':
            power=self.client.buying_power(TRADE_SYMBOL,price)
            fraction=.2 if reason=='BREAKOUT_ADD' or not intent['confirmation']['vwap'].get('above') else .4
            amount=min(amount,power['quantity'],int(min(state['capital']*fraction,state['cash'],power['cash'])/(price*1.00015)))
            if not amount:raise TradingError('DEMO_BUYING_POWER_INSUFFICIENT')
        elif amount>balance['quantity'] or amount>quantity(state['position']):
            raise TradingError('DEMO_SELL_QUANTITY_BLOCKED')
        if may_submit is not None and not may_submit():
            state=self.load();state.update(status='STOPPED',message='사용자 중지 · 모의주문 전송 안 함');self.save(state);return self.view()
        intent['quantity']=amount
        strategy_id=uuid.uuid4().hex
        strategy=dict(id=strategy_id,symbol=TRADE_SYMBOL,side=side,quantity=amount,limit_price=0 if protective else price,
                      order_type='market' if protective else 'limit',mode='risk_stop' if protective else 'oi_demo')
        run_id=self.scope+':'+now.strftime('%Y%m%d')+':'+strategy_id
        intent['settled']=False
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if self.pending():raise TradingError('DEMO_ORDER_ALREADY_PENDING')
            self.db.execute('INSERT INTO orders(run_id,scope,day,symbol,quantity,strategy,status,order_no,branch,filled) VALUES (?,?,?,?,?,?,?,?,?,0)',
                (run_id,self.scope,now.strftime('%Y%m%d'),TRADE_SYMBOL,amount,json.dumps(strategy),'UNKNOWN','',''))
            self.db.execute('INSERT INTO broker_meta VALUES (?,?)',(run_id,json.dumps(intent)))
            self.db.execute('COMMIT')
        except Exception:self.db.execute('ROLLBACK');raise
        self.client.before_order=may_submit
        try:number,branch=self.client.order(strategy)
        except OrderNotSent:
            intent['settled']=True
            self.db.execute('BEGIN IMMEDIATE')
            try:
                self.db.execute("UPDATE orders SET status='CANCELLED' WHERE run_id=?",(run_id,))
                self.db.execute('UPDATE broker_meta SET payload=? WHERE id=?',(json.dumps(intent),run_id))
                self.db.execute('COMMIT')
            except Exception:self.db.execute('ROLLBACK');raise
            raise
        self.db.execute("UPDATE orders SET status='SUBMITTED',order_no=?,branch=? WHERE run_id=?",(number,branch,run_id))
        state=self.load();state.update(status='SUBMITTED',message='한투 모의주문 접수 · 체결 확인 대기',updated_at=now.isoformat());self.save(state)
        return self.view()
