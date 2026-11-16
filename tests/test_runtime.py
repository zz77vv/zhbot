import unittest
from test_domain import Fixture
from zhbot.runtime import Runtime

class RuntimeTests(Fixture,unittest.TestCase):
    def test_reused_id_after_inactivity_is_archived_and_dispatched(self):
        calls=[]
        class App:
            def handle(self,update): calls.append(update['update_id'])
        runtime=Runtime(self.s,App(),None)
        with self.s.tx() as c:
            c.execute("INSERT INTO inbox(id,state,created_at) VALUES(3,'uncertain',datetime('now','-8 days'))")
        self.assertEqual(0,runtime.offset())
        runtime.dispatch({'update_id':3})
        runtime.dispatch({'update_id':3})
        self.assertEqual([3],calls)
        self.assertEqual('uncertain',self.s.one('SELECT state FROM inbox_archive WHERE update_id=3')['state'])
        self.assertEqual('done',self.s.one('SELECT state FROM inbox WHERE id=3')['state'])
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
