import unittest
from test_domain import Fixture
from test_app import Telegram
from test_payments import Gateway
from zhbot.payments import Payments, PaymentUnavailable
from zhbot.app import App
from zhbot.config import build_payments

class PaymentUITests(Fixture,unittest.TestCase):
    def setUp(self):
        super().setUp(); self.t=Telegram(); self.p=Payments(self.s,Gateway(),'shop'); self.app=App(self.s,self.t,self.p)
    def test_two_buttons_and_history_after_restart(self):
        self.app.callback(1,'topup'); self.app.message(1,{'text':'2'})
        data=self.t.calls[-1][1]; buttons=data['reply_markup']['inline_keyboard']
        self.assertEqual('支付订单',buttons[0][0]['text']); self.assertEqual('检查支付状态',buttons[1][0]['text'])
        other=App(self.s,self.t,self.p); other.callback(1,'topup')
        self.assertIn('检查支付状态',str(self.t.calls[-1]))
    def test_credit_notification_durable_and_dispatchable(self):
        o=self.p.create(1,2); self.p.check(1,o['id']); self.p.check(1,o['id'])
        self.app.flush_events(); self.app.flush_events()
        messages=[d for method,d in self.t.calls if '充值已入账' in d.get('text','')]
        self.assertEqual(1,len(messages)); self.assertIn('2',messages[0]['text'])
    def test_disabled_by_default_and_partial_config_rejected(self):
        self.assertIsNone(build_payments(self.s,{}))
        with self.assertRaises(PaymentUnavailable): build_payments(self.s,{'OKPAY_ENABLED':'true'})
        with self.assertRaises(PaymentUnavailable): build_payments(self.s,{'OKPAY_ENABLED':'garbage'})
        configured=build_payments(self.s,{'OKPAY_ENABLED':'true','OKPAY_MERCHANT_ID':'10001','OKPAY_MERCHANT_TOKEN':'offline'})
        self.assertIsInstance(configured,Payments)
    def test_admin_can_restore_missing_link_after_signed_pending_check(self):
        from test_okpay import Provider
        from zhbot.okpay import OKPayGateway
        provider=Provider(); provider.timeout_create=True
        payments=Payments(self.s,OKPayGateway('10001','offline-test-token',opener=provider),'10001')
        with self.assertRaises(PaymentUnavailable): payments.create(1,2)
        row=payments.recent(1)[0]; provider.unique=row['id']; provider.amount='2'
        payments.check(1,row['id'])
        with self.assertRaises(ValueError): payments.attach_link(2,row['id'],'platform-1','https://t.me/okpay?start=recovered','后台核实')
        with self.assertRaises(ValueError): payments.attach_link(99,row['id'],'wrong','https://t.me/okpay?start=recovered','后台核实')
        payments.attach_link(99,row['id'],'platform-1','https://t.me/okpay?start=recovered','从商户后台核对订单后恢复')
        self.assertEqual('https://t.me/okpay?start=recovered',payments.create(1,2)['url'])
        self.assertEqual((10,0),self.s.balance(1))
        self.assertTrue(self.s.rows("SELECT * FROM audit WHERE action='payment_link'"))
    def test_paid_link_cannot_be_replaced(self):
        order=self.p.create(1,2); self.p.check(1,order['id'])
        with self.assertRaises(ValueError): self.p.attach_link(99,order['id'],order['platform_id'],'https://t.me/okpay?start=recovered','核实')
    def test_history_pagination_keeps_old_pending_order_accessible(self):
        first=self.p.create(1,1)
        for amount in range(2,12): self.p.create(1,amount)
        self.app.callback(1,'topup')
        self.assertIn('payhistory:1',str(self.t.calls))
        self.app.callback(1,'payhistory:1')
        self.assertIn(first['id'],str(self.t.calls[-3:]))
