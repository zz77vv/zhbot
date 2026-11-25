import unittest
from test_domain import Fixture
from zhbot.store import DomainError
from zhbot.payments import Payments, PaymentUnavailable

class Gateway:
    """Test-only protocol double. Never shipped as a payment provider."""
    def create(self,order): return {'url':'https://pay.example.test/'+order['id'],'platform_id':'platform-'+order['id']}
    def verify_query(self,order):
        return {'merchant':order['merchant'],'order_id':order['id'],'platform_id':'platform-'+order['id'],'amount':str(order['amount']),'currency':'USDT','paid':True}

class PaymentTests(Fixture,unittest.TestCase):
    def test_disabled_without_official_adapter(self):
        with self.assertRaises(PaymentUnavailable): Payments(self.s,None,'shop').create(1,1)
    def test_paid_belongs_to_creator_and_idempotent(self):
        p=Payments(self.s,Gateway(),'shop'); o=p.create(1,2)
        p.check(1,o['id']); p.check(1,o['id'])
        self.assertEqual((12,0),self.s.balance(1))
        with self.assertRaises(DomainError): p.check(2,o['id'])
    def test_wrong_amount_currency_merchant_order_rejected(self):
        for key,value in [('amount','3'),('currency','TRX'),('merchant','other'),('order_id','other'),('platform_id','')]:
            with self.subTest(key=key):
                g=Gateway(); original=g.verify_query
                g.verify_query=lambda order,k=key,v=value:{**original(order),k:v}
                p=Payments(self.s,g,'shop'); o=p.create(1,2)
                with self.assertRaises(DomainError): p.check(1,o['id'])
        self.assertEqual((10,0),self.s.balance(1))
    def test_gateway_error_never_credits(self):
        g=Gateway(); p=Payments(self.s,g,'shop'); o=p.create(1,2)
        def fail(order): raise PaymentUnavailable('signature invalid')
        g.verify_query=fail
        with self.assertRaises(PaymentUnavailable): p.check(1,o['id'])
        self.assertEqual((10,0),self.s.balance(1))
    def test_decimal_rejected_and_pending_uncredited(self):
        p=Payments(self.s,Gateway(),'shop')
        for v in [0,-1,1.1,True]:
            with self.assertRaises(DomainError): p.create(1,v)
        g=Gateway(); verify=g.verify_query; g.verify_query=lambda o:{**verify(o),'paid':False}
        p=Payments(self.s,g,'shop'); o=p.create(1,2)
        self.assertIn('未确认',p.check(1,o['id'])); self.assertEqual((10,0),self.s.balance(1))
    def test_duplicate_platform_transaction_cannot_credit_second_order(self):
        g=Gateway(); p=Payments(self.s,g,'shop'); first=p.create(1,2); second=p.create(2,2)
        p.check(1,first['id'])
        verify=g.verify_query; g.verify_query=lambda o:{**verify(o),'platform_id':first['platform_id']}
        with self.assertRaises(DomainError): p.check(2,second['id'])
        self.assertEqual((12,0),self.s.balance(1))
    def test_concurrent_checks_only_credit_once(self):
        import concurrent.futures
        p=Payments(self.s,Gateway(),'shop'); o=p.create(1,2)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _:p.check(1,o['id']),range(8)))
        self.assertEqual((12,0),self.s.balance(1))
