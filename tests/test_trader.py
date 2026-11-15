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

if __name__=='__main__':unittest.main()
