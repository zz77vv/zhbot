"""OKPay 商户签名 SDK 

注意:金额等数字请按字符串处理,不要重新格式化(如 "100.5" 不要变成 100.50 / 100)。
"""

import hmac
import hashlib
import secrets
import time


def _flatten(data, prefix=""):
    out = {}
    for k, v in data.items():
        key = str(k) if prefix == "" else f"{prefix}.{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, key))
            continue
        # 注意:bool 必须在其它判断之前处理(Python 中 bool 是 int 的子类)
        if isinstance(v, bool):
            out[key] = "true" if v else "false"
            continue
        if v is None or v == "":
            continue
        out[key] = str(v)
    return out


def build_base(params: dict) -> str:
    """构造签名原文:去 sign、去 null/空串(保留 0/false)、点号展开嵌套、键名升序、k=v 用 & 原样拼接。"""
    data = {k: v for k, v in params.items() if k != "sign"}
    flat = _flatten(data)
    return "&".join(f"{k}={flat[k]}" for k in sorted(flat.keys()))


def sign(params: dict, token: str) -> str:
    """计算 HMAC-SHA256 签名(64 位大写十六进制)。"""
    base = build_base(params).encode("utf-8")
    return hmac.new(token.encode("utf-8"), base, hashlib.sha256).hexdigest().upper()


def signed_request(params: dict, shop_id, token: str) -> dict:
    """构造一个带 id/timestamp/nonce/sign 的完整请求参数。"""
    p = dict(params)
    p["id"] = shop_id
    p["timestamp"] = int(time.time())
    p["nonce"] = secrets.token_hex(8)  # 16 位随机串
    p["sign"] = sign(p, token)
    return p


def verify(payload: dict, token: str) -> bool:
    """验证服务端返回 / 异步回调(恒定时间比较)。"""
    got = payload.get("sign")
    if not isinstance(got, str):
        return False
    return hmac.compare_digest(sign(payload, token), got.upper())
