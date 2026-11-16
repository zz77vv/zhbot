"""Paged administrator queues and snapshot-bound single-order receipt previews."""
import hashlib
import json
from .receipt_content import original_submission

from .store import DomainError, uid


PAGE_SIZE = 8
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

    def pending_view(self, u, page=0, message_id=None):
        self.s.admin(u)
        batches = self.s.rows(
            "SELECT batch_id, COUNT(*) AS count, MIN(rowid) AS first_row "
            "FROM orders WHERE status IN ('pending','delivering','bulk_delivering','uncertain') "
            'GROUP BY batch_id ORDER BY first_row'
        )
        pages = max(1, (len(batches) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(int(page), pages - 1))
        rows = []
        for batch in batches[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
            first = self.s.orders(batch['batch_id'])[0]
            name = json.loads(first['snapshot'])['name'][:18]
            label = f"{name} · 客户 {first['user_id']} · {batch['count']} 单 · {subject(first)[:20]}"
            rows.append([(label, 'batch:' + batch['batch_id'])])
        navigation = []
        if page:
            navigation.append(('上一页', f'pending:{page - 1}'))
        if page + 1 < pages:
            navigation.append(('下一页', f'pending:{page + 1}'))
        if navigation:
            rows.append(navigation)
        rows.append([('返回后台', 'admin')])
        text = (f'待处理订单 · {len(batches)} 批\n请选择批次查看订单。'
                if batches else '当前没有待处理订单。')
        if pages > 1:
            text += f'\n第 {page + 1} / {pages} 页'
        return self.card(u, text, rows, message_id=message_id)

    def batch_view(self, u, batch, page=0, message_id=None):
        self.s.admin(u)
        orders = self.s.orders(batch)
        if not orders:
            raise DomainError('找不到这批订单。')
        pages = max(1, (len(orders) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(int(page), pages - 1))
        first = orders[0]
        name = json.loads(first['snapshot'])['name']
        rows = [[(f"{page * PAGE_SIZE + index}. {subject(order)} · {ORDER_LABELS.get(order['status'], '待核实')}",
                  'order:' + order['id'])]
                for index, order in enumerate(orders[page * PAGE_SIZE:(page + 1) * PAGE_SIZE], 1)]
        navigation = []
        if page:
            navigation.append(('上一页', f'batch:{batch}:{page - 1}'))
        if page + 1 < pages:
            navigation.append(('下一页', f'batch:{batch}:{page + 1}'))
        if navigation:
            rows.append(navigation)
        try:
            self.s.bulk_rows(u, batch)
        except DomainError:
            pass
        else:
            rows.append([('批量回单', 'bulk:' + batch)])
        rows.append([('返回待处理', 'pending'), ('返回后台', 'admin')])
        pending = sum(order['status'] == 'pending' for order in orders)
        text = f"{name} · 共 {len(orders)} 单\n客户 {first['user_id']} · {pending} 单待处理"
        if pages > 1:
            text += f'\n第 {page + 1} / {pages} 页'
        return self.card(u, text, rows, message_id=message_id)

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
        for item in original_submission(order):
            self.send_payload(u,item)
        for receipt in receipts:
            item = json.loads(receipt['payload'])
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
