import json
import unittest
from unittest.mock import patch
from test_domain import Fixture
from zhbot.app import App, UI_ICONS
from zhbot.telegram import TelegramRejected

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
    def click(self,data,user=1,message_id=90):
        self.app.handle({'callback_query':{'id':'query','from':{'id':user},'data':data,'message':{'chat':{'id':user,'type':'private'},'message_id':message_id}}})
    def assert_custom_icon(self,request,emoji,emoji_id):
        entities=[e for e in request.get('entities',[]) if e.get('custom_emoji_id')==emoji_id]
        self.assertTrue(entities,f'Missing custom emoji {emoji_id}')
        encoded=request['text'].encode('utf-16-le')
        for entity in entities:
            self.assertEqual('custom_emoji',entity['type'])
            self.assertEqual(emoji,encoded[entity['offset']*2:(entity['offset']+entity['length'])*2].decode('utf-16-le'))
    def test_home_balance_layout_and_pagination(self):
        for index in range(1,9):
            self.s.save_product(99,dict(name=('长名称'*8 if index==2 else f'服务{index}'),price=2,
                                       description='',eta='',fields=[{'name':'资料','kind':'text'}]))
        self.msg('/start')
        card=self.t.calls[-1][1]
        self.assertIn('可用积分：10',card['text'])
        encoded=card['text'].encode('utf-16-le')
        self.assertEqual(['🛍','💵'],[encoded[e['offset']*2:(e['offset']+e['length'])*2].decode('utf-16-le') for e in card['entities']])
        self.assertNotIn('冻结',card['text'])
        self.assertIn('第 1 / 2 页',card['text'])
        rows=card['reply_markup']['inline_keyboard']
        goods=[[b for b in row if b.get('callback_data','').startswith('buy:')] for row in rows]
        goods=[row for row in goods if row]
        self.assertEqual([2,1,2,2,1],[len(row) for row in goods])
        self.assertTrue(all(b['style']=='primary' for row in goods for b in row))
        self.assertEqual('查询',goods[0][0]['text'])
        self.assertFalse(any('积分' in b['text'] for row in goods for b in row))
        recharge=rows[-1][0]
        self.assertEqual('充值积分',recharge['text'])
        self.assertEqual('primary',recharge['style'])
        self.assertIn('icon_custom_emoji_id',recharge)
        self.assertNotIn('admin',str(rows))
        self.click('home:1')
        method,card=self.t.calls[-1]
        self.assertEqual('editMessageText',method)
        self.assertIn('第 2 / 2 页',card['text'])
        self.assertEqual(2,len(card['entities']))
        self.assertEqual('home:0',card['reply_markup']['inline_keyboard'][1][0]['callback_data'])
        last=self.s.products()[-1]
        last['active']=0
        self.s.save_product(99,last)
        self.s.confirm(1,self.draft('one'))
        self.click('home:1')
        card=self.t.calls[-1][1]
        self.assertIn('可用积分：7',card['text'])
        self.assertIn('冻结积分：3',card['text'])
        self.assertNotIn('页',card['text'])
        self.assertNotIn('home:',str(card['reply_markup']))
    def test_home_empty_catalog_and_admin_entry(self):
        product=self.s.product(self.p)
        product['active']=0
        self.s.save_product(99,product)
        self.msg('/start')
        card=self.t.calls[-1][1]
        self.assertIn('暂无上架服务',card['text'])
        self.assertEqual(['地区神父','充值积分'],[row[0]['text'] for row in card['reply_markup']['inline_keyboard']])
        self.msg('/start',99)
        self.assertEqual('admin',self.t.calls[-1][1]['reply_markup']['inline_keyboard'][-1][0]['callback_data'])
    def test_region_catalog_admin_and_customer(self):
        self.click('catalog:region',99)
        self.assertIn('暂无项目',self.t.calls[-1][1]['text'])
        self.click('newproduct:region',99)
        for value in ('地区项目','7','项目说明','2 小时','资料'):
            self.msg(value,99)
        key=self.s.session(99)['key']
        self.click(f'pfieldkind:{key}:text',99)
        key=self.s.session(99)['key']
        self.click(f'pfieldsdone:{key}',99)
        key=self.s.session(99)['key']
        self.click(f'saveproduct:{key}',99)
        product,=self.s.products(placement='region')
        self.assertEqual(7,product['price'])
        buy=f"buy:{product['id']}"
        self.msg('/start')
        home=self.t.calls[-1][1]['reply_markup']['inline_keyboard']
        self.assertIn(f'buy:{self.p}',str(home))
        self.assertNotIn(buy,str(home))
        self.click('region')
        self.assertIn(buy,str(self.t.calls[-1][1]['reply_markup']))
        self.click(f"toggle:{product['id']}",99)
        self.click('region')
        self.assertIn('暂无项目',self.t.calls[-1][1]['text'])
        self.click(buy)
        self.assertIn('已下架',self.t.calls[-1][1]['text'])
    def test_customer_tier_selection_charges_selected_price_per_line(self):
        self.s.set_product_tiers(99,self.p,self.s.product(self.p)['revision'],{'基础':1,'加急':4})
        self.click(f'buy:{self.p}')
        buttons=self.t.calls[-1][1]['reply_markup']['inline_keyboard']
        self.assertEqual('加急 · 4U',buttons[1][0]['text'])
        self.assertEqual([],self.s.rows('SELECT * FROM drafts'))
        self.click(buttons[1][0]['callback_data'])
        self.msg('a\nb')
        card=self.t.calls[-1][1]
        self.assertIn('档位：加急',card['text'])
        self.assertIn('2 单 × 4 积分\n需冻结 8 积分',card['text'])
        self.click(card['reply_markup']['inline_keyboard'][0][0]['callback_data'])
        self.assertEqual([4,4],[order['price'] for order in self.s.orders()])
        self.assertEqual((2,8),self.s.balance(1))
        self.s.set_product_tiers(99,self.p,self.s.product(self.p)['revision'],{'基础':1,'加急':5})
        self.click(buttons[1][0]['callback_data'])
        self.assertIn('档位已更新',self.t.calls[-1][1]['text'])
        self.assertEqual((2,8),self.s.balance(1))
    def test_quote_card_locks_delivery_and_freezes_delta_once(self):
        self.click(f'pprofile:{self.p}',99)
        self.msg('新菜:1\n大众名：2\n剩菜:12',99)
        self.assertEqual(3,self.s.product(self.p)['price'])
        self.click('psavetiers:'+self.s.session(99)['key'],99)
        self.assertEqual(1,self.s.product(self.p)['price'])
        order,=self.s.orders(self.s.confirm(1,self.draft('x')))
        oid=order['id']
        self.click('quote:'+oid,99)
        self.assertIn('大众名',str(self.t.calls[-1][1]['reply_markup']))
        self.click('qoffer:'+oid+':12',99)
        self.app.flush_events()
        self.assertIn('需补 11 积分',self.t.calls[-1][1]['text'])
        quote=self.s.open_quote(oid)
        self.click('finish:'+oid,99)
        self.assertEqual('pending',self.s.order(oid)['status'])
        self.click('qaccept:'+quote['id'],2)
        self.click('qaccept:'+quote['id'])
        self.assertIn('还差 2 积分',self.t.calls[-1][1]['text'])
        self.assertEqual((9,1),self.s.balance(1))
        self.s.adjust(99,1,2,'充值','quote-topup')
        self.click('qaccept:'+quote['id'])
        self.click('qaccept:'+quote['id'])
        self.assertEqual((0,12),self.s.balance(1))
        self.assertEqual(12,self.s.order(oid)['price'])
        self.s.refund(99,oid,'cancel','测试')
        self.assertEqual((12,0),self.s.balance(1))
    def test_home_custom_emoji_rejection_falls_back_but_timeout_does_not_retry(self):
        with patch.object(self.t,'call',side_effect=[TelegramRejected(400,reason='custom_emoji'),{'message_id':1},{'message_id':2}]) as call:
            self.app.home(1)
            self.app.home(1)
        first,retry,later=[request.kwargs for request in call.call_args_list]
        self.assertEqual(2,len(first['entities']))
        self.assertEqual([],retry['entities'])
        self.assertEqual([],later['entities'])
        self.assertEqual(first['text'],retry['text'])
        self.assertNotIn('icon_custom_emoji_id',retry['reply_markup']['inline_keyboard'][-1][0])
        self.app.custom_emoji=True
        with patch.object(self.t,'call',side_effect=TimeoutError()) as call:
            with self.assertRaises(TimeoutError): self.app.home(1)
        self.assertEqual(1,call.call_count)
    def test_unrelated_rejection_and_unchanged_edit_do_not_disable_custom_emoji(self):
        with patch.object(self.t,'call',side_effect=[TelegramRejected(400),{'message_id':1},{'message_id':2}]) as call:
            self.app.home(1)
            self.app.home(1)
        first,retry,later=[request.kwargs for request in call.call_args_list]
        self.assertEqual(2,len(first['entities']))
        self.assertEqual([],retry['entities'])
        self.assertEqual(2,len(later['entities']))
        self.assertTrue(self.app.custom_emoji)
        with patch.object(self.t,'call',side_effect=[TelegramRejected(400,reason='not_modified'),{'message_id':3}]) as call:
            self.app.home(1,message_id=90)
            self.app.home(1)
        self.assertEqual(['editMessageText','sendMessage'],[request.args[0] for request in call.call_args_list])
        self.assertTrue(self.app.custom_emoji)
        self.assertEqual(2,len(call.call_args_list[-1].kwargs['entities']))
    def test_customer_draft_edit_and_confirm(self):
        self.msg('/start')
        self.assertNotIn('mine',str(self.t.calls[-1][1]['reply_markup']))
        self.t.calls.clear()
        self.click('buy:'+str(self.p))
        cards=[data for method,data in self.t.calls if method=='sendMessage']
        self.assertEqual(1,len(cards))
        self.assertIn('批量报单每行一个',cards[0]['text'])
        self.assert_custom_icon(cards[0],'🛍',UI_ICONS['🛍'])
        self.msg('first'); self.msg('a\nb')
        self.assertIn('2 单 × 3 积分\n需冻结 6 积分',self.t.calls[-1][1]['text'])
        d=self.s.session(1)['draft']; self.click('confirm:'+d+':'+str(self.s.draft(1,d)['edit_version']))
        self.assertEqual(2,len(self.s.orders())); self.assertEqual((4,6),self.s.balance(1))
        method,receipt=self.t.calls[-1]
        self.assertEqual('editMessageText',method)
        self.assertIn('已冻结 6 积分 · 可用 4 积分',receipt['text'])
        self.assertEqual([],receipt['reply_markup']['inline_keyboard'])
        self.assertIn('等待回单',receipt['text'])
        self.assert_custom_icon(receipt,'✔️',UI_ICONS['✔️'])
        self.click('confirm:'+d+':'+str(self.s.draft(1,d)['edit_version']))
        self.assertEqual(2,len(self.s.orders()))
        self.assertIn('等待回单',self.t.calls[-1][1]['text'])
        self.app.flush_events()
        self.assertEqual(1,len([x for x in self.t.calls if '新批次' in x[1].get('text','')]))
    def test_button_icons_fall_back_after_definite_rejection(self):
        rows=[[('确认发送','bulkyes:key'),('取消','admin'),('返回','home'),('商品','buy:1')]]
        with patch.object(self.t,'call',side_effect=[TelegramRejected(400,reason='custom_emoji'),{'message_id':1},{'message_id':2}]) as call:
            self.app.send(99,'回单预览',rows)
            self.app.send(99,'回单预览',rows)
        first,retry,next_send=[
            request.kwargs['reply_markup']['inline_keyboard'][0] for request in call.call_args_list
        ]
        self.assertEqual(UI_ICONS['✔️'],first[0]['icon_custom_emoji_id'])
        self.assertEqual('success',first[0]['style'])
        self.assertEqual(['danger','primary','primary'],[button['style'] for button in first[1:]])
        self.assertEqual('✔️ 确认发送',retry[0]['text'])
        self.assertEqual('success',retry[0]['style'])
        self.assertNotIn('icon_custom_emoji_id',retry[0])
        self.assertEqual(retry,next_send)
    def test_text_only_custom_emoji_falls_back_after_rejection(self):
        with patch.object(self.t,'call',side_effect=[TelegramRejected(400,reason='custom_emoji'),{'message_id':1},{'message_id':2}]) as call:
            self.app.send(1,'✔️ 已完成')
            self.app.send(1,'✔️ 已完成')
        first,retry,later=[request.kwargs for request in call.call_args_list]
        self.assert_custom_icon(first,'✔️',UI_ICONS['✔️'])
        self.assertFalse(any(e['type']=='custom_emoji' for e in retry.get('entities',[])))
        self.assertFalse(any(e['type']=='custom_emoji' for e in later.get('entities',[])))
        self.assertEqual(first['text'],retry['text'])
        self.app.custom_emoji=True
        with patch.object(self.t,'call',side_effect=TimeoutError()) as call:
            with self.assertRaises(TimeoutError): self.app.send(1,'✔️ 已完成')
        self.assertEqual(1,call.call_count)
    def test_unchanged_plain_fallback_keeps_existing_card_without_resending(self):
        with patch.object(self.t,'call',side_effect=[TelegramRejected(400,reason='custom_emoji'),
                                                   TelegramRejected(400,reason='not_modified')]) as call:
            result=self.app.card(1,'💵 充值已入账 2 积分',message_id=123)
        self.assertEqual({'message_id':123},result)
        self.assertEqual(['editMessageText','editMessageText'],[request.args[0] for request in call.call_args_list])
        self.assertFalse(self.app.custom_emoji)
    def test_admin_reply_staged_and_delivery(self):
        o=self.s.orders(self.s.confirm(1,self.draft('one')))[0]['id']
        self.click('order:'+o,99)
        # detail message is linked by returned Telegram message ID
        link=self.s.rows('SELECT * FROM reply_links')[0]['message_id']
        raw='🛍 原始回单 ✔️ 💵'
        self.msg(raw,99,reply=link)
        self.assertEqual(1,len(self.s.receipts(o))); self.assertEqual('pending',self.s.order(o)['status'])
        self.click('finish:'+o,99)
        self.assertEqual('pending',self.s.order(o)['status'])
        preview=[data for method,data in self.t.calls if method=='sendMessage' and data.get('chat_id')==99
                 and data.get('text','').endswith('回单结果\n'+raw)]
        self.assertEqual(1,len(preview))
        self.assertNotIn('entities',preview[0])
        self.click('sendreceipt:'+self.s.session(99)['key'],99); self.app.deliver()
        self.assertEqual('completed',self.s.order(o)['status'])
        delivered=[d for method,d in self.t.calls if method=='sendMessage' and d.get('chat_id')==1]
        self.assertEqual(1,len(delivered))
        self.assertEqual(preview[0]['text'],delivered[0]['text'])
        self.assertIn('原报单',delivered[0]['text'])
        self.assertNotIn('entities',delivered[0])
        self.app.flush_events()
        summary=[d for method,d in self.t.calls if method=='sendMessage' and d.get('chat_id')==1 and '已完成 · 已扣' in d.get('text','')]
        self.assertEqual(1,len(summary))
        self.assert_custom_icon(summary[0],'✔️',UI_ICONS['✔️'])
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
        self.click('saveproduct:'+self.s.session(99)['key'],99)
        self.assertEqual(2,len(self.s.products()))
    def test_admin_home_is_compact_and_adjustment_is_guided(self):
        self.msg('/admin',99)
        home=self.t.calls[-1][1]
        self.assertNotIn('/adjust',home['text'])
        self.assertIn('积分管理',str(home))
        self.click('adjust',99)
        self.msg('1',99); self.msg('5',99); self.msg('测试加分',99)
        key=self.s.session(99)['key']
        self.assertEqual((10,0),self.s.balance(1))
        self.click('adjustyes:'+key,99)
        self.assertEqual((15,0),self.s.balance(1))
    def test_product_field_buttons_and_stale_save(self):
        self.click('newproduct',99)
        for value in ('商品甲','2','说明','2 小时','抖音号'): self.msg(value,99)
        key=self.s.session(99)['key']
        self.click('pfieldkind:'+key+':text',99)
        key=self.s.session(99)['key']
        self.click('pfieldsdone:'+key,99)
        old_key=self.s.session(99)['key']
        self.click('newproduct',99)
        for value in ('商品乙','3','说明','1 小时','账号:text'): self.msg(value,99)
        new_key=self.s.session(99)['key']
        self.click('saveproduct:'+old_key,99)
        self.assertEqual(1,len(self.s.products()))
        self.click('saveproduct:'+new_key,99)
        saved=self.s.products()[-1]
        self.assertEqual('商品乙',saved['name'])
        self.assertEqual([{'name':'账号','kind':'text'}],saved['fields'])
    def test_customer_order_details_and_receipts_are_owner_scoped(self):
        batch=self.s.confirm(1,self.draft('account'))
        order=self.s.orders(batch)[0]['id']
        self.click('mine')
        self.assertIn('mineorder:'+order,str(self.t.calls[-1][1]))
        self.click('mineorder:'+order,2)
        self.assertNotIn('account',self.t.calls[-1][1]['text'])
        self.s.add_receipt(99,order,{'kind':'text','text':'private-result'})
        self.click('myreceipts:'+order)
        self.assertNotIn('private-result',str(self.t.calls[-1]))
        self.s.begin_delivery(99,order)
        self.app.deliver()
        self.t.calls.clear()
        self.click('myreceipts:'+order,2)
        self.assertNotIn('private-result',str(self.t.calls))
        self.click('mineorder:'+order)
        self.assertIn('已完成',self.t.calls[-1][1]['text'])
        self.click('myreceipts:'+order)
        self.assertIn('private-result',str(self.t.calls))
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
        self.click('finish:'+o,99)
        self.click('sendreceipt:'+self.s.session(99)['key'],99); self.app.deliver()
        self.assertEqual('completed',self.s.order(o)['status'])
        self.assertEqual((7,0),self.s.balance(1))
    def test_bulk_delivery_validates_accounts_and_settles_once(self):
        batch=self.s.confirm(1,self.draft('abc\ndef'))
        self.app.flush_events()
        notification=self.t.calls[-1][1]
        self.assertIn('1. abc · 处理中',notification['text'])
        self.assertNotIn(batch,notification['text'])
        self.click('bulk:'+batch,99)
        self.msg('abc 查询结果\nabc 空',99)
        self.assertIn('第 2 行的账号重复了',self.t.calls[-1][1]['text'])
        self.msg('def 空\nabc 查询结果🛍',99)
        key=self.s.session(99)['key']
        self.click('bulkyes:'+key,99)
        self.click('bulkyes:'+key,99)
        self.assertEqual((4,6),self.s.balance(1))
        self.app.deliver()
        self.assertEqual((4,6),self.s.balance(1))
        self.app.flush_events()
        self.app.flush_events()
        messages=[d['text'] for method,d in self.t.calls if method=='sendMessage' and d.get('chat_id')==1]
        self.assertEqual(1,len(messages))
        self.assertIn('原报单：abc\n回单：查询结果🛍\n\n2. 原报单：def\n回单：空单，退回 3 积分',messages[0])
        customer_calls=[d for method,d in self.t.calls if method=='sendMessage' and d.get('chat_id')==1]
        self.assertEqual([],customer_calls[0]['reply_markup']['inline_keyboard'])
        self.assertEqual(1,len(customer_calls[0]['entities']))
        self.assert_custom_icon(customer_calls[0],'✉️',UI_ICONS['✉️'])
        self.assertEqual(['completed','empty'],[o['status'] for o in self.s.orders(batch)])
        self.assertEqual((7,0),self.s.balance(1))
    def test_bulk_timeout_keeps_balance_until_delivery_is_verified(self):
        batch=self.s.confirm(1,self.draft('abc\ndef'))
        self.app.flush_events()
        self.s.queue_bulk(99,batch,{'abc':'查询结果','def':'空'})
        self.t.fail=True
        self.app.flush_events()
        self.assertEqual((4,6),self.s.balance(1))
        event=self.s.events()[-1]
        self.assertEqual('uncertain',event['state'])
        self.t.fail=False
        self.app.flush_events()
        self.assertEqual((4,6),self.s.balance(1))
        self.msg(f"/event {event['id']} sent 123",99)
        self.assertEqual((7,0),self.s.balance(1))
        self.assertEqual('sent',self.s.events()[-1]['state'])
    def test_admin_can_order_as_customer(self):
        self.s.adjust(99,99,5,'测试','admin-seed')
        self.click('buy:'+str(self.p),99); self.msg('管理员的报单',99)
        d=self.s.session(99)['draft']; v=self.s.draft(99,d)['edit_version']
        self.click('confirm:'+d+':'+str(v),99)
        self.assertEqual((2,3),self.s.balance(99))
