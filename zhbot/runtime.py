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
        self._last_was_answer = False

    def _answer_once(self):
        answer = self.s.claim_callback_answer()
        if answer is None:
            return False
        self._last_was_answer = True
        try:
            accepted = self.transport.answer_callback(answer['id'], answer['answer'])
        except TelegramError as error:
            self.s.callback_answered(answer['id'], False)
            if error.fatal:
                raise
        except Exception:
            self.s.callback_answered(answer['id'], False)
        else:
            self.s.callback_answered(answer['id'], accepted is True, unavailable=accepted is False)
        return True

    def work_once(self):
        # At most one acknowledgment before giving business messages a turn.
        if not self._last_was_answer and self._answer_once():
            return True
        message = self.s.claim_message()
        if message is None:
            return self._answer_once()
        self._last_was_answer = False
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
