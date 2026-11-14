"""Load explicit SQL registrations and preserve their provenance."""
import hashlib
from pathlib import Path

from common.codec import digest
from providers.query import validate_rule


def validate_filters(settings):
    if not isinstance(settings, dict) or set(settings)-{'rulesets'}:
        raise ValueError('Only filters.rulesets is supported; migrate retired filter fields into SQL files (see FILTERS.md)')


def load_rulesets(config):
    settings = config.get('filters', {})
    validate_filters(settings)
    entries = settings.get('rulesets', [])
    if not isinstance(entries, list):
        raise ValueError('filters.rulesets must be an array')
    base = Path(config.get('_config_path', '.')).resolve().parent
    result = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry)-{'file', 'params', 'recent_count', 'enabled'} or not entry.get('file'):
            raise ValueError('Each ruleset needs file, optional enabled, params and recent_count')
        if not isinstance(entry['file'], str) or Path(entry['file']).suffix.lower() != '.sql':
            raise ValueError('ruleset.file must name a .sql file')
        if not isinstance(entry.get('enabled', True), bool):
            raise ValueError('ruleset.enabled must be true or false')
        count = entry.get('recent_count', 0)
        if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 1000:
            raise ValueError('ruleset.recent_count must be an integer from 0 to 1000')
        params = entry.get('params', {})
        if not isinstance(params, dict) or any(
            not isinstance(k, str) or k.startswith('_') or k in {'window_start', 'window_end', 'recent_count'}
            or (v is not None and not isinstance(v, (str, int, float))) for k, v in params.items()
        ):
            raise ValueError('Ruleset params must be named scalars; window_start/window_end/recent_count and _ prefixes are reserved')
        if not entry.get('enabled', True):
            continue
        path = (base / entry['file']).resolve()
        sql = path.read_text(encoding='utf-8-sig').strip()
        validate_rule(sql, params, count, path)
        provenance = dict(file=Path(entry['file']).as_posix(), params=params,
                          recent_count=count, sql_sha256=hashlib.sha256(sql.encode('utf-8')).hexdigest())
        result.append(dict(file=str(path), sql=sql, params=params, recent_count=count,
                           provenance=provenance, id=digest(provenance)))
    return result
