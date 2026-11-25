import unittest
from zhbot.telegram import TelegramRejected


class TelegramErrorTests(unittest.TestCase):
    def test_only_known_error_categories_are_retained(self):
        cases=[
            ('Bad Request: CUSTOM_EMOJI_NOT_ALLOWED','custom_emoji'),
            ('Bad Request: BUTTON_ICON_INVALID','custom_emoji'),
            ('Bad Request: message is not modified: same content','not_modified'),
            ('Bad Request: message to edit not found',None),
            (None,None),
        ]
        for description,expected in cases:
            with self.subTest(description=description):
                error=TelegramRejected.from_response({'ok':False,'error_code':400,'description':description})
                self.assertEqual(expected,error.reason)
                self.assertEqual('Telegram 明确拒绝请求',str(error))

    def test_retry_delay_and_non_400_errors_do_not_gain_emoji_category(self):
        error=TelegramRejected.from_response({'ok':False,'error_code':429,
                                             'description':'custom emoji rate limit',
                                             'parameters':{'retry_after':30}})
        self.assertEqual(30,error.retry_after)
        self.assertIsNone(error.reason)
        self.assertIsNone(TelegramRejected.from_response({'ok':False,'error_code':500}))
