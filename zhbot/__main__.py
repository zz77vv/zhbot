import argparse
import fcntl
import logging
import os
from pathlib import Path
from .store import Store
from .telegram import Telegram
from .app import App
from .runtime import Runtime
from .config import build_payments

def main():
    parser=argparse.ArgumentParser(description='Telegram 报单机器人（OKPay 默认关闭）')
    parser.add_argument('command',choices=['run','migrate','backup','check'])
    parser.add_argument('--destination',help='backup target')
    args=parser.parse_args()
    path=os.environ.get('DATABASE_PATH','data/zhbot.sqlite3')
    admin=int(os.environ.get('ADMIN_TELEGRAM_ID','0'))
    if admin<=0: parser.error('ADMIN_TELEGRAM_ID must be a positive Telegram user ID')
    os.umask(0o077)
    maintenance=args.command in ('check','backup')
    if maintenance and not Path(path).is_file():
        parser.error('Source database does not exist')
    if args.command=='backup' and not args.destination:
        parser.error('--destination required')
    if not maintenance:
        Path(path).parent.mkdir(parents=True,exist_ok=True)
    with open(path+'.lock','a') as lock:
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: parser.error('Another process owns this database')
        s=Store(path,admin,readonly=maintenance)
        if args.command=='backup':
            s.backup(args.destination); print('Backup completed')
        elif args.command=='check':
            integrity=s.rows('PRAGMA integrity_check')
            if integrity!=[{'integrity_check':'ok'}]:
                parser.error('Database integrity check failed')
            print(integrity); payments=build_payments(s); print('OKPay: configured (live validation pending)' if payments else 'OKPay: disabled')
        elif args.command=='run':
            logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
            payments=build_payments(s); t=Telegram(os.environ.get('BOT_TOKEN','')); Runtime(s,App(s,t,payments),t).run()
        else: print('Migrations completed')

if __name__=='__main__': main()
