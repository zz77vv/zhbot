"""Customer journeys that cross draft state, cards, and accounting safeguards."""
import unittest

from test_domain import Fixture
from zhbot.app import App


class Telegram:
    def __init__(self):
        self.calls=[]
        self.next_id=100

    def call(self,method,**data):
        self.calls.append((method,data))
        if method=='editMessageText':
            return {'message_id':data['message_id']}
        self.next_id+=1
        return {'message_id':self.next_id}


class CustomerUXTests(Fixture,unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.t=Telegram()
        self.app=App(self.s,self.t)

    def msg(self,text=None,user=1,reply=None,**media):
        message={'chat':{'id':user,'type':'private'},'from':{'id':user},'message_id':88,**media}
        if text is not None:
            message['text']=text
        if reply is not None:
            message['reply_to_message']={'message_id':reply}
        self.app.handle({'message':message})

    def click(self,data,user=1):
        self.app.handle({'callback_query':{
            'id':'query','from':{'id':user},'data':data,
            'message':{'chat':{'id':user,'type':'private'},'message_id':self.s.session(user).get('card_id',90)},
        }})

    def card(self):
        return next(data for method,data in reversed(self.t.calls) if method in ('sendMessage','editMessageText'))

    def buttons(self):
        return [button['callback_data'] for row in self.card().get('reply_markup',{}).get('inline_keyboard',[])
                for button in row if 'callback_data' in button]

    def product(self,fields):
        return self.s.save_product(99,dict(name='资料核验',price=3,description='核验说明',eta='人工',fields=fields))

    def start(self,pid=None):
        self.click('buy:'+str(pid or self.p))
        return self.s.session(1)['draft']

    def confirm_button(self):
        return next(button for button in self.buttons() if button.startswith('confirm:'))

    def test_tier_picker_returns_to_original_region_page(self):
        for number in range(9):
            pid=self.s.save_product(99,dict(self.s.product(self.p),id=None,name=f'地区{number}',placement='region'))
        self.s.set_product_tiers(99,pid,self.s.product(pid)['revision'],{'普通':2,'进阶':5})
        self.click('region:1')
        self.click(next(button for button in self.buttons() if button.startswith('buy:')))
        self.assertIn('region:1',self.buttons())
        self.click('region:1')
        self.assertIn('第 2 / 2 页',self.card()['text'])
        self.assertEqual([],self.s.rows('SELECT * FROM drafts'))

    def test_text_fields_advance_to_next_missing_field_and_reuse_card(self):
        pid=self.product([{'name':'账号','kind':'text'},{'name':'地区','kind':'text'},{'name':'说明','kind':'text'}])
        d=self.start(pid)
        card_id=self.s.session(1)['card_id']
        self.assertEqual(1,len([1 for method,_ in self.t.calls if method=='sendMessage']))
        self.msg('客户账号')
        self.assertEqual(1,self.s.session(1)['field'])
        self.assertIn('地区',self.card()['text'])
        self.assertFalse(any(button.startswith('confirm:') for button in self.buttons()))
        self.msg('上海')
        self.assertEqual(2,self.s.session(1)['field'])
        self.msg('补充说明')
        self.assertEqual('confirm:'+d+':'+str(self.s.draft(1,d)['edit_version']),self.confirm_button())
        self.assertEqual(1,len([1 for method,_ in self.t.calls if method=='sendMessage']))
        edits=[data for method,data in self.t.calls if method=='editMessageText']
        self.assertEqual(3,len(edits))
        self.assertTrue(all(data['message_id']==card_id for data in edits))
        self.assertEqual([],self.s.orders())

    def test_text_replacement_skips_fields_already_filled(self):
        pid=self.product([{'name':'账号','kind':'text'},{'name':'地区','kind':'text'},{'name':'说明','kind':'text'}])
        d=self.start(pid)
        self.click('field:'+d+':2'); self.msg('预填说明')
        self.assertEqual(0,self.s.session(1)['field'])
        self.msg('账号甲')
        self.assertEqual(1,self.s.session(1)['field'])
        self.msg('上海')
        self.click('field:'+d+':0'); self.msg('账号乙')
        self.assertEqual([{'kind':'text','text':'账号乙'}],self.s.draft(1,d)['data']['0'])
        self.assertTrue(self.confirm_button())

    def test_media_append_stays_on_field_until_done_and_rejects_stale_done(self):
        for kind in ('photo','document'):
            with self.subTest(kind=kind):
                pid=self.product([{'name':'凭证','kind':kind},{'name':'账号','kind':'text'}])
                d=self.start(pid)
                def upload(file_id):
                    self.msg(**({'photo':[{'file_id':file_id}]} if kind=='photo' else {'document':{'file_id':file_id}}))
                upload('first')
                old_next='draftnext:'+d+':'+str(self.s.draft(1,d)['edit_version'])
                self.assertIn(old_next,self.buttons())
                upload('second')
                self.assertEqual(0,self.s.session(1)['field'])
                self.assertEqual(['first','second'],[item['file_id'] for item in self.s.draft(1,d)['data']['0']])
                next_button='draftnext:'+d+':'+str(self.s.draft(1,d)['edit_version'])
                self.assertIn(next_button,self.buttons())
                self.click(old_next)
                self.assertEqual(0,self.s.session(1)['field'])
                self.assertEqual([],self.s.orders())
                self.click(next_button)
                self.assertEqual(1,self.s.session(1)['field'])
                self.msg('客户账号')
                self.assertTrue(self.confirm_button())

    def test_last_media_field_can_append_before_confirming(self):
        d=self.start(self.product([{'name':'凭证','kind':'photo'}]))
        self.msg(photo=[{'file_id':'one'}])
        old_confirm=self.confirm_button()
        self.msg(photo=[{'file_id':'two'}])
        self.assertNotEqual(old_confirm,self.confirm_button())
        self.click(old_confirm)
        self.assertEqual([],self.s.orders())
        self.assertEqual(2,len(self.s.draft(1,d)['data']['0']))

    def test_insufficient_balance_preserves_data_and_requires_new_preview_confirmation(self):
        d=self.start()
        self.msg('a\nb\nc\nd')
        self.assertIn('topup:'+d,self.buttons())
        self.assertFalse(any(button.startswith('confirm:') for button in self.buttons()))
        self.app.payments=object()
        self.click('topup:'+d)
        self.assertIn('resume:'+d,self.buttons())
        self.s.adjust(99,1,2,'补款','initial-credit')
        self.click('resume:'+d)
        old_confirm=self.confirm_button()
        data=self.s.draft(1,d)['data']
        self.s.adjust(99,1,-2,'余额变动','credit-reversal')
        self.click(old_confirm)
        self.assertEqual((10,0),self.s.balance(1))
        self.assertEqual([],self.s.orders())
        self.assertEqual('editing',self.s.draft(1,d)['status'])
        self.assertEqual(data,self.s.draft(1,d)['data'])
        self.assertIn('resume:'+d,self.buttons())
        self.assertIn('topup',self.buttons())
        self.app.payments=object()
        self.click('topup')
        self.assertEqual('topup',self.s.session(1)['mode'])
        self.assertIn('resume:'+d,self.buttons())
        self.s.adjust(99,1,2,'补款','ux-credit')
        self.click('resume:'+d)
        new_confirm=self.confirm_button()
        self.assertNotEqual(old_confirm,new_confirm)
        self.assertIn('1. a\n2. b\n3. c\n4. d',self.card()['text'])
        self.assertEqual([],self.s.orders())
        self.click(old_confirm)
        self.assertEqual([],self.s.orders())
        self.assertEqual((12,0),self.s.balance(1))
        self.click(new_confirm)
        self.assertEqual(4,len(self.s.orders()))
        self.assertEqual((0,12),self.s.balance(1))

    def test_empty_draft_cancel_returns_home_immediately(self):
        d=self.start()
        self.click('abandon:'+d)
        self.assertEqual('abandoned',self.s.draft(1,d)['status'])
        self.assertEqual({},self.s.session(1))
        self.assertTrue(any(button.startswith('buy:') for button in self.buttons()))

    def test_filled_draft_cancel_requires_confirmation_and_resume_revokes_it(self):
        d=self.start(); self.msg('保留资料')
        self.click('abandon:'+d)
        state=self.s.session(1)
        self.assertEqual('draft_cancel',state['mode'])
        self.assertEqual('editing',self.s.draft(1,d)['status'])
        old_cancel='abandonyes:'+d+':'+state['key']
        self.assertIn(old_cancel,self.buttons())
        self.assertIn('resume:'+d,self.buttons())
        self.click('resume:'+d)
        self.assertEqual('draft',self.s.session(1)['mode'])
        self.assertTrue(self.confirm_button())
        self.click(old_cancel)
        self.assertEqual('editing',self.s.draft(1,d)['status'])
        self.click('abandon:'+d)
        self.click('abandonyes:'+d+':'+self.s.session(1)['key'])
        self.assertEqual('abandoned',self.s.draft(1,d)['status'])
        self.assertEqual([],self.s.orders())
        self.assertEqual((10,0),self.s.balance(1))

    def test_cancel_confirmation_cannot_discard_changed_data(self):
        d=self.start(); self.msg('旧资料'); self.click('abandon:'+d)
        key=self.s.session(1)['key']
        self.s.set_field(1,d,0,{'kind':'text','text':'更新资料'})
        self.click('abandonyes:'+d+':'+key)
        self.assertEqual('editing',self.s.draft(1,d)['status'])
        self.assertEqual('更新资料',self.s.draft(1,d)['data']['0'][0]['text'])

    def test_resume_rechecks_owner_status_and_product_revision(self):
        for reason in ('other_owner','abandoned','inactive','revision'):
            with self.subTest(reason=reason):
                d=self.start(); self.msg('私密资料')
                if reason=='abandoned': self.s.abandon(1,d)
                if reason in ('inactive','revision'):
                    p=self.s.product(self.p)
                    if reason=='inactive': p['active']=0
                    else: p['price']+=1
                    self.s.save_product(99,p)
                self.t.calls.clear()
                self.click('resume:'+d,user=2 if reason=='other_owner' else 1)
                self.assertFalse(any(button.startswith('confirm:') for button in self.buttons()))
                self.assertNotIn('私密资料',self.card()['text'])
                self.assertEqual([],self.s.orders())
                if reason=='inactive':
                    self.s.save_product(99,dict(self.s.product(self.p),active=1))

    def test_admin_wizard_accepts_reply_to_prompt_without_creating_receipt(self):
        order=self.s.orders(self.s.confirm(1,self.draft('客户账号')))[0]['id']
        self.click('order:'+order,user=99)
        linked=self.s.rows('SELECT message_id FROM reply_links')[0]['message_id']
        self.click('newproduct',user=99)
        self.msg('新商品名称',user=99,reply=linked)
        state=self.s.session(99)
        self.assertEqual('新商品名称',state['product']['name'])
        self.assertEqual(1,state['step'])
        self.assertEqual([],self.s.receipts(order))
        self.msg('2',user=99,reply=99999)
        self.assertEqual(2,self.s.session(99)['product']['price'])

    def test_admin_reply_outside_wizard_requires_linked_order(self):
        self.msg('误回复',user=99,reply=99999)
        self.assertIn('订单',self.card()['text'])
        self.assertEqual([],self.s.rows('SELECT * FROM receipts'))

    def test_wrong_media_type_names_expected_field_and_kind(self):
        for kind,label in (('photo','图片'),('document','文件')):
            with self.subTest(kind=kind):
                d=self.start(self.product([{'name':'付款凭证','kind':kind}]))
                self.msg('这不是附件')
                self.assertIn('付款凭证',self.card()['text'])
                self.assertIn(label,self.card()['text'])
                self.assertEqual({},self.s.draft(1,d)['data'])

    def test_customer_smalltalk_guides_to_service_without_admin_instructions(self):
        self.msg('你好')
        text=self.card()['text']
        self.assertIn('服务',text)
        for admin_hint in ('管理员','/admin','/adjust','回单'):
            self.assertNotIn(admin_hint,text)
