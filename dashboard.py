"""Loopback-only demo trading dashboard. Credentials never reach the browser."""
import argparse
import hashlib
from contextlib import contextmanager
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time
import webbrowser

from trader import DemoClient, SharedToken, TradingError, KST, is_rate_limit
from macd_strategy import MacdEngine, DEFAULT, validate, SYMBOL
from instruments import PREVIOUS_SYMBOL
from confirmation import assess
from futures_chart import FuturesChart, session_at
from wall_paper import WallPaper
from signals import macd
from history import History
from backtest import simulate, STRATEGIES
from oi import OIService, settings as oi_settings


API_VERSION = '2026-11-rate1'


def source_fingerprint():
    root=Path(__file__).resolve().parent
    digest=hashlib.sha256()
    for name in ('dashboard.py','signals.py','history.py','market.py','macd_strategy.py','backtest.py','trader.py','risk.py','oi.py','instruments.py','confirmation.py','futures_chart.py','wall_paper.py'):
        digest.update((root/name).read_bytes())
    return digest.hexdigest()


class Dashboard:
    def __init__(self, home, state):
        self.home, self.state = Path(home), Path(state)
        self.source_fingerprint=source_fingerprint()
        self.state.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.io_lock = threading.Lock()
        self.oi_lock=threading.Lock()
        self.oi_active=False
        self.oi_snapshot=None
        self.oi_job=dict(state='idle',message='OI 조회를 누르면 실제 수신 자료와 벽 후보를 표시합니다.')
        self.last_oi=0
        self.oi_thread=None
        self.future_snapshot=None
        self.future_job=dict(state='idle',message='선물 주간/야간 실제 분봉 조회 대기')
        self.last_future=0
        self.future_thread=None
        self.wall_enabled=False
        self.wall_exit_requested=False
        self.wall_message='가상 시뮬레이션 중지 · 주문전송 없음'
        self.enabled = False
        self.risk_active = False
        self.risk_snapshot = dict(status='DISARMED',message='손절 감시 중지')
        self.shutdown = threading.Event()
        self.last_message = '중지됨'
        self.last_check = None
        self.snapshot = None
        self.history = None
        self.backtest = None
        self.lookup_job = dict(state='idle',command=None,message='조회할 거래일을 선택하세요.')
        self.lookup_thread = None
        self.last_refresh = 0
        self.retry_after=0
        self.csrf = secrets.token_urlsafe(32)
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, value TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS confirmation_samples (id TEXT PRIMARY KEY, stamp TEXT, payload TEXT)')
            db.execute('INSERT OR IGNORE INTO settings VALUES (1, ?)', (json.dumps(DEFAULT),))
        with self.db() as db:
            previous = json.loads(db.execute('SELECT value FROM settings WHERE id=1').fetchone()[0])
            if previous.get('mode') != 'macd' or previous.get('symbol') != SYMBOL:
                backup_id=4 if previous.get('symbol')==PREVIOUS_SYMBOL else 2
                db.execute('INSERT OR IGNORE INTO settings VALUES (?, ?)', (backup_id,json.dumps(previous)))
                db.execute('UPDATE settings SET value=? WHERE id=1', (json.dumps(DEFAULT),))
                self.last_message='매매 종목을 KODEX 200(069500)으로 전환했습니다. 이전 설정과 주문 기록은 보존했습니다. 새 설정을 확인하고 저장하세요.'
        with self.db() as db:
            saved=db.execute('SELECT value FROM settings WHERE id=3').fetchone()
        self.oi_config=oi_settings(json.loads(saved[0])) if saved else dict(expiry='',radius_pct=5)
        paper=WallPaper(self.state);paper.close()
        self.state.chmod(0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.state, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def strategy(self):
        with self.db() as db:
            return json.loads(db.execute('SELECT value FROM settings WHERE id=1').fetchone()[0])

    def client(self):
        return DemoClient(self.home, os.environ.get('KIS_DEMO_CANO',''), os.environ.get('KIS_DEMO_PRODUCT','01'))

    def engine(self):
        return MacdEngine(self.state, self.client())

    @staticmethod
    def previous_exposure(engine):
        quantity=0;pending=0
        for row in engine.db.execute('SELECT filled,status,strategy FROM orders WHERE scope=? AND symbol=?',(engine.scope,PREVIOUS_SYMBOL)):
            side=json.loads(row['strategy']).get('side')
            quantity+=row['filled']*(1 if side=='buy' else -1 if side=='sell' else 0)
            pending+=row['status'] in ('UNKNOWN','SUBMITTED','PARTIAL')
        return dict(symbol=PREVIOUS_SYMBOL,quantity=quantity,pending=pending)

    def wall_status(self):
        paper=WallPaper(self.state)
        try:return dict(paper.view(),enabled=self.wall_enabled,exit_requested=self.wall_exit_requested,retry_after_seconds=max(0,int(self.retry_after-time.monotonic()+.999)),message=self.wall_message)
        finally:paper.close()

    def wall_tick(self,snapshot,client):
        if source_fingerprint()!=self.source_fingerprint:
            with self.lock:
                self.wall_enabled=False;self.wall_exit_requested=False
                self.wall_message='코드 갱신 감지 · 가상 시뮬레이션 중지, 서버를 재시작하세요.'
            return
        with self.lock:active=self.wall_enabled;exit_requested=self.wall_exit_requested
        if not active and not exit_requested:return
        quote={}
        now=datetime.now(KST)
        if now.weekday()<5 and (9,0)<=(now.hour,now.minute)<(15,20):
            price=client.quote(SYMBOL)
            quote=dict(price=price,observed_at=datetime.now(KST).isoformat())
        paper=WallPaper(self.state)
        try:
            with self.lock:
                # Stop is checked again after network I/O. A stopped simulator fills nothing.
                if not self.wall_enabled and not self.wall_exit_requested:return
                if source_fingerprint()!=self.source_fingerprint:
                    self.wall_enabled=False;self.wall_exit_requested=False;self.wall_message='코드 갱신 감지 · 서버 재시작 필요';return
                result=paper.step(snapshot,self.oi_snapshot,quote,force_exit=self.wall_exit_requested)
                self.wall_message=result['state']['message']
                if self.wall_exit_requested and not result['quantity']:
                    self.wall_exit_requested=False;self.wall_enabled=False
        finally:paper.close()

    def status(self):
        with self.lock:
            try:
                SharedToken(self.home).read()
                token = '연결 준비됨'
            except TradingError:
                token = '기존 domino에서 모의 토큰 갱신 필요'
            account = bool(__import__('re').fullmatch(r'\d{8}', os.environ.get('KIS_DEMO_CANO','')))
            orders=[];signals=[] # Historical broker ledger is retained, not part of this page.
            return dict(wall_paper=self.wall_status(),future_context=session_at(datetime.now(KST)),future_snapshot=self.future_snapshot,future_job=dict(self.future_job),confirmation=assess(self.snapshot,self.oi_snapshot),api_version=API_VERSION,build_id=self.source_fingerprint[:12],restart_required=source_fingerprint()!=self.source_fingerprint,oi_active=self.oi_active,oi_snapshot=self.oi_snapshot,oi_job=dict(self.oi_job),oi_config=self.oi_config,enabled=self.enabled,risk_active=self.risk_active,risk_snapshot=self.risk_snapshot, token=token, account_ready=account, strategy=self.strategy(), orders=orders, signals=signals, snapshot=self.snapshot, history=self.history, backtest=self.backtest, lookup_job=dict(self.lookup_job),
                        last_message=self.last_message, last_check=self.last_check, csrf=self.csrf)

    def record_confirmation(self):
        with self.lock:
            snapshot=self.snapshot or {};oi=self.oi_snapshot or {}
            value=assess(snapshot,oi)
            chart=snapshot.get('chart') or []
            signature=[SYMBOL,chart[-1].get('end') if chart else None,(snapshot.get('flow') or {}).get('observed_at'),oi.get('observed_at')]
        key=hashlib.sha256(json.dumps(signature).encode()).hexdigest()
        with self.db() as db:
            db.execute('INSERT OR IGNORE INTO confirmation_samples VALUES (?,?,?)',(key,value['observed_at'],json.dumps(value,ensure_ascii=False)))

    def tick(self, execute):
        with self.lock:
            cfg=self.strategy()
        engine = self.engine()
        try:
            result = engine.evaluate(cfg,False,observe_only=True)
            with self.lock:
                self.snapshot = result
                self.last_refresh = time.monotonic()
                self.last_check = datetime.now(KST).isoformat(timespec='seconds')
                self.last_message = json.dumps({key:result[key] for key in ('status','bars','required_bars','position','pending','submitted') if key in result}, ensure_ascii=False)
            self.record_confirmation()
            self.wall_tick(result,engine.client)
            return result
        finally:
            engine.db.close()

    def may_submit(self):
        # Read atomic state without holding a lock across SQLite / network work.
        return False # MACD-only order execution removed from this dashboard.

    def may_risk_submit(self):
        return False # Legacy protective broker orders cannot be armed here.

    def risk_tick(self):
        # Legacy broker-order protection is not an execution path on this page.
        return dict(status='REMOVED')

    def oi_progress(self,message):
        with self.lock:self.oi_job['message']=message

    def run_oi(self,cfg):
        service=None
        try:
            with self.oi_lock:
                service=OIService(self.home,self.state,progress=self.oi_progress)
                snapshot=service.collect(cfg)
                with self.db() as db:
                    db.execute('INSERT OR REPLACE INTO settings(id,value) VALUES (3,?)',(json.dumps(cfg),))
                with self.lock:
                    self.oi_snapshot=snapshot;self.last_oi=time.monotonic()
                    partial=not snapshot.get('future') or not snapshot.get('put_wall') or not snapshot.get('call_wall') or snapshot.get('coverage_complete') is False
                    sampling=snapshot.get('sampling') or {}
                    message=('OI 분할 갱신 · 최근 10분 근방 수신 '+str(sampling['fresh'])+'/'+str(sampling['total'])+'종목' if sampling.get('mode')=='incremental' else 'OI 조회 완료')
                    if partial:message+=' · 일부 수신, 임시 후보'
                    self.oi_job=dict(state='succeeded',message=message+(' · 60초 이내 저장 자료' if snapshot.get('cached') else ' · 관측 자료 저장됨'))
                self.record_confirmation()
        except Exception as exc:
            with self.lock:
                if not is_rate_limit(exc):self.oi_active=False
                self.last_oi=time.monotonic()
                self.oi_job=dict(state='failed',message=self.lookup_error(exc))
        finally:
            if service is not None:service.close()

    def request_oi(self,value,auto=False):
        cfg=oi_settings(value)
        if self.oi_job['state']=='running':raise TradingError('OI 조회가 진행 중입니다. 진행 상태를 확인하세요.')
        if self.oi_active and cfg!=self.oi_config:raise TradingError('OI 기록을 중지한 뒤 만기·범위를 변경하세요.')
        self.oi_config=cfg;self.oi_active=auto or self.oi_active
        self.oi_job=dict(state='running',message='실전 읽기 전용 OI 조회 준비 중…')
        self.oi_thread=threading.Thread(target=self.run_oi,args=(cfg,),daemon=True)
        self.oi_thread.start()

    def run_future(self):
        service=None
        try:
            service=FuturesChart(self.home,self.state,progress=self.future_progress)
            snapshot=service.collect()
            with self.lock:
                self.future_snapshot=snapshot
                self.future_job=dict(state='succeeded',message=('야간' if snapshot['session']['mode']=='night' else '주간')+' 선물 실제 3분봉 조회 완료')
        except Exception as exc:
            with self.lock:self.future_job=dict(state='failed',message=self.lookup_error(exc))
        finally:
            if service is not None:service.close()
            with self.lock:self.last_future=time.monotonic()

    def future_progress(self,message):
        with self.lock:self.future_job['message']=message

    def request_future(self):
        if self.future_job['state']=='running':raise TradingError('선물 차트 조회가 진행 중입니다.')
        self.future_job=dict(state='running',message='주간/야간 세션 확인 중…')
        self.future_thread=threading.Thread(target=self.run_future,daemon=True)
        self.future_thread.start()

    def future_worker(self):
        while not self.shutdown.wait(1):
            context=session_at(datetime.now(KST))
            with self.lock:
                same=self.future_snapshot and self.future_snapshot['session']['id']==context['id']
                due=time.monotonic()-self.last_future>=60
                if self.future_job['state']!='running' and due and (context['scheduled_open'] or not same):self.request_future()

    def oi_worker(self):
        while not self.shutdown.wait(1):
            now=datetime.now(KST)
            session=now.weekday()<5 and (8,45)<=(now.hour,now.minute)<(15,45)
            with self.lock:
                if self.oi_active and session and self.oi_job['state']!='running' and time.monotonic()-self.last_oi>=60:
                    self.request_oi(self.oi_config,True)

    def progress(self, job_id, message):
        with self.lock:
            if self.lookup_job.get('id')==job_id:
                self.lookup_job.update(message=message,updated_at=datetime.now(KST).isoformat(timespec='seconds'))

    @staticmethod
    def lookup_error(exc):
        if isinstance(exc,TradingError):
            code=str(exc)
            if is_rate_limit(exc):
                return '한투 API 호출 한도 초과 [EGW00201] · 최소 30초 대기 후 자동 재시도합니다. 가상 체결은 보류합니다.'
            if code.startswith('SHARED_REAL_TOKEN_UNAVAILABLE'):
                return ('기존 domino의 실전 조회 토큰이 없거나 만료됐거나 키가 일치하지 않습니다. '
                        '동일한 DOMINO_HOME에서 기존 domino_kis.py check --environment real로 연결을 확인하세요. '
                        'invest는 토큰을 새로 발급하지 않습니다. [SHARED_REAL_TOKEN_UNAVAILABLE]')
            return code
        kind=type(exc).__name__
        allowed={'KeyError','TypeError','ValueError','OperationalError','TimeoutError','AttributeError'}
        return '조회 데이터 처리 오류 ['+(kind if kind in allowed else 'INTERNAL_ERROR')+']. 서버 버전과 응답 형식을 확인해야 합니다.'

    def run_lookup(self, job_id, command, value, cfg):
        engine=None
        try:
            with self.io_lock:
                self.progress(job_id,'조회 연결 확인 중…')
                if command=='preview':
                    self.tick(False)
                elif command=='sync':
                    engine=self.engine()
                    result=engine.sync()
                    with self.lock:
                        self.last_message=json.dumps(result,ensure_ascii=False)
                        self.last_check=datetime.now(KST).isoformat(timespec='seconds')
                else:
                    engine=self.engine()
                    loaded=History(engine,progress=lambda message:self.progress(job_id,message)).load(value.get('date'),cfg)
                    with self.lock:
                        self.history=loaded
                    if command=='backtest':
                        self.progress(job_id,'확정 3분봉으로 백테스트 계산 중…')
                        options=dict(initial=value.get('capital',10000000),
                            fee_bps=value.get('fee_bps',1.5),slippage_bps=value.get('slippage_bps',5),
                            tax_bps=value.get('tax_bps',0),allocation=value.get('allocation',100),
                            force_close=value.get('force_close',False),max_buys=cfg['max_daily_buys'],
                            stop_loss_enabled=value.get('stop_loss_enabled',False),stop_loss_pct=value.get('stop_loss_pct',1),stop_halt=value.get('stop_halt',True))
                        selected=value.get('strategy')
                        if selected is None:
                            legacy=value.get('vwap_filter',False)
                            if not isinstance(legacy,bool):raise TradingError('INVALID_VWAP_FILTER')
                            selected='vwap' if legacy else 'baseline'
                        if selected not in STRATEGIES:raise TradingError('INVALID_BACKTEST_STRATEGY')
                        variants={}
                        for number,(key,(label,rules)) in enumerate(STRATEGIES.items(),1):
                            self.progress(job_id,str(number)+'/5 · '+label+' 계산 중…')
                            variants[key]=simulate(loaded,**options,**rules)
                        keys=('reference_price_pnl','total_cost','net_pnl','return_pct','max_drawdown_pct','buy_count','sell_count','round_trips','fees','tax','slippage_cost','vwap_filtered_signals','vwap_unavailable_signals','vwap_exit_unavailable_bars','macd_exits_held','ending_shares','stop_count')
                        without_stop={}
                        if options['stop_loss_enabled']:
                            control_options=dict(options,stop_loss_enabled=False)
                            for key,(label,rules) in STRATEGIES.items():
                                self.progress(job_id,'손절 미적용 대조 · '+label+' 계산 중…')
                                control=simulate(loaded,**control_options,**rules)
                                without_stop[key]={field:control.get(field) for field in keys}
                        comparison={key:{field:run.get(field) for field in keys} for key,run in variants.items()}
                        comparison['selected']=selected
                        result=dict(variants[selected],comparison=comparison,variants=variants,without_stop=without_stop,run_id=job_id)
                        with self.lock:
                            self.backtest=result
                message={'preview':'지표·수급 조회 완료','sync':'체결 조회 완료','backtest':'백테스트 계산 완료','historical':'과거 3분봉 조회 완료'}[command]
                with self.lock:
                    self.lookup_job.update(state='succeeded',message=message,finished_at=datetime.now(KST).isoformat(timespec='seconds'))
        except Exception as exc:
            message=self.lookup_error(exc)
            with self.lock:
                self.lookup_job.update(state='failed',message=message,finished_at=datetime.now(KST).isoformat(timespec='seconds'))
        finally:
            if engine is not None:
                engine.db.close()

    def action(self, command, value):
        with self.lock:
            if command in ('oi_refresh','oi_start'):
                self.request_oi(value,command=='oi_start')
            elif command=='future_refresh':
                self.request_future()
            elif command=='oi_stop':
                self.oi_active=False
                self.oi_job['message']='OI 자동 기록 중지 · 진행 중인 조회는 완료될 수 있습니다.'
            elif command=='wall_start':
                if self.enabled or self.risk_active:raise TradingError('기존 MACD 모의주문과 손절 감시를 중지한 뒤 가상 시뮬레이션을 시작하세요.')
                SharedToken(self.home).read();self.client()
                if not self.oi_active:
                    if self.oi_job['state']=='running':self.oi_active=True
                    else:self.request_oi(self.oi_config,True)
                self.wall_enabled=True;self.wall_exit_requested=False
                self.wall_message='가상 시뮬레이션 시작 · OI/ETF 자료 준비 후 조건 평가 · 주문전송 없음'
                self.last_refresh=0
            elif command=='wall_stop':
                self.wall_enabled=False;self.wall_exit_requested=False
                self.wall_message='시뮬레이션 일시중지 · 가상 보유 유지 · 가상 손절/청산도 중지됨'
            elif command=='wall_reset':
                if self.wall_enabled or self.wall_exit_requested:raise TradingError('시뮬레이션을 먼저 중지하세요.')
                paper=WallPaper(self.state)
                try:paper.reset(value.get('capital',10000000))
                finally:paper.close()
                self.wall_message='새 가상 자금 설정됨 · 이전 거래 기록은 보존됨'
            elif command=='wall_close':
                if self.enabled or self.risk_active:raise TradingError('기존 MACD 모의주문을 먼저 중지하세요.')
                self.wall_exit_requested=True;self.last_refresh=0
                self.wall_message='가상 전량청산 요청 · 다음 유효 장중 시세까지 대기'
            elif command == 'strategy':
                if self.enabled or self.risk_active or self.wall_enabled or self.wall_exit_requested:
                    raise TradingError('먼저 자동 실행·손절 감시·가상 시뮬레이션을 중지하세요.')
                strategy = validate(value)
                with self.db() as db:
                    db.execute('UPDATE settings SET value=? WHERE id=1', (json.dumps(strategy),))
                self.last_message = '전략 저장됨'
            elif command in ('historical','backtest','preview','sync'):
                if self.lookup_job['state']=='running':
                    raise TradingError('조회가 진행 중입니다. 아래 진행 상태를 확인하세요.')
                cfg=validate(self.strategy())
                if command in ('historical','backtest'):
                    self.history=None
                    self.backtest=None
                job_id=secrets.token_hex(8)
                self.lookup_job=dict(id=job_id,state='running',command=command,date=value.get('date'),
                    message='조회 대기 중…',started_at=datetime.now(KST).isoformat(timespec='seconds'))
                self.lookup_thread=threading.Thread(target=self.run_lookup,args=(job_id,command,dict(value),cfg),daemon=True)
                self.lookup_thread.start()
            elif command == 'start':
                raise TradingError('MACD 단독 모의주문은 제거했습니다. OI 가상 시뮬레이션을 사용하세요. [LEGACY_TRADING_REMOVED]')
            elif command == 'stop':
                self.enabled = False
                self.risk_active=False
                self.risk_snapshot=dict(status='DISARMED',message='사용자 중지 · 손절 감시도 중지됨')
                self.last_message = '자동 실행·손절 감시 중지 · 기존 주문과 보유는 유지됩니다.'
            else:
                raise TradingError('알 수 없는 요청')

    def worker(self):
        while not self.shutdown.wait(10):
            with self.lock:
                execute=self.enabled
                wall_due=(self.wall_enabled or self.wall_exit_requested) and datetime.now(KST).weekday()<5 and (9,0)<=(datetime.now(KST).hour,datetime.now(KST).minute)<(15,20)
                due=(execute or wall_due or time.monotonic()-self.last_refresh>=60) and time.monotonic()>=self.retry_after
                lookup_running=self.lookup_job['state']=='running'
            if not due or lookup_running or not self.io_lock.acquire(blocking=False):
                continue
            try:
                self.tick(execute)
            except Exception as exc:
                with self.lock:
                    self.last_refresh=time.monotonic()
                    if is_rate_limit(exc):self.retry_after=time.monotonic()+30
                    self.enabled=False
                    self.last_message=self.lookup_error(exc)+' · 자동 실행 중지'
                    if self.wall_enabled or self.wall_exit_requested:self.wall_message=self.lookup_error(exc)+' · 가상 체결 보류, 다음 조회 재시도'
            finally:
                self.io_lock.release()


def handler_for(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, code, value, html=False):
            raw = value if html else json.dumps(value, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'text/html; charset=utf-8' if html else 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(raw)

        def valid_host(self):
            return self.headers.get('Host') == '127.0.0.1:' + str(self.server.server_port)

        def do_GET(self):
            if not self.valid_host():
                return self.reply(403, {'error':'HOST_BLOCKED'})
            if self.path == '/':
                return self.reply(200, Path(__file__).with_name('dashboard.html').read_bytes(), True)
            if self.path == '/api/status':
                return self.reply(200, app.status())
            self.reply(404, {'error':'NOT_FOUND'})

        def do_POST(self):
            origin = 'http://127.0.0.1:' + str(self.server.server_port)
            if (not self.valid_host() or self.headers.get('Origin') != origin
                    or self.headers.get('X-CSRF-Token') != app.csrf):
                return self.reply(403, {'error':'REQUEST_BLOCKED'})
            try:
                size = int(self.headers.get('Content-Length','0'))
                if size < 0 or size > 10000:
                    return self.reply(413, {'error':'REQUEST_TOO_LARGE'})
                value = json.loads(self.rfile.read(size) or b'{}')
                if not self.path.startswith('/api/'):
                    return self.reply(404, {'error':'NOT_FOUND'})
                app.action(self.path[5:], value)
                self.reply(200, app.status())
            except TradingError as exc:
                self.reply(400, {'error':str(exc)})
            except Exception:
                self.reply(400, {'error':'설정 또는 응답 형식을 확인하세요.'})
    return Handler


def main():
    parser = argparse.ArgumentParser(description='별도 모의투자 관리 페이지')
    parser.add_argument('--domino-home', type=Path, required=True)
    parser.add_argument('--state', type=Path, default=Path('runtime/orders.sqlite3'))
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    app = Dashboard(args.domino_home, args.state)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), handler_for(app))
    threading.Thread(target=app.worker, daemon=True).start()

    threading.Thread(target=app.oi_worker, daemon=True).start()
    threading.Thread(target=app.future_worker, daemon=True).start()
    url = 'http://127.0.0.1:' + str(server.server_port)
    print('모의투자 관리: ' + url, flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.shutdown.set()
        server.server_close()


if __name__ == '__main__':
    main()
