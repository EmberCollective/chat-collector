"""Synthetic launch checks; never start configured workers or contact chat clients."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import socket
import sys
import time
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import psutil
import pytest
import tomllib

from tools.install import launcher, startup


def settings(root, port):
    path = root / 'tools/console/console.toml'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'# keep comment\nport = {port}\ncollector_root = "../.."\n', encoding='utf-8')
    return path


def test_free_configured_port_preserves_entire_file(tmp_path):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    path = settings(tmp_path, port)
    before = path.read_bytes()
    assert launcher.console_port(path) == port
    assert path.read_bytes() == before


def test_occupied_port_is_replaced_and_persisted_without_touching_listener(tmp_path):
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        occupied = listener.getsockname()[1]
        path = settings(tmp_path, occupied)
        chosen = launcher.console_port(path)
        assert chosen != occupied
        assert tomllib.loads(path.read_text())['port'] == chosen
        assert '# keep comment' in path.read_text()
        with socket.create_connection(('127.0.0.1', occupied), timeout=1):
            pass
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', chosen))


def test_windows_reserved_port_uses_bind_instead_of_connect(tmp_path, monkeypatch):
    path = settings(tmp_path, 10400)
    original = socket.socket
    binds = []
    class ReservedSocket:
        def __init__(self): self.socket = original()
        def __enter__(self): return self
        def __exit__(self, *args): self.socket.close()
        def setsockopt(self, *args): self.socket.setsockopt(*args)
        def getsockname(self): return self.socket.getsockname()
        def bind(self, address):
            binds.append(address)
            if address[1] == 10400:
                raise PermissionError(10013, 'synthetic Windows excluded port')
            self.socket.bind(address)
    monkeypatch.setattr(socket, 'socket', ReservedSocket)
    chosen = launcher.console_port(path)
    assert binds == [('127.0.0.1', 10400), ('127.0.0.1', 0)]
    assert tomllib.loads(path.read_text())['port'] == chosen


@pytest.mark.parametrize('port', [True, 0, 65536, '10400'])
def test_invalid_port_does_not_replace_config(tmp_path, port):
    import tomlkit
    path = settings(tmp_path, 10400)
    path.write_text(tomlkit.dumps({'port': port}), encoding='utf-8')
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match='端口无效'):
        launcher.console_port(path)
    assert path.read_bytes() == before


def test_session_requires_this_app_root_process_and_listener(tmp_path, monkeypatch):
    script = (tmp_path / 'tools/console/app.py').resolve()
    session = {'application': 'chat-collector-console', 'root': str(tmp_path), 'pid': 123,
               'token': 'synthetic-token-' * 3}
    process = Mock()
    process.cmdline.return_value = ['pythonw.exe', str(script)]
    process.cwd.return_value = str(tmp_path)
    connection = SimpleNamespace(status=psutil.CONN_LISTEN,
                                 laddr=SimpleNamespace(ip='127.0.0.1', port=12345))
    process.net_connections.return_value = [connection]
    monkeypatch.setattr(psutil, 'Process', lambda pid: process)
    client = Mock()
    def check(payload):
        client.get.return_value = httpx.Response(200, json=payload)
        return launcher.console_ready(client, 'http://127.0.0.1:12345', tmp_path, script)
    assert check(session)
    for payload in ({}, {'token': 'x' * 43}, [], dict(session, root=str(tmp_path / 'other')),
                    dict(session, application='unrelated'), dict(session, pid=True)):
        assert not check(payload)
    process.cmdline.return_value = ['pythonw.exe', str(tmp_path / 'unrelated.py')]
    assert not check(session)
    process.cmdline.return_value = ['pythonw.exe', str(script)]
    process.net_connections.return_value = []
    assert not check(session)


@pytest.fixture
def fake_launch(tmp_path, monkeypatch):
    from automation import control
    import automation
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config/automation.toml').write_text('')
    settings(tmp_path, 10400)
    monkeypatch.setattr(automation, 'load_config', lambda path: {'_config_path': str(path)})
    monkeypatch.setattr(control, 'start', Mock(return_value={'state': 'running'}))
    monkeypatch.setattr(control, 'status', Mock(return_value={'running': True}))
    monkeypatch.setattr(psutil, 'process_iter', lambda: [])
    monkeypatch.setattr(launcher, 'console_ready', lambda *args: False)
    return tmp_path


def test_simultaneous_login_and_start_wait_and_reuse_console(fake_launch, monkeypatch):
    ready = []
    calls = []
    monkeypatch.setattr(launcher, 'console_ready', lambda *args: bool(ready))
    def spawn(arguments, root):
        assert root == fake_launch
        assert arguments == [str(root / 'tools/console/app.py')]
        calls.append(arguments)
        time.sleep(.2)
        ready.append(True)
        return Mock(poll=lambda: None)
    monkeypatch.setattr(launcher, 'spawn', spawn)
    browser = Mock()
    monkeypatch.setattr(launcher.webbrowser, 'open', browser)
    with ThreadPoolExecutor(max_workers=2) as pool:
        login = pool.submit(launcher.start_all, fake_launch)
        manual = pool.submit(launcher.start_all, fake_launch, open_browser=True)
        login_result, manual_result = login.result(timeout=5), manual.result(timeout=5)
        assert login_result == manual_result
        assert login_result['automation'] == 'running'
    assert len(calls) == 1
    browser.assert_called_once()


def test_failed_child_reports_exit_code_and_log(fake_launch, monkeypatch):
    monkeypatch.setattr(launcher, 'console_port', lambda path: 12000)
    monkeypatch.setattr(launcher, 'spawn', lambda *args: Mock(poll=lambda: 17, returncode=17))
    with pytest.raises(RuntimeError, match='退出码 17.*console.log'):
        launcher.start_all(fake_launch)


def test_hidden_child_logs_stderr_and_uses_supplied_root(tmp_path):
    log = tmp_path / 'data/install/console.log'
    log.parent.mkdir(parents=True)
    log.write_text('old launch log')
    child = launcher.spawn(['-c', "import os, sys; print(os.getcwd()); print('synthetic failure', file=sys.stderr); sys.exit(17)"], tmp_path)
    assert child.wait(timeout=10) == 17
    text = log.read_text(encoding='utf-8')
    assert str(tmp_path) in text and 'synthetic failure' in text
    assert 'old launch log' not in text


def test_failure_status_exists_before_error_dialog(tmp_path, monkeypatch):
    import ctypes
    monkeypatch.setattr(launcher, 'ROOT', tmp_path)
    monkeypatch.setattr(sys, 'argv', ['launcher.py', '--open'])
    monkeypatch.setattr(launcher, 'start_all', Mock(side_effect=RuntimeError('synthetic failure')))
    def dialog(*args):
        saved = json.loads((tmp_path / 'data/install/launch-status.json').read_text(encoding='utf-8'))
        assert saved == {'ok': False, 'error_type': 'RuntimeError', 'message': 'synthetic failure'}
    monkeypatch.setattr(ctypes.windll.user32, 'MessageBoxW', dialog)
    assert launcher.main() == 1


def test_login_command_is_absolute_hidden_and_does_not_open_browser(tmp_path, monkeypatch):
    import hashlib
    private = tmp_path / 'installation with spaces/.runtime/python/pythonw.exe'
    private.parent.mkdir(parents=True)
    private.touch()
    root = private.parents[2]
    monkeypatch.chdir(tmp_path)
    name, command, executable = startup._entry(Path('installation with spaces'))
    assert executable == private
    assert name == 'ChatCollector-' + hashlib.sha256(str(root).lower().encode()).hexdigest()[:12]
    assert command == launcher.subprocess.list2cmdline([str(private), str(root / 'tools/install/launcher.py')])
    assert '--open' not in command and '.cmd' not in command


def test_start_cmd_sets_installation_working_directory():
    command = (launcher.ROOT / 'Start.cmd').read_text(encoding='utf-8')
    assert 'start "" /d "%~dp0"' in command
