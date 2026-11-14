"""SQL selection policy, merged provenance, durable events and publication."""
import datetime
import time

from common.codec import canonical
from common.locking import InstanceLock
from providers.query import query_rules, prepare_selected, selection_window_end
from .rules import load_rulesets
from .store import Store


def scan_filtered(config, start, end, *, rulesets=None):
    """Merge every rule's hits without excluding another rule's input."""
    if end <= start:
        raise ValueError('end must be after start')
    rulesets = load_rulesets(config) if rulesets is None else rulesets
    if not rulesets:
        return
    _, messages = _select(config, start, end, rulesets)
    yield from messages


def _scope(config):
    return dict(provider_id=config.get('provider_id', config['platform']),
                platform=config['platform'], account_id=config['source']['account_id'], config=config)


def _select(config, start, end, rulesets):
    result = query_rules(_scope(config),
                         dict(start_inclusive=start, end_exclusive=end), rulesets)
    provenance = {rule['id']: rule['provenance'] for rule in rulesets}
    matches = {}
    for hit in result['hits']:
        rule = provenance[hit['rule_id']]
        matches.setdefault(hit['identity'], {})[canonical(rule)] = rule
    messages = []
    for row in result['messages']:
        rules = matches.get(row['identity'])
        if rules:
            messages.append(dict(row['message'], matched_rules=[rules[key] for key in sorted(rules)]))
    return result['snapshot_id'], messages


def run_round(config, start=None, end=None):
    """Select from a verified source snapshot without refreshing the collector."""
    return run_window(config, start, end, advance=start is None and end is None)


def run_window(config, start, end, *, advance):
    """Publish an explicit window; application composition can freeze it before refresh."""
    storage = config['storage']
    with InstanceLock(storage['state_file']):
        store = Store(storage['state_file'], config['platform'], config['source']['account_id'])
        try:
            recovered = store.export(storage['output_dir'])
            rulesets = load_rulesets(config)
            if not rulesets:
                return dict(platform=config['platform'], status='no_enabled_rules',
                            matched=0, emitted=0, duplicates=0, batches=recovered)
            window_end = int(time.time()) if end is None else end
            if advance:
                window_end = selection_window_end(_scope(config), window_end)
            previous = store.get('window_end')
            window_start = start if start is not None else (
                int(previous) if previous else window_end-config['schedule']['initial_lookback_seconds'])
            if window_start >= window_end or (advance and previous and int(previous) >= window_end):
                if not advance:
                    raise ValueError('start must be before end')
                return dict(platform=config['platform'], status='no_new_time_window', batches=recovered)
            snapshot_id, messages = _select(config, window_start, window_end, rulesets)
            messages = prepare_selected(config, messages, snapshot_id=snapshot_id,
                                        end_exclusive=window_end if advance else None)
            result = store.ingest(messages, window_start, window_end,
                                  datetime.datetime.now(datetime.timezone.utc).isoformat(), advance=advance)
            result.update(platform=config['platform'], start=window_start, end=window_end,
                          intensity=config.get('processing', {}).get('intensity', 'full'),
                          attachments_enabled=config.get('attachments', {}).get('enabled', False),
                          batches=recovered+store.export(storage['output_dir']))
            return result
        finally:
            store.close()
