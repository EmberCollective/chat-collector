"""Preserve the combined collector CLI behavior through independent services."""
import time

from common.locking import InstanceLock
from collector.service import collect_once
from selection import run_window
from selection.store import Store


def run_round(config, *, start=None, end=None):
    storage = config['storage']
    # The application lock serializes combined rounds without nesting selection's lock.
    with InstanceLock(storage['state_file'] + '.application'):
        with InstanceLock(storage['state_file']):
            store = Store(storage['state_file'], config['platform'], config['source']['account_id'])
            try:
                recovered = store.export(storage['output_dir'])
            finally:
                store.close()
        window_end = int(time.time()) if end is None else end
        scheduled = start is None and end is None
        snapshot = collect_once(config, force=not scheduled)
        result = run_window(config, start, window_end, advance=scheduled)
        if snapshot.get('status') == 'waiting_for_refresh' and result.get('status') == 'no_new_time_window':
            result.update(status='waiting_for_refresh', retry_after_seconds=snapshot['retry_after_seconds'])
        result.update(refresh=snapshot['refresh'], batches=recovered+result['batches'])
        return result
