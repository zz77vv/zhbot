"""SQLite domain boundary. All money mutations are serialized and journaled."""
import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

class DomainError(ValueError):
    pass

def uid():
    return uuid.uuid4().hex[:16]

def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))

class Store:
    def __init__(self, path, admin_id):
        self.path, self.admin_id = str(path), admin_id
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.execute('PRAGMA journal_mode=WAL')
            c.execute('CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY)')
            for file in sorted((Path(__file__).parent.parent / 'migrations').glob('*.sql')):
                version = int(file.stem.split('_')[0])
                if not c.execute('SELECT 1 FROM schema_migrations WHERE version=?',(version,)).fetchone():
                    c.executescript('BEGIN IMMEDIATE;\n'+file.read_text()+f'\nINSERT INTO schema_migrations VALUES({version});\nCOMMIT;')
    @contextmanager
    def connect(self):
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
    def products(self,all_items=False):
        return [self.product(r['id']) for r in self.rows('SELECT id FROM products '+('' if all_items else 'WHERE active=1 ')+'ORDER BY position,id')]
    def save_product(self,actor,p):
        self.admin(actor)
        if not p['name'].strip() or type(p['price'])!=int or p['price']<1: raise DomainError('名称与正整数价格必填')
        fields=p['fields']
        if not fields or len(fields)>10 or any(not f['name'].strip() or f['kind'] not in ('text','photo','document') for f in fields): raise DomainError('字段配置无效（1至10项）')
        with self.tx() as c:
            values=(p['name'],p['price'],p['description'],p['eta'],dump(fields),int(p.get('active',1)))
            if p.get('id'):
                pid=p['id']; c.execute('UPDATE products SET name=?,price=?,description=?,eta=?,fields=?,active=?,revision=revision+1 WHERE id=?',values+(pid,))
            else:
                pos=c.execute('SELECT COALESCE(MAX(position),0)+1 FROM products').fetchone()[0]
                pid=c.execute('INSERT INTO products(name,price,description,eta,fields,active,position) VALUES(?,?,?,?,?,?,?)',values+(pos,)).lastrowid
            self.audit(c,actor,'product',pid,dump(p))
            return pid
    def move_product(self,actor,pid,direction):
        self.admin(actor)
        with self.tx() as c:
            items=c.execute('SELECT id,position FROM products ORDER BY position,id').fetchall()
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
    def set_field(self,user,d,index,payload,delete=None):
        with self.tx() as c:
            r=c.execute("SELECT * FROM drafts WHERE id=? AND user_id=? AND status='editing'",(d,user)).fetchone()
            if not r: raise DomainError('草稿已失效，请重新选商品')
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
                c.execute("UPDATE drafts SET status='rejected' WHERE id=?",(d,))
                error=f'整批拒绝：需要 {total}，现有 {available}，差额 {total-available} 积分。请充值后重新选商品报单。'
            else:
                batch=uid(); c.execute('INSERT INTO batches(id,user_id) VALUES(?,?)',(batch,user))
                for sub in subs:
                    o=uid(); c.execute('INSERT INTO orders(id,batch_id,user_id,price,snapshot,submission) VALUES(?,?,?,?,?,?)',(o,batch,user,p['price'],dump(p),dump(sub)))
                    self.money(c,user,-p['price'],p['price'],'freeze:'+o,'订单冻结')
                c.execute("UPDATE drafts SET status='submitted',batch_id=? WHERE id=?",(batch,d))
                self.event(c,'new:'+batch,'new',self.admin_id,{'batch':batch})
        if error: raise DomainError(error)
        return batch
    def orders(self,batch=None):
        return self.rows('SELECT * FROM orders'+(' WHERE batch_id=?' if batch else '')+' ORDER BY rowid',(batch,) if batch else ())
    def order(self,o): return self.one('SELECT * FROM orders WHERE id=?',(o,))
    def receipts(self,o): return self.rows('SELECT * FROM receipts WHERE order_id=? ORDER BY id',(o,))
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
    def begin_delivery(self,actor,o):
        self.admin(actor)
        with self.tx() as c:
            self.require_pending(c,o)
            if not c.execute('SELECT 1 FROM receipts WHERE order_id=?',(o,)).fetchone(): raise DomainError('无回单，使用空单退分')
            c.execute("UPDATE orders SET status='delivering' WHERE id=?",(o,)); self.audit(c,actor,'deliver',o)
    def mark_sending(self,rid):
        with self.tx() as c:
            n=c.execute("UPDATE receipts SET state='sending' WHERE id=? AND state='staged' AND order_id IN (SELECT id FROM orders WHERE status='delivering')",(rid,)).rowcount
            if n!=1: raise DomainError('回单不可重复投递')
    def mark_sent(self,rid,message_id):
        with self.tx() as c: c.execute("UPDATE receipts SET state='sent',message_id=? WHERE id=? AND state='sending'",(message_id,rid))
    def uncertain(self,o):
        with self.tx() as c: c.execute("UPDATE orders SET status='uncertain' WHERE id=? AND status='delivering'",(o,))
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
        with self.tx() as c: c.execute('INSERT INTO sessions VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET data=excluded.data',(user,dump(data)))
    def link_reply(self,message_id,o):
        with self.tx() as c: c.execute('INSERT OR IGNORE INTO reply_links VALUES(?,?)',(message_id,o))
    def backup(self,dest):
        with self.connect() as c:
            target=sqlite3.connect(str(dest))
            try: c.backup(target)
            finally: target.close()
