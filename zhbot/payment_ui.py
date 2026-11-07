"""Customer payment cards; payment verification and accounting stay in Payments."""
import re

from .payments import PaymentCreationUncertain
from .store import DomainError


class PaymentUI:
    def _payment_order(self, u, pid):
        return self.s.one('SELECT * FROM payments WHERE id=? AND user_id=?', (pid, u))

    def _payment_card(self, u, pid):
        rows = self.s.rows('SELECT * FROM payment_cards WHERE payment_id=? AND user_id=?', (pid, u))
        return rows[0] if rows else None

    def _payment_draft(self, u, did=None):
        if did is None:
            state = self.s.session(u)
            did = state.get('draft') if state.get('mode') == 'draft' else state.get('resume_draft')
        if not did:
            return None
        try:
            draft = self.s.draft(u, did)
        except DomainError:
            return None
        return did if draft['status'] == 'editing' else None

    def _payment_remember(self, u, pid, message_id, did=None):
        # Recheck ownership inside the write transaction, including draft ownership.
        with self.s.tx() as c:
            if not c.execute('SELECT 1 FROM payments WHERE id=? AND user_id=?', (pid, u)).fetchone():
                raise DomainError('找不到这笔充值。')
            if did and not c.execute("SELECT 1 FROM drafts WHERE id=? AND user_id=? AND status='editing'", (did, u)).fetchone():
                did = None
            c.execute('INSERT INTO payment_cards(payment_id,user_id,message_id,return_draft) VALUES(?,?,?,?) '
                      'ON CONFLICT(payment_id) DO UPDATE SET message_id=excluded.message_id, '
                      'return_draft=COALESCE(excluded.return_draft,payment_cards.return_draft) '
                      'WHERE payment_cards.user_id=excluded.user_id', (pid, u, message_id, did))

    def _payment_return_rows(self, u, did=None):
        rows = []
        did = self._payment_draft(u, did) if did else None
        if did:
            rows.append([('继续填写资料', 'resume:' + did)])
        rows.append([('返回首页', 'home')])
        return rows

    def topup_view(self, u):
        if not self.payments:
            raise DomainError('充值暂未开放，请联系管理员。')
        did = self._payment_draft(u)
        state = {'mode': 'topup'}
        if did:
            state['resume_draft'] = did
        self.s.set_session(u, state)
        return self.card(u, '💵 充值积分\n\n请输入充值金额（整数 USDT，最低 1 USDT）。\n1 USDT = 1 积分。',
                         self._payment_return_rows(u, did))

    def topup_input(self, u, text):
        if not self.payments:
            raise DomainError('充值暂未开放，请联系管理员。')
        did = self._payment_draft(u)
        value = str(text).strip()
        if re.fullmatch(r'[0-9]+', value):
            value = value.lstrip('0') or '0'
        error = None
        if re.fullmatch(r'-[0-9]+', value) or (re.fullmatch(r'[0-9]+', value) and not value.strip('0')):
            error = '充值金额至少为 1 USDT，请输入正整数。'
        elif not re.fullmatch(r'[0-9]+', value):
            error = '充值金额仅支持整数，请直接发送数字，例如 10（不含小数、币种或其他文字）。'
        elif len(value.lstrip('0')) > 10 or int(value) > 1_000_000_000:
            error = '单次充值最多 1000000000 USDT，请输入范围内的整数。'
        if error:
            return self.card(u, error, self._payment_return_rows(u, did))
        try:
            pid = self.payments.create(u, int(value))['id']
        except PaymentCreationUncertain as error:
            # Preserve the original order and offer its check button, never retry creation.
            pid = error.order_id
        result = self.payment_view(u, self._payment_order(u, pid))
        self.s.set_session(u, {'resume_draft': did} if did else {})
        return result

    def _payment_content(self, u, order, did=None, *, notice_pending=False):
        state = order['state']
        lines = ['💵 充值积分', '', f"充值金额：{order['amount']} USDT", f"到账积分：{order['amount']} 积分"]
        rows = []
        if state == 'credited':
            if notice_pending:
                lines += ['', '积分已入账，无需重复付款。', f"可用积分：{self.s.balance(u)[0]}"]
            else:
                lines += ['', f"✔️ 充值已入账 {order['amount']} 积分", f"可用积分：{self.s.balance(u)[0]}"]
        elif state == 'closed':
            lines += ['', '充值已关闭，请勿使用原支付链接。']
        elif state == 'pending' and order.get('url'):
            lines += ['', '等待付款', '付款后点击「检查到账」。']
            rows.append([{'text': '去付款', 'url': order['url']}])
        elif state == 'pending':
            lines += ['', '尚未确认到账', '支付链接暂不可用，请联系管理员核对恢复。']
        else:
            lines += ['', '支付链接暂未确认，请先检查到账情况。', '仍未确认时请联系管理员处理，暂勿重复付款。']
        if state not in ('credited', 'closed'):
            rows.append([('检查到账', 'checkpay:' + order['id'])])
        rows += self._payment_return_rows(u, did)
        return '\n'.join(lines), rows

    def payment_view(self, u, order, message_id=None):
        # Callers may pass stale records, so always load the owned, current state.
        order = self._payment_order(u, order['id'])
        saved = self._payment_card(u, order['id'])
        did = self._payment_draft(u) or (self._payment_draft(u, saved['return_draft']) if saved else None)
        if saved:
            message_id = saved['message_id']
        text, rows = self._payment_content(u, order, did)
        result = self.card(u, text, rows, message_id=message_id)
        self._payment_remember(u, order['id'], result['message_id'], did)
        return result

    def payment_check(self, u, pid, message_id=None):
        if not self.payments:
            raise DomainError('支付服务未开放，请联系管理员。')
        self._payment_order(u, pid)
        saved = self._payment_card(u, pid)
        if not saved and message_id is not None:
            self._payment_remember(u, pid, message_id, self._payment_draft(u))
        self.payments.check(u, pid)
        order = self._payment_order(u, pid)
        if order['state'] == 'credited':
            events = self.s.rows("SELECT state FROM events WHERE event_key=? AND kind='credited' AND target=?", ('credited:' + pid, u))
            if events and events[0]['state'] in ('pending', 'sending'):
                # The persistent event owns the single success feedback.
                return None
            if events and events[0]['state'] == 'sent' and saved and message_id is not None and message_id != saved['message_id']:
                # An old duplicate card may predate the persisted canonical card.
                # Refresh the clicked card too, so its payment link cannot stay active.
                did = self._payment_draft(u, saved['return_draft'])
                text, rows = self._payment_content(u, order, did)
                return self.card(u, text, rows, message_id=message_id)
            if events and events[0]['state'] == 'uncertain':
                saved = self._payment_card(u, pid)
                did = self._payment_draft(u, saved['return_draft']) if saved else None
                text, rows = self._payment_content(u, order, did, notice_pending=True)
                result = self.card(u, text, rows, message_id=saved['message_id'] if saved else message_id)
                self._payment_remember(u, pid, result['message_id'], did)
                return result
        return self.payment_view(u, order, message_id=message_id)

    def payment_credit_notice(self, event, payload):
        u, pid = event['target'], payload['payment']
        order = self._payment_order(u, pid)
        if order['state'] != 'credited':
            raise DomainError('充值尚未可靠确认，无法发送到账通知。')
        # card only falls back to sending on an explicit Telegram 400 rejection.
        # Ambiguous failures propagate to the existing durable event state machine.
        return self.payment_view(u, order)
