"""Internal authoritative task persistence and durable JSONL publication."""
import json
import os
from pathlib import Path
import sqlite3

from common.codec import canonical, digest


SCHEMA = '''
CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, size INTEGER, mtime INTEGER, sha256 TEXT);
CREATE TABLE IF NOT EXISTS events(event_id TEXT PRIMARY KEY, body_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages(message_key TEXT PRIMARY KEY, revision INTEGER, body TEXT);
CREATE TABLE IF NOT EXISTS groups(id TEXT PRIMARY KEY, fingerprint TEXT, task_ids TEXT);
CREATE TABLE IF NOT EXISTS group_results(id TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks(task_id TEXT PRIMARY KEY, revision INTEGER, body TEXT);
CREATE TABLE IF NOT EXISTS outbox(seq INTEGER PRIMARY KEY AUTOINCREMENT, body TEXT, exported INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS task_status_requests(request_id TEXT PRIMARY KEY, request TEXT NOT NULL, result TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS task_requests(request_id TEXT PRIMARY KEY, request TEXT NOT NULL, result TEXT NOT NULL, created_at TEXT NOT NULL);
'''


def _connect(config, *, write=False):
    path = Path(config['storage']['state_file'])
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.executescript(SCHEMA)
        return conn
    if not path.exists():
        return None
    return sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)


def _save_task(conn, task):
    for item in task.get('materials', []):
        item['material_id'] = digest([task['task_id'], item['message_key'], item['attachment_index']])
    previous = conn.execute('SELECT revision FROM tasks WHERE task_id=?', (task['task_id'],)).fetchone()
    task['revision'] = previous[0] + 1 if previous else 1
    body = canonical(task)
    conn.execute('INSERT INTO tasks VALUES(?,?,?) ON CONFLICT(task_id) DO UPDATE SET revision=excluded.revision,body=excluded.body',
                 (task['task_id'], task['revision'], body))
    event = {'schema_version': 1, 'event_id': digest([task['task_id'], task['revision']]),
             'operation': 'upsert', 'task_id': task['task_id'], 'revision': task['revision'], 'task': task}
    conn.execute('INSERT INTO outbox(body) VALUES(?)', (canonical(event),))


def _export(config, conn):
    output = Path(config['storage']['output_dir'])
    output.mkdir(parents=True, exist_ok=True)
    batches = []
    while rows := conn.execute('SELECT seq,body FROM outbox WHERE exported=0 ORDER BY seq LIMIT 1000').fetchall():
        destination = output / f'todos-{rows[0][0]:012d}-{rows[-1][0]:012d}.jsonl'
        raw = ('\n'.join(row[1] for row in rows) + '\n').encode('utf-8')
        if destination.exists():
            if destination.read_bytes() != raw:
                raise RuntimeError('Task output conflicts with the durable outbox')
        else:
            temporary = destination.with_suffix('.jsonl.tmp')
            with temporary.open('wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(destination)
        with conn:
            conn.execute('UPDATE outbox SET exported=1 WHERE seq BETWEEN ? AND ?', (rows[0][0], rows[-1][0]))
        batches.append(str(destination))
    return batches

