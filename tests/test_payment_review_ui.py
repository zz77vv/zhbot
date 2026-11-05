from dataclasses import replace
import hashlib
import json
import unittest

from tests.test_ui import UIHelpers
from zhbot.core import RuleError
from zhbot.payments import Payments, SimulatedGateway


def fingerprint(value):
    return hashlib.sha256(value.encode()).hexdigest()[:12]


class PaymentReviewUITests(UIHelpers, unittest.TestCase):
    def setup_payment(self, *, paid=True, conflict=True):
        self.setup_ui()
        self.now = 1000
        self.s.clock = lambda: self.now
        self.gateway = SimulatedGateway()
        self.p = Payments(self.s, self.gateway)
        payment = self.p.request(101, '3')
        self.p.work_once()
        self.now = 1002
        self.p.request_check(101, payment)
        job = self.p.claim_job()
        first = replace(self.gateway.paid(job['payment'], paid_at=1001),
                        transaction_id='private-original-transaction', payer=101 if paid else None)
        self.p.finish_job(job, first, observed_at=1002)
        if conflict:
            self.p.finish_job(job, replace(first, transaction_id='private-conflicting-transaction',
                payer=987654321, amount='4', coin='TRX'), observed_at=1003)
        self.drain()
        return payment, job, first

    def detail(self, payment, *, conflict=True, status='已入账'):
        menu = self.click(self.text('/admin', 9001), '待核实事项', 9001)
        return self.click(menu, f'充值 {payment[:12]} · {status}' + (' · 身份隔离' if conflict else ''), 9001)

    def labels(self, messages):
        return [b['text'] for m in messages for row in (m.get('reply_markup') or {}).get('inline_keyboard', []) for b in row]

    def test_paid_same_job_conflict_has_safe_evidence_warning_and_no_close(self):
        payment, job, first = self.setup_payment()
        self.assertEqual(self.p.get(101, payment)['reason'], '')
        self.assertEqual(self.db.one('SELECT COUNT(*) n FROM payment_identity_sightings WHERE payment_id=? AND quarantined=1', (payment,))['n'], 4)
        for record in self.db.all("SELECT id,evidence FROM payment_identity_sightings WHERE value='private-conflicting-transaction'"):
            evidence = dict(json.loads(record['evidence']), token='NEVER_SHOW_TOKEN', pay_url='https://private.invalid/NEVER_SHOW_URL')
            self.db.execute('UPDATE payment_identity_sightings SET evidence=? WHERE id=?', (json.dumps(evidence), record['id']))
        before = self.db.all('SELECT * FROM ledger')
        detail = self.detail(payment)
        text = '\n'.join(m['content'] for m in detail)
        for item in ['身份隔离', '4 条', '既有入账不自动撤销', '不同', '本地观察秒 1003',
                     '金额 4', '币种 TRX', '付款人 不匹配', fingerprint('private-conflicting-transaction')]:
            self.assertIn(item, text)
        for secret in ['987654321', 'private-conflicting-transaction', 'private-original-transaction',
                       'NEVER_SHOW_TOKEN', 'NEVER_SHOW_URL', 'SIMULATED']:
            self.assertNotIn(secret, text)
        self.assertNotIn('填写本地关闭依据', self.labels(detail))
        self.assertEqual(self.db.all('SELECT * FROM ledger'), before)
        self.assertEqual(self.s.balance(101), (13, 0))
        self.assertEqual(self.p.get(101, payment)['status'], 'paid')
        self.assertFalse(self.app.payments.gateway.enabled)

    def test_close_only_shown_when_allowed_and_stale_close_rechecked(self):
        payment, job, first = self.setup_payment(paid=False, conflict=False)
        detail = self.detail(payment, conflict=False, status='待核实')
        pair = self.button(detail, '填写本地关闭依据')
        self.p.close_no_credit(9001, payment, '另一个管理员操作已核实关闭')
        self.app.process_update(self.event(pair, 9001))
        self.assertEqual(self.db.one('SELECT flow FROM ui_sessions WHERE actor=9001')['flow'], '')
        self.assertEqual(self.p.get(101, payment)['status'], 'closed')
        self.assertEqual(self.s.balance(101), (10, 0))

    def test_active_check_hides_close_and_no_authenticated_evidence_is_claimed(self):
        self.setup_ui()
        self.gateway = SimulatedGateway(); self.gateway.fail_create = True
        self.p = Payments(self.s, self.gateway)
        payment = self.p.request(101, '3'); self.p.work_once(); self.drain()
        detail = self.detail(payment, conflict=False, status='结果未知')
        self.assertIn('无已认证身份记录', '\n'.join(m['content'] for m in detail))
        self.button(detail, '填写本地关闭依据')
        self.p.request_check(101, payment); self.p.claim_job()
        detail = self.detail(payment, conflict=False, status='结果未知')
        self.assertNotIn('填写本地关闭依据', self.labels(detail))
        self.assertIn('任务仍在执行', '\n'.join(m['content'] for m in detail))

    def test_evidence_pagination_is_bounded_and_keeps_conflicting_replays(self):
        payment, job, first = self.setup_payment()
        for i in range(12):
            self.p.finish_job(job, replace(first, transaction_id='private-replay-' + str(i)), observed_at=1010+i)
        detail = self.detail(payment)
        self.assertTrue(all(len(m['content'].encode('utf-16-le'))//2 <= 3500 for m in detail))
        detail = self.click(detail, '下一页', 9001)
        self.button(detail, '上一页')
        text = '\n'.join(m['content'] for m in detail)
        self.assertIn(fingerprint('private-replay-11'), text)
        self.assertEqual(self.s.balance(101), (13, 0))
        self.assertEqual(len(self.db.all('SELECT * FROM payment_transactions')), 1)

    def test_malformed_evidence_is_not_echoed_or_interpreted_as_verified_facts(self):
        payment, job, first = self.setup_payment()
        self.db.execute("UPDATE payment_identity_sightings SET evidence=? WHERE value='private-conflicting-transaction'", ('bad private raw response',))
        self.db.execute('UPDATE payments SET reason=? WHERE id=?', ('LOCAL_PRIVATE_REASON contains private customer data', payment))
        detail = self.detail(payment)
        text = '\n'.join(m['content'] for m in detail)
        self.assertIn('摘要不可用', text)
        self.assertIn('身份隔离', text)
        self.assertNotIn('bad private raw response', text)
        self.assertNotIn('LOCAL_PRIVATE_REASON', text)
        self.assertEqual(self.s.balance(101), (13, 0))

    def test_unauthorized_and_revoked_admin_cannot_read_queued_detail(self):
        payment, job, first = self.setup_payment()
        with self.assertRaises(RuleError):
            self.app.ui.route(102, 'payment_review', {'id': payment}, 'internal-test')
        self.app.ui.route(9001, 'payment_review', {'id': payment}, 'internal-test')
        self.s.admins = frozenset({9002}); self.s.recover()
        self.assertEqual(self.drain(), [])
        self.assertEqual(self.s.balance(101), (13, 0))
