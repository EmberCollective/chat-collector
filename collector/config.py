import importlib.util
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
    for section, names in [(source, ('encrypted_dir', 'decrypted_dir', 'key_file', 'database', 'media_dir', 'resource_db', 'image_cache_dir', 'decoder_dll')),
                           (config.setdefault('storage', {}), ('state_file', 'output_dir')),
                           (config.setdefault('attachments', {}), ('archive_dir',)),
                           (config.setdefault('filters', {}), ('sql_file', 'code_file'))]:
        for name in names:
            if section.get(name):
                value = Path(section[name]).expanduser()
                section[name] = str(value if value.is_absolute() else path.parent / value)
    if not config['storage'].get('state_file') or not config['storage'].get('output_dir'):
        raise ValueError('storage.state_file and output_dir are required')
    if not isinstance(source.get('refresh', True), bool):
        raise ValueError('source.refresh must be true or false, without quotes')
    for name in ('mentions_all', 'files'):
        if not isinstance(config['filters'].get(name, False), bool):
            raise ValueError(f'filters.{name} must be true or false, without quotes')
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


def load_filter_extension(settings):
    spec = importlib.util.spec_from_file_location('collector_local_filter', settings['code_file'])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_filter(config):
    from .filters import SqlRules
    settings = config.get('filters', {})
    for name in ('mentions_all', 'files'):
        if not isinstance(settings.get(name, False), bool):
            raise ValueError(f'filters.{name} must be a boolean')
    compiler = SqlRules(prefix='_f_p')
    params = compiler.params
    clauses = []
    groups = settings.get('conversation_ids', [])
    keywords = settings.get('keywords', [])
    if not isinstance(groups, list) or not isinstance(keywords, list):
        raise ValueError('conversation_ids and keywords must be arrays')
    if any(not isinstance(x, str) or not x for x in [*groups, *keywords]):
        raise ValueError('group IDs and keywords must be nonempty strings')
    if groups:
        clauses.append(compiler.compile({'type':'conversation','values':groups}))
    content = []
    if settings.get('mentions_all', False):
        content.append(compiler.compile({'type':'mentions_all'}))
    if settings.get('files', False):
        content.append(compiler.compile({'type':'kind','values':['file']}))
    if keywords:
        content.append(compiler.compile({'type':'keywords','values':keywords}))
    match = settings.get('match', 'any')
    if match not in {'any', 'all'}:
        raise ValueError('filters.match must be any or all')
    if content:
        clauses.append('(' + (' OR ' if match == 'any' else ' AND ').join(content) + ')')
    extra = dict(settings.get('params', {}))
    if any(str(name).startswith('_') for name in extra):
        raise ValueError('custom parameter names starting with _ are reserved')
    if settings.get('sql_file'):
        clauses.append('(' + Path(settings['sql_file']).read_text(encoding='utf-8').strip() + ')')
    if settings.get('code_file'):
        module = load_filter_extension(settings)
        if hasattr(module,'build_filter'):
            sql, values = module.build_filter(settings)
        elif hasattr(module,'register_filters'):
            sql, values = '1', {}
        else:
            raise ValueError('Filter extension must define build_filter or register_filters')
        if not isinstance(sql, str) or not isinstance(values, dict):
            raise ValueError('build_filter must return (SQL expression, parameter dict)')
        if any(str(name).startswith('_') or name in extra for name in values):
            raise ValueError('duplicate or reserved custom SQL parameter')
        clauses.append('(' + sql + ')')
        extra.update(values)
    params.update(extra)
    return ' AND '.join(clauses) if clauses else '1', params
