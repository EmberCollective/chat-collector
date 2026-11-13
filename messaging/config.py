"""Message delivery is configured independently from collection and processing."""
from pathlib import Path
import tomllib
import datetime as dt
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEADLINE_DEFAULTS = {'P0': [86400, 21600, 7200, 1800, 0], 'P1': [21600, 7200, 1800, 0],
                     'P2': [7200, 1800, 0], 'P3': [1800, 0], 'P4': [0]}


def load_config(path):
    path = Path(path).resolve()
    with path.open('rb') as stream:
        config = tomllib.load(stream)
    if type(config.setdefault('enabled', False)) is not bool:
        raise ValueError('enabled must be a boolean')
    config['env_file'] = str((path.parent / config.get('env_file', '../.env')).resolve())
    for section, key, default in [('input', 'batches_dir', '../data/todos'),
                                 ('storage', 'state_file', '../data/messaging/state.db'),
                                 ('storage', 'assets_dir', '../data/messaging/assets')]:
        value = config.setdefault(section, {}).setdefault(key, default)
        if not isinstance(value, str) or not value:
            raise ValueError(f'{section}.{key} must be a path')
        config[section][key] = str((path.parent / value).resolve())
    incoming = Path(config['input']['batches_dir'])
    state = Path(config['storage']['state_file'])
    assets = Path(config['storage']['assets_dir'])
    if (state == incoming or incoming in state.parents or state in incoming.parents
            or assets == incoming or incoming in assets.parents or assets in incoming.parents
            or state == assets or assets in state.parents or state in assets.parents
            or state in {path, Path(config['env_file'])}):
        raise ValueError('Message state and assets must be separate from input and configuration')
    roots = config.setdefault('attachments', {}).setdefault('allowed_roots',
        ['../data/assets', '../data/processor/attachment-cache'])
    if not isinstance(roots, list) or any(not isinstance(p, str) or not p for p in roots):
        raise ValueError('attachments.allowed_roots must be an array of paths')
    config['attachments']['allowed_roots'] = [str((path.parent / p).resolve()) for p in roots]
    feedback = config.setdefault('feedback', {})
    if type(feedback.setdefault('enabled', False)) is not bool:
        raise ValueError('feedback.enabled must be a boolean')
    for key, default, maximum in [('retry_base_seconds', 60, 86400), ('max_attempts', 5, 20)]:
        value = feedback.setdefault(key, default)
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError('feedback.' + key + ' is outside its allowed range')
    for key, default in [('state_file', '../data/messaging/feedback.db'), ('processor_config', 'processor.toml')]:
        value = feedback.setdefault(key, default)
        if not isinstance(value, str) or not value:
            raise ValueError('feedback.' + key + ' must be a path')
        feedback[key] = str((path.parent / value).resolve())
    source = config['input'].get('processor_config')
    if source is None and feedback['enabled']:
        source = feedback['processor_config']
    if source is not None:
        if not isinstance(source, str) or not source:
            raise ValueError('input.processor_config must be a configuration path')
        config['input']['processor_config'] = str((path.parent / source).resolve())
    feedback_state = Path(feedback['state_file'])
    if (feedback_state in {state, path, Path(config['env_file']), Path(feedback['processor_config'])}
            or any(feedback_state == root or root in feedback_state.parents for root in (incoming, assets))):
        raise ValueError('Feedback state must be separate from message state, input, assets and configuration')
    for section, key, default, minimum, maximum in [
            ('schedule', 'interval_seconds', 60, 1, 86400),
            ('schedule', 'retry_base_seconds', 60, 1, 86400),
            ('schedule', 'max_attempts', 5, 1, 20),
            ('schedule', 'max_deliveries_per_round', 100, 1, 1000),
            *[('reminders', priority, seconds, 0, 31536000) for priority, seconds in
              [('P0', 3600), ('P1', 14400), ('P2', 86400), ('P3', 604800), ('P4', 0)]]]:
        value = config.setdefault(section, {}).setdefault(key, default)
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f'{section}.{key} must be an integer in [{minimum}, {maximum}]')
    deadlines = config.setdefault('deadline_reminders', {})
    if not isinstance(deadlines, dict):
        raise ValueError('deadline_reminders must be a table')
    if type(deadlines.setdefault('enabled', True)) is not bool:
        raise ValueError('deadline_reminders.enabled must be boolean')
    try:
        ZoneInfo(deadlines.setdefault('timezone', 'Asia/Shanghai'))
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        raise ValueError('deadline_reminders.timezone must be an IANA timezone') from None
    for priority, defaults in DEADLINE_DEFAULTS.items():
        offsets = deadlines.setdefault(priority, list(defaults))
        if (not isinstance(offsets, list) or len(offsets) > 32
                or any(type(v) is not int or not 0 <= v <= 31536000 for v in offsets)
                or len(offsets) != len(set(offsets))):
            raise ValueError('deadline_reminders.' + priority + ' must contain unique nonnegative seconds')
        deadlines[priority] = sorted(offsets, reverse=True)
    initial = config.setdefault('initial_notifications', {})
    if not isinstance(initial, dict) or any(type(initial.setdefault(p, True)) is not bool for p in DEADLINE_DEFAULTS):
        raise ValueError('initial_notifications must contain priority booleans')
    daily = config.setdefault('daily_digest', {})
    if not isinstance(daily, dict) or type(daily.setdefault('enabled', False)) is not bool:
        raise ValueError('daily_digest.enabled must be boolean')
    clock = daily.setdefault('time', '08:00')
    try:
        if not isinstance(clock, str) or not re.fullmatch(r'\d{2}:\d{2}', clock):
            raise ValueError()
        dt.time.fromisoformat(clock)
        ZoneInfo(daily.setdefault('timezone', 'Asia/Shanghai'))
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        raise ValueError('daily_digest needs HH:MM time and an IANA timezone') from None
    if type(daily.setdefault('catch_up_minutes', 60)) is not int or not 1 <= daily['catch_up_minutes'] <= 1440:
        raise ValueError('daily_digest.catch_up_minutes must be between 1 and 1440')
    targets = config.setdefault('targets', [])
    if not isinstance(targets, list):
        raise ValueError('targets must be an array')
    identifiers = set()
    for target in targets:
        if not isinstance(target, dict) or any(not isinstance(target.get(k), str) or not target[k].strip() for k in ('id', 'kind')):
            raise ValueError('Every target needs nonempty id and kind')
        if target['id'] in identifiers or type(target.setdefault('enabled', False)) is not bool:
            raise ValueError('Target IDs must be unique and enabled must be a boolean')
        identifiers.add(target['id'])
        timeout = target.setdefault('timeout_seconds', 30)
        if type(timeout) is not int or not 1 <= timeout <= 300:
            raise ValueError('Target timeout_seconds must be between 1 and 300')
        snooze = target.setdefault('snooze_seconds', 3600)
        if type(snooze) is not int or not 1 <= snooze <= 31536000:
            raise ValueError('Target snooze_seconds must be between 1 and 31536000')
    config['_config_path'] = str(path)
    return config
