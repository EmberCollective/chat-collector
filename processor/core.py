from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tomllib
import urllib.parse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from common.locking import InstanceLock
from common.codec import canonical, digest
from contracts import validate_event as _validate_event


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
'''


def load_config(path):
    path = Path(path).resolve()
    with path.open('rb') as stream:
        config = tomllib.load(stream)
    for section, key in [('input', 'batches_dir'), ('storage', 'state_file'),
                         ('storage', 'output_dir'), ('rules', 'file')]:
        value = config.get(section, {}).get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f'{section}.{key} is required')
        config[section][key] = str((path.parent / value).resolve())
    model = config.setdefault('model', {})
    for key, default in [('base_url', ''), ('model', ''), ('api_key_env', 'CHAT_PROCESSOR_API_KEY'), ('api_key_file', ''), ('env_file', '../.env')]:
        value = model.setdefault(key, default)
        if not isinstance(value, str):
            raise ValueError(f'model.{key} must be text')
    if model['api_key_file']:
        model['api_key_file'] = str((path.parent / model['api_key_file']).resolve())
    if model['env_file']:
        model['env_file'] = str((path.parent / model['env_file']).resolve())
    if type(model.setdefault('vision', False)) is not bool:
        raise ValueError('model.vision must be a boolean')
    start_at = config['input'].setdefault('start_at', 0)
    if type(config['input'].setdefault('refresh_collectors', False)) is not bool:
        raise ValueError('input.refresh_collectors must be a boolean')
    if type(start_at) is not int or start_at < 0:
        raise ValueError('input.start_at must be a nonnegative Unix timestamp')
    if model['base_url']:
        url = urllib.parse.urlsplit(model['base_url'])
        if url.scheme not in {'http', 'https'} or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError('model.base_url must be an HTTP(S) API root without credentials, query or fragment')
    for section, key, default, minimum, maximum in [
        ('model', 'timeout_seconds', 120, 1, 600),
        ('model', 'context_window', 32768, 4096, 2000000),
        ('model', 'max_output_tokens', 4096, 256, 2000000),
        ('context', 'gap_seconds', 300, 0, 86400),
        ('context', 'max_messages', 40, 1, 500),
        ('context', 'history_messages', 5, 0, 100),
        ('context', 'max_conversation_chars', 20000, 1000, 1000000),
        ('processing', 'max_groups_per_round', 10, 1, 1000),
        ('processing', 'max_input_chars', 60000, 1000, 1000000),
        ('schedule', 'interval_seconds', 60, 1, 86400),
        ('agent', 'max_turns', 8, 1, 100),
        ('agent', 'max_tool_calls', 12, 1, 1000),
        ('agent', 'compaction_reserve_tokens', 8192, 256, 2000000),
        ('agent', 'compaction_keep_recent_tokens', 4096, 128, 2000000),
        ('agent', 'max_retries', 2, 0, 5),
    ]:
        value = config.setdefault(section, {}).setdefault(key, default)
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f'{section}.{key} must be an integer in [{minimum}, {maximum}]')
    grouping = config['context'].setdefault('grouping', 'sql')
    if grouping not in {'sql', 'time'}:
        raise ValueError('context.grouping must be sql or time')
    agent = config['agent']
    if agent.setdefault('batching', 'auto') not in {'auto', 'single_conversation'}:
        raise ValueError('agent.batching must be auto or single_conversation')
    if type(agent.setdefault('auto_compaction', True)) is not bool:
        raise ValueError('agent.auto_compaction must be a boolean')
    if model['max_output_tokens'] > model['context_window'] or agent['compaction_reserve_tokens'] >= model['context_window'] or agent['compaction_keep_recent_tokens'] >= model['context_window'] - agent['compaction_reserve_tokens']:
        raise ValueError('Model output and Pi compaction budgets must fit model.context_window')
    attachments = config.setdefault('attachments', {})
    collectors = attachments.setdefault('collector_configs', {})
    if not isinstance(collectors, dict) or set(collectors) - {'qq', 'wechat'} or any(not isinstance(p, str) or not p for p in collectors.values()):
        raise ValueError('attachments.collector_configs must map qq/wechat to local configuration files')
    attachments['collector_configs'] = {k: str((path.parent / p).resolve()) for k, p in collectors.items()}
    from providers.registry import configure_providers
    configure_providers(attachments, path.parent)
    roots = attachments.setdefault('archive_roots', ['../data/assets'])
    if not isinstance(roots, list) or any(not isinstance(p, str) or not p for p in roots):
        raise ValueError('attachments.archive_roots must be an array of paths')
    attachments['archive_roots'] = [str((path.parent / p).resolve()) for p in roots]
    cache = attachments.setdefault('cache_dir', '../data/processor/attachment-cache')
    if not isinstance(cache, str) or not cache:
        raise ValueError('attachments.cache_dir must be a path')
    attachments['cache_dir'] = str((path.parent / cache).resolve())
    if any(Path(root) == Path(attachments['cache_dir']) or Path(root) in Path(attachments['cache_dir']).parents for root in attachments['archive_roots']):
        raise ValueError('Attachment cache must be outside original archives')
    timezone = config['context'].setdefault('timezone', 'Asia/Shanghai')
    try:
        ZoneInfo(timezone)
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        raise ValueError('context.timezone must be a valid IANA timezone') from None
    descriptions = config.setdefault('source_descriptions', {})
    if not isinstance(descriptions, dict) or any(not isinstance(v, str) for v in descriptions.values()):
        raise ValueError('source_descriptions must contain text descriptions')
    incoming = Path(config['input']['batches_dir'])
    output = Path(config['storage']['output_dir'])
    state = Path(config['storage']['state_file'])
    protected = [incoming, Path(config['rules']['file'])]
    if model['api_key_file']:
        protected.append(Path(model['api_key_file']))
    if model.get('env_file'):
        protected.append(Path(model['env_file']))
    protected.append(path)
    if output == incoming or incoming in output.parents or output in incoming.parents:
        raise ValueError('Processor output must be separate from collector batches')
    if any(state == p or p in state.parents for p in protected) or state == output or output in state.parents:
        raise ValueError('Processor state must be separate from input, rules, credentials and output')
    config['_config_path'] = str(path)
    return config


def _rules(config):
    path = Path(config['rules']['file'])
    text = path.read_text(encoding='utf-8')
    if not text.strip():
        raise ValueError('The Markdown processing rules are empty')
    return {'path': str(path), 'sha256': hashlib.sha256(text.encode()).hexdigest(), 'text': text}


def _api_key(config):
    model = config['model']
    key = os.environ.get(model['api_key_env'], '').strip() if model['api_key_env'] else ''
    if not key and model['api_key_env'] and model.get('env_file'):
        from dotenv import dotenv_values
        env_path = Path(model['env_file'])
        if env_path.is_file():
            # Read only the configured key; never change the parent process environment.
            key = (dotenv_values(env_path, interpolate=False).get(model['api_key_env']) or '').strip()
    if not key and model['api_key_file'] and Path(model['api_key_file']).is_file():
        with Path(model['api_key_file']).open(encoding='utf-8') as stream:
            key = stream.read(16385).strip()
        if len(key) > 16384:
            raise ValueError('Model credential file is unexpectedly large')
    if key and (not key.isascii() or any(ord(char) < 33 or ord(char) == 127 for char in key)):
        raise ValueError('Model API key must be a single ASCII token; check the local credential setting')
    return key


def _model_info(config):
    model = config['model']
    credential = bool(_api_key(config))
    auth_optional = not model['api_key_env'] and not model['api_key_file']
    return {'configured': bool(model['base_url'] and model['model'] and (credential or auth_optional)),
            'base_url': model['base_url'], 'model': model['model'],
            'vision': model['vision'], 'context_window': model['context_window'],
            'max_output_tokens': model['max_output_tokens'],
            'api_key_env': model['api_key_env'], 'api_key_file': model['api_key_file']}


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


def _read_inputs(config, conn):
    """Read only new immutable batches; retain latest message revisions across batches."""
    messages = {} if conn is None else {key: json.loads(body) for key, body in conn.execute('SELECT message_key,body FROM messages')}
    known = {} if conn is None else {row[0]: row[1:] for row in conn.execute('SELECT path,size,mtime,sha256 FROM files')}
    files, events = [], {}
    root = Path(config['input']['batches_dir'])
    if not root.is_dir():
        raise ValueError('Collector batches directory does not exist')
    for path in sorted(root.glob('*.jsonl')):
        before = path.stat()
        name = str(path.resolve())
        prior = known.get(name)
        if prior and prior[:2] == (before.st_size, before.st_mtime_ns):
            continue
        raw = path.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise RuntimeError('Collector batch changed while being read')
        if prior:
            if prior[2] != sha:
                raise ValueError('An imported immutable collector batch was modified: ' + path.name)
        else:
            # JSONL separates records with LF; Unicode line separators belong to JSON strings.
            for line in raw.decode('utf-8').split('\n'):
                if not line.strip():
                    continue
                event = _validate_event(json.loads(line))
                key = event['event_id']
                hashed = digest(event)
                previous = events.get(key)
                persisted = None if conn is None else conn.execute('SELECT body_hash FROM events WHERE event_id=?', (key,)).fetchone()
                if (previous and digest(previous) != hashed) or (persisted and persisted[0] != hashed):
                    raise ValueError('Conflicting content for the same collector event_id')
                if not persisted:
                    events[key] = event
                old = messages.get(event['message_key'])
                if old and old['revision'] == event['revision'] and old != event:
                    raise ValueError('Conflicting content for the same message revision')
                if old is None or event['revision'] > old['revision']:
                    messages[event['message_key']] = event
        files.append((name, after.st_size, after.st_mtime_ns, sha))
    return messages, files, events


def _groups(config, messages, rules):
    context = config['context']
    conversations = {}
    for event in messages.values():
        message = event['message']
        if message['sent_at'] < config['input']['start_at']:
            try:
                newly_selected = dt.datetime.fromisoformat(event['collected_at']).timestamp() >= config['input']['start_at']
            except (KeyError, TypeError, ValueError):
                newly_selected = False
            if not newly_selected or not message.get('source', {}).get('selection_context'):
                continue
        identity = tuple(message[key] for key in ('platform', 'account_id', 'conversation_id'))
        conversations.setdefault(identity, []).append(event)
    groups = []
    for identity, events in sorted(conversations.items()):
        if context['grouping'] == 'sql':
            groups.extend(_sql_groups(identity, events, config, rules))
            continue
        events.sort(key=lambda e: (e['message']['sent_at'], e['message']['message_id'], e['message_key']))
        episodes = []
        for event in events:
            if not episodes or event['message']['sent_at'] - episodes[-1][-1]['message']['sent_at'] > context['gap_seconds']:
                episodes.append([])
            episodes[-1].append(event)
        for episode in episodes:
            if sum(_message_chars(e) for e in episode) > context['max_conversation_chars']:
                raise ValueError('完整时间片段超过 context.max_conversation_chars；内容未截断')
            for start in range(0, len(episode), context['max_messages']):
                primary = episode[start:start + context['max_messages']]
                history = episode[max(0, start - context['history_messages']):start]
                group = dict(zip(('platform', 'account_id', 'conversation_id'), identity))
                primary_keys = [e['message_key'] for e in primary if e['message']['sent_at'] >= config['input']['start_at']]
                if not primary_keys:
                    continue
                group.update(id=digest([*identity, primary_keys[0]]),
                             messages=history + primary,
                             primary_message_keys=primary_keys,
                             history_message_keys=[e['message_key'] for e in history],
                             continuity='time_adjacent_filtered_candidates',
                             original_conversation_complete=False,
                             start=primary[0]['message']['sent_at'], end=primary[-1]['message']['sent_at'])
                group['fingerprint'] = digest([group, rules['sha256'], context,
                                               config['source_descriptions'],
                                               config['model']['base_url'], config['model']['model'],
                                               config['model']['vision'], 'pi-tools-v1'])
                groups.append(group)
    return sorted(groups, key=lambda g: (g['start'], g['id']))


def _sql_groups(identity, events, config, rules):
    """Pack selected runs into one-conversation requests; never fill SQL gaps."""
    def order(event):
        message = event['message']
        source = message.get('source', {})
        local = str(source.get('local_id', '0'))
        return (message['sent_at'], int(local) if local.lstrip('-').isdigit() else 0,
                Path(source.get('database', '')).name, message['message_id'], event['message_key'])
    events.sort(key=order)
    segments = []
    for event in events:
        before = event['message'].get('source', {}).get('selection_context', {})
        if not segments or not before.get('previous_known') or before.get('previous_message_key') != segments[-1][-1]['message_key']:
            segments.append([])
        segments[-1].append(event)
    limit = config['context']['max_messages']
    char_limit = config['context']['max_conversation_chars']
    batches, size, chars = [], 0, 0
    for chunk in segments:
        needed = sum(_message_chars(e) for e in chunk)
        if needed > char_limit:
            raise ValueError(f'完整关联片段长度 {needed} 超过 context.max_conversation_chars={char_limit}；请提高单会话预算或调整 SQL，片段未拆分')
        if not batches or size + len(chunk) > limit or chars + needed > char_limit:
            batches.append([])
            size = chars = 0
        batches[-1].append(chunk)
        size += len(chunk)
        chars += needed
    result = []
    for batch in batches:
        selected = [e for segment in batch for e in segment]
        primary = [e['message_key'] for e in selected if e['message']['sent_at'] >= config['input']['start_at']]
        if not primary:
            continue
        group = dict(zip(('platform', 'account_id', 'conversation_id'), identity))
        group.update(id=digest([*identity, primary[0]]), messages=selected, segments=batch,
                     primary_message_keys=primary,
                     history_message_keys=[e['message_key'] for e in selected if e['message_key'] not in set(primary)],
                     continuity='sql_selected_segments', original_conversation_complete=False,
                     start=min(e['message']['sent_at'] for e in selected), end=max(e['message']['sent_at'] for e in selected))
        group['fingerprint'] = digest([group, rules['sha256'], config['context'], config['source_descriptions'],
                                       config['model']['base_url'], config['model']['model'], config['model']['vision'], 'pi-tools-v1'])
        result.append(group)
    return result


def _message_chars(event):
    message = event['message']
    return len(canonical({'text': message['text'], 'sender': message['sender_id'],
                          'attachments': [a.get('name') for a in message.get('attachments', []) if isinstance(a, dict)]})) + 160


def _batch_plan(config, groups, rules):
    # Reserve room for the one system prompt, tool catalog and short-reference envelopes.
    available = config['processing']['max_input_chars'] - len(rules['text']) - 3000
    batches, used, conversations = [], 0, set()
    for group in groups:
        needed = sum(_message_chars(e) for e in group['messages']) + 512
        if needed > available:
            raise ValueError('完整会话分组超过总输入预算；请调整 max_input_chars 或单会话预算，消息未截断')
        identity = tuple(group[k] for k in ('platform', 'account_id', 'conversation_id'))
        if (not batches or config['agent']['batching'] == 'single_conversation'
                or used + needed > available or identity in conversations):
            batches.append([])
            used, conversations = 0, set()
        batches[-1].append(group)
        used += needed
        conversations.add(identity)
    return batches


def _snapshot(config, conn):
    rules = _rules(config)
    messages, files, events = _read_inputs(config, conn)
    groups = _groups(config, messages, rules)
    known = {} if conn is None else dict(conn.execute('SELECT id,fingerprint FROM groups'))
    pending = [g for g in groups if known.get(g['id']) != g['fingerprint']]
    return rules, messages, files, events, groups, pending


def preview(config, limit=5):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('preview limit must be between 1 and 100')
    conn = _connect(config)
    try:
        rules, _, _, events, _, pending = _snapshot(config, conn)
        from .context import prepare_input
        plan = _batch_plan(config, pending[:config['processing']['max_groups_per_round']], rules)[:limit]
        shown, batch_preview = [], []
        for batch in plan:
            existing = {t['task_id']: t for g in batch for t in (_related_tasks(conn, g)[0] if conn is not None else [])}
            payload, _, refs, _, _ = prepare_input(config, batch, list(existing.values()))
            batch_preview.append({'groups': [g['id'] for g in batch], 'conversations': len(batch),
                                  'messages': sum(len(g['messages']) for g in batch),
                                  'model_input': payload, 'source_map': refs})
        for group in [g for batch in plan for g in batch]:
            payload, _, refs, _, _ = prepare_input(config, [group])
            shown.append(dict(group, model_input=payload, source_map=refs))
        return {'groups': shown, 'pending_groups': len(pending), 'new_events': len(events),
                'batch_plan': batch_preview,
                'input': {'start_at': config['input']['start_at']},
                'rules': rules, 'model': _model_info(config),
                'warnings': ['片段只来自 SQL 选中消息；保留会话与缺口边界，附件初始只有索引，未调用模型。']}
    finally:
        if conn is not None:
            conn.close()


def status(config):
    conn = _connect(config)
    try:
        rules = _rules(config)
        saved = {} if conn is None else dict(conn.execute('SELECT key,value FROM settings'))
        messages = 0 if conn is None else conn.execute('SELECT count(*) FROM messages').fetchone()[0]
        tasks = 0 if conn is None else conn.execute("SELECT count(*) FROM tasks WHERE json_extract(body,'$.status') != 'superseded'").fetchone()[0]
        from .pi_bridge import runtime_status
        return {'model': dict(_model_info(config), engine='pi'), 'engine': runtime_status(),
                'input': {'start_at': config['input']['start_at']},
                'rules': {'path': rules['path'], 'sha256': rules['sha256']},
                'counts': {'messages': messages, 'tasks': tasks,
                           'pending_groups': int(saved['pending_groups']) if 'pending_groups' in saved else None},
                'pending_as_of': saved.get('snapshot_at'), 'pending_is_last_scan': True,
                'warnings': [] if _model_info(config)['configured'] else ['模型接口尚未配置完整；预览可用，处理不会发送请求。']}
    finally:
        if conn is not None:
            conn.close()


def list_tasks(config, limit=20):
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError('task limit must be between 1 and 1000')
    conn = _connect(config)
    if conn is None:
        return []
    try:
        return [json.loads(row[0]) for row in conn.execute("SELECT body FROM tasks WHERE json_extract(body,'$.status') != 'superseded' ORDER BY json_extract(body,'$.updated_at') DESC,task_id LIMIT ?", (limit,))]
    finally:
        conn.close()


def list_group_results(config, limit=100):
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError('group result limit must be between 1 and 1000')
    conn = _connect(config)
    if conn is None:
        return []
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='group_results'").fetchone():
            return []
        return [json.loads(row[0]) for row in conn.execute(
            "SELECT body FROM group_results ORDER BY json_extract(body,'$.updated_at') DESC,id LIMIT ?", (limit,))]
    finally:
        conn.close()


def _call_model(config, rules, group, existing):
    from .context import process_batch
    return process_batch(config, rules, [group], existing, api_key=_api_key(config))[group['id']]


def _text(value, name, *, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 20000:
        raise ValueError(f'Model task {name} must be nonempty text')
    return value.strip()


def _validate_response(response, group, existing, timezone):
    if not isinstance(response, dict) or not isinstance(response.get('tasks'), list) or len(response['tasks']) > 100:
        raise ValueError('Model response must contain at most 100 tasks')
    allowed = {event['message_key'] for event in group['messages']}
    primary = set(group['primary_message_keys'])
    existing_ids = {task['task_id'] for task in existing}
    tasks, assigned, signatures = [], set(), set()
    for item in response['tasks']:
        if not isinstance(item, dict):
            raise ValueError('Model task must be an object')
        task = {name: _text(item.get(name), name) for name in ('title', 'event', 'priority_reason')}
        steps = item.get('action_steps', [])
        if not isinstance(steps, list) or len(steps) > 50 or any(not isinstance(step, str) or not step.strip() or len(step) > 2000 for step in steps):
            raise ValueError('Model task action_steps must contain at most 50 nonempty strings of at most 2000 characters')
        task['action_steps'] = [step.strip() for step in steps]
        if not isinstance(item.get('priority'), str) or item['priority'] not in {'P0', 'P1', 'P2', 'P3', 'P4'} or not isinstance(item.get('status'), str) or item['status'] not in {'open', 'cancelled'}:
            raise ValueError('Model task priority or status is invalid')
        task.update(priority=item['priority'], status=item['status'])
        keys = item.get('source_message_keys')
        if not isinstance(keys, list) or not keys or any(not isinstance(key, str) or key not in allowed for key in keys):
            raise ValueError('Model task references missing or fabricated source messages')
        task['source_message_keys'] = sorted(set(keys))
        identifier = item.get('task_id')
        if identifier is not None and (not isinstance(identifier, str) or identifier not in existing_ids or identifier in assigned):
            raise ValueError('Model task_id must be a unique supplied existing task ID or null')
        if identifier is None and not primary.intersection(keys):
            raise ValueError('New tasks cannot be extracted solely from historical context')
        task['task_id'] = identifier
        if identifier:
            assigned.add(identifier)
        time = item.get('time')
        if not isinstance(time, dict):
            raise ValueError('Model task time must be an object')
        task['time'] = {'timezone': timezone, 'text': _text(time.get('text'), 'time.text', nullable=True)}
        for field in ('start', 'deadline'):
            value = _text(time.get(field), 'time.' + field, nullable=True)
            if value:
                try:
                    if len(value) == 10:
                        dt.date.fromisoformat(value)
                    elif dt.datetime.fromisoformat(value).tzinfo is None:
                        raise ValueError('missing offset')
                except ValueError:
                    raise ValueError('Model times must be ISO dates or timestamps with timezone offsets') from None
            task['time'][field] = value
        people = item.get('people')
        if not isinstance(people, list) or len(people) > 100 or any(not isinstance(p, dict) for p in people):
            raise ValueError('Model task people must be an array')
        task['people'] = [{name: _text(person.get(name), 'people.' + name) for name in ('name', 'role')} for person in people]
        task['location'] = _text(item.get('location'), 'location', nullable=True)
        if type(item.get('needs_review', False)) is not bool or not isinstance(item.get('review_reasons', []), list):
            raise ValueError('Legacy task explanation fields are invalid')
        notes = item.get('notes', [])
        legacy_notes = item.get('review_reasons', [])
        if not isinstance(notes, list) or len(notes) + len(legacy_notes) > 100:
            raise ValueError('Model task notes must be an array of at most 100 strings')
        task['notes'] = list(dict.fromkeys(_text(note, 'note') for note in [*notes, *legacy_notes]))
        # Legacy fields remain readable; uncertainty is explanatory text, not an approval state.
        task['review_reasons'] = list(task['notes'])
        task['needs_review'] = False
        task['attachment_evidence'] = item.get('attachment_evidence', [])
        materials = item.get('materials', [])
        messages = {event['message_key']: event for event in group['messages']}
        if not isinstance(materials, list) or len(materials) > 1000:
            raise ValueError('Task materials must be a bounded array of mapped attachments')
        task['materials'] = []
        for material in materials:
            if (not isinstance(material, dict) or set(material) != {'message_key', 'attachment_index', 'attachment'}
                    or not isinstance(material['message_key'], str) or material['message_key'] not in task['source_message_keys']
                    or type(material['attachment_index']) is not int
                    or not 0 <= material['attachment_index'] < len(messages[material['message_key']]['message'].get('attachments', []))
                    or not isinstance(material['attachment'], dict)):
                raise ValueError('Task materials must map to cited source attachment indices')
            task['materials'].append(material)
        signature = digest([task['title'], task['event'], task['source_message_keys']])
        if signature in signatures:
            raise ValueError('Model returned duplicate tasks in one response')
        signatures.add(signature)
        tasks.append(task)
    return tasks


def _source(config, event):
    message = event['message']
    return {'message_key': event['message_key'], 'event_id': event['event_id'], 'revision': event['revision'],
            'collector_module': message['platform'],
            'collector_description': config['source_descriptions'].get(message['platform'], message['platform'] + ' local collector'),
            'account_id': message['account_id'], 'conversation_id': message['conversation_id'],
            'conversation_type': message.get('conversation_type'), 'message_id': message['message_id'],
            'sender_id': message['sender_id'], 'sent_at': message['sent_at'],
            'sent_at_local': dt.datetime.fromtimestamp(message['sent_at'], ZoneInfo(config['context']['timezone'])).isoformat(),
            'collected_at': event.get('collected_at'), 'text': message['text'],
            'attachments': message.get('attachments', []), 'source': message.get('source', {}),
            'identity': message.get('identity', {}), 'sender': message.get('sender'),
            'kind': message.get('kind'), 'mentions_all': message.get('mentions_all', False),
            'context_notes': message.get('context_notes', []),
            'matched_rules': event.get('matched_rules', []), 'window': event.get('window')}


def _save_task(conn, task):
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


def _related_tasks(conn, group):
    existing = [json.loads(row[0]) for row in conn.execute("SELECT body FROM tasks WHERE json_extract(body,'$.status') != 'superseded' AND json_extract(body,'$.conversation.platform')=? AND json_extract(body,'$.conversation.account_id')=? AND json_extract(body,'$.conversation.conversation_id')=? ORDER BY json_extract(body,'$.updated_at') DESC LIMIT 40",
        (group['platform'], group['account_id'], group['conversation_id']))]
    included = {task['task_id'] for task in existing}
    old_group = conn.execute('SELECT task_ids FROM groups WHERE id=?', (group['id'],)).fetchone()
    previous_ids = set(json.loads(old_group[0])) if old_group else set()
    related_ids = {row[0] for row in conn.execute("""SELECT task_id FROM tasks
        WHERE json_extract(body,'$.status') != 'superseded' AND EXISTS (
            SELECT 1 FROM json_each(tasks.body,'$.source_message_keys')
            WHERE value IN (SELECT value FROM json_each(?)))""",
        (canonical([event['message_key'] for event in group['messages']]),))}
    for identifier in sorted((previous_ids | related_ids) - included):
        row = conn.execute('SELECT body FROM tasks WHERE task_id=?', (identifier,)).fetchone()
        if row:
            existing.append(json.loads(row[0]))
    return existing, previous_ids


def _run_batches(config, rules, groups, conn):
    from .context import process_batch
    for batch in _batch_plan(config, groups, rules):
        related = {group['id']: _related_tasks(conn, group) for group in batch}
        if len(batch) == 1:
            group = batch[0]
            responses = {group['id']: _call_model(config, rules, group, related[group['id']][0])}
        else:
            existing = {task['task_id']: task for tasks, _ in related.values() for task in tasks}
            responses = process_batch(config, rules, batch, list(existing.values()), api_key=_api_key(config))
        for group in batch:
            yield group, *related[group['id']], responses[group['id']]


def run_round(config):
    if not _model_info(config)['configured']:
        raise ValueError('Configure model.base_url, model.model and a local API key before processing; preview remains available')
    with InstanceLock(config['storage']['state_file']):
        refreshes = []
        warnings = (['input.refresh_collectors 已停用；处理只读取已发布批次，请单独运行采集。']
                    if config['input'].get('refresh_collectors') else [])
        conn = _connect(config, write=True)
        try:
            batches = _export(config, conn)
            rules, messages, files, events, groups, pending = _snapshot(config, conn)
            with conn:
                conn.executemany('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                                 [('pending_groups', str(len(pending))), ('snapshot_at', dt.datetime.now(dt.timezone.utc).isoformat())])
                conn.executemany('INSERT INTO files VALUES(?,?,?,?) ON CONFLICT(path) DO UPDATE SET size=excluded.size,mtime=excluded.mtime,sha256=excluded.sha256', files)
                conn.executemany('INSERT INTO events VALUES(?,?)', [(key, digest(event)) for key, event in events.items()])
                for event in events.values():
                    conn.execute('INSERT INTO messages VALUES(?,?,?) ON CONFLICT(message_key) DO UPDATE SET revision=excluded.revision,body=excluded.body WHERE excluded.revision>messages.revision',
                                 (event['message_key'], event['revision'], canonical(event)))
            processed = updated = 0
            outcomes = []
            for group, existing, previous_ids, response in _run_batches(config, rules, pending[:config['processing']['max_groups_per_round']], conn):
                existing = list({task['task_id']:task for task in [*existing, *response.get('_existing_tasks', [])]}.values())
                tasks = _validate_response(response, group, existing, config['context']['timezone'])
                now = dt.datetime.now(dt.timezone.utc).isoformat()
                existing_by_id = {task['task_id']: task for task in existing}
                ids = []
                with conn:
                    for index, task in enumerate(tasks):
                        previous = existing_by_id.get(task['task_id'])
                        if previous:
                            if previous.get('priority_control'):
                                task['priority_control'] = previous['priority_control']
                                task['priority'] = previous['priority_control']['priority']
                                task['priority_reason'] = previous['priority_reason']
                            if previous.get('status_control'):
                                task['status_control'] = previous['status_control']
                                if previous['status_control']['status'] in {'completed', 'cancelled'}:
                                    task['status'] = previous['status_control']['status']
                            task['source_message_keys'] = sorted(set(task['source_message_keys']) | set(previous['source_message_keys']))
                            evidence = {canonical([e['message_key'], e['attachment_index'], e.get('locator')]): e
                                        for e in [*previous.get('attachment_evidence', []), *task.get('attachment_evidence', [])]}
                            task['attachment_evidence'] = list(evidence.values())
                            materials = {(item['message_key'], item['attachment_index']): item
                                         for item in [*previous.get('materials', []), *task.get('materials', [])]}
                            task['materials'] = list(materials.values())
                        task['task_id'] = task['task_id'] or digest([group['id'], group['fingerprint'], index])
                        task.update(schema_version=1, updated_at=now,
                            conversation=previous['conversation'] if previous else {key: group[key] for key in ('platform', 'account_id', 'conversation_id')},
                            sources=list({source['message_key']:source for source in [
                                *(previous.get('sources', []) if previous else []),
                                *[_source(config, messages[key]) for key in task['source_message_keys'] if key in messages]]}.values()),
                            context={'group_id': group['id'], 'continuity': group['continuity'],
                                     'original_conversation_complete': False,
                                     'primary_message_keys': group['primary_message_keys'], 'history_message_keys': group['history_message_keys']},
                            processing={'rules_sha256': rules['sha256'], 'rules_file': rules['path'],
                                        'model': config['model']['model'], 'base_url': config['model']['base_url'],
                                        'input_sha256': group['fingerprint'], 'processed_at': now,
                                        'agent_runtime': response.get('_agent', {})})
                        _save_task(conn, task)
                        ids.append(task['task_id'])
                        updated += 1
                    # A full re-extraction replaces this group's earlier result, never deletes history.
                    for identifier in previous_ids - set(ids):
                        row = conn.execute('SELECT body FROM tasks WHERE task_id=?', (identifier,)).fetchone()
                        if row:
                            old = json.loads(row[0])
                            if (old['context']['group_id'] == group['id'] and old['status'] != 'superseded'
                                    and old.get('status_control', {}).get('status') not in {'completed', 'cancelled'}):
                                old.update(status='superseded', updated_at=now, superseded_reason='group_reprocessed')
                                _save_task(conn, old)
                    conn.execute('INSERT INTO groups VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET fingerprint=excluded.fingerprint,task_ids=excluded.task_ids',
                                 (group['id'], group['fingerprint'], canonical(ids)))
                    outcome = {'group_id':group['id'],'updated_at':now,'tasks_count':len(ids),
                        'conversation':{k:group[k] for k in ('platform','account_id','conversation_id')},
                        'attachment_review':response.get('attachment_review', []),
                        'review_required':response.get('review_required', False),
                        'submission_file':response.get('submission_file'),
                        'scope':'selected_messages_and_requested_attachment_content'}
                    conn.execute('INSERT OR REPLACE INTO group_results VALUES(?,?)', (group['id'],canonical(outcome)))
                    conn.execute("UPDATE settings SET value=? WHERE key='pending_groups'", (str(len(pending) - processed - 1),))
                processed += 1
                outcomes.append(outcome)
                batches.extend(_export(config, conn))
            # Late messages can change chunk boundaries. Retire obsolete group outputs only
            # after every current group has succeeded, so API failure cannot discard valid tasks.
            if processed == len(pending):
                current_ids = {g['id'] for g in groups}
                now = dt.datetime.now(dt.timezone.utc).isoformat()
                with conn:
                    for identifier, in conn.execute('SELECT id FROM groups').fetchall():
                        if identifier not in current_ids:
                            for body, in conn.execute("SELECT body FROM tasks WHERE json_extract(body,'$.context.group_id')=? AND json_extract(body,'$.status')!='superseded'", (identifier,)).fetchall():
                                old = json.loads(body)
                                if old.get('status_control', {}).get('status') in {'completed', 'cancelled'}:
                                    continue
                                old.update(status='superseded', updated_at=now, superseded_reason='context_regrouped')
                                _save_task(conn, old)
                            conn.execute('DELETE FROM groups WHERE id=?', (identifier,))
                            conn.execute('DELETE FROM group_results WHERE id=?', (identifier,))
                batches.extend(_export(config, conn))
            return {'status': 'succeeded', 'imported_events': len(events), 'processed_groups': processed,
                    'pending_groups': len(pending) - processed, 'tasks_upserted': updated, 'batches': batches,
                    'group_results':outcomes, 'review_groups':sum(item['review_required'] for item in outcomes),
                    'collection':refreshes, 'warnings':warnings}
        finally:
            conn.close()
