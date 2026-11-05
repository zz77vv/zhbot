"""Transactional domain operations and persistent, ordered Telegram outbox."""
from functools import wraps
import time

from .core import MAX_ITEMS, MAX_POINTS, RuleError, bounded_text, item_values, parse_uint


def transactional(fn):
    @wraps(fn)
    def run(self, *args, **kwargs):
        with self.db.atomic():
            return fn(self, *args, **kwargs)
    return run


class Service:
    def __init__(self, db, admins, clock=None):
        self.db = db
        self.admins = frozenset(parse_uint(x) for x in admins)
        self.clock = clock or (lambda: int(time.time()))

    def admin(self, actor):
        if parse_uint(actor) not in self.admins:
            raise RuleError('仅管理员可操作。')

    def user(self, actor):
        row = self.db.one('SELECT * FROM users WHERE tg_id=?', (parse_uint(actor),))
        if row is None:
            raise RuleError('请先使用 /start 注册。')
        return row

    @transactional
    def register(self, actor):
        self.db.execute('INSERT OR IGNORE INTO users(tg_id) VALUES (?)', (parse_uint(actor),))

    def balance(self, actor):
        row = self.user(actor)
        return row['available'], row['frozen']

    def audit(self, actor, action, ref, reason):
        self.db.execute('INSERT INTO audit(at,actor,action,ref,reason) VALUES (?,?,?,?,?)',
                        (self.clock(), actor, action, str(ref), reason))

    def ledger(self, user, key, available, frozen):
        self.db.execute('UPDATE users SET available=available+?, frozen=frozen+? WHERE tg_id=?',
                        (available, frozen, user))
        balance = self.user(user)
        self.db.execute('INSERT INTO ledger(user_id,entry_key,available_delta,frozen_delta,available_after,frozen_after,at) VALUES (?,?,?,?,?,?,?)',
                        (user, key, available, frozen, balance['available'], balance['frozen'], self.clock()))

    @transactional
    def save_project(self, actor, project, title, price, active):
        self.admin(actor)
        title = bounded_text(title, 100)
        price = parse_uint(price, maximum=MAX_POINTS)
        if type(active) is not bool:
            raise RuleError('项目状态无效。')
        if project is None:
            project = self.db.execute('INSERT INTO projects(title,price,active) VALUES (?,?,?)',
                                      (title, price, int(active))).lastrowid
        else:
            project = parse_uint(project)
            if not self.db.execute('UPDATE projects SET title=?,price=?,active=?,revision=revision+1 WHERE id=?',
                                   (title, price, int(active), project)).rowcount:
                raise RuleError('项目不存在。')
        self.audit(actor, 'project', project, '更新项目')
        return project

    def projects(self, actor, include_inactive=False):
        self.user(actor)
        if include_inactive:
            self.admin(actor)
        return self.db.all('SELECT * FROM projects' + ('' if include_inactive else ' WHERE active=1') + ' ORDER BY id')

    @transactional
    def new_draft(self, actor, project):
        self.user(actor)
        row = self.db.one('SELECT * FROM projects WHERE id=? AND active=1', (parse_uint(project),))
        if row is None:
            raise RuleError('项目不可用。')
        if self.db.one("SELECT id FROM drafts WHERE user_id=? AND state='editing'", (actor,)):
            raise RuleError('已有草稿，请先 /draft 查看或 /cancel 取消。')
        draft = self.db.execute('INSERT INTO drafts(user_id,project_id,project_revision,title,price) VALUES (?,?,?,?,?)',
                                (actor, row['id'], row['revision'], row['title'], row['price'])).lastrowid
        self.select(actor, 'customer', draft)
        return draft

    def select(self, actor, kind, ref):
        self.db.execute('INSERT INTO selections(actor,kind,ref) VALUES (?,?,?) ON CONFLICT(actor) DO UPDATE SET kind=excluded.kind,ref=excluded.ref',
                        (actor, kind, ref))

    def draft(self, actor, draft=None):
        self.user(actor)
        if draft is None:
            row = self.db.one("SELECT * FROM drafts WHERE user_id=? AND state='editing'", (actor,))
        else:
            row = self.db.one('SELECT * FROM drafts WHERE id=? AND user_id=?', (parse_uint(draft), actor))
        if row is None:
            raise RuleError('草稿不存在或无权访问。')
        row['items'] = self.db.all('SELECT * FROM draft_items WHERE draft_id=? ORDER BY id', (row['id'],))
        return row

    @transactional
    def resume_customer(self, actor):
        """Explicitly leave receipt editing even when no customer preview exists."""
        self.user(actor)
        row = self.db.one("SELECT id FROM drafts WHERE user_id=? AND state='editing'", (actor,))
        if row is None:
            self.db.execute('DELETE FROM selections WHERE actor=?', (actor,))
            return None
        self.select(actor, 'customer', row['id'])
        return self.draft(actor, row['id'])

    @transactional
    def add_customer_item(self, actor, kind, content, caption=''):
        row = self.draft(actor)
        if len(row['items']) >= MAX_ITEMS:
            raise RuleError(f'每单最多 {MAX_ITEMS} 项材料。')
        self.db.execute('INSERT INTO draft_items(draft_id,kind,content,caption) VALUES (?,?,?,?)',
                        (row['id'], *item_values(kind, content, caption)))
        self.db.execute('UPDATE drafts SET revision=revision+1,preview_revision=NULL WHERE id=?', (row['id'],))

    @transactional
    def cancel_draft(self, actor):
        row = self.draft(actor)
        self.db.execute("UPDATE drafts SET state='cancelled' WHERE id=?", (row['id'],))
        self.db.execute("DELETE FROM selections WHERE actor=? AND kind='customer'", (actor,))

    def batch(self, actor, chat_id, kind, items, ref=None, revision=None, *, admin_only=False):
        if type(admin_only) is not bool:
            raise RuleError('发送权限标记无效。')
        admin_only = admin_only or kind == 'receipt_preview'
        if admin_only:
            self.admin(actor)
            if chat_id != actor:
                raise RuleError('管理员内容只能发送至其私聊。')
        audience = 'admin' if admin_only else 'customer'
        batch = self.db.execute('INSERT INTO batches(kind,ref,revision,actor,chat_id,audience) VALUES (?,?,?,?,?,?)',
                                (kind, ref, revision, actor, chat_id, audience)).lastrowid
        for seq, item in enumerate(items):
            self.db.execute('INSERT INTO messages(batch_id,seq,kind,content,caption) VALUES (?,?,?,?,?)',
                            (batch, seq, item['kind'], item['content'], item.get('caption', '')))
        return batch

    @transactional
    def note(self, actor, text, *, admin_only=False):
        return self.batch(actor, actor, 'note', [{'kind': 'text', 'content': bounded_text(text)}], admin_only=admin_only)

    @transactional
    def preview_customer(self, actor, draft=None):
        row = self.draft(actor, draft)
        if row['state'] != 'editing' or not row['items']:
            raise RuleError('请先添加材料。')
        summary = {'kind': 'text', 'content': f"草稿 {row['id']} · {row['title']} · {row['price']} 积分\n完整核对后发送 /confirm {row['id']} {row['revision']}"}
        return self.batch(actor, actor, 'customer_preview', row['items'] + [summary], row['id'], row['revision'])

    @transactional
    def confirm(self, actor, draft, revision):
        row = self.draft(actor, draft)
        revision = parse_uint(revision)
        if row['state'] == 'confirmed' and row['revision'] == revision:
            return row['order_id']
        if row['state'] != 'editing' or row['revision'] != revision or row['preview_revision'] != revision:
            raise RuleError('请重新 /draft 完整预览当前草稿后确认。')
        project = self.db.one('SELECT * FROM projects WHERE id=?', (row['project_id'],))
        if not project['active'] or project['revision'] != row['project_revision']:
            raise RuleError('项目已变更，请 /cancel 后重新选择项目。')
        if self.balance(actor)[0] < row['price']:
            raise RuleError('可用积分不足。')
        order = self.db.execute("INSERT INTO orders(draft_id,user_id,project_id,title,price,status,created_at) VALUES (?,?,?,?,?,'submitted',?)",
                                (row['id'], actor, project['id'], row['title'], row['price'], self.clock())).lastrowid
        for item in row['items']:
            self.db.execute('INSERT INTO order_items(order_id,kind,content,caption) VALUES (?,?,?,?)',
                            (order, item['kind'], item['content'], item['caption']))
        self.ledger(actor, f'freeze:{order}', -row['price'], row['price'])
        self.db.execute("UPDATE drafts SET state='confirmed',order_id=? WHERE id=?", (order, row['id']))
        self.db.execute("DELETE FROM selections WHERE actor=? AND kind='customer'", (actor,))
        for admin in self.admins:
            self.note(admin, f'新订单 {order}，使用 /order {order} 查看。', admin_only=True)
        return order

    def order(self, actor, order):
        actor = parse_uint(actor)
        row = self._order_record(order)
        if actor not in self.admins and row['user_id'] != actor:
            raise RuleError('订单不存在或无权访问。')
        return row

    def _order_record(self, order):
        # Internal outbox recovery relies on authorization at initiation, so a
        # later admin rotation cannot invalidate already-committed work.
        order = parse_uint(order)
        row = self.db.one('SELECT * FROM orders WHERE id=?', (order,))
        if row is None:
            raise RuleError('订单不存在或无权访问。')
        row['items'] = self.db.all('SELECT * FROM order_items WHERE order_id=? ORDER BY id', (order,))
        return row

    @transactional
    def fail_order(self, actor, order, reason):
        self.admin(actor)
        reason = bounded_text(reason, 500)
        row = self.order(actor, order)
        if row['status'] == 'failed':
            return
        if row['status'] != 'submitted':
            raise RuleError('该状态不能直接失败；发送不确定时请先人工核实。')
        self._release(row, actor, reason)

    def _release(self, row, actor, reason):
        self.ledger(row['user_id'], f'release:{row["id"]}', row['price'], -row['price'])
        self.db.execute("UPDATE orders SET status='failed',failure_reason=? WHERE id=?", (reason, row['id']))
        self.audit(actor, 'fail_order', row['id'], reason)
        self.note(row['user_id'], f'订单 {row["id"]} 失败，冻结积分已释放。原因：{reason}')

    @transactional
    def open_receipt(self, actor, order):
        self.admin(actor)
        row = self.order(actor, order)
        if row['status'] != 'submitted':
            raise RuleError('订单已进入交付或结束，不能编辑回执。')
        self.db.execute('INSERT OR IGNORE INTO receipts(order_id) VALUES (?)', (row['id'],))
        self.select(actor, 'receipt', row['id'])

    def receipt(self, actor, order):
        self.admin(actor)
        self.order(actor, order)
        row = self.db.one('SELECT * FROM receipts WHERE order_id=?', (parse_uint(order),))
        if row is None:
            raise RuleError('请先 /receipt 选择订单。')
        row['items'] = self.db.all('SELECT * FROM receipt_items WHERE order_id=? ORDER BY id', (row['order_id'],))
        return row

    def _selected_receipt(self, actor):
        self.admin(actor)
        selection = self.db.one("SELECT * FROM selections WHERE actor=? AND kind='receipt'", (actor,))
        if selection is None:
            raise RuleError('请先 /receipt 选择订单。')
        row = self.order(actor, selection['ref'])
        if row['status'] != 'submitted':
            raise RuleError('交付中或已结束，不能编辑回执。')
        return self.receipt(actor, row['id'])

    @transactional
    def add_receipt_item(self, actor, kind, content, caption=''):
        row = self._selected_receipt(actor)
        if len(row['items']) >= MAX_ITEMS:
            raise RuleError(f'每份回执最多 {MAX_ITEMS} 项。')
        self.db.execute('INSERT INTO receipt_items(order_id,kind,content,caption) VALUES (?,?,?,?)',
                        (row['order_id'], *item_values(kind, content, caption)))
        self._bump_receipt(row['order_id'])

    def _bump_receipt(self, order):
        self.db.execute('UPDATE receipts SET revision=revision+1,preview_revision=NULL,preview_actor=NULL WHERE order_id=?', (order,))

    @transactional
    def undo_selected(self, actor):
        self._edit_selected(actor, False)

    @transactional
    def clear_selected(self, actor):
        self._edit_selected(actor, True)

    def _edit_selected(self, actor, clear):
        selection = self.db.one('SELECT * FROM selections WHERE actor=?', (actor,))
        if selection and selection['kind'] == 'receipt':
            row = self._selected_receipt(actor)
            table, key, ref = 'receipt_items', 'order_id', row['order_id']
            self._bump_receipt(ref)
        else:
            row = self.draft(actor)
            table, key, ref = 'draft_items', 'draft_id', row['id']
            self.db.execute('UPDATE drafts SET revision=revision+1,preview_revision=NULL WHERE id=?', (ref,))
        if clear:
            self.db.execute(f'DELETE FROM {table} WHERE {key}=?', (ref,))
        else:
            self.db.execute(f'DELETE FROM {table} WHERE id=(SELECT MAX(id) FROM {table} WHERE {key}=?)', (ref,))

    @transactional
    def preview_receipt(self, actor, order):
        self.admin(actor)
        target = self.order(actor, order)
        row = self.receipt(actor, order)
        if target['status'] != 'submitted' or not row['items']:
            raise RuleError('无可预览回执或订单已进入交付。')
        summary = {'kind': 'text', 'content': f"订单 {order} 回执 v{row['revision']}\n核对完整内容后发送 /deliver {order} {row['revision']}"}
        return self.batch(actor, actor, 'receipt_preview', row['items'] + [summary], target['id'], row['revision'])

    @transactional
    def deliver(self, actor, order, revision):
        self.admin(actor)
        target = self.order(actor, order)
        row = self.receipt(actor, order)
        revision = parse_uint(revision)
        if row['revision'] != revision or row['preview_revision'] != revision or row['preview_actor'] != actor:
            raise RuleError('请先完整 /preview 当前版本回执。')
        existing = self.db.one("SELECT id FROM batches WHERE kind='delivery' AND ref=?", (target['id'],))
        if existing:
            return existing['id']
        if target['status'] != 'submitted':
            raise RuleError('订单已结束。')
        batch = self.batch(actor, target['user_id'], 'delivery', row['items'], target['id'], revision)
        self.db.execute("UPDATE orders SET status='delivering' WHERE id=?", (target['id'],))
        self.audit(actor, 'delivery_started', order, f'回执版本 {revision}')
        return batch

    @transactional
    def claim_message(self):
        self._cancel_unauthorized_outbox()
        row = self.db.one("""SELECT m.*, b.chat_id,b.kind AS batch_kind,b.ref,b.revision
            FROM messages m JOIN batches b ON b.id=m.batch_id
            WHERE b.status='queued' AND m.status='pending' AND m.not_before<=?
            AND NOT EXISTS (SELECT 1 FROM messages p WHERE p.batch_id=m.batch_id AND p.seq<m.seq AND p.status!='sent')
            ORDER BY m.id LIMIT 1""", (self.clock(),))
        if row:
            self.db.execute("UPDATE messages SET status='sending' WHERE id=?", (row['id'],))
        return row

    def _admin_audience_allowed(self, batch):
        return batch['audience'] == 'admin' and batch['actor'] in self.admins and batch['chat_id'] == batch['actor']

    def _cancel_unauthorized_outbox(self):
        for batch in self.db.all("SELECT * FROM batches WHERE status IN ('queued','review') AND audience!='customer'"):
            if self._admin_audience_allowed(batch):
                continue
            reason = '管理员权限已撤销或收件人不匹配' if batch['audience'] == 'admin' else '旧队列无法确认内容权限，停止未发送部分'
            self.db.execute("UPDATE batches SET status='cancelled' WHERE id=?", (batch['id'],))
            self.db.execute("UPDATE messages SET status='cancelled',reason=? WHERE batch_id=? AND status='pending'", (reason, batch['id']))
            self.audit(0, 'outbox_cancelled', batch['id'], reason)
        for receipt in self.db.all('SELECT order_id,preview_actor FROM receipts WHERE preview_actor IS NOT NULL'):
            if receipt['preview_actor'] not in self.admins:
                self.db.execute('UPDATE receipts SET preview_revision=NULL,preview_actor=NULL WHERE order_id=?', (receipt['order_id'],))

    @transactional
    def sent(self, message, telegram_id):
        row = self.db.one("SELECT * FROM messages WHERE id=? AND status='sending'", (message,))
        if row is None:
            raise RuleError('发送记录状态已变更。')
        self.db.execute("UPDATE messages SET status='sent',telegram_id=? WHERE id=?", (parse_uint(telegram_id), message))
        self._finish_batch(row['batch_id'])

    def _finish_batch(self, batch):
        row = self.db.one('SELECT * FROM batches WHERE id=?', (batch,))
        if row['status'] == 'cancelled':
            return
        if row['audience'] != 'customer' and not self._admin_audience_allowed(row):
            self._cancel_unauthorized_outbox()
            return
        if self.db.one("SELECT id FROM messages WHERE batch_id=? AND status!='sent'", (batch,)):
            return
        self.db.execute("UPDATE batches SET status='sent' WHERE id=?", (batch,))
        if row['kind'] == 'customer_preview':
            self.db.execute("UPDATE drafts SET preview_revision=? WHERE id=? AND revision=? AND state='editing'",
                            (row['revision'], row['ref'], row['revision']))
        elif row['kind'] == 'receipt_preview':
            self.db.execute('UPDATE receipts SET preview_revision=?,preview_actor=? WHERE order_id=? AND revision=?',
                            (row['revision'], row['actor'], row['ref'], row['revision']))
        elif row['kind'] == 'delivery':
            target = self._order_record(row['ref'])
            if target['status'] not in {'delivering', 'delivery_review'}:
                raise RuleError('交付状态冲突。')
            self.ledger(target['user_id'], f'charge:{target["id"]}', 0, -target['price'])
            self.db.execute("UPDATE orders SET status='completed' WHERE id=?", (target['id'],))
            self.audit(row['actor'], 'delivery_completed', target['id'], '全部回执发送已确认')
            self.note(target['user_id'], f'订单 {target["id"]} 回执已全部发送，扣除 {target["price"]} 积分。')
            for admin in self.admins:
                self.note(admin, f'订单 {target["id"]} 完成交付。', admin_only=True)

    @transactional
    def uncertain(self, message, reason):
        self._send_problem(message, 'unknown', reason)

    @transactional
    def defer(self, message, seconds):
        seconds = parse_uint(seconds, maximum=3600)
        if not self.db.execute("UPDATE messages SET status='pending',not_before=? WHERE id=? AND status='sending'",
                               (self.clock() + seconds, parse_uint(message))).rowcount:
            raise RuleError('发送记录状态已变更。')

    @transactional
    def rejected(self, message, reason):
        self._send_problem(message, 'failed', reason)

    def _send_problem(self, message, status, reason):
        reason = bounded_text(reason, 500)
        msg = self.db.one("SELECT * FROM messages WHERE id=? AND status='sending'", (parse_uint(message),))
        if msg is None:
            raise RuleError('发送记录状态已变更。')
        batch = self.db.one('SELECT * FROM batches WHERE id=?', (msg['batch_id'],))
        self.db.execute('UPDATE messages SET status=?,reason=? WHERE id=?', (status, reason, message))
        self.db.execute("UPDATE batches SET status='review' WHERE id=?", (batch['id'],))
        self.audit(0, 'send_' + status, message, reason)
        if batch['kind'] == 'delivery':
            target = self._order_record(batch['ref'])
            prior_sent = self.db.one("SELECT id FROM messages WHERE batch_id=? AND status='sent'", (batch['id'],))
            if status == 'failed' and not prior_sent:
                self._cancel_batch(batch['id'])
                self._release(target, batch['actor'], reason)
            else:
                self.db.execute("UPDATE orders SET status='delivery_review' WHERE id=?", (target['id'],))
                for admin in self.admins:
                    self.note(admin, f'订单 {target["id"]} 交付待核实；消息记录 {message}。冻结积分保留，使用 /review 查看。', admin_only=True)

    def _cancel_batch(self, batch):
        self.db.execute("UPDATE batches SET status='failed' WHERE id=?", (batch,))
        self.db.execute("UPDATE messages SET status='cancelled' WHERE batch_id=? AND status='pending'", (batch,))

    @transactional
    def resolve(self, actor, message, delivered, reason):
        self.admin(actor)
        reason = bounded_text(reason, 500)
        if type(delivered) is not bool:
            raise RuleError('核实结果无效。')
        msg = self.db.one('SELECT m.*,b.ref,b.kind AS batch_kind FROM messages m JOIN batches b ON b.id=m.batch_id WHERE m.id=?', (parse_uint(message),))
        if msg is None or msg['batch_kind'] != 'delivery' or msg['status'] not in {'unknown', 'failed'}:
            raise RuleError('该记录不处于可人工核实状态。')
        target = self.order(actor, msg['ref'])
        if target['status'] != 'delivery_review':
            raise RuleError('订单不处于待核实状态。')
        self.audit(actor, 'resolve_sent' if delivered else 'resolve_failed', message, reason)
        if delivered:
            self.db.execute("UPDATE messages SET status='sent',reason=? WHERE id=?", ('人工核实：' + reason, message))
            self.db.execute("UPDATE batches SET status='queued' WHERE id=?", (msg['batch_id'],))
            self.db.execute("UPDATE orders SET status='delivering' WHERE id=?", (target['id'],))
            self._finish_batch(msg['batch_id'])
        else:
            self._cancel_batch(msg['batch_id'])
            self._release(target, actor, reason)

    @transactional
    def recover(self):
        for msg in self.db.all("SELECT id FROM messages WHERE status='sending'"):
            self._send_problem(msg['id'], 'unknown', '进程中断，发送结果未知')
        self._cancel_unauthorized_outbox()
        for row in self.db.all("""SELECT DISTINCT p.id FROM payments p LEFT JOIN payment_jobs j ON j.payment_id=p.id
            WHERE (p.status='creating' OR j.status='sending') AND p.status NOT IN ('paid','closed','closed_review')"""):
            reason = '进程中断，支付请求结果未知；不自动重建或入账'
            self.db.execute("UPDATE payments SET status='unknown',pay_url=NULL,reason=? WHERE id=?", (reason, row['id']))
            self.audit(0, 'payment_recovered_unknown', row['id'], reason)
        self.db.execute("UPDATE payment_jobs SET status='unknown' WHERE status='sending'")
