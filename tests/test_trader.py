import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import trader as t

STRATEGY = dict(id='sample', symbol='005930', side='buy', quantity=2, limit_price=50000, trigger_price=51000, max_notional=100000)

class TradingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.binding = hashlib.sha256(b'demo\0key\0secret').hexdigest()
        (self.home/'kis_config.json').write_text(json.dumps({'environment':'real','profiles':{'demo':{'app_key':'key','app_secret':'secret'}}}))
        self.cache = self.home/'kis_token_demo.json'
        self.token()
        self.calls = []
        self.fail = False
        self.filled = 0
        self.client = t.DemoClient(self.home, '12345678', '01', self.send, 0)
        self.engine = t.Engine(self.home/'orders.db', self.client)
        self.addCleanup(self.engine.db.close)

    def token(self, token='cached', expiry=None):
        self.cache.write_text(json.dumps(dict(binding=self.binding, token=token, expires=expiry or time.time()+3600)))

    def send(self, path, headers, payload, post):
        self.calls.append((path, headers, payload, post))
        if path == t.QUOTE:
            return dict(rt_cd='0', output={'stck_shrn_iscd':'005930','stck_prpr':'50000'})
        if path == t.ORDER:
            if self.fail:
                raise TimeoutError('secret')
            return dict(rt_cd='0', output={'ODNO':'000123','KRX_FWDG_ORD_ORGNO':'001'})
        return dict(rt_cd='0', output1=[dict(odno='000123', pdno='005930',ord_dt='20261116',ord_qty='2',ord_unpr='50000',sll_buy_dvsn_cd='02', tot_ccld_qty=str(self.filled),rmn_qty=str(2-self.filled),cncl_yn='N')])

    def run_order(self):
        return self.engine.run(STRATEGY, True, '20261116')

    def test_shared_token_never_written_and_no_auth_route(self):
        original = self.cache.read_bytes()
        self.run_order()
        self.assertEqual(original, self.cache.read_bytes())
        self.assertEqual(self.calls[-1][1]['authorization'], 'Bearer cached')
        self.assertEqual(self.calls[-1][1]['tr_id'], 'VTTC0012U')
        with self.assertRaises(t.TradingError):
            self.client.request('/oauth2/tokenP','VTTC0012U',{},True)
        self.assertTrue(all('/oauth' not in c[0] for c in self.calls))

    def test_expired_or_wrong_binding_stops_before_network(self):
        for field, value in [('expires',time.time()-1),('binding','wrong')]:
            self.token()
            data=json.loads(self.cache.read_text());data[field]=value
            self.cache.write_text(json.dumps(data))
            with self.assertRaises(t.TradingError): self.run_order()
        self.assertEqual(self.calls,[])

    def test_token_rotation_seen_without_restart(self):
        self.client.quote('005930');self.token('rotated');self.client.quote('005930')
        self.assertEqual(self.calls[-1][1]['authorization'],'Bearer rotated')

    def test_preview_and_trigger(self):
        self.assertEqual(self.engine.run(STRATEGY)['status'],'READY')
        self.assertEqual(self.engine.run(dict(STRATEGY,trigger_price=40000),True)['status'],'WAITING')
        self.assertFalse(any(c[3] for c in self.calls))

    def test_restart_deduplicates(self):
        self.run_order()
        other=t.Engine(self.home/'orders.db',self.client)
        self.addCleanup(other.db.close)
        self.assertEqual(other.run(STRATEGY,True,'20261116')['status'],'SUBMITTED')
        self.assertEqual(sum(c[3] for c in self.calls),1)

    def test_timeout_blocks_resubmission(self):
        self.fail=True
        with self.assertRaisesRegex(t.TradingError,'UNCERTAIN'):self.run_order()
        self.assertEqual(self.run_order()['status'],'UNKNOWN')
        self.assertEqual(sum(c[3] for c in self.calls),1)
        self.assertEqual(self.engine.sync()[0]['status'],'UNKNOWN')

    def test_partial_then_filled(self):
        self.run_order();self.filled=1
        self.assertEqual(self.engine.sync()[0]['status'],'PARTIAL')
        self.filled=2
        self.assertEqual(self.engine.sync()[0]['status'],'FILLED')

    def test_notional_and_real_route_blocked(self):
        with self.assertRaises(t.TradingError):self.engine.run(dict(STRATEGY,quantity=3),True)
        with self.assertRaises(t.TradingError):self.client.request(t.ORDER,'TTTC0012U',{},True)
        self.assertEqual(self.calls,[])

    def test_unmatched_order_is_not_filled(self):
        self.run_order()
        original=self.client.send
        def wrong(*args):
            result=original(*args)
            for row in result.get('output1',[]):row['pdno']='000000'
            return result
        self.client.send=wrong
        self.assertEqual(self.engine.sync()[0]['status'],'NOT_FOUND')

    def test_pagination(self):
        self.run_order()
        original=self.client.send
        def pages(path,headers,payload,post):
            if path==t.FILLS and not payload['CTX_AREA_NK100']:
                return dict(rt_cd='0',output1=[],ctx_area_fk100='a',ctx_area_nk100='b')
            if path==t.FILLS:self.assertEqual(headers['tr_cont'],'N')
            return original(path,headers,payload,post)
        self.client.send=pages
        self.assertEqual(self.engine.sync()[0]['status'],'SUBMITTED')

    def test_protective_market_sell_uses_demo_transaction_and_zero_price(self):
        cfg=dict(id='stop',symbol='472150',side='sell',quantity=2,limit_price=0,order_type='market',mode='risk_stop')
        self.client.order(cfg)
        _,headers,payload,post=self.calls[-1]
        self.assertTrue(post)
        self.assertEqual(headers['tr_id'],'VTTC0011U')
        self.assertEqual(payload['ORD_DVSN'],'01')
        self.assertEqual(payload['ORD_UNPR'],'0')
        with self.assertRaises(t.TradingError):self.client.order(dict(cfg,side='buy'))
        with self.assertRaises(t.TradingError):self.client.order(dict(cfg,symbol='005930'))

    def test_market_fills_capture_actual_amount_and_validate_price_identity(self):
        strategy=dict(symbol='472150',side='sell',quantity=2,limit_price=0,order_type='market',mode='risk_stop')
        row=dict(strategy=json.dumps(strategy),day='20261116',symbol='472150',quantity=2,branch='001',order_no='000123')
        def send(*args):
            return dict(rt_cd='0',output1=[dict(odno='000123',pdno='472150',ord_dt='20261116',ord_qty='2',ord_unpr='0',
                sll_buy_dvsn_cd='01',tot_ccld_qty='2',rmn_qty='0',tot_ccld_amt='30010',avg_prvs='15005')])
        client=t.DemoClient(self.home,'12345678','01',send,0)
        self.assertEqual(client.fills(row),('FILLED',2,30010))

    def test_legacy_order_database_migration_preserves_records(self):
        import sqlite3
        path=self.home/'legacy.db'
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE orders (run_id TEXT PRIMARY KEY,scope TEXT,day TEXT,symbol TEXT,quantity INTEGER,strategy TEXT,status TEXT,order_no TEXT,branch TEXT,filled INTEGER)")
            db.execute("INSERT INTO orders VALUES ('old','scope','20261116','472150',2,'{}','FILLED','1','001',2)")
        engine=t.Engine(path,self.client)
        self.addCleanup(engine.db.close)
        row=engine.db.execute("SELECT * FROM orders WHERE run_id='old'").fetchone()
        self.assertEqual(row['filled'],2)
        self.assertIsNone(row['filled_cash'])


class DiagnosticTests(unittest.TestCase):
    def test_http_status_and_code_exclude_secret_response(self):
        import io
        import urllib.error
        from unittest.mock import patch
        raw = io.BytesIO(json.dumps({'msg_cd':'EGW00123','msg1':'private-secret'}).encode())
        failure = urllib.error.HTTPError('https://private-secret.invalid',500,'private-secret',None,raw)
        with patch.object(t.urllib.request,'build_opener') as opener:
            opener.return_value.open.side_effect = failure
            with self.assertRaises(t.RequestFailure) as caught:
                t.transport(t.ORDER,{}, {},True)
        self.assertEqual(str(caught.exception),'HTTP HTTP=500 CODE=EGW00123')
        self.assertNotIn('private-secret',str(caught.exception))

    def test_timeout_does_not_echo_exception(self):
        from unittest.mock import patch
        with patch.object(t.urllib.request,'build_opener') as opener:
            opener.return_value.open.side_effect=TimeoutError('private-secret')
            with self.assertRaisesRegex(t.RequestFailure,'^TIMEOUT$'):
                t.transport(t.ORDER,{}, {},True)

    def test_invalid_codes_are_not_displayed(self):
        self.assertEqual(t.safe_code({'msg_cd':'private-secret'}),'')

if __name__=='__main__':unittest.main()
