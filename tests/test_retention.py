import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from test_domain import Fixture
from zhbot.retention import Retention


class RetentionTests(Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.now = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
        self.r = Retention(self.s, batch_size=2)

    def age(self, days):
        return (self.now - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')

    def run_cleanup(self):
        for _ in range(30):
            result = self.r.tick(self.now)
            if result and result['state'] == 'complete':
                return result
        self.fail('cleanup did not finish')

    def due(self):
        self.r.tick(self.now - timedelta(days=14))

    def terminal_order(self):
        draft = self.draft('private-account')
        batch = self.s.confirm(1, draft)
        order = self.s.orders(batch)[0]['id']
        self.s.refund(99, order, 'empty', 'test refund')
        with self.s.tx() as c:
            c.execute('UPDATE orders SET finished_at=? WHERE id=?', (self.age(31), order))
            c.execute("UPDATE events SET state='sent'")
            c.execute('UPDATE events SET finished_at=?', (self.age(31),))
            c.execute('UPDATE drafts SET updated_at=? WHERE id=?', (self.age(31), draft))
        return draft, batch, order

    def test_installation_does_not_clean_and_waits_fourteen_days(self):
        d = self.draft()
        with self.s.tx() as c:
            c.execute('UPDATE drafts SET updated_at=?', (self.age(60),))
        self.assertIsNone(self.r.tick(self.now))
        self.assertIsNone(self.r.tick(self.now + timedelta(days=13)))
        self.assertEqual(1, len(self.s.rows('SELECT * FROM drafts WHERE id=?', (d,))))
        result = self.r.tick(self.now + timedelta(days=14))
        self.assertEqual('complete', result['state'])
        self.assertEqual([], self.s.rows('SELECT * FROM drafts WHERE id=?', (d,)))

    def test_inactive_drafts_and_sessions_only(self):
        old = self.draft(); recent = self.draft()
        self.s.set_session(1, {'mode': 'draft', 'draft': old})
        self.s.set_session(2, {'mode': 'topup'})
        with self.s.tx() as c:
            c.execute('UPDATE drafts SET updated_at=? WHERE id=?', (self.age(15), old))
            c.execute('UPDATE sessions SET updated_at=? WHERE user_id=1', (self.age(15),))
        self.due(); self.run_cleanup()
        self.assertEqual([], self.s.rows('SELECT * FROM drafts WHERE id=?', (old,)))
        self.assertEqual({}, self.s.session(1))
        self.assertEqual('editing', self.s.draft(1, recent)['status'])
        self.assertEqual({'mode': 'topup'}, self.s.session(2))

    def test_recent_session_protects_old_draft(self):
        d = self.draft(); self.s.set_session(1, {'draft': d})
        with self.s.tx() as c:
            c.execute('UPDATE drafts SET updated_at=?', (self.age(15),))
        self.due(); self.run_cleanup()
        self.assertEqual('editing', self.s.draft(1, d)['status'])

    def test_pending_payment_preserves_draft_and_session(self):
        d = self.draft(); self.s.set_session(1, {'draft': d})
        with self.s.tx() as c:
            c.execute("INSERT INTO payments(id,user_id,amount,merchant) VALUES('pay',1,2,'merchant')")
            c.execute("INSERT INTO payment_cards VALUES('pay',1,7,?)", (d,))
            c.execute('UPDATE drafts SET updated_at=?', (self.age(90),))
            c.execute('UPDATE sessions SET updated_at=?', (self.age(90),))
        self.due(); self.run_cleanup()
        self.assertEqual(d, self.s.session(1)['draft'])
        self.assertEqual('editing', self.s.draft(1, d)['status'])

    def test_completed_material_cleared_but_financial_rows_and_keys_retained(self):
        d, b, o = self.terminal_order()
        before = {t: self.s.rows('SELECT * FROM '+t) for t in ('wallets','ledger','audit','payments','payment_checks')}
        with self.s.tx() as c:
            c.execute("INSERT INTO receipts(order_id,payload,state,message_id) VALUES(?,?,'sent',123)",
                      (o, json.dumps({'kind': 'text', 'text': 'private-result'})))
            c.execute('INSERT INTO reply_links VALUES(12,?)', (o,))
        self.due(); result = self.run_cleanup()
        order = self.s.order(o)
        self.assertEqual('{}', order['submission'])
        self.assertTrue(order['data_cleared_at'])
        self.assertEqual('test refund', order['reason'])
        self.assertNotIn('private-result', self.s.receipts(o)[0]['payload'])
        self.assertEqual('sent', self.s.receipts(o)[0]['state'])
        self.assertEqual(123, self.s.receipts(o)[0]['message_id'])
        self.assertEqual([], self.s.rows('SELECT * FROM reply_links'))
        self.assertEqual([], self.s.rows('SELECT * FROM drafts WHERE id=?', (d,)))
        self.assertTrue(all(e['payload']=='{}' for e in self.s.events() if e['kind']!='maintenance'))
        with self.s.tx() as c:
            self.s.event(c, 'refund:'+o, 'refund', 1, {'secret': 'replay'})
        self.assertTrue(all(e['payload']=='{}' for e in self.s.events() if e['kind']!='maintenance'))
        for t, rows in before.items(): self.assertEqual(rows, self.s.rows('SELECT * FROM '+t))
        self.assertGreater(result['counts']['orders'], 0)

    def test_unresolved_related_event_or_receipt_protects_order(self):
        d, b, o = self.terminal_order()
        with self.s.tx() as c:
            c.execute("UPDATE events SET state='uncertain' WHERE event_key=?", ('refund:'+o,))
        self.due(); self.run_cleanup()
        self.assertIn('private-account', self.s.order(o)['submission'])
        self.assertEqual('submitted', self.s.draft(1, d)['status'])

    def test_unresolved_receipt_and_batch_peer_protect_material(self):
        d, b, o = self.terminal_order()
        with self.s.tx() as c:
            c.execute("INSERT INTO receipts(order_id,payload,state) VALUES(?,?,'sending')", (o,'{}'))
        self.due(); self.run_cleanup()
        self.assertIsNone(self.s.order(o)['data_cleared_at'])
        with self.s.tx() as c:
            c.execute('DELETE FROM receipts')
            c.execute("INSERT INTO orders(id,batch_id,user_id,price,snapshot,submission) "
                      "SELECT 'peer',batch_id,user_id,price,snapshot,submission FROM orders WHERE id=?", (o,))
        self.r.tick(self.now+timedelta(days=14))
        self.assertIsNone(self.s.order(o)['data_cleared_at'])

    def test_resolved_cancelled_bulk_event_can_expire(self):
        d,b,o=self.terminal_order()
        with self.s.tx() as c:
            self.s.event(c,'bulk-test','bulk_receipt',1,{'batch':b,'text':'private bulk result'})
            c.execute("UPDATE events SET state='cancelled' WHERE event_key='bulk-test'")
            c.execute("UPDATE events SET finished_at=? WHERE event_key='bulk-test'",(self.age(31),))
        self.due(); self.run_cleanup()
        self.assertTrue(self.s.order(o)['data_cleared_at'])
        self.assertEqual('{}',self.s.one("SELECT payload FROM events WHERE event_key='bulk-test'")['payload'])

    def test_size_warning_is_deduplicated_without_deleting_financial_data(self):
        with patch.object(self.r, 'status', return_value={'database_bytes': 101*1024*1024}):
            self.r.tick(self.now)
            self.r.tick(self.now)
        warnings=[e for e in self.s.events() if e['event_key'].startswith('retention-size:')]
        self.assertEqual(1,len(warnings))
        self.assertEqual((10,0), self.s.balance(1))

    def test_status_admin_only_and_cleared_receipts_not_replayed(self):
        from test_app import Telegram
        from zhbot.app import App
        d, b, o = self.terminal_order()
        self.due(); self.run_cleanup()
        t=Telegram(); app=App(self.s,t)
        app.callback(99,'database')
        self.assertIn('不备份',t.calls[-1][1]['text'])
        from zhbot.store import DomainError
        with self.assertRaises(DomainError): app.callback(1,'database')
        with self.assertRaises(DomainError): app.callback(1,'myreceipts:'+o)
        app.callback(99,'order:'+o)
        self.assertIn('已按保留规则清理', t.calls[-1][1]['text'])
        app.flush_events()
        self.assertEqual('sent', [e for e in self.s.events() if e['kind']=='maintenance'][0]['state'])

    def test_only_old_successful_inbound_messages_are_deleted(self):
        with self.s.tx() as c:
            for ident, state, age in [(1,'done',31),(2,'uncertain',90),(3,'processing',90),(4,'done',29)]:
                c.execute('INSERT INTO inbox VALUES(?,?,?)', (ident,state,self.age(age)))
                c.execute('INSERT INTO inbox_archive(update_id,state,created_at) VALUES(?,?,?)', (ident,state,self.age(age)))
        self.due(); self.run_cleanup()
        self.assertEqual([2,3,4], [x['id'] for x in self.s.rows('SELECT id FROM inbox ORDER BY id')])
        self.assertEqual([2,3,4], [x['update_id'] for x in self.s.rows('SELECT update_id FROM inbox_archive ORDER BY update_id')])

    def test_bounded_batches_resume_after_process_restart(self):
        with self.s.tx() as c:
            for i in range(7): c.execute("INSERT INTO inbox VALUES(?,'done',?)", (i,self.age(31)))
        self.due(); first = self.r.tick(self.now)
        self.assertEqual('running', first['state'])
        self.assertEqual(5, len(self.s.rows('SELECT * FROM inbox')))
        self.r = Retention(self.s, batch_size=2)
        result = self.run_cleanup()
        self.assertEqual(7, result['counts']['inbox'])

    def test_failed_batch_rolls_back_and_reports_sanitized_error(self):
        d = self.draft()
        with self.s.tx() as c:
            c.execute('UPDATE drafts SET updated_at=?', (self.age(15),))
            c.execute("CREATE TRIGGER fail_cleanup BEFORE DELETE ON drafts BEGIN SELECT RAISE(ABORT,'sensitive-value'); END")
        self.due(); result = self.r.tick(self.now)
        self.assertEqual('failed', result['state'])
        self.assertNotIn('sensitive-value', json.dumps(result))
        self.assertEqual('editing', self.s.draft(1, d)['status'])
        self.assertTrue(any(e['kind']=='maintenance' for e in self.s.events()))
        with self.s.tx() as c: c.execute('DROP TRIGGER fail_cleanup')
        self.run_cleanup()
        self.assertEqual([], self.s.rows('SELECT * FROM drafts'))

    def test_timestamps_refresh_and_old_terminal_rows_start_at_upgrade(self):
        d = self.draft()
        self.s.set_session(1, {'draft': d})
        with self.s.tx() as c:
            c.execute('UPDATE drafts SET updated_at=?', (self.age(60),))
            c.execute('UPDATE sessions SET updated_at=?', (self.age(60),))
        self.s.set_field(1,d,0,{'kind':'text','text':'new'})
        self.s.set_session(1, {'draft':d,'step':2})
        self.assertGreater(self.s.draft(1,d)['updated_at'], self.age(1))
        self.assertGreater(self.s.one('SELECT updated_at FROM sessions')['updated_at'], self.age(1))
        b = self.s.confirm(1,d); o = self.s.orders(b)[0]['id']
        self.s.refund(99,o,'empty','reason')
        self.assertGreater(self.s.order(o)['finished_at'],self.age(1))

    def test_existing_database_upgrade_has_conservative_timestamps_and_is_repeatable(self):
        from zhbot.store import Store
        path=Path(self.tmp.name)/'old.db'
        with closing(sqlite3.connect(path)) as c, c:
            c.execute('CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY)')
            for migration in sorted((Path(__file__).parent.parent/'migrations').glob('*.sql')):
                version=int(migration.stem.split('_')[0])
                if version>=9: continue
                c.executescript(migration.read_text(encoding='utf-8'))
                c.execute('INSERT INTO schema_migrations VALUES(?)',(version,))
            c.execute("INSERT INTO products(id,name,price,description,eta,fields,position) VALUES(1,'test',1,'','','[]',1)")
            c.execute("INSERT INTO batches VALUES('batch',1,'2020-01-01 00:00:00')")
            c.execute("INSERT INTO orders(id,batch_id,user_id,price,snapshot,submission,status) VALUES('order','batch',1,1,'{}','{}','completed')")
            c.execute("INSERT INTO drafts(id,user_id,product_id,revision) VALUES('draft',1,1,1)")
            c.execute("INSERT INTO sessions VALUES(1,'{}')")
            c.execute("INSERT INTO events(event_key,kind,target,payload,state) VALUES('old','new',99,'{}','sent')")
        upgraded=Store(path,99)
        self.assertGreater(upgraded.order('order')['finished_at'],self.age(1))
        self.assertGreater(upgraded.draft(1,'draft')['updated_at'],self.age(1))
        self.assertGreater(upgraded.one('SELECT updated_at FROM sessions')['updated_at'],self.age(1))
        self.assertGreater(upgraded.events()[0]['finished_at'],self.age(1))
        self.assertEqual(9,Store(path,99).one('SELECT MAX(version) AS n FROM schema_migrations')['n'])
