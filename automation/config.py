from pathlib import Path
import tomllib
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


JOBS = ('wechat', 'qq', 'processor', 'messaging', 'feedback')


def load_config(path):
    path = Path(path).resolve()
    with path.open('rb') as stream:
        config = tomllib.load(stream)
    config['_config_path'] = str(path)
    config['_root'] = str(Path(__file__).resolve().parents[1])
    if type(config.setdefault('enabled', False)) is not bool:
        raise ValueError('enabled must be boolean')
    storage = config.setdefault('storage', {})
    value = storage.setdefault('state_file', '../data/automation/state.db')
    if not isinstance(value, str) or not value:
        raise ValueError('storage.state_file must be a path')
    storage['state_file'] = str((path.parent / value).resolve())
    if Path(storage['state_file']) == path:
        raise ValueError('State must not overwrite configuration')
    schedule = config.setdefault('scheduler', {})
    schedule.setdefault('timezone', 'Asia/Shanghai')
    try:
        ZoneInfo(schedule['timezone'])
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        raise ValueError('scheduler.timezone must be an IANA timezone') from None
    _number(schedule, 'control_interval_seconds', 5, 1, 60)
    _number(schedule, 'worker_threads', 8, 2, 64)
    retry = config.setdefault('retry', {})
    _retry(retry, {})
    jobs = config.setdefault('jobs', {})
    if not isinstance(jobs, dict) or set(jobs) - set(JOBS):
        raise ValueError('Unknown automation job')
    for name in JOBS:
        entry = jobs.setdefault(name, {})
        _entry(entry, path.parent, 'processor.toml' if name == 'processor' else
               'messaging.toml' if name in {'messaging', 'feedback'} else name + '.toml')
        _number(entry, 'interval_seconds', 5 if name == 'feedback' else 60, 1, 86400)
        _retry(entry, retry)
    listener = config.setdefault('listener', {})
    _entry(listener, path.parent, 'messaging.toml')
    _number(listener, 'check_interval_seconds', 5, 1, 60)
    _retry(listener, retry)
    alerts = config.setdefault('alerts', {})
    _entry(alerts, path.parent, 'messaging.toml')
    if type(alerts.setdefault('recovery_enabled', True)) is not bool:
        raise ValueError('alerts.recovery_enabled must be boolean')
    _number(alerts, 'cooldown_seconds', 3600, 1, 86400 * 30)
    target = alerts.setdefault('target_id', '')
    if not isinstance(target, str):
        raise ValueError('alerts.target_id must be text')
    protected = {Path(config['_config_path']), *[Path(v['config']) for v in jobs.values()], Path(listener['config']), Path(alerts['config'])}
    if Path(storage['state_file']) in protected:
        raise ValueError('State must not overwrite job configurations')
    return config


def _entry(entry, base, default):
    if not isinstance(entry, dict) or type(entry.setdefault('enabled', False)) is not bool:
        raise ValueError('Every entry needs a boolean enabled value')
    value = entry.setdefault('config', default)
    if not isinstance(value, str) or not value:
        raise ValueError('Every entry needs a configuration path')
    entry['config'] = str((base / value).resolve())


def _number(entry, key, default, low, high):
    value = entry.setdefault(key, default)
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'{key} must be an integer in [{low}, {high}]')


def _retry(entry, defaults):
    _number(entry, 'max_attempts', defaults.get('max_attempts', 3), 1, 20)
    _number(entry, 'backoff_seconds', defaults.get('backoff_seconds', 30), 1, 86400)
    _number(entry, 'max_backoff_seconds', defaults.get('max_backoff_seconds', 900), 1, 86400)
    if entry['max_backoff_seconds'] < entry['backoff_seconds']:
        raise ValueError('max_backoff_seconds must be >= backoff_seconds')
