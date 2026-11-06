"""Minimal Telegram Bot API client. TLS verification on, no automatic send retries."""
import json
import urllib.request

class TelegramError(RuntimeError):
    pass

class Telegram:
    def __init__(self,token):
        if not token: raise ValueError('BOT_TOKEN is required')
        self._base='https://api.telegram.org/bot'+token+'/'
    def call(self,method,**data):
        req=urllib.request.Request(self._base+method,data=json.dumps(data).encode(),headers={'Content-Type':'application/json'},method='POST')
        try:
            with urllib.request.urlopen(req,timeout=45) as response:
                result=json.load(response)
        except Exception:
            # urllib errors may include the URL and hence token. Do not propagate them.
            raise TelegramError('Telegram 请求失败，结果可能不确定') from None
        if result.get('ok') is not True: raise TelegramError('Telegram 未确认请求成功')
        return result['result']
