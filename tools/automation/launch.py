"""Explicit local launcher; child services never open a console window."""
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'runtime-deps'))


def main():
    from automation import load_config
    from automation.control import start, stop
    config = load_config(ROOT / 'config' / 'automation.toml')
    if len(sys.argv) > 1 and sys.argv[1] == 'stop':
        stop(config)
        print('Stop requested. Running jobs finish safely before exit.')
        return 0
    result = start(config)
    print('Automation: ' + result['state'] + '; use automation status to verify.')
    return 0 if result['state'] in {'starting', 'running'} else 1


if __name__ == '__main__':
    raise SystemExit(main())
