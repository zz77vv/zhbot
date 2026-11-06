"""Explicit payment configuration, with charging disabled by default."""
import os
from .okpay import OKPayGateway
from .payments import Payments, PaymentUnavailable


def build_payments(store, env=None):
    env = os.environ if env is None else env
    enabled = env.get('OKPAY_ENABLED', 'false').strip().lower()
    if enabled in ('false', '0', ''):
        return None
    if enabled not in ('true', '1'):
        raise PaymentUnavailable('OKPAY_ENABLED 必须为 true/false')
    merchant = env.get('OKPAY_MERCHANT_ID', '').strip()
    token = env.get('OKPAY_MERCHANT_TOKEN', '')
    gateway = OKPayGateway(merchant, token)
    return Payments(store, gateway, merchant)
