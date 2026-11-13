from pathlib import Path
import pytest

from test_console import console
from test_messaging_console import setup


def configured(console, monkeypatch):
    client, root = setup(console)
    path = root/'automation.toml'
    path.write_text('''enabled=false
[storage]
state_file="automatic/state.db"
[jobs.processor]
config="processing/config.toml"
[jobs.messaging]
config="messaging.toml"
[jobs.feedback]
config="messaging.toml"
''', encoding='utf8')
    console[1].state.bridge.settings['automation_config'] = str(path)
    from automation import control
    monkeypatch.setattr(control, 'status', lambda cfg: {'running':False,'state':'stopped','verified':True})
    return client, root, path


def test_startup_setting_is_authenticated_and_independent_of_job_settings(console, monkeypatch):
    client, root, path = configured(console, monkeypatch)
    from tools.install import startup
    calls = []
    monkeypatch.setattr(startup, 'startup_status', lambda folder: {'supported': True, 'enabled': False, 'installed': False})
    monkeypatch.setattr(startup, 'set_startup', lambda folder, enabled: calls.append((folder, enabled)) or {'supported': True, 'enabled': enabled})
    before = path.read_bytes()
    assert client.get('/api/operations/startup').json()['enabled'] is False
    assert not calls
    assert client.put('/api/operations/startup', json={'enabled': True}, headers={'X-Console-Token': 'wrong'}).status_code == 401
    assert client.put('/api/operations/startup', json={'enabled': 'true'}).status_code == 422
    assert client.put('/api/operations/startup', json={'enabled': True, 'path': 'evil'}).status_code == 422
    assert client.put('/api/operations/startup', json={'enabled': True}).json()['enabled'] is True
    assert client.put('/api/operations/startup', json={'enabled': False}).json()['enabled'] is False
    assert [enabled for _, enabled in calls] == [True, False]
    assert path.read_bytes() == before


def test_startup_permission_failure_is_not_reported_as_disabled(console, monkeypatch):
    client, _, _ = configured(console, monkeypatch)
    from tools.install import startup
    def failure(*args):
        raise RuntimeError('无法读取当前用户 Windows 启动项')
    monkeypatch.setattr(startup, 'startup_status', failure)
    assert client.get('/api/operations/startup').status_code == 503


def test_settings_are_safe_versioned_and_preserve_unexposed_fields(console, monkeypatch):
    client, root, path = configured(console, monkeypatch)
    with (root/'messaging.toml').open('a', encoding='utf8') as stream:
        stream.write('\n[private]\nsecret="do-not-display"\n')
    response = client.get('/api/operations/config')
    assert response.status_code == 200, response.text
    assert 'do-not-display' not in response.text and 'state_file' not in response.text
    body = response.json()
    body['automation']['jobs']['processor']['interval_seconds'] = 180
    body['messaging']['reminders']['P1'] = 7200
    saved = client.put('/api/operations/config', json=body)
    assert saved.status_code == 200, saved.text
    assert saved.json()['automation']['jobs']['processor']['interval_seconds'] == 180
    assert saved.json()['messaging']['reminders']['P1'] == 7200
    assert 'do-not-display' in (root/'messaging.toml').read_text(encoding='utf8')
    assert client.put('/api/operations/config', json=body).status_code == 409
    assert not (root/'automatic/state.db').exists()


def test_invalid_changes_do_not_partially_save(console, monkeypatch):
    client, root, path = configured(console, monkeypatch)
    before = path.read_bytes(), (root/'messaging.toml').read_bytes()
    body = client.get('/api/operations/config').json()
    body['automation']['jobs']['processor']['backoff_seconds'] = 2000
    body['automation']['jobs']['processor']['max_backoff_seconds'] = 100
    body['messaging']['enabled'] = True
    assert client.put('/api/operations/config', json=body).status_code == 400
    assert before == (path.read_bytes(), (root/'messaging.toml').read_bytes())
    body['automation']['jobs']['processor']['config'] = 'malicious.toml'
    assert client.put('/api/operations/config', json=body).status_code == 422


@pytest.mark.parametrize('failure', ['backup', 'replace'])
def test_disk_failure_preserves_both_configuration_files(console, monkeypatch, failure):
    client, root, path = configured(console, monkeypatch)
    before = path.read_bytes(), (root/'messaging.toml').read_bytes()
    body = client.get('/api/operations/config').json()
    body['messaging']['enabled'] = True
    if failure == 'backup':
        original = Path.write_bytes
        def write(file, data):
            if file.name.startswith('automation-'):
                raise OSError('fixture_full_disk')
            return original(file, data)
        monkeypatch.setattr(Path, 'write_bytes', write)
    else:
        import os
        original = os.replace
        def replace(source, destination):
            if Path(destination) == path:
                raise OSError('fixture_replace_failure')
            return original(source, destination)
        monkeypatch.setattr(os, 'replace', replace)
    assert client.put('/api/operations/config', json=body).status_code == 503
    assert before == (path.read_bytes(), (root/'messaging.toml').read_bytes())


def test_controls_are_authenticated_explicit_and_delegate_to_core(console, monkeypatch):
    client, root, path = configured(console, monkeypatch)
    from automation import control
    from messaging import management
    calls = []
    monkeypatch.setattr(control, 'start', lambda cfg: calls.append('start') or {'state':'starting'})
    monkeypatch.setattr(control, 'trigger', lambda cfg, job: calls.append(job) or {'state':'queued'})
    monkeypatch.setattr(management, 'send_test', lambda path, target: calls.append(target) or {'synthetic':True})
    assert client.get('/api/operations/status').status_code == 200
    assert not calls
    for url in ['/api/operations/service/start','/api/operations/jobs/processor/once','/api/operations/targets/test/test']:
        assert client.post(url, headers={'X-Console-Token':'bad'}).status_code == 401
    assert not calls
    assert client.post('/api/operations/service/start').json()['state'] == 'starting'
    assert client.post('/api/operations/jobs/processor/once').json()['state'] == 'queued'
    assert client.post('/api/operations/targets/test/test').json()['synthetic']
    assert calls == ['start','processor','test']
    assert client.post('/api/operations/jobs/wechat/once').status_code == 422
    assert client.post('/api/operations/service/arbitrary').status_code == 422


def test_save_restarts_only_an_already_running_service(console, monkeypatch):
    client, root, path = configured(console, monkeypatch)
    from automation import control
    calls = []
    monkeypatch.setattr(control, 'status', lambda cfg: {'running':True})
    monkeypatch.setattr(control, 'restart', lambda cfg: calls.append('restart') or {'state':'restarting'})
    monkeypatch.setattr(control, 'stop', lambda cfg: calls.append('stop') or {'state':'stopping'})
    body = client.get('/api/operations/config').json()
    body['automation']['enabled'] = True
    result = client.put('/api/operations/config', json=body)
    assert result.status_code == 200, result.text
    assert result.json()['operation']['state'] == 'restarting'
    body = client.get('/api/operations/config').json()
    body['automation']['enabled'] = False
    assert client.put('/api/operations/config', json=body).status_code == 200
    assert calls == ['restart','stop']


def test_processor_panel_recognizes_automatic_work_without_fake_console_job(console, monkeypatch):
    client, root, path = configured(console, monkeypatch)
    from automation import control
    monkeypatch.setattr(control, 'status', lambda cfg: {'running':True, 'jobs':[{'name':'processor','state':'running'}]})
    result = client.get('/api/processor/status').json()
    assert result['automation_running'] is True
    assert result['running_job'] is None
