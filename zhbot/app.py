"""Private-chat command router with atomic inbound deduplication and cursor."""
import sqlite3

from .core import MAX_ID, RuleError, bounded_text, parse_uint
from .payments import Payments


HELP = '''综合报单机器人 V1.0（新实现）
/start 注册 · /balance 积分 · /projects 可用项目
/new 项目ID 开始草稿；随后发送文字、图片、文件
/text 内容 可提交以 / 开头的文字
/draft 完整预览 · /confirm 草稿ID 版本 确认冻结
/undo 撤销末项 · /clear 清空当前材料 · /cancel 取消客户草稿
/orders [页码] · /order 订单ID
/recharge 整数金额 · /payments · /check 充值单号
真实充值尚未开放；只支持人工报单。
管理员：/admin 查看管理命令。'''

ADMIN_HELP = '''仅限管理员私聊：
/project new|项目ID 价格 on|off 项目名称
/adminprojects 查看所有项目
/orders [页码] · /order 订单ID 查看完整报单
/receipt 订单ID 选择回执，随后发送文字、图片、文件
/undo 撤销末项 · /clear 清空当前编辑内容
/preview 订单ID 完整预览回执
/deliver 订单ID 版本 确认交付
/fail 订单ID 原因 终止尚未交付订单
/review [页码] 查看交付与充值待核实项
/resolve 消息记录ID sent|failed 核实依据
/payment_close 充值单号 核实依据 本地关闭异常单（不入账/退款，不代表上游取消）
sent 表示已人工确认该项送达；failed 表示已核实整单交付失败、不扣费。
结果不明不能直接终止或重发。'''


class Application:
    def __init__(self, service, payments=None):
        self.s = service
        self.db = service.db
        self.payments = payments if payments is not None else Payments(service)

    def offset(self):
        row = self.db.one("SELECT value FROM settings WHERE key='next_offset'")
        return int(row['value']) if row else 0

    def process_update(self, update):
        if type(update) is not dict or type(update.get('update_id')) is not int:
            raise RuleError('Telegram 更新编号无效。')
        number = parse_uint(update['update_id'], minimum=0, maximum=MAX_ID - 1)
        with self.db.atomic():
            if self.db.one('SELECT id FROM processed_updates WHERE id=?', (number,)) or number < self.offset():
                return False
            actor, message = self._private_message(update)
            status = 'ignored'
            if actor is not None:
                try:
                    with self.db.atomic():
                        self.dispatch(actor, message)
                    status = 'done'
                except sqlite3.Error:
                    raise  # A broken disk/transaction must never acknowledge the update.
                except RuleError as error:
                    self.s.note(actor, str(error))
                    status = 'rejected'
                except Exception:
                    # A programming error cannot poison the polling queue indefinitely.
                    # The savepoint above rolls back every domain/reply mutation first.
                    status = 'quarantined'
                    self.s.audit(0, 'update_quarantined', number, '内部处理异常；原始内容不记录')
                    self.s.note(actor, '此条消息处理异常，业务变更已回滚。请联系管理员核实后重试。')
                    for admin in self.s.admins:
                        self.s.note(admin, f'更新 {number} 已隔离，业务变更已回滚。请检查本地审计记录。', admin_only=True)
            self.db.execute('INSERT INTO processed_updates(id,status,at) VALUES (?,?,?)', (number, status, self.s.clock()))
            self.db.execute("INSERT INTO settings(key,value) VALUES ('next_offset',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(number + 1),))
            return True

    @staticmethod
    def _private_message(update):
        message = update.get('message')
        if type(message) is not dict:
            return None, None
        sender, chat = message.get('from'), message.get('chat')
        if type(sender) is not dict or type(chat) is not dict:
            return None, None
        actor = sender.get('id')
        if (type(actor) is not int or actor <= 0 or actor > MAX_ID
                or sender.get('is_bot') is not False or chat.get('type') != 'private'
                or type(chat.get('id')) is not int or chat['id'] != actor):
            return None, None
        return actor, message

    @staticmethod
    def _args(text, count):
        args = text.split()
        if len(args) != count:
            raise RuleError('命令参数不正确，使用 /help 或 /admin 查看格式。')
        return args

    def dispatch(self, actor, message):
        text = message.get('text')
        if text is not None and type(text) is not str:
            raise RuleError('文字内容无效。')
        if type(text) is str and text.startswith('/'):
            bounded_text(text, 4000)
            command, _, body = text.partition(' ')
            body = body.strip()
            # No @suffix routing or callback actions: only explicit private commands.
            if command == '/start':
                self.s.register(actor)
                self.s.note(actor, HELP)
                return
            if command == '/help':
                self.s.note(actor, HELP)
                return
            if command == '/admin':
                self.s.admin(actor)
                self.s.note(actor, ADMIN_HELP, admin_only=True)
                return
            self.s.user(actor)
            if command == '/balance':
                available, frozen = self.s.balance(actor)
                self.s.note(actor, f'可用 {available} 积分，冻结 {frozen} 积分。')
            elif command in {'/projects', '/adminprojects'}:
                rows = self.s.projects(actor, command == '/adminprojects')
                lines = [f'{r["id"]}: {r["title"]} · {r["price"]} 积分 · {"上架" if r["active"] else "下架"}' for r in rows]
                self._lines(actor, lines or ['暂无项目。'], admin_only=command == '/adminprojects')
            elif command == '/project':
                self.s.admin(actor)
                args = body.split(maxsplit=3)
                if len(args) != 4 or args[2] not in {'on', 'off'}:
                    raise RuleError('格式：/project new|项目ID 价格 on|off 名称')
                project = None if args[0] == 'new' else parse_uint(args[0])
                project = self.s.save_project(actor, project, args[3], args[1], args[2] == 'on')
                self.s.note(actor, f'项目 {project} 已保存。', admin_only=True)
            elif command == '/new':
                draft = self.s.new_draft(actor, self._args(body, 1)[0])
                self.s.note(actor, f'草稿 {draft} 已建立，请发送材料；/draft 预览后确认才冻结积分。')
            elif command == '/draft':
                draft = self.s.resume_customer(actor)
                if draft is None:
                    self.s.note(actor, '已退出回执编辑；暂无客户草稿，请使用 /new 项目ID 创建。')
                elif not draft['items']:
                    self.s.note(actor, f'已切换到客户草稿 {draft["id"]}，请先添加材料，再使用 /draft 完整预览。')
                else:
                    self.s.preview_customer(actor, draft['id'])
            elif command == '/confirm':
                args = self._args(body, 2)
                order = self.s.confirm(actor, args[0], args[1])
                status = self.s.order(actor, order)['status']
                self.s.note(actor, f'订单 {order} 当前状态：{status}。重复确认不会重复冻结，余额请使用 /balance 查看。')
            elif command == '/cancel':
                self.s.cancel_draft(actor)
                self.s.note(actor, '客户草稿已取消，未冻结积分。')
            elif command in {'/undo', '/clear'}:
                selected = self.db.one('SELECT kind FROM selections WHERE actor=?', (actor,))
                operation = self.s.undo_selected if command == '/undo' else self.s.clear_selected
                operation(actor)
                self.s.note(actor, '当前编辑内容已更新，请重新预览。', admin_only=bool(selected and selected['kind'] == 'receipt'))
            elif command == '/orders':
                page = parse_uint(body or '1', maximum=1_000_000)
                if actor in self.s.admins:
                    rows = self.db.all('SELECT id,title,status,price FROM orders ORDER BY id DESC LIMIT 20 OFFSET ?', ((page - 1) * 20,))
                else:
                    rows = self.db.all('SELECT id,title,status,price FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 20 OFFSET ?', (actor, (page - 1) * 20))
                self._lines(actor, [f'{r["id"]}: {r["title"]} · {r["status"]} · {r["price"]} 积分' for r in rows] or ['暂无订单。'], admin_only=actor in self.s.admins)
            elif command == '/order':
                row = self.s.order(actor, self._args(body, 1)[0])
                summary = {'kind': 'text', 'content': f'订单 {row["id"]} · {row["title"]} · {row["status"]} · {row["price"]} 积分'}
                self.s.batch(actor, actor, 'note', [summary] + row['items'], ref=row['id'], admin_only=row['user_id'] != actor)
            elif command == '/receipt':
                self.s.open_receipt(actor, self._args(body, 1)[0])
                self.s.note(actor, '已选择回执；发送文字、图片、文件继续编辑。/preview 订单ID 查看当前全部内容。', admin_only=True)
            elif command == '/preview':
                self.s.preview_receipt(actor, self._args(body, 1)[0])
            elif command == '/deliver':
                args = self._args(body, 2)
                batch = self.s.deliver(actor, args[0], args[1])
                self.s.note(actor, f'交付批次 {batch} 已登记；结果以订单状态为准。', admin_only=True)
            elif command == '/fail':
                args = body.split(maxsplit=1)
                if len(args) != 2:
                    raise RuleError('格式：/fail 订单ID 原因')
                self.s.fail_order(actor, args[0], args[1])
                self.s.note(actor, '订单已终止，冻结积分已释放。', admin_only=True)
            elif command == '/review':
                self.s.admin(actor)
                page = parse_uint(body or '1', maximum=1_000_000)
                rows = self.db.all("""SELECT m.id,b.ref,m.status,m.reason FROM messages m
                    JOIN batches b ON b.id=m.batch_id JOIN orders o ON o.id=b.ref
                    WHERE b.kind='delivery' AND o.status='delivery_review' AND m.status IN ('unknown','failed')
                    ORDER BY m.id LIMIT 20 OFFSET ?""", ((page - 1) * 20,))
                payments = self.db.all("SELECT id,status,reason FROM payments WHERE status IN ('unknown','review','closed_review') ORDER BY created_at LIMIT 20 OFFSET ?", ((page - 1) * 20,))
                self._lines(actor, [f'订单 {r["ref"]}，消息记录 {r["id"]}，{r["status"]}：{r["reason"]}' for r in rows]
                            + [f'充值 {r["id"]}，{r["status"]}：{r["reason"]}' for r in payments] or ['暂无待核实项。'], admin_only=True)
            elif command == '/resolve':
                self.s.admin(actor)
                args = body.split(maxsplit=2)
                if len(args) != 3 or args[1] not in {'sent', 'failed'}:
                    raise RuleError('格式：/resolve 消息记录ID sent|failed 人工核实依据')
                self.s.resolve(actor, args[0], args[1] == 'sent', args[2])
                self.s.note(actor, '人工核实已记录，请通过 /order 查看最终状态。', admin_only=True)
            elif command == '/recharge':
                payment = self.payments.request(actor, self._args(body, 1)[0])
                self.s.note(actor, f'【离线模拟】充值单 {payment} 已登记。')
            elif command == '/payment_close':
                self.s.admin(actor)
                args = body.split(maxsplit=1)
                if len(args) != 2:
                    raise RuleError('格式：/payment_close 充值单号 人工核实依据；仅本地关闭，不入账/退款。')
                self.payments.close_no_credit(actor, args[0], args[1])
                self.s.note(actor, '本地充值单已核实关闭，未加分或退款，也未取消上游订单。', admin_only=True)
            elif command == '/payments':
                rows = self.db.all('SELECT id,status,amount FROM payments WHERE user_id=? ORDER BY created_at DESC LIMIT 20', (actor,))
                self._lines(actor, [f'{r["id"]} · {r["amount"]} USDT · {r["status"]}' for r in rows] or ['暂无充值单；真实充值未开放。'])
            elif command == '/check':
                payment = self._args(body, 1)[0]
                row = self.payments.get(actor, payment)
                self.payments.request_check(actor, payment)
                self.s.note(actor, f'充值单 {payment} 当前 {row["status"]}；查单请求已登记或已入账。')
            elif command == '/text':
                self._append(actor, 'text', bounded_text(body))
            else:
                raise RuleError('未知命令，请使用 /help。')
            return
        if text is not None:
            self._append(actor, 'text', text)
        elif 'photo' in message:
            photos = message['photo']
            if type(photos) is not list or not photos or type(photos[-1]) is not dict:
                raise RuleError('图片格式无效。')
            self._append(actor, 'photo', photos[-1].get('file_id'), message.get('caption', ''))
        elif 'document' in message:
            document = message['document']
            if type(document) is not dict:
                raise RuleError('文件格式无效。')
            self._append(actor, 'document', document.get('file_id'), message.get('caption', ''))
        else:
            raise RuleError('仅支持文字、图片和文件。')

    def _append(self, actor, kind, content, caption=''):
        selection = self.db.one('SELECT * FROM selections WHERE actor=?', (actor,))
        if selection and selection['kind'] == 'receipt':
            self.s.add_receipt_item(actor, kind, content, caption)
        else:
            self.s.add_customer_item(actor, kind, content, caption)
        self.s.note(actor, '已保存一项内容；请完整预览后确认。', admin_only=bool(selection and selection['kind'] == 'receipt'))

    def _lines(self, actor, lines, *, admin_only=False):
        # Split listing output without splitting an attachment or Unicode surrogate pair.
        chunk = ''
        for line in lines:
            if len((chunk + '\n' + line).encode('utf-16-le')) // 2 > 3400:
                self.s.note(actor, chunk, admin_only=admin_only)
                chunk = ''
            chunk += ('\n' if chunk else '') + line
        if chunk:
            self.s.note(actor, chunk, admin_only=admin_only)
