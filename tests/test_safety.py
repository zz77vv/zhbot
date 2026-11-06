import concurrent.futures
import json
import unittest
from test_domain import Fixture
from zhbot.store import DomainError, Store

class SafetyTests(Fixture,unittest.TestCase):
    def test_concurrent_confirmation_has_one_batch(self):
        d=self.draft('one')
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            ids=list(pool.map(lambda _:self.s.confirm(1,d),range(12)))
        self.assertEqual(1,len(set(ids))); self.assertEqual((7,3),self.s.balance(1))
        self.assertEqual(1,len(self.s.orders()))
    def test_competing_drafts_do_not_overspend(self):
        d=self.draft('one\ntwo\nthree')
        # Separate customers aren't a balance race; competing confirmations of a shared draft are.
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _:self.s.confirm(1,d),range(8)))
        self.assertEqual((1,9),self.s.balance(1)); self.assertEqual(3,len(self.s.orders()))
    def test_ledger_rolls_back_on_exception(self):
        with self.assertRaises(RuntimeError):
            with self.s.tx() as c:
                self.s.money(c,1,-5,5,'test','freeze')
                raise RuntimeError('crash')
        self.assertEqual((10,0),self.s.balance(1))
        self.assertEqual([],self.s.rows("SELECT * FROM ledger WHERE event_key='test'"))
    def test_media_fields_and_deletion_persist(self):
        p=self.s.product(self.p); p['fields']=[{'name':'姓名','kind':'text'},{'name':'图片','kind':'photo'},{'name':'文件','kind':'document'}]; self.s.save_product(99,p)
        d=self.s.new_draft(1,self.p); self.s.set_field(1,d,0,{'kind':'text','text':'a\nb'})
        with self.assertRaises(DomainError): self.s.confirm(1,d)
        self.s.set_field(1,d,1,{'kind':'photo','file_id':'photo'})
        self.s.set_field(1,d,2,{'kind':'document','file_id':'doc'})
        self.s.set_field(1,d,1,{},delete=0)
        with self.assertRaises(DomainError): self.s.confirm(1,d)
        self.s.set_field(1,d,1,{'kind':'photo','file_id':'new'})
        b=self.s.confirm(1,d); self.assertEqual(1,len(self.s.orders(b)))
        restart=Store(self.s.path,99); self.assertEqual((7,3),restart.balance(1))
    def test_nonempty_cannot_refund_as_empty(self):
        o=self.s.orders(self.s.confirm(1,self.draft('x')))[0]['id']
        self.s.add_receipt(99,o,{'kind':'text','text':'r'})
        with self.assertRaises(DomainError): self.s.refund(99,o,'empty','')
        r=self.s.receipts(o)[0]['id']; self.s.delete_receipt(99,o,r)
        self.s.refund(99,o,'empty',''); self.s.refund(99,o,'empty','')
        self.assertEqual((10,0),self.s.balance(1))
    def test_delivery_resolution_and_backup(self):
        o=self.s.orders(self.s.confirm(1,self.draft('x')))[0]['id']
        self.s.add_receipt(99,o,{'kind':'text','text':'r'}); self.s.begin_delivery(99,o)
        rid=self.s.receipts(o)[0]['id']; self.s.mark_sending(rid); self.s.recover()
        self.s.resolve(99,rid,True,456,'客户聊天记录核实'); self.s.settle(o)
        dest=self.tmp.name+'/backup.db'; self.s.backup(dest)
        self.assertEqual((7,0),Store(dest,99).balance(1))
    def test_product_revision_invalidates_draft(self):
        d=self.draft('x'); p=self.s.product(self.p); p['price']=8; self.s.save_product(99,p)
        with self.assertRaises(DomainError): self.s.confirm(1,d)
        self.assertEqual((10,0),self.s.balance(1))
    def test_adjust_cannot_spend_frozen(self):
        self.s.confirm(1,self.draft('x'))
        with self.assertRaises(DomainError): self.s.adjust(99,1,-8,'扣减','negative')
        self.assertEqual((7,3),self.s.balance(1))
    def test_resume_uncertain_after_all_receipts_confirmed(self):
        o=self.s.orders(self.s.confirm(1,self.draft('x')))[0]['id']
        self.s.add_receipt(99,o,{'kind':'text','text':'r'}); self.s.begin_delivery(99,o)
        r=self.s.receipts(o)[0]['id']; self.s.mark_sending(r); self.s.mark_sent(r,123)
        self.s.uncertain(o)
        self.s.resume(99,o,'核实所有消息已发送，恢复结算')
        self.s.settle(o); self.assertEqual((7,0),self.s.balance(1))
    def test_old_draft_preview_cannot_submit_edited_data(self):
        d=self.draft('first'); version=self.s.draft(1,d)['edit_version']
        self.s.set_field(1,d,0,{'kind':'text','text':'changed'})
        with self.assertRaises(DomainError): self.s.confirm(1,d,expected_version=version)
        self.assertEqual((10,0),self.s.balance(1))
