"""Independent APScheduler jobs with durable retry budgets and local incident records."""
from __future__ import annotations

from contextlib import closing
import datetime as dt
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import time

from collector.__main__ import InstanceLock
from collector.store import canonical, digest

from .config import JOBS


APPLICATION_ID = 0x43434131
SCHEMA = '''
CREATE TABLE IF NOT EXISTS jobs(name TEXT PRIMARY KEY,state TEXT NOT NULL,attempts INTEGER DEFAULT 0,
 next_run REAL DEFAULT 0,last_start REAL,last_success REAL,error TEXT,active_error TEXT,result TEXT);
CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY AUTOINCREMENT,job TEXT,kind TEXT,code TEXT,
 created_at REAL,last_seen REAL,occurrences INTEGER DEFAULT 1,external_status TEXT DEFAULT 'disabled');
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
CREATE TABLE IF NOT EXISTS listener(id INTEGER PRIMARY KEY CHECK(id=1),state TEXT DEFAULT 'idle',attempts INTEGER DEFAULT 0,
 next_run REAL DEFAULT 0,pid INTEGER,create_time REAL,command TEXT,error TEXT);
'''


def _connect(config, *, write=False):
    path = Path(config['storage']['state_file'])
    if not write and not path.exists():
        return None
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=10)
    else:
        conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=10)
    owner = conn.execute('PRAGMA application_id').fetchone()[0]
    if owner != APPLICATION_ID and (owner or conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone()):
        conn.close()
        raise ValueError('Automation requires its own state database')
    if write:
        conn.execute(f'PRAGMA application_id={APPLICATION_ID}')
        conn.executescript(SCHEMA)
        with conn:
            conn.executemany("INSERT OR IGNORE INTO jobs(name,state) VALUES(?,'idle')", [(name,) for name in JOBS])
            conn.execute('INSERT OR IGNORE INTO listener(id) VALUES(1)')
    conn.row_factory = sqlite3.Row
    return conn


def _setting(conn, key, value):
    conn.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, canonical(value)))


def _error(exc):
    code = getattr(exc, 'code', None)
    return code if isinstance(code, str) and re.fullmatch(r'[a-z][a-z0-9_]{0,100}', code) else type(exc).__name__


def _stats(result):
    if not isinstance(result, dict):
        return {}
    return {key: value for key, value in result.items()
            if isinstance(key, str) and re.fullmatch('[a-z_]{1,60}', key) and type(value) in (bool, int, float) and not (isinstance(value, float) and not math.isfinite(value))}


def _callback(config, name):
    path = config['jobs'][name]['config']
    if name in {'wechat', 'qq'}:
        from collector.config import load_config
        from collector.__main__ import run_round
        return run_round(load_config(path))
    if name == 'processor':
        from processor import load_config, run_round
        return run_round(load_config(path))
    from messaging import load_config
    settings = load_config(path)
    if name == 'feedback':
        from messaging.feedback import process_pending
        return process_pending(settings)
    from messaging import run_round, status
    result = run_round(settings)
    counts = status(settings)['delivery_counts']
    result['unresolved_deliveries'] = sum(counts.get(state, 0) for state in ('failed', 'unknown', 'retry'))
    return result


def check(config):
    """Configuration checks only: no database refresh, model call or network access."""
    checks = {}
    for name in JOBS:
        entry = config['jobs'][name]
        if not entry['enabled']:
            checks[name] = {'enabled': False, 'ready': False, 'reason': 'disabled'}
            continue
        try:
            if name in {'wechat', 'qq'}:
                from collector.config import load_config
                loaded = load_config(entry['config'])
                if loaded['platform'] != name:
                    checks[name] = {'enabled': True, 'ready': False, 'reason': 'collector_platform_mismatch'}
                    continue
            elif name == 'processor':
                from processor import load_config
                from processor.core import _model_info
                loaded = load_config(entry['config'])
                if not _model_info(loaded)['configured']:
                    checks[name] = {'enabled': True, 'ready': False, 'reason': 'processor_model_not_configured'}
                    continue
            else:
                from messaging import load_config
                loaded = load_config(entry['config'])
                if name == 'feedback' and not loaded.get('feedback', {}).get('enabled'):
                    checks[name] = {'enabled': True, 'ready': False, 'reason': 'feedback_not_enabled'}
                    continue
            checks[name] = {'enabled': True, 'ready': True}
        except Exception as exc:
            checks[name] = {'enabled': True, 'ready': False, 'reason': _error(exc)}
    for name, module in [('listener', 'messaging.feishu'), ('alerts', 'messaging.channels')]:
        entry = config[name]
        if not entry['enabled']:
            checks[name] = {'enabled': False, 'ready': False, 'reason': 'disabled'}
            continue
        try:
            from messaging import load_config
            loaded = load_config(entry['config'])
            ready = importlib.util.find_spec(module) is not None
            if name == 'alerts':
                ready = ready and loaded['enabled'] and any(t['id'] == entry['target_id'] and t['enabled'] and t['kind'] in {'feishu', 'feishu_app'} for t in loaded['targets'])
            checks[name] = {'enabled': True, 'ready': ready}
        except Exception as exc:
            checks[name] = {'enabled': True, 'ready': False, 'reason': _error(exc)}
    dependencies = {name: importlib.util.find_spec(name) is not None for name in ('apscheduler', 'psutil')}
    return {'enabled': config['enabled'], 'jobs': checks, 'dependencies': dependencies,
            'ready': all(dependencies.values()) and all(v['ready'] for v in checks.values() if v['enabled']),
            'warnings': ['Disable matching console schedules before enabling automation collectors.',
                         'Configuration changes require restarting automation; check performs no live API validation.']}


def status(config):
    conn = _connect(config)
    try:
        return {'enabled': config['enabled'],
                'jobs': [] if conn is None else [dict(row, enabled=config['jobs'][row['name']]['enabled']) for row in conn.execute('SELECT * FROM jobs ORDER BY name')],
                'alerts': [] if conn is None else [dict(row) for row in conn.execute('SELECT * FROM alerts ORDER BY id DESC LIMIT 100')],
                'listener': None if conn is None else dict(conn.execute('SELECT * FROM listener WHERE id=1').fetchone()),
                'service': {} if conn is None else {row['key']: json.loads(row['value']) for row in conn.execute('SELECT * FROM settings')}}
    finally:
        if conn is not None:
            conn.close()


def resume(config, name):
    if name not in (*JOBS, 'listener'):
        raise ValueError('Unknown job')
    with closing(_connect(config, write=True)) as conn, conn:
        if name == 'listener':
            row = conn.execute('SELECT state FROM listener WHERE id=1').fetchone()
            if row['state'] == 'running':
                raise ValueError('Listener is already running')
            conn.execute("UPDATE listener SET state='idle',attempts=0,next_run=?,error=NULL WHERE id=1", (time.time(),))
        else:
            row = conn.execute('SELECT state FROM jobs WHERE name=?', (name,)).fetchone()
            if row['state'] == 'running':
                raise ValueError('Job is already running')
            conn.execute("UPDATE jobs SET state='idle',attempts=0,next_run=?,error=NULL WHERE name=?", (time.time(), name))
    return {'job': name, 'resumed': True}


def request_stop(config):
    with closing(_connect(config, write=True)) as conn, conn:
        _setting(conn, 'stop_requested', True)
    return {'stop_requested': True, 'detail': 'Running jobs finish before shutdown.'}


def _send_alert(config, record):
    from messaging import load_config
    from messaging.channels import deliver
    settings = load_config(config['alerts']['config'])
    target = next((t for t in settings['targets'] if t['id'] == config['alerts']['target_id']), None)
    if not target or target['kind'] not in {'feishu', 'feishu_app'}:
        raise ValueError('Dedicated Feishu alert target is not configured')
    restored = record['kind'] == 'recovery'
    task = {'task_id': 'automation:' + record['job'], 'revision': record['id'],
            'title': ('自动任务恢复：' if restored else '自动任务需要检查：') + record['job'],
            'event': ('本轮执行成功。' if restored else '检查本地自动化状态；重试耗尽后需手动恢复。') + ' 代码：' + record['code'],
            'priority': 'P3' if restored else 'P1', 'status': 'open', 'time': {}, 'people': [], 'resources': [], 'sources': []}
    envelope = {'schema_version': 1, 'delivery_id': digest(['automation-alert', record['id'], record['created_at']]), 'kind': 'alert', 'task': task}
    return deliver(target, envelope, settings)


class SchedulerService:
    def __init__(self, config, *, callbacks=None, clock=time.time, scheduler=None, alert_sender=None):
        self.config, self.callbacks, self.clock = config, callbacks or {}, clock
        self.scheduler = scheduler
        self.alert_sender = alert_sender or _send_alert
        self.stop_event = threading.Event()
        self.lock = None
        self.listener = None

    def _alert(self, name, kind, code):
        now = self.clock()
        settings = self.config['alerts']
        if kind == 'recovery' and not settings['recovery_enabled']:
            return
        with closing(_connect(self.config, write=True)) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            recent = conn.execute('SELECT * FROM alerts WHERE job=? AND kind=? AND code=? ORDER BY id DESC LIMIT 1', (name, kind, code)).fetchone()
            if recent and now - recent['created_at'] < settings['cooldown_seconds']:
                conn.execute('UPDATE alerts SET occurrences=occurrences+1,last_seen=? WHERE id=?', (now, recent['id']))
                return
            cursor = conn.execute('INSERT INTO alerts(job,kind,code,created_at,last_seen) VALUES(?,?,?,?,?)', (name, kind, code, now, now))
            record = dict(conn.execute('SELECT * FROM alerts WHERE id=?', (cursor.lastrowid,)).fetchone())
        if not settings['enabled']:
            return
        try:
            self.alert_sender(self.config, record)
            outcome = 'sent'
        except Exception:
            outcome = 'failed_or_unknown'
        with closing(_connect(self.config, write=True)) as conn, conn:
            conn.execute('UPDATE alerts SET external_status=? WHERE id=?', (outcome, record['id']))

    def _failed(self, name, attempts, code):
        entry, now = self.config['jobs'][name], self.clock()
        exhausted = attempts >= entry['max_attempts']
        delay = min(entry['backoff_seconds'] * 2 ** max(0, attempts - 1), entry['max_backoff_seconds'])
        with closing(_connect(self.config, write=True)) as conn, conn:
            conn.execute('UPDATE jobs SET state=?,next_run=?,error=?,active_error=? WHERE name=?',
                         ('paused' if exhausted else 'retry', 0 if exhausted else now + delay, code, code, name))
        self._alert(name, 'failure', code)

    def execute(self, name, *, force=False):
        if name not in JOBS or not self.config['enabled'] or not self.config['jobs'][name]['enabled']:
            return {'job': name, 'state': 'disabled'}
        now = self.clock()
        with closing(_connect(self.config, write=True)) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM jobs WHERE name=?', (name,)).fetchone()
            if row['state'] in {'running', 'paused'} or (not force and row['next_run'] > now):
                return dict(row)
            attempts = row['attempts'] + 1
            conn.execute("UPDATE jobs SET state='running',attempts=?,last_start=? WHERE name=?", (attempts, now, name))
        try:
            result = self.callbacks[name]() if name in self.callbacks else _callback(self.config, name)
            if isinstance(result, dict) and result.get('status') == 'failed':
                raise RuntimeError('job_returned_failed')
            summary = _stats(result)
            attention = any(summary.get(key, 0) > 0 for key in ('failed', 'conflict', 'unknown', 'unresolved_deliveries'))
            code = 'downstream_attention' if attention else None
            with closing(_connect(self.config, write=True)) as conn, conn:
                conn.execute("UPDATE jobs SET state='idle',attempts=0,next_run=?,last_success=?,error=?,active_error=?,result=? WHERE name=?",
                             (self.clock() + self.config['jobs'][name]['interval_seconds'], self.clock(), code, code, canonical(summary), name))
            if attention:
                self._alert(name, 'failure', code)
            elif row['active_error']:
                self._alert(name, 'recovery', 'recovered')
        except Exception as exc:
            self._failed(name, attempts, _error(exc))
        with closing(_connect(self.config)) as conn:
            return dict(conn.execute('SELECT * FROM jobs WHERE name=?', (name,)).fetchone())

    def _reconcile(self):
        if self.stop_event.is_set():
            return
        with closing(_connect(self.config)) as conn:
            row = conn.execute("SELECT value FROM settings WHERE key='stop_requested'").fetchone()
            if row and json.loads(row['value']):
                self.stop_event.set()
                return
            rows = conn.execute('SELECT * FROM jobs').fetchall()
        for row in rows:
            if not self.config['jobs'][row['name']]['enabled'] or row['state'] in {'running', 'paused'}:
                continue
            scheduled = self.scheduler.get_job(row['name'])
            when = max(row['next_run'], self.clock())
            if scheduled is None:
                self.scheduler.add_job(self.execute, 'date', id=row['name'], args=[row['name']],
                    run_date=dt.datetime.fromtimestamp(when, dt.timezone.utc), max_instances=1, coalesce=True, misfire_grace_time=None)
            elif abs(scheduled.next_run_time.timestamp() - when) > 1 and row['next_run'] <= self.clock():
                self.scheduler.reschedule_job(row['name'], trigger='date', run_date=dt.datetime.fromtimestamp(when, dt.timezone.utc))

    def start(self):
        if not self.config['enabled']:
            raise ValueError('Automation is disabled; check configuration before enabling')
        self.lock = InstanceLock(self.config['storage']['state_file'])
        self.lock.__enter__()
        try:
            with closing(_connect(self.config, write=True)) as conn, conn:
                _setting(conn, 'stop_requested', False)
                _setting(conn, 'service', {'pid': os.getpid(), 'started_at': self.clock(), 'state': 'running'})
                interrupted = conn.execute("SELECT * FROM jobs WHERE state='running'").fetchall()
            for row in interrupted:
                self._failed(row['name'], row['attempts'], 'interrupted_execution')
            if self.scheduler is None:
                from apscheduler.schedulers.background import BackgroundScheduler
                from apscheduler.executors.pool import ThreadPoolExecutor
                self.scheduler = BackgroundScheduler(timezone=self.config['scheduler']['timezone'],
                    executors={'default': ThreadPoolExecutor(self.config['scheduler']['worker_threads'])},
                    job_defaults={'max_instances': 1, 'coalesce': True, 'misfire_grace_time': None})
            self.scheduler.add_job(self._reconcile, 'interval', id='_control',
                                   seconds=self.config['scheduler']['control_interval_seconds'], max_instances=1, coalesce=True)
            if self.config['listener']['enabled']:
                from .listener import ListenerSupervisor
                self.listener = ListenerSupervisor(self)
                self.scheduler.add_job(self.listener.poll, 'interval', id='_listener',
                    seconds=self.config['listener']['check_interval_seconds'], max_instances=1, coalesce=True)
            self.scheduler.start()
            self._reconcile()
        except Exception:
            try:
                if self.scheduler is not None and getattr(self.scheduler, 'running', False):
                    self.scheduler.shutdown(wait=True)
                with closing(_connect(self.config, write=True)) as conn, conn:
                    _setting(conn, 'service', {'pid': os.getpid(), 'stopped_at': self.clock(), 'state': 'start_failed'})
            finally:
                self.lock.__exit__(None, None, None)
                self.lock = None
            raise
        return self

    def stop(self):
        self.stop_event.set()
        try:
            if self.scheduler:
                self.scheduler.shutdown(wait=True)
            if self.listener:
                self.listener.stop()
            with closing(_connect(self.config, write=True)) as conn, conn:
                _setting(conn, 'service', {'pid': os.getpid(), 'stopped_at': self.clock(), 'state': 'stopped'})
        finally:
            if self.lock:
                self.lock.__exit__(None, None, None)
                self.lock = None


def run_once(config, name, *, callbacks=None, clock=time.time):
    if name not in JOBS:
        raise ValueError('once requires one explicit --job')
    with InstanceLock(config['storage']['state_file']):
        service = SchedulerService(config, callbacks=callbacks, clock=clock)
        return service.execute(name, force=True)
