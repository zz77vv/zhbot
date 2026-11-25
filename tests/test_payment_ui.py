import unittest
from unittest.mock import patch
from test_domain import Fixture
from test_app import Telegram
from test_payments import Gateway
from zhbot.payments import Payments, PaymentUnavailable
from zhbot.app import App
from zhbot.config import build_payments
from zhbot.store import DomainError
from zhbot.telegram import TelegramRejected


class PaymentTelegram(Telegram):
    def call(self,method,**data):
        result=super().call(method,**data)
        if method=='editMessageText':
            return {'message_id':data['message_id']}
        return result


class PaymentUITests(Fixture,unittest.TestCase):
    def setUp(self):
        super().setUp(); self.t=PaymentTelegram(); self.pay=Payments(self.s,Gateway(),'shop'); self.app=App(self.s,self.t,self.pay)
    def test_one_payment_card_and_clean_topup_after_restart(self):
        self.app.callback(1,'topup'); self.app.message(1,{'text':'2'})
        data=self.t.calls[-1][1]; buttons=data['reply_markup']['inline_keyboard']
        self.assertEqual('去付款',buttons[0][0]['text']); self.assertEqual('检查到账',buttons[1][0]['text'])
        self.assertEqual('返回首页',buttons[2][0]['text'])
        order=self.pay.recent(1)[0]
        self.assertIn('充值金额：2 USDT',data['text'])
        self.assertIn('到账积分：2 积分',data['text'])
        self.assertIn('等待付款',data['text'])
        self.assertNotIn(order['id'],data['text'])
        self.assertNotIn('pending',data['text'])
        other=App(self.s,self.t,self.pay)
        self.t.calls.clear()
        other.callback(1,'topup')
        self.assertEqual(1,len(self.t.calls))
        self.assertIn('请输入充值金额',self.t.calls[0][1]['text'])
        self.assertEqual('home',self.t.calls[0][1]['reply_markup']['inline_keyboard'][-1][0]['callback_data'])
    def test_credit_notification_durable_and_dispatchable(self):
        o=self.pay.create(1,2); self.pay.check(1,o['id']); self.pay.check(1,o['id'])
        self.app.flush_events(); self.app.flush_events()
        messages=[d for method,d in self.t.calls if '充值已入账' in d.get('text','')]
        self.assertEqual(1,len(messages)); self.assertIn('2',messages[0]['text'])
        self.assertEqual('sent',self.s.events()[0]['state'])
        self.assertNotIn(o['id'],messages[0]['text'])

    def test_check_credits_only_through_persistent_event_and_edits_payment_card(self):
        self.app.topup_view(1); self.app.topup_input(1,'2')
        order=self.pay.recent(1)[0]
        mid=self.s.one('SELECT * FROM payment_cards WHERE payment_id=?',(order['id'],))['message_id']
        count=len(self.t.calls)
        self.app.payment_check(1,order['id'],message_id=mid)
        self.assertEqual(count,len(self.t.calls))
        self.assertEqual('pending',self.s.events()[0]['state'])
        self.app.flush_events()
        method,data=self.t.calls[-1]
        self.assertEqual('editMessageText',method)
        self.assertEqual(mid,data['message_id'])
        self.assertIn('充值已入账 2 积分',data['text'])
        self.assertIn('可用积分：12',data['text'])
        self.assertFalse(any('url' in b for row in data['reply_markup']['inline_keyboard'] for b in row))
        self.app.payment_check(1,order['id'],message_id=mid)
        self.assertEqual('editMessageText',self.t.calls[-1][0])
        self.assertEqual(mid,self.t.calls[-1][1]['message_id'])
        self.assertEqual((12,0),self.s.balance(1))
        self.assertEqual(1,len(self.s.rows("SELECT * FROM ledger WHERE event_key=?",('payment:'+order['id'],))))
        self.assertEqual(1,len(self.s.events()))

    def test_payment_card_and_resume_context_survive_restart(self):
        did=self.draft('saved-account')
        self.s.set_session(1,{'mode':'draft','draft':did,'field':0})
        self.app.topup_view(1)
        self.assertEqual(did,self.s.session(1)['resume_draft'])
        self.app.topup_input(1,'2')
        order=self.pay.recent(1)[0]
        saved=self.s.one('SELECT * FROM payment_cards WHERE payment_id=?',(order['id'],))
        self.assertEqual(did,saved['return_draft'])
        self.s.set_session(1,{})
        self.pay.check(1,order['id'])
        other=App(self.s,self.t,self.pay); other.flush_events()
        method,data=self.t.calls[-1]
        self.assertEqual('editMessageText',method)
        self.assertEqual(saved['message_id'],data['message_id'])
        self.assertEqual('resume:'+did,data['reply_markup']['inline_keyboard'][0][0]['callback_data'])
        self.assertEqual([],self.s.orders())
        self.assertEqual('editing',self.s.draft(1,did)['status'])

    def test_legacy_payment_card_is_bound_before_first_paid_check(self):
        order=self.pay.create(1,2)
        self.app.payment_check(1,order['id'],message_id=543)
        self.assertEqual([],self.t.calls)
        self.app.flush_events()
        self.assertEqual('editMessageText',self.t.calls[-1][0])
        self.assertEqual(543,self.t.calls[-1][1]['message_id'])
        self.app.payment_check(1,order['id'],message_id=543)
        self.assertEqual((12,0),self.s.balance(1))
        self.assertEqual(1,len(self.s.events()))

    def test_old_duplicate_paid_card_loses_its_payment_buttons_without_new_message(self):
        order=self.pay.create(1,2); self.app.payment_view(1,order)
        self.pay.check(1,order['id']); self.app.flush_events()
        before=len(self.t.calls)
        self.app.payment_check(1,order['id'],message_id=987)
        self.assertEqual(before+1,len(self.t.calls))
        method,data=self.t.calls[-1]
        self.assertEqual('editMessageText',method)
        self.assertEqual(987,data['message_id'])
        self.assertFalse(any('url' in b for row in data['reply_markup']['inline_keyboard'] for b in row))
        self.assertEqual((12,0),self.s.balance(1))
        self.assertEqual(1,len(self.s.events()))

    def test_missing_link_pending_card_does_not_suggest_order_recreation(self):
        order=self.pay.create(1,2)
        with self.s.tx() as c:
            c.execute('UPDATE payments SET url=NULL WHERE id=?',(order['id'],))
        self.app.payment_view(1,order)
        data=self.t.calls[-1][1]
        self.assertIn('尚未确认到账',data['text'])
        self.assertIn('联系管理员',data['text'])
        self.assertNotIn('创建结果',data['text'])
        self.assertFalse(any('url' in b for row in data['reply_markup']['inline_keyboard'] for b in row))

    def test_invalid_amount_remains_in_input_with_specific_error_and_return(self):
        self.app.topup_view(1)
        for value,word in [('','整数'),('abc','整数'),('1.2','整数'),('0','至少'),('-1','至少'),('1000000001','最多'),('9'*5000,'最多')]:
            with self.subTest(value=value[:20]):
                self.app.topup_input(1,value)
                data=self.t.calls[-1][1]
                self.assertIn(word,data['text'])
                self.assertEqual('home',data['reply_markup']['inline_keyboard'][-1][0]['callback_data'])
                self.assertEqual('topup',self.s.session(1)['mode'])
        self.assertEqual([],self.pay.recent(1))
        self.app.topup_input(1,'0'*5000+'2')
        self.assertEqual(2,self.pay.recent(1)[0]['amount'])

    def test_repeated_amount_reuses_same_payment_and_message(self):
        self.app.topup_view(1); self.app.topup_input(1,'2')
        order=self.pay.recent(1)[0]
        saved=self.s.one('SELECT * FROM payment_cards WHERE payment_id=?',(order['id'],))
        self.app.topup_view(1); self.app.topup_input(1,'2')
        self.assertEqual(1,len(self.pay.recent(1)))
        self.assertEqual('editMessageText',self.t.calls[-1][0])
        self.assertEqual(saved['message_id'],self.t.calls[-1][1]['message_id'])

    def test_created_uncertain_card_hides_internal_id_and_checks_original(self):
        with patch.object(self.pay.gateway,'create',side_effect=TimeoutError()):
            self.app.topup_view(1); self.app.topup_input(1,'2')
            order=self.pay.recent(1)[0]
            data=self.t.calls[-1][1]
            self.assertIn('暂未确认',data['text'])
            self.assertNotIn(order['id'],data['text'])
            self.assertNotIn('uncertain',data['text'])
            buttons=[b for row in data['reply_markup']['inline_keyboard'] for b in row]
            self.assertFalse(any('url' in b for b in buttons))
            self.assertEqual('checkpay:'+order['id'],buttons[0]['callback_data'])
            self.app.topup_view(1); self.app.topup_input(1,'2')
        self.assertEqual(1,len(self.pay.recent(1)))

    def test_pending_check_never_claims_payment_or_creates_another_order(self):
        order=self.pay.create(1,2); self.app.payment_view(1,order)
        verify=self.pay.gateway.verify_query
        with patch.object(self.pay.gateway,'verify_query',side_effect=lambda o:{**verify(o),'paid':False}):
            self.app.payment_check(1,order['id'])
        data=self.t.calls[-1][1]
        self.assertIn('等待付款',data['text'])
        self.assertNotIn('已入账',data['text'])
        self.assertEqual((10,0),self.s.balance(1))
        self.assertEqual(1,len(self.pay.recent(1)))

    def test_payment_ownership_applies_before_render_check_and_notice(self):
        order=self.pay.create(1,2); self.app.payment_view(1,order)
        before=list(self.t.calls)
        with self.assertRaises(DomainError): self.app.payment_view(2,order)
        with self.assertRaises(DomainError): self.app.payment_check(2,order['id'],message_id=999)
        with self.assertRaises(DomainError): self.app.payment_credit_notice({'target':2},{'payment':order['id'],'points':2})
        self.assertEqual(before,self.t.calls)
        self.assertEqual(1,self.s.one('SELECT * FROM payment_cards WHERE payment_id=?',(order['id'],))['user_id'])

    def test_closed_card_removes_old_payment_and_check_buttons(self):
        order=self.pay.create(1,2); self.app.payment_view(1,order)
        self.pay.close_unpaid(99,order['id'],'closed','商户确认永久关闭')
        self.app.payment_view(1,order)
        data=self.t.calls[-1][1]
        self.assertIn('已关闭',data['text'])
        buttons=[b for row in data['reply_markup']['inline_keyboard'] for b in row]
        self.assertFalse(any('url' in b or b.get('callback_data','').startswith('checkpay:') for b in buttons))

    def test_notice_timeout_is_uncertain_and_does_not_send_fallback_or_replay(self):
        order=self.pay.create(1,2); self.app.payment_view(1,order); self.pay.check(1,order['id'])
        with patch.object(self.t,'call',side_effect=TimeoutError()) as call:
            self.app.flush_events(); self.app.flush_events()
        self.assertEqual(1,call.call_count)
        self.assertEqual('editMessageText',call.call_args.args[0])
        self.assertEqual('uncertain',self.s.events()[0]['state'])
        self.app.payment_check(1,order['id'])
        data=self.t.calls[-1][1]
        self.assertIn('积分已入账',data['text'])
        self.assertIn('可用积分：12',data['text'])
        self.assertFalse(any('url' in b for row in data['reply_markup']['inline_keyboard'] for b in row))
        self.assertEqual('uncertain',self.s.events()[0]['state'])

    def test_notice_explicit_edit_rejection_can_fall_back_once(self):
        order=self.pay.create(1,2); self.app.payment_view(1,order); self.pay.check(1,order['id'])
        # call_with_buttons may first retry without custom emoji; both failures are explicit.
        with patch.object(self.t,'call',side_effect=[TelegramRejected(400),TelegramRejected(400),{'message_id':777}]) as call:
            self.app.flush_events()
        self.assertEqual(['editMessageText','editMessageText','sendMessage'],[c.args[0] for c in call.call_args_list])
        self.assertEqual('sent',self.s.events()[0]['state'])
        self.assertEqual(777,self.s.events()[0]['message_id'])
        self.assertEqual(777,self.s.one('SELECT * FROM payment_cards WHERE payment_id=?',(order['id'],))['message_id'])

    def test_invalid_or_finished_resume_context_is_omitted(self):
        did=self.draft('a')
        self.s.set_session(2,{'mode':'draft','draft':did})
        self.app.topup_view(2)
        self.assertNotIn('resume:',str(self.t.calls[-1][1]))
        self.s.abandon(1,did)
        self.s.set_session(1,{'resume_draft':did})
        self.app.topup_view(1)
        self.assertNotIn('resume:',str(self.t.calls[-1][1]))
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
        order=self.pay.create(1,2); self.pay.check(1,order['id'])
        with self.assertRaises(ValueError): self.pay.attach_link(99,order['id'],order['platform_id'],'https://t.me/okpay?start=recovered','核实')
