"""Start configured workers quietly; the optional console never owns their schedules."""
from pathlib import Path
import argparse
import json
import os
import subprocess
import sys
import time
import webbrowser

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'runtime-deps')]


def spawn(arguments):
    quiet = Path(sys.executable).with_name('pythonw.exe')
    return subprocess.Popen([str(quiet if quiet.exists() else sys.executable), *arguments], cwd=ROOT,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def start_all(root=ROOT, *, open_browser=False):
    import httpx
    import psutil
    from automation import load_config, control
    from collector.__main__ import InstanceLock
    import tomllib
    if not (root / 'config/automation.toml').is_file() or ((root / 'data/install/progress.json').exists() and not (root / 'data/install/ready.json').is_file()):
        raise RuntimeError('请先运行 Configure.cmd 完成配置和首次处理。')
    config = load_config(root / 'config/automation.toml')
    result = control.start(config)
    if result['state'] not in {'starting', 'running'}:
        raise RuntimeError('自动服务未能启动，请在调试台核对运行状态。')
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not control.status(config)['running']:
        time.sleep(.5)
    if not control.status(config)['running']:
        raise RuntimeError('自动服务未在 30 秒内进入运行状态。')
    settings = tomllib.loads((root / 'tools/console/console.toml').read_text(encoding='utf-8'))
    url = f"http://127.0.0.1:{settings.get('port', 10400)}"
    script = (root / 'tools/console/app.py').resolve()
    with InstanceLock(root / 'data/install/console-launch'):
        existing = False
        for proc in psutil.process_iter(['cmdline']):
            try:
                if any(Path(arg).resolve() == script for arg in (proc.info['cmdline'] or [])[1:] if not arg.startswith('-')):
                    existing = True
                    break
            except (OSError, ValueError, psutil.Error):
                continue
        if not existing:
            import socket
            with socket.socket() as sock:
                if sock.connect_ex(('127.0.0.1', settings.get('port', 10400))) == 0:
                    raise RuntimeError('面板端口已被其他程序占用，请调整 console.toml 中的 port。')
            child = spawn([str(script)])
        else:
            child = None
        with httpx.Client(trust_env=False, timeout=2) as client:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                try:
                    if client.get(url + '/api/session').status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if child and child.poll() is not None:
                    raise RuntimeError('调试台启动失败，请检查运行依赖和配置。')
                time.sleep(.5)
            else:
                raise RuntimeError('调试台启动超时；自动服务可能已运行，请查看启动状态文件。')
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
        result = start_all(open_browser=args.open)
        result['ok'] = True
    except Exception as exc:
        result = {'ok': False, 'error_type': type(exc).__name__,
                  'message': str(exc) if isinstance(exc, RuntimeError) else '启动失败，请运行 Configure.cmd 检查配置。'}
        if args.open and os.name == 'nt':
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, result['message'], '聊天待办启动失败', 0x10)
    status.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
