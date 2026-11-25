"""Paged administrator queues and snapshot-bound single-order receipt previews."""
import hashlib
import json
from .receipt_content import combined_receipt, original_submission

from .store import DomainError, product_tiers, uid


PAGE_SIZE = 8
ORDER_QUEUES = {'ready':'可处理','quote':'等补差价','verify':'待核实'}
ORDER_LABELS = {
    'pending': '处理中', 'delivering': '发送回单中', 'bulk_delivering': '发送回单中',
    'uncertain': '待核实', 'completed': '已完成', 'empty': '已退款', 'cancel': '已退款',
}


def subject(order):
    if order.get('data_cleared_at'):
        return '资料已清理'
    for items in json.loads(order['submission']).values():
        for item in items:
            if item['kind'] == 'text':
                return ' '.join(item['text'].split())[:36]
    return '附件资料'


def receipt_snapshot(receipts):
    return [{field: item[field] for field in ('id', 'payload', 'state')} for item in receipts]


def fingerprint(snapshot):
    encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()


class AdminUI:
    def retention_status(self, u):
        from .retention import Retention
        self.s.admin(u)
        status = Retention(self.s).status()
        labels = {'sessions':'临时会话', 'drafts':'草稿', 'orders':'订单资料',
                  'inbox':'已处理消息', 'inbox_archive':'旧消息记录', 'events':'成功通知正文'}
        states = {'not_initialized':'等待初始化', 'scheduled':'等待执行',
                  'running':'清理中', 'complete':'上次清理完成', 'failed':'失败，等待重试'}
        lines = ['数据库管理', '每 14 天执行；不备份。',
                 '状态：'+states.get(status['state'], status['state']),
                 f"数据库及 WAL：{status['database_bytes']/1024:.1f} KiB",
                 '上次完成（UTC）：'+str(status.get('last_success') or '尚未执行'),
                 '下次执行（UTC）：'+str(status.get('next_run') or '等待初始化')]
        for key, count in status.get('counts', {}).items():
            lines.append(labels.get(key,key)+'：'+str(count))
        if status.get('error'): lines.append('错误类型：'+status['error'])
        lines.append('资金流水、结算摘要、未完成及异常业务保留。')
        return self.card(u, '\n'.join(lines), [[('刷新','database'),('返回后台','admin')]])

    def pending_view(self, u, page=0, message_id=None, queue='ready'):
        self.s.admin(u)
        if queue not in ORDER_QUEUES:
            raise DomainError('订单筛选已失效。')
        groups = self.s.rows("""
            WITH bulk_uncertain AS (
                SELECT items.key AS id FROM events e, json_each(e.payload,'$.items') items
                WHERE e.kind='bulk_receipt' AND e.state='uncertain'
            ), active AS (
                SELECT o.rowid, o.batch_id, o.price, CASE
                    WHEN o.status='pending' AND q.id IS NOT NULL THEN 'quote'
                    WHEN o.status='pending' THEN 'ready'
                    ELSE 'verify' END AS queue
                FROM orders o LEFT JOIN order_quotes q ON q.order_id=o.id AND q.state='pending'
                WHERE o.status IN ('pending','uncertain')
                  OR (o.status='bulk_delivering' AND o.id IN (SELECT id FROM bulk_uncertain))
            ), grouped AS (
                SELECT batch_id, queue, COUNT(*) AS count, COUNT(DISTINCT price) AS prices, MIN(rowid) AS first_row
                FROM active GROUP BY batch_id,queue
            )
            SELECT g.*,o.snapshot,o.price FROM grouped g JOIN orders o ON o.rowid=g.first_row
            ORDER BY g.first_row
        """)
        batches = [group for group in groups if group['queue']==queue]
        pages = max(1, (len(batches) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(int(page), pages - 1))
        counts = {key:sum(group['count'] for group in groups if group['queue']==key) for key in ORDER_QUEUES}
        rows = [[(f'{label} {counts[key]}',f'pending:0:{key}') for key,label in ORDER_QUEUES.items()]]
        for batch in batches[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
            product=json.loads(batch['snapshot'])
            tier=next((name for name,price in product_tiers(product).items() if price==batch['price']),None)
            if batch['prices']>1: tier='多档位'
            label=' · '.join(filter(None,(product['name'][:18],tier,f"{batch['count']} 单")))
            rows.append([(label, f"batch:{batch['batch_id']}:0:{queue}:{page}")])
        navigation = []
        suffix='' if queue=='ready' else ':'+queue
        if page:
            navigation.append(('上一页', f'pending:{page - 1}{suffix}'))
        if page + 1 < pages:
            navigation.append(('下一页', f'pending:{page + 1}{suffix}'))
        if navigation:
            rows.append(navigation)
        rows.append([('返回后台', 'admin')])
        text=f'订单管理 · {ORDER_QUEUES[queue]}\n{counts[queue]} 单 · {len(batches)} 批'
        if pages > 1:
            text += f'\n第 {page + 1} / {pages} 页'
        return self.card(u, text, rows, message_id=message_id)

    def batch_view(self, u, batch, page=0, message_id=None, queue=None, list_page=0):
        self.s.admin(u)
        orders = self.s.orders(batch)
        if not orders:
            raise DomainError('找不到这批订单。')
        first = orders[0]
        name = json.loads(first['snapshot'])['name']
        quote_ids={r['order_id'] for r in self.s.rows(
            "SELECT q.order_id FROM order_quotes q JOIN orders o ON o.id=q.order_id WHERE o.batch_id=? AND q.state='pending'",(batch,))}
        bulk_ids={r['id'] for r in self.s.rows("""
            SELECT items.key AS id FROM events e, json_each(e.payload,'$.items') items
            WHERE e.kind='bulk_receipt' AND e.state='uncertain' AND json_extract(e.payload,'$.batch')=?
        """,(batch,))}
        if queue and queue not in ORDER_QUEUES:
            raise DomainError('订单筛选已失效。')

        def order_queue(order):
            if order['status']=='pending':
                return 'quote' if order['id'] in quote_ids else 'ready'
            if order['status']=='uncertain' or (order['status']=='bulk_delivering' and order['id'] in bulk_ids):
                return 'verify'
            return None

        visible=[o for o in orders if queue is None or order_queue(o)==queue]
        pages = max(1, (len(visible) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(int(page), pages - 1))
        rows=[]
        for index,order in enumerate(visible[page * PAGE_SIZE:(page + 1) * PAGE_SIZE],page * PAGE_SIZE + 1):
            state=order_queue(order)
            label=ORDER_QUEUES[state] if state in ('quote','verify') else ORDER_LABELS.get(order['status'],'待核实')
            rows.append([(f"{index}. {subject(order)} · {label}",'order:'+order['id'])])
        navigation = []
        suffix=f':{queue}:{list_page}' if queue else ''
        if page:
            navigation.append(('上一页', f'batch:{batch}:{page - 1}{suffix}'))
        if page + 1 < pages:
            navigation.append(('下一页', f'batch:{batch}:{page + 1}{suffix}'))
        if navigation:
            rows.append(navigation)
        try:
            self.s.bulk_rows(u, batch)
        except DomainError:
            pass
        else:
            rows.append([('批量回单', 'bulk:' + batch)])
        back=f'pending:{list_page}:{queue}' if queue else 'pending'
        rows.append([('返回订单管理',back),('返回后台','admin')])
        pending = sum(order['status']=='pending' and order['id'] not in quote_ids for order in orders)
        text = f"{name} · 共 {len(orders)} 单\n客户 {first['user_id']} · {pending} 单待处理"
        if queue:
            text+=f'\n{ORDER_QUEUES[queue]} · {len(visible)} 单'
        if pages > 1:
            text += f'\n第 {page + 1} / {pages} 页'
        return self.card(u, text, rows, message_id=message_id)

    def verify_order(self,u,order_id):
        self.s.admin(u)
        order=self.s.order(order_id)
        events=self.s.uncertain_bulk_events(order_id)
        rows=[]
        lines=['核实回单 · '+subject(order)]
        if events:
            lines.append('这笔订单所在的批量回单分段送达状态不明。')
            for event in events:
                rows.append([(f"查看待核实分段 {event['id']}",f"vpart:{order_id}:bulk:{event['id']}")])
        elif order['status']=='uncertain':
            parts=self.s.receipts(order_id,include_context=True)
            states={'sent':'已确认送达','sending':'送达不明','staged':'未发送'}
            for index,part in enumerate(parts,1):
                kind='原报单' if part['context'] else '回单结果'
                lines.append(f"第 {index} 段 · {kind} · {states.get(part['state'],'待核实')}")
                if part['state']=='sending':
                    rows.append([(f'查看第 {index} 段',f"vpart:{order_id}:part:{part['id']}")])
            if not rows:
                rows.append([('恢复剩余发送',f'vstart:{order_id}:resume:0:retry')])
        else:
            raise DomainError('订单已离开待核实状态。')
        rows.append([('返回订单','order:'+order_id),('待核实列表','pending:0:verify')])
        self.send(u,'\n'.join(lines),rows)

    def verify_part(self,u,oid,kind,ident,*,show=False):
        self.s.admin(u)
        order=self.s.order(oid)
        if kind=='part' and order['status']=='uncertain':
            row=next((r for r in self.s.receipts(oid,include_context=True) if r['id']==ident and r['state']=='sending'),None)
            if row:
                if show: self.send_payload(u,json.loads(row['delivery_payload'] or row['payload']))
                return
        elif kind=='bulk':
            row=next((r for r in self.s.uncertain_bulk_events(oid) if r['id']==ident),None)
            if row:
                if show: self.send(u,json.loads(row['payload'])['text'])
                return
        elif kind=='resume' and order['status']=='uncertain' and not any(
                r['state']=='sending' for r in self.s.receipts(oid,include_context=True)):
            return
        raise DomainError('该段已离开待核实状态，请刷新订单。')

    def receipt_preview(self, u, order_id):
        self.s.admin(u)
        self.s.set_session(u, {})
        order = self.s.order(order_id)
        if order['status'] != 'pending':
            raise DomainError('订单状态已变化，请返回详情后重新预览。')
        receipts = self.s.receipts(order_id)
        if not receipts:
            return self.card(u, '尚未暂存回单。请回复订单详情添加回单，再点击发送回单。', [
                [('继续编辑', 'order:' + order_id), ('取消', 'admin')],
            ])
        if any(receipt['state'] != 'staged' for receipt in receipts):
            raise DomainError('回单状态已变化，请核实后重新预览。')
        snapshot = receipt_snapshot(receipts)
        name = json.loads(order['snapshot'])['name']
        self.send(u, f"✉️ 回单预览\n{name} · {subject(order)}\n客户 {order['user_id']}\n以下内容将按顺序发送给客户。")
        context=list(original_submission(order))
        results=[json.loads(receipt['payload']) for receipt in receipts]
        combined=combined_receipt(context,results)
        for item in ([combined] if combined else context+results):
            if item['kind'] == 'text':
                self.send(u, item['text'], theme=False)
            else:
                self.send_payload(u, item)
        key = uid()
        self.s.set_session(u, {
            'mode': 'receipt_preview', 'key': key, 'order': order_id, 'actor': u,
            'receipts': snapshot, 'fingerprint': fingerprint(snapshot),
        })
        return self.card(u, f"共 {len(receipts)} 条回单\n全部发送成功后扣费 {order['price']} 积分。\n请确认回单内容与客户资料。", [
            [('确认发送', 'sendreceipt:' + key)],
            [('继续编辑', 'order:' + order_id), ('取消', 'admin')],
        ])

    def receipt_confirm(self, u, key):
        self.s.admin(u)
        state = self.s.session(u)
        if (state.get('mode') != 'receipt_preview' or state.get('key') != key
                or state.get('actor') != u):
            raise DomainError('这张回单预览已失效，请重新预览。')
        try:
            order = self.s.order(state['order'])
            snapshot = receipt_snapshot(self.s.receipts(order['id']))
            if (order['status'] != 'pending' or not snapshot
                    or any(receipt['state'] != 'staged' for receipt in snapshot)
                    or fingerprint(snapshot) != state['fingerprint']):
                raise DomainError('回单或订单状态已变化。')
            # Store repeats this comparison while holding its delivery transaction lock.
            self.s.begin_delivery(u, order['id'], expected_receipts=state['receipts'], include_submission=True)
        except DomainError as error:
            self.s.set_session(u, {})
            raise DomainError('回单或订单状态已变化，请重新预览后再发送。') from error
        self.s.set_session(u, {})
        return self.card(u, '正在发送回单。全部发送成功后扣费；发送结果待核实时保留冻结积分。', [
            [('查看订单', 'order:' + order['id']), ('返回后台', 'admin')],
        ])
