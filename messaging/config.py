"""Message delivery is configured independently from collection and processing."""
from pathlib import Path
import tomllib


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
    config['_config_path'] = str(path)
    return config
