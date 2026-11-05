"""Small HTTPS Bot API adapter. No live request runs during import or tests."""
import json
import re
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .core import RuleError, item_values, parse_uint


class TelegramError(Exception):
    def __init__(self, message, *, definite=False, retry_after=None, fatal=False):
        super().__init__(message)
        self.definite = definite
        self.retry_after = retry_after
        self.fatal = fatal


class CallbackUnavailable(TelegramError):
    """A consistent, recognized rejection of an obsolete/unanswerable ACK.

    This says nothing about whether the already-persisted action succeeded,
    or whether the callback was expired versus its query ID being invalid.
    """


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Telegram:
    MAX_RESPONSE = 8 * 1024 * 1024

    def __init__(self, token, opener=None):
        # Preserve real-token validation; managed handles instead use one
        # bounded, visible-ASCII transport segment, never a credential claim.
        real_token = type(token) is str and re.fullmatch(r'[0-9]{5,20}:[A-Za-z0-9_-]{20,100}', token)
        opaque_handle = (type(token) is str and 1 <= len(token) <= 64
                         and all(33 <= ord(c) <= 126 and c not in '/\\?#%' for c in token))
        if not (real_token or opaque_handle):
            raise RuleError('TELEGRAM_BOT_TOKEN 格式无效。')
        # The literal 'bot' prefix ensures even a '.'/'..' handle is never a
        # dot segment. Slash/backslash/percent/query/fragment cannot enter it.
        self._path_prefix = '/bot' + token + '/'
        self._base = 'https://api.telegram.org' + self._path_prefix
        # Keep proxy settings and default TLS certificate verification; forbid redirects.
        self._open = opener or build_opener(NoRedirect()).open

    def call(self, method, params, timeout=40):
        if method not in {'sendMessage', 'sendPhoto', 'sendDocument', 'getUpdates', 'getMe', 'getWebhookInfo', 'answerCallbackQuery'}:
            raise RuleError('不支持的 Telegram 操作。')
        try:
            target = self._base + method
            request = Request(target, data=json.dumps(params, ensure_ascii=True).encode(),
                              headers={'Content-Type': 'application/json'}, method='POST')
            # Do not quote, strip, unwrap or normalize a proxy handle. Fail
            # closed if the URL library changes the literal route or origin.
            if (request.full_url != target or request.type != 'https' or request.host != 'api.telegram.org'
                    or request.selector != self._path_prefix + method):
                raise TelegramError('Telegram 请求目标无效；未发起请求。')
            http_error = None
            try:
                response = self._open(request, timeout=timeout)
            except HTTPError as error:
                http_error = error.code
                response = error
            with response:
                raw = response.read(self.MAX_RESPONSE + 1)
            if len(raw) > self.MAX_RESPONSE:
                raise TelegramError('Telegram 响应过大，结果未知。')
            body = json.loads(raw)
            if type(body) is not dict or type(body.get('ok')) is not bool:
                raise TelegramError('Telegram 响应无效，结果未知。')
            if http_error is not None and body['ok'] is True:
                raise TelegramError('Telegram HTTP 状态与响应冲突，结果未知。')
            if body['ok'] is False:
                code = body.get('error_code')
                if http_error is not None and (type(code) is not int or code != http_error):
                    raise TelegramError('Telegram HTTP 状态与错误码冲突，结果未知。')
                if (method == 'answerCallbackQuery' and http_error == 400 and type(code) is int and code == 400
                        and body.get('description') == 'Bad Request: query is too old and response timeout expired or query ID is invalid'):
                    raise CallbackUnavailable('回调提示已不可确认；不代表业务未执行。', definite=True)
                definite = type(code) is int and code in {400, 403, 429}
                retry = None
                if code == 429:
                    parameters = body.get('parameters')
                    candidate = parameters.get('retry_after') if type(parameters) is dict else None
                    retry = candidate if type(candidate) is int and 1 <= candidate <= 3600 else 60
                raise TelegramError('Telegram 明确拒绝请求。' if definite else 'Telegram 请求未确认，结果未知。',
                                    definite=definite, retry_after=retry, fatal=code in {401, 409})
            return body.get('result')
        except TelegramError:
            raise
        except Exception:
            # Never include raw urllib errors: they can embed token-bearing URLs.
            raise TelegramError('Telegram 连接或响应异常，结果未知。') from None

    def send(self, message):
        kind, content, caption = item_values(message['kind'], message['content'], message.get('caption', ''))
        params = {'chat_id': parse_uint(message['chat_id']), 'protect_content': True}
        method, field = {'text': ('sendMessage', 'text'), 'photo': ('sendPhoto', 'photo'),
                         'document': ('sendDocument', 'document')}[kind]
        params[field] = content
        if kind != 'text' and caption:
            params['caption'] = caption
        markup = message.get('reply_markup')
        if markup is not None:
            if type(markup) is not dict or type(markup.get('inline_keyboard')) is not list:
                raise RuleError('按钮格式无效。')
            params['reply_markup'] = markup
        result = self.call(method, params)
        if type(result) is not dict or type(result.get('message_id')) is not int or result['message_id'] <= 0:
            raise TelegramError('Telegram 没有返回有效发送编号，结果未知。')
        return result['message_id']

    def answer_callback(self, query, text):
        """Return ACK availability only; False never means the action failed."""
        if type(query) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', query):
            raise RuleError('回调编号无效。')
        try:
            result = self.call('answerCallbackQuery', {'callback_query_id': query, 'text': text})
        except CallbackUnavailable:
            return False  # Terminal ACK outcome only, never action success/failure.
        if result is not True:
            raise TelegramError('回调确认结果未知。')
        return True

    def get_updates(self, offset):
        result = self.call('getUpdates', {'offset': offset, 'timeout': 25, 'limit': 50,
                                          'allowed_updates': ['message', 'callback_query']})
        if (type(result) is not list or any(type(x) is not dict or type(x.get('update_id')) is not int
                                           or x['update_id'] < 0 for x in result)):
            raise TelegramError('Telegram 更新列表无效。')
        return result

    def identity(self):
        result = self.call('getMe', {})
        if type(result) is not dict or result.get('is_bot') is not True:
            raise TelegramError('无法确认机器人身份。', fatal=True)
        return parse_uint(result.get('id'))

    def has_webhook(self):
        result = self.call('getWebhookInfo', {})
        if type(result) is not dict or type(result.get('url')) is not str:
            raise TelegramError('无法确认 webhook 状态。', fatal=True)
        return bool(result['url'])
