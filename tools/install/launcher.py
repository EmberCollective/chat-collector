"""Start configured workers quietly; the optional console never owns their schedules."""
from pathlib import Path
import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import webbrowser

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'runtime-deps')]


def spawn(arguments, root):
    quiet = Path(sys.executable).with_name('pythonw.exe')
    log = root / 'data/install/console.log'
    log.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PYTHONUTF8='1')
    env['PYTHONPATH'] = os.pathsep.join(filter(None, [str(root), str(root / 'runtime-deps'), env.get('PYTHONPATH')]))
    with log.open('wb') as stream:
        return subprocess.Popen([str(quiet if quiet.exists() else sys.executable), *arguments], cwd=root,
            env=env, stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def console_process(process, script):
    import psutil
    try:
        return any((Path(process.cwd()) / arg).resolve() == script
                   for arg in process.cmdline()[1:] if not arg.startswith('-'))
    except (OSError, ValueError, psutil.Error):
        return False


def console_ready(client, url, root, script):
    import httpx
    import psutil
    try:
        response = client.get(url + '/api/session')
        if response.status_code != 200:
            return False
        session = response.json()
        if (session.get('application') != 'chat-collector-console'
                or Path(session.get('root', '')).resolve() != root
                or not isinstance(session.get('token'), str) or len(session['token']) < 32
                or type(session.get('pid')) is not int):
            return False
        process = psutil.Process(session['pid'])
        return console_process(process, script) and any(
            connection.status == psutil.CONN_LISTEN
            and connection.laddr.ip == '127.0.0.1'
            and connection.laddr.port == int(url.rsplit(':', 1)[1])
            for connection in process.net_connections(kind='tcp'))
    except (httpx.HTTPError, ValueError, TypeError, AttributeError, OSError, psutil.Error):
        return False


def console_port(path):
    import tomlkit
    document = tomlkit.parse(path.read_text(encoding='utf-8'))
    port = document.get('port', 10400)
    if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
        raise RuntimeError('调试台端口无效，请运行 Configure.cmd 检查配置。')
    with socket.socket() as sock:
        if os.name == 'nt':
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            sock.bind(('127.0.0.1', port))
            return port
        except OSError:
            # Let Windows choose outside occupied and system-reserved ranges.
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
    document['port'] = port
    pending = path.with_suffix('.tmp')
    pending.write_text(tomlkit.dumps(document), encoding='utf-8')
    os.replace(pending, path)
    return port


def start_all(root=ROOT, *, open_browser=False):
    import httpx
    import psutil
    from automation import load_config, control
    from common.locking import InstanceLock
    import tomllib
    root = Path(root).resolve()
    if not (root / 'config/automation.toml').is_file() or ((root / 'data/install/progress.json').exists() and not (root / 'data/install/ready.json').is_file()):
        raise RuntimeError('请先运行 Configure.cmd 完成配置和首次处理。')
    lock = InstanceLock(root / 'data/install/console-launch', timeout=90)
    try:
        lock.__enter__()
    except RuntimeError:
        raise RuntimeError('另一次启动仍在进行，请稍后重试；详情见 data/install/launch-status.json。') from None
    try:
        config = load_config(root / 'config/automation.toml')
        result = control.start(config)
        if result['state'] not in {'starting', 'running'}:
            raise RuntimeError('自动服务未能启动，请在调试台核对运行状态。')
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not control.status(config)['running']:
            time.sleep(.5)
        if not control.status(config)['running']:
            raise RuntimeError('自动服务未在 30 秒内进入运行状态。')
        settings_path = root / 'tools/console/console.toml'
        settings = tomllib.loads(settings_path.read_text(encoding='utf-8'))
        url = f"http://127.0.0.1:{settings.get('port', 10400)}"
        script = (root / 'tools/console/app.py').resolve()
        with httpx.Client(trust_env=False, timeout=2) as client:
            if not console_ready(client, url, root, script):
                existing = any(console_process(process, script) for process in psutil.process_iter())
                child = None
                if not existing:
                    url = f'http://127.0.0.1:{console_port(settings_path)}'
                    child = spawn([str(script)], root)
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if child and child.poll() is not None:
                        raise RuntimeError(f'调试台启动失败（退出码 {child.returncode}），请查看 data/install/console.log。')
                    if console_ready(client, url, root, script):
                        break
                    time.sleep(.5)
                else:
                    raise RuntimeError('调试台启动超时；自动服务可能已运行，请查看 data/install/console.log 和 launch-status.json。')
    finally:
        lock.__exit__(None, None, None)
    if open_browser:
        webbrowser.open(url)
    return {'automation': 'running', 'console': url}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--open', action='store_true')
    args = parser.parse_args()
    status = ROOT / 'data/install/launch-status.json'
    status.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = start_all(ROOT, open_browser=args.open)
        result['ok'] = True
    except Exception as exc:
        result = {'ok': False, 'error_type': type(exc).__name__,
                  'message': str(exc) if isinstance(exc, RuntimeError) else '启动失败，请运行 Configure.cmd 检查配置。'}
    # Record the failure before a user-facing dialog can block the launcher.
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=status.parent, suffix='.tmp', delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(result, stream, ensure_ascii=False)
    try:
        os.replace(temporary, status)
    finally:
        temporary.unlink(missing_ok=True)
    if not result['ok'] and args.open and os.name == 'nt':
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, result['message'], '聊天待办启动失败', 0x10)
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
