"""Bounded execution episodes; retry attempts belong to their original episode."""
from contextlib import closing
import json
import uuid

from common.codec import canonical


MAX_TERMINAL_RUNS = 1000
ACTIONS = {'wechat': 'collect', 'qq': 'collect', 'processor': 'process',
           'messaging': 'deliver', 'feedback': 'feedback'}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,module TEXT NOT NULL,action TEXT NOT NULL,
 origin TEXT NOT NULL,status TEXT NOT NULL,started_at REAL NOT NULL,finished_at REAL,
 attempt_count INTEGER NOT NULL DEFAULT 0,error TEXT,result TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS runs_started ON runs(started_at DESC);
CREATE TABLE IF NOT EXISTS run_attempts(run_id TEXT NOT NULL,number INTEGER NOT NULL,
 status TEXT NOT NULL,started_at REAL NOT NULL,finished_at REAL,error TEXT,
 result TEXT NOT NULL DEFAULT '{}',details TEXT,PRIMARY KEY(run_id,number));
'''


def _prune(conn, name):
    stale = "SELECT id FROM runs WHERE module=? AND finished_at IS NOT NULL ORDER BY started_at DESC,rowid DESC LIMIT -1 OFFSET ?"
    conn.execute('DELETE FROM run_attempts WHERE run_id IN (' + stale + ')', (name, MAX_TERMINAL_RUNS))
    conn.execute('DELETE FROM runs WHERE id IN (' + stale + ')', (name, MAX_TERMINAL_RUNS))


def _begin(conn, row, origin, now):
    run = conn.execute('SELECT * FROM runs WHERE id=? AND finished_at IS NULL', (row['current_run_id'],)).fetchone()
    if run is None:
        identifier = uuid.uuid4().hex
        conn.execute("INSERT INTO runs(id,module,action,origin,status,started_at) VALUES(?,?,?,?,'running',?)",
                     (identifier, row['name'], ACTIONS[row['name']], origin, now))
        number = 1
    else:
        identifier, number = run['id'], run['attempt_count'] + 1
    conn.execute("UPDATE runs SET status='running',attempt_count=?,error=NULL,result='{}' WHERE id=?", (number, identifier))
    conn.execute("INSERT INTO run_attempts(run_id,number,status,started_at) VALUES(?,?,'running',?)", (identifier, number, now))
    conn.execute('UPDATE jobs SET current_run_id=? WHERE name=?', (identifier, row['name']))


def _finish(conn, name, status, now, *, error=None, result=None, details=None, interrupted=False):
    row = conn.execute('SELECT current_run_id FROM jobs WHERE name=?', (name,)).fetchone()
    if row is None or row['current_run_id'] is None:
        return
    identifier = row['current_run_id']
    # An interrupted attempt may still be retried; only exhausted episodes finish.
    attempt_status = 'interrupted' if interrupted else 'failed' if status == 'retry' else status
    conn.execute("UPDATE run_attempts SET status=?,finished_at=?,error=?,result=?,details=? WHERE run_id=? AND status='running'",
                 (attempt_status, now, error, canonical(result or {}), canonical(details) if details else None, identifier))
    conn.execute('UPDATE runs SET status=?,finished_at=?,error=?,result=? WHERE id=? AND finished_at IS NULL',
                 (status, None if status == 'retry' else now, error, canonical(result or {}), identifier))
    _prune(conn, name)


def _record(row):
    record = dict(row)
    record['result'] = json.loads(record['result'])
    record['duration_seconds'] = (max(0, record['finished_at'] - record['started_at'])
                                  if record['finished_at'] is not None else None)
    return record


def list_runs(config, *, limit=100, module=None, origin=None, status=None):
    """Read newest episodes without creating or upgrading the state database."""
    from .core import _connect
    if type(limit) is not int or not 1 <= limit <= MAX_TERMINAL_RUNS:
        raise ValueError('History limit must be between 1 and 1000')
    filters = {'module': module, 'origin': origin, 'status': status}
    clauses = [key + '=?' for key, value in filters.items() if value is not None]
    conn = _connect(config)
    if conn is None:
        return []
    with closing(conn):
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'").fetchone():
            return []
        query = 'SELECT * FROM runs' + (' WHERE ' + ' AND '.join(clauses) if clauses else '')
        rows = conn.execute(query + ' ORDER BY started_at DESC,rowid DESC LIMIT ?',
                            [value for value in filters.values() if value is not None] + [limit])
        return [_record(row) for row in rows]


def get_run(config, identifier):
    """Return one episode and its attempts, or None when it is unavailable."""
    from .core import _connect
    conn = _connect(config)
    if conn is None:
        return None
    with closing(conn):
        conn.execute('BEGIN')
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'").fetchone():
            return None
        row = conn.execute('SELECT * FROM runs WHERE id=?', (identifier,)).fetchone()
        if row is None:
            return None
        record = _record(row)
        record['attempts'] = []
        for row in conn.execute('SELECT number,status,started_at,finished_at,error,result,details FROM run_attempts WHERE run_id=? ORDER BY number', (identifier,)):
            attempt = dict(row)
            for key in ('result', 'details'):
                attempt[key] = json.loads(attempt[key]) if attempt[key] is not None else None
            record['attempts'].append(attempt)
        return record
