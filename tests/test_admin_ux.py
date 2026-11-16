import json
import unittest
from unittest.mock import patch

from test_app import Telegram
from test_domain import Fixture
from zhbot.app import App


class AdminUXTests(Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.t = Telegram()
        self.app = App(self.s, self.t)

    def click(self, data, user=99, message_id=90):
        self.app.handle({'callback_query': {
            'id': 'query', 'from': {'id': user}, 'data': data,
            'message': {'chat': {'id': user, 'type': 'private'}, 'message_id': message_id},
        }})

    def order(self, text='account'):
        return self.s.orders(self.s.confirm(1, self.draft(text)))[0]

    def buttons(self):
        return [button for row in self.t.calls[-1][1].get('reply_markup', {}).get('inline_keyboard', [])
                for button in row]

    def preview(self, order):
        self.click('finish:' + order['id'])
        confirmations = [button['callback_data'] for button in self.buttons()
                         if button.get('callback_data', '').startswith('sendreceipt:')]
        self.assertEqual(1, len(confirmations), '发送回单应先显示唯一确认按钮')
        return confirmations[0]

    def test_pending_list_has_eight_batch_buttons_and_edits_on_paging(self):
        self.s.adjust(99, 1, 100, '测试', 'more')
        for index in range(10):
            self.order(f'客户资料{index}')
        self.click('pending')
        card = self.t.calls[-1][1]
        self.assertIn('第 1 / 2 页', card['text'])
        self.assertEqual(8, sum(b.get('callback_data', '').startswith('batch:') for b in self.buttons()))
        self.assertIn('pending:1', [b.get('callback_data') for b in self.buttons()])
        self.click('pending:1')
        self.assertEqual('editMessageText', self.t.calls[-1][0])
        self.assertEqual(2, sum(b.get('callback_data', '').startswith('batch:') for b in self.buttons()))
        self.assertIn('pending:0', [b.get('callback_data') for b in self.buttons()])

    def test_batch_order_buttons_are_paged_without_losing_bulk_action(self):
        self.s.adjust(99, 1, 100, '测试', 'more')
        batch = self.s.confirm(1, self.draft('\n'.join(f'account{i}' for i in range(10))))
        self.click('batch:' + batch)
        self.assertEqual(8, sum(b.get('callback_data', '').startswith('order:') for b in self.buttons()))
        self.assertIn('bulk:' + batch, [b.get('callback_data') for b in self.buttons()])
        self.click('batch:' + batch + ':1')
        self.assertEqual('editMessageText', self.t.calls[-1][0])
        self.assertEqual(2, sum(b.get('callback_data', '').startswith('order:') for b in self.buttons()))
        self.assertTrue(any(b['text'].startswith('9.') for b in self.buttons()))
        self.assertIn('pending', [b.get('callback_data') for b in self.buttons()])

    def test_old_finish_previews_all_payloads_and_confirmation_queues_once(self):
        order = self.order()
        receipts = [
            {'kind': 'text', 'text': '结果  原样\n第二行'},
            {'kind': 'photo', 'file_id': 'photo-result', 'caption': '图片说明'},
            {'kind': 'document', 'file_id': 'document-result', 'caption': '附件说明'},
        ]
        for item in receipts:
            self.s.add_receipt(99, order['id'], item)
        before = len(self.t.calls)
        confirm = self.preview(order)
        self.assertEqual('pending', self.s.order(order['id'])['status'])
        self.assertEqual((7, 3), self.s.balance(1))
        self.assertFalse(any(data.get('chat_id') == 1 for _, data in self.t.calls[before:]))
        self.assertTrue(any(data.get('text') == receipts[0]['text'] for _, data in self.t.calls))
        self.assertTrue(any(data.get('photo') == 'photo-result' for _, data in self.t.calls))
        self.assertTrue(any(data.get('document') == 'document-result' for _, data in self.t.calls))
        summary = self.t.calls[-1][1]['text']
        self.assertIn('3 条', summary)
        self.assertIn('3 积分', summary)
        self.assertNotIn(order['id'], summary)
        self.assertNotIn('staged', summary)
        self.assertIn('order:' + order['id'], [b.get('callback_data') for b in self.buttons()])
        self.click(confirm)
        self.assertEqual('delivering', self.s.order(order['id'])['status'])
        self.assertEqual({}, self.s.session(99))
        self.assertEqual((7, 3), self.s.balance(1))
        self.app.deliver()
        self.assertEqual('completed', self.s.order(order['id'])['status'])
        self.assertEqual((7, 0), self.s.balance(1))
        self.click(confirm)
        self.app.deliver()
        delivered = [(method, data) for method, data in self.t.calls if data.get('chat_id') == 1]
        self.assertEqual(4, len(delivered))
        self.assertIn('原报单',delivered[0][1]['text'])
        self.assertIn('回单结果',delivered[0][1]['text'])
        self.assertEqual(receipts[0]['text'], delivered[1][1]['text'])

    def test_receipt_replays_full_original_text_and_attachment_to_customer(self):
        product=dict(self.s.product(self.p),fields=[{'name':'说明','kind':'text'},{'name':'文件','kind':'document'}])
        self.s.save_product(99,product)
        draft=self.s.new_draft(1,self.p)
        original='原'*2100+'最后一行'
        self.s.set_field(1,draft,0,{'kind':'text','text':original})
        self.s.set_field(1,draft,1,{'kind':'document','file_id':'original-file','caption':'原附件'})
        order,=self.s.orders(self.s.confirm(1,draft))
        self.s.add_receipt(99,order['id'],{'kind':'document','file_id':'result-file'})
        self.click(self.preview(order))
        self.app.deliver()
        self.assertEqual('completed',self.s.order(order['id'])['status'])
        delivered=[data for _,data in self.t.calls if data.get('chat_id')==1]
        self.assertIn(original,''.join(data.get('text','') for data in delivered))
        self.assertEqual(['original-file','result-file'],[data['document'] for data in delivered if 'document' in data])
        self.assertEqual((7,0),self.s.balance(1))

    def test_receipt_changes_invalidate_confirmation_even_when_count_is_unchanged(self):
        order = self.order()
        self.s.add_receipt(99, order['id'], {'kind': 'text', 'text': 'old'})
        confirm = self.preview(order)
        self.s.delete_receipt(99, order['id'], self.s.receipts(order['id'])[0]['id'])
        self.s.add_receipt(99, order['id'], {'kind': 'text', 'text': 'new'})
        self.click(confirm)
        self.assertEqual('pending', self.s.order(order['id'])['status'])
        self.assertIn('重新预览', self.t.calls[-1][1]['text'])
        self.assertEqual({}, self.s.session(99))
        self.assertFalse(any(data.get('chat_id') == 1 for _, data in self.t.calls))

    def test_payload_state_and_order_changes_invalidate_confirmation(self):
        for mutation in ('payload', 'state', 'order'):
            with self.subTest(mutation=mutation):
                order = self.order()
                self.s.add_receipt(99, order['id'], {'kind': 'text', 'text': 'old'})
                confirm = self.preview(order)
                with self.s.tx() as connection:
                    if mutation == 'payload':
                        connection.execute('UPDATE receipts SET payload=? WHERE order_id=?',
                                           (json.dumps({'kind': 'text', 'text': 'changed'}), order['id']))
                    elif mutation == 'state':
                        connection.execute("UPDATE receipts SET state='sent' WHERE order_id=?", (order['id'],))
                    else:
                        connection.execute("UPDATE orders SET status='uncertain' WHERE id=?", (order['id'],))
                self.click(confirm)
                self.assertNotEqual('delivering', self.s.order(order['id'])['status'])
                self.assertIn('重新预览', self.t.calls[-1][1]['text'])

    def test_confirm_checks_snapshot_with_delivery_lock(self):
        order = self.order()
        self.s.add_receipt(99, order['id'], {'kind': 'text', 'text': 'old'})
        confirm = self.preview(order)
        begin = self.s.begin_delivery

        def race(actor, order_id, **kwargs):
            self.s.add_receipt(actor, order_id, {'kind': 'text', 'text': 'concurrent'})
            return begin(actor, order_id, **kwargs)

        with patch.object(self.s, 'begin_delivery', side_effect=race):
            self.click(confirm)
        self.assertEqual('pending', self.s.order(order['id'])['status'])
        self.assertIn('重新预览', self.t.calls[-1][1]['text'])

    def test_empty_receipts_and_nonadmin_never_start_delivery(self):
        order = self.order()
        self.click('finish:' + order['id'])
        self.assertEqual('pending', self.s.order(order['id'])['status'])
        self.assertFalse(any(b.get('callback_data', '').startswith('sendreceipt:') for b in self.buttons()))
        self.s.add_receipt(99, order['id'], {'kind': 'text', 'text': 'result'})
        confirm = self.preview(order)
        self.click(confirm, user=1)
        self.assertEqual('pending', self.s.order(order['id'])['status'])
        self.assertIn('管理员', self.t.calls[-1][1]['text'])

    def test_timeout_after_confirmation_keeps_frozen_and_never_replays(self):
        order = self.order()
        self.s.add_receipt(99, order['id'], {'kind': 'text', 'text': 'result'})
        self.click(self.preview(order))
        self.t.fail = True
        self.app.deliver()
        self.t.fail = False
        self.assertEqual('uncertain', self.s.order(order['id'])['status'])
        self.assertEqual((7, 3), self.s.balance(1))
        before = len(self.t.calls)
        self.app.deliver()
        self.assertEqual(before, len(self.t.calls))

    def test_uncertain_queue_resolution_requires_confirmation_and_sends_only_unsent(self):
        order=self.order()
        oid=order['id']
        self.s.add_receipt(99,oid,{'kind':'text','text':'结果'})
        self.click(self.preview(order))
        self.t.fail=True
        self.app.deliver()
        self.t.fail=False
        self.click('pending:0:verify')
        self.assertIn('1 单 · 1 批',self.t.calls[-1][1]['text'])
        self.click('order:'+oid)
        self.assertIn('verify:'+oid,[button['callback_data'] for button in self.buttons()])
        self.click('verify:'+oid)
        part=self.s.receipts(oid,include_context=True)[0]
        self.click(f"vstart:{oid}:part:{part['id']}:sent")
        self.app.message(99,{'text':'123 已向客户核对'})
        confirm=next(b['callback_data'] for b in self.buttons() if b['callback_data'].startswith('vconfirm:'))
        self.assertEqual((7,3),self.s.balance(1))
        self.click(confirm,user=1)
        self.assertEqual('uncertain',self.s.order(oid)['status'])
        before=len(self.t.calls)
        self.click(confirm)
        self.app.deliver()
        self.assertEqual('completed',self.s.order(oid)['status'])
        self.assertEqual((7,0),self.s.balance(1))
        self.assertFalse(any(data.get('chat_id')==1 for _,data in self.t.calls[before:]))

    def test_quote_queue_does_not_count_waiting_orders_as_ready(self):
        self.s.set_product_tiers(99,self.p,self.s.product(self.p)['revision'],{'低':1,'高':2})
        order=self.order()
        self.s.offer_quote(99,order['id'],'高')
        self.click('pending')
        self.assertIn('0 单 · 0 批',self.t.calls[-1][1]['text'])
        self.click('pending:0:quote')
        self.assertIn('1 单 · 1 批',self.t.calls[-1][1]['text'])
        batch=next(b['callback_data'] for b in self.buttons() if b['callback_data'].startswith('batch:'))
        self.click(batch)
        self.assertIn('等补差价 · 1 单',self.t.calls[-1][1]['text'])
        self.assertIn('pending:0:quote',[b['callback_data'] for b in self.buttons()])
