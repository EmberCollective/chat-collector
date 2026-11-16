from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import threading

import pytest
import tomlkit

from messaging import management


@pytest.fixture
def settings(tmp_path):
    path = tmp_path / 'messaging.toml'
    path.write_text('''# preserve operator comments
enabled = true
unknown_top = "never-output-this"
[storage]
state_file = "state.db"
assets_dir = "assets"
[input]
batches_dir = "batches"
[feedback]
state_file = "feedback.db"
[[targets]]
id = "sample"
kind = "feishu_app"
enabled = true
app_id_env = "TEST_APP_ID"
app_secret_env = "TEST_APP_SECRET"
recipient_id = "oc_group"
allowed_operator_ids = ["ou_me"]
unknown_secret = "never-output-this"
[targets.extra]
secret = "never-output-this"
''', encoding='utf-8')
    return path


def test_view_exposes_allowlist_and_preserves_unknown_content_on_save(settings):
    current = management.read_settings(settings)
    assert 'never-output-this' not in json.dumps(current)
    assert current['targets'][0]['app_secret_env'] == 'TEST_APP_SECRET'
    updated = management.save_settings(settings, {
        'enabled': False, 'feedback_enabled': True,
        'schedule': {'interval_seconds': 45}, 'reminders': {'P4': 600},
        'feedback': {'max_attempts': 7},
        'deadline_reminders': {'P0': [86400, 3600, 0], 'P4': []},
        'initial_notifications': {'P4': False},
        'daily_digest': {'enabled': True, 'time': '08:30', 'timezone': 'Asia/Shanghai'},
        'targets': [{'id': 'sample', 'enabled': False},
                    {'id': 'another', 'kind': 'webhook', 'enabled': False, 'url_env': 'MY_OTHER_URL'}],
    }, current['version'])
    assert updated['version'] != current['version']
    assert updated['schedule']['interval_seconds'] == 45
    assert updated['feedback']['max_attempts'] == 7
    assert updated['reminders']['P4'] == 600
    assert updated['deadline_reminders']['P0'] == [86400, 3600, 0]
    assert updated['deadline_reminders']['P4'] == []
    assert updated['initial_notifications']['P4'] is False
    assert updated['daily_digest']['time'] == '08:30'
    assert [t['id'] for t in updated['targets']] == ['sample', 'another']
    stored = tomlkit.parse(settings.read_text(encoding='utf-8'))
    assert stored['targets'][0]['unknown_secret'] == 'never-output-this'
    assert stored['targets'][0]['extra']['secret'] == 'never-output-this'
    assert '# preserve operator comments' in settings.read_text(encoding='utf-8')
    assert 'never-output-this' not in json.dumps(updated)


@pytest.mark.parametrize('payload', [
    {'enabled': 'true'}, {'schedule': {'interval_seconds': 0}},
    {'deadline_reminders': {'P0': [False]}}, {'deadline_reminders': {'P1': [0, 0]}},
    {'deadline_reminders': {'P2': [-1]}}, {'deadline_reminders': {'timezone': 'not/a/zone'}},
    {'daily_digest': {'time': '25:00'}}, {'daily_digest': {'enabled': 'true'}},
    {'initial_notifications': {'P4': 1}},
    {'feedback': {'state_file': 'other.db'}}, {'reminders': {'P0': -1}},
    {'targets': [{'id': 'sample', 'kind': 'webhook'}]},
    {'targets': [{'id': 'sample', 'app_secret': 'secret'}]},
    {'targets': [{'id': 'sample', 'app_secret_env': 'sk-secret-value'}]},
    {'targets': [{'id': 'sample', 'recipient_id': 'https://secret'}]},
    {'targets': [{'id': 'sample', 'allowed_operator_ids': ['wrong']}]},
    {'targets': [{'id': 'sample', 'max_file_bytes': 0}]},
    {'targets': [{'id': 'sample', 'enabled': True, 'recipient_id': ''}]},
    {'targets': [{'id': 'new', 'kind': 'notion', 'enabled': True}]},
    {'targets': [{'id': 'sample'}, {'id': 'sample'}]},
    {'targets': ['bad']}, {'targets': [{'id': '../bad', 'kind': 'webhook'}]},
])
def test_invalid_patch_never_changes_original(settings, payload):
    before = settings.read_bytes()
    with pytest.raises(ValueError):
        management.save_settings(settings, payload, hashlib.sha256(before).hexdigest())
    assert settings.read_bytes() == before


def test_competing_saves_only_one_wins(settings):
    version = management.read_settings(settings)['version']
    barrier = threading.Barrier(2)
    def save(interval):
        barrier.wait()
        try:
            management.save_settings(settings, {'schedule': {'interval_seconds': interval}}, version)
            return 'saved'
        except FileExistsError:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(save, [30, 40]))
    assert sorted(result) == ['conflict', 'saved']


def test_misconfigured_inline_credential_in_env_field_is_not_returned(settings):
    settings.write_text(settings.read_text(encoding='utf-8').replace('TEST_APP_SECRET', 'sk-never-expose'), encoding='utf-8')
    result = management.read_settings(settings)
    assert result['targets'][0]['app_secret_env'] == ''
    assert 'sk-never-expose' not in json.dumps(result)


def test_failed_atomic_replace_keeps_original_and_cleans_temporary(settings, monkeypatch):
    before = settings.read_bytes()
    def fail(*args):
        raise OSError('disk unavailable')
    monkeypatch.setattr(management.os, 'replace', fail)
    with pytest.raises(OSError):
        management.save_settings(settings, {'enabled': False}, hashlib.sha256(before).hexdigest())
    assert settings.read_bytes() == before
    assert list(settings.parent.glob('*.toml')) == [settings]


def test_synthetic_probe_has_no_collected_data_and_disabled_never_sends(settings, monkeypatch):
    from messaging import channels
    sent = []
    def deliver(target, envelope, config):
        sent.append(envelope)
        asset = envelope['task']['resources'][0]
        from pathlib import Path
        assert hashlib.sha256(Path(asset['local_path']).read_bytes()).hexdigest() == asset['sha256']
        return {'delivery_id': envelope['delivery_id'], 'message_id': 'om_test'}
    monkeypatch.setattr(channels, 'deliver', deliver)
    result = management.send_test(settings, 'sample')
    assert result['status'] == 'sent' and result['synthetic']
    assert sent[0]['task']['sources'] == []
    assert '合成测试' in sent[0]['task']['title']
    assert not (settings.parent / 'state.db').exists()
    assert management.test_history(settings)[0]['test_id'] == result['test_id']
    current = management.read_settings(settings)
    management.save_settings(settings, {'enabled': False}, current['version'])
    with pytest.raises(ValueError, match='not_enabled'):
        management.send_test(settings, 'sample')
    assert len(sent) == 1


def test_probe_exception_is_sanitized(settings, monkeypatch):
    from messaging import channels
    def fail(*args):
        raise RuntimeError('https://secret-token.local/path')
    monkeypatch.setattr(channels, 'deliver', fail)
    result = management.send_test(settings, 'sample')
    assert result['status'] == 'unknown'
    assert 'secret-token' not in json.dumps(result)


def test_channel_descriptors_supply_fields_defaults_and_capabilities(settings):
    result = management.read_settings(settings)
    for kind, schema in result['schemas'].items():
        description = result['types'][kind]
        assert set(description['fields']) == set(schema)
        assert description['defaults']['enabled'] is False
        assert description['capabilities']['send'] and description['capabilities']['test']
        assert all(description['fields'][key]['type'] == value for key, value in schema.items())
    assert result['types']['feishu_app']['capabilities']['listener']
    assert not result['types']['webhook']['capabilities']['files']
    assert not result['types']['notion']['capabilities']['listener']
    assert result['types']['webhook']['fields']['mode']['choices']['notify'] == '消息通知'
    assert result['types']['notion']['fields']['properties']['fields']['date']['label'] == '日期'
    result['types']['feishu_app']['defaults']['upload_files'] = False
    assert management.read_settings(settings)['types']['feishu_app']['defaults']['upload_files'] is True


def test_unknown_targets_are_visible_but_secrets_and_actions_remain_unavailable(settings):
    with settings.open('a', encoding='utf-8') as stream:
        stream.write('\n[[targets]]\nid="future"\nkind="not-installed"\nenabled=true\nsecret="never-output-this"\n')
    current = management.read_settings(settings)
    unknown = current['targets'][-1]
    assert unknown['id'] == 'future' and unknown['kind'] == 'not-installed'
    assert unknown['kind'] not in current['types'] and unknown['kind'] not in current['schemas']
    assert 'never-output-this' not in json.dumps(current)
    with pytest.raises(ValueError, match='unsupported_target_kind'):
        management.send_test(settings, 'future')
    management.save_settings(settings, {'enabled': False}, current['version'])
    stored = tomlkit.parse(settings.read_text(encoding='utf-8'))
    assert stored['targets'][-1]['secret'] == 'never-output-this'
