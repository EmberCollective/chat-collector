"""Preserve the combined collector CLI behavior through independent services."""
import time

from common.locking import InstanceLock
from collector.service import collect_once
from selection import run_window
from selection.rules import load_rulesets
from selection.store import Store


def run_round(config, *, start=None, end=None):
    storage = config['storage']
    # The application lock serializes combined rounds without nesting selection's lock.
    with InstanceLock(storage['state_file'] + '.application'):
        with InstanceLock(storage['state_file']):
            store = Store(storage['state_file'], config['platform'], config['source']['account_id'])
            try:
                recovered = store.export(storage['output_dir'])
                rules = load_rulesets(config)
                if not rules:
                    return dict(platform=config['platform'], status='no_enabled_rules',
                                matched=0, emitted=0, duplicates=0, batches=recovered)
                window_end = int(time.time()) if end is None else end
                previous = store.get('window_end')
                window_start = start if start is not None else (
                    int(previous) if previous else window_end-config['schedule']['initial_lookback_seconds'])
                if window_start >= window_end:
                    if start is not None:
                        raise ValueError('start must be before end')
                    return dict(platform=config['platform'], status='no_new_time_window', batches=recovered)
                if config['source'].get('refresh', True) and start is None and end is None and previous:
                    remaining = config['schedule'].get('refresh_interval_seconds', 0)-(window_end-int(previous))
                    if remaining > 0:
                        return dict(platform=config['platform'], status='waiting_for_refresh',
                                    retry_after_seconds=remaining, batches=recovered)
            finally:
                store.close()
        snapshot = collect_once(config)
        result = run_window(config, window_start, window_end, advance=start is None and end is None)
        result.update(refresh=snapshot['refresh'], batches=recovered+result['batches'])
        return result
