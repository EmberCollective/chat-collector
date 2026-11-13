"""Local channel settings and explicit synthetic probes, independent of the console."""
from contextlib import closing
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
import uuid

from .config import load_config


COMMON = {'id': 'string', 'kind': 'string', 'enabled': 'boolean', 'timeout_seconds': 'integer'}
FIELDS = {
    'feishu_app': {'app_id_env': 'env', 'app_secret_env': 'env', 'recipient_id': 'string',
                   'receive_id_type': 'string', 'allowed_operator_ids': 'strings',
                   'snooze_seconds': 'integer', 'upload_files': 'boolean', 'max_file_bytes': 'integer'},
    'feishu': {'url_env': 'env', 'secret_env': 'env'},
    'notion': {'token_env': 'env', 'data_source_id': 'string', 'properties': 'properties'},
    'webhook': {'url_env': 'env', 'token_env': 'env', 'mode': 'string', 'purpose': 'string'},
}
PROPERTIES = {'title', 'task_id', 'priority', 'status', 'date', 'instructions', 'files'}
SECTIONS = {'schedule': {'interval_seconds', 'retry_base_seconds', 'max_attempts', 'max_deliveries_per_round'},
            'reminders': {'P0', 'P1', 'P2', 'P3', 'P4'},
            'feedback': {'max_attempts', 'retry_base_seconds'}}


def _view(raw, config):
    targets = []
    for target in config['targets']:
        fields = {**COMMON, **FIELDS.get(target['kind'], {})}
        safe = {k: deepcopy(v) for k, v in target.items() if k in fields}
        for key, field_type in fields.items():
            if field_type == 'env' and key in safe and (not isinstance(safe[key], str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,127}', safe[key])):
                safe[key] = ''
        if 'properties' in safe:
            safe['properties'] = {k: v for k, v in safe['properties'].items() if k in PROPERTIES}
        targets.append(safe)
    return {'version': hashlib.sha256(raw).hexdigest(), 'enabled': config['enabled'],
            'feedback_enabled': config['feedback']['enabled'], 'targets': targets,
            'schemas': {kind: {**COMMON, **fields} for kind, fields in FIELDS.items()},
            **{s: {k: config[s][k] for k in keys} for s, keys in SECTIONS.items()}}


def read_settings(path):
    path = Path(path).resolve()
    # A writer replaces atomically, so validate exactly the bytes used for the version.
    from collector.__main__ import InstanceLock
    with InstanceLock(path, timeout=5):
        return _view(path.read_bytes(), load_config(path))


def _validate_target(target, original=None):
    if not isinstance(target, dict) or not isinstance(target.get('id'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', target['id']):
        raise ValueError('invalid_target_id')
    kind = target.get('kind', (original or {}).get('kind'))
    if kind not in FIELDS:
        raise ValueError('unsupported_target_kind')
    if original and kind != original['kind']:
        raise ValueError('target_kind_is_immutable_create_a_new_target')
    fields = {**COMMON, **FIELDS[kind]}
    if set(target) - set(fields):
        raise ValueError('unsupported_target_field')
    for key, value in target.items():
        field_type = fields[key]
        valid = (type(value) is bool if field_type == 'boolean' else
                 type(value) is int if field_type == 'integer' else
                 isinstance(value, list) and all(isinstance(v, str) and len(v) <= 200 for v in value) if field_type == 'strings' else
                 isinstance(value, dict) and not set(value) - PROPERTIES and all(isinstance(v, str) and 0 < len(v) <= 200 for v in value.values()) if field_type == 'properties' else
                 isinstance(value, str) and len(value) <= 500 and not any(ord(c) < 32 for c in value))
        if not valid:
            raise ValueError('invalid_target_field_' + key)
        if field_type == 'env' and not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,127}', value):
            raise ValueError('environment_variable_name_required')
    merged = {**(original or {}), **target, 'kind': kind}
    if kind == 'feishu_app':
        if merged.get('receive_id_type', 'chat_id') != 'chat_id':
            raise ValueError('receive_id_type_must_be_chat_id')
        if merged.get('recipient_id') and not re.fullmatch(r'oc_[A-Za-z0-9]+', merged['recipient_id']):
            raise ValueError('invalid_feishu_chat_id')
        if any(not re.fullmatch(r'ou_[A-Za-z0-9]+', x) for x in merged.get('allowed_operator_ids', [])):
            raise ValueError('invalid_feishu_operator_id')
        if not 1 <= merged.get('max_file_bytes', 30_000_000) <= 30_000_000:
            raise ValueError('invalid_max_file_bytes')
        if merged.get('enabled') and (not merged.get('recipient_id') or not merged.get('allowed_operator_ids')
                                    or not merged.get('app_id_env') or not merged.get('app_secret_env')):
            raise ValueError('feishu_app_target_incomplete')
    if kind == 'webhook' and merged.get('mode', 'notify') not in {'sync', 'notify'}:
        raise ValueError('invalid_webhook_mode')
    if merged.get('enabled'):
        required = {'feishu': ['url_env'], 'webhook': ['url_env'], 'notion': ['token_env', 'data_source_id']}.get(kind, [])
        if any(not merged.get(k) for k in required):
            raise ValueError('target_incomplete')


def save_settings(path, payload, expected_version):
    """Merge submitted targets by ID; omitted targets and unknown settings survive."""
    import tomlkit
    from collector.__main__ import InstanceLock
    path = Path(path).resolve()
    if not isinstance(payload, dict) or set(payload) - {'enabled', 'feedback_enabled', 'targets', *SECTIONS}:
        raise ValueError('unsupported_settings_field')
    with InstanceLock(path, timeout=5):
        before = path.read_bytes()
        if hashlib.sha256(before).hexdigest() != expected_version:
            raise FileExistsError('settings_changed_reload_first')
        document = tomlkit.parse(before.decode('utf-8'))
        for key in ('enabled', 'feedback_enabled'):
            if key in payload:
                if type(payload[key]) is not bool:
                    raise ValueError('boolean_required')
                if key == 'enabled':
                    document[key] = payload[key]
                else:
                    document.setdefault('feedback', tomlkit.table())['enabled'] = payload[key]
        for section, fields in SECTIONS.items():
            if section in payload:
                values = payload[section]
                if not isinstance(values, dict) or set(values) - fields:
                    raise ValueError('unsupported_' + section + '_field')
                for key, value in values.items():
                    document.setdefault(section, tomlkit.table())[key] = value
        if 'targets' in payload:
            if not isinstance(payload['targets'], list):
                raise ValueError('targets_must_be_array')
            existing = {t['id']: t for t in document.get('targets', [])}
            seen = set()
            for target in payload['targets']:
                _validate_target(target, existing.get(target.get('id')) if isinstance(target, dict) else None)
                if target['id'] in seen:
                    raise ValueError('duplicate_target_id')
                seen.add(target['id'])
                entry = existing.get(target['id'])
                if entry is None:
                    entry = tomlkit.table()
                    document.setdefault('targets', tomlkit.aot()).append(entry)
                for key, value in target.items():
                    if key == 'properties':
                        entry.setdefault(key, tomlkit.table()).update(value)
                    else:
                        entry[key] = value
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='', suffix='.toml', dir=path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(tomlkit.dumps(document))
                stream.flush()
                os.fsync(stream.fileno())
            proposed = load_config(temporary)
            if path.read_bytes() != before:
                raise FileExistsError('settings_changed_reload_first')
            raw = temporary.read_bytes()
            os.replace(temporary, path)
            return _view(raw, proposed)
        finally:
            if temporary and temporary.exists():
                temporary.unlink()


def send_test(path, target_id):
    """One explicit external synthetic delivery; never writes a processor task."""
    from . import channels
    config = load_config(path)
    target = next((t for t in config['targets'] if t['id'] == target_id), None)
    if not target or not config['enabled'] or not target['enabled']:
        raise ValueError('test_target_not_enabled')
    identifier = 'synthetic-' + uuid.uuid4().hex
    folder = Path(config['storage']['state_file']).parent / 'tests'
    folder.mkdir(parents=True, exist_ok=True)
    content = 'Chat Collector 合成测试附件。没有真实聊天、个人信息或正式待办。\n'.encode('utf-8')
    digest = hashlib.sha256(content).hexdigest()
    asset = Path(config['storage']['assets_dir']) / 'synthetic' / 'connection-test.txt'
    asset.parent.mkdir(parents=True, exist_ok=True)
    if not asset.exists() or asset.read_bytes() != content:
        asset.write_bytes(content)
    task = {'task_id': identifier, 'revision': 1, 'title': '【合成测试】消息通道与附件检查',
            'event': '检查此测试卡片及小文本附件是否可见。按钮只记录测试结果，不修改正式待办。',
            'priority': 'P4', 'status': 'open', 'time': {}, 'people': [], 'sources': [],
            'action_steps': ['查看合成测试附件', '点击任一按钮检查回调；不会创建或修改正式待办'],
            'notes': ['这是人工触发的合成测试，不含真实采集数据。'],
            'resources': [{'id': 'synthetic-file', 'name': asset.name, 'type': 'file', 'status': 'ready',
                           'local_path': str(asset), 'sha256': digest, 'size_bytes': len(content)}]}
    envelope = {'delivery_id': identifier, 'kind': 'upsert', 'task': task}
    if target['kind'] == 'feishu_app':
        from .feishu import _state
        with closing(_state(config)) as conn, conn:
            conn.execute('INSERT INTO feishu_tests(delivery_id,created_at) VALUES(?,?)', (identifier, time.time()))
    result = {'test_id': identifier, 'target_id': target_id, 'synthetic': True, 'created_at': time.time(), 'status': 'sending'}
    output = folder / (identifier + '.json')
    output.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    try:
        receipt = channels.deliver(target, envelope, config)
        result.update(status='sent', receipt=receipt)
    except channels.DeliveryError as exc:
        result.update(status='unknown' if exc.uncertain else 'failed', error=exc.code)
        if exc.receipt:
            result['receipt'] = exc.receipt
    except Exception:
        result.update(status='unknown', error='synthetic_test_outcome_unknown')
    output.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    return result


def test_history(path, limit=20):
    """Local synthetic results with callback acknowledgement, no task database read."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('invalid_test_history_limit')
    from .feedback import _connect
    config = load_config(path)
    folder = Path(config['storage']['state_file']).parent / 'tests'
    entries = []
    for file in sorted(folder.glob('synthetic-*.json'), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        value = json.loads(file.read_text(encoding='utf-8'))
        entries.append({k: value[k] for k in ('test_id', 'target_id', 'synthetic', 'created_at', 'status', 'error', 'receipt') if k in value})
    conn = _connect(config)
    if conn:
        with closing(conn):
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='feishu_tests'").fetchone():
                for entry in entries:
                    row = conn.execute('SELECT clicked_at,action FROM feishu_tests WHERE delivery_id=?', (entry['test_id'],)).fetchone()
                    if row and row['clicked_at'] is not None:
                        entry['callback'] = {'status': 'test_received', 'clicked_at': row['clicked_at'], 'action': row['action']}
    return entries
