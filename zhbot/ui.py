"""Persistent Chinese inline menus. Callback tokens carry no trusted user input."""
import json
import re
import secrets

from .core import MAX_POINTS, RuleError, bounded_text, parse_uint


STATUS = {'submitted': '待处理', 'delivering': '交付中', 'completed': '已完成',
          'failed': '失败/已终止', 'delivery_review': '交付待核实', 'unknown': '结果未知',
          'review': '待核实', 'closed_review': '关闭后待核实', 'expired': '本地已过期',
          'pending': '待处理', 'paid': '已入账', 'closed': '本地已关闭'}


def status_label(value):
    return STATUS.get(value, '待核实')


def button(label, command, data=None):
    return label, command, data or {}


class GuidedUI:
    PAGE = 8

    def __init__(self, app):
        self.app, self.s, self.db = app, app.s, app.db

    def reset(self, actor, flow='', data=None, admin=False):
        self.db.execute("""INSERT INTO ui_sessions(actor,generation,flow,data,admin_only) VALUES (?,1,?,?,?)
            ON CONFLICT(actor) DO UPDATE SET generation=generation+1,flow=excluded.flow,
            data=excluded.data,admin_only=excluded.admin_only""",
                        (actor, flow, json.dumps(data or {}), int(admin)))
        return self.db.one('SELECT * FROM ui_sessions WHERE actor=?', (actor,))

    def attach(self, actor, message, buttons, *, admin=False, flow='', data=None):
        session = self.reset(actor, flow, data, admin)
        rows = []
        for label, command, payload in buttons:
            token = secrets.token_hex(16)
            self.db.execute('INSERT INTO ui_actions(token,actor,generation,command,data,admin_only,expires_at,message_id) VALUES (?,?,?,?,?,?,?,?)',
                (token, actor, session['generation'], command, json.dumps(payload),
                 int(admin or command == 'admin'), self.s.clock() + 1800, message))
            rows.append([{'text': label, 'callback_data': 'u:' + token}])
        self.db.execute('UPDATE messages SET reply_markup=? WHERE id=?',
                        (json.dumps({'inline_keyboard': rows}, ensure_ascii=True), message))

    def show(self, actor, text, buttons=(), *, admin=False, flow='', data=None):
        if admin:
            self.s.admin(actor)
        batch = self.s.note(actor, text, admin_only=admin)
        message = self.db.one('SELECT id FROM messages WHERE batch_id=?', (batch,))['id']
        self.attach(actor, message, list(buttons), admin=admin, flow=flow, data=data)
        return batch

    def home(self, actor):
        self.s.user(actor)
        self.db.execute('DELETE FROM selections WHERE actor=?', (actor,))
        buttons = [button('开始报单', 'projects'), button('我的草稿', 'draft'),
                   button('我的订单', 'orders'), button('积分余额', 'balance'),
                   button('充值说明', 'payment'), button('使用帮助', 'help')]
        if actor in self.s.admins:
            buttons.append(button('管理后台', 'admin'))
        return self.show(actor, f'综合报单机器人\n你的 Telegram ID：{actor}\n请选择操作。发送 /menu 随时返回主菜单。', buttons)

    def admin_home(self, actor):
        self.s.admin(actor)
        self.db.execute('DELETE FROM selections WHERE actor=?', (actor,))
        return self.show(actor, '管理后台：订单、项目、积分与异常核实。',
            [button('订单管理', 'orders', {'admin': True}), button('项目管理', 'projects', {'admin': True}),
             button('积分管理', 'points'), button('待核实事项', 'review'), button('返回客户菜单', 'home')], admin=True)

    def prompt(self, actor, text, flow, data=None):
        return self.show(actor, text + '\n发送 /menu 或点击取消退出此步骤。',
                         [button('取消输入', 'admin')], admin=True, flow=flow, data=data)

    def projects(self, actor, page=0, admin=False):
        self.s.user(actor)
        if admin:
            self.s.admin(actor)
        page = parse_uint(page, minimum=0, maximum=1_000_000)
        rows = self.db.all('SELECT * FROM projects' + ('' if admin else ' WHERE active=1') +
                           ' ORDER BY id LIMIT 9 OFFSET ?', (page * self.PAGE,))
        buttons = [button((r['title'] + ' · ' + str(r['price']) + ' 积分')[:100],
                          'project' if admin else 'new', {'id': r['id'], 'revision': r['revision']})
                   for r in rows[:self.PAGE]]
        if admin:
            buttons.append(button('新增项目', 'project_new'))
        buttons += self.pages('projects', page, len(rows) > self.PAGE, {'admin': admin})
        buttons.append(button('返回管理菜单' if admin else '返回主菜单', 'admin' if admin else 'home'))
        return self.show(actor, '项目管理' if admin else '选择项目开始报单；确认前不冻结积分。' if rows else '暂无可用项目，请联系管理员。', buttons, admin=admin)

    @staticmethod
    def pages(command, page, more, data=None):
        buttons = []
        if page:
            buttons.append(button('上一页', command, dict(data or {}, page=page - 1)))
        if more:
            buttons.append(button('下一页', command, dict(data or {}, page=page + 1)))
        return buttons

    def project(self, actor, data):
        self.s.admin(actor)
        row = self.db.one('SELECT * FROM projects WHERE id=?', (parse_uint(data['id']),))
        if row is None:
            raise RuleError('项目不存在。')
        snapshot = {k: row[k] for k in ['id', 'revision', 'title', 'price', 'active']}
        return self.show(actor, f'项目 {row["id"]}：{row["title"]}\n{row["price"]} 积分 · {"上架" if row["active"] else "下架"}',
            [button('修改名称和价格', 'project_edit', snapshot),
             button('下架项目' if row['active'] else '上架项目', 'project_toggle', snapshot),
             button('返回项目管理', 'projects', {'admin': True})], admin=True)

    def _project_current(self, data):
        row = self.db.one('SELECT * FROM projects WHERE id=?', (data.get('id'),))
        if row is None or row['revision'] != data['revision']:
            raise RuleError('项目已经变更，请重新打开项目列表。')
        return row

    def editor(self, actor):
        selected = self.db.one('SELECT * FROM selections WHERE actor=?', (actor,))
        if selected and selected['kind'] == 'receipt':
            row = self.s._selected_receipt(actor)
            data = {'kind': 'receipt', 'id': row['order_id'], 'revision': row['revision']}
            text = f'正在编辑订单 {row["order_id"]} 的回执，共 {len(row["items"])} 项。\n继续发送文字、图片或文件；完整预览后才能交付。'
            admin = True
        else:
            row = self.s.draft(actor)
            data = {'kind': 'customer', 'id': row['id'], 'revision': row['revision']}
            text = f'客户草稿 {row["id"]} · {row["title"]} · {row["price"]} 积分，共 {len(row["items"])} 项。\n请发送文字、图片或文件；预览确认后才冻结积分。'
            admin = False
        buttons = [button('完整预览', 'preview', data), button('撤销末项', 'undo', data), button('清空内容', 'clear_ask', data)]
        if not admin:
            buttons.append(button('取消报单', 'cancel_ask', data))
        buttons.append(button('返回管理菜单' if admin else '返回主菜单', 'admin' if admin else 'home'))
        return self.show(actor, text, buttons, admin=admin)

    def _editing(self, actor, data):
        selected = self.db.one('SELECT * FROM selections WHERE actor=?', (actor,))
        if selected is None or (selected['kind'], selected['ref']) != (data['kind'], data['id']):
            raise RuleError('编辑目标已变化，请重新打开草稿或回执。')
        row = self.s._selected_receipt(actor) if data['kind'] == 'receipt' else self.s.draft(actor, data['id'])
        if row['revision'] != data['revision'] or (data['kind'] == 'customer' and row['state'] != 'editing'):
            raise RuleError('内容版本已变化，请重新打开并预览。')
        return row

    def decorate_preview(self, actor, batch, receipt=False):
        b = self.db.one('SELECT * FROM batches WHERE id=?', (batch,))
        last = self.db.one('SELECT id FROM messages WHERE batch_id=? ORDER BY seq DESC LIMIT 1', (batch,))
        data = {'kind': 'receipt' if receipt else 'customer', 'id': b['ref'], 'revision': b['revision']}
        if receipt:
            label, command = '确认交付回执', 'deliver'
        else:
            row = self.s.draft(actor, b['ref'])
            label, command = f'确认报单并冻结 {row["price"]} 积分', 'confirm'
        self.attach(actor, last['id'], [button(label, command, data), button('继续编辑', 'edit', data)], admin=receipt)

    def preview(self, actor, data):
        self._editing(actor, data)
        receipt = data['kind'] == 'receipt'
        batch = self.s.preview_receipt(actor, data['id']) if receipt else self.s.preview_customer(actor, data['id'])
        self.decorate_preview(actor, batch, receipt)

    def orders(self, actor, page=0, admin=False):
        self.s.user(actor)
        if admin:
            self.s.admin(actor)
        page = parse_uint(page, minimum=0, maximum=1_000_000)
        rows = self.db.all('SELECT * FROM orders' + ('' if admin else ' WHERE user_id=?') +
            ' ORDER BY id DESC LIMIT 9 OFFSET ?', ((page*self.PAGE,) if admin else (actor, page*self.PAGE)))
        buttons = [button(f'{r["id"]} · {r["title"][:35]} · {status_label(r["status"])}', 'order', {'id': r['id'], 'admin': admin}) for r in rows[:self.PAGE]]
        buttons += self.pages('orders', page, len(rows) > self.PAGE, {'admin': admin})
        buttons.append(button('返回管理菜单' if admin else '返回主菜单', 'admin' if admin else 'home'))
        self.show(actor, '订单管理' if admin else '我的订单' if rows else '暂无订单。', buttons, admin=admin)

    def order(self, actor, data, materials=False):
        admin = bool(data.get('admin'))
        if admin:
            self.s.admin(actor)
        row = self.s.order(actor, data['id'])
        if not admin and row['user_id'] != actor:
            raise RuleError('只能从客户菜单查看自己的订单。')
        payload = {'id': row['id'], 'admin': admin}
        buttons = [button('查看原始材料', 'materials', payload)]
        if admin:
            buttons.append(button('逐条交付检查', 'inspect', payload))
            if row['status'] == 'submitted':
                buttons += [button('编辑回执', 'receipt', payload), button('终止订单', 'fail_ask', payload)]
        buttons.append(button('返回订单列表', 'orders', {'admin': admin}))
        text = f'订单 {row["id"]} · {row["title"]}\n状态 {status_label(row["status"])} · {row["price"]} 积分'
        if admin:
            text += f'\n客户 Telegram ID：{row["user_id"]}'
        if materials:
            batch = self.s.batch(actor, actor, 'note', row['items'] + [{'kind': 'text', 'content': text}], admin_only=admin)
            last = self.db.one('SELECT id FROM messages WHERE batch_id=? ORDER BY seq DESC LIMIT 1', (batch,))
            self.attach(actor, last['id'], buttons, admin=admin)
        else:
            self.show(actor, text, buttons, admin=admin)

    def inspect(self, actor, data):
        self.s.admin(actor)
        row = self.s.order(actor, data['id'])
        page = parse_uint(data.get('page', 0), minimum=0, maximum=1_000_000)
        messages = self.db.all("""SELECT m.*,b.chat_id,b.status AS batch_status FROM messages m JOIN batches b ON b.id=m.batch_id
            WHERE b.kind='delivery' AND b.ref=? ORDER BY m.seq,m.id LIMIT 9 OFFSET ?""", (row['id'], page*self.PAGE))
        lines = [f'订单 {row["id"]}，状态 {status_label(row["status"])}；以下为实际交付队列，不代表客户已读。']
        buttons = []
        for m in messages[:self.PAGE]:
            lines.append(f'第 {m["seq"]+1} 项 · 记录 {m["id"]} · {m["kind"]}\n收件人 {m["chat_id"]} · 状态 {m["status"]} · Telegram 消息编号 {m["telegram_id"] or "未确认"}\n依据：{m["reason"][:120] or "无"}')
            if row['status'] == 'delivery_review' and m['status'] in {'unknown', 'failed'}:
                buttons.append(button(f'核实第 {m["seq"]+1} 项', 'resolve_choose', {'id': m['id'], 'order': row['id']}))
        if not messages:
            lines.append('尚未发起交付。未知结果不会自动重发或自动判定送达。')
        buttons += self.pages('inspect', page, len(messages) > self.PAGE, {'id': row['id']})
        buttons.append(button('返回订单', 'order', {'id': row['id'], 'admin': True}))
        self.show(actor, '\n\n'.join(lines), buttons, admin=True)

    def user_card(self, actor, user):
        self.s.admin(actor)
        row = self.s.user(user)
        self.show(actor, f'用户 {row["tg_id"]}\n可用 {row["available"]}，冻结 {row["frozen"]}。调整只影响可用积分。',
            [button('增加积分', 'adjust_begin', {'user': row['tg_id'], 'sign': 1}),
             button('扣减积分', 'adjust_begin', {'user': row['tg_id'], 'sign': -1}),
             button('查询其他用户', 'points'), button('返回管理菜单', 'admin')], admin=True)

    def review(self, actor, page=0):
        self.s.admin(actor)
        page = parse_uint(page, minimum=0, maximum=1_000_000)
        rows = self.db.all("""SELECT 'delivery' kind,CAST(m.id AS TEXT) ref,b.ref order_id,m.status FROM messages m
            JOIN batches b ON b.id=m.batch_id JOIN orders o ON o.id=b.ref
            WHERE b.kind='delivery' AND o.status='delivery_review' AND m.status IN ('unknown','failed')
            UNION ALL SELECT 'payment',p.id,NULL,p.status FROM payments p WHERE p.status IN ('unknown','review','closed_review','expired')
            OR EXISTS (SELECT 1 FROM payment_identity_sightings x WHERE x.payment_id=p.id AND x.quarantined=1)
            ORDER BY kind,ref LIMIT 9 OFFSET ?""", (page*self.PAGE,))
        buttons = []
        for r in rows[:self.PAGE]:
            if r['kind'] == 'delivery':
                buttons.append(button(f'订单 {r["order_id"]} · {status_label(r["status"])}', 'inspect', {'id': r['order_id']}))
            else:
                buttons.append(button(f'充值 {r["ref"][:12]} · {status_label(r["status"])}', 'payment_review', {'id': r['ref']}))
        buttons += self.pages('review', page, len(rows) > self.PAGE)
        buttons.append(button('返回管理菜单', 'admin'))
        self.show(actor, '待核实事项：先检查证据，不能凭未知结果重发或入账。' if rows else '暂无待核实事项。', buttons, admin=True)

    def callback(self, actor, query):
        data = query.get('data')
        if type(data) is not str or not re.fullmatch('u:[0-9a-f]{32}', data):
            raise RuleError('按钮无效，请发送 /menu 重新打开。')
        action = self.db.one('SELECT * FROM ui_actions WHERE token=?', (data[2:],))
        session = self.db.one('SELECT * FROM ui_sessions WHERE actor=?', (actor,))
        source = self.db.one('SELECT m.*,b.chat_id,b.status AS batch_status FROM messages m JOIN batches b ON b.id=m.batch_id WHERE m.id=?',
                             (action['message_id'],)) if action else None
        if (not action or not session or not source or action['actor'] != actor or action['used_at'] is not None
                or action['generation'] != session['generation'] or self.s.clock() >= action['expires_at']
                or source['status'] != 'sent' or source['batch_status'] != 'sent' or source['chat_id'] != actor
                or source['telegram_id'] != query['message']['message_id']):
            raise RuleError('按钮已使用、过期或不属于当前页面，请发送 /menu。')
        if action['admin_only']:
            self.s.admin(actor)
        self.s.user(actor)
        self.db.execute('UPDATE ui_actions SET used_at=? WHERE token=?', (self.s.clock(), action['token']))
        self.route(actor, action['command'], json.loads(action['data']), action['token'])

    def route(self, actor, command, data, token):
        if command == 'home': self.home(actor)
        elif command == 'admin': self.admin_home(actor)
        elif command == 'projects': self.projects(actor, data.get('page', 0), bool(data.get('admin')))
        elif command == 'orders': self.orders(actor, data.get('page', 0), bool(data.get('admin')))
        elif command == 'balance':
            available, frozen = self.s.balance(actor)
            self.show(actor, f'Telegram ID：{actor}\n可用 {available} 积分，冻结 {frozen} 积分。',
                      [button('积分记录', 'ledger'), button('返回主菜单', 'home')])
        elif command == 'ledger':
            rows = self.db.all('SELECT * FROM ledger WHERE user_id=? ORDER BY id DESC LIMIT 10', (actor,))
            self.show(actor, '最近 10 条积分记录：\n' + ('\n'.join(f'{r["id"]} · 可用 {r["available_delta"]:+d} / 冻结 {r["frozen_delta"]:+d} · 余额 {r["available_after"]}/{r["frozen_after"]}' for r in rows) or '暂无记录。'),
                      [button('积分余额', 'balance'), button('返回主菜单', 'home')])
        elif command == 'payment':
            self.show(actor, '真实在线充值尚未开放，不会创建收款链接。\n如需人工调整积分，请联系管理员并提供你的 Telegram ID：' + str(actor), [button('返回主菜单', 'home')])
        elif command == 'help': self.show(actor, '选择项目 → 发送文字/图片/文件 → 完整预览 → 确认冻结积分 → 等待人工回执。\n菜单操作不会加入材料；以 / 开头的材料请用 /text 内容。返回菜单会退出当前编辑目标，草稿和回执仍保存。', [button('返回主菜单', 'home')])
        elif command == 'new':
            self._project_current(data)
            self.s.new_draft(actor, data['id'])
            self.editor(actor)
        elif command == 'draft':
            if self.s.resume_customer(actor) is None:
                self.show(actor, '暂无客户草稿。', [button('开始报单', 'projects'), button('返回主菜单', 'home')])
            else: self.editor(actor)
        elif command in {'edit', 'preview', 'undo', 'clear_ask', 'cancel_ask', 'clear', 'cancel', 'confirm', 'deliver'}:
            self._editing(actor, data)
            if command == 'edit': self.editor(actor)
            elif command == 'preview': self.preview(actor, data)
            elif command == 'undo': self.s.undo_selected(actor); self.editor(actor)
            elif command in {'clear_ask', 'cancel_ask'}:
                self.show(actor, '确认清空当前内容？' if command == 'clear_ask' else '确认取消客户草稿？未确认草稿不会冻结积分。',
                    [button('确认清空' if command == 'clear_ask' else '确认取消报单', 'clear' if command == 'clear_ask' else 'cancel', data),
                     button('继续编辑', 'edit', data)], admin=data['kind'] == 'receipt')
            elif command == 'clear': self.s.clear_selected(actor); self.editor(actor)
            elif command == 'cancel': self.s.cancel_draft(actor); self.home(actor)
            elif command == 'confirm':
                order = self.s.confirm(actor, data['id'], data['revision'])
                self.order(actor, {'id': order})
            else:
                self.s.deliver(actor, data['id'], data['revision'])
                self.order(actor, {'id': data['id'], 'admin': True})
        elif command in {'order', 'materials'}: self.order(actor, data, materials=command == 'materials')
        elif command == 'receipt': self.s.open_receipt(actor, data['id']); self.editor(actor)
        elif command == 'inspect': self.inspect(actor, data)
        elif command == 'points': self.prompt(actor, '请输入已注册用户的 Telegram 数字 ID。', 'points_user')
        elif command == 'adjust_begin': self.prompt(actor, '请输入要增加的整数积分：' if data['sign'] == 1 else '请输入要扣减的整数积分（不含负号）：', 'points_amount', data)
        elif command == 'adjust_confirm':
            self.s.adjust_points(actor, data['user'], data['delta'], data['reason'], token, expected=data['expected'])
            self.user_card(actor, data['user'])
        elif command == 'project': self.project(actor, data)
        elif command in {'project_new', 'project_edit'}:
            if command == 'project_edit': self._project_current(data)
            self.prompt(actor, '请输入项目名称（最多 100 字符）：', 'project_title', data)
        elif command == 'project_toggle':
            row = self._project_current(data)
            self.s.save_project(actor, row['id'], row['title'], row['price'], not bool(row['active']))
            self.project(actor, data)
        elif command == 'project_save':
            if data.get('id'): self._project_current(data)
            project = self.s.save_project(actor, data.get('id'), data['title'], data['price'], bool(data.get('active', 1)))
            self.project(actor, {'id': project})
        elif command == 'review': self.review(actor, data.get('page', 0))
        elif command == 'fail_ask':
            self.s.admin(actor)
            if self.s.order(actor, data['id'])['status'] != 'submitted': raise RuleError('订单已不允许终止。')
            self.prompt(actor, '请输入终止订单的具体原因；确认后释放冻结积分。', 'fail_reason', data)
        elif command == 'fail_confirm':
            self.s.fail_order(actor, data['id'], data['reason']); self.order(actor, data)
        elif command == 'resolve_choose':
            self.s.admin(actor)
            self.show(actor, '先核对实际聊天记录与逐条交付检查。\n确认该项送达不会重发该项；确认整单失败将停止剩余发送并释放全部冻结。',
                [button('已核实该项送达', 'resolve_reason', dict(data, delivered=True)),
                 button('已核实整单交付失败', 'resolve_reason', dict(data, delivered=False)),
                 button('返回逐条检查', 'inspect', {'id': data['order']})], admin=True)
        elif command == 'resolve_reason': self.prompt(actor, '请输入实际聊天记录/消息编号等核实依据；不能仅因超时判定。', 'resolve_reason', data)
        elif command == 'resolve_confirm':
            self.s.resolve(actor, data['id'], data['delivered'], data['reason'])
            self.inspect(actor, {'id': data['order']})
        elif command == 'payment_review':
            self.s.admin(actor)
            row = self.app.payments._get(data['id'])
            self.show(actor, f'充值 {row["id"]}\n用户 {row["user_id"]} · {row["status"]}\n依据：{row["reason"]}\n本地关闭不加分、不退款、不代表上游取消。',
                [button('填写本地关闭依据', 'payment_reason', data), button('返回待核实', 'review')], admin=True)
        elif command == 'payment_reason': self.prompt(actor, '请输入人工核实依据；只关闭本地异常单。', 'payment_reason', data)
        elif command == 'payment_confirm': self.app.payments.close_no_credit(actor, data['id'], data['reason']); self.review(actor)
        else: raise RuleError('不支持的按钮操作，请发送 /menu。')

    def input(self, actor, message):
        session = self.db.one('SELECT * FROM ui_sessions WHERE actor=?', (actor,))
        if not session or not session['flow']:
            return False
        if session['admin_only']:
            self.s.admin(actor)
        text = message.get('text')
        if type(text) is not str:
            raise RuleError('当前步骤只接受文字，请输入提示要求的内容或发送 /menu 退出。')
        data, flow = json.loads(session['data']), session['flow']
        if flow == 'points_user': self.user_card(actor, parse_uint(text))
        elif flow == 'points_amount':
            data['delta'] = data.pop('sign') * parse_uint(text, maximum=MAX_POINTS)
            self.prompt(actor, '请输入本次人工加减分的具体原因（必填，最多 500 字符）：', 'points_reason', data)
        elif flow == 'points_reason':
            data['reason'] = bounded_text(text, 500).strip()
            row = self.s.user(data['user'])
            data['expected'] = [row['available'], row['frozen']]
            if row['available'] + data['delta'] < 0: raise RuleError('可用积分不足，不能扣减冻结积分。')
            self.show(actor, f'确认人工调整用户 {data["user"]} 积分 {data["delta"]:+d}？\n可用 {row["available"]} → {row["available"] + data["delta"]}，冻结 {row["frozen"]} 不变。\n原因：{data["reason"]}',
                [button('确认调整积分', 'adjust_confirm', data), button('取消调整', 'admin')], admin=True)
        elif flow == 'project_title':
            data['title'] = bounded_text(text, 100)
            self.prompt(actor, '请输入项目整数价格（1～1,000,000）：', 'project_price', data)
        elif flow == 'project_price':
            data['price'] = parse_uint(text, maximum=MAX_POINTS)
            self.show(actor, f'确认保存项目：{data["title"]}\n价格 {data["price"]} 积分 · {"上架" if data.get("active", 1) else "下架"}',
                [button('确认保存项目', 'project_save', data), button('取消修改', 'admin')], admin=True)
        elif flow in {'fail_reason', 'resolve_reason', 'payment_reason'}:
            data['reason'] = bounded_text(text, 500).strip()
            if flow == 'resolve_reason':
                text = f'确认记录 {data["id"]} 已人工核实送达（不重发该项）？' if data['delivered'] else f'确认订单 {data["order"]} 整单交付失败、停止剩余发送并释放全部冻结？'
                command = 'resolve_confirm'
            elif flow == 'fail_reason': text, command = f'确认终止订单 {data["id"]} 并释放冻结积分？', 'fail_confirm'
            else: text, command = '确认只关闭本地异常充值单（不加分/退款/取消上游）？', 'payment_confirm'
            self.show(actor, text + '\n核实依据：' + data['reason'],
                [button('确认人工处置', command, data), button('取消处置', 'admin')], admin=True)
        else: raise RuleError('输入流程无效，请发送 /menu。')
        return True
