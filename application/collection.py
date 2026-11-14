"""Preserve the combined collector CLI behavior through independent services."""
import time

from common.locking import InstanceLock
from collector.service import collect_once
from selection import run_window


def run_round(config, *, start=None, end=None):
    storage = config['storage']
    # Collection must run even when selection state or publication needs repair.
    with InstanceLock(storage['state_file'] + '.application'):
        window_end = int(time.time()) if end is None else end
        scheduled = start is None and end is None
        snapshot = collect_once(config, force=not scheduled)
        result = run_window(config, start, window_end, advance=scheduled)
        if snapshot.get('status') == 'waiting_for_refresh' and result.get('status') == 'no_new_time_window':
            result.update(status='waiting_for_refresh', retry_after_seconds=snapshot['retry_after_seconds'])
        result.update(refresh=snapshot['refresh'])
        return result
