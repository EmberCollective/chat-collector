from contextlib import closing
import json
from pathlib import Path
import time

import pytest

from automation import core, load_config
from automation import control


@pytest.fixture
def config(tmp_path):
    path = tmp_path / 'automation.toml'
    path.write_text('enabled=true\n[storage]\nstate_file="state.db"\n[scheduler]\ncontrol_interval_seconds=1\n', encoding='utf-8')
    return load_config(path)


def fake_record(config, pid=900):
    return {'pid': pid, 'create_time': 5., 'command': ['python.exe', '-m', 'automation', 'run', '--config', config['_config_path']],
            'config_hash': control.config_digest(config)}


def save_service(config, record):
    with closing(core._connect(config, write=True)) as conn, conn:
        core._setting(conn, 'service', dict(record, state='running'))


def test_stale_database_running_does_not_claim_process_alive(config, monkeypatch):
    save_service(config, fake_record(config))
    monkeypatch.setattr(control, '_owned', lambda *args, **kwargs: False)
    monkeypatch.setattr(control, '_discover', lambda *args, **kwargs: [])
    result = control.status(config)
    assert result['state'] == 'stopped'
    assert not result['running'] and not result['verified']


@pytest.mark.parametrize('mismatch', ['created', 'command', 'config', 'access'])
def test_identity_rejects_pid_reuse_wrong_command_and_access_denied(config, monkeypatch, mismatch):
    record = fake_record(config)
    class Process:
        def create_time(self):
            return 6. if mismatch == 'created' else 5.
        def cmdline(self):
            if mismatch == 'access':
                raise control.psutil.AccessDenied()
            return ['python.exe', 'unrelated.py'] if mismatch == 'command' else record['command']
        def cwd(self):
            return config['_root']
        def is_running(self):
            return True
        def status(self):
            return 'running'
    monkeypatch.setattr(control.psutil, 'Process', lambda pid: Process())
    if mismatch == 'config':
        record['command'][-1] = str(Path(config['_config_path']).with_name('other.toml'))
    assert control._owned(record, config) is (None if mismatch == 'access' else False)


def test_start_is_idempotent_and_reports_config_change(config, monkeypatch):
    record = fake_record(config)
    save_service(config, record)
    monkeypatch.setattr(control, '_owned', lambda *args, **kwargs: True)
    monkeypatch.setattr(control, '_spawn', lambda *args: pytest.fail('already running'))
    assert control.start(config)['state'] == 'running'
    assert not control.status(config)['config_changed']
    Path(config['_config_path']).write_text('enabled=false\n', encoding='utf-8')
    assert control.status(config)['config_changed']


def test_start_refuses_active_setup_listener(config, monkeypatch):
    monkeypatch.setattr(control, '_discover', lambda config, setup=False: [fake_record(config)] if setup else [])
    monkeypatch.setattr(control, '_spawn', lambda *args: pytest.fail('setup owns listener'))
    assert control.start(config)['state'] == 'setup_listener_active'


def test_spawn_is_hidden_detached_and_supplies_runtime_dependencies(config, monkeypatch):
    captured = {}
    def spawn(command, **kwargs):
        captured.update(command=command, **kwargs)
        return type('Child', (), {'pid': 900})()
    monkeypatch.setattr(control.subprocess, 'Popen', spawn)
    monkeypatch.setattr(control, 'process_record', lambda pid: fake_record(config, pid))
    control._spawn(config, ['automation', 'run'])
    assert captured['stdin'] == captured['stdout'] == captured['stderr'] == control.subprocess.DEVNULL
    assert captured['creationflags'] == getattr(control.subprocess, 'CREATE_NO_WINDOW', 0)
    assert 'runtime-deps' in captured['env']['PYTHONPATH']
    assert captured['command'][-1] == config['_config_path']


def test_manual_request_runs_disabled_job_once_and_preserves_flags(config, monkeypatch):
    config['enabled'] = False
    monkeypatch.setattr(control, '_discover', lambda *args, **kwargs: [])
    monkeypatch.setattr(control, '_spawn', lambda *args: fake_record(config))
    calls = []
    monkeypatch.setattr(core, '_callback', lambda settings, name: calls.append(name) or {'processed': 1})
    receipt = control.trigger(config, 'processor')
    control.worker(config, receipt['operation']['id'])
    assert calls == ['processor']
    assert not config['enabled'] and not config['jobs']['processor']['enabled']
    assert control._get_operation(config, receipt['operation']['id'])['state'] == 'succeeded'
    control.worker(config, receipt['operation']['id'])
    assert calls == ['processor']


def test_active_service_queue_deduplicates_and_does_not_execute_in_request(config, monkeypatch):
    save_service(config, fake_record(config))
    monkeypatch.setattr(control, '_owned', lambda *args, **kwargs: True)
    monkeypatch.setattr(control, '_spawn', lambda *args: pytest.fail('running service consumes request'))
    first = control.trigger(config, 'processor')
    second = control.trigger(config, 'processor')
    assert first['operation']['id'] == second['operation']['id']
    calls = []
    service = core.SchedulerService(config, callbacks={'processor': lambda: calls.append(1) or {}})
    assert service.execute('processor')['state'] == 'idle'
    assert calls == [1]
    assert service.execute('processor')['state'] == 'disabled'


def test_paused_job_requires_resume(config, monkeypatch):
    monkeypatch.setattr(control, '_discover', lambda *args, **kwargs: [])
    with closing(core._connect(config, write=True)) as conn, conn:
        conn.execute("UPDATE jobs SET state='paused' WHERE name='processor'")
    assert control.trigger(config, 'processor')['state'] == 'paused'
    core.resume(config, 'processor')
    monkeypatch.setattr(control, '_spawn', lambda *args: fake_record(config))
    assert control.trigger(config, 'processor')['state'] == 'queued'


def test_stop_cancels_restart_and_queued_once_without_killing(config, monkeypatch):
    monkeypatch.setattr(control, '_discover', lambda *args, **kwargs: [])
    monkeypatch.setattr(control, '_spawn', lambda *args: pytest.fail('cancelled operation must not start'))
    operation = control._new_operation(config, 'restart')
    one = control._new_operation(config, 'once', 'processor')
    assert control.stop(config)['stop_requested']
    control.worker(config, operation['id'])
    assert control._get_operation(config, operation['id'])['state'] == 'cancelled'
    assert control._get_operation(config, one['id'])['state'] == 'cancelled'
    result = core.SchedulerService(config, callbacks={'processor': lambda: pytest.fail('cancelled')}).execute('processor', manual=True, operation_id=one['id'])
    assert result['state'] == 'cancelled'


def test_restart_timeout_keeps_stop_request_and_never_force_kills(config, monkeypatch):
    operation = control._new_operation(config, 'restart')
    core.request_stop(config)
    control.worker(config, operation['id'], timeout=0)
    row = control._get_operation(config, operation['id'])
    assert row['state'] == 'failed' and row['error'] == 'graceful_stop_timeout'
    assert core.status(config)['service']['stop_requested']


def test_restart_loads_current_config_and_launches_once(config, monkeypatch):
    monkeypatch.setattr(control, '_discover', lambda *args, **kwargs: [])
    launched = []
    monkeypatch.setattr(control, '_launch_service', lambda fresh: launched.append(fresh) or {'state': 'starting'})
    operation = control._new_operation(config, 'restart')
    control.worker(config, operation['id'])
    assert len(launched) == 1
    assert control._get_operation(config, operation['id'])['state'] == 'succeeded'


def test_crashed_once_worker_pauses_job_instead_of_leaving_running(config, monkeypatch):
    operation = control._new_operation(config, 'once', 'processor')
    with closing(core._connect(config, write=True)) as conn, conn:
        conn.execute("UPDATE operations SET state='running',process=? WHERE id=?", (json.dumps(fake_record(config)), operation['id']))
        conn.execute("UPDATE jobs SET state='running' WHERE name='processor'")
    monkeypatch.setattr(control, '_owned', lambda *args, **kwargs: False)
    monkeypatch.setattr(control, '_discover', lambda *args, **kwargs: [])
    assert control.trigger(config, 'processor')['state'] == 'paused'


def test_stop_before_child_initialization_is_not_lost(config):
    with closing(core._connect(config, write=True)) as conn, conn:
        core._setting(conn, 'pending_service', control.process_record())
    core.request_stop(config)
    service = core.SchedulerService(config).start()
    try:
        assert service.stop_event.is_set()
        assert service.execute('processor')['state'] == 'stopping'
    finally:
        service.stop()


def test_real_hidden_service_starts_restarts_stops_and_rejects_duplicate(config):
    # Isolated scheduler: every job/listener/alert remains disabled, so no user data or APIs are accessed.
    receipt = control.start(config)
    assert receipt['state'] == 'starting'
    deadline = time.monotonic() + 10
    try:
        while time.monotonic() < deadline:
            current = control.status(config)
            if current['state'] == 'running':
                break
            time.sleep(.1)
        assert current['state'] == 'running'
        assert control.start(config)['pid'] == receipt['pid']
        restart = control.restart(config)
        assert restart['state'] == 'queued'
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            current = control.status(config)
            if current['state'] == 'running' and current['pid'] != receipt['pid']:
                break
            time.sleep(.1)
        assert current['state'] == 'running' and current['pid'] != receipt['pid']
        assert control._get_operation(config, restart['operation']['id'])['state'] == 'succeeded'
    finally:
        control.stop(config)
        while time.monotonic() < deadline + 5 and control.status(config)['running']:
            time.sleep(.1)
    assert control.status(config)['state'] == 'stopped'
