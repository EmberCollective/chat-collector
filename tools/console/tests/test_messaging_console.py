import hashlib
import json

from test_console import console


def setup(console):
    client, app, root, _ = console
    (root/'delivery-input').mkdir()
    (root/'delivery-archive').mkdir()
    asset = root/'delivery-archive/report.pdf'
    asset.write_bytes(b'fixture-pdf')
    path = root/'messaging.toml'
    path.write_text('''enabled=false
[input]
batches_dir="delivery-input"
[storage]
state_file="delivery/state.db"
assets_dir="delivery/assets"
[attachments]
allowed_roots=["delivery-archive"]
''', encoding='utf8')
    app.state.bridge.settings['messaging_config'] = str(path)
    task = {'task_id': 't1', 'revision': 1, 'title': 'Test task', 'event': 'Submit report',
            'priority': 'P2', 'status': 'open', 'action_steps': ['Complete the form'], 'sources': [
                {'message_key': 'm1', 'attachments': [{'name': 'report.pdf', 'type': 'file', 'status': 'archived',
                 'archive_path': str(asset), 'sha256': hashlib.sha256(asset.read_bytes()).hexdigest()}]}]}
    task['materials'] = [{'message_key': 'm1', 'attachment_index': 0, 'attachment': task['sources'][0]['attachments'][0]}]
    event = {'schema_version': 1, 'event_id': 'e1', 'task_id': 't1', 'revision': 1, 'operation': 'upsert', 'task': task}
    (root/'delivery-input/tasks.jsonl').write_text(json.dumps(event)+'\n', encoding='utf8')
    return client, root


def test_local_preview_sync_complete_and_material_download(console):
    client, root = setup(console)
    response = client.get('/api/messaging/preview')
    assert response.status_code == 200, response.text
    assert not (root/'delivery/state.db').exists()
    assert response.json()['tasks'][0]['action_steps'] == ['Complete the form']
    assert client.post('/api/messaging/sync').json()['delivery_attempts'] == 0
    task = client.get('/api/messaging/preview').json()['tasks'][0]
    ref = task['resources'][0]['id']
    url = '/api/messaging/tasks/t1/materials/' + ref
    assert client.get(url).content == b'fixture-pdf'
    assert client.get(url, headers={'X-Console-Token': 'invalid'}).status_code == 401
    assert client.get('/api/messaging/tasks/t1/materials/unknown').status_code == 400
    assert client.post('/api/messaging/tasks/t1/state', json={'action': 'complete'}).status_code == 200
    assert client.get('/api/messaging/preview').json()['tasks'][0]['status'] == 'completed'
    assert client.get(url).content == b'fixture-pdf'
    assert client.post('/api/messaging/tasks/t1/state', json={'action': 'reopen'}).status_code == 200
    assert client.get('/api/messaging/preview').json()['tasks'][0]['status'] == 'open'
    assert client.get('/api/messaging/status').json()['enabled'] is False


def test_panel_is_local_only_and_cannot_send_or_choose_arbitrary_files(console):
    client, root = setup(console)
    page = client.get('/').text
    assert 'messaging-panel' in page and '参与人员' in page
    assert client.post('/api/messaging/send').status_code in {404, 405}
    assert client.get('/api/messaging/preview', headers={'Origin': 'https://evil.example'}).status_code == 403
    assert client.post('/api/messaging/tasks/t1/state', json={'action': 'send'}).status_code == 422
    assert client.get('/api/messaging/tasks/unknown/materials/anything').status_code == 400


def test_feedback_queue_and_automation_are_authenticated_and_read_only(console):
    client, root = setup(console)
    path = root/'automation.toml'
    console[1].state.bridge.settings['automation_config'] = str(path)
    assert client.get('/api/messaging/feedback').json()['enabled'] is False
    for url in ['/api/messaging/feedback', '/api/automation/status', '/api/automation/check']:
        assert client.get(url, headers={'X-Console-Token': 'invalid'}).status_code == 401
    assert client.get('/api/automation/status').status_code == 404
    path.write_text('enabled=false\n[storage]\nstate_file="automation/state.db"\n', encoding='utf8')
    assert client.get('/api/automation/status').json()['enabled'] is False
    assert client.get('/api/automation/check').status_code == 200
    assert not (root/'automation/state.db').exists()


def test_state_action_queues_feedback_without_claiming_completion(console):
    client, root = setup(console)
    from processor import load_config, storage
    settings = load_config(root/'processing/config.toml')
    with storage._connect(settings, write=True) as conn:
        storage._save_task(conn, {'task_id':'t1','title':'Test task','event':'Submit report',
                                 'priority':'P2','status':'open','sources':[]})
    with (root/'messaging.toml').open('a', encoding='utf8') as stream:
        stream.write('\n[feedback]\nenabled=true\nprocessor_config="processing/config.toml"\nstate_file="delivery/feedback.db"\n')
    client.post('/api/messaging/sync').raise_for_status()
    response = client.post('/api/messaging/tasks/t1/state', json={'action':'complete'})
    assert response.status_code == 200 and response.json()['status'] == 'queued'
    assert client.get('/api/messaging/feedback').json()['counts']['queued'] == 1
    assert client.get('/api/messaging/preview').json()['tasks'][0]['status'] == 'open'
