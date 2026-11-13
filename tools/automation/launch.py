"""Explicit local launcher; child services never open a console window."""
from pathlib import Path
import os
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'runtime-deps'))


def main():
    from automation import check, load_config, request_stop
    config = load_config(ROOT / 'config' / 'automation.toml')
    if len(sys.argv) > 1 and sys.argv[1] == 'stop':
        request_stop(config)
        print('Stop requested. Running jobs finish safely before exit.')
        return 0
    if not config['enabled'] or not check(config)['ready']:
        print('Automation is disabled or configuration is incomplete. Run python -m automation check first.')
        return 1
    executable = Path(sys.executable)
    hidden_python = executable.with_name('pythonw.exe')
    if hidden_python.is_file():
        executable = hidden_python
    folder = Path(config['storage']['state_file']).parent
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / 'service.log').open('ab') as out, (folder / 'service-errors.log').open('ab') as err:
        process = subprocess.Popen([str(executable), '-m', 'automation', 'run', '--config', config['_config_path']],
            cwd=ROOT, env=dict(os.environ, PYTHONUTF8='1', PYTHONPATH=str(ROOT / 'runtime-deps') + os.pathsep + os.environ.get('PYTHONPATH', '')),
            stdin=subprocess.DEVNULL, stdout=out, stderr=err,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    print(f'Hidden automation start requested (PID {process.pid}); use automation status to verify.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
