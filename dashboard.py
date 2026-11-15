"""Loopback-only demo trading dashboard. Credentials never reach the browser."""
import argparse
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

from trader import DemoClient, SharedToken, TradingError, KST
from macd_strategy import MacdEngine, DEFAULT, validate, SYMBOL
from signals import macd


class Dashboard:
    def __init__(self, home, state):
        self.home, self.state = Path(home), Path(state)
        self.state.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.enabled = False
        self.shutdown = threading.Event()
        self.last_message = '중지됨'
        self.last_check = None
        self.snapshot = None
        self.history = None
        self.last_refresh = 0
        self.csrf = secrets.token_urlsafe(32)
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, value TEXT)')
            db.execute('INSERT OR IGNORE INTO settings VALUES (1, ?)', (json.dumps(DEFAULT),))
        with self.db() as db:
            previous = json.loads(db.execute('SELECT value FROM settings WHERE id=1').fetchone()[0])
            if previous.get('mode') != 'macd':
                db.execute('INSERT OR IGNORE INTO settings VALUES (2, ?)', (json.dumps(previous),))
                db.execute('UPDATE settings SET value=? WHERE id=1', (json.dumps(DEFAULT),))
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

    def status(self):
        with self.lock:
            try:
                SharedToken(self.home).read()
                token = '연결 준비됨'
            except TradingError:
                token = '기존 domino에서 모의 토큰 갱신 필요'
            account = bool(__import__('re').fullmatch(r'\d{8}', os.environ.get('KIS_DEMO_CANO','')))
            orders = []
            signals = []
            try:
                engine = self.engine()
                try:
                    orders = [dict(row) for row in engine.db.execute('SELECT day,symbol,quantity,status,order_no,filled,strategy FROM orders WHERE scope=? ORDER BY rowid DESC LIMIT 100', (engine.scope,))]
                    signals = [dict(row) for row in engine.db.execute('SELECT bar_time,side,status,value FROM signals WHERE scope=? ORDER BY bar_time DESC LIMIT 100', (engine.scope,))]
                finally:
                    engine.db.close()
            except TradingError:
                pass
            return dict(enabled=self.enabled, token=token, account_ready=account, strategy=self.strategy(), orders=orders, signals=signals, snapshot=self.snapshot, history=self.history,
                        last_message=self.last_message, last_check=self.last_check, csrf=self.csrf)

    def tick(self, execute):
        engine = self.engine()
        try:
            result = engine.evaluate(self.strategy(), execute)
            self.snapshot = result
            self.last_refresh = time.monotonic()
            self.last_check = datetime.now(KST).isoformat(timespec='seconds')
            self.last_message = json.dumps({key:result[key] for key in ('status','bars','required_bars','position','pending','submitted') if key in result}, ensure_ascii=False)
            return result
        finally:
            engine.db.close()

    def action(self, command, value):
        with self.lock:
            if command == 'strategy':
                if self.enabled:
                    raise TradingError('먼저 자동 실행을 중지하세요.')
                strategy = validate(value)
                with self.db() as db:
                    db.execute('UPDATE settings SET value=? WHERE id=1', (json.dumps(strategy),))
                self.last_message = '전략 저장됨'
            elif command == 'daily':
                cutoff=value.get('date')
                cfg=validate(self.strategy())
                engine=self.engine()
                try:
                    bars=engine.market.daily(SYMBOL,cutoff)
                    self.history=dict(cutoff=cutoff,last_date=bars[-1]['time'],symbol=SYMBOL,
                        chart=macd(bars,cfg['fast'],cfg['slow'],cfg['signal']),adjusted=True,
                        periods=[cfg['fast'],cfg['slow'],cfg['signal']],observed_at=datetime.now(KST).isoformat())
                finally:
                    engine.db.close()
            elif command == 'preview':
                self.tick(False)
            elif command == 'start':
                SharedToken(self.home).read()
                self.client()
                validate(self.strategy())
                self.enabled = True
                self.last_message = '자동 실행 시작 · 확정 3분봉 MACD 신호 평가 / 코스피 수급 60초 조회'
            elif command == 'stop':
                self.enabled = False
                self.last_message = '자동 실행 중지 · 기존 미체결 주문은 유지됩니다.'
            elif command == 'sync':
                engine = self.engine()
                try:
                    self.last_message = json.dumps(engine.sync(), ensure_ascii=False)
                    self.last_check = datetime.now(KST).isoformat(timespec='seconds')
                finally:
                    engine.db.close()
            else:
                raise TradingError('알 수 없는 요청')

    def worker(self):
        while not self.shutdown.wait(10):
            with self.lock:
                if self.enabled or time.monotonic()-self.last_refresh >= 60:
                    try:
                        self.tick(self.enabled)
                    except TradingError as exc:
                        self.last_refresh = time.monotonic()
                        self.enabled = False
                        self.last_message = str(exc) + ' · 자동 실행 중지'
                    except Exception:
                        self.last_refresh = time.monotonic()
                        self.enabled = False
                        self.last_message = '처리 오류 · 자동 실행 중지'


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
