from pathlib import Path
import tomllib


def load_config(path):
    path = Path(path).resolve()
    with path.open('rb') as stream:
        config = tomllib.load(stream)
    if config.get('platform') not in {'wechat', 'qq'}:
        raise ValueError('platform must be wechat or qq')
    source = config['source']
    if not str(source.get('account_id', '')).strip():
        raise ValueError('source.account_id is required')
    if 'account_label' in source and (not isinstance(source['account_label'], str) or len(source['account_label'])>200):
        raise ValueError('source.account_label must be text up to 200 characters')
    for section, names in [(source, ('encrypted_dir', 'decrypted_dir', 'key_file', 'database', 'media_dir', 'resource_db', 'image_cache_dir', 'decoder_dll', 'work_dir')),
                           (config.setdefault('storage', {}), ('state_file', 'output_dir')),
                           (config.setdefault('attachments', {}), ('archive_dir',))]:
        for name in names:
            if section.get(name):
                value = Path(section[name]).expanduser()
                section[name] = str(value if value.is_absolute() else path.parent / value)
    if not config['storage'].get('state_file') or not config['storage'].get('output_dir'):
        raise ValueError('storage.state_file and output_dir are required')
    if not isinstance(source.get('refresh', True), bool):
        raise ValueError('source.refresh must be true or false, without quotes')
    if source.get('work_dir') and source.get('encrypted_dir'):
        work = Path(source['work_dir']).resolve()
        original = Path(source['encrypted_dir']).resolve()
        if work == original or original in work.parents:
            raise ValueError('source.work_dir must be outside the original encrypted directory')
    validate_filters(config.setdefault('filters', {}))
    attachments = config['attachments']
    if not isinstance(attachments.get('enabled', False), bool):
        raise ValueError('attachments.enabled must be a boolean')
    if attachments.get('enabled') and not attachments.get('archive_dir'):
        raise ValueError('attachments.archive_dir is required when enabled')
    roots = attachments.get('allowed_roots', [])
    if not isinstance(roots, list) or any(not isinstance(item, str) for item in roots):
        raise ValueError('attachments.allowed_roots must be an array of paths')
    attachments['allowed_roots'] = [str((path.parent / item).resolve()) for item in roots]
    if attachments.get('enabled'):
        archive = Path(attachments['archive_dir']).resolve()
        media_roots = [Path(source[name]).resolve() for name in ('encrypted_dir', 'decrypted_dir', 'media_dir', 'image_cache_dir') if source.get(name)]
        if any(archive == root or root in archive.parents for root in media_roots):
            raise ValueError('Attachment archive must be outside original data directories')
    state = Path(config['storage']['state_file']).resolve()
    output = Path(config['storage']['output_dir']).resolve()
    for name in ('encrypted_dir', 'decrypted_dir'):
        if source.get(name):
            folder = Path(source[name]).resolve()
            if any(target == folder or folder in target.parents for target in (state, output)):
                raise ValueError('State and batches must be stored outside source database directories')
    for name in ('database', 'key_file'):
        if source.get(name):
            input_path = Path(source[name]).resolve()
            if state == input_path or (state.exists() and input_path.exists() and state.samefile(input_path)):
                raise ValueError('State file must not be an input database or key file')
    intensity = config.setdefault('processing', {}).setdefault('intensity', 'full')
    if not isinstance(intensity, str) or intensity not in {'metadata', 'standard', 'full'}:
        raise ValueError('processing.intensity must be metadata, standard or full')
    schedule = config.setdefault('schedule', {})
    refresh_interval = schedule.setdefault('refresh_interval_seconds', 0)
    if isinstance(refresh_interval, bool) or not isinstance(refresh_interval, int) or refresh_interval < 0:
        raise ValueError('schedule.refresh_interval_seconds must be a nonnegative integer')
    for name, default in [('interval_seconds', 60), ('initial_lookback_seconds', 86400)]:
        value = schedule.setdefault(name, default)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f'schedule.{name} must be a positive integer')
    config['_config_path'] = str(path)
    return config


def validate_filters(settings):
    if not isinstance(settings, dict) or set(settings)-{'rulesets'}:
        raise ValueError('Only filters.rulesets is supported; migrate retired filter fields into SQL files (see FILTERS.md)')
