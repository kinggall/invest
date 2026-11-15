"""invest: standalone KIS demo trader; never issues or writes shared tokens."""
import argparse
from datetime import datetime, timezone, timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
import urllib.request
import urllib.error
import ssl
import urllib.parse

BASE = 'https://openapivts.koreainvestment.com:29443'
QUOTE = '/uapi/domestic-stock/v1/quotations/inquire-price'
ORDER = '/uapi/domestic-stock/v1/trading/order-cash'
FILLS = '/uapi/domestic-stock/v1/trading/inquire-daily-ccld'
KST = timezone(timedelta(hours=9))


class TradingError(Exception):
    pass


class RequestFailure(TradingError):
    """Only internally controlled stage names and validated KIS codes."""
    def __init__(self, stage, status=None, code=''):
        self.stage = stage if stage in ('HTTP', 'TLS', 'TIMEOUT', 'CONNECTION', 'RESPONSE') else 'RESPONSE'
        self.status = status if isinstance(status, int) and 100 <= status <= 599 else None
        self.code = safe_code({'msg_cd': code})
        super().__init__(self.stage + (' HTTP=' + str(self.status) if self.status else '') + (' CODE=' + self.code if self.code else ''))


def safe_code(data):
    if isinstance(data, dict):
        for key in ('msg_cd', 'error_code'):
            value = data.get(key)
            if isinstance(value, str) and re.fullmatch(r'[A-Z]{2,8}[0-9]{3,8}', value):
                return value
    return ''


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def transport(path, headers, payload, post):
    url = BASE + path
    body = None
    if post:
        body = json.dumps(payload).encode()
    else:
        url += '?' + urllib.parse.urlencode(payload)
    try:
        request = urllib.request.Request(url, headers=headers, data=body)
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=15) as response:
            raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise TradingError('RESPONSE_TOO_LARGE')
            data = json.loads(raw)
        if not isinstance(data, dict):
            raise TradingError('RESPONSE_INVALID')
        return data
    except urllib.error.HTTPError as exc:
        code = ''
        try:
            code = safe_code(json.loads(exc.read(65536)))
        except Exception:
            pass
        finally:
            exc.close()
        raise RequestFailure('HTTP', exc.code, code) from None
    except urllib.error.URLError as exc:
        stage = 'TLS' if isinstance(exc.reason, ssl.SSLCertVerificationError) else ('TIMEOUT' if isinstance(exc.reason, TimeoutError) else 'CONNECTION')
        raise RequestFailure(stage) from None
    except TimeoutError:
        raise RequestFailure('TIMEOUT') from None
    except TradingError:
        raise RequestFailure('RESPONSE') from None
    except Exception:
        raise RequestFailure('RESPONSE') from None


def integer(value, label, minimum=1):
    if isinstance(value, bool) or not re.fullmatch(r'\d+', str(value)) or int(value) < minimum:
        raise TradingError('INVALID_' + label)
    return int(value)


class SharedToken:
    def __init__(self, home):
        self.home = Path(home)

    def read(self):
        try:
            config = json.loads((self.home / 'kis_config.json').read_text(encoding='utf-8-sig'))
            profile = config['profiles']['demo']
            key, secret = profile['app_key'], profile['app_secret']
            if os.environ.get('KIS_ENV') == 'demo':
                pair = (os.environ.get('KIS_APP_KEY'), os.environ.get('KIS_APP_SECRET'))
                if any(pair):
                    key, secret = pair
            if not all(isinstance(v, str) and re.fullmatch(r'[!-~]+', v) for v in (key, secret)):
                raise ValueError()
            binding = hashlib.sha256(('demo\0' + key + '\0' + secret).encode()).hexdigest()
            cached = json.loads((self.home / 'kis_token_demo.json').read_text(encoding='utf-8'))
            expiry = float(cached['expires'])
            token = cached['token']
            if cached['binding'] != binding or not math.isfinite(expiry) or expiry <= time.time() + 120:
                raise ValueError()
            if not isinstance(token, str) or not re.fullmatch(r'[!-~]+', token):
                raise ValueError()
            return key, secret, token
        except Exception:
            raise TradingError('SHARED_DEMO_TOKEN_UNAVAILABLE: 기존 domino에서 demo 토큰을 갱신하세요.') from None


class DemoClient:
    def __init__(self, home, cano, product, send=transport, interval=0.6):
        if not re.fullmatch(r'\d{8}', cano) or not re.fullmatch(r'\d{2}', product):
            raise TradingError('INVALID_DEMO_ACCOUNT')
        self.tokens = SharedToken(home)
        self.account = {'CANO': cano, 'ACNT_PRDT_CD': product}
        self.send, self.interval, self.last = send, interval, 0

    def request(self, path, tr_id, payload, post=False, continuation=False):
        allowed = {'/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice': {'FHKST03010100'},
                   '/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice': {'FHKST03010200'},
                   '/uapi/domestic-stock/v1/quotations/inquire-investor-time-by-market': {'FHPTJ04030000'}, QUOTE: {'FHKST01010100'}, ORDER: {'VTTC0011U', 'VTTC0012U'}, FILLS: {'VTTC0081R'}}
        if tr_id not in allowed.get(path, set()) or post != (path == ORDER):
            raise TradingError('ROUTE_BLOCKED')
        key, secret, token = self.tokens.read()  # Re-read before every call, never renew.
        headers = {'Content-Type': 'application/json; charset=utf-8', 'authorization': 'Bearer ' + token,
                   'appkey': key, 'appsecret': secret, 'tr_id': tr_id, 'custtype': 'P'}
        if continuation:
            headers['tr_cont'] = 'N'
        time.sleep(max(0, self.interval - (time.monotonic() - self.last)))
        try:
            data = self.send(path, headers, payload, post)
        except RequestFailure as exc:
            prefix = 'REQUEST_UNCERTAIN' if post else 'QUERY_FAILED'
            raise TradingError(prefix + ' / ' + str(exc)) from None
        except Exception:
            raise TradingError('REQUEST_UNCERTAIN' if post else 'QUERY_FAILED') from None
        finally:
            self.last = time.monotonic()
        if not isinstance(data, dict) or str(data.get('rt_cd')) != '0':
            # Even a negative response is not automatically safe to resubmit.
            prefix = 'ORDER_REQUIRES_RECONCILIATION' if post else 'KIS_QUERY_REJECTED'
            code = safe_code(data)
            raise TradingError(prefix + (' / CODE=' + code if code else ''))
        return data

    def quote(self, symbol):
        output = self.request(QUOTE, 'FHKST01010100', {'FID_COND_MRKT_DIV_CODE': 'J', 'FID_INPUT_ISCD': symbol})['output']
        if output.get('stck_shrn_iscd') != symbol:
            raise TradingError('QUOTE_SYMBOL_MISMATCH')
        return integer(output.get('stck_prpr'), 'QUOTE')

    def order(self, strategy):
        payload = dict(self.account, PDNO=strategy['symbol'], ORD_DVSN='00', ORD_QTY=str(strategy['quantity']),
                       ORD_UNPR=str(strategy['limit_price']), EXCG_ID_DVSN_CD='KRX',
                       SLL_TYPE='01' if strategy['side'] == 'sell' else '', CNDT_PRIC='')
        result = self.request(ORDER, 'VTTC0012U' if strategy['side'] == 'buy' else 'VTTC0011U', payload, True)['output']
        if not re.fullmatch(r'\d+', str(result.get('ODNO', ''))):
            raise TradingError('ORDER_RESPONSE_UNCERTAIN')
        return str(result['ODNO']), str(result.get('KRX_FWDG_ORD_ORGNO', ''))

    def fills(self, row):
        params = dict(self.account, INQR_STRT_DT=row['day'], INQR_END_DT=row['day'], SLL_BUY_DVSN_CD='00',
                      PDNO=row['symbol'], CCLD_DVSN='00', INQR_DVSN='00', INQR_DVSN_3='00',
                      ORD_GNO_BRNO=row['branch'], ODNO=row['order_no'], INQR_DVSN_1='',
                      CTX_AREA_FK100='', CTX_AREA_NK100='')
        seen = set()
        for _ in range(100):
            data = self.request(FILLS, 'VTTC0081R', params, continuation=bool(seen))
            for item in data.get('output1', []):
                if (str(item.get('odno', '')).lstrip('0') == row['order_no'].lstrip('0')
                        and item.get('pdno') == row['symbol'] and item.get('ord_dt') == row['day']):
                    strategy = json.loads(row['strategy'])
                    if (integer(item.get('ord_qty'), 'ORDER_QTY') != row['quantity']
                            or item.get('sll_buy_dvsn_cd') != ('02' if strategy['side'] == 'buy' else '01')
                            or integer(item.get('ord_unpr'), 'ORDER_PRICE') != strategy['limit_price']):
                        raise TradingError('ORDER_IDENTITY_MISMATCH')
                    filled = integer(item.get('tot_ccld_qty'), 'FILLED', 0)
                    remaining = integer(item.get('rmn_qty'), 'REMAINING', 0)
                    if filled > row['quantity'] or filled + remaining > row['quantity']:
                        raise TradingError('FILL_QUANTITY_MISMATCH')
                    status = 'FILLED' if filled == row['quantity'] else ('CANCELLED' if item.get('cncl_yn') == 'Y' else ('PARTIAL' if filled else 'SUBMITTED'))
                    return status, filled
            cursor = (data.get('ctx_area_fk100', '').strip(), data.get('ctx_area_nk100', '').strip())
            if not any(cursor):
                return None
            if cursor in seen:
                raise TradingError('PAGINATION_STALLED')
            seen.add(cursor)
            params.update(CTX_AREA_FK100=cursor[0], CTX_AREA_NK100=cursor[1])
        raise TradingError('PAGINATION_LIMIT')


def validate_strategy(value):
    if value.get('side') not in ('buy', 'sell') or not re.fullmatch(r'\d{6}', str(value.get('symbol', ''))):
        raise TradingError('INVALID_STRATEGY')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', str(value.get('id', ''))):
        raise TradingError('INVALID_STRATEGY_ID')
    for field in ('quantity', 'limit_price', 'trigger_price', 'max_notional'):
        value[field] = integer(value.get(field), field.upper())
    if value['quantity'] * value['limit_price'] > value['max_notional']:
        raise TradingError('MAX_NOTIONAL_EXCEEDED')
    return value


class Engine:
    def __init__(self, database, client):
        path = Path(database)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10, isolation_level=None)
        path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.client = client
        self.scope = hashlib.sha256(json.dumps(client.account, sort_keys=True).encode()).hexdigest()
        self.db.execute('''CREATE TABLE IF NOT EXISTS orders (
            run_id TEXT PRIMARY KEY, scope TEXT, day TEXT, symbol TEXT, quantity INTEGER,
            strategy TEXT, status TEXT, order_no TEXT DEFAULT '', branch TEXT DEFAULT '', filled INTEGER DEFAULT 0)''')

    def run(self, strategy, execute=False, day=None):
        strategy = validate_strategy(dict(strategy))
        day = day or datetime.now(KST).strftime('%Y%m%d')
        run_id = self.scope + ':' + day + ':' + strategy['id']
        existing = self.db.execute('SELECT * FROM orders WHERE run_id=?', (run_id,)).fetchone()
        if existing:
            return {'status': existing['status'], 'run_id': run_id}
        price = self.client.quote(strategy['symbol'])
        triggered = price <= strategy['trigger_price'] if strategy['side'] == 'buy' else price >= strategy['trigger_price']
        if not triggered or not execute:
            return {'status': 'READY' if triggered else 'WAITING', 'price': price, 'submitted': False}
        # Commit intent before the network call. A crash or timeout never permits blind resubmission.
        try:
            self.db.execute('INSERT INTO orders(run_id,scope,day,symbol,quantity,strategy,status) VALUES (?,?,?,?,?,?,?)',
                            (run_id, self.scope, day, strategy['symbol'], strategy['quantity'], json.dumps(strategy), 'UNKNOWN'))
        except sqlite3.IntegrityError:
            return {'status': 'ALREADY_RESERVED'}
        number, branch = self.client.order(strategy)
        self.db.execute('UPDATE orders SET status=?,order_no=?,branch=? WHERE run_id=?', ('SUBMITTED', number, branch, run_id))
        return {'status': 'SUBMITTED', 'order_no': number}

    def sync(self):
        results = []
        for row in self.db.execute("SELECT * FROM orders WHERE scope=? AND status IN ('SUBMITTED','PARTIAL','UNKNOWN')", (self.scope,)).fetchall():
            if not row['order_no']:
                results.append({'status': 'UNKNOWN', 'action': '한투 주문내역 확인 후 reconcile 명령으로 주문번호 연결'})
                continue
            result = self.client.fills(row)
            if result:
                status, filled = result
                if filled < row['filled']:
                    raise TradingError('FILL_REGRESSION')
                self.db.execute('UPDATE orders SET status=?,filled=? WHERE run_id=?', (status, filled, row['run_id']))
                results.append({'order_no': row['order_no'], 'status': status, 'filled': filled})
            else:
                results.append({'order_no': row['order_no'], 'status': 'NOT_FOUND'})
        return results


def main():
    parser = argparse.ArgumentParser(description='한투 국내주식 모의투자 전용')
    parser.add_argument('--domino-home', required=True, type=Path)
    parser.add_argument('--state', type=Path, default=Path('runtime/orders.sqlite3'))
    sub = parser.add_subparsers(dest='command', required=True)
    run = sub.add_parser('run')
    run.add_argument('strategy', type=Path)
    run.add_argument('--execute-demo', action='store_true')
    sub.add_parser('sync')
    sub.add_parser('diagnose')
    rec = sub.add_parser('reconcile')
    rec.add_argument('--strategy-id', required=True)
    rec.add_argument('--day', required=True)
    rec.add_argument('--order-no', required=True)
    rec.add_argument('--branch', required=True)
    args = parser.parse_args()
    try:
        client = DemoClient(args.domino_home, os.environ.get('KIS_DEMO_CANO', ''), os.environ.get('KIS_DEMO_PRODUCT', '01'))
        if args.command == 'diagnose':
            client.quote('005930')
            print('모의 시세 조회 성공')
            day = datetime.now(KST).strftime('%Y%m%d')
            client.request(FILLS, 'VTTC0081R', dict(client.account, INQR_STRT_DT=day,
                INQR_END_DT=day, SLL_BUY_DVSN_CD='00', PDNO='', CCLD_DVSN='00',
                INQR_DVSN='00', INQR_DVSN_3='00', ORD_GNO_BRNO='', ODNO='',
                INQR_DVSN_1='', CTX_AREA_FK100='', CTX_AREA_NK100=''))
            print('모의 주문내역 조회 성공 · 주문 전송 및 토큰 발급 없음')
            return 0
        engine = Engine(args.state, client)
        if args.command == 'run':
            result = engine.run(json.loads(args.strategy.read_text(encoding='utf-8-sig')), args.execute_demo)
        elif args.command == 'sync':
            result = engine.sync()
        else:
            integer(args.order_no, 'ORDER_NO')
            run_id = engine.scope + ':' + args.day + ':' + args.strategy_id
            row = engine.db.execute("SELECT * FROM orders WHERE run_id=? AND status='UNKNOWN'", (run_id,)).fetchone()
            if row is None:
                raise TradingError('UNKNOWN_ORDER_NOT_FOUND')
            candidate = dict(row, order_no=args.order_no, branch=args.branch)
            if client.fills(candidate) is None:
                raise TradingError('ORDER_NOT_VERIFIED')
            engine.db.execute('UPDATE orders SET order_no=?,branch=?,status=? WHERE run_id=?', (args.order_no, args.branch, 'SUBMITTED', run_id))
            result = engine.sync()
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except TradingError as exc:
        print(str(exc))
        return 1
    except Exception:
        print('LOCAL_CONFIG_OR_DATA_ERROR')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
