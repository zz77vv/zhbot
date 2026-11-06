import argparse
import fcntl
import logging
import os
from pathlib import Path
from .store import Store
from .telegram import Telegram
from .app import App
from .runtime import Runtime

def main():
    parser=argparse.ArgumentParser(description='Telegram 报单机器人（OKPay 集成待完成）')
    parser.add_argument('command',choices=['run','migrate','backup','check'])
    parser.add_argument('--destination',help='backup target')
    args=parser.parse_args()
    path=os.environ.get('DATABASE_PATH','data/zhbot.sqlite3')
    admin=int(os.environ.get('ADMIN_TELEGRAM_ID','0'))
    if admin<=0: parser.error('ADMIN_TELEGRAM_ID must be a positive Telegram user ID')
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    os.umask(0o077)
    with open(path+'.lock','a') as lock:
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: parser.error('Another process owns this database')
        s=Store(path,admin)
        if args.command=='backup':
            if not args.destination: parser.error('--destination required')
            s.backup(args.destination); print('Backup completed')
        elif args.command=='check':
            print(s.rows('PRAGMA integrity_check')); print('OKPay: BLOCKED (official SDK/contract not yet verified)')
        elif args.command=='run':
            logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
            t=Telegram(os.environ.get('BOT_TOKEN','')); Runtime(s,App(s,t),t).run()
        else: print('Migrations completed')

if __name__=='__main__': main()
