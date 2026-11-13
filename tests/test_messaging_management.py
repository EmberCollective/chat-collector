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
        'targets': [{'id': 'sample', 'enabled': False},
                    {'id': 'another', 'kind': 'webhook', 'enabled': False, 'url_env': 'MY_OTHER_URL'}],
    }, current['version'])
    assert updated['version'] != current['version']
    assert updated['schedule']['interval_seconds'] == 45
    assert updated['feedback']['max_attempts'] == 7
    assert updated['reminders']['P4'] == 600
    assert [t['id'] for t in updated['targets']] == ['sample', 'another']
    stored = tomlkit.parse(settings.read_text(encoding='utf-8'))
    assert stored['targets'][0]['unknown_secret'] == 'never-output-this'
    assert stored['targets'][0]['extra']['secret'] == 'never-output-this'
    assert '# preserve operator comments' in settings.read_text(encoding='utf-8')
    assert 'never-output-this' not in json.dumps(updated)


@pytest.mark.parametrize('payload', [
    {'enabled': 'true'}, {'schedule': {'interval_seconds': 0}},
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
