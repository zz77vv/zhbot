"""Private-chat Telegram UI; domain authorization is also enforced in Store."""
import json
from .store import DomainError, InsufficientPoints, uid
from .telegram import TelegramRejected
from .payment_ui import PaymentUI
from .admin_ui import AdminUI


FIELD_TYPES = {'text':'文字','photo':'图片','document':'文件'}
ORDER_STATES = {
    'pending':'处理中', 'delivering':'发送回单中', 'bulk_delivering':'发送回单中', 'uncertain':'待核实',
    'completed':'已完成', 'empty':'已退款', 'cancel':'已退款',
}
BUTTON_STYLES = {
    **dict.fromkeys(('确认提交','确认发送','发送回单','确认保存','确认保存并上架','确认调整','去付款'), 'success'),
    **dict.fromkeys(('取消','取消报单','确认取消','管理员取消','空单退分','确认退款','删除','放弃'), 'danger'),
}
BUTTON_ICONS = {
    # Selected from https://t.me/addemoji/yingzihuiyuan9 after inspecting thumbnails.
    '充值': ('💵','4988068987716764567'),
    '充值积分': ('💵','4988068987716764567'),
    '去充值': ('💵','4988068987716764567'),
    '支付订单': ('💸','4988068987716764567'),
    '去付款': ('💸','4988068987716764567'),
    '我的订单': ('🔖','4985872253678781168'),
    '查看订单': ('🔍','4987889505328431871'),
    '检查到账': ('🔍','4987889505328431871'),
    '确认提交': ('✔️','4987968902093865732'),
    '确认发送': ('✔️','4987968902093865732'),
    '发送回单': ('✉️','4987876955433993117'),
    '批量回单': ('✉️','4987876955433993117'),
    '管理后台': ('⚙️','4987895501102777018'),
}
UI_ICONS = {'🛍':'4988146357257634651','💵':'4988068987716764567',
            '✔️':'4987968902093865732','✉️':'4987876955433993117'}
THEME_ALT='💎'


def ui_entities(text):
    entities=[]
    for emoji,emoji_id in UI_ICONS.items():
        start=0
        while (index:=text.find(emoji,start))!=-1:
            entities.append({'type':'custom_emoji','offset':len(text[:index].encode('utf-16-le'))//2,
                             'length':len(emoji.encode('utf-16-le'))//2,'custom_emoji_id':emoji_id})
            start=index+len(emoji)
    return sorted(entities,key=lambda entity:entity['offset'])


def themed_text(text,entities):
    # This pack uses the same two-unit fallback for every icon. Keep the real
    # fallback in the API text; restore the semantic source text on rejection.
    encoded=text.encode('utf-16-le')
    for entity in reversed(entities):
        if entity['type']=='custom_emoji' and entity['custom_emoji_id'] in UI_ICONS.values():
            start,end=entity['offset']*2,(entity['offset']+entity['length'])*2
            encoded=encoded[:start]+THEME_ALT.encode('utf-16-le')+encoded[end:]
    return encoded.decode('utf-16-le')


def keyboard(rows, custom_emoji=True):
    rendered=[]
    for row in rows:
        buttons=[]
        for item in row:
            if isinstance(item,dict):
                button=dict(item)
            else:
                label,data=item
                button={'text':label,'callback_data':data}
            label=button['text']
            is_product=button.get('callback_data','').startswith('buy:')
            button['style']='success' if is_product else BUTTON_STYLES.get(label,'primary')
            icon=BUTTON_ICONS.get(label)
            if icon:
                if custom_emoji:
                    button['icon_custom_emoji_id']=icon[1]
                else:
                    button['text']=f'{icon[0]} {label}'
            buttons.append(button)
        rendered.append(buttons)
    return {'inline_keyboard':rendered}

def payload(m):
    if m.get('photo'): return {'kind':'photo','file_id':m['photo'][-1]['file_id'],'caption':m.get('caption','')}
    if m.get('document'): return {'kind':'document','file_id':m['document']['file_id'],'caption':m.get('caption','')}
    if m.get('text'): return {'kind':'text','text':m['text']}
    raise DomainError('仅支持文字、图片和文件')

def order_subject(order):
    for items in json.loads(order['submission']).values():
        for item in items:
            if item['kind']=='text':
                return ' '.join(item['text'].split())[:36]
    return '附件资料'

class App(PaymentUI,AdminUI):
    def __init__(self,store,telegram,payments=None):
        self.s,self.t,self.payments=store,telegram,payments
        self.custom_emoji=True
    def call_with_buttons(self,method,rows,**data):
        if 'entities' not in data:
            entities=ui_entities(data.get('text',''))
            if entities: data['entities']=entities
        if not self.custom_emoji and 'entities' in data:
            data['entities']=[e for e in data['entities'] if e['type']!='custom_emoji']
        markup=keyboard(rows,self.custom_emoji)
        wire_data=dict(data)
        if self.custom_emoji and data.get('entities') and 'text' in data:
            wire_data['text']=themed_text(data['text'],data['entities'])
        try:
            return self.t.call(method,reply_markup=markup,**wire_data)
        except TelegramRejected as error:
            if method=='editMessageText' and error.reason=='not_modified':
                return {'message_id':data['message_id']}
            has_custom=any(
                'icon_custom_emoji_id' in button for row in markup['inline_keyboard'] for button in row
            ) or any(e['type']=='custom_emoji' for e in data.get('entities',[]))
            if error.code!=400 or not has_custom:
                raise
            if 'entities' in data:
                data['entities']=[e for e in data['entities'] if e['type']!='custom_emoji']
            try:
                result=self.t.call(method,reply_markup=keyboard(rows,custom_emoji=False),**data)
            except TelegramRejected as fallback_error:
                if method!='editMessageText' or fallback_error.reason!='not_modified': raise
                result={'message_id':data['message_id']}
            if error.reason=='custom_emoji': self.custom_emoji=False
            return result
    def send(self,user,text,rows=None,*,theme=True):
        chunks=[str(text)[i:i+2000] for i in range(0,len(str(text)),2000)] or ['（空）']
        for index,chunk in enumerate(chunks):
            args={'chat_id':user,'text':chunk,'link_preview_options':{'is_disabled':True}}
            if (rows and index==len(chunks)-1) or (theme and ui_entities(chunk)):
                if not theme: args['entities']=[]
                result=self.call_with_buttons('sendMessage',rows if index==len(chunks)-1 and rows else [],**args)
            else:
                result=self.t.call('sendMessage',**args)
        return result
    def send_payload(self,user,p):
        if p['kind']=='text': return self.t.call('sendMessage',chat_id=user,text=p['text'],link_preview_options={'is_disabled':True})
        method='sendPhoto' if p['kind']=='photo' else 'sendDocument'
        return self.t.call(method,chat_id=user,**{p['kind']:p['file_id']},caption=p.get('caption','')[:1024])
    def card(self,u,text,rows=None,message_id=None):
        if message_id is not None and len(text)<=2000:
            try:
                return self.call_with_buttons('editMessageText',rows or [],chat_id=u,message_id=message_id,
                                              text=text,link_preview_options={'is_disabled':True})
            except TelegramRejected as error:
                if error.code!=400: raise
        return self.send(u,text,rows)
    def handle(self,update):
        q=update.get('callback_query'); m=q.get('message',{}) if q else update.get('message',{})
        actor=(q or m).get('from',{}).get('id'); chat=m.get('chat',{})
        if not actor or chat.get('type')!='private' or chat.get('id')!=actor: return
        try:
            if q:
                self.t.call('answerCallbackQuery',callback_query_id=q['id'])
                self.callback(actor,q.get('data',''),message_id=m.get('message_id'))
            else: self.message(actor,m)
        except (DomainError,ValueError,KeyError,IndexError) as e:
            self.send(actor,str(e) if isinstance(e,DomainError) else '输入格式不正确，请检查后重试。')
    def home(self,u,page=0,message_id=None):
        a,f=self.s.balance(u)
        products=self.s.products()
        pages=max(1,(len(products)+7)//8)
        page=max(0,min(page,pages-1))
        rows=[]
        for p in products[page*8:(page+1)*8]:
            button=(p['name'],'buy:'+str(p['id']))
            if len(button[0])<=18 and rows and len(rows[-1])==1 and len(rows[-1][0][0])<=18:
                rows[-1].append(button)
            else:
                rows.append([button])
        if pages>1:
            nav=[]
            if page: nav.append(('上一页',f'home:{page-1}'))
            if page+1<pages: nav.append(('下一页',f'home:{page+1}'))
            rows.append(nav)
        rows.append([('充值积分','topup')])
        if u==self.s.admin_id: rows.append([('管理后台','admin')])
        lines=['🛍 综合服务','',f'💵 可用积分：{a}']
        if f: lines.append(f'冻结积分：{f}')
        lines += ['', '请选择需要的服务' if products else '暂无上架服务']
        if pages>1: lines.append(f'第 {page+1} / {pages} 页')
        text='\n'.join(lines)
        entities=ui_entities(text)
        if message_id is not None:
            try:
                self.call_with_buttons('editMessageText',rows,chat_id=u,message_id=message_id,text=text,entities=entities)
                return
            except TelegramRejected as error:
                if error.code!=400: raise
        self.call_with_buttons('sendMessage',rows,chat_id=u,text=text,entities=entities,
                               link_preview_options={'is_disabled':True})
    def admin_home(self,u):
        self.s.admin(u)
        pending=self.s.one("SELECT COUNT(*) AS n FROM orders WHERE status='pending'")['n']
        self.send(u,f'管理后台 · 待处理 {pending} 单',[
            [('待处理订单','pending')],
            [('商品管理','catalog'),('积分管理','adjust')],
            [('异常处理','exceptions'),('返回客户首页','home')],
        ])
    def callback(self,u,data,message_id=None):
        bits=data.split(':'); action=bits[0]; args=bits[1:]
        admin_actions={'admin','catalog','newproduct','manage','editproduct','saveproduct','pfieldkind','paddfield','pfieldsdone','pkeepfields','move','toggle','pending','order','batch','bulk','bulkyes','receipts','delreceipt','finish','sendreceipt','refund','refundyes','adjust','adjustyes','exceptions','opshelp'}
        if action in admin_actions: self.s.admin(u)
        state=self.s.session(u)
        if action in ('catalog','manage','pending','batch','order','receipts'):
            self.s.set_session(u,{})
        if action=='home':
            self.s.set_session(u,{})
            self.home(u,int(args[0]) if args else 0,message_id=message_id if args else None)
        elif action=='admin': self.s.set_session(u,{}); self.admin_home(u)
        elif action=='adjust':
            self.s.set_session(u,{'mode':'adjust_user'})
            self.send(u,'请输入客户的 Telegram 数字 ID。',[[('取消','admin')]])
        elif action=='opshelp':
            self.send(u,'核实后再操作\n\n已送达回单\n/resolve 回单ID sent 消息ID 核实证据\n\n确认未送达回单\n/resolve 回单ID retry 0 核实证据\n\n恢复已核实订单\n/resume 订单ID 核实证据\n\n通知核实\n/event 事件ID sent 消息ID\n/event 事件ID retry 0',[[('返回异常处理','exceptions')]])
        elif action=='buy':
            p=self.s.product(int(args[0])); d=self.s.new_draft(u,p['id']); self.s.set_session(u,{'mode':'draft','draft':d,'field':0})
            self.draft_view(u,d,show_product=True)
        elif action=='field':
            d=args[0]
            if self.s.draft(u,d)['status']!='editing': raise DomainError('草稿已结束，请重新选商品。')
            prior=state if state.get('draft')==d else {}
            self.s.set_session(u,dict(prior,mode='draft',draft=d,field=int(args[1])))
            self.draft_view(u,d,prompt_only=True)
        elif action=='resume': self.resume_draft(u,args[0])
        elif action=='draftnext':
            draft=self.s.draft(u,args[0])
            if draft['status']!='editing' or draft['edit_version']!=int(args[1]):
                raise DomainError('资料已变化，请使用最新提示。')
            p=self.s.product(draft['product_id'])
            missing=next((i for i in range(len(p['fields'])) if not draft['data'].get(str(i))),None)
            current=state if state.get('draft')==args[0] else {}
            self.s.set_session(u,dict(current,mode='draft',draft=args[0],field=missing if missing is not None else 0))
            self.draft_view(u,args[0])
        elif action=='deldraft':
            if len(args)!=4: raise DomainError('请重新打开草稿，使用最新的删除按钮。')
            d=args[0]; self.s.set_field(u,d,int(args[1]),{},delete=int(args[2]),expected_version=int(args[3])); self.draft_view(u,d)
        elif action=='abandon':
            d=args[0]; draft=self.s.draft(u,d)
            if draft['status']!='editing': raise DomainError('这次报单已结束。')
            if any(draft['data'].values()):
                key=uid()
                self.s.set_session(u,dict(state,mode='draft_cancel',draft=d,key=key,cancel_version=draft['edit_version']))
                self.send(u,'取消本次报单？已填写的资料将不再提交。',[
                    [('确认取消','abandonyes:'+d+':'+key),('继续填写','resume:'+d)],
                ])
            else:
                self.s.abandon(u,d); self.s.set_session(u,{}); self.home(u)
        elif action=='abandonyes':
            if state.get('mode')!='draft_cancel' or state.get('draft')!=args[0] or state.get('key')!=args[1]:
                raise DomainError('取消确认已失效，请使用最新提示。')
            draft=self.s.draft(u,args[0])
            if draft['edit_version']!=state['cancel_version']: raise DomainError('资料已修改，请重新确认取消。')
            self.s.abandon(u,args[0]); self.s.set_session(u,{}); self.home(u)
        elif action=='confirm':
            draft=self.s.draft(u,args[0])
            if draft['status']=='submitted':
                self.send(u,'已提交，请等待回单。')
                return
            try: b=self.s.confirm(u,args[0],expected_version=int(args[1]))
            except InsufficientPoints as error:
                self.s.set_session(u,dict(state,mode='draft',draft=args[0],field=state.get('field',0),resume_draft=args[0]))
                result=self.card(u,str(error),[[('去充值','topup'),('返回预览','resume:'+args[0])],
                                             [('取消报单','abandon:'+args[0])]],message_id=message_id)
                current=self.s.session(u); current['card_id']=result['message_id']; self.s.set_session(u,current)
                return
            self.s.set_session(u,{})
            orders=self.s.orders(b)
            frozen=sum(order['price'] for order in orders)
            available=self.s.balance(u)[0]
            receipt=f'✔️ 已提交，请等待回单\n已冻结 {frozen} 积分 · 可用 {available} 积分'
            rows=[]
            if message_id is not None:
                try:
                    self.call_with_buttons('editMessageText',rows,chat_id=u,message_id=message_id,text=receipt)
                    return
                except TelegramRejected as error:
                    if error.code!=400: raise
            self.send(u,receipt,rows)
        elif action=='mybatch':
            page=int(args[1]) if len(args)>1 else 0
            if page<0: raise DomainError('页码无效。')
            orders=self.s.rows('SELECT id,price,status,snapshot,submission FROM orders WHERE batch_id=? AND user_id=? ORDER BY rowid',(args[0],u))
            if not orders: raise DomainError('找不到这笔订单。')
            product=json.loads(orders[0]['snapshot'])['name']
            visible=orders[page*10:(page+1)*10]
            if not visible: raise DomainError('这一页没有订单。')
            rows=[[(f"{page*10+i}. {order_subject(o)} · {ORDER_STATES[o['status']]}",'mineorder:'+o['id'])] for i,o in enumerate(visible,1)]
            nav=[]
            if page: nav.append(('上一页',f'mybatch:{args[0]}:{page-1}'))
            if len(orders)>(page+1)*10: nav.append(('下一页',f'mybatch:{args[0]}:{page+1}'))
            if nav: rows.append(nav)
            rows.append([('返回首页','home')])
            self.send(u,f'{product} · 共 {len(orders)} 单 · 第 {page+1} 页',rows)
        elif action=='mine':
            page=int(args[0]) if args else 0
            if page<0 or page>100000: raise DomainError('页码无效。')
            orders=self.s.rows('SELECT * FROM orders WHERE user_id=? ORDER BY rowid DESC LIMIT 11 OFFSET ?',(u,page*10))
            rows=[[(f"{json.loads(o['snapshot'])['name'][:16]} · {order_subject(o)} · {ORDER_STATES[o['status']]}",'mineorder:'+o['id'])] for o in orders[:10]]
            nav=[]
            if page: nav.append(('上一页','mine:'+str(page-1)))
            if len(orders)>10: nav.append(('下一页','mine:'+str(page+1)))
            if nav: rows.append(nav)
            rows.append([('返回首页','home')])
            self.send(u,f'我的订单 · 第 {page+1} 页' if orders else '暂无订单。',rows)
        elif action in ('mineorder','myreceipts'):
            o=self.s.one('SELECT * FROM orders WHERE id=? AND user_id=?',(args[0],u))
            if action=='myreceipts':
                receipts=self.s.rows("SELECT payload FROM receipts WHERE order_id=? AND state='sent'",(o['id'],))
                if o['status']!='completed' or not receipts: raise DomainError('回单正在处理中，请稍后再看。')
                for receipt in receipts: self.send_payload(u,json.loads(receipt['payload']))
                self.send(u,'以上为本单回单。',[[('返回订单','mineorder:'+o['id'])]])
                return
            p=json.loads(o['snapshot'])
            lines=[p['name'],f"{ORDER_STATES[o['status']]} · {o['price']} 积分"]
            attachments=[]
            for index,items in json.loads(o['submission']).items():
                for item in items:
                    if item['kind']=='text': lines.append(f"{p['fields'][int(index)]['name']}：{item['text']}")
                    else: attachments.append(item)
            if o['reason']: lines.append('处理说明：'+o['reason'])
            rows=[]
            if o['status']=='completed': rows.append([('查看回单','myreceipts:'+o['id'])])
            rows.append([('返回首页','home')])
            self.send(u,'\n'.join(lines),rows)
            for item in attachments: self.send_payload(u,item)
        elif action=='catalog':
            self.send(u,'全部商品',[[((p['name']+'（上架）' if p['active'] else p['name']+'（下架）'),'manage:'+str(p['id']))] for p in self.s.products(True)]+[[('新增','newproduct')]])
        elif action=='manage':
            p=self.s.product(int(args[0])); self.send(u,self.product_text(p),[[('修改','editproduct:'+args[0]),('上架/下架','toggle:'+args[0])],[('上移','move:'+args[0]+':-1'),('下移','move:'+args[0]+':1')]])
        elif action=='toggle':
            p=self.s.product(int(args[0])); p['active']=1-p['active']; self.s.save_product(u,p); self.callback(u,'catalog')
        elif action=='move': self.s.move_product(u,int(args[0]),int(args[1])); self.callback(u,'catalog')
        elif action in ('newproduct','editproduct'):
            p=self.s.product(int(args[0])) if args else {}
            self.s.set_session(u,{'mode':'product','step':0,'product':p})
            self.send(u,'请输入商品名称。'+(' 修改时可发送「保留」。' if p else ''),[[('取消','admin')]])
        elif action=='saveproduct':
            if state.get('mode')!='product_preview' or not args or state.get('key')!=args[0]:
                raise DomainError('商品预览已失效，请使用最新的确认按钮。')
            self.s.save_product(u,state['product']); self.s.set_session(u,{}); self.callback(u,'catalog')
        elif action in ('pfieldkind','paddfield','pfieldsdone','pkeepfields'):
            if not args or state.get('key')!=args[0]:
                raise DomainError('这个按钮已失效，请使用最新提示。')
            if action=='pfieldkind':
                if state.get('mode')!='product_field_type' or len(args)!=2 or args[1] not in FIELD_TYPES:
                    raise DomainError('请先填写字段名称。')
                fields=state['product']['fields']
                fields.append({'name':state.pop('field_name'),'kind':args[1]})
                state.update(mode='product_fields',key=uid())
                self.s.set_session(u,state)
                self.product_fields_view(u,state)
            elif action=='pkeepfields':
                if state.get('mode')!='product_fields' or not state.get('original_fields'):
                    raise DomainError('没有原字段可保留。')
                state['product']['fields']=state['original_fields']
                self.product_preview(u,state)
            elif action=='pfieldsdone':
                if state.get('mode')!='product_fields' or not state['product']['fields']:
                    raise DomainError('请先添加一个字段。')
                self.product_preview(u,state)
            else:
                if state.get('mode')!='product_fields' or len(state['product']['fields'])>=10:
                    raise DomainError('最多添加 10 个字段。')
                state.update(mode='product_field_name',key=uid())
                self.s.set_session(u,state)
                self.send(u,'请输入下一个字段名称。',[[('取消','admin')]])
        elif action=='pending':
            self.pending_view(u,int(args[0]) if args else 0,message_id=message_id if args else None)
        elif action=='batch':
            self.batch_view(u,args[0],int(args[1]) if len(args)>1 else 0,message_id=message_id if len(args)>1 else None)
        elif action=='bulk':
            batch=args[0]
            orders=self.s.bulk_rows(u,batch)
            self.s.set_session(u,{'mode':'bulk','batch':batch})
            example=next(iter(orders))
            accounts='\n'.join(orders)
            self.send(u,f'请粘贴 {len(orders)} 行回单，每行「账号 结果」。\n空单写「账号 空」。\n\n例如\n{example} 查询结果\n\n待回单账号\n{accounts}',[[('取消','admin')]])
        elif action=='bulkyes':
            if state.get('mode')!='bulk_preview' or state.get('key')!=args[0]:
                raise DomainError('这张回单预览已失效。')
            self.s.queue_bulk(u,state['batch'],state['results'])
            self.s.set_session(u,{})
            self.send(u,'批量回单已排队。')
        elif action=='order':
            o=self.s.order(args[0]); p=json.loads(o['snapshot'])
            details=[f"{p['name']} · {ORDER_STATES[o['status']]} · {o['price']} 积分"]
            attachments=[]
            for index,items in json.loads(o['submission']).items():
                label=p['fields'][int(index)]['name']
                for item in items:
                    if item['kind']=='text': details.append(f"{label}：{item['text']}")
                    else:
                        details.append(f"{label}：{FIELD_TYPES[item['kind']]}")
                        attachments.append(item)
            details.append(f"客户 {o['user_id']} · 编号 {o['id']}")
            rows=[[('查看回单','receipts:'+o['id'])]]
            if o['status']=='pending':
                details.append('回复这条消息暂存回单。')
                rows[0].append(('发送回单','finish:'+o['id']))
                rows.append([('空单退分','refund:'+o['id']+':empty'),('管理员取消','refund:'+o['id']+':cancel')])
            elif o['status']=='uncertain':
                rows.append([('管理员取消','refund:'+o['id']+':cancel')])
            result=self.send(u,'\n'.join(details),rows)
            if o['status']=='pending': self.s.link_reply(result['message_id'],o['id'])
            for item in attachments: self.send_payload(u,item)
        elif action=='receipts':
            for r in self.s.receipts(args[0]):
                self.send_payload(u,json.loads(r['payload']))
                self.send(u,f"回单 {r['id']} · {r['state']}",[[('删除','delreceipt:'+args[0]+':'+str(r['id']))]])
        elif action=='delreceipt': self.s.delete_receipt(u,args[0],int(args[1])); self.send(u,'回单已删除，可回复订单详情重新添加。')
        elif action=='finish': self.receipt_preview(u,args[0])
        elif action=='sendreceipt': self.receipt_confirm(u,args[0])
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
            self.s.set_session(u,{})
            self.send(u,'异常处理\n请先核实客户聊天记录和账务，再执行恢复。',[[('恢复操作说明','opshelp'),('返回后台','admin')]])
            self.send(u,'异常订单：\n'+'\n'.join(o['id'] for o in self.s.rows("SELECT id FROM orders WHERE status='uncertain'")))
            for r in self.s.rows("SELECT id,order_id FROM receipts WHERE state='sending'"): self.send(u,f"待核实回单 {r['id']} / {r['order_id']}")
            for item in self.s.rows("SELECT id FROM inbox WHERE state='uncertain'"):
                self.send(u,f"中断的入站更新 {item['id']}，请核实订单/流水后让发送者重新操作。")
            for e in self.s.events():
                if e['state']=='uncertain':
                    if e['kind']=='bulk_receipt':
                        self.send(u,f"批量回单待核实 · 事件 {e['id']}\n核实已送达：/event {e['id']} sent 消息ID\n确认未送达：/event {e['id']} retry 0 或 /event {e['id']} cancel 0")
                    else:
                        self.send(u,f"待核实通知事件 {e['id']} / {e['kind']}")
        elif action=='topup':
            self.topup_view(u)
        elif action=='checkpay':
            self.payment_check(u,args[0],message_id=message_id)
        else: raise DomainError('按钮已失效，请发送 /start')
    def batch_card(self,batch,*,new=False):
        orders=self.s.orders(batch)
        first=orders[0]
        product=json.loads(first['snapshot'])['name']
        pending=sum(o['status']=='pending' for o in orders)
        title=('新批次 · ' if new else '')+f'{product} · {len(orders)} 单'
        details=[title,f"客户 {first['user_id']} · {pending} 单待处理"]
        details.extend(f"{i}. {order_subject(o)} · {ORDER_STATES[o['status']]}" for i,o in enumerate(orders[:8],1))
        if len(orders)>8: details.append(f'另有 {len(orders)-8} 单')
        buttons=[('逐单处理','batch:'+batch)] if len(orders)>1 else [('查看订单','order:'+first['id'])]
        try:
            self.s.bulk_rows(self.s.admin_id,batch)
        except DomainError:
            pass
        else:
            buttons.insert(0,('批量回单','bulk:'+batch))
        return '\n'.join(details),[buttons]
    def product_text(self,p):
        return f"{p.get('name','')}\n{p.get('price','')} 积分 / 单 · 预计 {p.get('eta','')}\n{p.get('description','')}\n提交资料："+'、'.join(f["name"]+'（'+FIELD_TYPES[f["kind"]]+'）' for f in p.get('fields',[]))
    def product_preview(self,u,state):
        state.update(mode='product_preview',key=uid())
        self.s.set_session(u,state)
        self.send(u,'商品预览\n'+self.product_text(state['product']),[
            [('确认保存','saveproduct:'+state['key']),('取消','admin')],
        ])
    def product_fields_view(self,u,state):
        fields=state['product']['fields']
        lines=[f'{i}. {f["name"]} · {FIELD_TYPES[f["kind"]]}' for i,f in enumerate(fields,1)]
        rows=[]
        if fields:
            if len(fields)<10: rows.append([('继续添加','paddfield:'+state['key'])])
            rows.append([('填写完成','pfieldsdone:'+state['key'])])
            text='客户需要提交的资料\n'+'\n'.join(lines)
        else:
            text='客户需要提交什么？请输入字段名称，例如「抖音号」。'
            if state.get('original_fields'):
                rows.append([('保留原字段','pkeepfields:'+state['key'])])
        rows.append([('取消','admin')])
        self.send(u,text,rows)
    def adjust_preview(self,u,customer,amount,reason):
        if customer<=0 or not amount or not reason.strip():
            raise DomainError('客户 ID、非零积分和原因都需要填写。')
        key=uid()
        self.s.set_session(u,{'mode':'adjust_confirm','user':customer,'amount':amount,'reason':reason,'key':key})
        self.send(u,f'确认调整\n客户 {customer}\n积分 {amount:+d}\n原因 {reason}',[[('确认调整','adjustyes:'+key),('取消','admin')]])
    def resume_draft(self,u,d):
        draft=self.s.draft(u,d)
        if draft['status']!='editing': raise DomainError('这次报单已结束，请重新选择商品。')
        p=self.s.product(draft['product_id'])
        if not p['active'] or p['revision']!=draft['revision']:
            raise DomainError('商品内容已调整，请重新选择商品后核对资料。')
        current=self.s.session(u)
        prior=current if current.get('draft')==d else {}
        missing=next((i for i in range(len(p['fields'])) if not draft['data'].get(str(i))),0)
        self.s.set_session(u,dict(prior,mode='draft',draft=d,field=missing))
        self.draft_view(u,d)

    def draft_view(self,u,d,show_product=False,prompt_only=False):
        r=self.s.draft(u,d); p=self.s.product(r['product_id']); state=self.s.session(u)
        if r['status']!='editing': raise DomainError('这次报单已结束，请重新选择商品。')
        index=state.get('field',0)
        if not 0<=index<len(p['fields']): raise DomainError('请使用当前资料填写按钮。')
        field=p['fields'][index]; batch_text=len(p['fields'])==1 and field['kind']=='text'
        missing=[i for i in range(len(p['fields'])) if not r['data'].get(str(i))]
        details=[f"🛍 {p['name']}"]
        rows=[]
        if show_product:
            details.append(f"{p['price']} 积分 / 单 · 预计 {p['eta']}")
            if p['description']: details.append(p['description'])
        if missing or prompt_only or show_product:
            verb='发送' if field['kind']=='text' else '上传'
            kind='' if field['kind']=='text' else FIELD_TYPES[field['kind']]
            prefix=f'第 {index+1}/{len(p["fields"])} 项：' if len(p['fields'])>1 else ''
            details.append(f'{prefix}请{verb}「{field["name"]}」{kind}。')
            if batch_text: details.append('批量报单每行一个，最多 100 条。')
            current=r['data'].get(str(index),[])
            if current and field['kind']!='text':
                details.append(f'已收到 {len(current)} 份，可继续上传。')
                if missing: rows.append([('下一项',f'draftnext:{d}:{r["edit_version"]}')])
        if not missing and not prompt_only and not show_product:
            for i,f in enumerate(p['fields']):
                items=r['data'].get(str(i),[])
                if f['kind']=='text': details.extend(f"{f['name']}：{item['text']}" for item in items)
                else: details.append(f"{f['name']}：已收到 {len(items)} 份{FIELD_TYPES[f['kind']]}")
            try:
                count=len(self.s.submissions(r,p))
                details.append(f'共 {count} 单，需 {count*p["price"]} 积分。')
                rows.insert(0,[('确认提交',f'confirm:{d}:{r["edit_version"]}'),('修改','field:'+d+':0')])
            except DomainError as error: details.append(str(error))
        if len(p['fields'])>1:
            fields=[(('已填 · ' if r['data'].get(str(i)) else '待填 · ')+f['name'],'field:'+d+':'+str(i)) for i,f in enumerate(p['fields'])]
            rows.extend([fields[i:i+2] for i in range(0,len(fields),2)])
        for i,f in enumerate(p['fields']):
            for j,item in enumerate(r['data'].get(str(i),[])):
                if item['kind']!='text': rows.append([('删除 '+f['name']+str(j+1),f'deldraft:{d}:{i}:{j}:{r["edit_version"]}')])
        rows.append([('取消报单','abandon:'+d)])
        text='\n\n'.join(details)
        old_id=state.get('card_id') if not state.get('card_long') else None
        result=self.card(u,text,rows,message_id=old_id)
        state.update(mode='draft',draft=d,card_id=result['message_id'],card_long=len(text)>2000)
        self.s.set_session(u,state)

    def draft_input(self,u,m,state):
        d=state['draft']; r=self.s.draft(u,d); p=self.s.product(r['product_id']); index=state['field']
        if not 0<=index<len(p['fields']): raise DomainError('请使用当前资料填写按钮。')
        field=p['fields'][index]
        try: item=payload(m)
        except DomainError:
            item={}
        if item.get('kind')!=field['kind']:
            kind=FIELD_TYPES[field['kind']]
            verb='发送' if field['kind']=='text' else '上传'
            raise DomainError(f'请{verb}「{field["name"]}」{kind}。')
        self.s.set_field(u,d,index,item)
        if field['kind']=='text':
            data=self.s.draft(u,d)['data']
            state['field']=next((i for i in range(len(p['fields'])) if not data.get(str(i))),index)
        self.s.set_session(u,state)
        self.draft_view(u,d)
    def message(self,u,m):
        text=m.get('text',''); state=self.s.session(u)
        if text in ('/start','/products'): self.s.set_session(u,{}); self.home(u); return
        if text=='/admin': self.s.set_session(u,{}); self.admin_home(u); return
        if text=='/balance':
            available,frozen=self.s.balance(u)
            self.send(u,f'可用积分：{available}'+(f'\n冻结积分：{frozen}' if frozen else '')); return
        if text.startswith('/paymentclose '):
            self.s.admin(u)
            if not self.payments: raise DomainError('支付服务未开放')
            _,pid,disposition,reason=text.split(' ',3)
            self.payments.close_unpaid(u,pid,disposition,reason)
            self.send(u,'已关闭原充值单。请通过充值菜单重新建单。'); return
        if text.startswith('/paymentlink '):
            self.s.admin(u)
            if not self.payments: raise DomainError('支付服务未开放')
            _,pid,platform,url,reason=text.split(' ',4)
            self.payments.attach_link(u,pid,platform,url,reason)
            self.send(u,'原充值订单链接已恢复；未加积分，客户仍须付款并查单。'); return
        if text.startswith('/adjust '):
            self.s.admin(u); _,customer,amount,reason=text.split(' ',3)
            self.adjust_preview(u,int(customer),int(amount),reason); return
        if text.startswith('/resume '):
            self.s.admin(u); _,order,evidence=text.split(' ',2)
            self.s.resume(u,order,evidence); self.send(u,'订单已恢复交付队列。'); return
        if text.startswith('/resolve '):
            self.s.admin(u); _,rid,choice,mid,evidence=text.split(' ',4)
            if choice not in ('sent','retry'): raise DomainError('选择 sent 或 retry')
            self.s.resolve(u,int(rid),choice=='sent',int(mid),evidence); self.send(u,'已保存核实结论。'); return
        if text.startswith('/event '):
            self.s.admin(u); _,eid,choice,mid=text.split()
            if choice not in ('sent','retry','cancel'): raise DomainError('选择 sent、retry 或 cancel')
            message_id=int(mid)
            if choice=='sent' and message_id<1: raise DomainError('请提供已发送消息ID')
            if choice!='sent' and message_id!=0: raise DomainError('请将未送达的消息ID填为 0')
            with self.s.tx() as c:
                e=c.execute("SELECT * FROM events WHERE id=? AND state='uncertain'",(int(eid),)).fetchone()
                if not e: raise DomainError('事件不在待核实状态')
                if choice=='cancel' and e['kind']!='bulk_receipt': raise DomainError('仅批量回单可用 cancel')
                if e['kind']=='bulk_receipt' and choice in ('sent','cancel'):
                    self.s.finish_bulk(c,e,message_id,cancel=choice=='cancel')
                event_state={'sent':'sent','retry':'pending','cancel':'cancelled'}[choice]
                c.execute('UPDATE events SET state=?,message_id=?,retry_at=NULL WHERE id=?',(event_state,message_id,int(eid)))
                self.s.audit(c,u,'event_resolve',eid,choice+':'+mid)
            self.send(u,'通知核实结果已记录。'); return
        if state.get('mode') in ('bulk','bulk_preview'):
            self.s.admin(u)
            batch=state['batch']
            self.s.set_session(u,{'mode':'bulk','batch':batch})
            orders=self.s.bulk_rows(u,batch)
            results={}
            for i,line in enumerate(text.splitlines(),1):
                if not line.strip(): continue
                parts=line.split(maxsplit=1)
                if len(parts)!=2: raise DomainError(f'第 {i} 行缺少结果，请填写「账号 结果」。')
                account,result=parts
                if account not in orders: raise DomainError(f'第 {i} 行的账号不在这批订单里。')
                if account in results: raise DomainError(f'第 {i} 行的账号重复了。')
                results[account]=result.strip()
            missing=orders.keys()-results.keys()
            if missing:
                names='、'.join(account for account in orders if account in missing)
                raise DomainError(f'还有 {len(missing)} 单未填写：{names}')
            empty=sum(result=='空' for result in results.values())
            key=uid()
            self.s.set_session(u,{'mode':'bulk_preview','batch':batch,'results':results,'key':key})
            preview='\n'.join(f'{account} {results[account]}' for account in orders)
            self.send(u,f'回单预览 · 成功 {len(results)-empty} 单 · 空单 {empty} 单\n\n{preview}',
                      [[('确认发送','bulkyes:'+key),('取消','admin')]],theme=False)
            return
        guided={'draft','draft_cancel','product','product_fields','product_field_name','product_field_type',
                'adjust_user','adjust_amount','adjust_reason','refund_reason','topup'}
        if u==self.s.admin_id and m.get('reply_to_message') and state.get('mode') not in guided:
            links=self.s.rows('SELECT order_id FROM reply_links WHERE message_id=?',(m['reply_to_message']['message_id'],))
            if not links: raise DomainError('请回复指定订单详情消息，普通聊天不会作为回单。')
            self.s.add_receipt(u,links[0]['order_id'],payload(m)); self.send(u,'已暂存，客户尚不可见。'); return
        mode=state.get('mode')
        if mode=='draft': self.draft_input(u,m,state)
        elif mode=='draft_cancel': self.send(u,'请先确认取消，或点击「继续填写」。',[[('继续填写','resume:'+state['draft'])]])
        elif mode in ('adjust_user','adjust_amount','adjust_reason'):
            self.s.admin(u)
            if mode=='adjust_reason':
                self.adjust_preview(u,state['user'],state['amount'],text)
                return
            try: value=int(text)
            except ValueError: raise DomainError('请输入整数。') from None
            if mode=='adjust_user':
                if value<=0: raise DomainError('请输入大于 0 的客户数字 ID。')
                state.update(mode='adjust_amount',user=value)
                prompt='输入调整积分。加分填 5，扣分填 -5。'
            else:
                if not value: raise DomainError('调整积分不能为 0。')
                state.update(mode='adjust_reason',amount=value)
                prompt='请输入调整原因。'
            self.s.set_session(u,state)
            self.send(u,prompt,[[('取消','admin')]])
        elif mode=='product':
            self.s.admin(u)
            keys=['name','price','description','eta','fields']
            prompts=['请输入商品名称。','请输入每单价格，例如 7。','请输入商品说明。','请输入预计出单时间，例如 2 小时。']
            i=state['step']; key=keys[i]
            if key=='fields':
                state.update(mode='product_fields',original_fields=state['product'].get('fields',[]),key=uid())
                state['product']['fields']=[]
                self.s.set_session(u,state)
                self.message(u,m)
                return
            if text!='保留' or key not in state['product']:
                if key=='price':
                    try: value=int(text)
                    except ValueError: raise DomainError('价格请填大于 0 的整数，例如 7。') from None
                else: value=text.strip()
                if not value or (key=='price' and value<1): raise DomainError('内容不能为空，价格必须为正整数')
                state['product'][key]=value
            i+=1; state['step']=i
            if i==4:
                state.update(mode='product_fields',original_fields=state['product'].get('fields',[]),key=uid())
                state['product']['fields']=[]
                self.s.set_session(u,state)
                self.product_fields_view(u,state)
            else:
                self.s.set_session(u,state)
                self.send(u,prompts[i],[[('取消','admin')]])
        elif mode in ('product_fields','product_field_name'):
            self.s.admin(u)
            if text=='保留' and state.get('original_fields') and not state['product']['fields']:
                state['product']['fields']=state['original_fields']
                self.product_preview(u,state)
                return
            if mode=='product_fields' and state['product']['fields']:
                raise DomainError('请点「继续添加」或「填写完成」。')
            if ':' in text:
                fields=[]
                for line in text.splitlines():
                    try: name,kind=line.rsplit(':',1)
                    except ValueError: raise DomainError('请输入字段名称，再选择文字、图片或文件。') from None
                    fields.append({'name':name.strip(),'kind':kind.strip()})
                if not 1<=len(fields)<=10 or any(not f['name'] or f['kind'] not in FIELD_TYPES for f in fields):
                    raise DomainError('请填写 1 至 10 项有效字段。')
                if mode=='product_field_name': raise DomainError('请输入一个字段名称，再点选类型。')
                state['product']['fields']=fields
                self.product_preview(u,state)
                return
            if not text.strip() or '\n' in text: raise DomainError('请输入一个字段名称，例如「抖音号」。')
            state.update(mode='product_field_type',field_name=text.strip(),key=uid())
            self.s.set_session(u,state)
            self.send(u,'「'+text.strip()+'」用什么方式提交？',[
                [(label,'pfieldkind:'+state['key']+':'+kind) for kind,label in FIELD_TYPES.items()],
                [('取消','admin')],
            ])
        elif mode=='product_field_type':
            self.s.admin(u)
            self.send(u,'请点选文字、图片或文件。',[
                [(label,'pfieldkind:'+state['key']+':'+kind) for kind,label in FIELD_TYPES.items()],
                [('取消','admin')],
            ])
        elif mode=='refund_reason':
            self.s.admin(u); state.update(mode='refund_confirm',reason='' if text=='跳过' else text,key=uid()); self.s.set_session(u,state)
            self.send(u,f"二次确认：{state['order']} 全额退分\n原因：{state['reason'] or '未填写'}",[[('确认退款','refundyes:'+state['key']),('取消','admin')]])
        elif mode=='topup':
            self.topup_input(u,text)
        else:
            text='请先选择需要的服务。' if u!=self.s.admin_id else '请选择服务，或回复订单详情添加回单。'
            self.send(u,text,[[('商品列表','home')]])
    def deliver(self):
        for o in self.s.rows("SELECT * FROM orders WHERE status='delivering' AND (retry_at IS NULL OR retry_at<=CURRENT_TIMESTAMP)"):
            try:
                for r in self.s.receipts(o['id']):
                    if r['state']=='sent': continue
                    if r['state']!='staged': raise DomainError('投递状态不确定')
                    self.s.mark_sending(r['id'])
                    try:
                        result=self.send_payload(o['user_id'],json.loads(r['payload']))
                    except TelegramRejected as error:
                        self.s.reject_receipt(o['id'],r['id'],error.retry_after)
                        break
                    self.s.mark_sent(r['id'],result['message_id'])
                else:
                    self.s.settle(o['id'])
            except Exception:
                # Even a failed HTTP response may follow a successful send: never auto-replay.
                self.s.uncertain(o['id'])
    def flush_events(self):
        for e in self.s.rows("SELECT * FROM events WHERE state='pending' AND (retry_at IS NULL OR retry_at<=CURRENT_TIMESTAMP) ORDER BY id"):
            p=json.loads(e['payload']); rows=None
            if e['kind']=='new':
                text,rows=self.batch_card(p['batch'],new=True)
            elif e['kind']=='bulk_receipt':
                text=p['text']
                if text.startswith('📋 回单结果'):
                    text='✉️'+text[1:]
            elif e['kind']=='credited': text=''
            elif e['kind']=='summary':
                if p['success']==1 and not p['refund']:
                    text=f"✔️ 已完成 · 已扣 {p['charged']} 积分"
                else:
                    text=f"报单已结束\n完成 {p['success']} 单 · 退款 {p['refund']} 单\n已扣 {p['charged']} 积分 · 已退 {p['returned']} 积分"
            else:
                text=f"{order_subject(self.s.order(p['order']))} · 已退 {p['points']} 积分"
                if p['reason']: text+='\n'+p['reason']
            with self.s.tx() as c:
                if not c.execute("UPDATE events SET state='sending' WHERE id=? AND state='pending'",(e['id'],)).rowcount: continue
            try:
                if e['kind']=='credited':
                    result=self.payment_credit_notice(e,p)
                elif e['kind']=='bulk_receipt':
                    result=self.call_with_buttons('sendMessage',[],chat_id=e['target'],text=text,
                                                  entities=ui_entities(text.split('\n',1)[0]),
                                                  link_preview_options={'is_disabled':True})
                else:
                    result=self.send(e['target'],text,rows)
                with self.s.tx() as c:
                    if e['kind']=='bulk_receipt': self.s.finish_bulk(c,e,result['message_id'])
                    c.execute("UPDATE events SET state='sent',message_id=? WHERE id=?",(result['message_id'],e['id']))
                    if e['kind']=='new':
                        orders=self.s.orders(p['batch'])
                        if len(orders)==1: c.execute('INSERT OR IGNORE INTO reply_links VALUES(?,?)',(result['message_id'],orders[0]['id']))
            except TelegramRejected as error:
                with self.s.tx() as c:
                    if error.retry_after is not None and (len(text)<=2000 or e['kind']=='bulk_receipt'):
                        c.execute("UPDATE events SET state='pending',retry_at=datetime('now',?) WHERE id=?",(f'+{error.retry_after} seconds',e['id']))
                    else:
                        c.execute("UPDATE events SET state='uncertain' WHERE id=?",(e['id'],))
            except Exception:
                with self.s.tx() as c: c.execute("UPDATE events SET state='uncertain' WHERE id=?",(e['id'],))
