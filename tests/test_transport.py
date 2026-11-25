import io
import json
import unittest
from unittest.mock import patch
from zhbot.telegram import Telegram, TelegramError

class TransportTests(unittest.TestCase):
    def test_json_request_and_success(self):
        response=io.BytesIO(b'{"ok":true,"result":{"message_id":12}}')
        with patch('urllib.request.urlopen',return_value=response) as call:
            result=Telegram('TEST_ONLY').call('sendMessage',chat_id=1,text='中文')
        self.assertEqual(12,result['message_id'])
        request=call.call_args.args[0]
        self.assertEqual({'chat_id':1,'text':'中文'},json.loads(request.data))
        self.assertEqual(45,call.call_args.kwargs['timeout'])
    def test_errors_do_not_disclose_token(self):
        for failure in [OSError('https://api.telegram.org/botSECRET/sendMessage')]:
            with patch('urllib.request.urlopen',side_effect=failure):
                with self.assertRaises(TelegramError) as result: Telegram('SECRET').call('sendMessage')
            self.assertNotIn('SECRET',str(result.exception))
    def test_api_rejection_not_success(self):
        with patch('urllib.request.urlopen',return_value=io.BytesIO(b'{"ok":false}')):
            with self.assertRaises(TelegramError): Telegram('TEST_ONLY').call('sendMessage')
