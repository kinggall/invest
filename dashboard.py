from market_session import market_time, market_stamp, order_window, session_info
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

from types import SimpleNamespace
from trader import ReadOnlyHistoryClient, DemoClient, SharedToken, TradingError, KST, is_rate_limit, ReadOnlyDerivativesClient, OI_INDEX, DERIVATIVE_READS
from macd_strategy import MacdEngine, DEFAULT, validate, SYMBOL
from instruments import PREVIOUS_SYMBOL, ETF_FEE_BPS
from confirmation import assess
from futures_chart import FuturesChart, session_at
from wall_paper import WallPaper
from wall_broker import WallBroker
from wall_replay import replay, available_dates, ensure_observations, capture
from signals import macd
from history import History
from monitoring import append as watch_append, ensure as watch_ensure, status as watch_status
from price_archive import archive, latest as archive_latest
from backtest import simulate, STRATEGIES
from oi import OIService, settings as oi_settings


API_VERSION = '2026-11-demo2'


def source_fingerprint():
    root=Path(__file__).resolve().parent
    digest=hashlib.sha256()
    for name in ('dashboard.py','signals.py','history.py','market.py','macd_strategy.py','backtest.py','trader.py','risk.py','oi.py','instruments.py','confirmation.py','futures_chart.py','wall_paper.py','wall_replay.py','wall_broker.py','price_archive.py','monitoring.py','market_session.py'):
        digest.update((root/name).read_bytes())
    return digest.hexdigest()


class Dashboard:
    def __init__(self, home, state):
        self.home, self.state = Path(home), Path(state)
        self.watch_session=secrets.token_hex(12)
        self.watch_error=None
        self.source_fingerprint=source_fingerprint()
        self.state.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.io_lock = threading.Lock()
        self.execution_lock=threading.RLock()
        self.protection_retry_after=0
        self.protection_health=dict(state='STOPPED',last_attempt=None,last_success=None,error=None)
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
        self.replay_result=None
        self.replay_job=dict(state='idle',message='저장 자료가 있는 거래일을 선택하세요.')
        self.replay_thread=None
        self.replay_dates=[]
        self.account_job=dict(state='idle',message='모의계좌 주문·잔고를 읽기 전용으로 다시 확인할 수 있습니다.')
        self.account_thread=None
        self.wall_enabled=False
        self.day_exit_armed=False
        self.wall_exit_requested=False
        self.wall_message='한투 모의매매 중지 · 시작하면 모의계좌로 주문합니다'
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
            watch_ensure(db)
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
        paper=WallPaper(self.state);ensure_observations(paper.db);paper.close()
        self.replay_dates=available_dates(self.state)
        self.state.chmod(0o600)
        with self.db() as db:
            recording=db.execute("SELECT value FROM watch_settings WHERE name='oi_recording'").fetchone()
        self.oi_active=bool(recording and json.loads(recording[0]))
        self.restore_oi()
        self.record_watch('SERVER_START',build=self.source_fingerprint[:12],oi_active=self.oi_active)

    def restore_oi(self):
        # Display persisted distribution without treating it as a new live input.
        with self.db() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='oi_snapshots'").fetchone():return
            expiry=self.oi_config.get('expiry')
            sql='SELECT payload FROM oi_snapshots'+(' WHERE expiry=?' if expiry else '')+' ORDER BY id DESC LIMIT 20'
            rows=db.execute(sql,(expiry,) if expiry else ()).fetchall()
            snapshots=[json.loads(row[0]) for row in rows]
            snapshot=next((s for s in snapshots if s.get('radius_pct')==self.oi_config['radius_pct'] and isinstance(s.get('spot'),(int,float)) and s['spot']>0 and s.get('calls') and s.get('puts')),None)
            if not snapshot:return
            snapshot.update(archived=True,restored=True,cached=False,coverage_complete=False)
            snapshot['history']=OIService.history(SimpleNamespace(db=db),snapshot['expiry'],datetime.fromisoformat(snapshot['observed_at']))
        self.oi_snapshot=snapshot
        self.oi_job=dict(state='idle',message='최근 저장 풋콜월 분포 표시 · '+snapshot['observed_at']+' / 과거 참고 자료, 새 매매 판단에 사용하지 않음')

    def record_watch(self,kind,**fields):
        try:
            watch_append(self.state,kind,dict(session=self.watch_session,**fields))
            self.watch_error=None
        except (OSError,sqlite3.Error):self.watch_error='LOCAL_RECORDING_WRITE_FAILED: 로컬 감시 기록 저장 실패 · 디스크/DB를 확인하세요.'

    def save_recording_flag(self):
        with self.db() as db:db.execute("INSERT OR REPLACE INTO watch_settings VALUES ('oi_recording',?)",(json.dumps(self.oi_active),))

    def watch_worker(self):
        while not self.shutdown.is_set():
            try:
                self.record_watch('HEARTBEAT',trading=self.wall_enabled,exit_requested=self.wall_exit_requested,
                    protection=dict(self.protection_health),oi_active=self.oi_active,oi_job=dict(self.oi_job),
                    oi_at=(self.oi_snapshot or {}).get('observed_at'),last_evaluation=self.last_check,build=self.source_fingerprint[:12])
            except Exception as exc:self.record_demo_error('감시 기록 저장',exc)
            if self.shutdown.wait(30):break

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
        paper=WallBroker(self.state)
        try:
            result=paper.view();now=datetime.now(KST);message=self.wall_message
            open_window=now.weekday()<5 and market_time(now,9,0)<=(now.hour,now.minute)<market_time(now,15,20)
            if (self.wall_enabled or self.wall_exit_requested or self.day_exit_armed) and now.weekday()<5 and (now.hour,now.minute)>=market_time(now,15,19) and (result['quantity'] or result['pending_count']):
                result['state']['status']='CLOSE_DELAYED' if (now.hour,now.minute)<market_time(now,15,20) else 'CLOSE_FAILED'
                message=session_info(now)['deadline']+' 청산 기한 초과 · 한투 모의계좌의 잔여 보유/주문을 확인하세요 / '+self.wall_message
            if source_fingerprint()!=self.source_fingerprint:
                message+=' / 서버 코드가 갱신됐습니다. Ctrl+C로 종료 후 재시작해야 합니다.'
            health=dict(self.protection_health)
            if open_window and result['quantity'] and (self.wall_enabled or self.day_exit_armed):
                last=health.get('last_success') or health.get('last_attempt')
                elapsed=(now-datetime.fromisoformat(last)).total_seconds() if last else None
                if elapsed is not None and elapsed>45 and health['state']!='ERROR':
                    health.update(state='STALE',error='보유 감시가 45초 이상 지연됐습니다. 연결과 최근 오류를 확인하세요.')
            return dict(result,protection=health,order_window_open=open_window,account_job=dict(self.account_job),enabled=self.wall_enabled,day_exit_armed=self.day_exit_armed,exit_requested=self.wall_exit_requested,retry_after_seconds=max(0,int(self.retry_after-time.monotonic()+.999)),message=message)
        finally:paper.close()

    def wall_tick(self,snapshot,client):
        clock=datetime.now(KST)
        if clock.weekday()<5 and (clock.hour,clock.minute)>=market_time(clock,15,15):return
        if source_fingerprint()!=self.source_fingerprint:
            self.record_watch('STRATEGY_BLOCKED',reason='SOURCE_CHANGED')
            with self.lock:
                self.wall_message='코드 갱신 감지 · 신규 진입 차단, 독립 보유 감시·청산 유지 · 서버 재시작 필요'
            return
        with self.lock:active=self.wall_enabled;exit_requested=self.wall_exit_requested
        if not active and not exit_requested:return
        quote={}
        now=datetime.now(KST)
        if now.weekday()<5 and market_time(now,9,0)<=(now.hour,now.minute)<market_time(now,15,20):
            price=client.quote(SYMBOL)
            quote=dict(price=price,observed_at=datetime.now(KST).isoformat())
        paper=WallBroker(self.state,client)
        try:
            with self.lock:
                # Stop is checked again after network I/O. A stopped simulator fills nothing.
                if not self.wall_enabled and not self.wall_exit_requested:return
                if source_fingerprint()!=self.source_fingerprint:
                    self.record_watch('STRATEGY_BLOCKED',reason='SOURCE_CHANGED')
                    self.wall_message='코드 갱신 감지 · 신규 진입 차단, 독립 보유 감시·청산 유지 · 서버 재시작 필요';return
                observed=datetime.now(KST)
                with self.db() as records:
                    capture(records,snapshot,self.oi_snapshot or {},quote,observed,self.wall_exit_requested)
                if quote:self.replay_dates=sorted(set(self.replay_dates+[observed.date().isoformat()]),reverse=True)[:90]
                exit_now=self.wall_exit_requested;oi_now=self.oi_snapshot if not (self.oi_snapshot or {}).get('archived') else {}
            with self.execution_lock:
                self.record_watch('STRATEGY_INPUT',evaluated_at=observed.isoformat(),quote=quote,confirmation=assess(snapshot,oi_now,observed),oi_at=(oi_now or {}).get('observed_at'))
                result=paper.step(snapshot,oi_now,quote,now=observed,force_exit=exit_now,may_submit=self.demo_submission_allowed)
            self.record_watch('STRATEGY_RESULT',evaluated_at=observed.isoformat(),status=result['state']['status'],message=result['state']['message'],quantity=result['quantity'],pending=result.get('pending_count'))
            with self.lock:
                if self.wall_enabled or self.wall_exit_requested:self.wall_message=result['state']['message']
                if self.wall_exit_requested and not result['quantity'] and not result.get('pending_count'):
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
            return dict(market_session=session_info(datetime.now(KST)),recording=watch_status(self.state),recording_error=self.watch_error,price_archive=archive_latest(self.state),replay_result=self.replay_result,replay_job=dict(self.replay_job),replay_dates=self.replay_dates,wall_paper=self.wall_status(),future_context=session_at(datetime.now(KST)),future_snapshot=self.future_snapshot,future_job=dict(self.future_job),confirmation=assess(self.snapshot,self.oi_snapshot),api_version=API_VERSION,build_id=self.source_fingerprint[:12],restart_required=source_fingerprint()!=self.source_fingerprint,oi_active=self.oi_active,oi_snapshot=self.oi_snapshot,oi_job=dict(self.oi_job),oi_config=self.oi_config,enabled=self.enabled,risk_active=self.risk_active,risk_snapshot=self.risk_snapshot, token=token, account_ready=account, strategy=self.strategy(), orders=orders, signals=signals, snapshot=self.snapshot, history=self.history, backtest=self.backtest, lookup_job=dict(self.lookup_job),
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
        if self.wall_enabled or self.day_exit_armed:self.record_watch('STRATEGY_STARTED',last_evaluation=self.last_check)
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
        self.record_watch('OI_STARTED',config=cfg)
        try:
            with self.oi_lock:
                service=OIService(self.home,self.state,progress=self.oi_progress)
                snapshot=service.collect(cfg,force=True)
                with self.db() as db:
                    db.execute('INSERT OR REPLACE INTO settings(id,value) VALUES (3,?)',(json.dumps(cfg),))
                with self.lock:
                    self.oi_snapshot=snapshot;self.last_oi=time.monotonic()
                    if snapshot.get('observed_at'):self.replay_dates=sorted(set(self.replay_dates+[snapshot['observed_at'][:10]]),reverse=True)[:90]
                    partial=not snapshot.get('future') or not snapshot.get('put_wall') or not snapshot.get('call_wall') or snapshot.get('coverage_complete') is False
                    sampling=snapshot.get('sampling') or {}
                    message=('OI 전체 새 수신 · 유효 '+str(sampling['fresh'])+'/'+str(sampling['total'])+'종목' if sampling.get('mode')=='full' else 'OI 조회 완료')
                    if partial:message+=' · 일부 수신, 임시 후보'
                    self.oi_job=dict(state='succeeded',message=message+(' · 60초 이내 저장 자료' if snapshot.get('cached') else ' · 관측 자료 저장됨'))
                self.record_watch('OI_SAVED',observed_at=snapshot.get('observed_at'),expiry=snapshot.get('expiry'),put_wall=snapshot.get('put_wall'),call_wall=snapshot.get('call_wall'),sampling=snapshot.get('sampling'),coverage_complete=snapshot.get('coverage_complete'))
                self.record_confirmation()
        except Exception as exc:
            self.record_watch('OI_ERROR',message=self.lookup_error(exc),config=cfg)
            with self.lock:
                self.oi_snapshot=None
                self.restore_oi()
                self.last_oi=time.monotonic()
                self.oi_job=dict(state='failed',message=self.lookup_error(exc)+' · 자동 기록 요청은 유지, 60초 후 재시도')
        finally:
            if service is not None:service.close()

    def request_oi(self,value,auto=False):
        cfg=oi_settings(value)
        if self.oi_job['state']=='running':raise TradingError('OI 조회가 진행 중입니다. 진행 상태를 확인하세요.')
        if self.oi_active and cfg!=self.oi_config:raise TradingError('OI 기록을 중지한 뒤 만기·범위를 변경하세요.')
        self.oi_config=cfg;self.oi_active=auto or self.oi_active
        with self.db() as db:db.execute('INSERT OR REPLACE INTO settings(id,value) VALUES (3,?)',(json.dumps(cfg),))
        self.save_recording_flag()
        self.oi_job=dict(state='running',message='실전 읽기 전용 OI 조회 준비 중…')
        self.oi_thread=threading.Thread(target=self.run_oi,args=(cfg,),daemon=True)
        self.oi_thread.start()

    def replay_progress(self,message):
        with self.lock:self.replay_job['message']=message

    def run_replay(self,value,cfg):
        try:
            filled=bool(value.get('fetch_prices',False));loaded=None
            if filled:
                self.replay_progress('한투에서 선택일 ETF 전체 분봉을 조회·저장하는 중…')
                # Real quote token only; never construct a demo order client here.
                with self.db() as db:
                    loaded=History(SimpleNamespace(db=db),client=ReadOnlyHistoryClient(self.home),progress=self.replay_progress).load(value.get('date'),cfg)
            result=replay(self.state,value.get('date'),cfg,value.get('capital',10000000),value.get('mode','auto'),self.replay_progress,options=value.get('options'),supplement=filled)
            if loaded:
                result['chart']=loaded['chart']
                result['downloaded_prices']=dict(source=loaded['source'],bars=len(loaded['chart']),missing_bars=loaded['missing_bars'])
                result['warnings'].extend(loaded.get('warnings',[]))
                result['warnings'].append('차트는 한투에서 확보한 분봉입니다. 매매 판단이 가능한 구간과 가격만 보충된 구간은 다릅니다.')
                if loaded['missing_bars']:result['warnings'].append('ETF 3분봉 누락 '+str(len(loaded['missing_bars']))+'개: '+', '.join(loaded['missing_bars'][:8]))
            with self.lock:
                self.replay_result=result;self.replay_job=dict(state='succeeded',message='저장 자료 백테스트/재생 완료'+(' · 필요 자료 동시 충족 0건: 성과 판단 보류' if not result['ready_samples'] else ''))
        except Exception as exc:
            with self.lock:self.replay_job=dict(state='failed',message=self.lookup_error(exc))

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
            session=now.weekday()<5 and market_time(now,8,45)<=(now.hour,now.minute)<market_time(now,15,45)
            with self.lock:
                if self.oi_active and self.oi_job['state']!='running' and (session and time.monotonic()-self.last_oi>=60 or self.oi_snapshot is None and self.last_oi==0):
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
                            fee_bps=value.get('fee_bps',ETF_FEE_BPS),slippage_bps=value.get('slippage_bps',5),
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
        if command in ('wall_start','wall_stop','wall_close','oi_start','oi_stop'):self.record_watch('CONTROL',command=command)
        with self.lock:
            if command=='wall_sync':
                if self.account_job['state']=='running':raise TradingError('모의계좌 조회가 진행 중입니다.')
                self.account_job=dict(state='running',message='한투 모의주문·잔고를 조회 중입니다. 주문은 전송하지 않습니다.')
                self.account_thread=threading.Thread(target=self.run_account_sync,daemon=True)
                self.account_thread.start()
            elif command in ('oi_refresh','oi_start'):
                self.request_oi(value,command=='oi_start')
            elif command=='future_refresh':
                self.request_future()
            elif command=='oi_stop':
                self.oi_active=False
                self.save_recording_flag()
                self.record_watch('OI_STOPPED')
                self.oi_job['message']='OI 자동 기록 중지 · 진행 중인 조회는 완료될 수 있습니다.'
            elif command=='replay':
                if self.replay_job['state']=='running':raise TradingError('저장 자료 재생이 진행 중입니다.')
                cfg=validate(self.strategy());self.replay_result=None
                self.replay_job=dict(state='running',message='저장 자료 재생 준비 중…')
                self.replay_thread=threading.Thread(target=self.run_replay,args=(dict(value),cfg),daemon=True)
                self.replay_thread.start()
            elif command=='wall_start':
                if self.enabled or self.risk_active:raise TradingError('기존 MACD 모의주문과 손절 감시를 중지한 뒤 가상 시뮬레이션을 시작하세요.')
                SharedToken(self.home).read()
                broker=WallBroker(self.state,self.client())
                try:broker.start_check()
                finally:broker.close()
                if not self.oi_active:
                    if self.oi_job['state']=='running':self.oi_active=True
                    else:self.request_oi(self.oi_config,True)
                self.save_recording_flag()
                self.wall_enabled=True;self.day_exit_armed=True;self.wall_exit_requested=False
                self.protection_health.update(state='STARTING',last_attempt=datetime.now(KST).isoformat(),error=None)
                self.wall_message='한투 모의매매 시작 · OI/ETF 자료 준비 후 신호에 따라 모의주문 전송'
                self.last_refresh=0
            elif command=='wall_stop':
                self.wall_enabled=False;self.wall_exit_requested=False
                self.wall_message='신규 진입 중지 · 기존 보유 손절·익절 감시와 당일 전량청산은 유지'
            elif command=='wall_reset':
                if self.wall_enabled or self.wall_exit_requested:raise TradingError('시뮬레이션을 먼저 중지하세요.')
                paper=WallBroker(self.state)
                try:paper.reset(value.get('capital',10000000))
                finally:paper.close()
                self.wall_message='전략 예산 설정됨 · 모의계좌 입금/초기화가 아닌 전략 한도입니다'
            elif command=='wall_close':
                now=datetime.now(KST)
                if now.weekday()>=5 or not market_time(now,9,0)<=(now.hour,now.minute)<market_time(now,15,20):
                    raise TradingError('지금은 모의주문 실행시간('+session_info(now)['open']+'~'+session_info(now)['order_cutoff']+')이 아닙니다. 주문·잔고 다시 조회로 상태를 확인하세요.')
                if self.enabled or self.risk_active:raise TradingError('기존 MACD 모의주문을 먼저 중지하세요.')
                self.wall_exit_requested=True;self.last_refresh=0
                self.wall_message='모의 전량청산 요청 · 미체결 확인 후 다음 유효 장중 시세에 주문'
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

    def record_demo_error(self,stage,exc):
        message=self.lookup_error(exc)
        try:self.record_watch('ERROR',stage=stage,message=message)
        except Exception:pass
        try:
            broker=WallBroker(self.state)
            try:broker.record_error(stage,message)
            finally:broker.close()
        except Exception:pass # Preserve the original failure even if diagnostics cannot be saved.
        return message

    def run_account_sync(self):
        broker=None
        try:
            with self.io_lock,self.execution_lock:
                broker=WallBroker(self.state,self.client())
                broker.sync() # GET-only: no cancellation or new order.
                balance=broker.check_balance()
                view=broker.view()
                message='한투 모의계좌 조회 완료 · 계좌 보유 '+str(balance['quantity'])+'주 / 전략 장부 '+str(view['quantity'])+'주 / 미확인 주문 '+str(view['pending_count'])+'건'
                with self.lock:self.account_job=dict(state='succeeded',message=message)
        except Exception as exc:
            message=self.record_demo_error('수동 주문·잔고 조회',exc)
            with self.lock:self.account_job=dict(state='failed',message=message)
        finally:
            if broker is not None:broker.close()

    def demo_submission_allowed(self):
        now=datetime.now(KST)
        return ((self.wall_enabled or self.wall_exit_requested or (self.day_exit_armed and (now.hour,now.minute)>=market_time(now,15,15))) and now.weekday()<5
                and market_time(now,9,0)<=(now.hour,now.minute)<market_time(now,15,20)
                and source_fingerprint()==self.source_fingerprint)

    def sync_demo(self):
        broker=WallBroker(self.state)
        try:pending=bool(broker.pending())
        finally:broker.close()
        now=datetime.now(KST)
        closing=(self.wall_enabled or self.wall_exit_requested or self.day_exit_armed) and now.weekday()<5 and (now.hour,now.minute)>=market_time(now,15,15)
        if not pending and not closing:return
        broker=WallBroker(self.state,self.client())
        try:
            with self.execution_lock:
                if closing:
                    result=broker.step({}, {}, {},now=now,force_exit=True,protect_only=True,
                        may_submit=self.exit_submission_allowed)
                else:result=broker.sync(now)
            self.wall_message=result['state']['message']
            if self.wall_exit_requested and not result['quantity'] and not result['pending_count']:
                self.wall_exit_requested=False;self.wall_enabled=False
        finally:broker.close()

    def exit_submission_allowed(self):
        now=datetime.now(KST)
        return ((self.wall_enabled or self.wall_exit_requested or self.day_exit_armed) and now.weekday()<5
                and market_time(now,9,0)<=(now.hour,now.minute)<market_time(now,15,20))

    def protection_tick(self):
        now=datetime.now(KST)
        if not (self.wall_enabled or self.wall_exit_requested or self.day_exit_armed):
            self.sync_demo() # Existing orders can still be polled read-only.
            return
        with self.lock:self.protection_health.update(last_attempt=now.isoformat())
        if now.weekday()<5 and market_time(now,9,0)<=(now.hour,now.minute)<market_time(now,15,20):self.record_watch('PROTECTION_STARTED',manual=self.wall_exit_requested)
        if now.weekday()>=5 or not market_time(now,9,0)<=(now.hour,now.minute)<market_time(now,15,15):
            self.sync_demo()
            with self.lock:self.protection_health.update(state='CLOSING' if market_time(now,15,15)<=(now.hour,now.minute)<market_time(now,15,20) else 'CLOSED',error=None)
            return
        local=WallBroker(self.state)
        try:view=local.view()
        finally:local.close()
        if not view['quantity'] and not view['pending_count']:
            with self.lock:self.protection_health.update(state='FLAT',last_success=now.isoformat(),error=None)
            return
        client=self.client();quote={};index={};index_error=None
        if view['quantity']:
            price=client.quote(SYMBOL)
            quote=dict(symbol=SYMBOL,price=price,observed_at=datetime.now(KST).isoformat())
            p=view['state']['position']
            self.record_watch('PROTECTION_QUOTE',quote=quote,quantity=view['quantity'],stop_price=p.get('stop_price'),put_stop=p.get('put_stop'),fixed_put=p.get('put'),fixed_call=p.get('call'))
            if not WallBroker.protection_reason(p,quote,{},datetime.now(KST)) and not self.wall_exit_requested:
                try:
                    reader=ReadOnlyDerivativesClient(self.home)
                    out=reader.request(OI_INDEX,DERIVATIVE_READS[OI_INDEX],dict(FID_COND_MRKT_DIV_CODE='U',FID_INPUT_ISCD='2001')).get('output')
                    if isinstance(out,list):out=out[0] if len(out)==1 else None
                    from oi import number
                    value=number(out.get('bstp_nmix_prpr'),'PROTECTION_INDEX') if isinstance(out,dict) else None
                    if not value or value<=0:raise TradingError('PROTECTION_INDEX_MISSING')
                    index=dict(symbol='2001',price=value,observed_at=datetime.now(KST).isoformat())
                except Exception as exc:index_error=self.record_demo_error('보유 감시 지수 조회 · ETF 손절은 계속',exc)
        broker=WallBroker(self.state,client)
        try:
            with self.execution_lock:
                if view['quantity'] and not WallBroker.quote_valid(quote,datetime.now(KST)):
                    quote=dict(symbol=SYMBOL,price=client.quote(SYMBOL),observed_at=datetime.now(KST).isoformat())
                result=broker.step({}, {}, quote,now=datetime.now(KST),force_exit=self.wall_exit_requested,
                    protect_only=True,index=index,may_submit=self.exit_submission_allowed)
            self.record_watch('PROTECTION_RESULT',quote=quote,index=index,index_error=index_error,status=result['state']['status'],message=result['state']['message'],quantity=result['quantity'],pending=result['pending_count'],oi_at=(self.oi_snapshot or {}).get('observed_at'))
            with self.lock:
                self.wall_message=result['state']['message']
                self.protection_health.update(state='DEGRADED' if index_error else 'RUNNING',last_success=datetime.now(KST).isoformat(),
                    error=index_error,price=quote.get('price'),index=index.get('price'),decision=result['state']['status'])
                if self.wall_exit_requested and not result['quantity'] and not result['pending_count']:
                    self.wall_exit_requested=False;self.wall_enabled=False
        finally:broker.close()

    def protection_worker(self):
        while not self.shutdown.wait(5):
            if time.monotonic()<self.protection_retry_after:continue
            try:self.protection_tick()
            except Exception as exc:
                message=self.record_demo_error('독립 보유 감시·청산',exc)
                self.protection_retry_after=time.monotonic()+30
                with self.lock:
                    self.wall_message=message+' · 보호 감시 재시도 대기'
                    self.protection_health.update(state='ERROR',error=message,last_attempt=datetime.now(KST).isoformat())

    def archive_worker(self):
        # Independent of trading start/stop and indicator locks. The shared lock
        # and persisted check times also coordinate the Windows scheduled task.
        while not self.shutdown.is_set():
            try:archive(self.state,self.home)
            except (OSError,TradingError):pass # A concurrent archive owns the lock.
            except Exception as exc:self.record_demo_error('자동 ETF 분봉 저장',exc)
            if self.shutdown.wait(60):break

    def worker(self):
        while not self.shutdown.wait(10):
            with self.lock:
                execute=self.enabled
                wall_due=(self.wall_enabled or self.wall_exit_requested) and datetime.now(KST).weekday()<5 and order_window(datetime.now(KST))
                due=(execute or wall_due or time.monotonic()-self.last_refresh>=60) and time.monotonic()>=self.retry_after
                lookup_running=self.lookup_job['state']=='running'
            if not due or lookup_running or not self.io_lock.acquire(blocking=False):
                continue
            try:
                self.tick(execute)
            except Exception as exc:
                message=self.record_demo_error('전략 평가·모의주문',exc)
                with self.lock:
                    self.last_refresh=time.monotonic()
                    if is_rate_limit(exc):self.retry_after=time.monotonic()+30
                    self.enabled=False
                    self.last_message=self.lookup_error(exc)+' · 자동 실행 중지'
                    if self.wall_enabled or self.wall_exit_requested:self.wall_message=message+' · 모의주문/체결 확인 보류, 다음 조회 재시도'
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
    threading.Thread(target=app.protection_worker, daemon=True).start()
    threading.Thread(target=app.archive_worker, daemon=True).start()
    threading.Thread(target=app.watch_worker, daemon=True).start()

    with app.db() as records:
        configured=records.execute("SELECT 1 FROM watch_settings WHERE name='oi_recording'").fetchone()
    if not configured:
        app.oi_active=True
        app.save_recording_flag()
        app.record_watch('OI_AUTOSTART')
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
        app.record_watch('SERVER_STOP')
        app.shutdown.set()
        server.server_close()


if __name__ == '__main__':
    main()
