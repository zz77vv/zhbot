import unittest
from test_domain import Fixture
from zhbot.runtime import Runtime

class RuntimeTests(Fixture,unittest.TestCase):
    def test_replayed_update_is_not_dispatched(self):
        calls=[]
        class App:
            def handle(self,u): calls.append(u['update_id'])
        r=Runtime(self.s,App(),None); r.dispatch({'update_id':3}); r.dispatch({'update_id':3})
        self.assertEqual([3],calls); self.assertEqual(4,r.offset())
    def test_interrupted_update_is_quarantined(self):
        class App:
            def handle(self,u): raise RuntimeError('crash')
        r=Runtime(self.s,App(),None)
        with self.assertRaises(RuntimeError): r.dispatch({'update_id':4})
        r.recover()
        self.assertEqual('uncertain',self.s.one('SELECT state FROM inbox WHERE id=4')['state'])
