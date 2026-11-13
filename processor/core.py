from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import tomllib
import urllib.parse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from common.locking import InstanceLock
from common.codec import canonical, digest
from contracts import validate_event as _validate_event


from .storage import SCHEMA, _connect, _save_task, _export
from . import service
from .task_validation import _text, _validate_response, _source


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
    from providers.content import configure_content
    configure_content(config.setdefault('attachments', {}), path.parent)
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
        committed = {}
        for group in batch:
            yield group, *related[group['id']], responses[group['id']], committed


def run_round(config):
    if not _model_info(config)['configured']:
        raise ValueError('Configure model.base_url, model.model and a local API key before processing; preview remains available')
    with InstanceLock(config['storage']['state_file'] + '.round'):
        refreshes = []
        warnings = (['input.refresh_collectors 已停用；处理只读取已发布批次，请单独运行采集。']
                    if config['input'].get('refresh_collectors') else [])
        batches = service._export_pending(config)
        conn = _connect(config)
        try:
            rules, messages, files, events, groups, pending = _snapshot(config, conn)
            with service._task_connection(config) as writer, writer:
                writer.executemany('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                                 [('pending_groups', str(len(pending))), ('snapshot_at', dt.datetime.now(dt.timezone.utc).isoformat())])
                writer.executemany('INSERT INTO files VALUES(?,?,?,?) ON CONFLICT(path) DO UPDATE SET size=excluded.size,mtime=excluded.mtime,sha256=excluded.sha256', files)
                writer.executemany('INSERT INTO events VALUES(?,?)', [(key, digest(event)) for key, event in events.items()])
                for event in events.values():
                    writer.execute('INSERT INTO messages VALUES(?,?,?) ON CONFLICT(message_key) DO UPDATE SET revision=excluded.revision,body=excluded.body WHERE excluded.revision>messages.revision',
                                 (event['message_key'], event['revision'], canonical(event)))
            processed = updated = 0
            outcomes = []
            for group, existing, previous_ids, response, committed in _run_batches(config, rules, pending[:config['processing']['max_groups_per_round']], conn):
                existing = list({task['task_id']:task for task in [*existing, *response.get('_existing_tasks', [])]}.values())
                # Rebase only onto snapshots committed by earlier groups in this Pi batch.
                existing = [committed.get(task['task_id'], task) for task in existing]
                result = service.apply_extraction(config, response,
                    request_id='extraction:' + digest([group['id'], group['fingerprint'],
                        [(task['task_id'], task['revision']) for task in existing]]),
                    context={'group': group, 'existing': existing, 'previous_ids': sorted(previous_ids),
                             'messages': messages, 'rules': rules, 'pending_groups': len(pending) - processed - 1})
                processed += 1
                updated += result['tasks_upserted']
                outcomes.append(result['outcome'])
                batches.extend(result['batches'])
                committed.update({task['task_id']: task for task in result['tasks']})
            # Retire regrouped outputs only after every current group succeeds.
            if processed == len(pending):
                batches.extend(service._retire_groups(config, {group['id'] for group in groups}))
            return {'status': 'succeeded', 'imported_events': len(events), 'processed_groups': processed,
                    'pending_groups': len(pending) - processed, 'tasks_upserted': updated, 'batches': batches,
                    'group_results':outcomes, 'review_groups':sum(item['review_required'] for item in outcomes),
                    'collection':refreshes, 'warnings':warnings}
        finally:
            conn.close()
