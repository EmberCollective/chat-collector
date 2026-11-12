"""Read-only, parameterized search over persisted tasks (never source chat databases)."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import sqlite3
import time
from zoneinfo import ZoneInfo


def search_tasks(config, *, query=None, platforms=None, account_id=None, conversation_id=None,
                 people=None, person_role=None, statuses=None, priorities=None, needs_review=None,
                 time_field='deadline', time_from=None, time_to=None, sort_by='updated_at',
                 order='desc', limit=20, offset=0):
    """Different filters are ANDed; lists are ORed. Time intervals are [from, to)."""
    def text(value, field):
        if not isinstance(value, str) or not value.strip() or len(value) > 200:
            raise ValueError(f'{field}_must_be_nonempty_text_up_to_200_characters')
        return value.strip()

    def values(items, field, allowed=None):
        if items is None:
            return None
        if not isinstance(items, list) or not 1 <= len(items) <= 20:
            raise ValueError(f'{field}_must_contain_1_to_20_values')
        result = [text(item, field) for item in items]
        if allowed and any(item not in allowed for item in result):
            raise ValueError(f'invalid_{field}')
        return result

    platforms = values(platforms, 'platforms')
    people = values(people, 'people')
    statuses = values(statuses, 'statuses', {'open', 'cancelled', 'superseded'})
    priorities = values(priorities, 'priorities', {'P0', 'P1', 'P2', 'P3', 'P4'})
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 1000000:
        raise ValueError('invalid_search_pagination')
    if needs_review is not None and type(needs_review) is not bool:
        raise ValueError('needs_review_must_be_boolean')
    if time_field not in {'deadline', 'start', 'updated_at', 'source_time'}:
        raise ValueError('invalid_time_field')
    if sort_by not in {'deadline', 'start', 'updated_at'} or order not in {'asc', 'desc'}:
        raise ValueError('invalid_search_sort')
    zone = ZoneInfo(config.get('context', {}).get('timezone', 'Asia/Shanghai'))

    def timestamp(value):
        if value is None:
            return None
        try:
            if len(value) == 10:
                return dt.datetime.combine(dt.date.fromisoformat(value), dt.time(), zone).timestamp()
            parsed = dt.datetime.fromisoformat(value)
            return parsed.timestamp() if parsed.tzinfo is not None else None
        except (ValueError, TypeError, OverflowError):
            return None

    bounds = []
    for value in (time_from, time_to):
        parsed = timestamp(value) if isinstance(value, str) else None
        if value is not None and parsed is None:
            raise ValueError('search_time_requires_ISO_date_or_timestamp_with_timezone')
        bounds.append(parsed)
    if all(v is not None for v in bounds) and bounds[0] >= bounds[1]:
        raise ValueError('time_from_must_precede_time_to')
    params, where = [], []
    field = lambda name: f"json_extract(t.body,'$.{name}')"
    if statuses:
        where.append(f"{field('status')} IN (SELECT value FROM json_each(?))")
        params.append(json.dumps(statuses))
    else:
        where.append(f"{field('status')} != 'superseded'")
    if query is not None:
        where.append(f"instr(casefold(coalesce({field('title')},'') || char(10) || coalesce({field('event')},'')),?)>0")
        params.append(text(query, 'query').casefold())
    if priorities:
        where.append(f"{field('priority')} IN (SELECT value FROM json_each(?))")
        params.append(json.dumps(priorities))
    if needs_review is not None:
        where.append(f"{field('needs_review')}=?")
        params.append(int(needs_review))
    origin_conditions, origin_params = [], []
    if platforms:
        origin_conditions.append("coalesce(json_extract({body},'$.platform'),json_extract({body},'$.collector_module')) IN (SELECT value FROM json_each(?))")
        origin_params.append(json.dumps(platforms))
    for name, value in [('account_id', account_id), ('conversation_id', conversation_id)]:
        if value is not None:
            origin_conditions.append(f"json_extract({{body}},'$.{name}')=?")
            origin_params.append(text(value, name))
    if origin_conditions:
        criteria = ' AND '.join(origin_conditions)
        where.append('(' + criteria.format(body=field('conversation')) +
            " OR EXISTS (SELECT 1 FROM json_each(t.body,'$.sources') s WHERE " + criteria.format(body='s.value') + '))')
        params.extend(origin_params * 2)
    if people or person_role is not None:
        conditions = []
        if people:
            conditions.append("EXISTS (SELECT 1 FROM json_each(?) n WHERE instr(casefold(json_extract(p.value,'$.name')),casefold(n.value))>0)")
            params.append(json.dumps(people, ensure_ascii=False))
        if person_role is not None:
            conditions.append("instr(casefold(json_extract(p.value,'$.role')),?)>0")
            params.append(text(person_role, 'person_role').casefold())
        where.append("EXISTS (SELECT 1 FROM json_each(t.body,'$.people') p WHERE " + ' AND '.join(conditions) + ')')
    if any(v is not None for v in bounds):
        expr = "CAST(json_extract(s.value,'$.sent_at') AS REAL)" if time_field == 'source_time' else f"task_time({field('updated_at' if time_field == 'updated_at' else 'time.'+time_field)})"
        clauses = []
        for value, operator in zip(bounds, ['>=', '<']):
            if value is not None:
                clauses.append(f'{expr}{operator}?')
                params.append(value)
        criteria = ' AND '.join(clauses)
        where.append("EXISTS (SELECT 1 FROM json_each(t.body,'$.sources') s WHERE " + criteria + ')' if time_field == 'source_time' else criteria)
    sort = f"task_time({field('updated_at' if sort_by == 'updated_at' else 'time.'+sort_by)})"
    paths = list(dict.fromkeys(Path(p).resolve() for p in [config['storage']['state_file'],
        config.get('_task_search_state_file', config['storage']['state_file'])]))
    conn = sqlite3.connect(':memory:', timeout=5, uri=True)
    try:
        conn.create_function('casefold', 1, lambda v: str(v or '').casefold(), deterministic=True)
        conn.create_function('task_time', 1, timestamp, deterministic=True)
        tables = []
        for path in paths:
            if not path.is_file():
                continue
            alias = 'db' + str(len(tables))
            conn.execute(f'ATTACH DATABASE ? AS {alias}', (path.as_uri()+'?mode=ro',))
            if not conn.execute(f"SELECT 1 FROM {alias}.sqlite_master WHERE name='tasks'").fetchone():
                conn.execute(f'DETACH DATABASE {alias}')
                continue
            tables.append(alias+'.tasks')
        if not tables:
            return {'tasks': [], 'has_more': False, 'next_offset': None}
        # Debug revisions override formal tasks, while the formal database stays read-only.
        rows = f'SELECT task_id,body FROM {tables[0]}'
        if len(tables) == 2:
            rows += f' UNION ALL SELECT task_id,body FROM {tables[1]} b WHERE NOT EXISTS (SELECT 1 FROM {tables[0]} a WHERE a.task_id=b.task_id)'
        conn.execute('PRAGMA query_only=ON')
        deadline = time.monotonic() + 5
        conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        result = conn.execute(f"WITH t AS ({rows}) SELECT body FROM t WHERE {' AND '.join(where)} ORDER BY {sort} IS NULL, {sort} {order},task_id ASC LIMIT ? OFFSET ?",
            [*params, limit+1, offset]).fetchall()
        return {'tasks': [json.loads(row[0]) for row in result[:limit]], 'has_more': len(result)>limit,
                'next_offset': offset+limit if len(result)>limit else None}
    finally:
        conn.close()


class TaskLookup:
    """Per-run short references; only requested result pages are sent to Pi."""
    def __init__(self, config, groups, task_map):
        self.config, self.tasks = config, task_map
        self.refs = {task['task_id']: ref for ref, task in task_map.items()}
        self.conversations = {f'c{i}': {k: group[k] for k in ('platform','account_id','conversation_id')}
                              for i, group in enumerate(groups, 1)}
        self.accounts = {}
        for origin in self.conversations.values():
            self._account(origin)

    def _account(self, origin):
        identity = {k: origin[k] for k in ('platform','account_id')}
        ref = next((ref for ref, value in self.accounts.items() if value == identity), None)
        if ref is None:
            ref = f'a{len(self.accounts)+1}'
            self.accounts[ref] = identity
        return ref

    def _origin(self, origin):
        identity = {k: origin[k] for k in ('platform','account_id','conversation_id')}
        ref = next((ref for ref, value in self.conversations.items() if value == identity), None)
        if ref is None:
            ref = f'c{len(self.conversations)+1}'
            self.conversations[ref] = identity
        return {'conversation_ref':ref, 'account_ref':self._account(identity), 'platform':identity['platform']}

    def find(self, *, conversation_ref=None, account_ref=None, **filters):
        # Raw account/conversation identifiers are never accepted from the model.
        if 'account_id' in filters or 'conversation_id' in filters:
            raise ValueError('use_short_account_and_conversation_refs')
        origin = {}
        if account_ref is not None:
            if account_ref not in self.accounts:
                raise ValueError('unknown_account_ref')
            origin.update(self.accounts[account_ref])
        if conversation_ref is not None:
            if conversation_ref not in self.conversations:
                raise ValueError('unknown_conversation_ref')
            conversation = self.conversations[conversation_ref]
            if any(conversation[k] != v for k, v in origin.items()):
                raise ValueError('account_and_conversation_filters_conflict')
            origin.update(conversation)
        if origin:
            if filters.get('platforms') is not None and origin['platform'] not in filters['platforms']:
                raise ValueError('platform_and_reference_filters_conflict')
            filters.update(platforms=[origin.pop('platform')], **origin)
        result = search_tasks(self.config, **filters)
        rows = []
        for task in result['tasks']:
            ref = self.refs.get(task['task_id'])
            if ref is None:
                ref = f't{len(self.tasks)+1}'
                self.refs[task['task_id']] = ref
            self.tasks[ref] = task
            row = {k:task.get(k) for k in ('title','event','priority','status','time','people','location','needs_review','updated_at')}
            origins = []
            for source in [task['conversation'], *task.get('sources', [])]:
                source = dict(source, platform=source.get('platform') or source.get('collector_module'))
                if not all(k in source for k in ('platform','account_id','conversation_id')):
                    continue
                identity = self._origin(source)
                if identity not in origins:
                    origins.append(identity)
            row.update(task_id=ref, **self._origin(task['conversation']), origins=origins,
                       updatable=task.get('status')!='superseded')
            rows.append(row)
        return dict(result, tasks=rows, scope='full_task_database',
                    timezone=self.config.get('context', {}).get('timezone','Asia/Shanghai'))
