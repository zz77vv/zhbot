"""Payment accounting boundary, NOT an implementation of the unverified OKPay API.

An official SDK adapter must implement create(order) -> HTTPS URL and
verify_query(order) -> verified evidence. verify_query MUST authenticate the
server response using the official protocol before returning any evidence.
No such adapter is included or enabled until the official contract is obtained.
"""
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse
from .store import DomainError, uid

class PaymentUnavailable(DomainError):
    pass

class Payments:
    def __init__(self,store,gateway,merchant):
        self.s,self.gateway,self.merchant=store,gateway,merchant
    def create(self,user,amount):
        if not self.gateway: raise PaymentUnavailable('缺少已核验的 OKPay 官方 SDK 适配器，未开放收款')
        if type(amount)!=int or amount<1 or amount>1_000_000_000: raise DomainError('充值仅支持 1 至 1000000000 整数 USDT')
        order={'id':uid(),'user_id':user,'amount':amount,'currency':'USDT','merchant':self.merchant}
        with self.s.tx() as c:
            c.execute('INSERT INTO payments(id,user_id,amount,merchant) VALUES(?,?,?,?)',(order['id'],user,amount,self.merchant))
        try:
            url=self.gateway.create(order)
            parsed=urlparse(url)
            if parsed.scheme!='https' or not parsed.hostname or parsed.username: raise PaymentUnavailable('支付链接无效')
            with self.s.tx() as c: c.execute("UPDATE payments SET url=?,state='pending' WHERE id=?",(url,order['id']))
            return dict(order,url=url)
        except Exception:
            with self.s.tx() as c: c.execute("UPDATE payments SET state='uncertain' WHERE id=?",(order['id'],))
            raise PaymentUnavailable('创建支付链接失败，待商户后台核实；未增加积分') from None
    def check(self,user,pid):
        if not self.gateway: raise PaymentUnavailable('支付服务未开放')
        order=self.s.one('SELECT * FROM payments WHERE id=? AND user_id=?',(pid,user))
        if order['merchant']!=self.merchant: raise DomainError('商户配置变更，需人工核实')
        if order['state']=='credited': return '此充值已入账，请勿重复支付'
        evidence=self.gateway.verify_query(order)
        if evidence.get('paid') is not True: return '尚未确认到账，请稍后检查支付状态'
        try: amount=Decimal(str(evidence['amount']))
        except (KeyError,InvalidOperation): raise DomainError('支付金额无效') from None
        if (not amount.is_finite() or amount!=order['amount'] or evidence.get('currency')!='USDT'
            or evidence.get('merchant')!=order['merchant'] or evidence.get('order_id')!=pid
            or not isinstance(evidence.get('platform_id'),str) or not evidence['platform_id'].strip()):
            raise DomainError('支付归属、金额、币种或交易号不匹配，拒绝入账')
        with self.s.tx() as c:
            # Re-read under the money lock: concurrent status checks cannot credit twice.
            current=c.execute('SELECT * FROM payments WHERE id=?',(pid,)).fetchone()
            if current['state']=='credited': return '此充值已入账'
            used=c.execute('SELECT id FROM payments WHERE platform_id=?',(evidence['platform_id'],)).fetchone()
            if used and used['id']!=pid: raise DomainError('平台交易已用于其他充值')
            c.execute("UPDATE payments SET state='credited',platform_id=? WHERE id=?",(evidence['platform_id'],pid))
            self.s.money(c,order['user_id'],order['amount'],0,'payment:'+pid,'OKPay查单入账')
        return f"已入账 {order['amount']} 积分"
