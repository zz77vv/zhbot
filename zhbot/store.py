"""SQLite domain boundary. All money mutations are serialized and journaled."""
import json
import os
import sqlite3
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path

class DomainError(ValueError):
    pass


class InsufficientPoints(DomainError):
    def __init__(self,missing):
        self.missing=missing
        super().__init__(f'还差 {missing} 积分，资料已保留。')

def uid():
    return uuid.uuid4().hex[:16]

def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))

class Store:
    def __init__(self, path, admin_id, *, readonly=False):
        self.path, self.admin_id = str(path), admin_id
        self.readonly = readonly
        if readonly:
            if not Path(self.path).is_file():
                raise FileNotFoundError('Source database does not exist')
            with self.connect() as c:
                c.execute('SELECT version FROM schema_migrations LIMIT 1').fetchone()
                c.execute('SELECT user_id, available, frozen FROM wallets LIMIT 1').fetchone()
                c.execute('SELECT event_key FROM ledger LIMIT 1').fetchone()
            return
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.execute('PRAGMA journal_mode=WAL')
            c.execute('CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY)')
            for file in sorted((Path(__file__).parent.parent / 'migrations').glob('*.sql')):
                version = int(file.stem.split('_')[0])
                if not c.execute('SELECT 1 FROM schema_migrations WHERE version=?',(version,)).fetchone():
                    c.executescript('BEGIN IMMEDIATE;\n'+file.read_text()+f'\nINSERT INTO schema_migrations VALUES({version});\nCOMMIT;')
    @contextmanager
    def connect(self, *, readonly=False):
        if self.readonly or readonly:
            uri=Path(self.path).resolve().as_uri()+'?mode=ro'
            c=sqlite3.connect(uri, uri=True, timeout=15, isolation_level=None)
        else:
            c=sqlite3.connect(self.path, timeout=15, isolation_level=None)
        c.row_factory=sqlite3.Row
        c.execute('PRAGMA foreign_keys=ON'); c.execute('PRAGMA synchronous=FULL')
        try: yield c
        finally: c.close()
    @contextmanager
    def tx(self):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            try: yield c; c.commit()
            except BaseException: c.rollback(); raise
    def rows(self, sql, args=()):
        with self.connect() as c: return [dict(r) for r in c.execute(sql,args)]
    def one(self, sql, args=()):
        rows=self.rows(sql,args)
        if not rows: raise DomainError('记录不存在')
        return rows[0]
    def admin(self, actor):
        if actor!=self.admin_id: raise DomainError('仅管理员可操作')
    def audit(self,c,actor,action,target,detail=''):
        c.execute('INSERT INTO audit(actor,action,target,detail) VALUES(?,?,?,?)',(actor,action,str(target),detail))
    def balance(self,user):
        r=self.rows('SELECT available,frozen FROM wallets WHERE user_id=?',(user,))
        return (r[0]['available'],r[0]['frozen']) if r else (0,0)
    def money(self,c,user,available,frozen,key,source,reason=''):
        if c.execute('SELECT 1 FROM ledger WHERE event_key=?',(key,)).fetchone(): return False
        c.execute('INSERT OR IGNORE INTO wallets(user_id) VALUES(?)',(user,))
        r=c.execute('SELECT * FROM wallets WHERE user_id=?',(user,)).fetchone()
        a,f=r['available']+available,r['frozen']+frozen
        if a<0 or f<0: raise DomainError('积分不足或冻结记录不一致')
        c.execute('UPDATE wallets SET available=?,frozen=? WHERE user_id=?',(a,f,user))
        c.execute('INSERT INTO ledger(event_key,user_id,available_delta,frozen_delta,available_after,frozen_after,source,reason) VALUES(?,?,?,?,?,?,?,?)',(key,user,available,frozen,a,f,source,reason))
        return True
    def adjust(self,actor,user,amount,reason,key):
        self.admin(actor)
        if type(amount)!=int or not amount or not reason.strip(): raise DomainError('积分必须是非零整数且原因必填')
        with self.tx() as c:
            if self.money(c,user,amount,0,'adjust:'+key,'管理员调整／其他收款',reason): self.audit(c,actor,'adjust',user,dump({'amount':amount,'reason':reason}))
    def product(self,pid):
        p=self.one('SELECT * FROM products WHERE id=?',(pid,)); p['fields']=json.loads(p['fields']); return p
    def products(self,all_items=False,placement=None):
        products=self.rows(
            'SELECT * FROM products WHERE (? OR active=1) AND (? IS NULL OR placement=?) ORDER BY position,id',
            (all_items,placement,placement),
        )
        for product in products:
            product['fields']=json.loads(product['fields'])
        return products
    def save_product(self,actor,p):
        self.admin(actor)
        if not p['name'].strip() or type(p['price'])!=int or p['price']<1: raise DomainError('名称与正整数价格必填')
        fields=p['fields']
        if not fields or len(fields)>10 or any(not f['name'].strip() or f['kind'] not in ('text','photo','document') for f in fields): raise DomainError('字段配置无效（1至10项）')
        with self.tx() as c:
            placement=p.get('placement')
            if placement is not None and placement not in ('home','region'):
                raise DomainError('商品展示位置无效。')
            values=(p['name'],p['price'],p['description'],p['eta'],dump(fields),int(p.get('active',1)))
            if p.get('id'):
                pid=p['id']
                c.execute('UPDATE products SET name=?,price=?,description=?,eta=?,fields=?,active=?,placement=COALESCE(?,placement),revision=revision+1 WHERE id=?',values+(placement,pid))
            else:
                pos=c.execute('SELECT COALESCE(MAX(position),0)+1 FROM products').fetchone()[0]
                pid=c.execute('INSERT INTO products(name,price,description,eta,fields,active,position,placement) VALUES(?,?,?,?,?,?,?,?)',values+(pos,placement or 'home')).lastrowid
            self.audit(c,actor,'product',pid,dump(p))
            return pid
    def move_product(self,actor,pid,direction):
        self.admin(actor)
        with self.tx() as c:
            items=c.execute('SELECT id,position FROM products WHERE placement=(SELECT placement FROM products WHERE id=?) ORDER BY position,id',(pid,)).fetchall()
            i=next(i for i,p in enumerate(items) if p['id']==pid); j=i+direction
            if 0<=j<len(items):
                c.execute('UPDATE products SET position=? WHERE id=?',(items[j]['position'],pid))
                c.execute('UPDATE products SET position=? WHERE id=?',(items[i]['position'],items[j]['id']))
            self.audit(c,actor,'move',pid,str(direction))
    def new_draft(self,user,pid):
        with self.tx() as c:
            p=c.execute('SELECT * FROM products WHERE id=? AND active=1',(pid,)).fetchone()
            if not p: raise DomainError('商品已下架')
            c.execute("UPDATE drafts SET status='abandoned' WHERE user_id=? AND status='editing'",(user,))
            d=uid(); c.execute('INSERT INTO drafts(id,user_id,product_id,revision) VALUES(?,?,?,?)',(d,user,pid,p['revision'])); return d
    def draft(self,user,d):
        r=self.one('SELECT * FROM drafts WHERE id=? AND user_id=?',(d,user)); r['data']=json.loads(r['data']); return r
    def set_field(self,user,d,index,payload,delete=None,expected_version=None):
        with self.tx() as c:
            r=c.execute("SELECT * FROM drafts WHERE id=? AND user_id=? AND status='editing'",(d,user)).fetchone()
            if not r: raise DomainError('草稿已失效，请重新选商品')
            if expected_version is not None and r['edit_version']!=expected_version:
                raise DomainError('资料已修改，请使用最新的删除按钮。')
            p=self.product(r['product_id']); fields=p['fields']
            if index<0 or index>=len(fields): raise DomainError('字段不存在')
            data=json.loads(r['data']); key=str(index)
            if delete is not None:
                items=data.get(key,[])
                if 0<=delete<len(items): items.pop(delete)
                data[key]=items
            else:
                if payload.get('kind')!=fields[index]['kind']: raise DomainError('请按字段要求发送文字／图片／文件')
                if payload['kind']=='text':
                    if not payload.get('text','').strip(): raise DomainError('文字不能为空')
                    data[key]=[payload]
                else: data.setdefault(key,[]).append(payload)
            c.execute('UPDATE drafts SET data=?,edit_version=edit_version+1 WHERE id=?',(dump(data),d))
    def abandon(self,user,d):
        with self.tx() as c: c.execute("UPDATE drafts SET status='abandoned' WHERE id=? AND user_id=? AND status='editing'",(d,user))
    def submissions(self,r,p):
        data=r['data']
        if any(not data.get(str(i)) for i in range(len(p['fields']))): raise DomainError('请填写所有字段')
        if len(p['fields'])==1 and p['fields'][0]['kind']=='text':
            lines=[s.strip() for s in data['0'][0]['text'].splitlines() if s.strip()]
            if not lines or len(lines)>100: raise DomainError('批量支持1至100行')
            return [{'0':[{'kind':'text','text':line}]} for line in lines]
        return [data]
    def confirm(self,user,d,expected_version=None):
        error=None; batch=None
        with self.tx() as c:
            r=c.execute('SELECT * FROM drafts WHERE id=? AND user_id=?',(d,user)).fetchone()
            if not r: raise DomainError('草稿不存在')
            if r['status']=='submitted': return r['batch_id']
            if r['status']!='editing': raise DomainError('草稿已失效，请重新选商品')
            if expected_version is not None and r['edit_version']!=expected_version: raise DomainError('草稿已修改，请使用最新预览确认')
            p=self.product(r['product_id'])
            if not p['active'] or p['revision']!=r['revision']: raise DomainError('商品已变更，请重新选择')
            subs=self.submissions(dict(r,data=json.loads(r['data'])),p); total=len(subs)*p['price']
            available=self.balance(user)[0]
            if available<total:
                c.execute('UPDATE drafts SET edit_version=edit_version+1 WHERE id=?',(d,))
                error=InsufficientPoints(total-available)
            else:
                batch=uid(); c.execute('INSERT INTO batches(id,user_id) VALUES(?,?)',(batch,user))
                for sub in subs:
                    o=uid(); c.execute('INSERT INTO orders(id,batch_id,user_id,price,snapshot,submission) VALUES(?,?,?,?,?,?)',(o,batch,user,p['price'],dump(p),dump(sub)))
                    self.money(c,user,-p['price'],p['price'],'freeze:'+o,'订单冻结')
                c.execute("UPDATE drafts SET status='submitted',batch_id=? WHERE id=?",(batch,d))
                self.event(c,'new:'+batch,'new',self.admin_id,{'batch':batch})
        if error: raise error
        return batch
    def orders(self,batch=None):
        return self.rows('SELECT * FROM orders'+(' WHERE batch_id=?' if batch else '')+' ORDER BY rowid',(batch,) if batch else ())
    def order(self,o): return self.one('SELECT * FROM orders WHERE id=?',(o,))
    def receipts(self,o): return self.rows('SELECT * FROM receipts WHERE order_id=? ORDER BY id',(o,))
    def bulk_rows(self,actor,batch):
        self.admin(actor)
        with self.connect() as c:
            return self._bulk_rows(c,batch)
    def _bulk_rows(self,c,batch):
        orders=c.execute(
            "SELECT o.*, EXISTS(SELECT 1 FROM receipts WHERE order_id=o.id) AS has_receipt "
            "FROM orders o WHERE o.batch_id=? ORDER BY o.rowid",(batch,)
        ).fetchall()
        if len(orders)<2 or any(o['status']!='pending' or o['has_receipt'] for o in orders):
            raise DomainError('仅全批待处理且未暂存回单时可批量回单。')
        rows={}
        for o in orders:
            fields=json.loads(o['snapshot'])['fields']
            if len(fields)!=1 or fields[0]['kind']!='text':
                raise DomainError('批量回单仅支持单个文字字段的商品。')
            account=json.loads(o['submission'])['0'][0]['text']
            if len(account.split())!=1 or account in rows:
                raise DomainError('批次含重复账号或带空格的资料，请逐单处理。')
            rows[account]=dict(o)
        return rows
    def queue_bulk(self,actor,batch,results):
        self.admin(actor)
        with self.tx() as c:
            orders=self._bulk_rows(c,batch)
            if orders.keys()!=results.keys() or any(not value.strip() or '\n' in value or '\r' in value for value in results.values()):
                raise DomainError('回单必须与这批账号一一对应。')
            items={order['id']:results[account] for account,order in orders.items()}
            charged=sum(o['price'] for account,o in orders.items() if results[account]!='空')
            returned=sum(o['price'] for account,o in orders.items() if results[account]=='空')
            lines='\n'.join(f'{account} {results[account]}' for account in orders)
            text=f'✉️ 回单结果\n\n{lines}\n\n扣费 {charged} 积分 · 退回 {returned} 积分'
            if len(text.encode('utf-16-le'))//2>4000:
                raise DomainError('回单太长，请缩短结果或逐单处理。')
            first=next(iter(orders.values()))
            c.execute("UPDATE orders SET status='bulk_delivering' WHERE batch_id=?",(batch,))
            c.execute('INSERT INTO events(event_key,kind,target,payload) VALUES(?,?,?,?)',
                      ('bulk:'+uid(),'bulk_receipt',first['user_id'],dump({'batch':batch,'items':items,'text':text,'actor':actor})))
            self.audit(c,actor,'bulk_queue',batch,f'{len(items)} 单')
    def finish_bulk(self,c,event,message_id,*,cancel=False):
        if not cancel and (type(message_id) is not int or message_id<=0):
            raise DomainError('缺少回单消息编号。')
        payload=json.loads(event['payload'])
        orders=c.execute('SELECT * FROM orders WHERE batch_id=?',(payload['batch'],)).fetchall()
        if not orders or any(o['status']!='bulk_delivering' for o in orders):
            raise DomainError('批次状态已改变，请核实回单。')
        for order in orders:
            oid=order['id']
            result=payload['items'][oid]
            price=order['price']
            if cancel or result=='空':
                reason='批量回单确认未送达' if cancel else '批量回单空单'
                self.money(c,order['user_id'],price,-price,'refund:'+oid,reason)
                c.execute('UPDATE orders SET status=?,reason=? WHERE id=?',('cancel' if cancel else 'empty',reason,oid))
            else:
                self.money(c,order['user_id'],0,-price,'charge:'+oid,'批量回单扣费')
                c.execute("UPDATE orders SET status='completed' WHERE id=?",(oid,))
                c.execute("INSERT INTO receipts(order_id,payload,state,message_id) VALUES(?,?,'sent',?)",
                          (oid,dump({'kind':'text','text':result}),message_id))
        self.audit(c,payload['actor'],'bulk_cancel' if cancel else 'bulk_finish',payload['batch'],str(message_id))
    def add_receipt(self,actor,o,payload):
        self.admin(actor)
        if payload.get('kind') not in ('text','photo','document'): raise DomainError('回单类型不支持')
        with self.tx() as c:
            self.require_pending(c,o)
            c.execute('INSERT INTO receipts(order_id,payload) VALUES(?,?)',(o,dump(payload)))
            self.audit(c,actor,'receipt_add',o)
    def require_pending(self,c,o):
        r=c.execute('SELECT * FROM orders WHERE id=?',(o,)).fetchone()
        if not r or r['status']!='pending': raise DomainError('订单已锁定或已结束')
        return r
    def delete_receipt(self,actor,o,rid):
        self.admin(actor)
        with self.tx() as c:
            self.require_pending(c,o)
            c.execute('DELETE FROM receipts WHERE id=? AND order_id=?',(rid,o)); self.audit(c,actor,'receipt_delete',o,str(rid))
    def begin_delivery(self,actor,o,expected_receipts=None):
        self.admin(actor)
        with self.tx() as c:
            self.require_pending(c,o)
            receipts=[dict(r) for r in c.execute('SELECT id,payload,state FROM receipts WHERE order_id=? ORDER BY id',(o,))]
            if not receipts: raise DomainError('还没有回单，请先添加；无结果请用空单退分。')
            if expected_receipts is not None and receipts!=expected_receipts:
                raise DomainError('回单已修改，请重新预览后发送。')
            if any(r['state']!='staged' for r in receipts):
                raise DomainError('回单状态已改变，请核实后再处理。')
            c.execute("UPDATE orders SET status='delivering' WHERE id=?",(o,)); self.audit(c,actor,'deliver',o)
    def mark_sending(self,rid):
        with self.tx() as c:
            n=c.execute("UPDATE receipts SET state='sending' WHERE id=? AND state='staged' AND order_id IN (SELECT id FROM orders WHERE status='delivering')",(rid,)).rowcount
            if n!=1: raise DomainError('回单不可重复投递')
    def mark_sent(self,rid,message_id):
        with self.tx() as c: c.execute("UPDATE receipts SET state='sent',message_id=? WHERE id=? AND state='sending'",(message_id,rid))
    def uncertain(self,o):
        with self.tx() as c: c.execute("UPDATE orders SET status='uncertain' WHERE id=? AND status='delivering'",(o,))
    def reject_receipt(self,o,rid,retry_after):
        with self.tx() as c:
            updated=c.execute("UPDATE receipts SET state='staged' WHERE id=? AND order_id=? AND state='sending'",(rid,o))
            if updated.rowcount!=1: raise DomainError('回单状态已变更')
            if retry_after is None:
                c.execute("UPDATE orders SET status='uncertain',retry_at=NULL WHERE id=?",(o,))
            else:
                c.execute("UPDATE orders SET retry_at=datetime('now',?) WHERE id=?",(f'+{retry_after} seconds',o))
    def resolve(self,actor,rid,delivered,message_id,evidence):
        self.admin(actor)
        if not evidence.strip() or (delivered and message_id<=0): raise DomainError('必须填写核实证据；已送达须提供消息ID')
        with self.tx() as c:
            r=c.execute("SELECT * FROM receipts WHERE id=? AND state='sending'",(rid,)).fetchone()
            if not r: raise DomainError('没有待核实回单')
            c.execute('UPDATE receipts SET state=?,message_id=? WHERE id=?',('sent' if delivered else 'staged',message_id if delivered else None,rid))
            if not c.execute("SELECT 1 FROM receipts WHERE order_id=? AND state='sending'",(r['order_id'],)).fetchone(): c.execute("UPDATE orders SET status='delivering' WHERE id=?",(r['order_id'],))
            self.audit(c,actor,'resolve',rid,evidence)
    def resume(self,actor,o,evidence):
        self.admin(actor)
        if not evidence.strip(): raise DomainError('核实证据必填')
        with self.tx() as c:
            r=c.execute("SELECT * FROM orders WHERE id=? AND status='uncertain'",(o,)).fetchone()
            if not r or c.execute("SELECT 1 FROM receipts WHERE order_id=? AND state='sending'",(o,)).fetchone(): raise DomainError('须先逐条核实所有不确定回单')
            c.execute("UPDATE orders SET status='delivering' WHERE id=?",(o,))
            self.audit(c,actor,'resume',o,evidence)
    def settle(self,o):
        with self.tx() as c:
            r=c.execute('SELECT * FROM orders WHERE id=?',(o,)).fetchone()
            if r['status']=='completed': return
            if r['status']!='delivering' or c.execute("SELECT 1 FROM receipts WHERE order_id=? AND state!='sent'",(o,)).fetchone(): raise DomainError('交付尚未可靠确认')
            self.money(c,r['user_id'],0,-r['price'],'charge:'+o,'交付扣费')
            c.execute("UPDATE orders SET status='completed' WHERE id=?",(o,)); self.summary(c,r['batch_id'])
    def refund(self,actor,o,kind,reason):
        self.admin(actor)
        if kind not in ('empty','cancel'): raise DomainError('退款类型无效')
        with self.tx() as c:
            old=c.execute('SELECT * FROM orders WHERE id=?',(o,)).fetchone()
            if old and old['status']==kind: return
            if old and old['status']=='uncertain' and kind=='cancel':
                if not reason.strip() or c.execute("SELECT 1 FROM receipts WHERE order_id=? AND state!='staged'",(o,)).fetchone():
                    raise DomainError('仅全部回单明确未送达且填写原因时才可取消')
                r=old
            else:
                r=self.require_pending(c,o)
            if kind=='empty' and c.execute('SELECT 1 FROM receipts WHERE order_id=?',(o,)).fetchone(): raise DomainError('有回单不可按空单退款')
            self.money(c,r['user_id'],r['price'],-r['price'],'refund:'+o,'空单退款' if kind=='empty' else '管理员取消',reason)
            c.execute('UPDATE orders SET status=?,reason=? WHERE id=?',(kind,reason,o)); self.audit(c,actor,kind,o,reason)
            self.event(c,'refund:'+o,'refund',r['user_id'],{'order':o,'points':r['price'],'reason':reason}); self.summary(c,r['batch_id'])
    def summary(self,c,b):
        rows=c.execute('SELECT * FROM orders WHERE batch_id=?',(b,)).fetchall()
        if any(r['status'] not in ('completed','empty','cancel') for r in rows): return
        self.event(c,'summary:'+b,'summary',rows[0]['user_id'],{'batch':b,'success':sum(r['status']=='completed' for r in rows),'refund':sum(r['status']!='completed' for r in rows),'charged':sum(r['price'] for r in rows if r['status']=='completed'),'returned':sum(r['price'] for r in rows if r['status']!='completed')})
    def event(self,c,key,kind,target,payload):
        c.execute('INSERT OR IGNORE INTO events(event_key,kind,target,payload) VALUES(?,?,?,?)',(key,kind,target,dump(payload)))
    def events(self): return self.rows('SELECT * FROM events ORDER BY id')
    def recover(self):
        with self.tx() as c:
            c.execute("UPDATE orders SET status='uncertain' WHERE status='delivering' AND id IN (SELECT order_id FROM receipts WHERE state='sending')")
            c.execute("UPDATE events SET state='uncertain' WHERE state='sending'")
    def session(self,user):
        r=self.rows('SELECT data FROM sessions WHERE user_id=?',(user,)); return json.loads(r[0]['data']) if r else {}
    def set_session(self,user,data):
        with self.tx() as c: c.execute('INSERT INTO sessions(user_id,data) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET data=excluded.data',(user,dump(data)))
    def link_reply(self,message_id,o):
        with self.tx() as c: c.execute('INSERT OR IGNORE INTO reply_links VALUES(?,?)',(message_id,o))
    def backup(self,dest):
        destination=Path(dest).absolute()
        if os.path.lexists(destination):
            raise FileExistsError('Backup destination already exists')
        fd, name=tempfile.mkstemp(prefix='.zhbot-backup-', dir=destination.parent)
        staging=Path(name)
        os.close(fd)
        try:
            with self.connect(readonly=True) as source:
                target=sqlite3.connect(staging)
                try:
                    source.backup(target)
                    if target.execute('PRAGMA integrity_check').fetchall()!=[('ok',)]:
                        raise sqlite3.DatabaseError('Backup integrity check failed')
                finally:
                    target.close()
            with staging.open('r+b') as completed:
                os.fsync(completed.fileno())
            # Publish only a complete snapshot, with no replacement of any existing path.
            if os.name=='nt':
                os.rename(staging,destination)
            else:
                os.link(staging,destination)
        finally:
            staging.unlink(missing_ok=True)
