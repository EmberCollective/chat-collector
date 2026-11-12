import hashlib
import json
import os
from pathlib import Path
import sqlite3


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


class Store:
    def __init__(self, path, platform, account):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS seen(message_key TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, revision INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox(seq INTEGER PRIMARY KEY AUTOINCREMENT, body TEXT NOT NULL, exported INTEGER NOT NULL DEFAULT 0);
        ''')
        identity = canonical([platform, str(account)])
        stored = self.get('identity')
        if stored is not None and stored != identity:
            self.db.close()
            raise ValueError('State database belongs to a different platform/account')
        with self.db:
            self.set('identity', identity)
        self.identity = digest([platform, str(account)])[:16]
        self.platform, self.account = platform, str(account)

    def close(self):
        self.db.close()

    def get(self, key):
        row = self.db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return row[0] if row else None

    def set(self, key, value):
        self.db.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(value)))

    def ingest(self, messages, window_start, window_end, collected_at, advance=True):
        matched = emitted = 0
        with self.db:
            for message in messages:
                identity = [message.get(k) for k in ('platform', 'account_id', 'conversation_id', 'message_id')]
                if any(value is None or str(value) == '' for value in identity):
                    raise ValueError('Message lacks a stable platform/account/conversation/message identity')
                if [str(value) for value in identity[:2]] != [self.platform, self.account]:
                    raise ValueError('Message platform/account does not match this collector')
                message_key = digest([str(value) for value in identity])
                semantic = {k: v for k, v in message.items() if k != 'source'}
                payload_hash = digest(semantic)
                previous = self.db.execute('SELECT payload_hash,revision FROM seen WHERE message_key=?', (message_key,)).fetchone()
                matched += 1
                if previous and previous[0] == payload_hash:
                    continue
                revision = previous[1] + 1 if previous else 1
                event = {'schema_version': 1, 'event_id': digest([message_key, revision]),
                         'message_key': message_key, 'revision': revision, 'operation': 'upsert',
                         'payload_sha256': payload_hash, 'collected_at': collected_at,
                         'window': {'start_inclusive': window_start, 'end_exclusive': window_end},
                         'message': message}
                self.db.execute('INSERT INTO outbox(body) VALUES (?)', (canonical(event),))
                self.db.execute('INSERT INTO seen VALUES (?,?,?) ON CONFLICT(message_key) DO UPDATE SET payload_hash=excluded.payload_hash,revision=excluded.revision', (message_key, payload_hash, revision))
                emitted += 1
            if advance:
                self.set('window_end', window_end)
        return {'matched': matched, 'emitted': emitted, 'duplicates': matched - emitted}

    def export(self, output_dir):
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        produced = []
        while True:
            rows = self.db.execute('SELECT seq,body FROM outbox WHERE exported=0 ORDER BY seq LIMIT 1000').fetchall()
            if not rows:
                return produced
            name = f'{self.identity}-{rows[0][0]:012d}-{rows[-1][0]:012d}.jsonl'
            destination = output_dir / name
            data = ('\n'.join(row[1] for row in rows) + '\n').encode('utf-8')
            if destination.exists():
                if destination.read_bytes() != data:
                    raise RuntimeError('Output batch conflicts with durable outbox: ' + name)
            else:
                temporary = destination.with_suffix('.jsonl.tmp')
                with temporary.open('wb') as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.replace(destination)
            with self.db:
                self.db.execute('UPDATE outbox SET exported=1 WHERE seq BETWEEN ? AND ?', (rows[0][0], rows[-1][0]))
            produced.append(str(destination))
