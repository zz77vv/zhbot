import json
import unittest
from test_domain import Fixture
from zhbot.app import App

class Telegram:
    def __init__(self): self.calls=[]; self.fail=False
    def call(self,method,**data):
        if self.fail: raise TimeoutError()
        self.calls.append((method,data)); return {'message_id':len(self.calls)}

class AppTests(Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp(); self.t=Telegram(); self.app=App(self.s,self.t)
    def msg(self,text,user=1,reply=None,chat_type='private'):
        m={'chat':{'id':user,'type':chat_type},'from':{'id':user},'message_id':88,'text':text}
        if reply: m['reply_to_message']={'message_id':reply}
        self.app.handle({'message':m})
    def click(self,data,user=1):
        self.app.handle({'callback_query':{'id':'query','from':{'id':user},'data':data,'message':{'chat':{'id':user,'type':'private'}}}})
    def test_customer_draft_edit_and_confirm(self):
        self.click('buy:'+str(self.p)); self.msg('first'); self.msg('a\nb')
        d=self.s.session(1)['draft']; self.click('confirm:'+d+':'+str(self.s.draft(1,d)['edit_version']))
        self.assertEqual(2,len(self.s.orders())); self.assertEqual((4,6),self.s.balance(1))
        self.app.flush_events()
        self.assertEqual(1,len([x for x in self.t.calls if '新批次' in x[1].get('text','')]))
    def test_admin_reply_staged_and_delivery(self):
        o=self.s.orders(self.s.confirm(1,self.draft('one')))[0]['id']
        self.click('order:'+o,99)
        # detail message is linked by returned Telegram message ID
        link=self.s.rows('SELECT * FROM reply_links')[0]['message_id']
        self.msg('回单',99,reply=link)
        self.assertEqual(1,len(self.s.receipts(o))); self.assertEqual('pending',self.s.order(o)['status'])
        self.click('finish:'+o,99); self.app.deliver()
        self.assertEqual('completed',self.s.order(o)['status'])
    def test_group_and_forged_admin_rejected(self):
        self.msg('/admin',1); self.msg('/start',chat_type='group')
        self.click('manage:'+str(self.p),1)
        self.assertFalse(self.s.session(1))
    def test_timeout_preserves_frozen(self):
        o=self.s.orders(self.s.confirm(1,self.draft('x')))[0]['id']
        self.s.add_receipt(99,o,{'kind':'text','text':'r'}); self.s.begin_delivery(99,o)
        self.t.fail=True; self.app.deliver()
        self.assertEqual('uncertain',self.s.order(o)['status']); self.assertEqual((7,3),self.s.balance(1))
    def test_product_wizard(self):
        self.click('newproduct',99)
        for value in ['新商品','2','说明','一天','客户资料:text']: self.msg(value,99)
        self.assertEqual(1,len(self.s.products()))
        self.click('saveproduct',99)
        self.assertEqual(2,len(self.s.products()))
    def test_cancel_revokes_adjustment_confirmation(self):
        self.msg('/adjust 1 5 补款',99)
        key=self.s.session(99)['key']; self.click('admin',99); self.click('adjustyes:'+key,99)
        self.assertEqual((10,0),self.s.balance(1))
    def test_cancel_revokes_refund_confirmation(self):
        o=self.s.orders(self.s.confirm(1,self.draft('one')))[0]['id']
        self.click('refund:'+o+':empty',99); self.msg('跳过',99)
        key=self.s.session(99)['key']; self.click('admin',99); self.click('refundyes:'+key,99)
        self.assertEqual('pending',self.s.order(o)['status'])
    def test_long_preview_is_not_truncated(self):
        long='资料'*2048
        self.app.send(1,long)
        self.assertEqual(long,''.join(data.get('text','') for method,data in self.t.calls if method=='sendMessage'))
    def test_mixed_receipts_deliver_only_on_finish(self):
        o=self.s.orders(self.s.confirm(1,self.draft('one')))[0]['id']
        self.click('order:'+o,99)
        link=self.s.rows('SELECT * FROM reply_links')[0]['message_id']
        self.msg('文字回单',99,reply=link)
        for media in [{'photo':[{'file_id':'picture'}]},{'document':{'file_id':'file'}}]:
            self.app.handle({'message':{'chat':{'id':99,'type':'private'},'from':{'id':99},'reply_to_message':{'message_id':link},**media}})
        self.assertEqual(3,len(self.s.receipts(o)))
        self.assertFalse(any(d.get('chat_id')==1 and method in ('sendPhoto','sendDocument') for method,d in self.t.calls))
        self.click('finish:'+o,99); self.app.deliver()
        self.assertEqual('completed',self.s.order(o)['status'])
        self.assertEqual((7,0),self.s.balance(1))
    def test_admin_can_order_as_customer(self):
        self.s.adjust(99,99,5,'测试','admin-seed')
        self.click('buy:'+str(self.p),99); self.msg('管理员的报单',99)
        d=self.s.session(99)['draft']; v=self.s.draft(99,d)['edit_version']
        self.click('confirm:'+d+':'+str(v),99)
        self.assertEqual((2,3),self.s.balance(99))
