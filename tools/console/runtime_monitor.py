"""Shared five-second probes while the console runs; no task hooks or persistence."""
from collections import deque
from contextlib import closing
import ctypes
import json
import logging
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from types import SimpleNamespace

from fastapi import Depends
import psutil


INTERVAL = 5
WINDOW = 60
MAX_GAP = 15


def _number(value):
    return float(value) if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0 else None


def _automation_state(config):
    from automation.core import APPLICATION_ID
    path = Path(config['storage']['state_file'])
    if not path.is_file():
        return {'jobs': [], 'listener': None, 'service': {}}
    # Resource polling must not inherit the controller's ten-second lock wait.
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=.15)) as conn:
        conn.row_factory = sqlite3.Row
        if conn.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID:
            raise ValueError('automation_state_unverified')
        listener = conn.execute('SELECT * FROM listener WHERE id=1').fetchone()
        return {'jobs': [dict(row) for row in conn.execute('SELECT name,state,last_start FROM jobs')],
                'listener': dict(listener) if listener else None,
                'service': {row['key']: json.loads(row['value']) for row in conn.execute("SELECT key,value FROM settings WHERE key IN ('service','pending_service')")}}


class WindowsDisks:
    """PDH's idle counter measures physical disk busy time, not summed request latency."""
    def __init__(self):
        self.query, self.counters, self.inventory = None, {}, {}
        if os.name != 'nt':
            return
        from ctypes import wintypes as w
        class Value(ctypes.Union):
            _fields_ = [('double', ctypes.c_double), ('large', ctypes.c_longlong)]
        class CounterValue(ctypes.Structure):
            _fields_ = [('status', w.DWORD), ('value', Value)]
        class Item(ctypes.Structure):
            _fields_ = [('name', w.LPWSTR), ('counter', CounterValue)]
        self.Item = Item
        self.api = ctypes.WinDLL('pdh')
        self.api.PdhOpenQueryW.argtypes = [w.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(w.HANDLE)]
        self.api.PdhAddEnglishCounterW.argtypes = [w.HANDLE, w.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(w.HANDLE)]
        self.api.PdhCollectQueryData.argtypes = [w.HANDLE]
        self.api.PdhGetFormattedCounterArrayW.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.POINTER(w.DWORD), ctypes.c_void_p]
        self.api.PdhCloseQuery.argtypes = [w.HANDLE]
        query = w.HANDLE()
        if self.api.PdhOpenQueryW(None, 0, ctypes.byref(query)):
            return
        self.query = query
        for key, name in [('idle', '% Idle Time'), ('read', 'Disk Read Bytes/sec'), ('write', 'Disk Write Bytes/sec')]:
            handle = w.HANDLE()
            if self.api.PdhAddEnglishCounterW(query, '\\PhysicalDisk(*)\\' + name, 0, ctypes.byref(handle)):
                self.close()
                return
            self.counters[key] = handle

    def _values(self, handle):
        from ctypes import wintypes as w
        size, count = w.DWORD(), w.DWORD()
        status = self.api.PdhGetFormattedCounterArrayW(handle, 0x200, ctypes.byref(size), ctypes.byref(count), None) & 0xffffffff
        if status != 0x800007D2 or not 0 < size.value <= 1024 * 1024:
            return {}
        buffer = ctypes.create_string_buffer(size.value)
        if self.api.PdhGetFormattedCounterArrayW(handle, 0x200, ctypes.byref(size), ctypes.byref(count), buffer):
            return {}
        items = ctypes.cast(buffer, ctypes.POINTER(self.Item))
        return {items[i].name: _number(items[i].counter.value.double) if items[i].counter.status in (0, 1) else None
                for i in range(min(count.value, 1024)) if items[i].name}

    @staticmethod
    def _kind(number):
        from ctypes import wintypes as w
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
        api.CreateFileW.restype = w.HANDLE
        api.DeviceIoControl.argtypes = [w.HANDLE, w.DWORD, ctypes.c_void_p, w.DWORD, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.c_void_p]
        api.CloseHandle.argtypes = [w.HANDLE]
        handle = api.CreateFileW('\\\\.\\PhysicalDrive' + str(number), 0, 3, None, 3, 0, None)
        if handle == ctypes.c_void_p(-1).value:
            return 'unknown'
        try:
            # StorageDeviceSeekPenaltyProperty is a capability query, not a disk read.
            query = (w.DWORD * 3)(7, 0, 0)
            output, returned = ctypes.create_string_buffer(16), w.DWORD()
            if not api.DeviceIoControl(handle, 0x2D1400, query, ctypes.sizeof(query), output, len(output), ctypes.byref(returned), None) or returned.value < 9:
                return 'unknown'
            return 'hdd' if output.raw[8] else 'ssd'
        finally:
            api.CloseHandle(handle)

    def read(self, *, reset=False):
        if not self.query:
            return [], [], 'disk_counters_unavailable' if os.name == 'nt' else 'disk_monitoring_unsupported'
        if self.api.PdhCollectQueryData(self.query):
            return list(self.inventory.values()), [], 'disk_counters_unavailable'
        counters = {key: self._values(handle) for key, handle in self.counters.items()}
        samples, inventory = [], {}
        for name in sorted(set().union(*(values for values in counters.values()))):
            match = re.fullmatch(r'(\d+)(?:\s+(.*))?', name)
            if not match:
                continue
            number = int(match[1])
            identifier = 'physical-disk-' + str(number)
            old = self.inventory.get(identifier, {})
            inventory[identifier] = {'id': identifier, 'name': '磁盘 ' + str(number),
                                     'mounts': (match[2] or '').split(), 'kind': old.get('kind') or self._kind(number)}
            values = {key: None if reset else counters[key].get(name) for key in counters}
            samples.append({'id': identifier, 'busy': None if values['idle'] is None else round(max(0, min(100, 100 - values['idle'])), 1),
                            **{key + '_mbps': None if values[key] is None else round(values[key] / 1000000, 3) for key in ('read', 'write')}})
        if inventory:
            self.inventory = inventory
        error = None
        if not samples:
            error = 'disk_counters_warming' if reset else 'disk_counters_unavailable'
        elif not reset and any(any(row[key] is None for key in ('busy', 'read_mbps', 'write_mbps')) for row in samples):
            error = 'disk_counters_unavailable'
        return list(self.inventory.values()), samples, error

    def close(self):
        if self.query:
            self.api.PdhCloseQuery(self.query)
            self.query = None


class RuntimeMonitor:
    def __init__(self, bridge, jobs, *, clock=time.monotonic, wall_clock=time.time, disk_probe=None):
        self.bridge, self.jobs, self.clock, self.wall_clock = bridge, jobs, clock, wall_clock
        self.disks = disk_probe
        self.lock = threading.Lock()
        self.samples = deque(maxlen=WINDOW // INTERVAL + 1)
        self.last, self.cpu_baseline, self.process_baselines, self.cached = None, None, {}, None
        self.active_jobs = None
        self._stop = threading.Event()
        self._worker = None

    def start(self):
        with self.lock:
            if self._worker and self._worker.is_alive():
                return
            self._stop.clear()
            self._worker = threading.Thread(target=self._run, name='runtime-monitor', daemon=True)
            self._worker.start()

    def _run(self):
        while not self._stop.is_set():
            delay = INTERVAL
            try:
                self.snapshot()
                with self.lock:
                    delay = max(.05, INTERVAL - (self.clock() - self.last))
            except Exception:
                logging.getLogger(__name__).warning('Runtime sampling unavailable; retrying next interval')
            if self._stop.wait(delay):
                break

    def _groups(self):
        bridge = self.bridge
        self.active_jobs = []
        modules = {source['id']: {'id': source['id'], 'name': source['name'], 'group_ids': [], 'attribution': 'unassigned'}
                   for source in bridge.sources.values()}
        for identifier, name in [('processor', '待办处理'), ('messaging', '消息投递'), ('feedback', '状态回写'), ('listener', '渠道监听'), ('console', '控制台')]:
            modules[identifier] = {'id': identifier, 'name': name, 'group_ids': [], 'attribution': 'unassigned'}
        groups = []

        def group(identifier, name, module_ids, processes=(), status='stopped'):
            groups.append({'id': identifier, 'name': name, 'module_ids': module_ids, 'roots': list(processes), 'status': status})
            for module in module_ids:
                if module in modules:
                    modules[module]['group_ids'].append(identifier)
                    modules[module]['attribution'] = 'shared' if identifier == 'shared' else modules[module]['attribution'] if modules[module]['attribution'] == 'shared' else 'independent'

        shared, listener, shared_ids, shared_state, listener_state, active = [], [], [], 'stopped', 'stopped', False
        job_modules, live_jobs = {}, []
        try:
            from automation import control, load_config
            from automation.listener import ListenerSupervisor
            path = bridge.root / bridge.settings.get('automation_config', 'config/automation.toml')
            if path.is_file():
                self.active_jobs = None
                config = load_config(path)
                live = _automation_state(config)
                live_jobs = live['jobs']
                active = any(job['state'] == 'running' for job in live['jobs'])
                for name, entry in config['jobs'].items():
                    matches = [identifier for identifier, source_path in bridge.paths.items() if Path(entry['config']) == source_path]
                    module = matches[0] if len(matches) == 1 else name if name in {'processor', 'messaging', 'feedback'} else None
                    job_modules[name] = module
                    if module and module not in shared_ids:
                        shared_ids.append(module)
                for key in ('service', 'pending_service'):
                    record = live['service'].get(key)
                    if not record:
                        continue
                    owned = control._owned(record, config)
                    if owned:
                        shared.append(psutil.Process(record['pid']))
                    elif owned is None:
                        shared_state = 'unknown'
                if live['listener']:
                    process = ListenerSupervisor(SimpleNamespace(config=config))._owned(live['listener'])
                    if process:
                        listener.append(process)
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, psutil.Error):
            shared_state = listener_state = 'unknown'
            self.active_jobs = None
        group('listener', '渠道监听', ['listener'], listener, listener_state)
        pi, children = [], []
        if shared and active:
            try:
                for process in shared:
                    children.extend(process.children(recursive=True)[:128])
                runner = bridge.root / 'processor/pi-runtime/runner.mjs'
                pi = [process for process in children if any(Path(arg).is_absolute() and Path(arg).resolve() == runner.resolve() for arg in process.cmdline()[1:])]
                pi_ids = {process.pid for process in pi}
                for _ in range(len(children)):
                    added = [process for process in children if process.pid not in pi_ids and process.ppid() in pi_ids]
                    if not added:
                        break
                    pi.extend(added)
                    pi_ids.update(process.pid for process in added)
                excluded = pi_ids | {process.pid for process in listener}
                shared.extend(process for process in children if process.pid not in excluded)
            except psutil.Error:
                shared_state = 'unknown'
        group('pi', '待办处理 · Pi 子进程', ['processor'], pi)
        group('shared', '共享后台合计', shared_ids, shared, shared_state)
        if shared and shared_state != 'unknown':
            self.active_jobs = [{'module': job['name'], 'source_id': job_modules[job['name']] if job_modules[job['name']] in bridge.sources else None,
                                 'name': modules[job_modules[job['name']]]['name'], 'started_at': _number(job.get('last_start'))}
                                for job in live_jobs if job.get('state') == 'running' and job_modules.get(job.get('name')) in modules]
        with self.jobs.mutex:
            running = [dict(self.jobs.jobs[identifier]) for identifier in self.jobs.running.values() if identifier in self.jobs.jobs]
        for job in running[:64]:
            module, processes, status = job['module'], [], 'unknown'
            if module not in modules or job.get('status', 'running') != 'running':
                continue
            try:
                process = psutil.Process(job['pid'])
                command = process.cmdline()
                expected = [str(Path(__file__).with_name('bridge.py')), '--settings', str(bridge.settings_path),
                            '--request', str(self.jobs.directory / job['id'] / 'request.json'),
                            '--result', str(self.jobs.directory / job['id'] / 'result.json')]
                if command[-len(expected):] == expected and job['started_at'] - 1 <= process.create_time() <= job['started_at'] + 10 and process.is_running():
                    processes = [process, *process.children(recursive=True)[:128]]
                    status = 'ok'
            except (psutil.Error, KeyError, TypeError, ValueError):
                pass
            group('task:' + module, modules[module]['name'] + ' · 任务进程', [module], processes, status)
            if status != 'ok':
                self.active_jobs = None
            elif self.active_jobs is not None:
                self.active_jobs.append({'module': module, 'source_id': module if module in bridge.sources else None,
                                         'name': modules[module]['name'], 'started_at': _number(job['started_at'])})
        group('console', '控制台与探针', ['console'], [psutil.Process(os.getpid())], 'ok')
        return list(modules.values()), groups

    def _processes(self, groups, tick):
        baselines, seen, result = {}, set(), []
        cpus = psutil.cpu_count() or 1
        for group in groups:
            rows, denied = [], False
            for process in group.pop('roots'):
                try:
                    with process.oneshot():
                        identity = (process.pid, process.create_time())
                        if identity in seen:
                            continue
                        if not process.is_running():
                            denied = True
                            continue
                        seen.add(identity)
                        times = process.cpu_times()
                        cpu = times.user + times.system
                        try:
                            io = process.io_counters()
                            read, write = io.read_bytes, io.write_bytes
                        except psutil.Error:
                            read = write = None
                    previous = self.process_baselines.get(identity)
                    baselines[identity] = (tick, cpu, read, write)
                    values = {'cpu': None, 'read_mbps': None, 'write_mbps': None}
                    if previous and 0 < tick - previous[0] <= MAX_GAP:
                        elapsed = tick - previous[0]
                        values['cpu'] = None if cpu < previous[1] else round(min(100, (cpu - previous[1]) / elapsed / cpus * 100), 2)
                        for key, current, old in [('read_mbps', read, previous[2]), ('write_mbps', write, previous[3])]:
                            values[key] = None if current is None or old is None or current < old else round((current - old) / elapsed / 1000000, 3)
                    rows.append(values)
                except psutil.Error:
                    denied = True
            uncertain = denied or group['status'] == 'unknown'
            values = {key: round(sum(row[key] for row in rows), 3) if rows and not uncertain and all(row[key] is not None for row in rows) else None for key in ('cpu', 'read_mbps', 'write_mbps')}
            status = 'unknown' if uncertain else 'ok' if values['cpu'] is not None else 'warming' if rows else 'stopped'
            result.append({**group, **values, 'status': status})
        self.process_baselines = baselines
        return result

    def snapshot(self):
        with self.lock:
            tick = self.clock()
            if self.last is not None and tick - self.last < INTERVAL:
                return self.cached
            started = time.perf_counter()
            reset = self.last is None or tick - self.last > MAX_GAP
            if reset:
                self.cpu_baseline, self.process_baselines = None, {}
            cpu = None
            try:
                values = psutil.cpu_times()
                total = sum(values) - getattr(values, 'guest', 0) - getattr(values, 'guest_nice', 0)
                idle = values.idle + getattr(values, 'iowait', 0)
                if self.cpu_baseline:
                    delta = total - self.cpu_baseline[0]
                    if delta > 0 and idle >= self.cpu_baseline[1]:
                        cpu = round(max(0, min(100, (1 - (idle - self.cpu_baseline[1]) / delta) * 100)), 1)
                self.cpu_baseline = total, idle
            except (OSError, psutil.Error):
                self.cpu_baseline = None
            modules, groups = self._groups()
            processes = self._processes(groups, tick)
            try:
                if self.disks is None:
                    self.disks = WindowsDisks()
                inventory, disks, error = self.disks.read(reset=reset)
            except (OSError, AttributeError):
                inventory, disks, error = [], [], 'disk_counters_unavailable'
            now = self.wall_clock()
            while self.samples and self.samples[0]['at'] < now - WINDOW:
                self.samples.popleft()
            self.samples.append({'at': now, 'cpu': cpu, 'disks': disks, 'processes': processes, 'active_jobs': self.active_jobs})
            status = 'warming' if reset else 'partial' if error or cpu is None or any(process['status'] == 'unknown' for process in processes) else 'ok'
            self.last = tick
            self.cached = {'generated_at': now, 'interval_seconds': INTERVAL, 'status': status, 'error': error,
                           'disks': inventory, 'samples': list(self.samples), 'modules': modules,
                           'sample_duration_ms': round((time.perf_counter() - started) * 1000, 2)}
            return self.cached

    def close(self):
        self._stop.set()
        if self._worker:
            self._worker.join()
        with self.lock:
            if self.disks:
                self.disks.close()


def register_runtime_monitor_routes(app, bridge, jobs, authenticate):
    monitor = app.state.runtime_monitor = RuntimeMonitor(bridge, jobs)

    @app.get('/api/runtime-monitor', dependencies=[Depends(authenticate)], summary='读取控制台持续采样的整机、物理磁盘和已核验进程资源占用')
    def runtime_monitor():
        return monitor.snapshot()
