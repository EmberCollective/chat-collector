"""Bind a capture store to its verified account without adopting unknown history."""
import sqlite3
from contextlib import closing
from .transport import SourceError


def bind_account(root, account_id):
    path = root / 'classroom-output' / 'school-reception' / 'inbox.local.sqlite'
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS capture_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
        prior = db.execute("SELECT value FROM capture_metadata WHERE key='account_id'").fetchone()
        if prior and prior[0] != account_id:
            raise SourceError('account_changed_use_new_state', 3600)
        has_messages = db.execute("SELECT 1 FROM sqlite_master WHERE name='messages'").fetchone()
        if not prior and has_messages and db.execute('SELECT 1 FROM messages LIMIT 1').fetchone():
            raise SourceError('unbound_history_use_new_state', 3600)
        db.execute("INSERT OR IGNORE INTO capture_metadata VALUES('account_id',?)", (account_id,))
