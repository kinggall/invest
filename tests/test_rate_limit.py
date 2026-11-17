from unittest import TestCase
from unittest.mock import Mock,patch
import trader as t

class Clock:
    def __init__(self):self.now=0.;self.waits=[];self.late=0.
    def time(self):return self.now
    def sleep(self,seconds):self.waits.append(seconds);self.now+=seconds+self.late;self.late=0.

class Limits(TestCase):
    def setUp(self):
        self.clock=Clock();self.gate=t.APIGate(self.clock.time,self.clock.sleep)
        self.patch=patch.object(t,'_api_gate',self.gate);self.patch.start();self.addCleanup(self.patch.stop)
    def client(self,send):
        client=t.DemoClient('.', '12345678','01',send,1.1)
        client.tokens=Mock();client.tokens.read.return_value=('key','secret','cached')
        return client
    def test_late_wake_does_not_bunch_dispatches(self):
        self.gate.wait(t.BASE,'key',.6);first=self.clock.now
        self.clock.late=5
        self.gate.wait(t.BASE,'key',.6);second=self.clock.now
        self.gate.wait(t.BASE,'key',.6);third=self.clock.now
        self.assertGreaterEqual(second-first,1.1)
        self.assertAlmostEqual(third-second,1.1)
    def test_real_history_and_derivative_instances_share_key_spacing(self):
        calls=[]
        def send(*args):calls.append(self.clock.now);return dict(rt_cd='0')
        a=t.ReadOnlyDerivativesClient('.',send,1.1);b=t.ReadOnlyHistoryClient('.',send,.2)
        for client in (a,b):client.tokens=Mock();client.tokens.read.return_value=('key','secret','cached')
        a.request(t.OI_INDEX,'FHPUP02100000',{})
        b.request(t.HISTORY_PATH,'FHKST03010230',{})
        a.request(t.OI_INDEX,'FHPUP02100000',{})
        self.assertEqual(len(calls),3)
        self.assertAlmostEqual(calls[1]-calls[0],1.1);self.assertAlmostEqual(calls[2]-calls[1],1.1)
    def test_different_app_keys_and_hosts_do_not_share_budget(self):
        for base,key in [(t.BASE,'one'),(t.BASE,'two'),(t.REAL_BASE,'one')]:self.gate.wait(base,key,1.1)
        self.assertEqual(self.clock.now,0)
    def test_cooldown_rejects_without_sleeping_or_sending_more_requests(self):
        self.gate.penalize(t.BASE,'key')
        with self.assertRaises(t.RateLimitError) as error:self.gate.wait(t.BASE,'key',1.1)
        self.assertEqual(error.exception.retry_after,30)
        self.assertEqual(self.clock.waits,[])
        self.clock.now=30;self.gate.wait(t.BASE,'key',1.1)
    def test_http_rate_error_has_no_immediate_retry_and_shared_cooldown(self):
        send=Mock(side_effect=t.RequestFailure('HTTP',500,'EGW00201'));client=self.client(send)
        for _ in range(2):
            with self.assertRaises(t.RateLimitError):client.request(t.QUOTE,'FHKST01010100',{})
        self.assertEqual(send.call_count,1)
        self.clock.now=30
        with self.assertRaises(t.RateLimitError):client.request(t.QUOTE,'FHKST01010100',{})
        self.assertEqual(send.call_count,2)
    def test_post_failure_remains_uncertain_and_is_never_retried(self):
        send=Mock(side_effect=t.RequestFailure('HTTP',500,'EGW00201'));client=self.client(send)
        with self.assertRaisesRegex(t.TradingError,'REQUEST_UNCERTAIN'):
            client.request(t.ORDER,'VTTC0012U',{},True)
        self.assertEqual(send.call_count,1)
    def test_body_rate_error_is_recognized_for_real_quotes(self):
        send=Mock(return_value=dict(rt_cd='1',msg_cd='EGW00201'))
        client=t.ReadOnlyDerivativesClient('.',send,1.1);client.tokens=Mock();client.tokens.read.return_value=('key','secret','cached')
        for _ in range(2):
            with self.assertRaises(t.RateLimitError):client.request(t.OI_INDEX,'FHPUP02100000',{})
        self.assertEqual(send.call_count,1)
