"""Public task API; this module alone owns task writes and human controls."""
from __future__ import annotations

import datetime as dt
import json
from contextlib import contextmanager
from pathlib import Path

from common.locking import InstanceLock
from common.codec import canonical, digest
from . import storage
from .task_search import search_tasks, TaskLookup
from .task_validation import _validate_response


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
    actor = _actor(actor)
    if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 1):
        raise ValueError('invalid_expected_revision')
    request = canonical({'task_id': task_id, field: value, 'actor': actor, 'expected_revision': expected_revision})
    if not Path(config['storage']['state_file']).is_file():
        raise UnknownTask('task_not_found')
    def apply(conn):
        task = _get_task(conn, task_id)
        if expected_revision is not None and expected_revision != task['revision']:
            raise TaskStatusConflict('task_revision_conflict')
        if task['status'] == 'superseded':
            raise TaskStatusConflict('task_superseded')
        previous_value = task[field]
        changed = previous_value != value or task.get(field + '_control', {}).get(field) != value
        now = _now()
        if changed:
            task.update({field: value, 'updated_at': now,
                         field + '_control': {field: value, 'request_id': request_id, 'actor': actor, 'at': now}})
            if field == 'priority':
                task['priority_reason'] = '用户手动设置为 ' + value
            storage._save_task(conn, task)
        return {'request_id': request_id, 'task_id': task_id, field: task[field],
                'revision': task['revision'], 'previous_' + field: previous_value,
                'changed': changed, 'actor': actor, 'recorded_at': now}
    return _write(config, request_id, request, apply)[0]


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _actor(actor):
    # Authentication belongs to the invoking channel; only its verified identity enters here.
    if not isinstance(actor, dict) or set(actor) != {'platform', 'user_id', 'source'}:
        raise ValueError('invalid_task_status_actor')
    return {key: _text(actor[key], 'actor_' + key, 64 if key == 'platform' else 200)
            for key in ('platform', 'user_id', 'source')}


@contextmanager
def _task_connection(config):
    lock = InstanceLock(config['storage']['state_file'])
    try:
        lock.__enter__()
    except RuntimeError:
        raise TaskStatusBusy('task_status_busy') from None
    try:
        conn = storage._connect(config, write=True)
        try:
            yield conn
        finally:
            conn.close()
    finally:
        lock.__exit__()


def _notify(config):
    try:
        from automation.wakeup import notify
        if config.get('_config_path'):
            notify(config['_config_path'], 'messaging')
    except Exception:
        pass  # The durable outbox is the recovery path if a wakeup is lost.


def _write(config, request_id, request, apply, *, export_pending=True):
    committed = False
    try:
        with _task_connection(config) as conn:
            with conn:
                prior = conn.execute('SELECT request,result FROM task_status_requests WHERE request_id=?', (request_id,)).fetchone()
                if prior:
                    if prior[0] != request:
                        raise TaskStatusConflict('request_id_content_conflict')
                    result = json.loads(prior[1])
                else:
                    result = apply(conn)
                    conn.execute('INSERT INTO task_status_requests VALUES(?,?,?,?)',
                                 (request_id, request, canonical(result), _now()))
            committed = True
            try:
                batches = storage._export(config, conn)
            except Exception:
                if export_pending:
                    raise TaskStatusExportPending('task_status_export_pending') from None
                raise
        return result, batches
    finally:
        if committed:
            _notify(config)


def _get_task(conn, task_id):
    row = conn.execute('SELECT body FROM tasks WHERE task_id=?', (task_id,)).fetchone()
    if row is None:
        raise UnknownTask('task_not_found')
    return json.loads(row[0])


def get_task(config, task_id):
    task_id = _text(task_id, 'task_id')
    conn = storage._connect(config)
    if conn is None:
        raise UnknownTask('task_not_found')
    try:
        task = _get_task(conn, task_id)
        for item in task.get('materials', []):
            item['material_id'] = _material_id(task_id, item)
        return task
    finally:
        conn.close()


def _material_id(task_id, item):
    return digest([task_id, item['message_key'], item['attachment_index']])


def read_changes(config, cursor=None, limit=100):
    """Replay immutable outbox snapshots, including changes awaiting JSONL export."""
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError('invalid_change_limit')
    if cursor is not None and (not isinstance(cursor, dict) or set(cursor) != {'version', 'seq'}
            or type(cursor['version']) is not int or cursor['version'] != 1
            or type(cursor['seq']) is not int or cursor['seq'] < 0):
        raise ValueError('invalid_change_cursor')
    seq = cursor['seq'] if cursor else 0
    conn = storage._connect(config)
    try:
        rows = [] if conn is None else conn.execute(
            'SELECT seq,body FROM outbox WHERE seq>? ORDER BY seq LIMIT ?', (seq, limit + 1)).fetchall()
        page = rows[:limit]
        return {'events': [json.loads(row[1]) for row in page],
                'next_cursor': {'version': 1, 'seq': page[-1][0] if page else seq}, 'has_more': len(rows) > limit}
    finally:
        if conn is not None:
            conn.close()


def get_material(config, task_id, material_id):
    material_id = _text(material_id, 'material_id')
    task = get_task(config, task_id)
    item = next((item for item in task.get('materials', []) if item['material_id'] == material_id), None)
    if item is None:
        raise ValueError('task_material_not_found')
    source = next((source for source in task.get('sources', []) if source['message_key'] == item['message_key']), None)
    if source is None or not 0 <= item['attachment_index'] < len(source.get('attachments', [])):
        raise ValueError('task_material_source_missing')
    from providers.content import export_material
    event = {'message_key': source['message_key'],
             'message': dict(source, platform=source.get('platform') or source['collector_module'])}
    return export_material(item['attachment'], config, event=event, attachment_index=item['attachment_index'])


def create_tasks(config, tasks, *, request_id, actor, origin):
    """Create explicit manual/channel tasks; chat evidence is reserved for extraction."""
    request_id = _text(request_id, 'request_id')
    actor = _actor(actor)
    if (not isinstance(origin, dict) or set(origin) != {'kind', 'platform', 'account_id', 'conversation_id'}
            or origin.get('kind') not in {'manual', 'channel'}):
        raise ValueError('invalid_task_origin')
    origin = {key: _text(value, 'origin_' + key) for key, value in origin.items()}
    if origin['platform'] != actor['platform']:
        raise ValueError('task_origin_actor_platform_conflict')
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= 100:
        raise ValueError('create_tasks_requires_1_to_100_tasks')
    mapped = []
    for task in tasks:
        if not isinstance(task, dict) or any(task.get(key) for key in (
                'task_id', 'sources', 'source_message_keys', 'attachment_evidence', 'materials', 'material_refs')):
            raise ValueError('external_task_cannot_claim_chat_evidence')
        mapped.append(dict(task, task_id=None, source_message_keys=['request']))
    # Reuse the mapped submission validator; this marker is removed before persistence.
    validated = _validate_response({'tasks': mapped}, {'messages': [{'message_key': 'request'}],
        'primary_message_keys': ['request']}, [], config['context']['timezone'])
    request = canonical({'operation': 'create_tasks', 'tasks': tasks, 'actor': actor, 'origin': origin})
    def apply(conn):
        created, now = [], _now()
        for index, task in enumerate(validated):
            task.update(task_id=digest(['create_tasks', request_id, index]), schema_version=1, updated_at=now,
                        conversation={key: origin[key] for key in ('platform', 'account_id', 'conversation_id')},
                        origin=dict(origin, actor=actor, request_id=request_id, recorded_at=now),
                        source_message_keys=[], sources=[], context={'origin': origin['kind']})
            storage._save_task(conn, task)
            created.append(task)
        return {'request_id': request_id, 'tasks': created}
    return _write(config, request_id, request, apply)[0]
