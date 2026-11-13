"""Best-effort local scheduling hints; durable inboxes remain the source of truth."""
from __future__ import annotations

import hmac
import json
import os
from pathlib import Path
import secrets
import socket
import threading


def _path(config_path):
    path = Path(config_path).resolve()
    return path.with_name('.' + path.name + '.wakeup.json')


def notify(config_path, job):
    """Only wake feedback/delivery; never transmit task content or wait for work."""
    if not config_path or job not in {'feedback', 'messaging'}:
        return False
    try:
        with _path(config_path).open('rb') as stream:
            endpoint = json.loads(stream.read(4096))
        if job not in endpoint['jobs'] or type(endpoint['port']) is not int or not 1 <= endpoint['port'] <= 65535:
            return False
        token = endpoint['token']
        if not isinstance(token, str) or len(token) != 64:
            return False
        data = json.dumps({'token': token, 'job': job}).encode('ascii')
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.settimeout(.1)
            sender.sendto(data, ('127.0.0.1', endpoint['port']))
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


class WakeupListener:
    def __init__(self, routes, callback):
        self.routes, self.callback = routes, callback
        self.token = secrets.token_hex(32)
        self.stop_event = threading.Event()
        self.paths = []
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(('127.0.0.1', 0))
        self.socket.settimeout(.2)
        self.thread = threading.Thread(target=self._receive, name='local-wakeup', daemon=True)

    def start(self):
        try:
            for config_path, jobs in self.routes.items():
                path = _path(config_path)
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_name(path.name + '.' + self.token + '.tmp')
                try:
                    with temporary.open('x', encoding='utf-8') as stream:
                        json.dump({'port': self.socket.getsockname()[1], 'token': self.token, 'jobs': sorted(jobs)}, stream)
                    os.replace(temporary, path)
                    self.paths.append(path)
                finally:
                    temporary.unlink(missing_ok=True)
            self.thread.start()
        except Exception:
            self.close()
            raise
        return self

    def _receive(self):
        while not self.stop_event.is_set():
            try:
                data, address = self.socket.recvfrom(1024)
                if address[0] != '127.0.0.1':
                    continue
                value = json.loads(data)
                token, job = value.get('token'), value.get('job')
                if isinstance(token, str) and hmac.compare_digest(token, self.token) and job in {'feedback', 'messaging'}:
                    self.callback(job)
            except (OSError, ValueError, TypeError, AttributeError):
                continue
            except Exception:
                # Persistent queues and the scheduler's normal polling recover lost hints.
                continue

    def close(self):
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=1)
        self.socket.close()
        for path in self.paths:
            try:
                if json.loads(path.read_text(encoding='utf-8')).get('token') == self.token:
                    path.unlink(missing_ok=True)
            except (OSError, ValueError, AttributeError):
                pass
