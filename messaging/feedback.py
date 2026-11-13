"""Durable feedback inbox; callbacks never wait for processing or message delivery."""
from __future__ import annotations

from contextlib import closing
import datetime as dt
import json
import math
from pathlib import Path
import sqlite3
import time
import uuid

from collector.__main__ import InstanceLock
from collector.store import canonical, digest


SCHEMA = '''
CREATE TABLE IF NOT EXISTS feedback_requests(
 request_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, request TEXT NOT NULL,
 acknowledgement TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER DEFAULT 0,
 next_attempt REAL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL,
 snoozed_until REAL, result TEXT, error TEXT);
CREATE INDEX IF NOT EXISTS feedback_task_status ON feedback_requests(task_id,status);
'''


def _connect(config, write=False):
    path = Path(config['feedback']['state_file'])
    if not write and not path.exists():
        return None
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=1)
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if any(name != 'feedback_requests' and not name.startswith('feishu_') for name in tables):
            conn.close()
            raise ValueError('feedback_state_wrong_database')
        conn.executescript(SCHEMA)
    else:
        conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1)
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='feedback_requests'").fetchone():
            conn.close()
            return None
    conn.row_factory = sqlite3.Row
    return conn


def _text(value, name, maximum=200):
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError('invalid_feedback_' + name)
    return value.strip()


def _until(value):
    if isinstance(value, str):
        parsed = dt.datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError('feedback_snooze_requires_timezone')
        value = parsed.timestamp()
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError('invalid_feedback_snooze_time')
    return float(value)


def _enqueue(config, target_id, event_id, task_id, action, actor, expected_revision=None, *, until=None, priority=None):
    if not config.get('feedback', {}).get('enabled', False):
        raise ValueError('feedback_disabled')
    target_id = _text(target_id, 'target_id')
    event_id = _text(event_id, 'event_id')
    task_id = _text(task_id, 'task_id')
    target = next((t for t in config.get('targets', []) if t['id'] == target_id), None)
    if target is None and target_id != 'local':
        raise ValueError('feedback_target_not_registered')
    if not isinstance(action, str) or action not in {'complete', 'reopen', 'cancel', 'snooze', 'set_priority'}:
        raise ValueError('invalid_feedback_action')
    if action == 'set_priority':
        if not isinstance(priority, str) or priority not in {'P0', 'P1', 'P2', 'P3', 'P4'}:
            raise ValueError('invalid_feedback_priority')
    elif priority is not None:
        raise ValueError('feedback_priority_only_for_set_priority')
    if not isinstance(actor, dict) or set(actor) != {'platform', 'user_id', 'source'}:
        raise ValueError('invalid_feedback_actor')
    actor = {k: _text(actor[k], 'actor_' + k, 64 if k == 'platform' else 200) for k in ('platform', 'user_id', 'source')}
    if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 1):
        raise ValueError('invalid_feedback_expected_revision')
    if until is not None:
        if action != 'snooze':
            raise ValueError('feedback_until_only_for_snooze')
        until = _until(until)
    payload = {'target_id': target_id, 'event_id': event_id, 'task_id': task_id, 'action': action,
               'actor': actor, 'expected_revision': expected_revision, 'until': until}
    if action == 'set_priority':
        payload['priority'] = priority
    request = canonical(payload)
    request_id = digest(['task-feedback.v1', target_id, event_id])
    now = time.time()
    with closing(_connect(config, True)) as conn, conn:
        # The unique key handles concurrent duplicate callbacks without a process-wide lock.
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT request,acknowledgement FROM feedback_requests WHERE request_id=?', (request_id,)).fetchone()
        if row:
            if row['request'] != request:
                raise ValueError('feedback_event_content_conflict')
            return json.loads(row['acknowledgement'])
        if action == 'snooze':
            if until is None:
                seconds = (target or {}).get('snooze_seconds', 3600)
                if type(seconds) is not int or not 1 <= seconds <= 31536000:
                    raise ValueError('invalid_feedback_snooze_seconds')
                until = now + seconds
            if until <= now:
                raise ValueError('feedback_snooze_must_be_future')
        ack = {'request_id': request_id, 'task_id': task_id, 'action': action, 'status': 'queued'}
        conn.execute('INSERT INTO feedback_requests(request_id,task_id,request,acknowledgement,status,created_at,updated_at,snoozed_until) VALUES(?,?,?,?,?,?,?,?)',
                     (request_id, task_id, request, canonical(ack), 'queued', now, now, until))
        return ack


def enqueue_feedback(config, target_id, event_id, task_id, action, actor, *, expected_revision=None, priority=None):
    return _enqueue(config, target_id, event_id, task_id, action, actor, expected_revision, priority=priority)


def request_task_action(config, task_id, action, *, event_id=None, actor=None, expected_revision=None, until=None):
    if not config.get('feedback', {}).get('enabled', False):
        from .core import set_task_state
        return set_task_state(config, task_id, action, until=until)
    return _enqueue(config, 'local', event_id or uuid.uuid4().hex, task_id, action,
                    actor or {'platform': 'local', 'user_id': 'local-user', 'source': 'local-console'},
                    expected_revision, until=until)


def blocked_task_ids(config):
    if not config.get('feedback', {}).get('enabled', False):
        return set()
    conn = _connect(config)
    try:
        return set() if conn is None else {row[0] for row in conn.execute(
            "SELECT DISTINCT task_id FROM feedback_requests WHERE status NOT IN ('applied','superseded')")}
    finally:
        if conn is not None:
            conn.close()


def feedback_status(config):
    if not config.get('feedback'):
        return {'enabled': False, 'counts': {}, 'requests': []}
    conn = _connect(config)
    try:
        return {'enabled': config['feedback']['enabled'],
                'counts': {} if conn is None else dict(conn.execute('SELECT status,count(*) FROM feedback_requests GROUP BY status')),
                'requests': [] if conn is None else [dict(row) for row in conn.execute(
                    'SELECT request_id,task_id,status,attempts,next_attempt,created_at,updated_at,error FROM feedback_requests ORDER BY created_at DESC,rowid DESC LIMIT 100')]}
    finally:
        if conn is not None:
            conn.close()


def retry_feedback(config, request_id):
    request_id = _text(request_id, 'request_id')
    with closing(_connect(config, True)) as conn, conn:
        row = conn.execute('SELECT status FROM feedback_requests WHERE request_id=?', (request_id,)).fetchone()
        if row is None or row['status'] not in {'failed', 'conflict', 'retry'}:
            raise ValueError('feedback_request_not_retryable')
        conn.execute("UPDATE feedback_requests SET status='queued',attempts=0,next_attempt=0,error=NULL,updated_at=? WHERE request_id=?",
                     (time.time(), request_id))
    return {'request_id': request_id, 'status': 'queued'}


def _apply(config, row):
    from . import core
    from processor import load_config, update_task_status, update_task_priority, TaskStatusConflict, UnknownTask
    request = json.loads(row['request'])
    if request['action'] == 'snooze':
        core.sync(config)
        with closing(core._connect(config)) as conn:
            task = conn.execute('SELECT revision FROM tasks WHERE task_id=?', (row['task_id'],)).fetchone()
        if task is None:
            raise UnknownTask('task_not_found')
        if request['expected_revision'] is not None and task['revision'] != request['expected_revision']:
            raise TaskStatusConflict('task_revision_conflict')
        if row['snoozed_until'] <= time.time():
            return {'task_id': row['task_id'], 'status': 'snooze_expired'}
        return core.set_task_state(config, row['task_id'], 'snooze', until=row['snoozed_until'])
    change_priority = request['action'] == 'set_priority'
    update = update_task_priority if change_priority else update_task_status
    value = request['priority'] if change_priority else {'complete': 'completed', 'reopen': 'open', 'cancel': 'cancelled'}[request['action']]
    result = update(load_config(config['feedback']['processor_config']), row['task_id'], value,
        request_id=row['request_id'], actor=request['actor'], expected_revision=request['expected_revision'])
    core.sync(config)
    # A retried historical acknowledgement must never overwrite a newer formal status.
    with InstanceLock(config['storage']['state_file']), closing(core._connect(config, True)) as conn, conn:
        task = conn.execute('SELECT body FROM tasks WHERE task_id=?', (row['task_id'],)).fetchone()
        if task is None:
            raise ValueError('feedback_task_not_imported')
        imported = json.loads(task['body'])
        if imported['revision'] < result['revision']:
            raise RuntimeError('feedback_publication_not_imported')
        completed = int(imported['status'] == 'completed')
        conn.execute('INSERT OR IGNORE INTO local_state(task_id) VALUES(?)', (row['task_id'],))
        conn.execute('UPDATE local_state SET completed=?,snoozed_until=0,version=version+1 WHERE task_id=? AND (completed!=? OR snoozed_until!=0)',
                     (completed, row['task_id'], completed))
    return result


def process_pending(config):
    result = {'applied': 0, 'retry': 0, 'failed': 0, 'conflict': 0, 'pending': 0}
    if not config.get('feedback', {}).get('enabled', False):
        return result
    from processor import TaskStatusBusy, TaskStatusConflict, TaskStatusExportPending, UnknownTask
    with InstanceLock(config['feedback']['state_file']), closing(_connect(config, True)) as conn:
        with conn:
            conn.execute("UPDATE feedback_requests SET status='retry',next_attempt=0,error='interrupted_feedback' WHERE status='applying'")
        now = time.time()
        rows = conn.execute("""SELECT f.rowid AS queue_order,f.* FROM feedback_requests f
            WHERE f.status IN ('queued','retry') AND f.next_attempt<=? AND NOT EXISTS (
              SELECT 1 FROM feedback_requests older WHERE older.task_id=f.task_id AND older.rowid<f.rowid
              AND older.status IN ('queued','retry','applying'))
            ORDER BY f.rowid LIMIT ?""", (now, config['schedule']['max_deliveries_per_round'])).fetchall()
        for row in rows:
            with conn:
                conn.execute("UPDATE feedback_requests SET status='applying',updated_at=? WHERE request_id=?",
                             (time.time(), row['request_id']))
            try:
                outcome = _apply(config, row)
                with conn:
                    conn.execute("UPDATE feedback_requests SET status='applied',result=?,error=NULL,updated_at=? WHERE request_id=?",
                                 (canonical(outcome), time.time(), row['request_id']))
                    conn.execute("UPDATE feedback_requests SET status='superseded',updated_at=? WHERE task_id=? AND rowid<? AND status IN ('failed','conflict')",
                                 (time.time(), row['task_id'], row['queue_order']))
                result['applied'] += 1
            except Exception as exc:
                attempts = row['attempts']
                if isinstance(exc, TaskStatusBusy) or isinstance(exc, RuntimeError) and str(exc) == 'Another collector is using this state file':
                    state, code = 'retry', 'task_status_busy'
                elif isinstance(exc, TaskStatusConflict):
                    state, code = 'conflict', 'task_status_conflict'
                    attempts += 1
                elif isinstance(exc, (UnknownTask, ValueError)):
                    state, code = 'failed', 'task_not_found' if isinstance(exc, UnknownTask) else 'feedback_request_invalid'
                    attempts += 1
                else:
                    attempts += 1
                    state = 'failed' if attempts >= config['feedback']['max_attempts'] else 'retry'
                    code = 'task_status_export_pending' if isinstance(exc, TaskStatusExportPending) else 'feedback_apply_failed'
                delay = min(config['feedback']['retry_base_seconds'] * 2 ** max(0, attempts - 1), 86400)
                with conn:
                    conn.execute('UPDATE feedback_requests SET status=?,attempts=?,next_attempt=?,error=?,updated_at=? WHERE request_id=?',
                                 (state, attempts, time.time() + delay, code, time.time(), row['request_id']))
        counts = dict(conn.execute('SELECT status,count(*) FROM feedback_requests GROUP BY status'))
        result.update(retry=counts.get('retry', 0), failed=counts.get('failed', 0), conflict=counts.get('conflict', 0),
                      pending=sum(counts.get(state, 0) for state in ('queued', 'retry', 'applying')))
    return result
