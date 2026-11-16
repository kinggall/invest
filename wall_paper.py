"""Causal local paper ledger for OI rebound/scale-out/breakout. No broker client."""
from datetime import datetime,timedelta
import json
import math
import sqlite3
import uuid
from confirmation import assess,age,numeric
from instruments import TRADE_SYMBOL
from trader import KST,TradingError,integer

RULES=dict(touch_pct=.3,put_stop_pct=.3,etf_stop_pct=1,initial_pct=40,below_vwap_pct=20,add_pct=20,
    target1_box_pct=60,target2_box_pct=80,sell_each_pct=30,breakout_failure_pct=.2,cooldown_minutes=15,max_put_stops=2,
    fee_bps=1.5,slippage_bps=5,tax_bps=0,force_close='15:15',fill_model='신호 확정 후 수집한 ETF REST 현재가 · 불리한 5원 호가/수수료/슬리피지 · 호가 잔량/미체결 미재현')


def fresh(stamp,now,seconds):
    elapsed=age(stamp,now)
    return elapsed is not None and 0<=elapsed<=seconds


def quantity(position):return sum(lot['quantity'] for lot in position['lots']) if position else 0


def average(position):
    q=quantity(position)
    return sum(lot['quantity']*lot['price'] for lot in position['lots'])/q if q else 0


def new_state(capital):
    capital=integer(capital,'PAPER_CAPITAL',100000)
    if capital>1000000000:raise TradingError('가상 자금은 10억원 이하로 설정하세요.')
    return dict(run_id=uuid.uuid4().hex,capital=capital,cash=float(capital),position=None,armed=None,day=None,
        cooldown_until=None,losses={},used_touch=None,last_price=None,fees=0,realized=0,status='STOPPED',message='시뮬레이션 중지 · 실제 주문 없음')


class WallPaper:
    def __init__(self,database):
        self.db=sqlite3.connect(database,timeout=10,isolation_level=None);self.db.row_factory=sqlite3.Row
        self.db.execute('CREATE TABLE IF NOT EXISTS wall_paper_state (id INTEGER PRIMARY KEY,payload TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS wall_paper_events (event_id TEXT PRIMARY KEY,run_id TEXT,stamp TEXT,payload TEXT)')
        self.db.execute('INSERT OR IGNORE INTO wall_paper_state VALUES (1,?)',(json.dumps(new_state(10000000)),))
    def close(self):self.db.close()
    def load(self):return json.loads(self.db.execute('SELECT payload FROM wall_paper_state WHERE id=1').fetchone()[0])
    def save(self,state):self.db.execute('UPDATE wall_paper_state SET payload=? WHERE id=1',(json.dumps(state,ensure_ascii=False),))
    def view(self):
        state=self.load();p=state['position'];q=quantity(p);mark=state['last_price']
        cost=sum(r['quantity']*(r['price']+r['buy_fee_per_share']) for r in p['lots']) if p else 0
        equity=state['cash']+q*mark if mark is not None else (state['cash'] if not q else None)
        events=[json.loads(r[0]) for r in self.db.execute('SELECT payload FROM (SELECT rowid,payload FROM wall_paper_events WHERE run_id=? ORDER BY rowid DESC LIMIT 100) ORDER BY rowid',(state['run_id'],))]
        return dict(state=state,quantity=q,average_entry=average(p),equity=equity,unrealized=q*mark-cost if q and mark else 0,
            net_pnl=equity-state['capital'] if equity is not None else None,events=events,rules=RULES,mode='LOCAL_PAPER_ONLY',symbol=TRADE_SYMBOL)
    def reset(self,capital):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if quantity(self.load()['position']):raise TradingError('가상 보유가 남아 있어 자금을 초기화할 수 없습니다. 시뮬레이션을 재개해 청산하거나 가상 전량청산을 실행하세요.')
            self.save(new_state(capital));self.db.execute('COMMIT')
        except Exception:self.db.execute('ROLLBACK');raise
    def event(self,state,p,reason,side,q,reference,spot,now,confirmation):
        key=state['run_id']+':'+p['id']+':'+reason
        if self.db.execute('SELECT 1 FROM wall_paper_events WHERE event_id=?',(key,)).fetchone():return False
        factor=1+(RULES['slippage_bps']/10000 if side=='buy' else -RULES['slippage_bps']/10000)
        price=(math.ceil(reference*factor/5) if side=='buy' else math.floor(reference*factor/5))*5
        fee=round(q*price*RULES['fee_bps']/10000,2)
        if price<=0 or q<=0:raise TradingError('PAPER_FILL_INVALID')
        if side=='buy':
            if q*price+fee>state['cash']+.001:raise TradingError('PAPER_CASH_LIMIT')
            state['cash']=round(state['cash']-q*price-fee,2)
            p['lots'].append(dict(kind='add' if reason=='BREAKOUT_ADD' else 'base',quantity=q,price=price,buy_fee_per_share=fee/q))
            p['stop_price']=max(p.get('stop_price',0),average(p)*.99)
            realized=0
        else:
            remaining=q;cost=0
            for lot in p['lots']:
                if reason=='BREAKOUT_FAIL' and lot['kind']!='add':continue
                taken=min(remaining,lot['quantity']);lot['quantity']-=taken;remaining-=taken
                cost+=taken*(lot['price']+lot['buy_fee_per_share'])
            if remaining:raise TradingError('PAPER_SELL_EXCEEDS_POSITION')
            realized=round(q*price-fee-cost,2);state['cash']=round(state['cash']+q*price-fee,2)
            state['realized']=round(state['realized']+realized,2)
        state['fees']=round(state['fees']+fee,2)
        event=dict(id=key,time=now.isoformat(),side=side,quantity=q,price=price,reference_price=reference,fee=fee,realized=realized,reason=reason,
            spot=spot,put=p['put'],call=p['call'],target1=p['target1'],target2=p['target2'],put_stop=p['put_stop'],stop_price=p['stop_price'],
            expiry=p['expiry'],rules=dict(RULES),quote_observed_at=state.get('mark_at'),confirmation=confirmation,execution='LOCAL_PAPER_ONLY')
        self.db.execute('INSERT INTO wall_paper_events VALUES (?,?,?,?)',(key,state['run_id'],now.isoformat(),json.dumps(event,ensure_ascii=False)))
        return True
    @staticmethod
    def size(state,price,fraction,position=None):
        execution=math.ceil(price*1.0005/5)*5
        exposure=quantity(position)*price if position else 0
        budget=min(state['capital']*fraction,state['cash'],max(0,state['capital']-exposure))
        return math.floor(budget/(execution*1.00015))
    def step(self,snapshot,oi,quote,now=None,force_exit=False):
        now=now or datetime.now(KST);snapshot=snapshot or {};oi=oi or {};quote=quote or {}
        self.db.execute('BEGIN IMMEDIATE')
        try:
            state=self.load();result=self.advance(state,snapshot,oi,quote,now,force_exit)
            self.save(result);self.db.execute('COMMIT')
        except Exception:self.db.execute('ROLLBACK');raise
        return self.view()
    def advance(self,s,etf,oi,quote,now,force_exit):
        def wait(status,message):s.update(status=status,message=message,updated_at=now.isoformat());return s
        day=now.date().isoformat();p=s['position'];q=quantity(p)
        if s['day']!=day:s.update(day=day,armed=None,used_touch=None,losses={})
        if now.weekday()>=5 or not (9,0)<=(now.hour,now.minute)<(15,20):
            return wait('CLOSED','장외 · 가상 보유 유지, 유효한 장중 시세까지 대기')
        live=(etf.get('symbol')==TRADE_SYMBOL and fresh(etf.get('price_time'),now,120) and numeric(quote.get('price')) and quote['price']>0
            and fresh(quote.get('observed_at'),now,20))
        if not live:return wait('DATA_WAIT','현재 거래일 ETF 분봉과 최신 REST 현재가 확인 필요 · 가상 체결 보류')
        price=quote['price'];s['last_price']=price;s['mark_at']=quote['observed_at']
        spot=oi.get('spot') if fresh(oi.get('observed_at'),now,150) and numeric(oi.get('spot')) and oi['spot']>0 else None
        confirmation=assess(etf,oi,now)
        confirmation['etf_price_time']=etf.get('price_time')
        if q:
            reason=None
            if force_exit:reason='MANUAL_EXIT'
            elif p['entry_day']!=day:reason='OVERNIGHT_EXIT'
            elif (now.hour,now.minute)>=(15,15):reason='DAY_EXIT'
            elif price<=p['stop_price']:reason='PROFIT_PROTECT' if p['protected'] and price>=average(p)*.99 else 'ETF_STOP'
            elif spot is not None and spot<=p['put_stop']:reason='PUT_STOP'
            if reason:
                self.event(s,p,reason,'sell',q,price,spot,now,confirmation)
                if reason in ('ETF_STOP','PUT_STOP'):
                    key=p['expiry']+':'+str(p['put']);s['losses'][key]=s['losses'].get(key,0)+1
                    s['cooldown_until']=(now+timedelta(minutes=15)).isoformat()
                s['position']=None;s['armed']=None
                return wait('EXITED',reason+' · 가상 전량청산')
            add_qty=sum(lot['quantity'] for lot in p['lots'] if lot['kind']=='add')
            if add_qty and spot is not None and spot<=p['call']*.998:
                self.event(s,p,'BREAKOUT_FAIL','sell',add_qty,price,spot,now,confirmation)
                s['cooldown_until']=(now+timedelta(minutes=15)).isoformat()
                return wait('ADD_EXITED','돌파 실패 · 추가매수분만 가상 청산, 최초 보유 잔여분 유지')
            if spot is not None:
                for number in (1,2):
                    flag='tp'+str(number)
                    if not p[flag] and spot>=p['target'+str(number)]:
                        amount=min(quantity(p),math.floor(p['original_quantity']*.3))
                        if amount:self.event(s,p,'TAKE_PROFIT_'+str(number),'sell',amount,price,spot,now,confirmation)
                        p[flag]=True
                        if number==2:p['protected']=True;p['stop_price']=max(p['stop_price'],average(p))
            if not quantity(p):s['position']=None;return wait('EXITED','가상 보유 청산 완료')
            # Separate post-close index observations for two consecutive ETF bars.
            points=etf.get('chart') or [];last=points[-1] if points else {};end=last.get('end')
            bar_ok=fresh(end,now,90) and end!=p.get('breakout_bar') and fresh(oi.get('observed_at'),now,150)
            if bar_ok:
                index_after=age(end,datetime.fromisoformat(oi['observed_at']))
                if index_after is not None and 0<=index_after<=90 and spot is not None:
                    consecutive=age(p.get('breakout_bar'),datetime.fromisoformat(end))==180
                    p['breakout_count']=(p['breakout_count']+1 if consecutive else 1) if spot>p['call'] else 0
                    p['breakout_bar']=end
            cool=s['cooldown_until'] and datetime.fromisoformat(s['cooldown_until'])>now
            macd=confirmation['macd'];vwap=confirmation['vwap'];flow=confirmation['flow']
            if (not p['added'] and p['breakout_count']>=2 and end==p['breakout_bar'] and fresh(end,now,90) and not cool and spot is not None and spot>p['call'] and confirmation['status']!='WAITING'
                and macd.get('above_signal') and vwap.get('above') and flow.get('positive')):
                amount=self.size(s,price,.2,p)
                if amount and fresh(end,datetime.fromisoformat(quote['observed_at']),90):
                    self.event(s,p,'BREAKOUT_ADD','buy',amount,price,spot,now,confirmation);p['added']=True
                    return wait('ADDED','콜월 돌파 2회와 보조조건 확인 · 가상 추가매수 20% 한도')
            return wait('HOLDING','가상 보유 · 익절/손절/돌파 조건 대기')
        if force_exit:return wait('FLAT','가상 보유 없음')
        if (now.hour,now.minute)>=(15,15):return wait('ENTRY_CUTOFF','15:15 이후 신규 진입 중지')
        if s['cooldown_until'] and datetime.fromisoformat(s['cooldown_until'])>now:return wait('COOLDOWN','손절/돌파 실패 후 15분 대기')
        if confirmation['status']=='WAITING':return wait('DATA_WAIT',confirmation['message'])
        walls=confirmation['walls'];put=walls['put'];call=walls['call'];expiry=oi.get('expiry')
        if not isinstance(expiry,str):return wait('DATA_WAIT','옵션 만기 확인 필요')
        key=expiry+':'+str(put)
        if s['losses'].get(key,0)>=2:return wait('PUT_HALTED','같은 풋월에서 당일 손절 2회 · 재진입 중지')
        points=etf.get('chart') or [];last=points[-1] if points else {};previous=points[-2] if len(points)>1 else {}
        armed=s['armed']
        if armed and (armed['put']!=put or armed['call']!=call or armed['expiry']!=expiry):s['armed']=None;armed=None
        if armed:
            if spot is None or spot<=put*.997:s['armed']=None;return wait('ARM_CANCELLED','대기 중 풋월 지지 실패')
            elapsed=age(armed['at'],now)
            if elapsed is None or elapsed>360:s['armed']=None;return wait('ARM_EXPIRED','반등 확인 대기 만료 · 새 접근 필요')
            end=last.get('end');after=age(armed['at'],datetime.fromisoformat(end)) if end else None
            expected=(datetime.fromisoformat(armed['at']).replace(minute=datetime.fromisoformat(armed['at']).minute//3*3,second=0,microsecond=0)+timedelta(minutes=3)).isoformat()
            if end and end>expected:
                s['armed']=None;return wait('ARM_EXPIRED','접근 직후 확인할 확정봉을 놓쳤습니다. 새 접근부터 관측합니다.')
            if after is not None and after>0:
                s['armed']=None
                if not (numeric(previous.get('high')) and numeric(last.get('close')) and last['close']>previous['high'] and confirmation['macd'].get('improving')):
                    return wait('REBOUND_WAIT','접근 후 다음 확정봉의 고가 돌파/히스토그램 개선 미충족')
                if not fresh(end,datetime.fromisoformat(quote['observed_at']),90):return wait('DATA_WAIT','신호 확정 후 가상 체결 시세 확인 필요')
                fraction=.4 if confirmation['vwap'].get('above') else .2;amount=self.size(s,price,fraction)
                if not amount:return wait('NO_BUDGET','가상 자금/정수 수량 부족')
                position=dict(id=uuid.uuid4().hex,entry_day=day,expiry=expiry,put=put,call=call,put_stop=put*.997,
                    target1=put+(call-put)*.6,target2=put+(call-put)*.8,original_quantity=amount,lots=[],tp1=False,tp2=False,added=False,
                    protected=False,stop_price=0,breakout_bar=None,breakout_count=0,armed_at=armed['at'])
                confirmation['entry_reference']=dict(previous_high=previous['high'],bar_end=end,close=last['close'])
                self.event(s,position,'REBOUND_ENTRY','buy',amount,price,spot,now,confirmation);s['position']=position
                return wait('ENTERED','풋월 반등 가상 진입 '+str(int(fraction*100))+'% 한도 · 벽/손절선 고정')
            return wait('ARMED','풋월 접근 관측 · 다음 ETF 확정 3분봉 반등 확인 대기')
        if put<=spot<=put*1.003 and oi.get('observed_at')!=s['used_touch']:
            s['used_touch']=oi['observed_at'];s['armed']=dict(at=now.isoformat(),put=put,call=call,expiry=expiry)
            return wait('ARMED','풋월 위 0.3% 이내 접근 · 다음 확정봉 대기')
        return wait('WATCHING','풋월 위 0.3% 이내 접근 대기')
