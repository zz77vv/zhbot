"""Single-runner polling and transport coordination for a local SQLite DB."""
from contextlib import AbstractContextManager
import fcntl
from pathlib import Path

from .core import parse_uint
from .telegram import TelegramError


class SingleInstance(AbstractContextManager):
    def __init__(self, database):
        self.path = str(Path(database).resolve()) + '.lock'
        self.file = None

    def __enter__(self):
        self.file = open(self.path, 'a', encoding='utf-8')
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.file.close()
            self.file = None
            raise RuntimeError('此数据库已有运行实例；禁止并行 long polling。') from None
        return self

    def __exit__(self, *args):
        if self.file:
            fcntl.flock(self.file, fcntl.LOCK_UN)
            self.file.close()
            self.file = None


def bind_bot(service, bot_id):
    bot_id = parse_uint(bot_id)
    with service.db.atomic():
        row = service.db.one("SELECT value FROM settings WHERE key='bot_id'")
        if row and row['value'] != str(bot_id):
            raise RuntimeError('数据库绑定了另一个机器人，禁止复用。')
        service.db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES ('bot_id',?)", (str(bot_id),))


class OutboxWorker:
    def __init__(self, service, transport):
        self.s = service
        self.transport = transport

    def work_once(self):
        message = self.s.claim_message()
        if message is None:
            return False
        try:
            telegram_id = self.transport.send(message)
        except TelegramError as error:
            if error.retry_after is not None:
                self.s.defer(message['id'], error.retry_after)
            elif error.definite:
                self.s.rejected(message['id'], 'Telegram 明确拒绝此项发送')
            else:
                self.s.uncertain(message['id'], 'Telegram 发送结果未知')
            if error.fatal:
                raise
        except Exception:
            self.s.uncertain(message['id'], '发送适配器异常，结果未知')
        else:
            # Deliberately outside the network try-block. A recording failure exits
            # the runner, leaving sending to become unknown during recovery.
            self.s.sent(message['id'], telegram_id)
        return True


class Poller:
    def __init__(self, app, transport):
        self.app = app
        self.transport = transport

    def poll_once(self):
        updates = self.transport.get_updates(self.app.offset())
        for update in sorted(updates, key=lambda x: x['update_id']):
            self.app.process_update(update)
        return len(updates)
