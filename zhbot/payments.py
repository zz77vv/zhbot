"""Fail-closed payment boundary. There is deliberately no live OKPay adapter.

Evidence below is an INTERNAL simulation contract, not an OKPay API schema.
Only a future reviewed adapter could establish its provenance and semantics.
"""
from dataclasses import dataclass, fields
from decimal import Decimal
import json
import re
import uuid

from .core import MAX_ID, MAX_POINTS, RuleError, bounded_text, parse_uint
from .store import identity_conflicts, quarantine_identities


@dataclass(frozen=True)
class Evidence:
    """Internal authenticated facts, never raw user input or an official schema.

    platform_id identifies the provider's bill; transaction_id independently
    identifies a settlement. paid_at is an authenticated actual payment time,
    or None when the adapter cannot establish its meaning/provenance.
    """
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
                        'sim-tx-' + payment['id'], paid_at)


class Payments:
    def __init__(self, service, gateway=None):
        self.s = service
        self.db = service.db
        self.gateway = gateway if gateway is not None else DisabledGateway()

    def get(self, actor, payment):
        row = self._get(payment)
        if row['user_id'] != parse_uint(actor):
            raise RuleError('充值单不存在或无权访问。')
        return row

    def _get(self, payment):
        if type(payment) is not str or not re.fullmatch('[0-9a-f]{32}', payment):
            raise RuleError('充值单号无效。')
        row = self.db.one('SELECT * FROM payments WHERE id=?', (payment,))
        if row is None:
            raise RuleError('充值单不存在或无权访问。')
        return row

    def request(self, actor, amount):
        if not self.gateway.enabled:
            raise RuleError('充值暂未开放：真实 OKPay 身份和支付时间能力尚待核实。')
        amount = parse_uint(amount, maximum=MAX_POINTS)
        with self.db.atomic():
            self.s.user(actor)
            for row in self.db.all("SELECT * FROM payments WHERE user_id=? AND status IN ('queued','pending') AND expires_at<=?", (actor, self.s.clock())):
                self._expire(row, '本地支付窗口结束；不代表平台取消或实际付款状态')
            if self.db.one("SELECT id FROM payments WHERE user_id=? AND status IN ('queued','creating','pending','unknown','review','closed_review')", (actor,)):
                raise RuleError('已有待处理充值单，请使用 /check 核实原单。')
            payment = uuid.uuid4().hex
            now = self.s.clock()
            self.db.execute("INSERT INTO payments(id,user_id,amount,status,created_at,expires_at) VALUES (?,?,?,'queued',?,?)",
                            (payment, actor, amount, now, now + 600))
            self.db.execute("INSERT INTO payment_jobs(payment_id,kind) VALUES (?,'create')", (payment,))
            self.s.audit(actor, 'payment_requested', payment, '本地有效期 600 秒；真实支付未启用')
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
            while True:
                job = self.db.one("SELECT * FROM payment_jobs WHERE status='pending' ORDER BY id LIMIT 1")
                if job is None:
                    return None
                row = self._get(job['payment_id'])
                if job['kind'] == 'create' and row['status'] == 'queued' and self.s.clock() >= row['expires_at']:
                    self._expire(row, '截止前未发起创建；没有调用创建接口')
                    continue
                if (job['kind'] == 'create' and row['status'] != 'queued') or row['status'] == 'paid':
                    self.db.execute("UPDATE payment_jobs SET status='cancelled' WHERE id=?", (job['id'],))
                    self.s.audit(0, 'payment_job_cancelled', row['id'], '状态不允许执行旧任务')
                    continue
                self.db.execute("UPDATE payment_jobs SET status='sending' WHERE id=?", (job['id'],))
                if job['kind'] == 'create':
                    self.db.execute("UPDATE payments SET status='creating' WHERE id=?", (row['id'],))
                job['payment'] = self._get(row['id'])
                return job

    def _expire(self, row, reason):
        self.db.execute("UPDATE payments SET status='expired',pay_url=NULL,reason=? WHERE id=?", (reason, row['id']))
        self.db.execute("UPDATE payment_jobs SET status='cancelled' WHERE payment_id=? AND kind='create' AND status IN ('pending','sending')", (row['id'],))
        self.s.audit(0, 'payment_local_expired', row['id'], reason)

    def _dispatch_allowed(self, job):
        with self.db.atomic():
            current = self.db.one('SELECT * FROM payment_jobs WHERE id=?', (job['id'],))
            row = self._get(job['payment_id'])
            if current['status'] != 'sending':
                return False
            if job['kind'] == 'create':
                if row['status'] != 'creating':
                    return False
                if self.s.clock() >= row['expires_at']:
                    self._expire(row, '任务领取后、接口调用前达到本地截止；未调用创建接口')
                    return False
                if self.s.clock() < row['created_at']:
                    self.db.execute("UPDATE payment_jobs SET status='cancelled' WHERE id=?", (job['id'],))
                    self._review(row, '本地时钟早于订单创建，未发起请求')
                    return False
            return True

    def work_once(self):
        if not self.gateway.enabled:
            return False
        job = self.claim_job()
        if job is None:
            return False
        if not self._dispatch_allowed(job):
            return True
        # The provider call is outside all database transactions.
        try:
            operation = self.gateway.create if job['kind'] == 'create' else self.gateway.check
            evidence = operation(job['payment'])
            observed_at = self.s.clock()
        except Exception:
            with self.db.atomic():
                current = self.db.one('SELECT status FROM payment_jobs WHERE id=?', (job['id'],))
                row = self._get(job['payment_id'])
                if current['status'] == 'sending':
                    self.db.execute("UPDATE payment_jobs SET status='unknown' WHERE id=?", (job['id'],))
                    self._review(row, '支付请求结果未知；只能核实原单', status='unknown')
                else:
                    self.s.audit(0, 'payment_stale_error', row['id'], '旧任务异常，不覆盖当前处置')
            return True
        self.finish_job(job, evidence, observed_at=observed_at)
        return True

    def finish_job(self, job, evidence, *, observed_at):
        observed_at = parse_uint(observed_at, minimum=0)
        with self.db.atomic():
            current = self.db.one('SELECT * FROM payment_jobs WHERE id=?', (parse_uint(job['id']),))
            if current is None:
                raise RuleError('支付任务不存在。')
            row = self._get(current['payment_id'])
            encoded = self._safe_evidence(evidence)
            duplicate = not self.db.execute('INSERT OR IGNORE INTO payment_observations(job_id,payment_id,observed_at,evidence) VALUES (?,?,?,?)',
                                           (current['id'], row['id'], observed_at, encoded)).rowcount
            observation = self.db.one('SELECT id FROM payment_observations WHERE job_id=?', (current['id'],))
            stale = current['status'] != 'sending'
            if not stale and not duplicate:
                self.db.execute("UPDATE payment_jobs SET status='done' WHERE id=?", (current['id'],))
            if not self._authenticated(row, evidence):
                if not duplicate:
                    self._review(row, '来源或订单关联未通过核实')
                return False
            transaction = evidence.transaction_id if type(evidence.status) is int and evidence.status == 1 and self._identity(evidence.transaction_id) else None
            values = ([('platform', evidence.platform_id)] if self._identity(evidence.platform_id) else []) + ([('transaction', transaction)] if transaction else [])
            new_sightings = 0
            for kind, value in values:
                new_sightings += self.db.execute('INSERT OR IGNORE INTO payment_identity_sightings(observation_id,payment_id,kind,value,observed_at,evidence) VALUES (?,?,?,?,?,?)',
                                                (observation['id'], row['id'], kind, value, observed_at, encoded)).rowcount
            if not self._bound(row, evidence) or not self._reserve_identities(row, evidence.platform_id, transaction):
                changed = quarantine_identities(self.db.connection, row['id'])
                if changed or new_sightings or not duplicate:
                    self.s.audit(0, 'payment_identity_quarantined', row['id'], '已认证身份矛盾或关联不完整；保留全部身份，禁止自动复用')
                    self._review(row, '平台订单或结算交易身份已经关联其他充值单，或同单证据冲突')
                    for admin in self.s.admins:
                        self.s.note(admin, f'充值单 {row["id"]} 身份出现隔离记录；请 /review 核实。既有入账不自动撤销。', admin_only=True)
                return False
            if duplicate:
                return False  # Identity contradictions above still count on a replay.
            if row['status'] == 'paid':
                self.s.audit(0, 'payment_duplicate_observation', row['id'], '已经入账，不重复处理')
                return False
            if row['platform_id'] is None:
                self.db.execute('UPDATE payments SET platform_id=? WHERE id=?', (evidence.platform_id, row['id']))
                row['platform_id'] = evidence.platform_id
            if stale:
                self._review(row, '已恢复或处置的旧任务响应，只留证据，不自动入账', action='payment_stale_response')
                return False
            if current['kind'] == 'create':
                if row['status'] != 'creating':
                    self._review(row, '创建响应与当前本地状态冲突')
                elif observed_at >= row['expires_at'] or self.s.clock() >= row['expires_at']:
                    self._review(row, '创建响应在本地截止后到达或处理；不发布链接，需核实上游订单', action='payment_create_after_deadline')
                elif observed_at < row['created_at'] or self.s.clock() < observed_at:
                    self._review(row, '本地时间顺序异常；不发布链接')
                elif (type(evidence.status) is not int or evidence.status != 0
                        or type(evidence.pay_url) is not str or not 0 < len(evidence.pay_url) <= 512):
                    self._review(row, '创建响应未通过核实')
                else:
                    self.db.execute("UPDATE payments SET status='pending',pay_url=?,reason='' WHERE id=?", (evidence.pay_url, row['id']))
                    self.s.audit(0, 'payment_created', row['id'], '模拟创建核实；截止时间不延长')
                    self.s.note(row['user_id'], f'【离线模拟，无实际收款】充值单 {row["id"]}，{row["amount"]} USDT，剩余 {row["expires_at"] - self.s.clock()} 秒本地有效期。/check {row["id"]}')
            else:
                self._apply_check(row, evidence, observed_at)
            return True

    @staticmethod
    def _safe_evidence(evidence):
        if not isinstance(evidence, Evidence):
            return json.dumps({'invalid_evidence_type': True})
        record = {}
        for field in fields(evidence):
            if field.name == 'pay_url':  # No tokens/opaque link contents in evidence logs.
                continue
            value = getattr(evidence, field.name)
            if type(value) is str:
                value = value[:512]
            elif type(value) is int and abs(value) > MAX_ID:
                value = '<out-of-range>'
            elif value is not None and type(value) not in {int, bool}:
                value = '<invalid-type>'
            record[field.name] = value
        return json.dumps(record, ensure_ascii=True, sort_keys=True)

    @staticmethod
    def _identity(value):
        return type(value) is str and 0 < len(value) <= 128 and bool(value.strip())

    def _reserve_identities(self, row, platform, transaction):
        if identity_conflicts(self.db.connection, row['id']):
            return False
        identities = [('platform', platform)] + ([('transaction', transaction)] if transaction else [])
        for kind, value in identities:
            owner = self.db.one('SELECT payment_id FROM payment_identity_claims WHERE kind=? AND value=?', (kind, value))
            previous = self.db.one('SELECT value FROM payment_identity_claims WHERE payment_id=? AND kind=?', (row['id'], kind))
            if (owner and owner['payment_id'] != row['id']) or (previous and previous['value'] != value):
                return False
            if kind == 'platform' and self.db.one('SELECT id FROM payments WHERE platform_id=? AND id!=?', (value, row['id'])):
                return False  # Includes pre-migration records without a claim row.
            if kind == 'transaction':
                paid = self.db.one('SELECT payment_id FROM payment_transactions WHERE transaction_id=?', (value,))
                if paid and paid['payment_id'] != row['id']:
                    return False
        for kind, value in identities:
            self.db.execute('INSERT OR IGNORE INTO payment_identity_claims(kind,value,payment_id) VALUES (?,?,?)', (kind, value, row['id']))
        return True

    def _bound(self, row, evidence):
        return (self._authenticated(row, evidence)
                and self._identity(evidence.platform_id)
                and (row['platform_id'] is None or row['platform_id'] == evidence.platform_id))

    def _authenticated(self, row, evidence):
        return (isinstance(evidence, Evidence) and evidence.verified is True
                and evidence.merchant == self.gateway.merchant
                and evidence.unique_id == row['id'])

    def _review(self, row, reason, status='review', action=None):
        if row['status'] == 'paid':
            self.s.audit(0, 'payment_duplicate_observation', row['id'], reason)
            return
        if row['status'] in {'closed', 'closed_review'}:
            status = 'closed_review'
        self.db.execute('UPDATE payments SET status=?,pay_url=NULL,reason=? WHERE id=?', (status, reason, row['id']))
        self.s.audit(0, action or 'payment_' + status, row['id'], reason)
        self.s.note(row['user_id'], f'充值单 {row["id"]} 待人工核实，未入账。{reason}')

    def _apply_check(self, row, evidence, observed_at):
        if type(evidence.status) is int and evidence.status == 0:
            if row['status'] in {'closed', 'closed_review'}:
                self.s.audit(0, 'payment_closed_pending_observation', row['id'], '本地关闭保持；未付状态不证明上游已取消')
            elif row['status'] in {'unknown', 'review'}:
                self._review(row, '先前结果不明，需人工核实')
            elif observed_at >= row['expires_at']:
                self._expire(row, '本地窗口结束，当前观察为未付；不代表上游取消')
            return
        amount_ok = (type(evidence.amount) in {str, int}
                     and len(str(evidence.amount)) <= 32
                     and re.fullmatch(r'[0-9]{1,7}(?:\.[0-9]{1,8})?', str(evidence.amount))
                     and Decimal(str(evidence.amount)) == row['amount'])
        complete = (type(evidence.status) is int and evidence.status == 1
                    and type(evidence.payer) is int and evidence.payer == row['user_id']
                    and evidence.coin == 'USDT' and amount_ok
                    and self._identity(evidence.transaction_id))
        if not complete:
            self._review(row, '付款人、金额、币种、已付状态或结算交易身份需人工核实')
            return
        if row['status'] in {'closed', 'closed_review'}:
            self._review(row, '本地关闭后收到已付证据，另行人工核实；不恢复自动入账')
            return
        if type(evidence.paid_at) is not int or not row['created_at'] <= evidence.paid_at < row['expires_at']:
            self._review(row, '实际付款时间缺失或超出本地窗口，需要人工核实')
            return
        if not row['created_at'] <= evidence.paid_at <= observed_at <= self.s.clock():
            self._review(row, '付款时间与本地观察时间顺序无法核实')
            return
        if observed_at >= row['expires_at']:
            self._review(row, '截止后首次可信发现已付，仅转人工核实，不推断实际付款时间')
            return
        if row['status'] != 'pending':
            self._review(row, '先前已进入非待付状态，不能自动入账')
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

    def close_no_credit(self, actor, payment, reason):
        self.s.admin(actor)
        reason = bounded_text(reason, 500)
        with self.db.atomic():
            row = self._get(payment)
            if row['status'] == 'closed':
                return False
            if row['status'] not in {'unknown', 'review', 'expired', 'closed_review'}:
                raise RuleError('仅可关闭待核实或本地过期单；不得更改已入账或执行中的订单。')
            if self.db.one("SELECT id FROM payment_jobs WHERE payment_id=? AND status='sending'", (payment,)):
                raise RuleError('支付任务仍在执行，须完成或恢复后再核实。')
            self.db.execute("UPDATE payment_jobs SET status='cancelled' WHERE payment_id=? AND status='pending'", (payment,))
            self.db.execute("UPDATE payments SET status='closed',pay_url=NULL,reason=? WHERE id=?", (reason, payment))
            self.s.audit(actor, 'payment_closed_no_credit', payment, reason)
            self.s.note(row['user_id'], f'充值单 {payment} 经人工核实已本地关闭，未加分、未退款；不代表上游取消。')
            return True
