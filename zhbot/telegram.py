"""Minimal Telegram Bot API client. TLS verification on, no automatic send retries."""
import json
from urllib.error import HTTPError
import urllib.request

class TelegramError(RuntimeError):
    pass

class TelegramRejected(TelegramError):
    def __init__(self, code, retry_after=None, reason=None):
        super().__init__('Telegram 明确拒绝请求')
        self.code = code
        self.retry_after = retry_after
        self.reason = reason

    @classmethod
    def from_response(cls, result):
        if not isinstance(result, dict) or result.get('ok') is not False:
            return None
        code = result.get('error_code')
        if type(code) is not int or code not in (400, 403, 429):
            return None
        params = result.get('parameters')
        delay = params.get('retry_after') if isinstance(params, dict) else None
        if code == 429:
            delay = delay if type(delay) is int and 0 < delay <= 86400 else 60
        description = result.get('description')
        description = description.lower().replace('_', ' ') if isinstance(description, str) else ''
        reason = None
        if code == 400:
            if 'message is not modified' in description:
                reason = 'not_modified'
            elif 'custom emoji' in description or 'button icon' in description:
                reason = 'custom_emoji'
        return cls(code, delay if code == 429 else None, reason)

class Telegram:
    def __init__(self,token):
        if not token: raise ValueError('BOT_TOKEN is required')
        self._base='https://api.telegram.org/bot'+token+'/'
    def call(self,method,**data):
        req=urllib.request.Request(self._base+method,data=json.dumps(data).encode(),headers={'Content-Type':'application/json'},method='POST')
        try:
            with urllib.request.urlopen(req,timeout=45) as response:
                result=json.load(response)
        except HTTPError as error:
            try:
                with error:
                    failure = TelegramRejected.from_response(json.loads(error.read(65536)))
            except Exception:
                failure = None
            if failure and failure.code == error.code:
                raise failure from None
            raise TelegramError('Telegram 请求失败，结果可能不确定') from None
        except Exception:
            # urllib errors may include the URL and hence token. Do not propagate them.
            raise TelegramError('Telegram 请求失败，结果可能不确定') from None
        if result.get('ok') is not True:
            raise TelegramRejected.from_response(result) or TelegramError('Telegram 未确认请求成功')
        return result['result']
