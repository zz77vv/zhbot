"""Synthetic server fixtures; public official test token, never real credentials.

This reference signer is only for simulated responses. Compatibility is proved
separately with the three literal official signatures, not with this helper.
"""
import hashlib
import hmac
import json

TOKEN = 'TESTtoken123456789abcdefghijABCD'
PAYMENT = {'id': 'a' * 32, 'user_id': 101, 'amount': 3, 'platform_id': 'abc123def456'}
VECTOR_B = {'status': 'success', 'code': 200, 'data': {
    'order_id': 'abc123def456', 'unique_id': 'ORDER-20260628-001',
    'pay_user_id': 123456789, 'amount': '100.5', 'coin': 'USDT',
    'status': 1, 'type': 'deposit'}, 'id': 10001,
    'sign': '64B09C8847849FA6921D8FFBDF8E406D4A8EA623E53970712350F61783403F7D'}


def response(data, *, merchant=10001, status='success', code=200):
    """Independent tiny server fixture, not downloaded SDK or production signer."""
    payload = {'status': status, 'code': code, 'data': data, 'id': merchant}
    pairs = []
    for key, value in payload.items():
        if key == 'data':
            pairs.extend(('data.' + k, str(v).lower() if type(v) is bool else str(v))
                         for k, v in value.items() if v is not None and v != '')
        else:
            pairs.append((key, str(value)))
    base = '&'.join(k + '=' + v for k, v in sorted(pairs))
    payload['sign'] = hmac.new(TOKEN.encode(), base.encode(), hashlib.sha256).hexdigest().upper()
    return json.dumps(payload)


def deposit(payment=PAYMENT, **changes):
    data = {'order_id': payment['platform_id'], 'unique_id': payment['id'],
            'pay_user_id': payment['user_id'], 'amount': str(payment['amount']),
            'coin': 'USDT', 'status': 1}
    data.update(changes)
    return data
