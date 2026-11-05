"""Entirely offline example using only fictitious identities and file IDs."""
from pathlib import Path
from tempfile import TemporaryDirectory

from .app import Application
from .payments import Payments, SimulatedGateway
from .runtime import OutboxWorker
from .service import Service
from .store import Store


class SimulationTransport:
    def __init__(self):
        self.sequence = 0

    def send(self, message):
        self.sequence += 1
        return self.sequence


def run_demo():
    with TemporaryDirectory(prefix='zhbot-offline-') as tmp:
        db = Store(str(Path(tmp) / 'demo.sqlite3'))
        try:
            now = [1000]
            service = Service(db, {9001}, clock=lambda: now[0])
            gateway = SimulatedGateway()
            payments = Payments(service, gateway)
            app = Application(service, payments)
            worker = OutboxWorker(service, SimulationTransport())
            sequence = 0

            def flush():
                while worker.work_once():
                    pass

            def message(actor, text=None, **fields):
                nonlocal sequence
                sequence += 1
                msg = {'from': {'id': actor, 'is_bot': False},
                       'chat': {'id': actor, 'type': 'private'}, 'message_id': sequence}
                if text is not None:
                    msg['text'] = text
                msg.update(fields)
                app.process_update({'update_id': sequence, 'message': msg})
                flush()

            message(101, '/start')
            message(9001, '/start')
            message(9001, '/project new 3 on 模拟人工项目')
            message(101, '/recharge 5')
            payments.work_once()
            payment = db.one('SELECT * FROM payments')
            now[0] = 1001
            gateway.results[payment['id']] = gateway.paid(payment, paid_at=1001)
            message(101, '/check ' + payment['id'])
            payments.work_once()
            flush()
            message(101, '/new 1')
            message(101, '模拟报单文字')
            message(101, photo=[{'file_id': 'fake-customer-photo'}], caption='模拟报单图片')
            message(101, document={'file_id': 'fake-customer-file'}, caption='模拟报单文件')
            message(101, '/draft')
            message(101, '/confirm 1 3')
            message(9001, '/order 1')
            message(9001, '/receipt 1')
            message(9001, '模拟处理结果')
            for index in range(2):
                message(9001, photo=[{'file_id': f'fake-receipt-photo-{index}'}])
            for index in range(2):
                message(9001, document={'file_id': f'fake-receipt-file-{index}'})
            message(9001, '/preview 1')
            message(9001, '/deliver 1 5')
            available, frozen = service.balance(101)
            return {'implementation': 'fresh', 'payment_mode': 'offline_simulation',
                    'order_status': service.order(101, 1)['status'],
                    'available_points': available, 'frozen_points': frozen,
                    'ledger_entries': db.one('SELECT COUNT(*) AS n FROM ledger')['n'],
                    'receipt_parts': len(service.receipt(9001, 1)['items'])}
        finally:
            db.close()
