"""Bounded retention without backups; financial rows and delivery keys survive."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

KEY = 'retention_v1'
TERMINAL = "('completed','empty','cancel')"
PAYMENT_TERMINAL = "('credited','closed')"
TASKS = ('sessions', 'orders', 'drafts', 'inbox', 'inbox_archive', 'events')
SIZE_WARNING_BYTES = 100 * 1024 * 1024


def stamp(value):
    return value.strftime('%Y-%m-%d %H:%M:%S')


def references(document):
    return f"(SELECT value FROM json_tree({document}) WHERE type='text')"


def protected(document, user=None):
    """Reject a document that refers to unresolved business (including admin sessions)."""
    values = references(document)
    owner = f'o.user_id={user} OR ' if user else ''
    pay_owner = f'p.user_id={user} OR ' if user else ''
    return f"""NOT EXISTS (
        SELECT 1 FROM orders o WHERE o.status NOT IN {TERMINAL}
        AND ({owner}o.id IN {values} OR o.batch_id IN {values}))
        AND NOT EXISTS (SELECT 1 FROM payments p WHERE p.state NOT IN {PAYMENT_TERMINAL}
        AND ({pay_owner}p.id IN {values}))"""


class Retention:
    def __init__(self, store, batch_size=200):
        self.s = store
        self.batch_size = max(1, min(int(batch_size), 500))

    def _load(self, c):
        row = c.execute('SELECT value FROM metadata WHERE key=?', (KEY,)).fetchone()
        return json.loads(row['value']) if row else None

    def _save(self, c, state):
        c.execute('INSERT INTO metadata(key,value) VALUES(?,?) '
                  'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                  (KEY, json.dumps(state, ensure_ascii=False, separators=(',', ':'))))

    def status(self):
        with self.s.connect(readonly=True) as c:
            state = self._load(c) or {'state': 'not_initialized'}
        path = Path(self.s.path)
        state['database_bytes'] = 0
        for p in (path, Path(str(path)+'-wal')):
            try: state['database_bytes'] += p.stat().st_size
            except FileNotFoundError: pass  # WAL may disappear after a checkpoint.
        return state

    def tick(self, now=None):
        now = now or datetime.now(timezone.utc).replace(tzinfo=None)
        size = self.status()['database_bytes']
        if size >= SIZE_WARNING_BYTES:
            with self.s.tx() as c:
                self.s.event(c, 'retention-size:'+now.strftime('%Y-%m-%d'), 'maintenance',
                    self.s.admin_id, {'text': f'数据库容量提醒：数据库及 WAL 合计 {size/1024/1024:.1f} MiB，'
                        '已达到 100 MiB。请检查增长来源；不会按容量自动删单。'})
        with self.s.tx() as c:
            state = self._load(c)
            if state is None:
                self._save(c, {'state': 'scheduled', 'last_success': None,
                    'next_run': stamp(now+timedelta(days=14)), 'counts': {}})
                return None
            if state['state'] not in ('running', 'failed') and stamp(now) < state['next_run']:
                return None
            if state['state'] not in ('running', 'failed'):
                state.update(state='running', started_at=stamp(now), counts={}, error=None)
                self._save(c, state)
        try:
            more = False
            for task in TASKS:
                with self.s.tx() as c:
                    state = self._load(c)
                    # Another polling process must never race a finished schedule.
                    if state['state'] not in ('running', 'failed'):
                        return None
                    state['state'] = 'running'
                    count = self._batch(c, task, datetime.strptime(state['started_at'], '%Y-%m-%d %H:%M:%S'))
                    state['counts'][task] = state['counts'].get(task, 0) + count
                    state['error'] = None
                    self._save(c, state)
                    more = more or count == self.batch_size
            if not more:
                with self.s.tx() as c:
                    state = self._load(c)
                    state.update(state='complete', last_success=stamp(now),
                                 next_run=stamp(now+timedelta(days=14)), error=None)
                    self._save(c, state)
                    self.s.event(c, 'retention-ok:'+state['started_at'], 'maintenance',
                        self.s.admin_id, {'text': '数据库定期清理完成。共处理 '+
                        str(sum(state['counts'].values()))+' 条记录；未创建备份。'})
            return state
        except Exception as error:
            with self.s.tx() as c:
                state = self._load(c)
                state.update(state='failed', error=type(error).__name__)
                self._save(c, state)
                self.s.event(c, 'retention-error:'+state['started_at'], 'maintenance',
                    self.s.admin_id, {'text': '数据库清理失败（'+type(error).__name__+
                        '）。当前批已回滚，稍后继续。请查看后台「数据库管理」。'})
            return state

    def _batch(self, c, task, started):
        short = stamp(started-timedelta(days=14))
        long = stamp(started-timedelta(days=30))
        limit = self.batch_size
        if task == 'sessions':
            rows = c.execute(f"SELECT s.user_id FROM sessions s WHERE s.updated_at<? AND "
                + protected('s.data', 's.user_id')
                + " AND NOT EXISTS (SELECT 1 FROM events e WHERE e.target=s.user_id AND e.state NOT IN ('sent','cancelled'))"
                + ' ORDER BY s.user_id LIMIT ?', (short, limit)).fetchall()
            c.executemany('DELETE FROM sessions WHERE user_id=?', [(r[0],) for r in rows])
        elif task == 'orders':
            values = references('e.payload')
            rows = c.execute(f"""SELECT o.id FROM orders o
                WHERE o.status IN {TERMINAL} AND o.finished_at<? AND o.data_cleared_at IS NULL
                AND NOT EXISTS (SELECT 1 FROM orders other WHERE other.batch_id=o.batch_id
                    AND (other.status NOT IN {TERMINAL} OR other.finished_at IS NULL OR other.finished_at>=?))
                AND NOT EXISTS (SELECT 1 FROM receipts r WHERE r.order_id=o.id AND r.state NOT IN ('sent','staged'))
                AND NOT EXISTS (SELECT 1 FROM events e WHERE e.state NOT IN ('sent','cancelled')
                    AND (o.id IN {values} OR o.batch_id IN {values}))
                AND NOT EXISTS (SELECT 1 FROM sessions s WHERE o.id IN {references('s.data')}
                    OR o.batch_id IN {references('s.data')})
                ORDER BY o.id LIMIT ?""", (long, long, limit)).fetchall()
            for row in rows:
                c.execute("UPDATE receipts SET payload=?,delivery_payload=NULL WHERE order_id=?",
                    (json.dumps({'kind': 'text', 'text': '回单内容已按保留规则清理'}, ensure_ascii=False), row[0]))
                c.execute('DELETE FROM reply_links WHERE order_id=?', (row[0],))
                c.execute("UPDATE orders SET submission='{}',data_cleared_at=? WHERE id=?", (stamp(started), row[0]))
        elif task == 'drafts':
            rows = c.execute(f"""SELECT d.id FROM drafts d WHERE (
                (d.status IN ('editing','abandoned') AND d.updated_at<?)
                OR (d.status='submitted' AND d.updated_at<? AND EXISTS
                    (SELECT 1 FROM orders o WHERE o.batch_id=d.batch_id)
                    AND NOT EXISTS (SELECT 1 FROM orders o WHERE o.batch_id=d.batch_id
                        AND o.data_cleared_at IS NULL)))
                AND NOT EXISTS (SELECT 1 FROM sessions s WHERE d.id IN {references('s.data')})
                AND NOT EXISTS (SELECT 1 FROM payment_cards pc JOIN payments p ON p.id=pc.payment_id
                    WHERE pc.return_draft=d.id AND p.state NOT IN {PAYMENT_TERMINAL})
                ORDER BY d.id LIMIT ?""", (short, long, limit)).fetchall()
            for row in rows:
                c.execute('UPDATE payment_cards SET return_draft=NULL WHERE return_draft=?', (row[0],))
                c.execute('DELETE FROM drafts WHERE id=?', (row[0],))
        elif task in ('inbox', 'inbox_archive'):
            rows = c.execute(f"SELECT id FROM {task} WHERE state='done' AND created_at<? ORDER BY id LIMIT ?",
                             (long, limit)).fetchall()
            c.executemany(f'DELETE FROM {task} WHERE id=?', [(r[0],) for r in rows])
        else:
            rows = c.execute("SELECT e.id FROM events e WHERE e.state IN ('sent','cancelled') AND e.finished_at<? "
                'AND e.data_cleared_at IS NULL AND '+protected('e.payload')+' ORDER BY e.id LIMIT ?',
                (long, limit)).fetchall()
            c.executemany("UPDATE events SET payload='{}',data_cleared_at=? WHERE id=?",
                          [(stamp(started), r[0]) for r in rows])
        return len(rows)
