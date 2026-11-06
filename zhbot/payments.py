"""Local payment ownership and atomic point accounting; gateway verifies evidence."""
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse
from .store import DomainError, uid


class PaymentUnavailable(DomainError):
    pass


class Payments:
    def __init__(self, store, gateway, merchant):
        self.s, self.gateway, self.merchant = store, gateway, merchant

    def recent(self, user, page=0):
        if type(page) is not int or page < 0 or page > 100000: raise DomainError('页码无效')
        return self.s.rows('SELECT * FROM payments WHERE user_id=? ORDER BY rowid DESC LIMIT 10 OFFSET ?', (user, page*10))

    def _record(self, c, pid, result, platform=None, digest=''):
        c.execute('INSERT INTO payment_checks(payment_id,result,platform_id,verification_hash) VALUES(?,?,?,?)',
                  (pid, result, platform, digest))

    def create(self, user, amount):
        if not self.gateway:
            raise PaymentUnavailable('OKPay 尚未配置，未开放充值')
        if type(amount) != int or amount < 1 or amount > 1_000_000_000:
            raise DomainError('充值仅支持 1 至 1000000000 整数 USDT')
        order = {'id': uid(), 'user_id': user, 'amount': amount,
                 'currency': 'USDT', 'merchant': self.merchant}
        with self.s.tx() as c:
            existing = c.execute("SELECT * FROM payments WHERE user_id=? AND amount=? AND merchant=? AND state IN ('created','pending','uncertain') ORDER BY rowid DESC LIMIT 1",
                                 (user, amount, self.merchant)).fetchone()
            if existing:
                if existing['url'] and existing['state'] == 'pending':
                    return dict(existing)
                raise PaymentUnavailable('原充值单 '+existing['id']+' 创建结果待核实，请检查其支付状态，勿重复创建')
            c.execute('INSERT INTO payments(id,user_id,amount,merchant) VALUES(?,?,?,?)',
                      (order['id'], user, amount, self.merchant))
        try:
            result = self.gateway.create(order)
            parsed = urlparse(result['url'])
            platform = result['platform_id']
            if parsed.scheme != 'https' or not parsed.hostname or parsed.username or not isinstance(platform, str) or not platform:
                raise PaymentUnavailable('支付链接或平台单号无效')
            with self.s.tx() as c:
                current = c.execute('SELECT * FROM payments WHERE id=?', (order['id'],)).fetchone()
                if current['platform_id'] and current['platform_id'] != platform:
                    raise PaymentUnavailable('平台单号绑定冲突')
                used = c.execute('SELECT id FROM payments WHERE platform_id=? AND id!=?', (platform, order['id'])).fetchone()
                if used:
                    raise PaymentUnavailable('平台单号已绑定其他充值订单')
                # A concurrent check may already have credited this order.
                c.execute("UPDATE payments SET url=?,platform_id=?,state=CASE WHEN state='credited' THEN state ELSE 'pending' END WHERE id=?",
                          (result['url'], platform, order['id']))
                self._record(c, order['id'], 'created', platform, result.get('verification_hash', ''))
            return dict(order, url=result['url'], platform_id=platform)
        except Exception:
            with self.s.tx() as c:
                c.execute("UPDATE payments SET state='uncertain' WHERE id=? AND state!='credited'", (order['id'],))
                self._record(c, order['id'], 'create_uncertain')
            raise PaymentUnavailable('创建支付链接未确认，原充值单 '+order['id']+' 可在充值菜单检查状态；未增加积分') from None

    def attach_link(self, actor, pid, platform, url, reason):
        self.s.admin(actor)
        try: parsed = urlparse(url)
        except ValueError: raise DomainError('链接格式错误') from None
        if (not reason.strip() or parsed.scheme != 'https' or not parsed.hostname
            or parsed.username or parsed.password or any(c.isspace() for c in url)):
            raise DomainError('需提供安全HTTPS支付链接及商户后台核实原因')
        with self.s.tx() as c:
            order = c.execute('SELECT * FROM payments WHERE id=?', (pid,)).fetchone()
            if (not order or order['merchant'] != self.merchant or order['state'] != 'pending'
                or not order['platform_id'] or order['platform_id'] != platform):
                raise DomainError('先可信查单确认原商户平台单号；仅待付订单允许恢复链接')
            if order['url']: raise DomainError('订单已有支付链接，不允许覆盖')
            c.execute('UPDATE payments SET url=? WHERE id=?', (url, pid))
            # Never confirm payment or change the customer's balance here.
            self.s.audit(c, actor, 'payment_link', pid, reason)
            self._record(c, pid, 'link_restored_by_admin', platform)

    def check(self, user, pid):
        if not self.gateway:
            raise PaymentUnavailable('支付服务未开放')
        order = self.s.one('SELECT * FROM payments WHERE id=? AND user_id=?', (pid, user))
        if order['merchant'] != self.merchant:
            raise DomainError('商户配置变更，需人工核实')
        if order['state'] == 'credited':
            return '此充值已入账，请勿重复支付'
        try:
            evidence = self.gateway.verify_query(order)
            platform = evidence.get('platform_id')
            if (evidence.get('merchant') != order['merchant'] or evidence.get('order_id') != pid
                or not isinstance(platform, str) or not platform.strip()
                or (order['platform_id'] and order['platform_id'] != platform)):
                raise DomainError('支付商户或订单绑定不匹配，拒绝入账')
            try:
                amount = Decimal(str(evidence['amount']))
            except (KeyError, InvalidOperation):
                raise DomainError('支付金额无效') from None
            if not amount.is_finite() or amount != order['amount'] or evidence.get('currency') != 'USDT':
                raise DomainError('支付金额或币种不匹配，拒绝入账')
            if type(evidence.get('paid')) is not bool:
                raise DomainError('支付状态无效')
            with self.s.tx() as c:
                current = c.execute('SELECT * FROM payments WHERE id=?', (pid,)).fetchone()
                if current['state'] == 'credited':
                    return '此充值已入账'
                if current['platform_id'] and current['platform_id'] != platform:
                    raise DomainError('平台订单绑定已变更，拒绝入账')
                used = c.execute('SELECT id FROM payments WHERE platform_id=? AND id!=?', (platform, pid)).fetchone()
                if used:
                    raise DomainError('平台交易已用于其他充值')
                state = 'credited' if evidence['paid'] else 'pending'
                c.execute('UPDATE payments SET state=?,platform_id=? WHERE id=?', (state, platform, pid))
                if evidence['paid']:
                    self.s.money(c, order['user_id'], order['amount'], 0, 'payment:'+pid, 'OKPay查单入账')
                    self.s.event(c, 'credited:'+pid, 'credited', order['user_id'], {'payment':pid,'points':order['amount']})
                self._record(c, pid, state, platform, evidence.get('verification_hash', ''))
        except Exception:
            with self.s.tx() as c:
                self._record(c, pid, 'query_unverified')
            raise
        return f"已入账 {order['amount']} 积分" if evidence['paid'] else '尚未确认到账，请稍后检查支付状态'
