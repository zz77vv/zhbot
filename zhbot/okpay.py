"""Offline subset of docs.okaypay.me/doc.html (retrieved 2026-11-05).

No HTTP client, credential loading, activation switch, callback server or live
payment gateway. See docs/OKPAY_VERIFICATION.md for supported canonicalization
and protocol gaps. Authenticated evidence is not permission to credit a ledger.
"""
from dataclasses import dataclass
import hashlib
import hmac
import json
import re
from urllib.parse import urlencode, urlsplit

from .core import MAX_ID, MAX_POINTS, RuleError
from .payments import DisabledGateway, Evidence


class ProtocolError(RuleError):
    """Untrusted, unsupported or ambiguous input; never include raw payloads."""


def _reject():
    raise ProtocolError('OKPay 离线协议校验失败；需人工核实。')


class _Number(str):
    """JSON number lexeme, retained verbatim for signing, including exponent."""


def build_base(params):
    """Official scalar/object subset; reject arrays and ambiguous key/value shapes.

    Values are never URL encoded here. An equals sign within a value is safe;
    ampersands could inject another key=value pair and are rejected. Dotted
    source keys are rejected so object flattening cannot overwrite a leaf.
    """
    if type(params) is not dict:
        _reject()
    flat = {}
    count = 0

    def flatten(obj, prefix='', depth=0):
        nonlocal count
        if depth > 8:
            _reject()
        for key, value in obj.items():
            count += 1
            if count > 256 or type(key) is not str or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', key):
                _reject()
            if not prefix and key == 'sign':
                continue
            name = prefix + key
            if type(value) is dict:
                flatten(value, name + '.', depth + 1)
                continue
            if value is None or (isinstance(value, str) and value == ''):
                continue
            if type(value) is bool:
                value = 'true' if value else 'false'
            elif type(value) is int:
                if abs(value) > 10 ** 100:
                    _reject()
                value = str(value)
            elif type(value) not in {str, _Number}:
                _reject()
            if len(value) > 4096 or '&' in value:
                _reject()
            flat[name] = value

    flatten(params)
    base = '&'.join(key + '=' + flat[key] for key in sorted(flat))
    try:
        if len(base.encode('utf-8')) > 65536:
            _reject()
    except UnicodeError:
        _reject()
    return base


def sign(params, token):
    if type(token) is not str or not token or len(token) > 4096:
        _reject()
    try:
        return hmac.new(token.encode('utf-8'), build_base(params).encode('utf-8'),
                        hashlib.sha256).hexdigest().upper()
    except UnicodeError:
        _reject()


def verify(payload, token):
    if type(payload) is not dict:
        return False
    signature = payload.get('sign')
    if type(signature) is not str or not re.fullmatch(r'[0-9A-Fa-f]{64}', signature):
        return False
    try:
        return hmac.compare_digest(sign(payload, token), signature.upper())
    except ProtocolError:
        return False


def load_payload(raw):
    """Read bounded UTF-8 JSON, reject duplicate keys, preserve numeric spelling."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                _reject()
            result[key] = value
        return result

    def invalid_constant(_):
        _reject()

    if type(raw) not in {str, bytes} or len(raw) > 65536:
        _reject()
    try:
        if type(raw) is bytes:
            raw = raw.decode('utf-8')
        if len(raw.encode('utf-8')) > 65536:
            _reject()
        payload = json.loads(raw, parse_int=_Number, parse_float=_Number,
                             parse_constant=invalid_constant, object_pairs_hook=pairs)
        build_base(payload)  # Also bounds depth, shape, strings and canonical keys.
        return payload
    except (ValueError, UnicodeError, RecursionError):
        _reject()


def _integer(value, *, maximum=MAX_ID, minimum=1, strings=True):
    allowed = {int, _Number, str} if strings else {int, _Number}
    if type(value) not in allowed:
        _reject()
    text = str(value)
    if len(text) > 20 or not re.fullmatch(r'[0-9]+', text):
        _reject()
    number = int(text)
    if not minimum <= number <= maximum:
        _reject()
    return number


def _identity(value):
    if type(value) is not str or not 0 < len(value) <= 128 or value.strip() != value:
        _reject()
    if any(ord(c) < 33 or ord(c) > 126 for c in value) or '&' in value:
        _reject()
    return value


@dataclass(frozen=True)
class PreparedRequest:
    """Offline POST form description; does not contain a destination or token."""
    path: str
    body: bytes


class OfflineOKPayAdapter(DisabledGateway):
    """Disabled gateway plus explicit offline preparation/parsing operations.

    All token inputs are explicit; the application never constructs this class.
    create/check remain disabled. parse_create requires an explicitly correlated
    request context, since payLink does not echo unique_id. It authenticates a
    platform order, not a payer, amount, remote expiry or settlement time.
    """
    def __init__(self, shop_id, token):
        self.merchant = str(_integer(shop_id))
        if type(token) is not str or not token or len(token) > 4096:
            _reject()
        self._token = token

    def _prepare(self, path, params, timestamp, nonce):
        timestamp = _integer(timestamp, minimum=0)
        if type(nonce) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{8,128}', nonce):
            _reject()
        # Freshness/uniqueness are the caller's responsibility; no network or
        # clock is consulted here. Never reuse a prepared request for a retry.
        params = dict(params, id=self.merchant, timestamp=str(timestamp), nonce=nonce)
        params['sign'] = sign(params, self._token)
        return PreparedRequest(path, urlencode(params).encode('ascii'))

    def prepare_create(self, payment, *, timestamp, nonce):
        amount = _integer(payment['amount'], maximum=MAX_POINTS)
        return self._prepare('/shop/payLink', {'amount': str(amount), 'coin': 'USDT',
                             'unique_id': _identity(payment['id'])}, timestamp, nonce)

    def prepare_query(self, payment, *, timestamp, nonce):
        if payment.get('platform_id') is not None:
            path, params = '/shop/checkTransferByTxid', {'txid': _identity(payment['platform_id'])}
        else:
            path, params = '/shop/checkDeposit', {'unique_id': _identity(payment['id'])}
        return self._prepare(path, params, timestamp, nonce)

    def _envelope(self, raw):
        payload = load_payload(raw)
        # warning/error is unsigned and can never establish payment facts.
        if payload.get('status') != 'success' or not verify(payload, self._token):
            _reject()
        if (_integer(payload.get('code'), strings=False) != 200
                or str(_integer(payload.get('id'))) != self.merchant
                or type(payload.get('data')) is not dict):
            _reject()
        return payload['data']

    def parse_create(self, raw, payment):
        data = self._envelope(raw)
        platform = _identity(data.get('order_id'))
        unique = _identity(payment['id'])
        try:
            status = _integer(data.get('status'), minimum=0, strings=False)
            if status != 0:
                _reject()
            url = data.get('pay_url')
            if type(url) is not str or len(url) > 512 or any(ord(c) < 33 for c in url):
                _reject()
            parts = urlsplit(url)
            if (parts.scheme != 'https' or parts.netloc != 't.me' or parts.path != '/okpay'
                    or not parts.query or parts.fragment):
                _reject()
        except ValueError:  # Includes ProtocolError; authenticated identity survives.
            status, url = None, None
        return Evidence(True, self.merchant, unique, platform, status, pay_url=url)

    def _deposit(self, raw, payment, *, callback):
        data = self._envelope(raw)
        unique = _identity(data.get('unique_id'))
        platform = _identity(data.get('order_id'))
        if unique != _identity(payment['id']):
            _reject()
        if (callback and data.get('type') != 'deposit') or data.get('type', 'deposit') != 'deposit':
            _reject()
        try:
            status = _integer(data.get('status'), minimum=0, strings=False)
            if callback and status != 1:
                _reject()
            payer = data.get('pay_user_id')
            if payer is not None:
                payer = _integer(payer)
            amount = data.get('amount')
            if amount is not None:
                if type(amount) not in {str, _Number} or not re.fullmatch(r'[0-9]{1,7}(?:\.[0-9]{1,8})?', amount):
                    _reject()
                amount = str(amount)
            coin = data.get('coin')
            if coin is not None and (type(coin) is not str or not re.fullmatch(r'[A-Z]{1,16}', coin)):
                _reject()
        except ProtocolError:
            # The financial facts cannot be used, but the authenticated order
            # identity must still reach durable quarantine before review.
            status = payer = amount = coin = None
        # A valid HMAC authenticates facts; it does not validate business
        # matches. There is no documented actual payment time or independent
        # settlement identity. None is internal unavailability, not remote status.
        return Evidence(True, self.merchant, unique, platform, status,
                        amount=amount, coin=coin, payer=payer)

    def parse_query(self, raw, payment):
        """Caller must retain trusted query endpoint/request/body association.

        HMAC authenticates the payload, not which endpoint returned it.
        """
        return self._deposit(raw, payment, callback=False)

    def parse_callback(self, raw, payment):
        return self._deposit(raw, payment, callback=True)
