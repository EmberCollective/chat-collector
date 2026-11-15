from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
import threading
import time

import psutil
import pytest

import runtime_monitor as runtime
from test_console import console


class DiskProbe:
    def __init__(self):
        self.calls, self.resets, self.closed = 0, [], False

    def read(self, *, reset=False):
        self.calls += 1
        self.resets.append(reset)
        return ([{'id': 'physical-disk-4', 'name': '磁盘 4', 'mounts': ['R:'], 'kind': 'hdd'}],
                [{'id': 'physical-disk-4', 'busy': None if reset else 72,
                  'read_mbps': None if reset else 12, 'write_mbps': None if reset else .5}], None)

    def close(self):
        self.closed = True


class Process:
    pid = 80
    created = 100
    cpu = 2
    read = 1000000
    write = 500000
    denied = False

    def oneshot(self): return nullcontext()
    def create_time(self): return self.created
    def is_running(self): return True
    def cpu_times(self):
        if self.denied:
            raise psutil.AccessDenied(self.pid)
        return SimpleNamespace(user=self.cpu, system=0)
    def io_counters(self): return SimpleNamespace(read_bytes=self.read, write_bytes=self.write)


@pytest.fixture
def monitor(monkeypatch):
    ticks, totals, process, disks = [0], [10, 10, 80], Process(), DiskProbe()
    types = namedtuple('CPU', 'user system idle')
    monkeypatch.setattr(psutil, 'cpu_times', lambda: types(*totals))
    monkeypatch.setattr(psutil, 'cpu_count', lambda: 2)
    result = runtime.RuntimeMonitor(None, None, clock=lambda: ticks[0], wall_clock=lambda: 1000 + ticks[0], disk_probe=disks)
    result._groups = lambda: ([], [{'id': 'shared', 'name': '共享后台', 'module_ids': ['work'], 'roots': [process], 'status': 'ok'}])
    return result, ticks, totals, process, disks


def test_shared_throttle_first_baseline_and_counter_deltas(monitor):
    probe, ticks, totals, process, disks = monitor
    first = probe.snapshot()
    assert first['status'] == 'warming'
    assert first['samples'][0]['cpu'] is None
    assert first['samples'][0]['processes'][0]['cpu'] is None
    ticks[0], totals[:] = 5, [15, 15, 90]
    process.cpu, process.read, process.write = 4, 6000000, 3000000
    second = probe.snapshot()
    assert second['status'] == 'ok'
    assert second['samples'][-1]['cpu'] == 50
    group = second['samples'][-1]['processes'][0]
    assert (group['cpu'], group['read_mbps'], group['write_mbps']) == (20, 1, .5)
    assert disks.calls == 2
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: probe.snapshot(), range(40)))
    assert disks.calls == 2 and all(value is second for value in results)
    assert second['samples'][-1]['disks'][0]['busy'] == 72
    probe.close()
    assert disks.closed


def test_pause_resets_baseline_and_history_is_bounded(monitor):
    probe, ticks, totals, process, disks = monitor
    for tick in range(0, 205, 5):
        ticks[0] = tick
        totals[:] = [10 + tick, 10 + tick, 80 + tick]
        process.cpu += 1
        snapshot = probe.snapshot()
    assert len(snapshot['samples']) == 13
    assert snapshot['samples'][-1]['at'] - snapshot['samples'][0]['at'] == 60
    ticks[0] += 80
    snapshot = probe.snapshot()
    assert len(snapshot['samples']) == 1
    assert snapshot['status'] == 'warming' and snapshot['samples'][0]['cpu'] is None
    assert snapshot['samples'][0]['processes'][0]['cpu'] is None
    assert disks.resets[-1] is True


def test_pid_reuse_missing_permissions_and_counter_reset_are_unknown(monitor):
    probe, ticks, totals, process, disks = monitor
    probe.snapshot()
    ticks[0], process.created = 5, 200
    snapshot = probe.snapshot()
    assert snapshot['samples'][-1]['processes'][0]['cpu'] is None
    assert set(probe.process_baselines) == {(80, 200)}
    ticks[0], process.denied = 10, True
    snapshot = probe.snapshot()
    assert snapshot['status'] == 'partial'
    assert snapshot['samples'][-1]['processes'][0]['status'] == 'unknown'
    assert probe.process_baselines == {}
    ticks[0], process.denied = 15, False
    probe.snapshot()
    ticks[0], process.cpu, process.read = 20, 1, 0
    snapshot = probe.snapshot()
    group = snapshot['samples'][-1]['processes'][0]
    assert group['cpu'] is None and group['read_mbps'] is None


def test_processes_count_once_and_stopped_is_not_zero(monitor):
    probe, ticks, totals, process, disks = monitor
    probe._groups = lambda: ([], [dict(id=identifier, name=identifier, module_ids=[], roots=[process], status='ok') for identifier in ['listener', 'shared']])
    probe.snapshot()
    ticks[0], process.cpu = 5, 4
    snapshot = probe.snapshot()
    groups = snapshot['samples'][-1]['processes']
    assert groups[0]['cpu'] == 20
    assert groups[1]['cpu'] is None and groups[1]['status'] == 'stopped'
    ticks[0] = 10
    probe._groups = lambda: ([], [dict(id='shared', name='共享后台', module_ids=[], roots=[process], status='unknown')])
    group = probe.snapshot()['samples'][-1]['processes'][0]
    assert group['status'] == 'unknown' and group['cpu'] is None and group['read_mbps'] is None


def test_ownership_uses_config_paths_and_does_not_claim_unknown_sources(tmp_path, monkeypatch):
    from automation import control
    config_path = tmp_path / 'automation.toml'
    config_path.write_text('')
    bridge = SimpleNamespace(root=tmp_path, settings={'automation_config': str(config_path)}, settings_path=tmp_path / 'console.toml',
                             sources={'work': {'id': 'work', 'name': '工作账号'}, 'new-source': {'id': 'new-source', 'name': '新来源'}},
                             paths={'work': tmp_path / 'work.toml'})
    config = {'jobs': {'old-job-name': {'config': str(tmp_path / 'work.toml')}, 'processor': {'config': str(tmp_path / 'processor.toml')}},
              '_config_path': str(config_path)}
    import automation
    monkeypatch.setattr(automation, 'load_config', lambda path: config)
    monkeypatch.setattr(runtime, '_automation_state', lambda cfg: {'jobs': [], 'listener': None, 'service': {'service': {'pid': 321, 'command': ['unrelated'], 'create_time': 10}}})
    monkeypatch.setattr(control, '_owned', lambda *args: False)
    actual = psutil.Process
    def process(pid):
        assert pid != 321, 'unverified PID must not be opened for resource attribution'
        return actual(pid)
    monkeypatch.setattr(psutil, 'Process', process)
    jobs = SimpleNamespace(mutex=nullcontext(), running={}, jobs={})
    probe = runtime.RuntimeMonitor(bridge, jobs)
    modules, groups = probe._groups()
    assert next(module for module in modules if module['id'] == 'work')['attribution'] == 'shared'
    assert next(module for module in modules if module['id'] == 'new-source')['group_ids'] == []
    assert next(group for group in groups if group['id'] == 'shared')['roots'] == []


def test_missing_disk_counters_keep_cpu_and_processes_available(monitor):
    probe, ticks, totals, process, disks = monitor
    disks.read = lambda **kwargs: ([], [], 'disk_counters_unavailable')
    probe.snapshot()
    ticks[0], process.cpu, totals[:] = 5, 4, [15, 15, 90]
    snapshot = probe.snapshot()
    assert snapshot['status'] == 'partial'
    assert snapshot['samples'][-1]['cpu'] == 50
    assert snapshot['samples'][-1]['processes'][0]['cpu'] == 20
    assert snapshot['error'] == 'disk_counters_unavailable'


def test_manual_worker_requires_exact_job_command_and_start_time(tmp_path, monkeypatch):
    bridge = SimpleNamespace(root=tmp_path, settings={}, settings_path=tmp_path / 'console.toml',
                             paths={}, sources={'account': {'id': 'account', 'name': '账号'}})
    jobs = SimpleNamespace(mutex=nullcontext(), directory=tmp_path / 'jobs', running={'account': 'run1'},
                           jobs={'run1': {'id': 'run1', 'module': 'account', 'pid': 901, 'started_at': 100}})
    worker = Process()
    worker.pid, worker.created = 901, 101
    command = ['python', str(Path(runtime.__file__).with_name('bridge.py')), '--settings', str(bridge.settings_path),
               '--request', str(jobs.directory / 'run1/request.json'), '--result', str(jobs.directory / 'run1/result.json')]
    worker.cmdline, worker.children = lambda: command, lambda **kwargs: []
    actual = psutil.Process
    monkeypatch.setattr(psutil, 'Process', lambda pid: worker if pid == 901 else actual(pid))
    probe = runtime.RuntimeMonitor(bridge, jobs)
    modules, groups = probe._groups()
    assert next(group for group in groups if group['id'] == 'task:account')['roots'] == [worker]
    assert probe.active_jobs == [{'module': 'account', 'source_id': 'account', 'name': '账号', 'started_at': 100}]
    jobs.jobs['run1']['status'] = 'succeeded'
    probe._groups()
    assert probe.active_jobs == []
    jobs.jobs['run1']['status'] = 'running'
    worker.created = 111
    assert next(group for group in probe._groups()[1] if group['id'] == 'task:account')['roots'] == []
    assert probe.active_jobs is None
    worker.created, command[-1] = 101, str(tmp_path / 'another-result.json')
    assert next(group for group in probe._groups()[1] if group['id'] == 'task:account')['roots'] == []
    assert probe.active_jobs is None


def test_endpoint_is_authenticated_and_does_not_create_or_modify_databases(console):
    client, app, root, now = console
    app.state.runtime_monitor.close()
    app.state.runtime_monitor.disks = DiskProbe()
    app.state.runtime_monitor.last = None
    before = {path: path.read_bytes() for path in root.rglob('*.db')}
    response = client.get('/api/runtime-monitor')
    assert response.status_code == 200, response.text
    snapshot = response.json()
    assert snapshot['status'] == 'warming'
    assert snapshot['interval_seconds'] == 5
    assert snapshot['samples'][-1]['cpu'] is None
    assert client.get('/api/runtime-monitor').json()['generated_at'] == snapshot['generated_at']
    assert client.get('/api/runtime-monitor', headers={'X-Console-Token': 'wrong'}).status_code == 401
    assert client.get('/api/runtime-monitor', headers={'Origin': 'https://other.example'}).status_code == 403
    assert {path: path.read_bytes() for path in root.rglob('*.db')} == before


def test_sampler_records_without_browser_and_stops_with_console(monitor, monkeypatch):
    probe, ticks, totals, process, disks = monitor
    monkeypatch.setattr(runtime, 'INTERVAL', .02)
    probe.clock = time.monotonic
    probe.wall_clock = time.time
    sampled = threading.Event()
    original = probe.snapshot
    def snapshot():
        result = original()
        if disks.calls >= 3:
            sampled.set()
        return result
    probe.snapshot = snapshot
    try:
        probe.start()
        worker = probe._worker
        probe.start()
        assert probe._worker is worker
        assert sampled.wait(2), 'sampling must advance without any HTTP requests'
    finally:
        probe.close()
    assert not worker.is_alive() and disks.closed
    assert len(probe.cached['samples']) >= 3


def test_sampler_retries_failure_at_regular_interval(monitor, monkeypatch):
    probe, *_ = monitor
    monkeypatch.setattr(runtime, 'INTERVAL', .05)
    calls, retried = [], threading.Event()
    def unavailable():
        calls.append(time.monotonic())
        if len(calls) >= 2:
            retried.set()
        raise RuntimeError('fixture')
    probe.snapshot = unavailable
    try:
        probe.start()
        assert retried.wait(2)
    finally:
        probe.close()
    assert calls[1] - calls[0] >= .04


def test_empty_and_partial_pdh_counters_do_not_stay_warming_forever():
    probe = runtime.WindowsDisks.__new__(runtime.WindowsDisks)
    probe.query, probe.inventory = 1, {}
    probe.api = SimpleNamespace(PdhCollectQueryData=lambda query: 0)
    probe.counters = {'idle': 1, 'read': 2, 'write': 3}
    probe._values = lambda handle: {}
    assert probe.read(reset=True)[2] == 'disk_counters_warming'
    assert probe.read()[2] == 'disk_counters_unavailable'
    probe._kind = lambda number: 'unknown'
    probe._values = lambda handle: {'4 R:': 20 if handle == 1 else None}
    inventory, samples, error = probe.read()
    assert inventory[0]['kind'] == 'unknown'
    assert samples[0]['busy'] == 80 and samples[0]['read_mbps'] is None
    assert error == 'disk_counters_unavailable'
    assert runtime._number(float('nan')) is None and runtime._number(-1) is None


def test_current_automatic_jobs_require_verified_service_and_config_mapping(tmp_path, monkeypatch):
    from automation import control
    import automation
    path = tmp_path / 'automation.toml'
    path.write_text('')
    bridge = SimpleNamespace(root=tmp_path, settings={'automation_config': str(path)}, settings_path=tmp_path / 'console.toml',
                             sources={'work': {'id': 'work', 'name': '工作账号'}}, paths={'work': tmp_path / 'source.toml'})
    config = {'jobs': {'collector-name': {'config': str(tmp_path / 'source.toml')}, 'unknown': {'config': str(tmp_path / 'unknown.toml')}}}
    live = {'jobs': [{'name': 'collector-name', 'state': 'running', 'last_start': 90},
                     {'name': 'unknown', 'state': 'running', 'last_start': 95}],
            'listener': None, 'service': {'service': {'pid': 902}}}
    ownership = [True]
    monkeypatch.setattr(automation, 'load_config', lambda path: config)
    monkeypatch.setattr(runtime, '_automation_state', lambda config: live)
    monkeypatch.setattr(control, '_owned', lambda *args: ownership[0])
    actual = psutil.Process
    worker = Process()
    worker.pid, worker.children = 902, lambda **kwargs: []
    monkeypatch.setattr(psutil, 'Process', lambda pid: worker if pid == 902 else actual(pid))
    probe = runtime.RuntimeMonitor(bridge, SimpleNamespace(mutex=nullcontext(), jobs={}, running={}))
    probe._groups()
    assert probe.active_jobs == [{'module': 'collector-name', 'source_id': 'work', 'name': '工作账号', 'started_at': 90}]
    live['jobs'][0]['state'] = 'idle'
    probe._groups()
    assert probe.active_jobs == []
    live['jobs'][0]['state'] = 'running'
    for state in [None, False]:
        ownership[0] = state
        probe._groups()
        assert probe.active_jobs is None


def test_sample_keeps_its_same_time_job_context(monitor):
    probe, ticks, totals, process, disks = monitor
    def groups():
        probe.active_jobs = [{'module': 'work', 'source_id': 'work', 'name': '工作账号', 'started_at': 990}] if ticks[0] == 0 else []
        return [], []
    probe._groups = groups
    probe.snapshot()
    ticks[0] = 5
    snapshot = probe.snapshot()
    assert snapshot['samples'][0]['active_jobs'][0]['name'] == '工作账号'
    assert snapshot['samples'][-1]['active_jobs'] == []
