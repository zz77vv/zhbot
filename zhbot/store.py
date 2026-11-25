"""SQLite domain boundary. All money mutations are serialized and journaled."""
import json
import os
import sqlite3
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from .receipt_content import bulk_sections, combined_receipt, original_submission

LEGACY_TIERS = {
    'owner': {'新菜': 1, '大众名': 2, '剩菜': 12},
    'secondary': {'新菜': 3, '大众名': 4, '剩菜': 12},
}

def product_tiers(product):
    if 'tiers' in product:
        return json.loads(product['tiers'])
    return LEGACY_TIERS.get(product.get('price_profile'), {})

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
            existing=c.execute('SELECT tiers FROM products WHERE id=?',(p.get('id'),)).fetchone()
            tiers=json.loads(existing['tiers']) if existing else {}
            price=min(tiers.values()) if tiers else p['price']
            values=(p['name'],price,p['description'],p['eta'],dump(fields),int(p.get('active',1)))
            if p.get('id'):
                pid=p['id']
                c.execute('UPDATE products SET name=?,price=?,description=?,eta=?,fields=?,active=?,placement=COALESCE(?,placement),revision=revision+1 WHERE id=?',values+(placement,pid))
            else:
                pos=c.execute('SELECT COALESCE(MAX(position),0)+1 FROM products').fetchone()[0]
                pid=c.execute('INSERT INTO products(name,price,description,eta,fields,active,position,placement) VALUES(?,?,?,?,?,?,?,?)',values+(pos,placement or 'home')).lastrowid
            self.audit(c,actor,'product',pid,dump(p))
            return pid
    def set_product_tiers(self,actor,pid,revision,tiers):
        self.admin(actor)
        if (len(tiers)>10 or len(set(tiers.values()))!=len(tiers)
                or any(not name.strip() or len(name)>30 or type(price)!=int or not 1<=price<=10**9
                       for name,price in tiers.items())):
            raise DomainError('最多 10 档，名称 1 至 30 字，价格须为不同的正整数。')
        with self.tx() as c:
            row=c.execute('SELECT price,revision FROM products WHERE id=?',(pid,)).fetchone()
            if not row or row['revision']!=revision:
                raise DomainError('商品已更新，请重新打开档位设置。')
            price=min(tiers.values()) if tiers else row['price']
            c.execute('UPDATE products SET tiers=?,price=?,price_profile=NULL,revision=revision+1 WHERE id=?',
                      (dump(tiers),price,pid))
            self.audit(c,actor,'product_tiers',pid,dump(tiers))
    def move_product(self,actor,pid,direction):
        self.admin(actor)
        with self.tx() as c:
            items=c.execute('SELECT id,position FROM products WHERE placement=(SELECT placement FROM products WHERE id=?) ORDER BY position,id',(pid,)).fetchall()
            i=next(i for i,p in enumerate(items) if p['id']==pid); j=i+direction
            if 0<=j<len(items):
                c.execute('UPDATE products SET position=? WHERE id=?',(items[j]['position'],pid))
                c.execute('UPDATE products SET position=? WHERE id=?',(items[i]['position'],items[j]['id']))
            self.audit(c,actor,'move',pid,str(direction))
    def new_draft(self,user,pid,*,tier_price=None,expected_revision=None):
        with self.tx() as c:
            p=c.execute('SELECT * FROM products WHERE id=? AND active=1',(pid,)).fetchone()
            if not p: raise DomainError('商品已下架')
            if tier_price is not None and (expected_revision!=p['revision'] or tier_price not in product_tiers(dict(p)).values()):
                raise DomainError('档位已更新，请重新打开商品选择。')
            c.execute("UPDATE drafts SET status='abandoned' WHERE user_id=? AND status='editing'",(user,))
            d=uid()
            c.execute('INSERT INTO drafts(id,user_id,product_id,revision,tier_price) VALUES(?,?,?,?,?)',
                      (d,user,pid,p['revision'],tier_price))
            return d
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
            price=r['tier_price'] if r['tier_price'] is not None else p['price']
            subs=self.submissions(dict(r,data=json.loads(r['data'])),p); total=len(subs)*price
            available=self.balance(user)[0]
            if available<total:
                c.execute('UPDATE drafts SET edit_version=edit_version+1 WHERE id=?',(d,))
                error=InsufficientPoints(total-available)
            else:
                batch=uid(); c.execute('INSERT INTO batches(id,user_id) VALUES(?,?)',(batch,user))
                for sub in subs:
                    o=uid(); c.execute('INSERT INTO orders(id,batch_id,user_id,price,snapshot,submission) VALUES(?,?,?,?,?,?)',(o,batch,user,price,dump(p),dump(sub)))
                    self.money(c,user,-price,price,'freeze:'+o,'订单冻结')
                c.execute("UPDATE drafts SET status='submitted',batch_id=? WHERE id=?",(batch,d))
                self.event(c,'new:'+batch,'new',self.admin_id,{'batch':batch})
        if error: raise error
        return batch
    def orders(self,batch=None):
        return self.rows('SELECT * FROM orders'+(' WHERE batch_id=?' if batch else '')+' ORDER BY rowid',(batch,) if batch else ())
    def order(self,o): return self.one('SELECT * FROM orders WHERE id=?',(o,))
    def open_quote(self,order_id):
        rows=self.rows("SELECT * FROM order_quotes WHERE order_id=? AND state='pending'",(order_id,))
        return rows[0] if rows else None
    def offer_quote(self,actor,order_id,tier):
        self.admin(actor)
        with self.tx() as c:
            order=self.require_pending(c,order_id,allow_quote=True)
            tiers=product_tiers(json.loads(order['snapshot']))
            price=tiers.get(tier)
            if price is None or price<=order['price']:
                raise DomainError('请选择高于当前价格的档位。')
            if c.execute('SELECT 1 FROM receipts WHERE order_id=?',(order_id,)).fetchone():
                raise DomainError('请先删除暂存回单，再发起补差价。')
            existing=c.execute(
                "SELECT *,expires_at<=CURRENT_TIMESTAMP AS expired FROM order_quotes WHERE order_id=? AND state='pending'",
                (order_id,),
            ).fetchone()
            if existing and existing['expired']:
                raise DomainError('原报价已超时，订单将自动取消退款。')
            if existing and existing['tier']==tier:
                return
            c.execute("UPDATE order_quotes SET state='replaced' WHERE order_id=? AND state='pending'",(order_id,))
            quote_id=uid()
            c.execute("INSERT INTO order_quotes(id,order_id,price,tier,expires_at) VALUES(?,?,?,?,datetime('now','+24 hours'))",
                      (quote_id,order_id,price,tier))
            self.audit(c,actor,'quote',order_id,dump({'quote':quote_id,'price':price,'tier':tier}))
            self.event(c,'quote:'+quote_id,'quote',order['user_id'],{'order':order_id,'quote':quote_id})
    def resolve_quote(self,user,quote_id,accept):
        with self.tx() as c:
            quote=c.execute('SELECT * FROM order_quotes WHERE id=?',(quote_id,)).fetchone()
            if not quote:
                raise DomainError('报价不存在。')
            order=self.require_pending(c,quote['order_id'],allow_quote=True)
            if order['user_id']!=user or quote['state']!='pending':
                raise DomainError('报价已失效或不属于你。')
            if c.execute('SELECT ?<=CURRENT_TIMESTAMP',(quote['expires_at'],)).fetchone()[0]:
                self._close_quote(c,order,quote,'expired')
                return 'expired'
            if not accept:
                self._close_quote(c,order,quote,'declined')
                return 'declined'
            delta=quote['price']-order['price']
            wallet=c.execute('SELECT available FROM wallets WHERE user_id=?',(user,)).fetchone()
            available=wallet['available'] if wallet else 0
            if available<delta:
                raise InsufficientPoints(delta-available)
            self.money(c,user,-delta,delta,'quote_accept:'+quote_id,'订单补差价')
            c.execute('UPDATE orders SET price=? WHERE id=?',(quote['price'],order['id']))
            c.execute("UPDATE order_quotes SET state='accepted' WHERE id=?",(quote_id,))
            self.audit(c,user,'quote_accepted',order['id'],quote_id)
            self.event(c,'quote_accepted:'+quote_id,'quote_accepted',self.admin_id,
                       {'order':order['id'],'price':quote['price'],'tier':quote['tier']})
            return 'accepted'
    def expire_quotes(self):
        with self.tx() as c:
            quotes=c.execute("SELECT * FROM order_quotes WHERE state='pending' AND expires_at<=CURRENT_TIMESTAMP ORDER BY expires_at LIMIT 100").fetchall()
            for quote in quotes:
                order=self.require_pending(c,quote['order_id'],allow_quote=True)
                self._close_quote(c,order,quote,'expired')
    def _close_quote(self,c,order,quote,state):
        reason='补差价报价已超时' if state=='expired' else '客户拒绝补差价'
        c.execute('UPDATE order_quotes SET state=? WHERE id=?',(state,quote['id']))
        self._refund_pending(c,order,order['user_id'],'cancel',reason,source='补差价取消')
    def receipts(self,o,*,include_context=False):
        return self.rows('SELECT * FROM receipts WHERE order_id=? AND (? OR context=0) ORDER BY context DESC,id',
                         (o,include_context))
    def bulk_rows(self,actor,batch):
        self.admin(actor)
        with self.connect() as c:
            return self._bulk_rows(c,batch)
    def _bulk_rows(self,c,batch):
        orders=c.execute(
            "SELECT o.*, EXISTS(SELECT 1 FROM receipts WHERE order_id=o.id) AS has_receipt, "
            "EXISTS(SELECT 1 FROM order_quotes WHERE order_id=o.id AND state='pending') AS has_quote "
            "FROM orders o WHERE o.batch_id=? ORDER BY o.rowid",(batch,)
        ).fetchall()
        if len(orders)<2 or any(o['status']!='pending' or o['has_receipt'] or o['has_quote'] for o in orders):
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
            groups=bulk_sections(orders,results)
            if not groups:
                raise DomainError('有一条原报单和回单合计过长，请逐单发送以保留全文。')
            c.execute("UPDATE orders SET status='bulk_delivering' WHERE batch_id=?",(batch,))
            for index,group in enumerate(groups,1):
                items={order['id']:result for order,result,_ in group}
                charged=sum(order['price'] for order,result,_ in group if result!='空')
                returned=sum(order['price'] for order,result,_ in group if result=='空')
                part=f' · {index}/{len(groups)}' if len(groups)>1 else ''
                lines='\n\n'.join(text for _,_,text in group)
                text=f'✉️ 回单结果{part}\n\n{lines}\n\n扣费 {charged} 积分 · 退回 {returned} 积分'
                self.event(c,'bulk:'+uid(),'bulk_receipt',group[0][0]['user_id'],
                           {'batch':batch,'items':items,'text':text,'actor':actor})
            self.audit(c,actor,'bulk_queue',batch,f'{len(orders)} 单')
    def finish_bulk(self,c,event,message_id,*,cancel=False):
        if not cancel and (type(message_id) is not int or message_id<=0):
            raise DomainError('缺少回单消息编号。')
        payload=json.loads(event['payload'])
        orders=[o for o in c.execute('SELECT * FROM orders WHERE batch_id=?',(payload['batch'],))
                if o['id'] in payload['items']]
        if not orders or len(orders)!=len(payload['items']) or any(o['status']!='bulk_delivering' or o['user_id']!=event['target'] for o in orders):
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
    def require_pending(self,c,o,*,allow_quote=False):
        r=c.execute('SELECT * FROM orders WHERE id=?',(o,)).fetchone()
        if not r or r['status']!='pending': raise DomainError('订单已锁定或已结束')
        if not allow_quote and c.execute("SELECT 1 FROM order_quotes WHERE order_id=? AND state='pending'",(o,)).fetchone():
            raise DomainError('订单正在等待客户确认补差价。')
        return r
    def delete_receipt(self,actor,o,rid):
        self.admin(actor)
        with self.tx() as c:
            self.require_pending(c,o)
            c.execute('DELETE FROM receipts WHERE id=? AND order_id=?',(rid,o)); self.audit(c,actor,'receipt_delete',o,str(rid))
    def begin_delivery(self,actor,o,expected_receipts=None,*,include_submission=False):
        self.admin(actor)
        with self.tx() as c:
            order=self.require_pending(c,o)
            receipts=[dict(r) for r in c.execute('SELECT id,payload,state FROM receipts WHERE order_id=? ORDER BY id',(o,))]
            if not receipts: raise DomainError('还没有回单，请先添加；无结果请用空单退分。')
            if expected_receipts is not None and receipts!=expected_receipts:
                raise DomainError('回单已修改，请重新预览后发送。')
            if any(r['state']!='staged' for r in receipts):
                raise DomainError('回单状态已改变，请核实后再处理。')
            if include_submission:
                context=list(original_submission(order))
                combined=combined_receipt(context,[json.loads(item['payload']) for item in receipts])
                if combined:
                    c.execute('UPDATE receipts SET delivery_payload=? WHERE id=?',
                              (dump(combined),receipts[0]['id']))
                else:
                    c.executemany('INSERT INTO receipts(order_id,payload,context) VALUES(?,?,1)',
                                  ((o,dump(item)) for item in context))
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
                if not reason.strip() or c.execute("SELECT 1 FROM receipts WHERE order_id=? AND (state='sending' OR (context=0 AND state!='staged'))",(o,)).fetchone():
                    raise DomainError('仅全部回单明确未送达且填写原因时才可取消')
                r=old
            else:
                r=self.require_pending(c,o)
            if kind=='empty' and c.execute('SELECT 1 FROM receipts WHERE order_id=?',(o,)).fetchone(): raise DomainError('有回单不可按空单退款')
            self._refund_pending(c,r,actor,kind,reason)
    def _refund_pending(self,c,order,actor,kind,reason,*,source=None):
        order_id=order['id']
        source=source or ('空单退款' if kind=='empty' else '管理员取消')
        self.money(c,order['user_id'],order['price'],-order['price'],'refund:'+order_id,source,reason)
        c.execute('UPDATE orders SET status=?,reason=? WHERE id=?',(kind,reason,order_id))
        self.audit(c,actor,kind,order_id,reason)
        self.event(c,'refund:'+order_id,'refund',order['user_id'],
                   {'order':order_id,'points':order['price'],'reason':reason})
        self.summary(c,order['batch_id'])
    def summary(self,c,b):
        rows=c.execute('SELECT * FROM orders WHERE batch_id=?',(b,)).fetchall()
        if any(r['status'] not in ('completed','empty','cancel') for r in rows): return
        self.event(c,'summary:'+b,'summary',rows[0]['user_id'],{'batch':b,'success':sum(r['status']=='completed' for r in rows),'refund':sum(r['status']!='completed' for r in rows),'charged':sum(r['price'] for r in rows if r['status']=='completed'),'returned':sum(r['price'] for r in rows if r['status']!='completed')})
    def event(self,c,key,kind,target,payload):
        c.execute('INSERT OR IGNORE INTO events(event_key,kind,target,payload) VALUES(?,?,?,?)',(key,kind,target,dump(payload)))
    def events(self): return self.rows('SELECT * FROM events ORDER BY id')
    def uncertain_bulk_events(self,order_id):
        return self.rows(
            "SELECT e.* FROM events e WHERE e.kind='bulk_receipt' AND e.state='uncertain' "
            "AND EXISTS(SELECT 1 FROM json_each(e.payload,'$.items') WHERE key=?) ORDER BY e.id",(order_id,))
    def resolve_event(self,actor,event_id,choice,message_id,evidence=''):
        self.admin(actor)
        if choice not in ('sent','retry','cancel') or (choice=='sent' and message_id<1) or (choice!='sent' and message_id!=0):
            raise DomainError('已送达须填写消息ID，未送达请填 0。')
        with self.tx() as c:
            event=c.execute("SELECT * FROM events WHERE id=? AND state='uncertain'",(event_id,)).fetchone()
            if not event: raise DomainError('事件不在待核实状态')
            if choice=='cancel' and event['kind']!='bulk_receipt': raise DomainError('仅批量回单可取消')
            if event['kind']=='bulk_receipt' and choice in ('sent','cancel'):
                self.finish_bulk(c,event,message_id,cancel=choice=='cancel')
            state={'sent':'sent','retry':'pending','cancel':'cancelled'}[choice]
            c.execute('UPDATE events SET state=?,message_id=?,retry_at=NULL WHERE id=?',(state,message_id,event_id))
            self.audit(c,actor,'event_resolve',event_id,f'{choice}:{message_id} {evidence}'.strip())
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
