"""Supervise only the explicitly launched Feishu listener; never kill Python by name."""
from contextlib import closing
import json
import os
from pathlib import Path
import subprocess
import sys

import psutil

from common.codec import canonical
from .core import _connect


class ListenerSupervisor:
    def __init__(self, service):
        self.service, self.config = service, service.config
        self.settings = self.config['listener']
        self.stop_file = Path(self.config['storage']['state_file']).parent / 'listener.stop'
        self.child = None

    def _owned(self, row):
        if not row['pid'] or not row['command']:
            return None
        try:
            process = psutil.Process(row['pid'])
            if abs(process.create_time() - row['create_time']) > .01:
                return None
            expected = json.loads(row['command'])
            if [part.casefold() for part in process.cmdline()] != [part.casefold() for part in expected]:
                return None
            if 'messaging.feishu' not in expected or self.settings['config'] not in expected:
                return None
            if self.child and self.child.poll() is not None:
                return None
            return process if process.is_running() and process.status() != psutil.STATUS_ZOMBIE else None
        except psutil.AccessDenied:
            raise ValueError('listener_identity_unverifiable') from None
        except (psutil.NoSuchProcess, ValueError, TypeError):
            return None

    def _failed(self, attempts, code):
        exhausted = attempts >= self.settings['max_attempts']
        delay = min(self.settings['backoff_seconds'] * 2 ** max(0, attempts - 1), self.settings['max_backoff_seconds'])
        with closing(_connect(self.config, write=True)) as conn, conn:
            conn.execute('UPDATE listener SET state=?,attempts=?,next_run=?,pid=NULL,create_time=NULL,command=NULL,error=? WHERE id=1',
                         ('paused' if exhausted else 'retry', attempts, 0 if exhausted else self.service.clock() + delay, code))
        self.service._alert('listener', 'failure', code)

    def poll(self):
        with closing(_connect(self.config)) as conn:
            row = conn.execute('SELECT * FROM listener WHERE id=1').fetchone()
        if row['state'] == 'paused':
            return
        try:
            process = self._owned(row)
        except ValueError:
            with closing(_connect(self.config, write=True)) as conn, conn:
                conn.execute("UPDATE listener SET state='paused',error='listener_identity_unverifiable' WHERE id=1")
            self.service._alert('listener', 'failure', 'listener_identity_unverifiable')
            return
        if process is not None:
            return
        if row['state'] == 'running':
            self._failed(row['attempts'], 'listener_exited')
            return
        if row['state'] == 'stopping':
            with closing(_connect(self.config, write=True)) as conn, conn:
                conn.execute("UPDATE listener SET state='idle',attempts=0,next_run=0,pid=NULL,create_time=NULL,command=NULL,error=NULL WHERE id=1")
            return
        if row['next_run'] > self.service.clock():
            return
        try:
            from messaging import load_config
            from messaging.feishu import listener_target
            target = listener_target(load_config(self.settings['config']))
        except Exception as exc:
            from messaging.channels import DeliveryRejected
            if isinstance(exc, DeliveryRejected):
                with closing(_connect(self.config, write=True)) as conn, conn:
                    conn.execute("UPDATE listener SET state='paused',error=? WHERE id=1", (exc.code,))
                self.service._alert('listener', 'failure', exc.code)
            else:
                self._failed(row['attempts'] + 1, 'listener_start_failed')
            return
        legacy_command = [sys.executable, '-m', 'messaging.feishu', 'listen', '--config', self.settings['config'], '--stop-file', str(self.stop_file)]
        command = [*legacy_command, '--target', target['id']]
        # A crash can happen between spawning the child and saving its PID.
        candidates = []
        for process in psutil.process_iter(['cmdline', 'create_time']):
            try:
                if [p.casefold() for p in (process.info['cmdline'] or [])] in [
                        [p.casefold() for p in expected] for expected in (command, legacy_command)]:
                    candidates.append(process)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        if candidates:
            if len(candidates) > 1:
                with closing(_connect(self.config, write=True)) as conn, conn:
                    conn.execute("UPDATE listener SET state='paused',error='duplicate_listener_detected' WHERE id=1")
                self.service._alert('listener', 'failure', 'duplicate_listener_detected')
            else:
                process = candidates[0]
                with closing(_connect(self.config, write=True)) as conn, conn:
                    conn.execute("UPDATE listener SET state='running',pid=?,create_time=?,command=? WHERE id=1",
                                 (process.pid, process.info['create_time'], canonical(process.info['cmdline'])))
            return
        if row['state'] == 'starting':
            self._failed(row['attempts'], 'listener_start_interrupted')
            return
        attempts = row['attempts'] + 1
        with closing(_connect(self.config, write=True)) as conn, conn:
            conn.execute("UPDATE listener SET state='starting',attempts=? WHERE id=1", (attempts,))
        try:
            self.stop_file.unlink(missing_ok=True)
            self.stop_file.parent.mkdir(parents=True, exist_ok=True)
            # Listener SDK output can contain message payloads; it owns its durable event inbox.
            self.child = subprocess.Popen(command, cwd=self.config['_root'], env=dict(os.environ, PYTHONUTF8='1'),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            created = psutil.Process(self.child.pid).create_time()
            with closing(_connect(self.config, write=True)) as conn, conn:
                conn.execute("UPDATE listener SET state='running',pid=?,create_time=?,command=?,error=NULL WHERE id=1",
                             (self.child.pid, created, canonical(command)))
            if row['error']:
                self.service._alert('listener', 'recovery', 'listener_restarted')
        except Exception:
            self._failed(attempts, 'listener_start_failed')

    def stop(self):
        with closing(_connect(self.config)) as conn:
            row = conn.execute('SELECT * FROM listener WHERE id=1').fetchone()
        try:
            process = self._owned(row)
        except ValueError:
            return
        if process is None:
            return
        self.stop_file.touch()
        with closing(_connect(self.config, write=True)) as conn, conn:
            conn.execute("UPDATE listener SET state='stopping' WHERE id=1")
        try:
            process.wait(timeout=10)
        except (psutil.TimeoutExpired, psutil.AccessDenied):
            return
        with closing(_connect(self.config, write=True)) as conn, conn:
            conn.execute("UPDATE listener SET state='idle',attempts=0,next_run=0,pid=NULL,create_time=NULL,command=NULL,error=NULL WHERE id=1")
