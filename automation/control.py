"""Small detached-process controls; the console is never the scheduler's owner."""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

import psutil

from common.locking import InstanceLock
from common.codec import canonical
from .config import JOBS, load_config
from . import core


ACTIVE = ('queued', 'running', 'waiting')


def config_digest(config):
    return hashlib.sha256(Path(config['_config_path']).read_bytes()).hexdigest()


def process_record(pid=None):
    process = psutil.Process(pid or os.getpid())
    return {'pid': process.pid, 'create_time': process.create_time(), 'command': process.cmdline()}


def _matches(command, module, mode, config_path, cwd=None):
    try:
        index = command.index('-m')
        if command[index + 1:index + 3] != [module, mode]:
            return False
        path = Path(command[command.index('--config') + 1])
        return (Path(cwd or os.getcwd()) / path).resolve() == Path(config_path).resolve()
    except (ValueError, IndexError, TypeError):
        return False


def _owned(record, config, *, worker=False):
    if not record or not record.get('pid') or not record.get('create_time') or not record.get('command'):
        return False
    try:
        process = psutil.Process(record['pid'])
        command = process.cmdline()
        if abs(process.create_time() - record['create_time']) > .01 or command != record['command']:
            return False
        if not _matches(command, 'automation.control' if worker else 'automation',
                        'worker' if worker else 'run', config['_config_path'], process.cwd()):
            return False
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.AccessDenied:
        return None
    except (psutil.NoSuchProcess, ValueError, TypeError):
        return False


def _discover(config, *, setup=False):
    matches = []
    for process in psutil.process_iter(['cmdline']):
        try:
            command = process.info['cmdline'] or []
            if setup and '--setup' not in command:
                continue
            module, mode, path = ('messaging.feishu', 'listen', config['listener']['config']) if setup else ('automation', 'run', config['_config_path'])
            if module not in command:
                continue
            if _matches(command, module, mode, path, process.cwd()):
                matches.append(process_record(process.pid))
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    return matches


def _operations(config):
    conn = core._connect(config)
    if conn is None:
        return []
    with closing(conn):
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='operations'").fetchone():
            return []
        rows = [dict(r) for r in conn.execute("SELECT * FROM operations WHERE state IN ('queued','running','waiting') OR id IN (SELECT id FROM operations ORDER BY created_at DESC LIMIT 30) ORDER BY created_at DESC")]
    for row in rows:
        if row['process']:
            row['process'] = json.loads(row['process'])
            if row['state'] in ACTIVE:
                alive = _owned(row['process'], config, worker=True)
                command = row['process'].get('command', [])
                if '--operation' not in command or command[command.index('--operation') + 1:] != [row['id'], '--config', config['_config_path']]:
                    alive = False
                if alive is False:
                    row.update(state='failed', error='control_worker_exited')
                elif alive is None:
                    row.update(error='process_identity_unverifiable')
    return rows


def status(config):
    result = core.status(config)
    settings = result['service']
    saved = settings.get('service', {})
    pending = settings.get('pending_service', {})
    records = [r for r in (saved, pending) if r]
    checks = [(record, _owned(record, config)) for record in records]
    alive = next((record for record, valid in checks if valid), None)
    unknown = any(valid is None for _, valid in checks)
    discovered = [] if alive or unknown else _discover(config)
    alive = alive or (discovered[0] if len(discovered) == 1 else None)
    state = 'unknown' if unknown else 'duplicate' if len(discovered) > 1 else 'stopped'
    if alive:
        state = 'stopping' if settings.get('stop_requested') else 'running' if alive is saved else 'starting'
    operations = _operations(config)
    active = next((r for r in operations if r['state'] in ACTIVE), None)
    result.update(state=state, running=bool(alive), verified=bool(alive), pid=alive.get('pid') if alive else None,
                  config_changed=bool(alive and alive.get('config_hash') != config_digest(config)),
                  operation=active or (operations[0] if operations else None), operations=operations)
    return result


def _admission(config):
    return InstanceLock(config['storage']['state_file'] + '.control', timeout=2)


def _spawn(config, arguments):
    executable = Path(sys.executable)
    quiet = executable.with_name('pythonw.exe')
    command = [str(quiet if quiet.exists() else executable), '-m', *arguments, '--config', config['_config_path']]
    env = dict(os.environ, PYTHONUTF8='1')
    env['PYTHONPATH'] = os.pathsep.join(filter(None, [str(Path(config['_root']) / 'runtime-deps'), config['_root'], env.get('PYTHONPATH')]))
    # Library errors and model output must not leak chat content to an unattended console.
    child = subprocess.Popen(command, cwd=config['_root'], env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    return process_record(child.pid)


def _update(config, operation_id, state, error=None):
    with closing(core._connect(config, write=True)) as conn, conn:
        conn.execute('UPDATE operations SET state=?,updated_at=?,error=? WHERE id=?', (state, time.time(), error, operation_id))


def _active(config):
    rows = _operations(config)
    orphaned = any(r['kind'] == 'once' and r['state'] == 'running' and not r['process'] for r in rows) and status(config)['state'] == 'stopped'
    # Persist crash detection so an abandoned operation cannot block the next request.
    for row in rows:
        if row['error'] == 'control_worker_exited' or (orphaned and row['kind'] == 'once' and row['state'] == 'running' and not row['process']):
            row.update(state='failed', error='control_worker_exited')
            _update(config, row['id'], 'failed', row['error'])
            if row['kind'] == 'once':
                with closing(core._connect(config, write=True)) as conn, conn:
                    changed = conn.execute("UPDATE jobs SET state='paused',error='interrupted_execution',active_error='interrupted_execution' WHERE name=? AND state='running'", (row['job'],)).rowcount
                    if changed:
                        core.history._finish(conn, row['job'], 'interrupted', time.time(), error='interrupted_execution', interrupted=True)
    return [row for row in rows if row['state'] in ACTIVE]


def _new_operation(config, kind, job=None):
    now, operation_id = time.time(), uuid.uuid4().hex
    with closing(core._connect(config, write=True)) as conn, conn:
        conn.execute('INSERT INTO operations(id,kind,job,state,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                     (operation_id, kind, job, 'queued', now, now))
    return {'id': operation_id, 'kind': kind, 'job': job, 'state': 'queued'}


def _launch_service(config):
    if not config['enabled']:
        return {'state': 'disabled'}
    if _discover(config, setup=True):
        return {'state': 'setup_listener_active'}
    checks = core.check(config)
    if not checks['ready']:
        return {'state': 'configuration_error', 'check': checks}
    # The service lock also catches an unrecorded once worker or an uninspectable service.
    try:
        with InstanceLock(config['storage']['state_file']):
            pass
    except RuntimeError:
        return {'state': 'busy'}
    with closing(core._connect(config, write=True)) as conn, conn:
        core._setting(conn, 'stop_requested', False)
    record = _spawn(config, ['automation', 'run'])
    record.update(config_hash=config_digest(config), state='starting')
    with closing(core._connect(config, write=True)) as conn, conn:
        core._setting(conn, 'pending_service', record)
    return {'state': 'starting', 'pid': record['pid'], 'verified': True}


def start(config):
    with _admission(config):
        current = status(config)
        if current['running'] or current['state'] in {'unknown', 'duplicate'}:
            return current
        active = _active(config)
        if any(r['process'] or r['kind'] != 'once' for r in active):
            return {'state': 'busy', 'operation': active[0]}
        return _launch_service(config)


def stop(config):
    with _admission(config):
        with closing(core._connect(config, write=True)) as conn, conn:
            conn.execute("UPDATE operations SET state='cancelled',updated_at=? WHERE state='queued' OR (kind='restart' AND state='waiting')", (time.time(),))
        core.request_stop(config)
        current = status(config)
        return {'state': 'stopping' if current['running'] else current['state'], 'stop_requested': True,
                'detail': 'Running jobs finish before shutdown; no process is killed.'}


def _launch_operation(config, operation):
    try:
        record = _spawn(config, ['automation.control', 'worker', '--operation', operation['id']])
        with closing(core._connect(config, write=True)) as conn, conn:
            conn.execute('UPDATE operations SET process=? WHERE id=?', (canonical(record), operation['id']))
        return {'state': 'queued', 'operation': operation, 'pid': record['pid']}
    except Exception:
        _update(config, operation['id'], 'failed', 'worker_start_failed')
        raise


def restart(config):
    with _admission(config):
        active = _active(config)
        existing = next((r for r in active if r['kind'] == 'restart'), None)
        if existing:
            return {'state': existing['state'], 'operation': existing}
        current = status(config)
        if current['state'] in {'unknown', 'duplicate'}:
            return current
        with closing(core._connect(config, write=True)) as conn, conn:
            conn.execute("UPDATE operations SET state='cancelled',updated_at=? WHERE kind='once' AND state='queued'", (time.time(),))
        core.request_stop(config)
        return _launch_operation(config, _new_operation(config, 'restart'))


def trigger(config, job):
    if job not in JOBS:
        raise ValueError('Unknown job')
    with _admission(config):
        active = _active(config)
        current = status(config)
        existing = next((r for r in active if r['kind'] == 'once' and r['job'] == job), None)
        if existing:
            if existing['state'] == 'queued' and not existing['process'] and current['state'] == 'stopped':
                return _launch_operation(config, existing)
            return {'state': existing['state'], 'operation': existing}
        if any(r['kind'] == 'restart' for r in active):
            return {'state': 'busy', 'operation': active[0]}
        if current['state'] in {'stopping', 'unknown', 'duplicate'}:
            return {'state': current['state']}
        row = next((r for r in current['jobs'] if r['name'] == job), {})
        if row.get('state') in {'paused', 'running'}:
            return {'state': row['state'], 'job': job}
        if not current['running'] and active:
            return {'state': 'busy', 'operation': active[0]}
        operation = _new_operation(config, 'once', job)
        if current['running']:
            return {'state': 'queued', 'operation': operation}
        return _launch_operation(config, operation)


def _get_operation(config, operation_id):
    with closing(core._connect(config)) as conn:
        row = conn.execute('SELECT * FROM operations WHERE id=?', (operation_id,)).fetchone()
        return dict(row) if row else None


def worker(config, operation_id, *, timeout=900):
    # Admission prevents a fast child from racing the parent's process-record write.
    with _admission(config):
        operation = _get_operation(config, operation_id)
        if not operation or operation['state'] != 'queued':
            return
        if operation['kind'] == 'restart':
            _update(config, operation_id, 'waiting')
    try:
        if operation['kind'] == 'once':
            with InstanceLock(config['storage']['state_file']):
                with _admission(config):
                    if _get_operation(config, operation_id)['state'] != 'queued':
                        return
                result = core.SchedulerService(config).execute(operation['job'], manual=True, operation_id=operation_id)
                if result['state'] in {'paused', 'running', 'disabled'}:
                    _update(config, operation_id, 'failed', 'job_' + result['state'])
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with _admission(config):
                if _get_operation(config, operation_id)['state'] != 'waiting':
                    return
                current = status(config)
                if current['state'] in {'unknown', 'duplicate'}:
                    _update(config, operation_id, 'failed', 'process_identity_unverifiable')
                    return
                if not current['running']:
                    fresh = load_config(config['_config_path'])
                    if fresh['storage']['state_file'] != config['storage']['state_file']:
                        _update(config, operation_id, 'failed', 'state_path_changed')
                        return
                    result = _launch_service(fresh)
                    if result['state'] != 'busy':
                        _update(config, operation_id, 'succeeded' if result['state'] in {'starting', 'disabled'} else 'failed',
                                None if result['state'] in {'starting', 'disabled'} else result['state'])
                        return
            time.sleep(.25)
        _update(config, operation_id, 'failed', 'graceful_stop_timeout')
    except Exception as exc:
        _update(config, operation_id, 'failed', core._error(exc))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['worker'])
    parser.add_argument('--config', required=True)
    parser.add_argument('--operation', required=True)
    args = parser.parse_args()
    worker(load_config(args.config), args.operation)
