import json
import sqlite3
import unittest

from tests import test_app
from tests.test_app import update
from zhbot.app import Application
from zhbot.runtime import bind_bot
from zhbot.service import Service
from zhbot.store import Store


class UIHelpers:
    setUp = test_app.AppTests.setUp
    tearDown = test_app.AppTests.tearDown
    drain = test_app.AppTests.drain

    def setup_ui(self):
        self.s.register(9001)
        bind_bot(self.s, 12345)
        self.app = Application(self.s)
        self.sequence = 100

    def text(self, text, actor=101, **fields):
        self.sequence += 1
        self.app.process_update(update(self.sequence, text, actor, **fields))
        return self.drain()

    def button(self, messages, label):
        for msg in reversed(messages):
            for row in (msg.get('reply_markup') or {}).get('inline_keyboard', []):
                for button in row:
                    if button['text'] == label:
                        return msg, button
        self.fail('Missing visible button: ' + label)

    def event(self, pair, actor=101, query=None):
        msg, button = pair
        self.sequence += 1
        return {'update_id': self.sequence, 'callback_query': {
            'id': query or 'query-' + str(self.sequence), 'from': {'id': actor, 'is_bot': False},
            'data': button['callback_data'], 'message': {
                'message_id': 10000 + msg['id'], 'from': {'id': 12345, 'is_bot': True},
                'chat': {'id': actor, 'type': 'private'}}}}

    def click(self, messages, label, actor=101):
        event = self.event(self.button(messages, label), actor)
        self.app.process_update(event)
        return self.drain()

class MenuTests(UIHelpers, unittest.TestCase):
    def test_start_has_chinese_menu_and_fresh_user_has_zero_points(self):
        self.setup_ui()
        messages = self.text('/start', 303)
        self.button(messages, '开始报单')
        self.button(messages, '我的草稿')
        self.button(messages, '我的订单')
        balances = self.click(messages, '积分余额', 303)
        self.assertTrue(any('可用 0' in m['content'] for m in balances))
        self.assertEqual(self.s.balance(303), (0, 0))
        self.assertEqual(self.db.all('SELECT * FROM ledger'), [])

    def test_customer_buttons_preserve_mixed_draft_preview_and_freeze_once(self):
        self.setup_ui()
        menu = self.text('/start')
        projects = self.click(menu, '开始报单')
        editor = self.click(projects, '测试项目 · 6 积分')
        self.text('模拟文字')
        self.text(None, photo=[{'file_id': 'fake-photo'}])
        editor = self.text(None, document={'file_id': 'fake-doc'})
        preview = self.click(editor, '完整预览')
        self.assertEqual([m['kind'] for m in preview], ['text', 'photo', 'document', 'text'])
        confirm_pair = self.button(preview, '确认报单并冻结 6 积分')
        event = self.event(confirm_pair)
        self.app.process_update(event)
        event['update_id'] += 1
        event['callback_query']['id'] = 'second-action-id'
        self.app.process_update(event)
        self.assertEqual(len(self.db.all('SELECT * FROM orders')), 1)
        self.assertEqual(self.s.balance(101), (4, 6))
        self.assertEqual(len(self.db.all('SELECT * FROM ledger')), 1)

    def test_forged_cross_user_private_bot_and_message_context_are_rejected(self):
        self.setup_ui()
        menu = self.text('/start', 9001)
        pair = self.button(menu, '管理后台')
        for mutate in ['actor', 'other_owner', 'chat', 'group', 'bot', 'message', 'token', 'inline']:
            event = self.event(pair, 9001)
            q = event['callback_query']
            if mutate == 'actor': q['from']['id'] = 101
            if mutate == 'other_owner': q['from']['id'] = 101; q['message']['chat']['id'] = 101
            if mutate == 'chat': q['message']['chat']['id'] = 101
            if mutate == 'group': q['message']['chat']['type'] = 'group'
            if mutate == 'bot': q['message']['from']['id'] = 999
            if mutate == 'message': q['message']['message_id'] += 500
            if mutate == 'token': q['data'] = 'u:' + 'f'*32
            if mutate == 'inline': q.pop('message'); q['inline_message_id'] = 'x'
            self.app.process_update(event)
        self.assertFalse(any('积分管理' in m['content'] for m in self.drain()))
        self.assertEqual(self.db.all('SELECT * FROM ledger'), [])

    def test_restart_and_callback_and_action_deduplication(self):
        self.setup_ui()
        menu = self.text('/start')
        pair = self.button(menu, '开始报单')
        event = self.event(pair, query='same-query')
        self.app.process_update(event)
        first = len(self.db.all('SELECT * FROM messages'))
        event['update_id'] += 1
        self.app.process_update(event)
        self.assertEqual(len(self.db.all('SELECT * FROM messages')), first)
        self.drain()
        self.sequence += 2
        projects = self.text('/menu')
        projects = self.click(projects, '开始报单')
        other = Store(self.path)
        try:
            self.app = Application(Service(other, {9001}, clock=lambda: 1000))
            self.click(projects, '测试项目 · 6 积分')
            self.text('重启后材料')
            self.assertEqual(len(self.s.draft(101)['items']), 1)
        finally:
            other.close()

    def test_admin_adjustment_guided_reason_confirm_and_revocation(self):
        self.setup_ui()
        menu = self.text('/start', 9001)
        menu = self.click(menu, '管理后台', 9001)
        self.click(menu, '积分管理', 9001)
        user = self.text('101', 9001)
        self.click(user, '增加积分', 9001)
        self.text('5', 9001)
        confirm = self.text('人工核实补分', 9001)
        self.assertEqual(self.s.balance(101), (10, 0))
        pair = self.button(confirm, '确认调整积分')
        self.s.admins = frozenset({9002})
        self.app.process_update(self.event(pair, 9001))
        self.assertEqual(self.s.balance(101), (10, 0))
        self.assertEqual(self.db.all('SELECT * FROM point_adjustments'), [])

    def test_stale_menu_and_expired_button_cannot_change_state(self):
        self.setup_ui()
        menu = self.text('/start')
        pair = self.button(menu, '开始报单')
        self.text('/menu')
        self.app.process_update(self.event(pair))
        self.assertEqual(self.db.all('SELECT * FROM drafts'), [])
        menu = self.text('/menu')
        pair = self.button(menu, '开始报单')
        self.s.clock = lambda: 4000
        self.app.process_update(self.event(pair))
        self.assertEqual(self.db.all('SELECT * FROM drafts'), [])

    def test_action_business_and_cursor_rollback_together(self):
        self.setup_ui()
        projects = self.click(self.text('/start'), '开始报单')
        event = self.event(self.button(projects, '测试项目 · 6 积分'))
        self.db.execute("CREATE TRIGGER cursor_failure BEFORE INSERT ON processed_updates BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(sqlite3.Error): self.app.process_update(event)
        self.assertEqual(self.db.all('SELECT * FROM drafts'), [])
        self.db.execute('DROP TRIGGER cursor_failure')
        self.app.process_update(event)
        self.assertEqual(len(self.db.all('SELECT * FROM drafts')), 1)

class WorkflowTests(UIHelpers, unittest.TestCase):
    def test_fresh_user_manual_funding_then_customer_order_and_admin_mixed_delivery(self):
        self.setup_ui()
        self.text('/start', 303)
        home = self.text('/admin', 9001)
        self.click(home, '积分管理', 9001)
        card = self.text('303', 9001)
        self.click(card, '增加积分', 9001)
        self.text('8', 9001)
        confirm = self.text('线下人工核实发放积分', 9001)
        pair = self.button(confirm, '确认调整积分')
        self.app.process_update(self.event(pair, 9001))
        event = self.event(pair, 9001)
        self.app.process_update(event)
        self.drain()
        self.assertEqual(self.s.balance(303), (8, 0))
        self.assertEqual(len(self.db.all('SELECT * FROM point_adjustments')), 1)
        projects = self.click(self.text('/menu', 303), '开始报单', 303)
        self.click(projects, '测试项目 · 6 积分', 303)
        editor = self.text('客户材料', 303)
        preview = self.click(editor, '完整预览', 303)
        self.click(preview, '确认报单并冻结 6 积分', 303)
        self.assertEqual(self.s.balance(303), (2, 6))
        orders = self.click(self.text('/admin', 9001), '订单管理', 9001)
        detail = self.click(orders, '1 · 测试项目 · 待处理', 9001)
        self.click(detail, '编辑回执', 9001)
        self.text('结果文字', 9001)
        self.text(None, 9001, photo=[{'file_id': 'result-photo'}])
        editor = self.text(None, 9001, document={'file_id': 'result-doc'})
        preview = self.click(editor, '完整预览', 9001)
        self.assertEqual([m['kind'] for m in preview], ['text', 'photo', 'document', 'text'])
        self.click(preview, '确认交付回执', 9001)
        self.assertEqual(self.s.order(303, 1)['status'], 'completed')
        self.assertEqual(self.s.balance(303), (2, 0))
        self.assertEqual(len(self.db.all('SELECT * FROM ledger')), 3)
        self.assertFalse(self.app.payments.gateway.enabled)
        self.assertEqual(self.db.all('SELECT * FROM payments'), [])

    def test_restarted_guided_input_and_snapshot_deduction_confirmation(self):
        self.setup_ui()
        home = self.text('/admin', 9001)
        self.click(home, '积分管理', 9001)
        card = self.text('101', 9001)
        self.click(card, '扣减积分', 9001)
        self.text('4', 9001)
        other = Store(self.path)
        try:
            self.app = Application(Service(other, {9001}, clock=lambda: 1000))
            confirm = self.text('人工核实扣分', 9001)
            self.s.adjust_points(9001, 101, 1, '并发调整导致确认余额变化', 'f'*32)
            self.app.process_update(self.event(self.button(confirm, '确认调整积分'), 9001))
            self.assertEqual(self.s.balance(101), (11, 0))
            self.assertEqual(len(self.db.all('SELECT * FROM point_adjustments')), 1)
        finally:
            other.close()

    def test_revoked_admin_pending_text_is_not_material_and_queue_is_cancelled(self):
        self.setup_ui()
        home = self.text('/admin', 9001)
        self.click(home, '积分管理', 9001)
        self.s.admins = frozenset({9002})
        self.text('101', 9001)
        self.assertEqual(self.db.one('SELECT flow FROM ui_sessions WHERE actor=9001')['flow'], '')
        self.assertEqual(self.db.all('SELECT * FROM draft_items'), [])
        self.assertEqual(self.db.all('SELECT * FROM point_adjustments'), [])

    def test_project_create_edit_toggle_and_old_project_button(self):
        self.setup_ui()
        old = self.click(self.text('/menu'), '开始报单')
        old_pair = self.button(old, '测试项目 · 6 积分')
        projects = self.click(self.text('/admin', 9001), '项目管理', 9001)
        self.click(projects, '新增项目', 9001)
        self.text('新项目', 9001)
        confirm = self.text('2', 9001)
        detail = self.click(confirm, '确认保存项目', 9001)
        self.assertEqual(len(self.s.projects(101)), 2)
        detail = self.click(detail, '下架项目', 9001)
        self.assertEqual(len(self.s.projects(101)), 1)
        self.click(detail, '修改名称和价格', 9001)
        self.text('调整后名称', 9001)
        confirm = self.text('3', 9001)
        self.click(confirm, '确认保存项目', 9001)
        self.assertEqual(self.db.one('SELECT title,price,active FROM projects WHERE id=2'),
                         {'title': '调整后名称', 'price': 3, 'active': 0})
        self.s.save_project(9001, self.project, '价格变更', 8, True)
        self.app.process_update(self.event(old_pair))
        self.assertEqual(self.db.all('SELECT * FROM drafts'), [])

    def test_home_exits_editing_without_losing_draft_or_adding_menu_text(self):
        self.setup_ui()
        projects = self.click(self.text('/start'), '开始报单')
        self.click(projects, '测试项目 · 6 积分')
        self.text('保存的材料')
        self.text('/menu')
        self.text('不应加入草稿')
        self.assertEqual([i['content'] for i in self.s.draft(101)['items']], ['保存的材料'])
        editor = self.click(self.text('/menu'), '我的草稿')
        self.button(editor, '完整预览')

    def test_unknown_delivery_inspection_requires_evidence_and_explicit_resolution(self):
        self.setup_ui()
        draft = self.s.new_draft(101, self.project)
        self.s.add_customer_item(101, 'text', '客户材料')
        self.s.preview_customer(101, draft)
        self.drain()
        order = self.s.confirm(101, draft, 1)
        self.s.open_receipt(9001, order)
        self.s.add_receipt_item(9001, 'text', '结果第一项')
        self.s.add_receipt_item(9001, 'document', 'result-document')
        self.s.preview_receipt(9001, order)
        self.drain()
        self.s.deliver(9001, order, 2)
        first = self.s.claim_message()
        self.s.uncertain(first['id'], '发送超时，实际结果未知')
        orders = self.click(self.text('/admin', 9001), '订单管理', 9001)
        detail = self.click(orders, '1 · 测试项目 · 交付待核实', 9001)
        inspection = self.click(detail, '逐条交付检查', 9001)
        text = '\n'.join(m['content'] for m in inspection)
        for value in ['第 1 项', '第 2 项', '收件人 101', 'unknown', 'Telegram 消息编号 未确认']:
            self.assertIn(value, text)
        choices = self.click(inspection, '核实第 1 项', 9001)
        self.click(choices, '已核实该项送达', 9001)
        self.text(' ', 9001)
        self.assertEqual(self.s.order(101, order)['status'], 'delivery_review')
        confirm = self.text('已核对客户聊天记录，第一项实际送达', 9001)
        self.assertEqual(self.db.one('SELECT status FROM messages WHERE id=?', (first['id'],))['status'], 'unknown')
        self.assertEqual(self.s.balance(101), (4, 6))
        self.click(confirm, '确认人工处置', 9001)
        self.assertEqual(self.s.order(101, order)['status'], 'completed')
        self.assertEqual(self.s.balance(101), (4, 0))
        self.assertEqual(self.db.one('SELECT telegram_id FROM messages WHERE id=?', (first['id'],))['telegram_id'], None)
        self.assertEqual(len(self.db.all("SELECT * FROM audit WHERE action='resolve_sent'")), 1)

    def test_preview_button_cannot_confirm_before_complete_outbox_ack(self):
        self.setup_ui()
        projects = self.click(self.text('/start'), '开始报单')
        self.click(projects, '测试项目 · 6 积分')
        editor = self.text('材料')
        event = self.event(self.button(editor, '完整预览'))
        self.app.process_update(event)
        last = self.db.one("SELECT m.* FROM messages m JOIN batches b ON b.id=m.batch_id WHERE b.kind='customer_preview' ORDER BY m.id DESC LIMIT 1")
        last['reply_markup'] = json.loads(last['reply_markup'])
        pair = self.button([last], '确认报单并冻结 6 积分')
        self.app.process_update(self.event(pair))
        self.assertEqual(self.db.all('SELECT * FROM orders'), [])
        self.assertIsNone(self.s.draft(101)['preview_revision'])
        self.drain()
        self.app.process_update(self.event(pair))
        self.assertEqual(len(self.db.all('SELECT * FROM orders')), 1)
        self.assertEqual(self.s.balance(101), (4, 6))

    def test_queued_admin_menu_cancelled_on_revocation(self):
        self.setup_ui()
        self.sequence += 1
        self.app.process_update(update(self.sequence, '/admin', 9001))
        self.s.admins = frozenset({9002})
        self.s.recover()
        self.assertEqual(self.drain(), [])
        self.assertTrue(all(r['status'] == 'cancelled' for r in self.db.all('SELECT status FROM batches')))

    def test_points_reason_required_and_negative_input_not_accepted(self):
        self.setup_ui()
        self.click(self.text('/admin', 9001), '积分管理', 9001)
        card = self.text('101', 9001)
        self.click(card, '扣减积分', 9001)
        self.text('-2', 9001)
        self.assertEqual(self.db.one('SELECT flow FROM ui_sessions WHERE actor=9001')['flow'], 'points_amount')
        self.text('2', 9001)
        self.text('  ', 9001)
        self.assertEqual(self.db.one('SELECT flow FROM ui_sessions WHERE actor=9001')['flow'], 'points_reason')
        self.assertEqual(self.db.all('SELECT * FROM point_adjustments'), [])
        confirm = self.text('核实撤销多发积分', 9001)
        self.click(confirm, '确认调整积分', 9001)
        self.assertEqual(self.s.balance(101), (8, 0))

    def test_pagination_and_group_callback_identity_fail_closed(self):
        self.setup_ui()
        for i in range(9): self.s.save_project(9001, None, f'项目{i}', 1, True)
        page = self.click(self.text('/menu'), '开始报单')
        next_page = self.click(page, '下一页')
        self.button(next_page, '上一页')
        self.assertFalse(any('callback_data' in m['content'] for m in next_page))
        event = self.event(self.button(next_page, '上一页'))
        event['callback_query']['from']['is_bot'] = True
        self.app.process_update(event)
        self.assertEqual(self.db.one('SELECT status FROM processed_updates WHERE id=?', (event['update_id'],))['status'], 'ignored')

    def test_configured_admin_can_open_menu_before_start_without_funding(self):
        bind_bot(self.s, 12345)
        self.app = Application(self.s)
        self.sequence = 100
        menu = self.text('/admin', 9001)
        points = self.click(menu, '积分管理', 9001)
        self.assertTrue(any('请输入' in m['content'] for m in points))
        self.assertEqual(self.s.balance(9001), (0, 0))
        self.assertEqual(self.db.all('SELECT * FROM ledger'), [])

    def test_point_confirmation_and_action_and_callback_rollback_with_cursor_failure(self):
        self.setup_ui()
        self.click(self.text('/admin', 9001), '积分管理', 9001)
        card = self.text('101', 9001)
        self.click(card, '增加积分', 9001)
        self.text('2', 9001)
        confirm = self.text('人工核实补分', 9001)
        event = self.event(self.button(confirm, '确认调整积分'), 9001)
        self.db.execute("CREATE TRIGGER cursor_failure BEFORE INSERT ON processed_updates BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(sqlite3.Error): self.app.process_update(event)
        self.assertEqual(self.s.balance(101), (10, 0))
        self.assertEqual(self.db.all('SELECT * FROM point_adjustments'), [])
        self.assertFalse(self.db.one('SELECT * FROM processed_callbacks WHERE id=?', (event['callback_query']['id'],)))
        self.db.execute('DROP TRIGGER cursor_failure')
        self.app.process_update(event)
        self.assertEqual(self.s.balance(101), (12, 0))
        self.assertEqual(len(self.db.all('SELECT * FROM point_adjustments')), 1)
        self.assertEqual(len(self.db.all('SELECT * FROM ledger')), 1)

    def test_confirmed_whole_order_failure_cancels_remaining_delivery_without_resend(self):
        self.setup_ui()
        draft = self.s.new_draft(101, self.project)
        self.s.add_customer_item(101, 'text', '材料')
        self.s.preview_customer(101, draft); self.drain()
        order = self.s.confirm(101, draft, 1)
        self.s.open_receipt(9001, order)
        self.s.add_receipt_item(9001, 'text', '未知结果项')
        self.s.add_receipt_item(9001, 'document', '不可继续发送项')
        self.s.preview_receipt(9001, order); self.drain()
        self.s.deliver(9001, order, 2)
        first = self.s.claim_message(); self.s.uncertain(first['id'], '发送超时')
        menu = self.click(self.text('/admin', 9001), '待核实事项', 9001)
        # Review labels show the unresolved part's state, while the inspector
        # shows the order state and actual per-part records.
        inspect = self.click(menu, '订单 1 · 结果未知', 9001)
        choices = self.click(inspect, '核实第 1 项', 9001)
        self.click(choices, '已核实整单交付失败', 9001)
        confirm = self.text('已核对客户聊天记录和接收情况，人工确认整单失败', 9001)
        self.assertEqual(self.s.balance(101), (4, 6))
        self.click(confirm, '确认人工处置', 9001)
        self.assertEqual(self.s.balance(101), (10, 0))
        self.assertEqual(self.s.order(101, order)['status'], 'failed')
        states = self.db.all("SELECT m.status,m.telegram_id FROM messages m JOIN batches b ON b.id=m.batch_id WHERE b.kind='delivery' ORDER BY m.seq")
        self.assertEqual(states, [{'status': 'unknown', 'telegram_id': None}, {'status': 'cancelled', 'telegram_id': None}])
