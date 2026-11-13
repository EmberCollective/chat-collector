"""Explicit task-state feedback; model extraction never marks a task completed."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from common.locking import InstanceLock
from common.codec import canonical
from . import core


class TaskStatusConflict(ValueError):
    pass


class UnknownTask(ValueError):
    pass


class TaskStatusBusy(RuntimeError):
    pass


class TaskStatusExportPending(RuntimeError):
    """The state change is durable; retry the same request to finish publishing it."""


def _text(value, field, maximum=200):
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError(f'invalid_{field}')
    return value.strip()


def update_task_status(config, task_id, status, *, request_id, actor, expected_revision=None):
    return _update_task(config, task_id, 'status', status, request_id=request_id,
                        actor=actor, expected_revision=expected_revision)


def update_task_priority(config, task_id, priority, *, request_id, actor, expected_revision=None):
    """Persist an explicit human priority using the same audited publication path."""
    return _update_task(config, task_id, 'priority', priority, request_id=request_id,
                        actor=actor, expected_revision=expected_revision)


def _update_task(config, task_id, field, value, *, request_id, actor, expected_revision=None):
    task_id = _text(task_id, 'task_id')
    request_id = _text(request_id, 'request_id')
    choices = {'completed', 'open', 'cancelled'} if field == 'status' else {'P0', 'P1', 'P2', 'P3', 'P4'}
    if not isinstance(value, str) or value not in choices:
        raise ValueError('invalid_task_' + field)
    if not isinstance(actor, dict) or set(actor) != {'platform', 'user_id', 'source'}:
        raise ValueError('invalid_task_status_actor')
    actor = {key: _text(actor[key], 'actor_' + key, 64 if key == 'platform' else 200) for key in ('platform', 'user_id', 'source')}
    if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 1):
        raise ValueError('invalid_expected_revision')
    request = canonical({'task_id': task_id, field: value, 'actor': actor, 'expected_revision': expected_revision})
    if not Path(config['storage']['state_file']).is_file():
        raise UnknownTask('task_not_found')
    lock = InstanceLock(config['storage']['state_file'])
    try:
        lock.__enter__()
    except RuntimeError:
        raise TaskStatusBusy('task_status_busy') from None
    try:
        conn = core._connect(config, write=True)
        try:
            with conn:
                prior = conn.execute('SELECT request,result FROM task_status_requests WHERE request_id=?', (request_id,)).fetchone()
                if prior:
                    if prior[0] != request:
                        raise TaskStatusConflict('request_id_content_conflict')
                    result = json.loads(prior[1])
                else:
                    row = conn.execute('SELECT body FROM tasks WHERE task_id=?', (task_id,)).fetchone()
                    if row is None:
                        raise UnknownTask('task_not_found')
                    task = json.loads(row[0])
                    if expected_revision is not None and expected_revision != task['revision']:
                        raise TaskStatusConflict('task_revision_conflict')
                    if task['status'] == 'superseded':
                        raise TaskStatusConflict('task_superseded')
                    previous_value = task[field]
                    changed = previous_value != value or task.get(field + '_control', {}).get(field) != value
                    now = dt.datetime.now(dt.timezone.utc).isoformat()
                    if changed:
                        task.update({field: value, 'updated_at': now,
                                     field + '_control': {field: value, 'request_id': request_id, 'actor': actor, 'at': now}})
                        if field == 'priority':
                            task['priority_reason'] = '用户手动设置为 ' + value
                        core._save_task(conn, task)
                    result = {'request_id': request_id, 'task_id': task_id, field: task[field],
                              'revision': task['revision'], 'previous_' + field: previous_value,
                              'changed': changed, 'actor': actor, 'recorded_at': now}
                    conn.execute('INSERT INTO task_status_requests VALUES(?,?,?,?)', (request_id, request, canonical(result), now))
            try:
                core._export(config, conn)
            except Exception:
                raise TaskStatusExportPending('task_status_export_pending') from None
            return result
        finally:
            conn.close()
    finally:
        lock.__exit__()
