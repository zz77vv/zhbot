import tempfile
import unittest
from pathlib import Path
from zhbot.store import Store, DomainError, InsufficientPoints

class Fixture:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = Store(Path(self.tmp.name) / 'test.db', 99)
        self.p = self.s.save_product(99, dict(name='查询', price=3, description='介绍', eta='人工', fields=[{'name':'资料','kind':'text'}]))
        self.s.adjust(99, 1, 10, '线下收款', 'seed')
    def tearDown(self):
        self.tmp.cleanup()
    def draft(self, text='a\nb'):
        d=self.s.new_draft(1,self.p)
        self.s.set_field(1,d,0,{'kind':'text','text':text})
        return d

class DomainTests(Fixture, unittest.TestCase):
    def test_long_bulk_receipt_settles_only_the_delivered_part(self):
        batch=self.s.confirm(1,self.draft('first\nsecond'))
        self.s.queue_bulk(99,batch,{'first':'甲'*1900,'second':'乙'*1900})
        first,second=[e for e in self.s.events() if e['kind']=='bulk_receipt']
        with self.s.tx() as c:
            self.s.finish_bulk(c,first,123)
        self.assertEqual(['completed','bulk_delivering'],[order['status'] for order in self.s.orders(batch)])
        self.assertEqual((4,3),self.s.balance(1))
        with self.s.tx() as c:
            self.s.finish_bulk(c,second,124)
        self.assertEqual(['completed','completed'],[order['status'] for order in self.s.orders(batch)])
        self.assertEqual((4,0),self.s.balance(1))

    def test_secondary_quote_is_per_order_and_expiration_refunds_once(self):
        self.s.set_product_tiers(99,self.p,self.s.product(self.p)['revision'],{'新菜':3,'大众名':4,'剩菜':12})
        batch=self.s.confirm(1,self.draft())
        first,second=self.s.orders(batch)
        oid=first['id']
        self.s.offer_quote(99,oid,'大众名')
        original=self.s.open_quote(oid)
        self.assertEqual(4,original['price'])
        with self.assertRaises(DomainError):
            self.s.queue_bulk(99,batch,{'a':'ok','b':'ok'})
        with self.assertRaises(DomainError):
            self.s.add_receipt(99,oid,{'kind':'text','text':'done'})
        self.s.offer_quote(99,oid,'剩菜')
        with self.assertRaises(DomainError):
            self.s.resolve_quote(1,original['id'],True)
        with self.s.tx() as c:
            c.execute("UPDATE order_quotes SET expires_at='2000-01-01 00:00:00' WHERE state='pending'")
        self.s.expire_quotes()
        self.s.expire_quotes()
        self.assertEqual('cancel',self.s.order(oid)['status'])
        self.assertEqual('pending',self.s.order(second['id'])['status'])
        self.assertEqual((7,3),self.s.balance(1))

    def test_batch_idempotent_freeze(self):
        d=self.draft(); b=self.s.confirm(1,d)
        self.assertEqual(b,self.s.confirm(1,d))
        self.assertEqual((4,6),self.s.balance(1))
        self.assertEqual(2,len(self.s.orders(b)))
        self.assertEqual(1,len(self.s.events()))
    def test_insufficient_preserves_whole_batch_for_reconfirmation_after_credit(self):
        d=self.draft('a\nb\nc\nd')
        before=self.s.draft(1,d)
        ledger_before=self.s.rows('SELECT * FROM ledger')
        with self.assertRaises(InsufficientPoints) as caught:
            self.s.confirm(1,d,expected_version=before['edit_version'])
        self.assertEqual(2,caught.exception.missing)
        self.assertEqual((10,0),self.s.balance(1))
        self.assertEqual([],self.s.orders())
        self.assertEqual([],self.s.rows('SELECT * FROM batches'))
        self.assertEqual([],self.s.events())
        self.assertEqual(ledger_before,self.s.rows('SELECT * FROM ledger'))
        retained=self.s.draft(1,d)
        self.assertEqual('editing',retained['status'])
        self.assertEqual(before['data'],retained['data'])
        self.assertEqual(before['edit_version']+1,retained['edit_version'])
        self.s.adjust(99,1,2,'补足报单积分','topup')
        with self.assertRaises(DomainError):
            self.s.confirm(1,d,expected_version=before['edit_version'])
        self.assertEqual((12,0),self.s.balance(1))
        self.assertEqual([],self.s.orders())
        batch=self.s.confirm(1,d,expected_version=retained['edit_version'])
        self.assertEqual(4,len(self.s.orders(batch)))
        self.assertEqual((0,12),self.s.balance(1))
        self.assertEqual(batch,self.s.confirm(1,d,expected_version=retained['edit_version']))
        self.assertEqual(4,len(self.s.orders()))
        self.assertEqual(4,len([row for row in self.s.rows('SELECT * FROM ledger') if row['event_key'].startswith('freeze:')]))
    def test_receipts_staged_then_settled_and_summary(self):
        b=self.s.confirm(1,self.draft()); a,c=self.s.orders(b)
        self.s.add_receipt(99,a['id'],{'kind':'text','text':'done'})
        self.assertEqual((4,6),self.s.balance(1))
        self.s.begin_delivery(99,a['id'])
        r=self.s.receipts(a['id'])[0]
        self.s.mark_sending(r['id']); self.s.mark_sent(r['id'],123)
        self.s.settle(a['id']); self.s.settle(a['id'])
        self.s.refund(99,c['id'],'empty','')
        self.assertEqual((7,0),self.s.balance(1))
        self.assertEqual(1,len([e for e in self.s.events() if e['kind']=='summary']))
    def test_uncertain_blocks_refund_and_replay(self):
        o=self.s.orders(self.s.confirm(1,self.draft('x')))[0]['id']
        self.s.add_receipt(99,o,{'kind':'text','text':'ok'})
        self.s.begin_delivery(99,o); r=self.s.receipts(o)[0]['id']
        self.s.mark_sending(r); self.s.recover()
        with self.assertRaises(DomainError): self.s.refund(99,o,'cancel','')
        with self.assertRaises(DomainError): self.s.begin_delivery(99,o)
        self.assertEqual((7,3),self.s.balance(1))
    def test_permissions_and_adjust_idempotence(self):
        with self.assertRaises(DomainError): self.s.adjust(2,1,10,'bad','bad')
        self.s.adjust(99,1,-2,'纠正','fix'); self.s.adjust(99,1,-2,'纠正','fix')
        self.assertEqual((8,0),self.s.balance(1))
        with self.assertRaises(DomainError): self.s.confirm(2,self.draft())
    def test_product_order_snapshot_and_offline(self):
        p2=self.s.save_product(99,dict(name='第二',price=1,description='',eta='',fields=[{'name':'资料','kind':'text'}]))
        self.s.move_product(99,p2,-1)
        self.assertEqual(p2,self.s.products()[0]['id'])
        b=self.s.confirm(1,self.draft('x'))
        self.s.save_product(99,dict(self.s.product(self.p),price=5,active=0))
        self.assertEqual(3,self.s.orders(b)[0]['price'])
        with self.assertRaises(DomainError): self.s.new_draft(1,self.p)
