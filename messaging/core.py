"""Consume published task events; keep local completion and delivery state here."""
from __future__ import annotations

from contextlib import closing
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from common.locking import InstanceLock
from common.codec import canonical, digest
from .notifications import deadline_node as _deadline_node, queue_digest


SCHEMA = '''
CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, size INTEGER, mtime INTEGER, sha256 TEXT);
CREATE TABLE IF NOT EXISTS events(event_id TEXT PRIMARY KEY, body_hash TEXT, task_id TEXT, revision INTEGER, task_hash TEXT);
CREATE INDEX IF NOT EXISTS event_revision ON events(task_id,revision);
CREATE TABLE IF NOT EXISTS tasks(task_id TEXT PRIMARY KEY, revision INTEGER, body TEXT);
CREATE TABLE IF NOT EXISTS local_state(task_id TEXT PRIMARY KEY, completed INTEGER DEFAULT 0, snoozed_until REAL DEFAULT 0, version INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS task_results(task_id TEXT, asset_id TEXT, body TEXT, note TEXT, PRIMARY KEY(task_id,asset_id));
CREATE TABLE IF NOT EXISTS deliveries(
 id TEXT PRIMARY KEY, target_id TEXT, target_hash TEXT, task_id TEXT, revision INTEGER, local_version INTEGER,
 kind TEXT, body TEXT, status TEXT, attempts INTEGER DEFAULT 0, next_attempt REAL DEFAULT 0,
 created_at REAL, finished_at REAL, error TEXT, receipt TEXT, package_hash TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS delivery_task ON deliveries(target_id,task_id,finished_at);
'''
APPLICATION_ID = 0x43434D31


def _connect(config, write=False):
    path = Path(config['storage']['state_file'])
    if not write and not path.exists():
        return None
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
    else:
        conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    owner = conn.execute('PRAGMA application_id').fetchone()[0]
    if owner != APPLICATION_ID and (owner or conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone()):
        conn.close()
        raise ValueError('State file belongs to another application; use a dedicated messaging database')
    if write:
        conn.execute(f'PRAGMA application_id={APPLICATION_ID}')
        conn.executescript(SCHEMA)
        if 'package_hash' not in {row[1] for row in conn.execute('PRAGMA table_info(deliveries)')}:
            conn.execute("ALTER TABLE deliveries ADD COLUMN package_hash TEXT NOT NULL DEFAULT ''")
        columns = {row[1] for row in conn.execute('PRAGMA table_info(deliveries)')}
        for column in ('transport_started_at', 'transport_finished_at'):
            if column not in columns:
                conn.execute(f'ALTER TABLE deliveries ADD COLUMN {column} REAL')
    conn.row_factory = sqlite3.Row
    return conn


def _event(value):
    _json_shape(value)
    if (not isinstance(value, dict) or type(value.get('schema_version')) is not int or value['schema_version'] != 1
            or value.get('operation') != 'upsert'
            or any(not isinstance(value.get(k), str) or not value[k].strip() or len(value[k]) > 2048 for k in ('event_id', 'task_id'))
            or type(value.get('revision')) is not int or not 1 <= value['revision'] < 2 ** 63):
        raise ValueError('Invalid task event envelope')
    task = value.get('task')
    if (not isinstance(task, dict) or task.get('task_id') != value['task_id']
            or type(task.get('revision')) is not int or task['revision'] != value['revision']
            or not isinstance(task.get('priority'), str) or task['priority'] not in {'P0', 'P1', 'P2', 'P3', 'P4'}
            or not isinstance(task.get('status'), str) or task['status'] not in {'open', 'cancelled', 'superseded', 'completed', 'done'}
            or any(not isinstance(task.get(k), str) or not task[k] for k in ('title', 'event'))):
        raise ValueError('Invalid task event payload')
    for name in ('title', 'event', 'priority_reason', 'location', 'updated_at'):
        if name in task and task[name] is not None:
            _string(task[name], name)
    if 'needs_review' in task and type(task['needs_review']) is not bool:
        raise ValueError('Task needs_review must be boolean')
    _strings(task.get('review_reasons', []), 'review_reasons', 100, 20000)
    _strings(task.get('notes', []), 'notes', 100, 20000)
    _strings(task.get('action_steps', []), 'action_steps', 50, 2000)
    if 'time' in task:
        value_time = task['time']
        if not isinstance(value_time, dict):
            raise ValueError('Task time must be an object')
        for key in ('start', 'deadline'):
            if value_time.get(key) is not None:
                _iso_time(value_time[key])
        if value_time.get('text') is not None:
            _string(value_time['text'], 'time.text')
        if 'timezone' in value_time:
            _string(value_time['timezone'], 'time.timezone', 200)
            try:
                ZoneInfo(value_time['timezone'])
            except (ValueError, ZoneInfoNotFoundError):
                raise ValueError('Task timezone must be an IANA timezone') from None
    for person in _objects(task.get('people', []), 'people', 100):
        for key in ('name', 'role'):
            _string(person.get(key), 'people.' + key)
    sources = {}
    for source in _objects(task.get('sources', []), 'sources', 10000):
        key = source.get('message_key')
        _string(key, 'source.message_key', 2048)
        if key in sources:
            raise ValueError('Duplicate source message_key')
        sources[key] = source
        for name in ('text', 'sender', 'collector_module', 'sent_at_local'):
            if source.get(name) is not None:
                _string(source[name], 'source.' + name, 2000000, empty=True)
        for attachment in _objects(source.get('attachments', []), 'source.attachments', 1000):
            _attachment(attachment)
        identity = source.get('identity')
        if identity is not None:
            if not isinstance(identity, dict):
                raise ValueError('Source identity must be an object')
            conversation = identity.get('conversation')
            if conversation is not None:
                if not isinstance(conversation, dict):
                    raise ValueError('Source identity.conversation must be an object')
                for name in ('display_name', 'remark', 'name'):
                    if conversation.get(name) is not None:
                        _string(conversation[name], 'conversation.' + name, empty=True)
    for field in ('attachment_evidence', 'materials'):
        for proof in _objects(task.get(field, []), field, 10000):
            key, index = proof.get('message_key'), proof.get('attachment_index')
            _string(key, field + '.message_key', 2048)
            if key not in sources or type(index) is not int or not 0 <= index < len(sources[key].get('attachments', [])):
                raise ValueError('Task material must reference an existing source attachment')
            if not isinstance(proof.get('attachment'), dict):
                raise ValueError('Task material attachment must be an object')
            _attachment(proof['attachment'])
    return value


def _json_shape(value):
    stack, nodes = [(value, 0)], 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if depth > 32 or nodes > 200000:
            raise ValueError('Task event exceeds JSON structure limits')
        if isinstance(item, dict):
            if len(item) > 200000 or any(not isinstance(key, str) or len(key) > 2048 for key in item):
                raise ValueError('Task event object keys must be bounded strings')
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            if len(item) > 200000:
                raise ValueError('Task event exceeds JSON structure limits')
            stack.extend((child, depth + 1) for child in item)
        elif item is not None and type(item) not in (str, bool, int, float):
            raise ValueError('Task event must contain JSON values only')
        elif isinstance(item, str) and len(item) > 2000000:
            raise ValueError('Task event string is too large')
        elif type(item) is float and not math.isfinite(item):
            raise ValueError('Task event cannot contain nonfinite numbers')
    if len(canonical(value).encode('utf-8')) > 16 * 1024 * 1024:
        raise ValueError('Task event exceeds 16 MiB')


def _string(value, name, limit=20000, *, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()) or len(value) > limit:
        raise ValueError(f'Task {name} must be bounded text')


def _objects(value, name, limit):
    if not isinstance(value, list) or len(value) > limit or any(not isinstance(item, dict) for item in value):
        raise ValueError(f'Task {name} must be an array of at most {limit} objects')
    return value


def _strings(value, name, limit, text_limit):
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError(f'Task {name} must be an array of at most {limit} strings')
    for item in value:
        _string(item, name, text_limit)


def _iso_time(value):
    _string(value, 'time', 100)
    try:
        if len(value) == 10:
            dt.date.fromisoformat(value)
        elif dt.datetime.fromisoformat(value).tzinfo is None:
            raise ValueError('timezone missing')
    except ValueError:
        raise ValueError('Task time must be an ISO date or timestamp with timezone') from None


def _attachment(value):
    for key in ('type', 'name', 'status', 'quality', 'archive_path', 'sha256', 'md5'):
        if value.get(key) is not None:
            _string(value[key], 'attachment.' + key, empty=True)
    if value.get('size_bytes') is not None and (type(value['size_bytes']) is not int or value['size_bytes'] < 0):
        raise ValueError('Attachment size_bytes must be a nonnegative integer')


def _inputs(config, conn):
    tasks = {} if conn is None else {r['task_id']: json.loads(r['body']) for r in conn.execute('SELECT * FROM tasks')}
    known = {} if conn is None else {r['path']: r for r in conn.execute('SELECT * FROM files')}
    imported, versions, files = {}, {}, []
    root = Path(config['input']['batches_dir'])
    for path in sorted(root.glob('*.jsonl')):
        before = path.stat()
        name = str(path.resolve())
        previous = known.get(name)
        if previous and (previous['size'], previous['mtime']) == (before.st_size, before.st_mtime_ns):
            continue
        raw = path.read_bytes()
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise RuntimeError('Task batch changed during import')
        sha = hashlib.sha256(raw).hexdigest()
        if previous and previous['sha256'] != sha:
            raise ValueError('An imported immutable task batch was modified')
        if not previous:
            # JSONL separates records with LF; Unicode line separators belong to JSON strings.
            for line in raw.decode('utf-8').split('\n'):
                if not line.strip():
                    continue
                if len(line) > 16 * 1024 * 1024:
                    raise ValueError('Task event exceeds 16 MiB')
                event = _event(json.loads(line))
                key, identity = event['event_id'], (event['task_id'], event['revision'])
                body_hash, task_hash = digest(event), digest(event['task'])
                persisted = None if conn is None else conn.execute('SELECT * FROM events WHERE event_id=?', (key,)).fetchone()
                if (key in imported and digest(imported[key]) != body_hash) or (persisted and persisted['body_hash'] != body_hash):
                    raise ValueError('Conflicting task event_id')
                old_version = None if conn is None else conn.execute('SELECT task_hash FROM events WHERE task_id=? AND revision=? LIMIT 1', identity).fetchone()
                if ((identity in versions and versions[identity] != task_hash)
                        or (old_version and old_version['task_hash'] != task_hash)):
                    raise ValueError('Conflicting task revision')
                versions[identity] = task_hash
                if not persisted:
                    imported[key] = event
                old = tasks.get(identity[0])
                if old is None or identity[1] > old['revision']:
                    tasks[identity[0]] = event['task']
        files.append((name, after.st_size, after.st_mtime_ns, sha))
    return tasks, imported, files


def _local(conn):
    return {} if conn is None else {r['task_id']: dict(r) for r in conn.execute('SELECT * FROM local_state')}


def _effective(task, local):
    result = dict(task)
    if local.get('completed') and result['status'] == 'open':
        result['status'] = 'completed'
    return result


def _package(task, config, prepare=False):
    from .package import build_package
    return build_package(task, config, prepare=prepare)


def _results(conn):
    results = {}
    if conn is not None:
        for row in conn.execute('SELECT * FROM task_results ORDER BY rowid'):
            results.setdefault(row['task_id'], []).append(dict(json.loads(row['body']), role='result', note=row['note']))
    return results


def _with_results(package, resources):
    result = dict(package)
    if resources:
        merged = {r['id']: dict(r) for r in result.get('resources', [])}
        for resource in resources:
            merged.setdefault(resource['id'], {}).update(resource)
        result['resources'] = list(merged.values())
    return result


def task_package(config, task_id):
    conn = _connect(config)
    try:
        tasks, _, _ = _inputs(config, conn)
        if task_id not in tasks:
            raise ValueError('Task not found')
        task = _effective(tasks[task_id], _local(conn).get(task_id, {}))
        return _with_results(_package(task, config), _results(conn).get(task_id, []))
    finally:
        if conn is not None:
            conn.close()


def preview(config, limit=20):
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError('limit must be between 1 and 200')
    conn = _connect(config)
    try:
        tasks, events, _ = _inputs(config, conn)
        local = _local(conn)
        results = _results(conn)
        selected = sorted(tasks.values(), key=lambda t: (t['priority'], t['task_id']))[:limit]
        return {'enabled': config['enabled'], 'pending_events': len(events), 'total_tasks': len(tasks),
                'tasks': [_with_results(_package(_effective(t, local.get(t['task_id'], {})), config), results.get(t['task_id'], [])) for t in selected],
                'has_more': len(tasks) > limit, 'local_state': local}
    finally:
        if conn is not None:
            conn.close()


def _sync(config, conn):
    tasks, events, files = _inputs(config, conn)
    with conn:
        conn.executemany('INSERT INTO files VALUES(?,?,?,?) ON CONFLICT(path) DO UPDATE SET size=excluded.size,mtime=excluded.mtime,sha256=excluded.sha256', files)
        for event in events.values():
            conn.execute('INSERT INTO events VALUES(?,?,?,?,?)', (event['event_id'], digest(event), event['task_id'], event['revision'], digest(event['task'])))
            conn.execute('INSERT INTO tasks VALUES(?,?,?) ON CONFLICT(task_id) DO UPDATE SET revision=excluded.revision,body=excluded.body WHERE excluded.revision>tasks.revision',
                         (event['task_id'], event['revision'], canonical(event['task'])))
    local = _local(conn)
    results = _results(conn)
    packages = {key: _with_results(_package(_effective(task, local.get(key, {})), config, True), results.get(key, [])) for key, task in tasks.items()}
    return tasks, local, packages, len(events)


def sync(config):
    with InstanceLock(config['storage']['state_file']), closing(_connect(config, True)) as conn:
        tasks, _, packages, imported = _sync(config, conn)
        return {'imported_events': imported, 'tasks': len(tasks), 'prepared_tasks': len(packages), 'delivery_attempts': 0,
                'missing_resources': sum(r.get('status') != 'ready' for p in packages.values() for r in p.get('resources', []))}


def _target_hash(target):
    return digest({k: v for k, v in target.items() if k not in {'enabled', 'name'}})


def _package_hash(package):
    def portable(value):
        if isinstance(value, dict):
            return {k: portable(v) for k, v in value.items() if k not in {'local_path', 'archive_path', 'source_path'}}
        if isinstance(value, list):
            return [portable(v) for v in value]
        return value
    return digest(portable(package))


def _queue(config, conn, tasks, local, packages, now):
    from .feedback import blocked_task_ids
    blocked = blocked_task_ids(config)
    for target in config['targets']:
        if not target['enabled']:
            continue
        fingerprint = _target_hash(target)
        sync_only = target['kind'] == 'notion' or target.get('mode') == 'sync'
        for key, task in tasks.items():
            if key in blocked:
                continue
            state = local.get(key, {})
            version = state.get('version', 0)
            package_hash = _package_hash(packages[key])
            deadline_mode, node = _deadline_node(config, task, now)
            # Unknown remote outcomes require reconciliation before any follow-up send.
            if conn.execute("SELECT 1 FROM deliveries WHERE target_id=? AND task_id=? AND status='unknown'", (target['id'], key)).fetchone():
                continue
            with conn:
                conn.execute("UPDATE deliveries SET status='obsolete' WHERE target_id=? AND task_id=? AND status IN ('pending','retry','failed') AND (target_hash!=? OR revision!=? OR local_version!=? OR package_hash!=?)",
                             (target['id'], key, fingerprint, task['revision'], version, package_hash))
                # A later node supersedes unsent earlier reminders, including their retries.
                if deadline_mode:
                    conn.execute("UPDATE deliveries SET status='obsolete' WHERE target_id=? AND task_id=? AND kind='reminder' AND status IN ('pending','retry','failed') AND json_extract(body,'$.reminder.due_at') IS NOT ?",
                                 (target['id'], key, node['due_at'] if node else None))
                else:
                    conn.execute("UPDATE deliveries SET status='obsolete' WHERE target_id=? AND task_id=? AND kind='reminder' AND status IN ('pending','retry','failed') AND json_extract(body,'$.reminder.due_at') IS NOT NULL",
                                 (target['id'], key))
            last = conn.execute("SELECT * FROM deliveries WHERE target_id=? AND target_hash=? AND task_id=? AND status IN ('sent','deferred') ORDER BY finished_at DESC,rowid DESC LIMIT 1",
                                (target['id'], fingerprint, key)).fetchone()
            current = last and last['revision'] == task['revision'] and last['local_version'] == version and last['package_hash'] == package_hash
            initial = config.get('initial_notifications', {}).get(task['priority'], True)
            if last and last['status'] == 'deferred' and initial:
                current = False
            kind, anchor = 'upsert', ''
            if current:
                interval = config['reminders'][task['priority']]
                if (sync_only or _effective(task, state)['status'] != 'open'
                        or state.get('snoozed_until', 0) > now):
                    continue
                if deadline_mode:
                    if node is None or last['finished_at'] >= node['due_at']:
                        continue
                    anchor = digest(node)
                else:
                    if not interval or now < last['finished_at'] + interval:
                        continue
                    anchor = last['id']
                kind = 'reminder'
            elif state.get('snoozed_until', 0) > now and not sync_only:
                continue
            elif not last and _effective(task, state)['status'] != 'open' and not sync_only:
                continue
            elif last and last['revision'] == task['revision'] and last['local_version'] == version:
                anchor = last['id']
            identifier = digest([target['id'], fingerprint, key, task['revision'], version, package_hash, kind, anchor])
            envelope = {'schema_version': 1, 'delivery_id': identifier, 'kind': kind, 'task': packages[key]}
            if kind == 'reminder' and deadline_mode:
                envelope['reminder'] = node
            deferred = (not sync_only and not initial and kind == 'upsert' and (not last or last['status'] == 'deferred'))
            with conn:
                conn.execute('INSERT OR IGNORE INTO deliveries(id,target_id,target_hash,task_id,revision,local_version,kind,body,status,created_at,package_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                             (identifier, target['id'], fingerprint, key, task['revision'], version, kind, canonical(envelope), 'pending', now, package_hash))
                if deferred:
                    conn.execute("UPDATE deliveries SET status='deferred',finished_at=? WHERE id=? AND status IN ('pending','retry','failed')", (now, identifier))


def run_round(config, *, now=None):
    live_clock = now is None
    now = time.time() if now is None else float(now)
    with InstanceLock(config['storage']['state_file']), closing(_connect(config, True)) as conn:
        tasks, local, packages, imported = _sync(config, conn)
        with conn:
            conn.execute("UPDATE deliveries SET status='unknown',error='interrupted_delivery' WHERE status='sending'")
        result = {'imported_events': imported, 'tasks': len(tasks), 'sent': 0, 'retry': 0, 'unknown': 0, 'failed': 0, 'enabled': config['enabled']}
        if not config['enabled']:
            return result
        from .channels import DeliveryError, deliver
        from .feedback import blocked_task_ids
        _queue(config, conn, tasks, local, packages, now)
        queue_digest(config, conn, {key: _effective(task, local.get(key, {})) for key, task in tasks.items()},
                     local, now, blocked_task_ids(config), _target_hash)
        targets = {t['id']: t for t in config['targets'] if t['enabled']}
        rows = conn.execute("SELECT * FROM deliveries WHERE status IN ('pending','retry') AND next_attempt<=? ORDER BY created_at,rowid", (now,)).fetchall()
        attempted = 0
        for row in rows:
            if row['task_id'] in blocked_task_ids(config):
                continue
            target = targets.get(row['target_id'])
            if target is None or _target_hash(target) != row['target_hash']:
                continue
            if attempted >= config['schedule']['max_deliveries_per_round']:
                break
            attempted += 1
            previous = conn.execute("SELECT receipt FROM deliveries WHERE target_id=? AND target_hash=? AND task_id=? AND status='sent' AND receipt IS NOT NULL ORDER BY finished_at DESC,rowid DESC LIMIT 1",
                                    (row['target_id'], row['target_hash'], row['task_id'])).fetchone()
            attempts = row['attempts'] + 1
            with conn:
                conn.execute("UPDATE deliveries SET status='sending',attempts=?,transport_started_at=?,transport_finished_at=NULL WHERE id=?", (attempts, time.time(), row['id']))
            try:
                receipt = deliver(target, json.loads(row['body']), config, previous=json.loads(previous['receipt']) if previous else None)
                if not isinstance(receipt, dict):
                    raise ValueError('Invalid delivery receipt')
                encoded = canonical(receipt)
                with conn:
                    finished = time.time()
                    conn.execute("UPDATE deliveries SET status='sent',finished_at=?,transport_finished_at=?,receipt=?,error=NULL WHERE id=?", (finished if live_clock else now, finished, encoded, row['id']))
                result['sent'] += 1
            except Exception as exc:
                uncertain = not isinstance(exc, DeliveryError) or exc.uncertain
                delivery_status = 'unknown' if uncertain else ('failed' if attempts >= config['schedule']['max_attempts'] else 'retry')
                error = exc.code if isinstance(exc, DeliveryError) else 'delivery_outcome_unknown'
                delay = min(config['schedule']['retry_base_seconds'] * 2 ** (attempts - 1), 86400)
                partial = canonical(exc.receipt) if isinstance(exc, DeliveryError) and isinstance(exc.receipt, dict) else None
                with conn:
                    finished = time.time()
                    conn.execute('UPDATE deliveries SET status=?,error=?,next_attempt=?,transport_finished_at=?,receipt=COALESCE(?,receipt) WHERE id=?',
                                 (delivery_status, error, (finished if live_clock else now) + delay, finished, partial, row['id']))
                result[delivery_status] += 1
        return result


def status(config):
    conn = _connect(config)
    try:
        return {'enabled': config['enabled'],
                'targets': [{k: t[k] for k in ('id', 'kind', 'enabled')} for t in config['targets']],
                'reminders': config['reminders'],
                'deadline_reminders': config.get('deadline_reminders', {}),
                'initial_notifications': config.get('initial_notifications', {}),
                'daily_digest': config.get('daily_digest', {}),
                'tasks': 0 if conn is None else conn.execute('SELECT count(*) FROM tasks').fetchone()[0],
                'delivery_counts': {} if conn is None else dict(conn.execute('SELECT status,count(*) FROM deliveries GROUP BY status')),
                'deliveries': [] if conn is None else [dict(r) for r in conn.execute('SELECT id,target_id,task_id,kind,status,attempts,next_attempt,error FROM deliveries ORDER BY created_at DESC,rowid DESC LIMIT 100')],
                'local_state': _local(conn)}
    finally:
        if conn is not None:
            conn.close()


def set_task_state(config, task_id, action, *, until=None):
    if action not in {'complete', 'reopen', 'snooze'}:
        raise ValueError('action must be complete, reopen or snooze')
    if action == 'snooze':
        if isinstance(until, str):
            parsed = dt.datetime.fromisoformat(until)
            if parsed.tzinfo is None:
                raise ValueError('Snooze time needs an explicit timezone')
            until = parsed.timestamp()
        if type(until) not in (int, float) or not math.isfinite(until) or until <= time.time():
            raise ValueError('Snooze time must be in the future')
    with InstanceLock(config['storage']['state_file']), closing(_connect(config, True)) as conn:
        task = conn.execute('SELECT body FROM tasks WHERE task_id=?', (task_id,)).fetchone()
        if not task:
            raise ValueError('Task is not imported; sync first')
        if action == 'reopen' and json.loads(task['body'])['status'] != 'open':
            raise ValueError('Cannot reopen an upstream closed task; update it in processing first')
        with conn:
            conn.execute('INSERT OR IGNORE INTO local_state(task_id) VALUES(?)', (task_id,))
            current = dict(conn.execute('SELECT * FROM local_state WHERE task_id=?', (task_id,)).fetchone())
            if action == 'snooze':
                if current['snoozed_until'] != until:
                    conn.execute('UPDATE local_state SET snoozed_until=?,version=version+1 WHERE task_id=?', (until, task_id))
            else:
                if current['completed'] != int(action == 'complete') or current['snoozed_until']:
                    conn.execute('UPDATE local_state SET completed=?,snoozed_until=0,version=version+1 WHERE task_id=?', (int(action == 'complete'), task_id))
        return dict(conn.execute('SELECT * FROM local_state WHERE task_id=?', (task_id,)).fetchone())


def add_result(config, task_id, file_path, *, note=''):
    if not isinstance(note, str) or len(note) > 20000:
        raise ValueError('Result note must be at most 20000 characters')
    from .package import archive_result
    with InstanceLock(config['storage']['state_file']), closing(_connect(config, True)) as conn:
        if not conn.execute('SELECT 1 FROM tasks WHERE task_id=?', (task_id,)).fetchone():
            raise ValueError('Task is not imported; sync first')
        resource = archive_result(file_path, config)
        encoded = canonical(resource)
        old = conn.execute('SELECT body,note FROM task_results WHERE task_id=? AND asset_id=?', (task_id, resource['id'])).fetchone()
        if not old or (old['body'], old['note']) != (encoded, note):
            with conn:
                conn.execute('INSERT INTO task_results VALUES(?,?,?,?) ON CONFLICT(task_id,asset_id) DO UPDATE SET body=excluded.body,note=excluded.note',
                             (task_id, resource['id'], encoded, note))
                conn.execute('INSERT INTO local_state(task_id,version) VALUES(?,1) ON CONFLICT(task_id) DO UPDATE SET version=version+1', (task_id,))
        return dict(resource, role='result', note=note)


def resolve_delivery(config, delivery_id, action):
    if action not in {'retry', 'delivered'}:
        raise ValueError('action must be retry or delivered')
    with InstanceLock(config['storage']['state_file']), closing(_connect(config, True)) as conn:
        row = conn.execute('SELECT * FROM deliveries WHERE id=?', (delivery_id,)).fetchone()
        if row is None or row['status'] not in {'unknown', 'failed'}:
            raise ValueError('Only unknown or failed deliveries can be reconciled')
        target = next((t for t in config['targets'] if t['id'] == row['target_id']), {})
        if action == 'delivered' and target.get('kind') == 'notion':
            raise ValueError('Notion requires a page receipt; retry lets the adapter reconcile by task ID')
        receipt = None
        if target.get('kind') == 'feishu_app':
            from .feishu import reconcile_delivery
            receipt = reconcile_delivery(target, json.loads(row['body']), config, action)
        with conn:
            conn.execute('UPDATE deliveries SET status=?,next_attempt=0,error=NULL,attempts=0,finished_at=?,receipt=COALESCE(?,receipt) WHERE id=?',
                         ('pending' if action == 'retry' else 'sent', time.time() if action == 'delivered' else None,
                          canonical(receipt) if receipt is not None else None, delivery_id))
        return {'delivery_id': delivery_id, 'status': 'pending' if action == 'retry' else 'sent'}
