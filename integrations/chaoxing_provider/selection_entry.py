"""Explicit application composition: Chaoxing snapshot -> SQL -> selection outbox.

No network, login, model call or delivery is started by this command.
"""
import argparse
import json
from pathlib import Path
import tomllib

from providers.chaoxing import register, snapshot
from providers.query_registry import QUERY_PROVIDERS
from selection import run_round, scan_filtered
from selection.rules import validate_filters


def load_config(path):
    path = Path(path).resolve()
    with path.open('rb') as stream:
        config = tomllib.load(stream)
    if config.get('platform') != 'chaoxing':
        raise ValueError('platform_must_be_chaoxing')
    source = config['source']
    if not isinstance(source.get('account_id'), str) or not source['account_id'].strip():
        raise ValueError('account_id_required')
    for section, keys in ((source, ['database']), (config['storage'], ['state_file', 'output_dir'])):
        for key in keys:
            if not isinstance(section.get(key), str) or not section[key]:
                raise ValueError('storage_paths_required')
            section[key] = str((path.parent / section[key]).resolve())
    database = Path(source['database'])
    state = Path(config['storage']['state_file'])
    output = Path(config['storage']['output_dir'])
    if (state == database or database.parent == output or database.parent in output.parents
            or state.parent == database.parent or state == output or state in output.parents
            or (state.exists() and database.exists() and state.samefile(database))):
        raise ValueError('selection_storage_must_be_separate_from_capture')
    lookback = config.setdefault('schedule', {}).setdefault('initial_lookback_seconds', 3600)
    if type(lookback) is not int or lookback <= 0:
        raise ValueError('invalid_initial_lookback_seconds')
    validate_filters(config.setdefault('filters', {}))
    # Only ordinary text is currently verified. No pretend attachment preparation.
    if config.get('attachments', {}).get('enabled'):
        raise ValueError('chaoxing_attachments_not_supported')
    config['_config_path'] = str(path)
    config['provider_id'] = 'chaoxing'
    return config


def configure_provider():
    existing = QUERY_PROVIDERS.get('chaoxing')
    if existing is None:
        register()
    elif existing is not snapshot:
        raise ValueError('chaoxing_provider_already_registered')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--start', required=True, type=int, help='Capture time, inclusive Unix seconds')
    parser.add_argument('--end', required=True, type=int, help='Capture time, exclusive Unix seconds')
    parser.add_argument('--publish', action='store_true')
    parser.add_argument('--allow-downstream-processing', action='store_true')
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        configure_provider()
        if not args.publish:
            count = sum(1 for _ in scan_filtered(config, args.start, args.end))
            print(json.dumps({'mode': 'preview', 'matched': count, 'published': 0}))
            return
        if not args.allow_downstream_processing:
            raise ValueError('explicit_downstream_processing_consent_required')
        result = run_round(config, args.start, args.end)
        print(json.dumps({'mode': 'publish', 'matched': result['matched'], 'emitted': result['emitted'],
                          'duplicates': result['duplicates']}))
    except Exception as error:
        # Avoid printing SQL parameters, cookies, private file paths or message contents.
        known = {'platform_must_be_chaoxing', 'account_id_required', 'storage_paths_required',
                 'selection_storage_must_be_separate_from_capture', 'invalid_initial_lookback_seconds',
                 'chaoxing_attachments_not_supported', 'chaoxing_provider_already_registered',
                 'chaoxing_scope_mismatch', 'explicit_unique_sources_required',
                 'chaoxing_capture_account_mismatch', 'source_missing_or_not_authorized',
                 'source_type_not_supported_yet', 'explicit_downstream_processing_consent_required'}
        code = str(error) if isinstance(error, ValueError) and str(error) in known else 'chaoxing_selection_failed'
        parser.exit(1, code + '\n')


if __name__ == '__main__':
    main()
