"""Notification schedules and digest data; channel adapters own presentation."""
import datetime as dt
from zoneinfo import ZoneInfo

from collector.store import canonical, digest


def deadline_schedule(config, task):
    settings = config.get('deadline_reminders', {})
    timing = task.get('time') or {}
    value = timing.get('deadline')
    if not settings.get('enabled') or not value:
        return None
    if len(value) == 10:
        deadline = dt.datetime.combine(dt.date.fromisoformat(value), dt.time(23, 59, 59),
                                       ZoneInfo(timing.get('timezone') or settings['timezone'])).timestamp()
    else:
        deadline = dt.datetime.fromisoformat(value).timestamp()
    return [{'deadline_at': deadline, 'offset_seconds': offset, 'due_at': deadline - offset}
            for offset in settings[task['priority']]]


def deadline_node(config, task, now):
    schedule = deadline_schedule(config, task)
    if schedule is None:
        return False, None
    due = [node for node in schedule if node['due_at'] <= now]
    return True, max(due, key=lambda n: n['due_at']) if due else None


def queue_digest(config, conn, tasks, local, now, blocked, target_hash):
    settings = config.get('daily_digest', {})
    zone = ZoneInfo(settings.get('timezone', 'Asia/Shanghai'))
    today = dt.datetime.fromtimestamp(now, zone).date()
    start = dt.datetime.combine(today, dt.time(), zone).timestamp()
    end = dt.datetime.combine(today + dt.timedelta(days=1), dt.time(), zone).timestamp()
    slot = dt.datetime.combine(today, dt.time.fromisoformat(settings.get('time', '08:00')), zone).timestamp()
    expires = min(end, slot + settings.get('catch_up_minutes', 60) * 60)
    with conn:
        conn.execute("UPDATE deliveries SET status='obsolete' WHERE kind='daily_digest' AND status IN ('pending','retry','failed') AND (json_extract(body,'$.digest.expires_at')<=? OR ?=0)",
                     (now, int(bool(settings.get('enabled')))))
    if not settings.get('enabled') or not slot <= now < expires:
        return
    for target in config['targets']:
        if not target['enabled'] or target['kind'] == 'notion' or target.get('mode') == 'sync':
            continue
        fingerprint = target_hash(target)
        items = []
        for key, task in tasks.items():
            if task['status'] != 'open' or key in blocked:
                continue
            snoozed = local.get(key, {}).get('snoozed_until', 0)
            schedule = deadline_schedule(config, task)
            notifications = []
            if schedule is not None:
                notifications = [{'time': dt.datetime.fromtimestamp(n['due_at'], zone).isoformat(), 'kind': 'deadline',
                                  'offset_seconds': n['offset_seconds']} for n in schedule
                                 if start <= n['due_at'] < end and n['due_at'] >= snoozed]
            last = conn.execute("SELECT * FROM deliveries WHERE target_id=? AND target_hash=? AND task_id=? AND status IN ('sent','deferred') ORDER BY finished_at DESC,rowid DESC LIMIT 1",
                                (target['id'], fingerprint, key)).fetchone()
            first = conn.execute("SELECT created_at FROM deliveries WHERE target_id=? AND target_hash=? AND task_id=? AND kind='upsert' AND status IN ('sent','pending','retry') ORDER BY created_at LIMIT 1",
                                 (target['id'], fingerprint, key)).fetchone()
            if first and start <= first[0] < end and first[0] >= snoozed:
                notifications.append({'time': dt.datetime.fromtimestamp(first[0], zone).isoformat(), 'kind': 'initial'})
            if last and last['status'] == 'sent' and start <= snoozed < end:
                notifications.append({'time': dt.datetime.fromtimestamp(snoozed, zone).isoformat(), 'kind': 'snooze'})
            if schedule is None and last and config['reminders'][task['priority']]:
                interval = config['reminders'][task['priority']]
                next_at = max(last['finished_at'] + interval, snoozed)
                if next_at < start:
                    next_at += ((start - next_at + interval - 1) // interval) * interval
                if next_at < end:
                    notifications.append({'time': dt.datetime.fromtimestamp(next_at, zone).isoformat(), 'kind': 'interval', 'interval_seconds': interval})
            if notifications:
                items.append({'task_id': key, 'title': task['title'], 'priority': task['priority'],
                              'deadline': (task.get('time') or {}).get('deadline'),
                              'notifications': sorted(notifications, key=lambda n: n['time'])})
        items.sort(key=lambda item: (item['priority'], item['notifications'][0]['time'], item['task_id']))
        identifier = digest([target['id'], fingerprint, 'daily_digest', today.isoformat()])
        payload = {'schema_version': 1, 'delivery_id': identifier, 'kind': 'daily_digest',
                   'digest': {'date': today.isoformat(), 'timezone': str(zone),
                              'generated_at': dt.datetime.fromtimestamp(now, zone).isoformat(),
                              'expires_at': expires, 'items': items}}
        with conn:
            conn.execute('INSERT OR IGNORE INTO deliveries(id,target_id,target_hash,task_id,revision,local_version,kind,body,status,created_at,package_hash) VALUES(?,?,?,?,1,0,?,?,?,?,?)',
                         (identifier, target['id'], fingerprint, 'digest:' + today.isoformat(), 'daily_digest', canonical(payload), 'pending', now, digest(items)))
