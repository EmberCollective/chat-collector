from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import threading

import pytest

from automation import SchedulerService, check, load_config, request_stop, resume, run_once, status
from automation import core


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv('TEST_AUTOMATION_APP', 'cli_automation')
    (tmp_path / 'messaging.toml').write_text('''enabled=true
[[targets]]
id="custom-team"
kind="feishu_app"
enabled=true
app_id_env="TEST_AUTOMATION_APP"
''', encoding='utf-8')
    path = tmp_path / 'automation.toml'
    path.write_text('''enabled = true
[storage]
state_file = "automation.db"
[retry]
max_attempts = 3
backoff_seconds = 10
max_backoff_seconds = 15
[jobs.wechat]
enabled = true
interval_seconds = 60
[jobs.qq]
enabled = true
interval_seconds = 30
''', encoding='utf-8')
    return load_config(path)


class Clock:
    value = 100
    def __call__(self):
        return self.value


class FakeScheduler:
    def __init__(self):
        self.jobs, self.started, self.stopped = {}, False, False
    def add_job(self, callback, trigger, **options):
        self.jobs[options['id']] = type('Job', (), {'next_run_time': options.get('run_date')})()
    def get_job(self, name):
        return self.jobs.get(name)
    def reschedule_job(self, name, **options):
        self.jobs[name].next_run_time = options['run_date']
    def start(self):
        self.started = True
    def shutdown(self, wait=True):
        self.stopped = wait


def job(config, name='wechat'):
    return next(row for row in status(config)['jobs'] if row['name'] == name)


def test_retry_is_bounded_persisted_and_does_not_restart_next_regular_period(config):
    calls, clock = [], Clock()
    def fail():
        calls.append(clock())
        raise RuntimeError('PRIVATE_TOKEN_and_chat_text')
    service = SchedulerService(config, callbacks={'wechat': fail}, clock=clock)
    assert service.execute('wechat')['state'] == 'retry'
    assert job(config)['next_run'] == 110
    clock.value = 109
    service.execute('wechat')
    assert len(calls) == 1
    clock.value = 110
    service.execute('wechat')
    assert job(config)['next_run'] == 125
    clock.value = 125
    assert service.execute('wechat')['state'] == 'paused'
    clock.value = 10000
    assert SchedulerService(config, callbacks={'wechat': fail}, clock=clock).execute('wechat')['state'] == 'paused'
    assert len(calls) == 3
    assert 'PRIVATE_TOKEN' not in json.dumps(status(config))


def test_explicit_resume_resets_budget_and_success_emits_one_recovery(config, monkeypatch):
    clock = Clock()
    config['jobs']['wechat']['max_attempts'] = 1
    service = SchedulerService(config, callbacks={'wechat': lambda: (_ for _ in ()).throw(ValueError('fail'))}, clock=clock)
    service.execute('wechat')
    assert job(config)['state'] == 'paused'
    monkeypatch.setattr(core.time, 'time', clock)
    resume(config, 'wechat')
    service.callbacks['wechat'] = lambda: {'processed_groups': 3, 'pending_groups': 9}
    assert service.execute('wechat')['state'] == 'idle'
    assert job(config)['attempts'] == 0
    assert [a['kind'] for a in status(config)['alerts']] == ['recovery', 'failure']
    clock.value += 60
    service.execute('wechat')
    assert len(status(config)['alerts']) == 2


def test_one_job_failure_does_not_block_another(config):
    service = SchedulerService(config, callbacks={'wechat': lambda: 1 / 0, 'qq': lambda: {'emitted': 2}}, clock=Clock())
    assert service.execute('wechat')['state'] == 'retry'
    assert service.execute('qq')['state'] == 'idle'
    assert json.loads(job(config, 'qq')['result']) == {'emitted': 2}


def test_real_sqlite_contention_keeps_safe_diagnostics_after_recovery(config, tmp_path):
    path = tmp_path / 'PRIVATE_DATABASE_NAME.db'
    owner = sqlite3.connect(path)
    owner.execute('CREATE TABLE private_messages(body TEXT)')
    owner.execute('BEGIN IMMEDIATE')
    clock = Clock()
    def write():
        connection = sqlite3.connect(path, timeout=0)
        try:
            connection.execute("INSERT INTO private_messages VALUES('PRIVATE_CHAT_CONTENT')")
            connection.commit()
        finally:
            connection.close()
        return {'sent': 0}
    service = SchedulerService(config, callbacks={'wechat': write}, clock=clock)
    try:
        result = service.execute('wechat')
        assert result['state'] == 'retry'
        assert result['error'] == 'sqlite_busy'
        recorded = status(config)['service']['last_failure:wechat']
        assert recorded['at'] == 100
        detail = recorded['failures'][0]
        assert detail['sqlite_errorcode'] == sqlite3.SQLITE_BUSY
        assert detail['frames'][-1]['file'] == 'tests/test_automation.py'
        assert detail['frames'][-1]['function'] == 'write'
        assert 'PRIVATE_' not in json.dumps(status(config))
        owner.rollback()
        clock.value = 110
        assert service.execute('wechat')['state'] == 'idle'
        assert job(config)['error'] is None
        assert status(config)['service']['last_failure:wechat'] == recorded
    finally:
        owner.close()


def test_diagnostics_explain_wrapped_failure_without_sql_or_exception_text(config):
    def fail():
        try:
            with sqlite3.connect(':memory:') as connection:
                connection.execute('SELECT PRIVATE_SQL_TOKEN FROM missing_table')
        except sqlite3.Error as exc:
            raise RuntimeError('PRIVATE_CHAT_AND_API_KEY') from exc
    result = SchedulerService(config, callbacks={'wechat': fail}, clock=Clock()).execute('wechat')
    assert result['error'] == 'RuntimeError'
    details = status(config)['service']['last_failure:wechat']
    assert [item['code'] for item in details['failures']] == ['RuntimeError', 'sqlite_error']
    assert 'PRIVATE_' not in json.dumps(status(config))


def test_status_reads_last_commit_during_another_worker_write(config):
    with closing(core._connect(config, write=True)) as writer:
        assert writer.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
        writer.execute('BEGIN EXCLUSIVE')
        writer.execute("UPDATE jobs SET error='uncommitted' WHERE name='wechat'")
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(status, config)
            try:
                snapshot = future.result(timeout=2)
                assert next(row for row in snapshot['jobs'] if row['name'] == 'wechat')['error'] is None
            finally:
                writer.rollback()


def test_legacy_automation_migration_preserves_state_and_serializes_initializers(config):
    path = config['storage']['state_file']
    with sqlite3.connect(path) as conn:
        conn.executescript(core.SCHEMA)
        conn.execute(f'PRAGMA application_id={core.APPLICATION_ID}')
        conn.execute("INSERT INTO jobs(name,state,attempts) VALUES('wechat','paused',3)")
        conn.execute("INSERT INTO settings VALUES('retained','true')")
    def initialize(_):
        with closing(core._connect(config, write=True)) as conn:
            return tuple(conn.execute("SELECT state,attempts FROM jobs WHERE name='wechat'").fetchone())
    with ThreadPoolExecutor(8) as pool:
        assert list(pool.map(initialize, range(16))) == [('paused', 3)] * 16
    assert status(config)['service']['retained'] is True
    with sqlite3.connect(path) as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == core.SCHEMA_VERSION
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


def test_concurrent_requests_never_execute_same_job_twice(config):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def work():
        calls.append(1)
        entered.set()
        assert release.wait(3)
        return {'done': 1}
    service = SchedulerService(config, callbacks={'wechat': work}, clock=Clock())
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(service.execute, 'wechat')
        assert entered.wait(2)
        second = pool.submit(service.execute, 'wechat')
        assert second.result(timeout=2)['state'] == 'running'
        release.set()
        assert first.result(timeout=2)['state'] == 'idle'
    assert calls == [1]


def test_alert_cooldown_and_reporting_failure_dont_recursively_retry(config):
    clock, reports = Clock(), []
    config['alerts'].update(enabled=True, cooldown_seconds=20)
    def report(*args):
        reports.append(1)
        raise RuntimeError('sensitive webhook token')
    service = SchedulerService(config, callbacks={'wechat': lambda: 1 / 0}, clock=clock, alert_sender=report)
    service.execute('wechat')
    clock.value = 110
    service.execute('wechat')
    alerts = status(config)['alerts']
    assert len(alerts) == 1 and alerts[0]['occurrences'] == 2
    assert alerts[0]['external_status'] == 'failed_or_unknown'
    assert reports == [1]
    clock.value = 125
    service.execute('wechat')
    assert len(status(config)['alerts']) == 2


def test_downstream_attention_reports_without_nested_retries(config):
    config['jobs']['messaging']['enabled'] = True
    clock = Clock()
    service = SchedulerService(config, callbacks={'messaging': lambda: {'unknown': 1, 'failed': 2, 'retry': 3}}, clock=clock)
    result = service.execute('messaging')
    assert result['state'] == 'idle' and result['attempts'] == 0
    assert result['error'] == 'downstream_attention'
    service.callbacks['messaging'] = lambda: {'unknown': 0, 'failed': 0, 'retry': 1}
    clock.value += 60
    service.execute('messaging')
    assert status(config)['alerts'][0]['kind'] == 'recovery'


@pytest.mark.parametrize('delivery_state', ['failed', 'unknown', 'retry'])
def test_messaging_empty_round_cannot_report_recovery_before_persisted_fault_resolved(config, tmp_path, delivery_state):
    import messaging
    from messaging import core as message_core
    path = tmp_path / 'messaging.toml'
    path.write_text('''enabled=true
[input]
batches_dir="incoming"
[storage]
state_file="messages.db"
assets_dir="assets"
[[targets]]
id="notify"
kind="feishu"
enabled=false
''', encoding='utf-8')
    (tmp_path / 'incoming').mkdir()
    settings = messaging.load_config(path)
    with message_core._connect(settings, True) as conn:
        conn.execute('INSERT INTO deliveries(id,target_id,task_id,kind,body,status,created_at,error) VALUES(?,?,?,?,?,?,?,?)',
                     ('d1', 'notify', 'task1', 'upsert', '{}', delivery_state, 1, 'feishu_material_delivery_failed'))
    config['jobs']['messaging'].update(enabled=True, config=str(path))
    clock = Clock()
    service = SchedulerService(config, clock=clock)
    first = service.execute('messaging')
    assert first['active_error'] == 'downstream_attention'
    first_summary = json.loads(first['result'])
    assert first_summary['failed'] == first_summary['unknown'] == 0
    assert first_summary['unresolved_deliveries'] == 1
    clock.value += 60
    second = service.execute('messaging')
    assert second['active_error'] == 'downstream_attention'
    assert not any(a['kind'] == 'recovery' for a in status(config)['alerts'])
    with message_core._connect(settings, True) as conn:
        conn.execute("UPDATE deliveries SET status='sent',error=NULL WHERE id='d1'")
    clock.value += 60
    third = service.execute('messaging')
    assert third['active_error'] is None
    assert json.loads(third['result'])['unresolved_deliveries'] == 0
    assert [a['kind'] for a in status(config)['alerts']].count('recovery') == 1
    clock.value += 60
    service.execute('messaging')
    assert [a['kind'] for a in status(config)['alerts']].count('recovery') == 1


def test_disabled_check_and_once_never_call_callbacks_or_network(config):
    config['enabled'] = False
    for entry in config['jobs'].values():
        entry['enabled'] = False
    assert check(config)['ready']
    assert not Path(config['storage']['state_file']).exists()
    result = run_once(config, 'processor', callbacks={'processor': lambda: pytest.fail('Model must not run')})
    assert result['state'] == 'disabled'


def test_check_rejects_multiple_listener_apps_before_start(config, monkeypatch):
    import messaging
    from messaging import feishu
    for entry in config['jobs'].values():
        entry['enabled'] = False
    config['listener']['enabled'] = True
    monkeypatch.setenv('APP_ONE', 'cli_one')
    monkeypatch.setenv('APP_TWO', 'cli_two')
    targets = [{'id': 'team-one', 'kind': 'feishu_app', 'enabled': True, 'app_id_env': 'APP_ONE'},
               {'id': 'team-two', 'kind': 'feishu_app', 'enabled': True, 'app_id_env': 'APP_TWO'}]
    monkeypatch.setattr(messaging, 'load_config', lambda path: {'enabled': True, 'targets': targets})
    monkeypatch.setattr(feishu, 'check', lambda *args: {'configured': True})
    result = check(config)
    assert not result['ready']
    assert result['jobs']['listener']['reason'] == 'feishu_listener_multiple_apps_not_supported'
    targets.pop()
    assert check(config)['ready']
    monkeypatch.setattr(feishu, 'check', lambda *args: {'configured': False})
    assert not check(config)['ready']


def test_scheduler_recovery_counts_interrupted_execution_and_prevents_duplicate_instances(config):
    clock = Clock()
    with core._connect(config, write=True) as conn:
        conn.execute("UPDATE jobs SET state='running',attempts=2 WHERE name='wechat'")
    scheduler = FakeScheduler()
    service = SchedulerService(config, scheduler=scheduler, clock=clock).start()
    try:
        assert job(config)['state'] == 'retry'
        assert job(config)['attempts'] == 2
        assert scheduler.get_job('wechat').next_run_time.timestamp() == 115
        other = SchedulerService(config, scheduler=FakeScheduler(), clock=clock)
        with pytest.raises(RuntimeError, match='Another collector'):
            other.start()
        request_stop(config)
        service._reconcile()
        assert service.stop_event.is_set()
    finally:
        service.stop()
    assert scheduler.stopped


def test_resume_is_seen_by_control_loop_without_service_restart(config, monkeypatch):
    clock = Clock()
    config['jobs']['wechat']['max_attempts'] = 1
    service = SchedulerService(config, callbacks={'wechat': lambda: 1 / 0}, scheduler=FakeScheduler(), clock=clock)
    service.execute('wechat')
    service.start()
    try:
        assert service.scheduler.get_job('wechat') is None
        monkeypatch.setattr(core.time, 'time', clock)
        resume(config, 'wechat')
        service._reconcile()
        assert service.scheduler.get_job('wechat') is not None
    finally:
        service.stop()


def test_invalid_config_and_other_database_are_not_overwritten(tmp_path, config):
    bad = tmp_path / 'bad.toml'
    bad.write_text('[jobs.qq]\nmax_attempts=0\n', encoding='utf-8')
    with pytest.raises(ValueError):
        load_config(bad)
    with sqlite3.connect(config['storage']['state_file']) as conn:
        conn.execute('CREATE TABLE tasks(body TEXT)')
    original = Path(config['storage']['state_file']).read_bytes()
    with pytest.raises(ValueError, match='own state'):
        run_once(config, 'wechat', callbacks={'wechat': lambda: {}})
    assert Path(config['storage']['state_file']).read_bytes() == original


def test_listener_restart_budget_is_not_reset_by_monitor_poll(config, monkeypatch):
    from automation.listener import ListenerSupervisor
    import automation.listener as module
    clock = Clock()
    service = SchedulerService(config, clock=clock)
    with core._connect(config, write=True):
        pass
    supervisor = ListenerSupervisor(service)
    monkeypatch.setattr(module.psutil, 'process_iter', lambda *args: [])
    monkeypatch.setattr(module.subprocess, 'Popen', lambda *args, **kwargs: (_ for _ in ()).throw(OSError('fail')))
    supervisor.poll()
    assert status(config)['listener']['attempts'] == 1
    clock.value = 110
    supervisor.poll()
    assert status(config)['listener']['attempts'] == 2
    clock.value = 125
    supervisor.poll()
    assert status(config)['listener']['state'] == 'paused'
    clock.value = 999
    supervisor.poll()
    assert status(config)['listener']['attempts'] == 3


@pytest.mark.parametrize('legacy', [True, False])
def test_listener_recovers_unrecorded_child_instead_of_launching_twice(config, monkeypatch, legacy):
    from automation.listener import ListenerSupervisor
    import automation.listener as module
    service = SchedulerService(config, clock=Clock())
    with core._connect(config, write=True) as conn:
        conn.execute("UPDATE listener SET state='starting',attempts=1 WHERE id=1")
    supervisor = ListenerSupervisor(service)
    command = [module.sys.executable, '-m', 'messaging.feishu', 'listen', '--config', config['listener']['config'], '--stop-file', str(supervisor.stop_file)]
    if not legacy:
        command += ['--target', 'custom-team']
    process = type('Process', (), {'pid': 123, 'info': {'cmdline': command, 'create_time': 1.0}})()
    monkeypatch.setattr(module.psutil, 'process_iter', lambda *args: [process])
    monkeypatch.setattr(module.subprocess, 'Popen', lambda *args, **kwargs: pytest.fail('Must adopt existing child'))
    supervisor.poll()
    assert status(config)['listener']['state'] == 'running'
    assert status(config)['listener']['pid'] == 123
    assert json.loads(status(config)['listener']['command']) == command


def test_listener_reports_multiple_apps_before_spawning(config, monkeypatch):
    from automation.listener import ListenerSupervisor
    import automation.listener as module
    message_config = Path(config['listener']['config'])
    message_config.write_text('''enabled=true
[[targets]]
id="custom-team"
kind="feishu_app"
enabled=true
app_id_env="LISTENER_APP_ONE"
[[targets]]
id="another-app"
kind="feishu_app"
enabled=true
app_id_env="LISTENER_APP_TWO"
''', encoding='utf-8')
    monkeypatch.setenv('LISTENER_APP_ONE', 'cli_one')
    monkeypatch.setenv('LISTENER_APP_TWO', 'cli_two')
    service = SchedulerService(config, clock=Clock())
    with core._connect(config, write=True):
        pass
    monkeypatch.setattr(module.psutil, 'process_iter', lambda *args: [])
    monkeypatch.setattr(module.subprocess, 'Popen', lambda *a, **kw: pytest.fail('Must reject ambiguous apps'))
    ListenerSupervisor(service).poll()
    listener = status(config)['listener']
    assert listener['state'] == 'paused'
    assert listener['error'] == 'feishu_listener_multiple_apps_not_supported'


def test_listener_spawns_custom_target_explicitly(config, monkeypatch):
    from types import SimpleNamespace
    from automation.listener import ListenerSupervisor
    import automation.listener as module
    service = SchedulerService(config, clock=Clock())
    with core._connect(config, write=True):
        pass
    commands = []
    monkeypatch.setattr(module.psutil, 'process_iter', lambda *args: [])
    monkeypatch.setattr(module.psutil, 'Process', lambda pid: SimpleNamespace(create_time=lambda: 1.0))
    monkeypatch.setattr(module.subprocess, 'Popen', lambda command, **kw:
                        commands.append(command) or SimpleNamespace(pid=123))
    ListenerSupervisor(service).poll()
    assert commands[0][-2:] == ['--target', 'custom-team']
    assert status(config)['listener']['state'] == 'running'


def test_running_listener_reports_new_app_configuration_without_killing_or_duplicating(config, monkeypatch):
    from automation.listener import ListenerSupervisor
    from messaging.management import read_settings, save_settings
    import automation.listener as module
    service = SchedulerService(config, clock=Clock())
    with core._connect(config, write=True) as conn:
        conn.execute("UPDATE listener SET state='running',pid=123,attempts=1 WHERE id=1")
    supervisor, alerts = ListenerSupervisor(service), []
    monkeypatch.setattr(supervisor, '_owned', lambda row: object())
    monkeypatch.setattr(service, '_alert', lambda *args: alerts.append(args))
    monkeypatch.setattr(module.subprocess, 'Popen', lambda *a, **k: pytest.fail('Must retain existing connection'))
    supervisor.poll()
    path = config['listener']['config']
    monkeypatch.setenv('OTHER_LISTENER_APP', 'cli_other')
    current = read_settings(path)
    save_settings(path, {'targets': [{'id': 'other-app', 'kind': 'feishu_app', 'enabled': True,
        'app_id_env': 'OTHER_LISTENER_APP', 'app_secret_env': 'OTHER_LISTENER_SECRET',
        'recipient_id': 'oc_other', 'allowed_operator_ids': ['ou_tester']}]}, current['version'])
    supervisor.poll()
    supervisor.poll()
    listener = status(config)['listener']
    assert (listener['state'], listener['pid'], listener['attempts']) == ('running', 123, 1)
    assert listener['error'] == 'feishu_listener_multiple_apps_not_supported'
    assert alerts == [('listener', 'failure', 'feishu_listener_multiple_apps_not_supported')]
    assert not supervisor.stop_file.exists()
    save_settings(path, {'targets': [{'id': 'other-app', 'enabled': False}]}, read_settings(path)['version'])
    supervisor.poll()
    assert status(config)['listener']['error'] is None
    assert alerts[-1] == ('listener', 'recovery', 'listener_configuration_restored')


def test_listener_stop_timeout_keeps_process_for_graceful_shutdown(config, monkeypatch):
    from automation.listener import ListenerSupervisor
    import automation.listener as module
    service = SchedulerService(config, clock=Clock())
    with core._connect(config, write=True) as conn:
        conn.execute("UPDATE listener SET state='running',attempts=1,pid=123 WHERE id=1")
    supervisor = ListenerSupervisor(service)
    class Process:
        def wait(self, timeout):
            raise module.psutil.TimeoutExpired(timeout)
    monkeypatch.setattr(supervisor, '_owned', lambda row: Process())
    supervisor.stop()
    assert supervisor.stop_file.exists()
    assert status(config)['listener']['state'] == 'stopping'
    assert status(config)['listener']['pid'] == 123


def test_unverifiable_listener_identity_pauses_instead_of_starting_another(config, monkeypatch):
    from automation.listener import ListenerSupervisor
    import automation.listener as module
    service = SchedulerService(config, clock=Clock())
    with core._connect(config, write=True) as conn:
        conn.execute("UPDATE listener SET state='running',pid=123 WHERE id=1")
    supervisor = ListenerSupervisor(service)
    monkeypatch.setattr(supervisor, '_owned', lambda row: (_ for _ in ()).throw(ValueError('unverifiable')))
    monkeypatch.setattr(module.subprocess, 'Popen', lambda *args, **kwargs: pytest.fail('Must not duplicate'))
    supervisor.poll()
    assert status(config)['listener']['state'] == 'paused'


def test_real_apscheduler_runs_independent_jobs_without_a_handwritten_poll_loop(config):
    from apscheduler.schedulers.background import BackgroundScheduler
    first, second = threading.Event(), threading.Event()
    callbacks = {'wechat': lambda: first.set(), 'qq': lambda: second.set()}
    service = SchedulerService(config, callbacks=callbacks, scheduler=BackgroundScheduler()).start()
    try:
        assert first.wait(3) and second.wait(3)
    finally:
        service.stop()
    assert job(config)['last_success'] is not None
