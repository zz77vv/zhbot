"""Fail-closed payment boundary. There is deliberately no live OKPay adapter.

Evidence below is an INTERNAL simulation contract, not an OKPay API schema.
Only a future reviewed adapter could establish its provenance and semantics.
"""
from dataclasses import dataclass
from decimal import Decimal
import re
import uuid

from .core import MAX_POINTS, RuleError, parse_uint
from .service import transactional


@dataclass(frozen=True)
class Evidence:
    verified: bool
    merchant: str
    unique_id: str
    platform_id: str
    status: int
    amount: object = None
    coin: str | None = None
    payer: int | None = None
    transaction_id: str | None = None
    paid_at: int | None = None
    pay_url: str | None = None


class DisabledGateway:
    enabled = False
    merchant = None

    def create(self, payment):
        raise RuleError('真实支付尚未核实并保持关闭。')

    def check(self, payment):
        raise RuleError('真实支付尚未核实并保持关闭。')


class SimulatedGateway:
    """In-memory, non-networked fixture for tests and the offline demo only."""
    enabled = True
    merchant = 'SIMULATED'

    def __init__(self):
        self.results = {}
        self.fail_create = False

    def create(self, payment):
        if self.fail_create:
            raise TimeoutError('simulated')
        evidence = Evidence(True, self.merchant, payment['id'], 'sim-' + payment['id'], 0,
                            pay_url='simulation://not-a-payment/' + payment['id'])
        self.results[payment['id']] = evidence
        return evidence

    def check(self, payment):
        return self.results[payment['id']]

    def paid(self, payment, paid_at):
        return Evidence(True, self.merchant, payment['id'], payment['platform_id'], 1,
                        str(payment['amount']), 'USDT', payment['user_id'],
                        payment['platform_id'], paid_at)


class Payments:
    def __init__(self, service, gateway=None):
        self.s = service
        self.db = service.db
        self.gateway = gateway if gateway is not None else DisabledGateway()

    def get(self, actor, payment):
        if type(payment) is not str or not re.fullmatch('[0-9a-f]{32}', payment):
            raise RuleError('充值单号无效。')
        row = self.db.one('SELECT * FROM payments WHERE id=? AND user_id=?', (payment, parse_uint(actor)))
        if row is None:
            raise RuleError('充值单不存在或无权访问。')
        return row

    def request(self, actor, amount):
        if not self.gateway.enabled:
            raise RuleError('充值暂未开放：真实 OKPay 身份和支付时间能力尚待核实。')
        amount = parse_uint(amount, maximum=MAX_POINTS)
        with self.db.atomic():
            self.s.user(actor)
            self.db.execute("UPDATE payments SET status='expired' WHERE user_id=? AND status='pending' AND expires_at<=?", (actor, self.s.clock()))
            if self.db.one("SELECT id FROM payments WHERE user_id=? AND status IN ('queued','creating','pending','unknown','review')", (actor,)):
                raise RuleError('已有待处理充值单，请使用 /check 核实原单。')
            payment = uuid.uuid4().hex
            now = self.s.clock()
            self.db.execute("INSERT INTO payments(id,user_id,amount,status,created_at,expires_at) VALUES (?,?,?,'queued',?,?)",
                            (payment, actor, amount, now, now + 600))
            self.db.execute("INSERT INTO payment_jobs(payment_id,kind) VALUES (?,'create')", (payment,))
            return payment

    def request_check(self, actor, payment):
        if not self.gateway.enabled:
            raise RuleError('真实支付查单暂未开放。')
        with self.db.atomic():
            row = self.get(actor, payment)
            if row['status'] == 'paid':
                return False
            if row['status'] in {'queued', 'creating'}:
                raise RuleError('充值单仍在创建，结果不明时不会重新创建。')
            if not self.db.one("SELECT id FROM payment_jobs WHERE payment_id=? AND status IN ('pending','sending')", (payment,)):
                self.db.execute("INSERT INTO payment_jobs(payment_id,kind) VALUES (?,'check')", (payment,))
            return True

    def claim_job(self):
        with self.db.atomic():
            job = self.db.one("SELECT * FROM payment_jobs WHERE status='pending' ORDER BY id LIMIT 1")
            if job is None:
                return None
            self.db.execute("UPDATE payment_jobs SET status='sending' WHERE id=?", (job['id'],))
            if job['kind'] == 'create':
                self.db.execute("UPDATE payments SET status='creating' WHERE id=?", (job['payment_id'],))
            job['payment'] = self.db.one('SELECT * FROM payments WHERE id=?', (job['payment_id'],))
            return job

    def work_once(self):
        job = self.claim_job()
        if job is None:
            return False
        # The provider call is outside all database transactions.
        try:
            operation = self.gateway.create if job['kind'] == 'create' else self.gateway.check
            evidence = operation(job['payment'])
        except Exception:
            with self.db.atomic():
                self.db.execute("UPDATE payment_jobs SET status='unknown' WHERE id=?", (job['id'],))
                self._review(job['payment'], '支付请求结果未知；只能核实原单', status='unknown')
            return True
        with self.db.atomic():
            row = self.db.one('SELECT * FROM payments WHERE id=?', (job['payment_id'],))
            self.db.execute("UPDATE payment_jobs SET status='done' WHERE id=?", (job['id'],))
            if row['status'] == 'paid':
                return True
            if not self._bound(row, evidence):
                self._review(row, '来源或订单关联未通过核实')
            elif job['kind'] == 'create':
                if (type(evidence.status) is not int or evidence.status != 0
                        or not isinstance(evidence.pay_url, str) or len(evidence.pay_url) > 512):
                    self._review(row, '创建响应未通过核实')
                else:
                    self.db.execute("UPDATE payments SET status='pending',platform_id=?,pay_url=? WHERE id=?",
                                    (evidence.platform_id, evidence.pay_url, row['id']))
                    self.s.note(row['user_id'], f'【离线模拟，无实际收款】充值单 {row["id"]}，{row["amount"]} USDT，600 秒本地有效期。/check {row["id"]}')
            else:
                self._apply_check(row, evidence)
        return True

    def _bound(self, row, evidence):
        return (isinstance(evidence, Evidence) and evidence.verified is True
                and evidence.merchant == self.gateway.merchant
                and evidence.unique_id == row['id']
                and type(evidence.platform_id) is str and 0 < len(evidence.platform_id) <= 128
                and (row['platform_id'] is None or row['platform_id'] == evidence.platform_id))

    def _review(self, row, reason, status='review'):
        self.db.execute('UPDATE payments SET status=?,reason=? WHERE id=?', (status, reason, row['id']))
        self.s.audit(0, 'payment_' + status, row['id'], reason)
        self.s.note(row['user_id'], f'充值单 {row["id"]} 待人工核实，未入账。{reason}')

    def _apply_check(self, row, evidence):
        now = self.s.clock()
        if type(evidence.status) is int and evidence.status == 0:
            if row['status'] in {'unknown', 'review'}:
                self._review(row, '先前结果不明，需人工核实')
            elif now >= row['expires_at']:
                self.db.execute("UPDATE payments SET status='expired' WHERE id=?", (row['id'],))
            return
        amount_ok = (type(evidence.amount) in {str, int}
                     and len(str(evidence.amount)) <= 32
                     and re.fullmatch(r'[0-9]{1,7}(?:\.[0-9]{1,8})?', str(evidence.amount))
                     and Decimal(str(evidence.amount)) == row['amount'])
        complete = (type(evidence.status) is int and evidence.status == 1
                    and type(evidence.payer) is int and evidence.payer == row['user_id']
                    and evidence.coin == 'USDT' and amount_ok
                    and type(evidence.paid_at) is int
                    and row['created_at'] <= evidence.paid_at <= now < row['expires_at']
                    and row['status'] == 'pending'
                    and type(evidence.transaction_id) is str
                    and evidence.transaction_id == evidence.platform_id)
        if not complete:
            self._review(row, '付款人、金额、币种、状态、时间或此前状态需人工核实')
            return
        if self.db.one('SELECT * FROM payment_transactions WHERE transaction_id=?', (evidence.transaction_id,)):
            self._review(row, '平台交易号已被其他充值单使用')
            return
        self.db.execute('INSERT INTO payment_transactions(transaction_id,payment_id) VALUES (?,?)',
                        (evidence.transaction_id, row['id']))
        self.s.ledger(row['user_id'], 'payment:' + row['id'], row['amount'], 0)
        self.db.execute("UPDATE payments SET status='paid',reason='' WHERE id=?", (row['id'],))
        self.s.audit(0, 'payment_paid', row['id'], '模拟受信证据核对通过')
        self.s.note(row['user_id'], f'【离线模拟】充值单 {row["id"]} 入账 {row["amount"]} 积分。')
