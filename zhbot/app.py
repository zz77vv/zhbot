"""Private-chat Telegram UI; domain authorization is also enforced in Store."""
import json
from .store import DomainError, uid


def keyboard(rows):
    return {'inline_keyboard':[[{'text':label,'callback_data':data} for label,data in row] for row in rows]}

def payload(m):
    if m.get('photo'): return {'kind':'photo','file_id':m['photo'][-1]['file_id'],'caption':m.get('caption','')}
    if m.get('document'): return {'kind':'document','file_id':m['document']['file_id'],'caption':m.get('caption','')}
    if m.get('text'): return {'kind':'text','text':m['text']}
    raise DomainError('仅支持文字、图片和文件')

class App:
    def __init__(self,store,telegram,payments=None):
        self.s,self.t,self.payments=store,telegram,payments
    def send(self,user,text,rows=None):
        chunks=[str(text)[i:i+2000] for i in range(0,len(str(text)),2000)] or ['（空）']
        for index,chunk in enumerate(chunks):
            args={'chat_id':user,'text':chunk,'link_preview_options':{'is_disabled':True}}
            if rows and index==len(chunks)-1: args['reply_markup']=keyboard(rows)
            result=self.t.call('sendMessage',**args)
        return result
    def send_payload(self,user,p):
        if p['kind']=='text': return self.t.call('sendMessage',chat_id=user,text=p['text'],link_preview_options={'is_disabled':True})
        method='sendPhoto' if p['kind']=='photo' else 'sendDocument'
        return self.t.call(method,chat_id=user,**{p['kind']:p['file_id']},caption=p.get('caption','')[:1024])
    def handle(self,update):
        q=update.get('callback_query'); m=q.get('message',{}) if q else update.get('message',{})
        actor=(q or m).get('from',{}).get('id'); chat=m.get('chat',{})
        if not actor or chat.get('type')!='private' or chat.get('id')!=actor: return
        try:
            if q:
                self.t.call('answerCallbackQuery',callback_query_id=q['id'])
                self.callback(actor,q.get('data',''))
            else: self.message(actor,m)
        except (DomainError,ValueError,KeyError,IndexError) as e:
            self.send(actor,str(e) if isinstance(e,DomainError) else '输入格式不正确，请检查后重试。')
    def home(self,u):
        a,f=self.s.balance(u)
        rows=[[(p['name']+' · '+str(p['price'])+'积分','buy:'+str(p['id']))] for p in self.s.products()]
        rows += [[('我的订单','mine'),('充值','topup')]]
        if u==self.s.admin_id: rows.append([('管理后台','admin')])
        self.send(u,f'商品列表\n可用 {a} 积分，冻结 {f} 积分\n选择商品后直接发送资料。',rows)
    def admin_home(self,u):
        self.s.admin(u)
        self.send(u,'管理后台\n手动积分：/adjust 客户ID 增减积分 原因\n交付核实：/resolve 回单ID sent 消息ID 证据\n恢复已核实订单：/resume 订单ID 证据\n确认未送达：/resolve 回单ID retry 0 证据\n通知核实：/event 事件ID sent 消息ID 或 retry 0\n仅在核实 Telegram 记录后操作。',[[('商品管理','catalog'),('待处理订单','pending')],[('新增商品','newproduct'),('异常列表','exceptions')],[('返回客户首页','home')]])
    def callback(self,u,data):
        bits=data.split(':'); action=bits[0]; args=bits[1:]
        admin_actions={'admin','catalog','newproduct','manage','editproduct','saveproduct','move','toggle','pending','order','batch','receipts','delreceipt','finish','refund','refundyes','adjustyes','exceptions'}
        if action in admin_actions: self.s.admin(u)
        state=self.s.session(u)
        if action=='home': self.s.set_session(u,{}); self.home(u)
        elif action=='admin': self.s.set_session(u,{}); self.admin_home(u)
        elif action=='buy':
            p=self.s.product(int(args[0])); d=self.s.new_draft(u,p['id']); self.s.set_session(u,{'mode':'draft','draft':d,'field':0})
            self.send(u,f"{p['name']} · {p['price']} 积分\n余额 {self.s.balance(u)[0]}\n{p['description']}\n预计出单：{p['eta']}\n纯文字单字段一行一单；图片/文件及多字段逐笔。")
            self.draft_view(u,d)
        elif action=='field':
            d=args[0]; self.s.draft(u,d); self.s.set_session(u,{'mode':'draft','draft':d,'field':int(args[1])}); self.draft_view(u,d)
        elif action=='deldraft':
            d=args[0]; self.s.set_field(u,d,int(args[1]),{},delete=int(args[2])); self.draft_view(u,d)
        elif action=='abandon': self.s.abandon(u,args[0]); self.s.set_session(u,{}); self.home(u)
        elif action=='confirm':
            try: b=self.s.confirm(u,args[0],expected_version=int(args[1]))
            except DomainError:
                if self.s.draft(u,args[0])['status']=='rejected': self.s.set_session(u,{})
                raise
            self.s.set_session(u,{}); self.send(u,'报单成功，积分已冻结。正式订单不可自行取消。\n批次 '+b)
        elif action=='mine':
            for o in self.s.rows('SELECT * FROM orders WHERE user_id=? ORDER BY rowid DESC LIMIT 30',(u,)):
                self.send(u,f"订单 {o['id']}\n状态 {o['status']} · {o['price']} 积分")
        elif action=='catalog':
            self.send(u,'全部商品',[[((p['name']+'（上架）' if p['active'] else p['name']+'（下架）'),'manage:'+str(p['id']))] for p in self.s.products(True)]+[[('新增','newproduct')]])
        elif action=='manage':
            p=self.s.product(int(args[0])); self.send(u,self.product_text(p),[[('修改','editproduct:'+args[0]),('上架/下架','toggle:'+args[0])],[('上移','move:'+args[0]+':-1'),('下移','move:'+args[0]+':1')]])
        elif action=='toggle':
            p=self.s.product(int(args[0])); p['active']=1-p['active']; self.s.save_product(u,p); self.callback(u,'catalog')
        elif action=='move': self.s.move_product(u,int(args[0]),int(args[1])); self.callback(u,'catalog')
        elif action in ('newproduct','editproduct'):
            p=self.s.product(int(args[0])) if args else {}
            self.s.set_session(u,{'mode':'product','step':0,'product':p}); self.send(u,'逐项填写商品（修改时发送「保留」沿用当前值）。\n商品名称：')
        elif action=='saveproduct':
            if state.get('mode')!='product_preview': raise DomainError('商品预览已失效')
            self.s.save_product(u,state['product']); self.s.set_session(u,{}); self.callback(u,'catalog')
        elif action=='pending':
            for b in self.s.rows("SELECT DISTINCT batch_id FROM orders WHERE status IN ('pending','delivering','uncertain')"):
                self.send(u,'批次 '+b['batch_id'],[[('查看子订单','batch:'+b['batch_id'])]])
        elif action=='batch':
            self.send(u,'批次 '+args[0],[[('订单 '+o['id']+' '+o['status'],'order:'+o['id'])] for o in self.s.orders(args[0])])
        elif action=='order':
            o=self.s.order(args[0]); p=json.loads(o['snapshot'])
            result=self.send(u,f"订单 {o['id']}\n客户 {o['user_id']}\n{p['name']} {o['price']}积分 · {o['status']}\n回复本条消息暂存回单。",[[('查看暂存回单','receipts:'+o['id']),('完成订单','finish:'+o['id'])],[('空单退分','refund:'+o['id']+':empty'),('管理员取消','refund:'+o['id']+':cancel')]])
            self.s.link_reply(result['message_id'],o['id'])
            for index,items in json.loads(o['submission']).items():
                self.send(u,'客户字段：'+p['fields'][int(index)]['name'])
                for item in items: self.send_payload(u,item)
        elif action=='receipts':
            for r in self.s.receipts(args[0]):
                self.send_payload(u,json.loads(r['payload']))
                self.send(u,f"回单 {r['id']} · {r['state']}",[[('删除','delreceipt:'+args[0]+':'+str(r['id']))]])
        elif action=='delreceipt': self.s.delete_receipt(u,args[0],int(args[1])); self.send(u,'回单已删除，可回复订单详情重新添加。')
        elif action=='finish': self.s.begin_delivery(u,args[0]); self.send(u,'开始交付；全部消息明确发送成功后结算。')
        elif action=='refund':
            self.s.set_session(u,{'mode':'refund_reason','order':args[0],'kind':args[1]})
            self.send(u,'填写退款原因，发送「跳过」可留空。下一步仍须确认。')
        elif action=='refundyes':
            if state.get('mode')!='refund_confirm' or state.get('key')!=args[0]: raise DomainError('确认已失效')
            self.s.refund(u,state['order'],state['kind'],state['reason']); self.s.set_session(u,{}); self.send(u,'退款已记录。')
        elif action=='adjustyes':
            if state.get('mode')!='adjust_confirm' or state.get('key')!=args[0]: raise DomainError('确认已失效')
            self.s.adjust(u,state['user'],state['amount'],state['reason'],state['key']); self.s.set_session(u,{}); self.send(u,'积分调整已记账。')
        elif action=='exceptions':
            self.send(u,'异常订单：\n'+'\n'.join(o['id'] for o in self.s.rows("SELECT id FROM orders WHERE status='uncertain'")))
            for r in self.s.rows("SELECT id,order_id FROM receipts WHERE state='sending'"): self.send(u,f"待核实回单 {r['id']} / {r['order_id']}")
            for item in self.s.rows("SELECT id FROM inbox WHERE state='uncertain'"):
                self.send(u,f"中断的入站更新 {item['id']}，请核实订单/流水后让发送者重新操作。")
            for e in self.s.events():
                if e['state']=='uncertain': self.send(u,f"待核实通知事件 {e['id']} / {e['kind']}")
        elif action=='topup':
            if not self.payments: raise DomainError('OKPay 充值暂未开放，请联系管理员。')
            self.s.set_session(u,{'mode':'topup'}); self.send(u,'输入充值整数 USDT，最低 1 USDT（1 USDT = 1 积分）。创建结果待核实时请先检查原订单。')
            self.payment_history(u,0)
        elif action=='payhistory':
            if not self.payments: raise DomainError('支付服务未开放')
            self.payment_history(u,int(args[0]))
        elif action=='checkpay':
            if not self.payments: raise DomainError('支付服务未开放')
            self.send(u,self.payments.check(u,args[0]))
        else: raise DomainError('按钮已失效，请发送 /start')
    def product_text(self,p):
        return f"{p.get('name','')}\n价格 {p.get('price','')} 积分\n{p.get('description','')}\n预计 {p.get('eta','')}\n字段："+'、'.join(f["name"]+':'+f["kind"] for f in p.get('fields',[]))
    def draft_view(self,u,d):
        r=self.s.draft(u,d); p=self.s.product(r['product_id']); state=self.s.session(u); index=state.get('field',0)
        text=f"草稿（未冻结积分）\n当前填写：{p['fields'][index]['name']} / {p['fields'][index]['kind']}\n文字重发覆盖；图片/文件继续发送追加。\n"
        rows=[]
        for i,f in enumerate(p['fields']):
            items=r['data'].get(str(i),[]); rows.append([(f['name'],'field:'+d+':'+str(i))])
            for j,item in enumerate(items):
                text+=f"{f['name']}[{j+1}]：{item.get('text',item.get('caption','附件'))}\n"
                if item['kind']!='text': rows.append([('删除 '+f['name']+str(j+1),f'deldraft:{d}:{i}:{j}')])
        try:
            count=len(self.s.submissions(r,p)); text+=f'共 {count} 笔，费用 {count*p["price"]} 积分'
            rows.append([('确认报单','confirm:'+d+':'+str(r['edit_version']))])
        except DomainError: text+='请继续补齐所有字段。'
        rows.append([('放弃草稿','abandon:'+d),('返回首页','home')]); self.send(u,text,rows)
    def message(self,u,m):
        text=m.get('text',''); state=self.s.session(u)
        if text in ('/start','/products'): self.s.set_session(u,{}); self.home(u); return
        if text=='/admin': self.s.set_session(u,{}); self.admin_home(u); return
        if text=='/balance': self.send(u,'可用/冻结：'+str(self.s.balance(u))); return
        if text.startswith('/paymentlink '):
            self.s.admin(u)
            if not self.payments: raise DomainError('支付服务未开放')
            _,pid,platform,url,reason=text.split(' ',4)
            self.payments.attach_link(u,pid,platform,url,reason)
            self.send(u,'原充值订单链接已恢复；未加积分，客户仍须付款并查单。'); return
        if text.startswith('/adjust '):
            self.s.admin(u); _,customer,amount,reason=text.split(' ',3)
            n=int(amount)
            if not n or not reason.strip() or int(customer)<=0: raise DomainError('客户ID、非零整数积分与原因必填')
            key=uid(); self.s.set_session(u,{'mode':'adjust_confirm','user':int(customer),'amount':n,'reason':reason,'key':key})
            self.send(u,f'二次确认：客户 {customer} 调整 {n} 积分\n原因：{reason}',[[('确认调整','adjustyes:'+key),('取消','admin')]]); return
        if text.startswith('/resume '):
            self.s.admin(u); _,order,evidence=text.split(' ',2)
            self.s.resume(u,order,evidence); self.send(u,'订单已恢复交付队列。'); return
        if text.startswith('/resolve '):
            self.s.admin(u); _,rid,choice,mid,evidence=text.split(' ',4)
            if choice not in ('sent','retry'): raise DomainError('选择 sent 或 retry')
            self.s.resolve(u,int(rid),choice=='sent',int(mid),evidence); self.send(u,'已保存核实结论。'); return
        if text.startswith('/event '):
            self.s.admin(u); _,eid,choice,mid=text.split()
            if choice not in ('sent','retry'): raise DomainError('选择 sent 或 retry')
            with self.s.tx() as c:
                e=c.execute("SELECT * FROM events WHERE id=? AND state='uncertain'",(int(eid),)).fetchone()
                if not e: raise DomainError('事件不在待核实状态')
                if choice=='sent' and int(mid)<1: raise DomainError('请提供已发送消息ID')
                c.execute('UPDATE events SET state=?,message_id=? WHERE id=?',('sent' if choice=='sent' else 'pending',int(mid),int(eid)))
                self.s.audit(c,u,'event_resolve',eid,choice+':'+mid)
            self.send(u,'通知核实结果已记录。'); return
        if u==self.s.admin_id and m.get('reply_to_message'):
            links=self.s.rows('SELECT order_id FROM reply_links WHERE message_id=?',(m['reply_to_message']['message_id'],))
            if not links: raise DomainError('请回复指定订单详情消息，普通聊天不会作为回单。')
            self.s.add_receipt(u,links[0]['order_id'],payload(m)); self.send(u,'已暂存，客户尚不可见。'); return
        mode=state.get('mode')
        if mode=='draft': self.s.set_field(u,state['draft'],state['field'],payload(m)); self.draft_view(u,state['draft'])
        elif mode=='product':
            self.s.admin(u); keys=['name','price','description','eta','fields']; prompts=['商品名称：','整数积分价格：','商品说明/提交说明：','预计出单时间：','自定义字段，每行 名称:text 或 名称:photo 或 名称:document（1至10项）：']; i=state['step']; key=keys[i]
            if text!='保留' or key not in state['product']:
                if key=='price': value=int(text)
                elif key=='fields':
                    value=[]
                    for line in text.splitlines():
                        name,kind=line.rsplit(':',1); value.append({'name':name.strip(),'kind':kind.strip()})
                    if not value or len(value)>10 or any(not f['name'] or f['kind'] not in ('text','photo','document') for f in value): raise DomainError('字段格式错误')
                else: value=text.strip()
                if not value or (key=='price' and value<1): raise DomainError('内容不能为空，价格必须为正整数')
                state['product'][key]=value
            i+=1; state['step']=i
            if i==len(keys): state['mode']='product_preview'
            self.s.set_session(u,state)
            if i==len(keys): self.send(u,'预览：\n'+self.product_text(state['product']),[[('确认保存并上架','saveproduct'),('放弃','admin')]])
            else: self.send(u,prompts[i])
        elif mode=='refund_reason':
            self.s.admin(u); state.update(mode='refund_confirm',reason='' if text=='跳过' else text,key=uid()); self.s.set_session(u,state)
            self.send(u,f"二次确认：{state['order']} 全额退分\n原因：{state['reason'] or '未填写'}",[[('确认退款','refundyes:'+state['key']),('取消','admin')]])
        elif mode=='topup':
            if not self.payments: raise DomainError('支付未开放')
            order=self.payments.create(u,int(text)); self.s.set_session(u,{})
            self.payment_view(u,self.s.one('SELECT * FROM payments WHERE id=? AND user_id=?',(order['id'],u)))
        else: self.send(u,'请先选择商品；管理员请回复订单详情暂存回单。',[[('商品列表','home')]])
    def payment_history(self,u,page):
        for order in self.payments.recent(u,page): self.payment_view(u,order)
        buttons=[]
        if page>0: buttons.append(('上一页','payhistory:'+str(page-1)))
        if self.payments.recent(u,page+1): buttons.append(('下一页','payhistory:'+str(page+1)))
        if buttons: self.send(u,'充值历史 · 第 '+str(page+1)+' 页',[buttons])
    def payment_view(self,u,order):
        buttons=[]
        if order.get('url') and order['state']=='pending':
            buttons.append([{'text':'支付订单','url':order['url']}])
        if not order.get('url') and order['state']!='credited':
            self.send(u,'原订单创建结果待核实，请先检查支付状态。待付但缺少链接时，请联系管理员从商户后台核对恢复。')
        buttons.append([{'text':'检查支付状态','callback_data':'checkpay:'+order['id']}])
        self.t.call('sendMessage',chat_id=u,text=f"充值订单 {order['id']}\n{order['amount']} USDT = {order['amount']} 积分\n状态 {order['state']}（允许代付）",reply_markup={'inline_keyboard':buttons})
    def deliver(self):
        for o in self.s.rows("SELECT * FROM orders WHERE status='delivering'"):
            try:
                for r in self.s.receipts(o['id']):
                    if r['state']=='sent': continue
                    if r['state']!='staged': raise DomainError('投递状态不确定')
                    self.s.mark_sending(r['id'])
                    result=self.send_payload(o['user_id'],json.loads(r['payload']))
                    self.s.mark_sent(r['id'],result['message_id'])
                self.s.settle(o['id'])
            except Exception:
                # Even a failed HTTP response may follow a successful send: never auto-replay.
                self.s.uncertain(o['id'])
    def flush_events(self):
        for e in self.s.rows("SELECT * FROM events WHERE state='pending' ORDER BY id"):
            p=json.loads(e['payload']); rows=None
            if e['kind']=='new':
                orders=self.s.orders(p['batch']); first=orders[0]; product=json.loads(first['snapshot'])
                created=self.s.one('SELECT created_at FROM batches WHERE id=?',(p['batch'],))['created_at']
                text=f"新批次 {p['batch']}\n商品：{product['name']}\n客户 {first['user_id']} · {len(orders)} 笔 · 总冻结 {sum(o['price'] for o in orders)} 积分\n创建 {created} UTC · 待处理"
                if len(orders)==1: text+='\n订单 '+first['id']
                rows=[[('展开子订单','batch:'+p['batch'])]]
            elif e['kind']=='credited': text=f"充值已入账 {p['points']} 积分\n充值订单 {p['payment']}"
            elif e['kind']=='summary': text=f"批次 {p['batch']} 全部结束\n成功 {p['success']}，空单/退款 {p['refund']}，扣分 {p['charged']}，退分 {p['returned']}"
            else: text=f"订单 {p['order']} 已退 {p['points']} 积分\n{p['reason']}"
            with self.s.tx() as c:
                if not c.execute("UPDATE events SET state='sending' WHERE id=? AND state='pending'",(e['id'],)).rowcount: continue
            try:
                result=self.send(e['target'],text,rows)
                with self.s.tx() as c:
                    c.execute("UPDATE events SET state='sent',message_id=? WHERE id=?",(result['message_id'],e['id']))
                    if e['kind']=='new':
                        orders=self.s.orders(p['batch'])
                        if len(orders)==1: c.execute('INSERT OR IGNORE INTO reply_links VALUES(?,?)',(result['message_id'],orders[0]['id']))
            except Exception:
                with self.s.tx() as c: c.execute("UPDATE events SET state='uncertain' WHERE id=?",(e['id'],))
