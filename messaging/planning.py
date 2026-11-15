"""Read-only projections of existing dispatch rules; never prepare assets or enqueue."""
from contextlib import contextmanager
import datetime as dt
import json
import time
from zoneinfo import ZoneInfo

from . import core
from .feedback import blocked_task_ids
from .notifications import deadline_schedule, deadline_node, reminder_due, digest_items


def _duration(seconds):
    for size, unit in ((86400, '天'), (3600, '小时'), (60, '分钟')):
        if seconds % size == 0:
            return f'{seconds // size} {unit}'
    return f'{seconds} 秒'


def targets(config):
    from .management import channel_types
    types = channel_types()
    return [{'id': t['id'], 'name': t.get('name') or t['id'], 'kind': t['kind'],
             'description': types.get(t['kind'], {}).get('name', t['kind']), 'enabled': t['enabled'],
             'mode': 'sync' if t['kind'] == 'notion' or t.get('mode') == 'sync' else 'notify',
             'digest': t['kind'] != 'notion' and t.get('mode') != 'sync'} for t in config['targets']]


@contextmanager
def snapshot(config):
    conn = core._connect(config)
    try:
        tasks, events, _ = core._inputs(config, conn)
        local = core._local(conn, config)
        yield conn, {k: core._effective(t, local.get(k, {})) for k, t in tasks.items()}, local, events
    finally:
        if conn is not None:
            conn.close()


def _plan(config, conn, task, state, target, blocked, imported, now):
    key = task['task_id']
    result = {'target_id': target['id'], 'state': 'scheduled', 'due_at': None, 'reason': '',
              'last_sent_at': None, 'delivery_id': None}

    def finish(status, reason, due=None):
        return dict(result, state=status, reason=reason, due_at=due)

    if key in blocked:
        return finish('feedback', '操作等待处理层回写，暂停投递')
    unknown = last = active = None
    fingerprint = core._target_hash(target)
    if conn is not None:
        unknown = conn.execute("SELECT id,error FROM deliveries WHERE target_id=? AND task_id=? AND status='unknown' LIMIT 1",
                               (target['id'], key)).fetchone()
        last = conn.execute("SELECT id,status,revision,local_version,package_hash,finished_at FROM deliveries WHERE target_id=? AND task_id=? AND target_hash=? AND status IN ('sent','deferred') ORDER BY finished_at DESC,rowid DESC LIMIT 1",
                            (target['id'], key, fingerprint)).fetchone()
        if last and last['status'] == 'sent':
            result['last_sent_at'] = last['finished_at']
    if unknown:
        result['delivery_id'] = unknown['id']
        return finish('unknown', '结果未知，需先在目标端核对')
    if not target['enabled'] or not config['enabled']:
        return finish('disabled', '投递目标未启用' if not target['enabled'] else '外部投递已关闭')
    sync_only = target['kind'] == 'notion' or target.get('mode') == 'sync'
    if task['status'] != 'open' and not last and not sync_only:
        return finish('ended', '待办已结束，不再通知')
    initial = config['initial_notifications'][task['priority']]
    if task['status'] != 'open' and last and last['status'] == 'deferred' and not initial and not sync_only:
        return finish('ended', '待办已结束，首次通知关闭时不补发')
    current = bool(last and last['revision'] == task['revision'] and last['local_version'] == state.get('version', 0))
    if last and last['status'] == 'deferred' and config['initial_notifications'][task['priority']]:
        current = False
    if conn is not None:
        rows = conn.execute("SELECT id,status,kind,next_attempt,created_at,package_hash,json_extract(body,'$.reminder.due_at') AS node_at FROM deliveries WHERE target_id=? AND task_id=? AND target_hash=? AND revision=? AND local_version=? AND status IN ('pending','retry','failed','sending') ORDER BY created_at DESC,rowid DESC",
                            (target['id'], key, fingerprint, task['revision'], state.get('version', 0)))
        mode, node = deadline_node(config, task, now)
        for row in rows:
            if row['kind'] == 'reminder' and ((mode and row['node_at'] != (node['due_at'] if node else None)) or (not mode and row['node_at'] is not None)):
                continue
            active = row
            break
    if active and active['kind'] == 'upsert' and not sync_only and not initial and (not last or last['status'] == 'deferred'):
        active = None
    if active:
        result['delivery_id'] = active['id']
        # A material-only update can be queued without a new task revision.
        if last and active['package_hash'] != last['package_hash']:
            current = False
        if active['status'] == 'sending':
            return finish('sending', '正在投递，等待目标回执')
        if state.get('snoozed_until', 0) <= now or sync_only:
            if active['status'] == 'failed':
                return finish('failed', '已达到重试上限，需要处理')
            return finish('retry' if active['status'] == 'retry' else 'queued',
                          '失败后按退避重试' if active['status'] == 'retry' else ('截止节点提醒已排队' if active['kind'] == 'reminder' else '首次通知或内容更新已排队'),
                          max(now, active['next_attempt'] or active['created_at']))
    if current:
        if sync_only or task['status'] != 'open':
            return finish('synced' if sync_only else 'ended', '仅在内容或状态变化后同步' if sync_only else '待办已结束，不再提醒')
        due, node = reminder_due(config, task, state, last, now)
    elif not sync_only and not initial and (not last or last['status'] == 'deferred'):
        due, node = reminder_due(config, task, state, {'finished_at': now}, now)
        if due is None:
            return finish('no_reminders', '首次通知已关闭，当前没有后续提醒节点')
        return finish('scheduled', '首次通知已关闭，等待' + ('截止提醒节点' if node else '普通间隔'), due)
    else:
        return finish('snoozed' if not sync_only and state.get('snoozed_until', 0) > now else 'queued' if imported else 'waiting_import',
                      '延后到期后检查' if not sync_only and state.get('snoozed_until', 0) > now else '首次同步 · 下一轮检查' if sync_only else '内容更新 · 下一轮检查' if last else '首次通知 · 下一轮接收后检查',
                      max(now, state.get('snoozed_until', 0)) if not sync_only else now)
    if due is None:
        return finish('no_reminders', '当前节点均已覆盖或未配置后续提醒')
    return finish('snoozed' if state.get('snoozed_until', 0) > now else 'scheduled',
                  '延后到期后检查最近节点' if state.get('snoozed_until', 0) > now else
                  ('截止时' if node['offset_seconds'] == 0 else '截止前 ' + _duration(node['offset_seconds'])) if node else '按 ' + _duration(config['reminders'][task['priority']]) + '间隔提醒', due)


def schedule(config, *, now=None, limit=50, offset=0, search='', state='all', target_id=None, task_id=None):
    now = time.time() if now is None else float(now)
    descriptions = targets(config)
    if target_id and target_id not in {t['id'] for t in descriptions}:
        raise ValueError('unknown_target')
    blocked = blocked_task_ids(config)
    with snapshot(config) as (conn, tasks, local, events):
        persisted = {} if conn is None else dict(conn.execute('SELECT task_id,revision FROM tasks'))
        items, attention = [], 0
        for key, task in tasks.items():
            if task_id is not None and key != task_id:
                continue
            names = list(dict.fromkeys(((s.get('identity') or {}).get('conversation') or {}).get('display_name') or
                        ((s.get('identity') or {}).get('conversation') or {}).get('remark') or
                        ((s.get('identity') or {}).get('conversation') or {}).get('name') or s.get('collector_module') or '名称未获取' for s in task.get('sources', [])))
            plans = [_plan(config, conn, task, local.get(key, {}), t, blocked,
                           persisted.get(key) == task['revision'], now) for t in config['targets']]
            needs_attention = any(p['state'] in {'unknown', 'feedback', 'failed'} for p in plans)
            attention += int(needs_attention)
            selected = [p for p in plans if not target_id or p['target_id'] == target_id]
            problem = any(p['state'] in {'unknown', 'feedback', 'failed'} for p in selected)
            due = min((p['due_at'] for p in selected if p['due_at'] is not None), default=None)
            if search.casefold() not in ' '.join([key, task['title'], *names]).casefold():
                continue
            if ((state == 'attention' and not problem) or (state == 'scheduled' and due is None)
                    or (state == 'ended' and task['status'] == 'open')):
                continue
            nodes = deadline_schedule(config, task)
            value = (task.get('time') or {}).get('deadline')
            deadline = (nodes[0]['deadline_at'] if nodes else None)
            if value and deadline is None:
                timing = dict(config, deadline_reminders=dict(config['deadline_reminders'], enabled=True, **{task['priority']: [0]}))
                deadline = deadline_schedule(timing, task)[0]['deadline_at']
            items.append({'task_id': key, 'title': task['title'], 'revision': task['revision'], 'priority': task['priority'],
                          'status': task['status'], 'updated_at': task.get('updated_at'), 'source_names': names,
                          'source_count': len(task.get('sources', [])), 'deadline_at': deadline,
                          'date_only': bool(value and len(value) == 10), 'nodes': nodes or [], 'plans': plans,
                          'next_at': due, 'snoozed_until': local.get(key, {}).get('snoozed_until'), 'attention': problem})
        items.sort(key=lambda t: (t['next_at'] is None, t['next_at'] or 0, t['priority'], t['task_id']))
        return {'generated_at': now, 'enabled': config['enabled'], 'targets': descriptions,
                'total_tasks': len(tasks), 'matched_tasks': len(items), 'pending_events': len(events), 'attention': attention,
                'tasks': items[offset:offset + limit], 'has_more': offset + limit < len(items),
                'rules': {k: config[k] for k in ('reminders', 'deadline_reminders', 'initial_notifications', 'daily_digest')}}


def digest_preview(config, *, day=None, target_id=None, now=None):
    now = time.time() if now is None else float(now)
    settings = config['daily_digest']
    zone = ZoneInfo(settings['timezone'])
    today = dt.datetime.fromtimestamp(now, zone).date()
    when = dt.time.fromisoformat(settings['time'])
    if day is None:
        slot = dt.datetime.combine(today, when, zone).timestamp()
        day = today if now < min(dt.datetime.combine(today + dt.timedelta(days=1), dt.time(), zone).timestamp(), slot + settings['catch_up_minutes'] * 60) else today + dt.timedelta(days=1)
    elif isinstance(day, str):
        day = dt.date.fromisoformat(day)
    if not today - dt.timedelta(days=31) <= day <= today + dt.timedelta(days=366):
        raise ValueError('digest_date_outside_preview_range')
    eligible = [t for t in targets(config) if t['digest']]
    target_id = target_id or next((t['id'] for t in eligible if t['enabled']), eligible[0]['id'] if eligible else None)
    if target_id and target_id not in {t['id'] for t in eligible}:
        raise ValueError('target_does_not_receive_digests')
    target = next((t for t in config['targets'] if t['id'] == target_id), None)
    slot = dt.datetime.combine(day, when, zone).timestamp()
    expires = min(dt.datetime.combine(day + dt.timedelta(days=1), dt.time(), zone).timestamp(), slot + settings['catch_up_minutes'] * 60)
    blocked = blocked_task_ids(config)
    with snapshot(config) as (conn, tasks, local, _):
        items = digest_items(config, conn, tasks, local, day, blocked, target, core._target_hash(target)) if target else []
        stored = None
        if target and conn is not None:
            from common.codec import digest
            identifier = digest([target_id, core._target_hash(target), 'daily_digest', day.isoformat()])
            row = conn.execute('SELECT status,body,created_at,finished_at,next_attempt FROM deliveries WHERE id=?', (identifier,)).fetchone()
            if row:
                stored = {k: row[k] for k in ('status', 'created_at', 'finished_at', 'next_attempt')}
                # Sent/unknown content is immutable in the real queue as well.
                if row['status'] in {'sent', 'unknown', 'sending'}:
                    items = json.loads(row['body'])['digest']['items']
                    stored['frozen'] = True
        included = {item['task_id'] for item in items}
        excluded = [{'task_id': key, 'title': task['title'], 'reason': '待办已结束' if task['status'] != 'open' else
                     '反馈等待回写，暂停通知' if key in blocked else '当天没有已知通知安排'}
                    for key, task in tasks.items() if key not in included] if not (stored or {}).get('frozen') else []
        return {'generated_at': now, 'date': day.isoformat(), 'timezone': str(zone), 'slot': slot, 'expires_at': expires,
                'enabled': settings['enabled'] and config['enabled'] and bool(target and target['enabled']),
                'targets': eligible, 'target_id': target_id, 'items': items, 'excluded': excluded, 'delivery': stored}
