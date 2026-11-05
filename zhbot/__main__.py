"""Explicit commands: offline demo, or an operator-configured long poll runner."""
import argparse
import json
import logging
import os
from pathlib import Path
import signal
import sqlite3
import sys
import tempfile
import threading

from .app import Application
from .core import RuleError, parse_uint
from .demo import run_demo
from .runtime import OutboxWorker, Poller, SingleInstance, bind_bot
from .service import Service
from .store import Store
from .telegram import Telegram, TelegramError


def run_bot(database, admins, api, *, stop=None, lock_root=None):
    stop = stop or threading.Event()
    Path(database).resolve().parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with SingleInstance(database):
        bot_id = api.identity()
        if api.has_webhook():
            raise RuntimeError('该机器人已有 webhook；本程序不会删除它，也不会启动 polling。')
        # Protect the same bot even if a second local process chooses a different DB.
        root = Path(lock_root) if lock_root else Path(tempfile.gettempdir()) / f'zhbot-{os.getuid()}'
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with SingleInstance(root / f'bot-{bot_id}'):
            db = Store(database)
            try:
                service = Service(db, admins)
                bind_bot(service, bot_id)
                service.recover()
                app = Application(service)  # Always uses DisabledGateway.
                outbox = OutboxWorker(service, api)
                poller = Poller(app, api)
                backoff = 1
                while not stop.is_set():
                    try:
                        # Bounded drain prevents a large queue starving inbound updates.
                        for _ in range(20):
                            if stop.is_set() or not outbox.work_once():
                                break
                            stop.wait(1.05)
                        if stop.is_set():
                            break
                        poller.poll_once()
                        for _ in range(20):
                            if stop.is_set() or not outbox.work_once():
                                break
                            stop.wait(1.05)
                        backoff = 1
                    except TelegramError as error:
                        if error.fatal:
                            raise
                        logging.warning('Telegram 暂不可用；保留本地状态后重试轮询。')
                        stop.wait(min(error.retry_after or backoff, 30))
                        backoff = min(backoff * 2, 30)
            except KeyboardInterrupt:
                pass
            finally:
                db.close()


def main(argv=None, *, environ=None):
    parser = argparse.ArgumentParser(description='Telegram 综合报单机器人 V1.0 · 全新实现')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('demo', help='仅用模拟数据运行离线端到端演示，不连接 Telegram 或支付')
    run = commands.add_parser('run', help='从环境读取配置，启动 Telegram long polling；真实支付始终关闭')
    run.add_argument('--db', default='var/bot.sqlite3', help='专用本地 SQLite 数据库路径')
    args = parser.parse_args(argv)
    if args.command == 'demo':
        print(json.dumps(run_demo(), ensure_ascii=False, indent=2))
        return 0
    env = os.environ if environ is None else environ
    try:
        raw_admins = env.get('TELEGRAM_ADMIN_IDS', '')
        if not raw_admins:
            raise RuleError('请配置 TELEGRAM_ADMIN_IDS。')
        admins = {parse_uint(x.strip()) for x in raw_admins.split(',')}
        api = Telegram(env.get('TELEGRAM_BOT_TOKEN'))
    except RuleError as error:
        print(str(error), file=sys.stderr)
        return 2
    os.umask(0o077)
    stop = threading.Event()
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        run_bot(args.db, admins, api, stop=stop)
    except (RuntimeError, TelegramError):
        print('启动或轮询已停止：请核实机器人身份、webhook、重复实例和连接；未自动修改外部配置。', file=sys.stderr)
        return 1
    except (sqlite3.Error, OSError):
        print('本地存储或运行环境异常，程序停止；请检查数据库后再重启。', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
