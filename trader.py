"""invest: standalone KIS demo trader; never issues or writes shared tokens."""
from instruments import TRADE_SYMBOL
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
import threading
from decimal import Decimal
import urllib.request
import urllib.error
import ssl
import urllib.parse

BASE = 'https://openapivts.koreainvestment.com:29443'
REAL_BASE = 'https://openapi.koreainvestment.com:9443'
HISTORY_PATH = '/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice'
OI_OPTIONS = '/uapi/domestic-futureoption/v1/quotations/display-board-callput'
OI_EXPIRIES = '/uapi/domestic-futureoption/v1/quotations/display-board-option-list'
OI_FUTURES = '/uapi/domestic-futureoption/v1/quotations/display-board-futures'
OI_INDEX = '/uapi/domestic-stock/v1/quotations/inquire-index-price'
OI_PRICE = '/uapi/domestic-futureoption/v1/quotations/inquire-price'
FUTURES_MINUTES = '/uapi/domestic-futureoption/v1/quotations/inquire-time-fuopchartprice'
DERIVATIVE_READS = {FUTURES_MINUTES:'FHKIF03020200',OI_PRICE:'FHMIF10000000',OI_OPTIONS:'FHPIF05030100',OI_EXPIRIES:'FHPIO056104C0',OI_FUTURES:'FHPIF05030200',OI_INDEX:'FHPUP02100000'}

QUOTE = '/uapi/domestic-stock/v1/quotations/inquire-price'
ORDER = '/uapi/domestic-stock/v1/trading/order-cash'
FILLS = '/uapi/domestic-stock/v1/trading/inquire-daily-ccld'
KST = timezone(timedelta(hours=9))


class TradingError(Exception):
    pass


class OrderNotSent(TradingError):
    """A local guard stopped a POST before any network transmission."""
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


class RateLimitError(TradingError):
    def __init__(self,seconds=30):
        self.retry_after=max(1,math.ceil(seconds))
        super().__init__('API_RATE_LIMIT [EGW00201] · '+str(self.retry_after)+'초 후 재시도')


def is_rate_limit(error):
    return isinstance(error,RateLimitError) or 'EGW00201' in str(error)


class APIGate:
    """Per-host/app-key actual dispatch spacing, shared across client instances."""
    def __init__(self,clock=None,sleep=None):
        self.clock=clock or (lambda:time.monotonic());self.sleep=sleep or (lambda seconds:time.sleep(seconds))
        self.lock=threading.Lock();self.buckets={}
    def bucket(self,base,key):
        identity=hashlib.sha256((base+'\0'+key).encode()).hexdigest()
        with self.lock:
            return self.buckets.setdefault(identity,dict(lock=threading.Lock(),next=0.0,cooldown=0.0))
    def wait(self,base,key,interval):
        bucket=self.bucket(base,key)
        with bucket['lock']:
            now=self.clock()
            if bucket['cooldown']>now:raise RateLimitError(bucket['cooldown']-now)
            while bucket['next']>now:
                self.sleep(bucket['next']-now);now=self.clock()
            # A late wake must not bunch requests reserved for earlier slots.
            bucket['next']=now+max(1.1,interval) if interval>0 else now
    def penalize(self,base,key,seconds=30):
        bucket=self.bucket(base,key)
        with bucket['lock']:bucket['cooldown']=max(bucket['cooldown'],self.clock()+seconds)


_api_gate=APIGate()


def read_only_get(client,base,path,tr_id,payload):
    key,secret,token=client.tokens.read()
    headers={'Content-Type':'application/json; charset=utf-8','authorization':'Bearer '+token,
        'appkey':key,'appsecret':secret,'tr_id':tr_id,'custtype':'P'}
    _api_gate.wait(base,key,client.interval)
    try:
        data=client.send(path,headers,payload,False) if client.send else transport(path,headers,payload,False,base=base)
    except RequestFailure as exc:
        if exc.code=='EGW00201':
            _api_gate.penalize(base,key);raise RateLimitError() from None
        raise
    finally:client.last=time.monotonic()
    if safe_code(data)=='EGW00201':
        _api_gate.penalize(base,key);raise RateLimitError()
    return data


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def transport(path, headers, payload, post, *, base=BASE):
    if base != BASE and not (base == REAL_BASE and path in {HISTORY_PATH,*DERIVATIVE_READS} and not post):
        raise TradingError('REAL_ROUTE_BLOCKED')
    url = base + path
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
            continuation=response.getheader('tr_cont','') if path in (FILLS,'/uapi/domestic-stock/v1/trading/inquire-balance') else None
        if not isinstance(data, dict):
            raise TradingError('RESPONSE_INVALID')
        if continuation is not None:data['_tr_cont']=continuation
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
    def __init__(self, home, environment='demo'):
        if environment not in ('demo','real'):raise TradingError('INVALID_TOKEN_ENV')
        self.home = Path(home)
        self.environment = environment

    def read(self):
        try:
            config = json.loads((self.home / 'kis_config.json').read_text(encoding='utf-8-sig'))
            profile = config['profiles'][self.environment]
            key, secret = profile['app_key'], profile['app_secret']
            if os.environ.get('KIS_ENV','real') == self.environment:
                pair = (os.environ.get('KIS_APP_KEY'), os.environ.get('KIS_APP_SECRET'))
                if any(pair):
                    key, secret = pair
            if not all(isinstance(v, str) and re.fullmatch(r'[!-~]+', v) for v in (key, secret)):
                raise ValueError()
            binding = hashlib.sha256((self.environment + '\0' + key + '\0' + secret).encode()).hexdigest()
            cached = json.loads((self.home / ('kis_token_' + self.environment + '.json')).read_text(encoding='utf-8'))
            expiry = float(cached['expires'])
            token = cached['token']
            if cached['binding'] != binding or not math.isfinite(expiry) or expiry <= time.time() + 120:
                raise ValueError()
            if not isinstance(token, str) or not re.fullmatch(r'[!-~]+', token):
                raise ValueError()
            return key, secret, token
        except Exception:
            raise TradingError('SHARED_' + self.environment.upper() + '_TOKEN_UNAVAILABLE: 기존 domino의 ' + self.environment + ' 조회 토큰을 확인하세요.') from None


class ReadOnlyHistoryClient:
    """Existing real cache is permitted only for this one historical GET endpoint."""
    def __init__(self, home, send=None, interval=0.2):
        self.tokens=SharedToken(home,'real')
        self.send=send
        self.interval=interval
        self.last=0

    def request(self,path,tr_id,payload,post=False):
        if path!=HISTORY_PATH or tr_id!='FHKST03010230' or post:
            raise TradingError('READ_ONLY_HISTORY_ROUTE_BLOCKED')
        data=read_only_get(self,REAL_BASE,path,tr_id,payload)
        if not isinstance(data,dict) or str(data.get('rt_cd'))!='0':
            raise TradingError('HISTORY_QUERY_REJECTED / CODE='+safe_code(data))
        return data


class ReadOnlyDerivativesClient:
    """Whitelisted real quote GETs only. No account endpoint, order, or token issuance."""
    def __init__(self,home,send=None,interval=1.1):
        self.tokens=SharedToken(home,'real');self.send=send;self.interval=max(0,interval);self.last=0

    def request(self,path,tr_id,payload,post=False):
        if DERIVATIVE_READS.get(path)!=tr_id or post:raise TradingError('DERIVATIVES_READ_ONLY_ROUTE_BLOCKED')
        data=read_only_get(self,REAL_BASE,path,tr_id,payload)
        if not isinstance(data,dict) or str(data.get('rt_cd'))!='0':raise TradingError('OI_QUOTE_REJECTED / CODE='+safe_code(data))
        return data


class DemoClient:
    def __init__(self, home, cano, product, send=transport, interval=1.1):
        if not re.fullmatch(r'\d{8}', cano) or not re.fullmatch(r'\d{2}', product):
            raise TradingError('INVALID_DEMO_ACCOUNT')
        self.tokens = SharedToken(home)
        self.account = {'CANO': cano, 'ACNT_PRDT_CD': product}
        self.send, self.interval, self.last = send, interval, 0
        self.before_order = None

    def request(self, path, tr_id, payload, post=False, continuation=False):
        allowed = {'/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice': {'FHKST03010100'},
                   '/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice': {'FHKST03010200'},
                   '/uapi/domestic-stock/v1/quotations/inquire-investor-time-by-market': {'FHPTJ04030000'}, QUOTE: {'FHKST01010100'}, ORDER: {'VTTC0011U', 'VTTC0012U'}, FILLS: {'VTTC0081R'}, '/uapi/domestic-stock/v1/trading/inquire-balance': {'VTTC8434R'}, '/uapi/domestic-stock/v1/trading/inquire-psbl-order': {'VTTC8908R'}, '/uapi/domestic-stock/v1/trading/order-rvsecncl': {'VTTC0013U'}}
        if tr_id not in allowed.get(path, set()) or post != (path in (ORDER, '/uapi/domestic-stock/v1/trading/order-rvsecncl')):
            raise TradingError('ROUTE_BLOCKED')
        key, secret, token = self.tokens.read()  # Re-read before every call, never renew.
        headers = {'Content-Type': 'application/json; charset=utf-8', 'authorization': 'Bearer ' + token,
                   'appkey': key, 'appsecret': secret, 'tr_id': tr_id, 'custtype': 'P'}
        if continuation:
            headers['tr_cont'] = 'N'
        _api_gate.wait(BASE,key,self.interval)
        if post and self.before_order is not None and not self.before_order():
            raise OrderNotSent('ORDER_NOT_SENT: 중지 또는 정규장 종료로 전송 전 차단')
        try:
            data = self.send(path, headers, payload, post)
        except RequestFailure as exc:
            if exc.code=='EGW00201':
                _api_gate.penalize(BASE,key)
                if not post:raise RateLimitError() from None
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
            if code=='EGW00201':
                _api_gate.penalize(BASE,key)
                if not post:raise RateLimitError()
            raise TradingError(prefix + (' / CODE=' + code if code else ''))
        return data

    def quote(self, symbol):
        output = self.request(QUOTE, 'FHKST01010100', {'FID_COND_MRKT_DIV_CODE': 'J', 'FID_INPUT_ISCD': symbol})['output']
        if output.get('stck_shrn_iscd') != symbol:
            raise TradingError('QUOTE_SYMBOL_MISMATCH')
        return integer(output.get('stck_prpr'), 'QUOTE')

    def order(self, strategy):
        market=strategy.get('order_type','limit')=='market'
        if market and (strategy.get('side')!='sell' or strategy.get('mode')!='risk_stop' or strategy.get('symbol')!=TRADE_SYMBOL):
            raise TradingError('MARKET_ORDER_RESTRICTED_TO_PROTECTIVE_EXIT')
        payload = dict(self.account, PDNO=strategy['symbol'], ORD_DVSN='01' if market else '00', ORD_QTY=str(strategy['quantity']),
                       ORD_UNPR='0' if market else str(strategy['limit_price']), EXCG_ID_DVSN_CD='KRX',
                       SLL_TYPE='01' if strategy['side'] == 'sell' else '', CNDT_PRIC='')
        result = self.request(ORDER, 'VTTC0012U' if strategy['side'] == 'buy' else 'VTTC0011U', payload, True)['output']
        if not re.fullmatch(r'\d+', str(result.get('ODNO', ''))):
            raise TradingError('ORDER_RESPONSE_UNCERTAIN')
        return str(result['ODNO']), str(result.get('KRX_FWDG_ORD_ORGNO', ''))

    def buying_power(self, symbol, price):
        data=self.request('/uapi/domestic-stock/v1/trading/inquire-psbl-order','VTTC8908R',
            dict(self.account,PDNO=symbol,ORD_UNPR=str(price),ORD_DVSN='01',CMA_EVLU_AMT_ICLD_YN='N',OVRS_ICLD_YN='N'))
        output=data.get('output') or {}
        return dict(cash=integer(output.get('nrcvb_buy_amt'),'CASH_BUY_AMOUNT',0),
                    quantity=integer(output.get('nrcvb_buy_qty'),'CASH_BUY_QUANTITY',0))

    def balance(self, symbol):
        params=dict(self.account,AFHR_FLPR_YN='N',OFL_YN='',INQR_DVSN='02',UNPR_DVSN='01',
                    FUND_STTL_ICLD_YN='N',FNCG_AMT_AUTO_RDPT_YN='N',PRCS_DVSN='00',CTX_AREA_FK100='',CTX_AREA_NK100='')
        seen=set();quantity=0;cash=None
        for page in range(100):
            data=self.request('/uapi/domestic-stock/v1/trading/inquire-balance','VTTC8434R',params,continuation=bool(page))
            for item in data.get('output1',[]):
                if item.get('pdno')==symbol:quantity+=integer(item.get('hldg_qty'),'BALANCE_QUANTITY',0)
            summary=data.get('output2') or []
            if summary:cash=integer(summary[0].get('dnca_tot_amt'),'DEPOSIT',0)
            cursor=(str(data.get('ctx_area_fk100','')).strip(),str(data.get('ctx_area_nk100','')).strip())
            if '_tr_cont' in data and data['_tr_cont'] not in ('F','M'):
                return dict(quantity=quantity,deposit=cash,observed_at=datetime.now(KST).isoformat())
            if not any(cursor):
                if data.get('_tr_cont') in ('F','M'):raise TradingError('BALANCE_CONTINUATION_MISSING')
                return dict(quantity=quantity,deposit=cash,observed_at=datetime.now(KST).isoformat())
            if cursor in seen:raise TradingError('BALANCE_PAGINATION_STALLED')
            seen.add(cursor);params.update(CTX_AREA_FK100=cursor[0],CTX_AREA_NK100=cursor[1])
        raise TradingError('BALANCE_PAGINATION_LIMIT')

    def cancel(self, row):
        return self.request('/uapi/domestic-stock/v1/trading/order-rvsecncl','VTTC0013U',
            dict(self.account,KRX_FWDG_ORD_ORGNO=row['branch'],ORGN_ODNO=row['order_no'],
                 ORD_DVSN='00',RVSE_CNCL_DVSN_CD='02',ORD_QTY='0',ORD_UNPR='0',QTY_ALL_ORD_YN='Y',EXCG_ID_DVSN_CD='KRX'),True)

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
                            or integer(item.get('ord_unpr'), 'ORDER_PRICE',0 if strategy.get('order_type')=='market' else 1) != strategy['limit_price']):
                        raise TradingError('ORDER_IDENTITY_MISMATCH')
                    filled = integer(item.get('tot_ccld_qty'), 'FILLED', 0)
                    remaining = integer(item.get('rmn_qty'), 'REMAINING', 0)
                    if filled > row['quantity'] or filled + remaining > row['quantity']:
                        raise TradingError('FILL_QUANTITY_MISMATCH')
                    status = 'FILLED' if filled == row['quantity'] else ('CANCELLED' if item.get('cncl_yn') == 'Y' else ('PARTIAL' if filled else 'SUBMITTED'))
                    amount=None
                    if filled:
                        def positive_decimal(raw):
                            try:
                                value=Decimal(str(raw))
                                if value.is_finite() and value>0 and math.isfinite(float(value)):return value
                            except Exception:pass
                            return None
                        total=positive_decimal(item.get('tot_ccld_amt'))
                        average=positive_decimal(item.get('avg_prvs'))
                        if total is not None and average is not None and abs(total/filled-average)>max(Decimal(1),average/10000):
                            raise TradingError('FILL_PRICE_INCONSISTENT')
                        value=total if total is not None else average*filled if average is not None else None
                        if value is not None and math.isfinite(float(value)):amount=float(value)
                    return status, filled, amount
            cursor = (data.get('ctx_area_fk100', '').strip(), data.get('ctx_area_nk100', '').strip())
            if '_tr_cont' in data and data['_tr_cont'] not in ('F','M'):return None
            if not any(cursor):
                if data.get('_tr_cont') in ('F','M'):raise TradingError('FILL_CONTINUATION_MISSING')
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
            strategy TEXT, status TEXT, order_no TEXT DEFAULT '', branch TEXT DEFAULT '', filled INTEGER DEFAULT 0, filled_cash REAL)''')
        columns={row[1] for row in self.db.execute('PRAGMA table_info(orders)')}
        if 'filled_cash' not in columns:
            try:self.db.execute('ALTER TABLE orders ADD COLUMN filled_cash REAL')
            except sqlite3.OperationalError:
                if 'filled_cash' not in {row[1] for row in self.db.execute('PRAGMA table_info(orders)')}:raise

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

    def refresh_order(self,row):
        result=self.client.fills(row)
        if not result:return {'order_no':row['order_no'],'status':'NOT_FOUND'}
        status,filled=result[:2]
        amount=result[2] if len(result)>2 else None
        if filled<row['filled']:raise TradingError('FILL_REGRESSION')
        if amount is not None and (not math.isfinite(amount) or amount<0 or (filled and amount==0)):
            raise TradingError('FILL_AMOUNT_INVALID')
        if amount is None and filled==row['filled']:amount=row['filled_cash']
        self.db.execute('UPDATE orders SET status=?,filled=?,filled_cash=? WHERE run_id=?',(status,filled,amount,row['run_id']))
        return dict(order_no=row['order_no'],status=status,filled=filled)

    def sync(self,symbol=None):
        results=[]
        sql="SELECT * FROM orders WHERE scope=? AND status IN ('SUBMITTED','PARTIAL','UNKNOWN')"
        params=[self.scope]
        if symbol is not None:sql+=" AND symbol=?";params.append(symbol)
        for row in self.db.execute(sql,params).fetchall():
            if not row['order_no']:
                results.append({'status':'UNKNOWN','action':'한투 주문내역 확인 후 reconcile 명령으로 주문번호 연결'})
            else:results.append(self.refresh_order(row))
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
