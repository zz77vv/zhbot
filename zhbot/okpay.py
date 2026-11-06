"""OKPay merchant HTTP adapter using the uploaded signing SDK.

Protocol source: supplied OKPay_通用对接文档.docx, sections 1-6.
No callback or outbound transfer endpoints are exposed.
"""
import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode, urlparse
from urllib.request import Request, HTTPRedirectHandler, build_opener
from .payments import PaymentUnavailable
from .vendor import okpay_sign as sdk


class OKPayError(PaymentUnavailable):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise OKPayError('OKPay 接口返回重定向，拒绝发送签名请求')


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result or '.' in key:
            raise OKPayError('OKPay 响应包含重复或歧义字段')
        result[key] = value
    return result


def _scalar(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value):
        raise OKPayError('OKPay 响应字段类型不正确')
    return str(value)


def _status(value):
    status = _scalar(value)
    if status not in ('0', '1'):
        raise OKPayError('OKPay 支付状态不正确')
    return status


def _amount(value, expected):
    try:
        amount = Decimal(_scalar(value))
    except (InvalidOperation, ValueError):
        raise OKPayError('OKPay 金额不正确') from None
    if not amount.is_finite() or amount != expected:
        raise OKPayError('OKPay 金额与本地充值单不一致')


class OKPayGateway:
    BASE = 'https://api.okaypay.me/shop'
    MAX_RESPONSE = 256 * 1024

    def __init__(self, merchant, token, opener=None):
        if not re.fullmatch(r'[1-9][0-9]*', str(merchant)) or not token:
            raise OKPayError('OKPay 商户ID或密钥未配置')
        self.merchant, self._token = str(merchant), token
        self.opener = opener or build_opener(NoRedirect()).open

    def _request(self, endpoint, params):
        if endpoint not in ('payLink', 'checkDeposit', 'checkTransferByTxid'):
            raise OKPayError('不支持的商户接口')
        signed = sdk.signed_request(params, self.merchant, self._token)
        request = Request(self.BASE + '/' + endpoint,
                          data=urlencode(signed).encode('utf-8'), method='POST',
                          headers={'Content-Type': 'application/x-www-form-urlencoded',
                                   'Accept': 'application/json'})
        try:
            with self.opener(request, timeout=20) as response:
                if getattr(response, 'status', 200) != 200:
                    raise OKPayError('OKPay 未返回成功的HTTP状态')
                raw = response.read(self.MAX_RESPONSE + 1)
            if len(raw) > self.MAX_RESPONSE:
                raise OKPayError('OKPay 响应过大')
            # Preserve numeric lexemes for the SDK's full-response signature.
            body = json.loads(raw, parse_int=str, parse_float=str,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
                              object_pairs_hook=_object)
            if not isinstance(body, dict) or body.get('status') != 'success' or _scalar(body.get('code')) != '200':
                raise OKPayError('OKPay 返回错误状态，未确认付款')
            def validate_types(value):
                if isinstance(value, dict):
                    for item in value.values(): validate_types(item)
                elif value is not None and not isinstance(value, (str, bool)):
                    raise OKPayError('OKPay 响应字段类型不支持')
            validate_types(body)
            signature = body.get('sign')
            if not isinstance(signature, str) or not re.fullmatch(r'[A-Fa-f0-9]{64}', signature) or not sdk.verify(body, self._token):
                raise OKPayError('OKPay 响应验签失败，拒绝确认付款')
            if _scalar(body.get('id')) != self.merchant:
                raise OKPayError('OKPay 商户身份不匹配')
            data = body.get('data')
            if not isinstance(data, dict):
                raise OKPayError('OKPay 响应缺少订单数据')
            return data, hashlib.sha256(raw).hexdigest()
        except OKPayError:
            raise
        except Exception:
            # Do not leak request URLs, signed parameters, response bodies or secrets.
            raise OKPayError('OKPay 请求失败或结果不确定，请检查原充值订单') from None

    def _local(self, order):
        if order['merchant'] != self.merchant or order['currency'] != 'USDT':
            raise OKPayError('本地充值商户或币种配置不匹配')

    def create(self, order):
        self._local(order)
        data, digest = self._request('payLink', {'amount': str(order['amount']),
                                               'coin': 'USDT', 'unique_id': order['id'],
                                               'name': '积分充值'})
        platform = _scalar(data.get('order_id'))
        if _status(data.get('status')) != '0':
            raise OKPayError('创建支付链接返回非待支付状态，请核实原订单')
        if 'unique_id' in data and _scalar(data['unique_id']) != order['id']:
            raise OKPayError('建单商户订单号不匹配')
        url = _scalar(data.get('pay_url'))
        parsed = urlparse(url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or any(c.isspace() for c in url):
            raise OKPayError('OKPay 支付链接无效')
        return {'url': url, 'platform_id': platform, 'verification_hash': digest}

    def _match(self, data, order, platform=None, require_coin=False):
        actual = _scalar(data.get('order_id'))
        if _scalar(data.get('unique_id')) != order['id']:
            raise OKPayError('OKPay 商户订单号不匹配')
        if platform and actual != platform:
            raise OKPayError('OKPay 平台订单号不匹配')
        if order.get('platform_id') and actual != order['platform_id']:
            raise OKPayError('OKPay 平台订单与本地绑定不一致')
        _amount(data.get('amount'), order['amount'])
        if require_coin or 'coin' in data:
            if data.get('coin') != 'USDT':
                raise OKPayError('OKPay 收款币种不匹配')
        if 'type' in data and data['type'] != 'deposit':
            raise OKPayError('OKPay 响应不是充值交易')
        return actual, _status(data.get('status'))

    def verify_query(self, order):
        self._local(order)
        data, first_hash = self._request('checkDeposit', {'unique_id': order['id']})
        platform, state = self._match(data, order)
        evidence = {'merchant': self.merchant, 'order_id': order['id'],
                    'platform_id': platform, 'amount': str(order['amount']),
                    'currency': 'USDT', 'paid': False, 'verification_hash': first_hash}
        if state == '0':
            return evidence
        # checkDeposit does not promise coin. Query the signed platform transaction
        # to confirm USDT and the same pair of local/platform order identifiers.
        data, second_hash = self._request('checkTransferByTxid', {'txid': platform})
        _, state = self._match(data, order, platform, require_coin=True)
        if state != '1':
            raise OKPayError('两次查单付款状态不一致，保留待核实')
        evidence.update(paid=True, amount=_scalar(data['amount']),
                        verification_hash=hashlib.sha256((first_hash + second_hash).encode()).hexdigest())
        return evidence
