"""Single-process long polling. Interrupted inbound updates are quarantined."""
import logging
import time

log=logging.getLogger(__name__)

class Runtime:
    def __init__(self,store,app,telegram): self.s,self.app,self.t=store,app,telegram
    def offset(self):
        rows=self.s.rows('SELECT MAX(id) AS last FROM inbox')
        return (rows[0]['last']+1) if rows[0]['last'] is not None else 0
    def recover(self):
        self.s.recover()
        with self.s.tx() as c: c.execute("UPDATE inbox SET state='uncertain' WHERE state='processing'")
    def dispatch(self,update):
        ident=update['update_id']
        with self.s.tx() as c:
            if not c.execute("INSERT OR IGNORE INTO inbox(id,state) VALUES(?,'processing')",(ident,)).rowcount: return
        self.app.handle(update)
        with self.s.tx() as c: c.execute("UPDATE inbox SET state='done' WHERE id=?",(ident,))
    def run(self):
        self.recover()
        while True:
            try:
                self.app.deliver(); self.app.flush_events()
                updates=self.t.call('getUpdates',offset=self.offset(),timeout=25,allowed_updates=['message','callback_query'])
                for update in updates:
                    try: self.dispatch(update)
                    except Exception as error:
                        log.error('Update %s quarantined (%s)',update['update_id'],type(error).__name__)
                        with self.s.tx() as c: c.execute("UPDATE inbox SET state='uncertain' WHERE id=?",(update['update_id'],))
            except Exception as error:
                log.error('Polling failed (%s)',type(error).__name__); time.sleep(3)
