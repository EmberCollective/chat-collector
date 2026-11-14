from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import threading
import uuid

import pytest

from automation import core, history, load_config, resume, run_once
from test_automation import Clock, FakeScheduler


@pytest.fixture
def config(tmp_path):
    path = tmp_path / 'automation.toml'
    path.write_text('''enabled=true
[storage]
state_file="automation.db"
[retry]
max_attempts=2
backoff_seconds=10
[jobs.wechat]
enabled=true
interval_seconds=60
''', encoding='utf-8')
    return load_config(path)


@pytest.mark.parametrize('manual', [False, True])
def test_retries_share_origin_and_durable_attempt_details(config, manual):
    clock = Clock()
    def fail():
        raise RuntimeError('PRIVATE_CHAT_AND_KEY')
    service = core.SchedulerService(config, callbacks={'wechat': fail}, clock=clock)
    assert service.execute('wechat', manual=manual)['state'] == 'retry'
    first = history.list_runs(config)[0]
    assert uuid.UUID(first['id']).hex == first['id']
    assert first['origin'] == ('manual' if manual else 'schedule')
    assert first['action'] == 'collect' and first['module'] == 'wechat'
    assert first['status'] == 'retry' and first['finished_at'] is None
    assert 'attempts' not in first
    assert service.execute('wechat')['state'] == 'retry'
    assert history.get_run(config, first['id'])['attempt_count'] == 1
    clock.value += 10
    service.callbacks['wechat'] = lambda: {'done': 2, 'chat': 'PRIVATE_TEXT', 'bad': float('nan')}
    assert service.execute('wechat')['state'] == 'idle'
    detail = history.get_run(config, first['id'])
    assert len(history.list_runs(config)) == 1
    assert detail['origin'] == first['origin']
    assert (detail['status'], detail['attempt_count'], detail['duration_seconds']) == ('succeeded', 2, 10)
    assert detail['result'] == {'done': 2} and detail['error'] is None
    assert [a['status'] for a in detail['attempts']] == ['failed', 'succeeded']
    assert detail['attempts'][0]['details']['failures'][0]['code'] == 'RuntimeError'
    assert detail['attempts'][0]['finished_at'] == 100
    assert detail['attempts'][1]['number'] == 2
    assert 'PRIVATE_' not in json.dumps(detail)
    assert history.list_runs(config, module='wechat', origin=first['origin'], status='succeeded') == [history.list_runs(config)[0]]
    assert history.list_runs(config, origin='missing') == []
    assert history.get_run(config, 'missing') is None


def test_exhaustion_resume_starts_new_episode_with_fresh_budget(config, monkeypatch):
    clock = Clock()
    service = core.SchedulerService(config, callbacks={'wechat': lambda: 1 / 0}, clock=clock)
    service.execute('wechat')
    clock.value = 110
    assert service.execute('wechat')['state'] == 'paused'
    failed = history.list_runs(config)[0]
    assert (failed['status'], failed['attempt_count'], failed['finished_at']) == ('failed', 2, 110)
    monkeypatch.setattr(core.time, 'time', clock)
    resume(config, 'wechat')
    service.callbacks['wechat'] = lambda: {'done': 1}
    assert service.execute('wechat')['state'] == 'idle'
    latest, previous = history.list_runs(config)
    assert latest['id'] != previous['id'] and latest['attempt_count'] == 1
    assert previous == failed


@pytest.mark.parametrize('exhausted', [False, True])
def test_restart_records_interruption_without_inventing_success(config, exhausted):
    clock = Clock()
    if exhausted:
        config['jobs']['wechat']['max_attempts'] = 1
    def crash():
        raise SystemExit()
    with pytest.raises(SystemExit):
        core.SchedulerService(config, callbacks={'wechat': crash}, clock=clock).execute('wechat', manual=True)
    identifier = history.list_runs(config)[0]['id']
    assert history.get_run(config, identifier)['status'] == 'running'
    clock.value = 105
    service = core.SchedulerService(config, scheduler=FakeScheduler(), clock=clock).start()
    try:
        detail = history.get_run(config, identifier)
        assert detail['status'] == ('interrupted' if exhausted else 'retry')
        assert detail['attempts'][0]['status'] == 'interrupted'
        assert detail['attempts'][0]['error'] == 'interrupted_execution'
        assert detail['attempts'][0]['finished_at'] == 105
        assert detail['origin'] == 'manual'
        if not exhausted:
            service.callbacks['wechat'] = lambda: {'done': 1}
            clock.value = 115
            service.execute('wechat')
            assert history.get_run(config, identifier)['status'] == 'succeeded'
            assert history.get_run(config, identifier)['attempt_count'] == 2
    finally:
        service.stop()


def test_read_only_queries_do_not_create_or_migrate_database(config):
    path = Path(config['storage']['state_file'])
    assert history.list_runs(config) == []
    assert history.get_run(config, 'missing') is None
    assert not path.exists()
    with sqlite3.connect(path) as conn:
        conn.executescript(core.SCHEMA)
        conn.execute('ALTER TABLE jobs ADD COLUMN wake_seq INTEGER NOT NULL DEFAULT 0')
        conn.execute('ALTER TABLE jobs ADD COLUMN wake_consumed INTEGER NOT NULL DEFAULT 0')
        conn.execute(f'PRAGMA application_id={core.APPLICATION_ID}')
        conn.execute('PRAGMA user_version=1')
        conn.execute("INSERT INTO jobs(name,state,attempts) VALUES('wechat','retry',1)")
        conn.execute("INSERT INTO settings VALUES('retained','true')")
        conn.execute("INSERT INTO operations(id,kind,job,state) VALUES('pending','once','qq','queued')")
    before = path.read_bytes()
    assert history.list_runs(config) == [] and history.get_run(config, 'missing') is None
    assert before == path.read_bytes()
    with closing(core._connect(config, write=True)) as conn:
        assert tuple(conn.execute("SELECT state,attempts FROM jobs WHERE name='wechat'").fetchone()) == ('retry', 1)
        assert conn.execute("SELECT value FROM settings WHERE key='retained'").fetchone()[0] == 'true'
        assert conn.execute("SELECT state FROM operations WHERE id='pending'").fetchone()[0] == 'queued'
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 2
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert history.list_runs(config) == []


def test_high_frequency_cleanup_bounds_terminal_runs_but_keeps_retry(config, monkeypatch):
    monkeypatch.setattr(history, 'MAX_TERMINAL_RUNS', 3)
    clock = Clock()
    service = core.SchedulerService(config, callbacks={'wechat': lambda: 1 / 0, 'feedback': lambda: {'done': 1}}, clock=clock)
    service.execute('wechat')
    identifier = history.list_runs(config, limit=3)[0]['id']
    for _ in range(8):
        clock.value += 1
        service.execute('feedback', manual=True)
    with closing(core._connect(config)) as conn:
        assert conn.execute('SELECT COUNT(*) FROM runs WHERE finished_at IS NOT NULL').fetchone()[0] == 3
        assert conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0] == 4
        assert conn.execute('SELECT COUNT(*) FROM run_attempts').fetchone()[0] == 4
    assert history.get_run(config, identifier)['status'] == 'retry'


def test_callback_completion_is_committed_before_alert_failure(config, monkeypatch):
    operation = {'id': 'op'}
    with closing(core._connect(config, write=True)) as conn, conn:
        conn.execute("INSERT INTO operations(id,kind,job,state) VALUES(?,'once','wechat','queued')", (operation['id'],))
    service = core.SchedulerService(config, callbacks={'wechat': lambda: {'failed': 1}}, clock=Clock())
    def fail_alert(*args):
        assert history.list_runs(config)[0]['status'] == 'warning'
        raise sqlite3.OperationalError('PRIVATE_ALERT')
    monkeypatch.setattr(service, '_alert', fail_alert)
    with pytest.raises(sqlite3.OperationalError):
        service.execute('wechat', operation_id=operation['id'])
    assert history.list_runs(config)[0]['status'] == 'warning'
    with closing(core._connect(config)) as conn:
        assert conn.execute("SELECT state FROM jobs WHERE name='wechat'").fetchone()[0] == 'idle'
        assert conn.execute("SELECT state FROM operations WHERE id='op'").fetchone()[0] == 'succeeded'


def test_concurrent_execute_creates_one_episode_and_running_attempt(config):
    entered, release = threading.Event(), threading.Event()
    def work():
        entered.set()
        assert release.wait(3)
        return {'done': 1}
    service = core.SchedulerService(config, callbacks={'wechat': work}, clock=Clock())
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(service.execute, 'wechat')
        assert entered.wait(2)
        try:
            assert pool.submit(service.execute, 'wechat').result(timeout=2)['state'] == 'running'
            rows = history.list_runs(config)
            assert len(rows) == 1 and rows[0]['attempt_count'] == 1
            assert history.get_run(config, rows[0]['id'])['attempts'][0]['finished_at'] is None
        finally:
            release.set()
        assert first.result(timeout=2)['state'] == 'idle'


def test_once_records_manual_origin_and_limit_validation(config):
    run_once(config, 'wechat', callbacks={'wechat': lambda: {'done': 1}}, clock=Clock())
    assert history.list_runs(config)[0]['origin'] == 'manual'
    for limit in (0, -1, 1001, True, '2'):
        with pytest.raises(ValueError):
            history.list_runs(config, limit=limit)


def test_manual_worker_crash_detection_finishes_history(config, monkeypatch):
    from automation import control
    def crash():
        raise SystemExit()
    operation = control._new_operation(config, 'once', 'wechat')
    with pytest.raises(SystemExit):
        core.SchedulerService(config, callbacks={'wechat': crash}, clock=Clock()).execute('wechat', operation_id=operation['id'])
    monkeypatch.setattr(control, 'status', lambda config: {'state': 'stopped'})
    assert control._active(config) == []
    detail = history.get_run(config, history.list_runs(config)[0]['id'])
    assert detail['origin'] == 'manual' and detail['status'] == 'interrupted'
    assert detail['attempts'][0]['status'] == 'interrupted'
