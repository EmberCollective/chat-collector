"""Stop only the recorded legacy collector, while both core locks are held."""
from contextlib import ExitStack
import datetime
from pathlib import Path
import sys
import time

import psutil

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from bridge import Bridge, atomic_json


def main():
    bridge = Bridge(ROOT/'console.toml')
    legacy = bridge.external_runner()
    if legacy is None:
        print('No legacy collector is running. You can resume schedules in the console.')
        return
    if not legacy['verified']:
        raise RuntimeError(legacy['message'])
    process = psutil.Process(legacy['pid'])
    until = time.monotonic()+90
    while time.monotonic() < until:
        try:
            with ExitStack() as stack:
                for name in ('wechat', 'qq'):
                    stack.enter_context(bridge.lock(bridge.config(name)['storage']['state_file']))
                process.terminate()
                process.wait(timeout=10)
                atomic_json(ROOT/'.runtime/handover.json', {'previous_pid': process.pid, 'stopped_between_rounds': True,
                    'time_utc': datetime.datetime.now(datetime.timezone.utc).isoformat()})
                print('Legacy collector stopped between rounds. Checkpoints and original files preserved.')
                return
        except RuntimeError:
            time.sleep(1)
    raise RuntimeError('No idle interval found in 90 seconds; legacy collector was not stopped.')


if __name__ == '__main__':
    try:
        main()
    except psutil.AccessDenied:
        print('Windows denied process termination. Right-click handover.cmd and run as administrator.')
        raise SystemExit(1)
