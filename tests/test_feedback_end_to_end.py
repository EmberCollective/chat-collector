"""Real local feedback pipeline with only model and Feishu network calls replaced."""
import json
import hashlib
from pathlib import Path
import sqlite3
import datetime as dt

import pytest

from messaging import core as messaging, feedback, feishu, load_config
from processor import core as processing
from test_feishu import callback, fake_client
from test_processor import config as processor_config, batch, event, response
from test_feedback import config as feedback_config
from test_pi_bridge import endpoint as pi_endpoint, send_tool, task as pi_task


@pytest.mark.parametrize('legacy_name', ['none', 'recorded', 'unrecorded'])
def test_native_pi_material_names_reach_feishu_without_replaying_legacy_files(processor_config, pi_endpoint, tmp_path, monkeypatch, legacy_name):
    config, _, responder, requests = pi_endpoint
    processor_config['model'].update(config['model'], api_key_env='')
    archive = tmp_path/'archive'
    archive.mkdir()
    path = archive/'shared.txt'
    path.write_text('Submit the report tomorrow.', encoding='utf-8')
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    processor_config['attachments']['archive_roots'] = [str(archive)]
    with Path(processor_config['_config_path']).open('a', encoding='utf-8') as stream:
        stream.write('\n[attachments]\narchive_roots=["archive"]\n')
    records = [event('a'), event('b', timestamp=101), event('c', timestamp=102)]
    for record, name in zip(records, ['report.txt', 'renamed.txt', 'report.txt']):
        record['message']['attachments'] = [{'type': 'file', 'name': name, 'archive_path': str(path), 'sha256': sha}]
    batch(processor_config, 'materials', *records)
    responder.append(lambda handler, n: send_tool(handler, 'read_attachment', {'ref': f'f{n}'}) if n < 3 else
                     send_tool(handler, 'submit_tasks', {'tasks': [dict(pi_task(['m1', 'm2', 'm3', 'f1:b1', 'f2:b1']),
                                                                       material_refs=['f1', 'f2'])]}))
    assert processing.run_round(processor_config)['tasks_upserted'] == 1
    task = processing.list_tasks(processor_config)[0]
    assert len(requests) == 3 and len(task['materials']) == 3
    monkeypatch.setenv('TEST_APP_ID', 'cli_test')
    monkeypatch.setenv('TEST_APP_SECRET', 'synthetic')
    message_path = tmp_path/'messaging.toml'
    message_path.write_text('''enabled=true
[input]
processor_config="processor.toml"
[storage]
state_file="messaging.db"
assets_dir="messaging-assets"
[attachments]
allowed_roots=["archive"]
[reminders]
P2=60
[deadline_reminders]
enabled=false
[[targets]]
id="assistant"
kind="feishu_app"
enabled=true
recipient_id="oc_group"
app_id_env="TEST_APP_ID"
app_secret_env="TEST_APP_SECRET"
allowed_operator_ids=["ou_me"]
''', encoding='utf-8')
    delivery = load_config(message_path)
    target = delivery['targets'][0]
    sha = task['materials'][0]['attachment']['sha256']
    if legacy_name != 'none':
        with feishu._state(delivery) as conn:
            conn.execute('INSERT INTO feishu_files VALUES(?,?,?,?,?,?,?,?)',
                         (feishu._scope(target, delivery), task['task_id'], sha, 'legacy-key', 'om_legacy',
                          'sent', None, 'report.txt' if legacy_name == 'recorded' else None))
    calls = fake_client(monkeypatch)
    assert messaging.run_round(delivery, now=100)['sent'] == 1
    package = messaging.task_package(delivery, task['task_id'])
    resources = package['resources']
    assert len(resources) == 2 and {item['name'] for item in resources} == {'report.txt', 'renamed.txt'}
    assert len({item['id'] for item in resources}) == 2 and {item['sha256'] for item in resources} == {sha}
    assert len({item['local_path'] for item in resources}) == 1
    assert next(item for item in resources if item['name'] == 'report.txt')['source_refs'] == ['a', 'c']
    expected = ['report.txt', 'renamed.txt'] if legacy_name == 'none' else ['renamed.txt']
    assert [item[1].file_name for item in calls if item[0] == 'upload'] == expected
    assert len([item for item in calls if item[0] == 'create' and item[1].msg_type == 'file']) == len(expected)
    assert messaging.run_round(delivery, now=161)['sent'] == 1
    assert [item[1].file_name for item in calls if item[0] == 'upload'] == expected
    with feishu._state(delivery) as conn:
        assert {row['asset_id'] for row in conn.execute('SELECT asset_id FROM feishu_files')} >= {item['id'] for item in resources}
        if legacy_name != 'none':
            assert conn.execute('SELECT message_id FROM feishu_files WHERE asset_id=?', (sha,)).fetchone()[0] == 'om_legacy'


def test_priority_callback_updates_existing_card_and_future_deadline_schedule(feedback_config, monkeypatch):
    config = feedback_config
    monkeypatch.setenv('TEST_APP_ID', 'cli_test')
    monkeypatch.setenv('TEST_APP_SECRET', 'synthetic')
    target = config['targets'][0]
    target.update(kind='feishu_app', recipient_id='oc_group', app_id_env='TEST_APP_ID',
                  app_secret_env='TEST_APP_SECRET', allowed_operator_ids=['ou_me'])
    config['deadline_reminders'].update(enabled=True)
    pconfig = processing.load_config(config['feedback']['processor_config'])
    now = 1800000000
    with processing._connect(pconfig, write=True) as conn:
        task = json.loads(conn.execute('SELECT body FROM tasks').fetchone()[0])
        task['priority'] = 'P1'
        task['time'] = {'deadline': dt.datetime.fromtimestamp(now + 8 * 3600, dt.timezone.utc).isoformat()}
        processing._save_task(conn, task)
        conn.commit()
        processing._export(pconfig, conn)
    from messaging import channels
    monkeypatch.setattr(channels, 'deliver', lambda target, envelope, config, previous=None:
        feishu.deliver_app(target, envelope, config, previous))
    calls = fake_client(monkeypatch)
    assert messaging.run_round(config, now=now)['sent'] == 1
    card = json.loads(calls[0][1].content)
    click = callback({'task': task}, card, 'set_priority', 'P0')
    ack = feishu.handle_callback(config, target, click)
    assert feishu.handle_callback(config, target, click) == ack
    assert feedback.process_pending(config)['applied'] == 1
    assert processing.list_tasks(pconfig)[0]['priority'] == 'P0'
    assert messaging.run_round(config, now=now + 1)['sent'] == 1
    assert calls[-1][0] == 'patch'
    updated = json.loads(calls[-1][1].request_body.content)
    assert updated['header']['title']['content'].startswith('[P0]')
    assert updated['header']['template'] != card['header']['template']
    assert messaging.run_round(config, now=now + 2)['sent'] == 0
    assert messaging.run_round(config, now=now + 2 * 3600)['sent'] == 1
    assert calls[-1][0] == 'create'
    assert messaging.run_round(config, now=now + 2 * 3600 + 1)['sent'] == 0


def test_feishu_completion_round_trip_and_later_model_update_stay_completed(processor_config, tmp_path, monkeypatch):
    batch(processor_config, 'initial', event('notice'))
    monkeypatch.setattr(processing, '_call_model',
        lambda cfg, rules, group, existing: response(group['primary_message_keys']))
    processing.run_round(processor_config)
    original = processing.list_tasks(processor_config)[0]

    monkeypatch.setenv('TEST_APP_ID', 'cli_test')
    monkeypatch.setenv('TEST_APP_SECRET', 'synthetic-do-not-send')
    path = tmp_path / 'messaging.toml'
    path.write_text('''enabled=true
[input]
batches_dir="todos"
[storage]
state_file="messaging.db"
assets_dir="messaging-assets"
[feedback]
enabled=true
state_file="feedback.db"
processor_config="processor.toml"
retry_base_seconds=15
max_attempts=2
[[targets]]
id="assistant"
kind="feishu_app"
enabled=true
recipient_id="oc_group"
receive_id_type="chat_id"
app_id_env="TEST_APP_ID"
app_secret_env="TEST_APP_SECRET"
allowed_operator_ids=["ou_me"]
''', encoding='utf-8')
    config = load_config(path)
    assert config['feedback']['processor_config'] == processor_config['_config_path']
    assert config['feedback']['state_file'] != config['storage']['state_file']
    assert config['feedback']['retry_base_seconds'] == 15
    assert config['feedback']['max_attempts'] == 2
    assert config['schedule']['retry_base_seconds'] == 60
    assert config['schedule']['max_attempts'] == 5

    calls = fake_client(monkeypatch)
    assert messaging.run_round(config, now=100)['sent'] == 1
    card = json.loads(calls[0][1].content)
    click = callback({'task': original}, card)
    queued = feishu.handle_callback(config, config['targets'][0], click)
    assert queued['status'] == 'queued' and queued['task_id'] == original['task_id']
    assert feishu.handle_callback(config, config['targets'][0], click) == queued
    assert feedback.process_pending(config)['applied'] == 1

    completed = processing.list_tasks(processor_config)[0]
    assert completed['status'] == 'completed' and completed['revision'] == original['revision'] + 1
    assert completed['status_control']['request_id'] == queued['request_id']
    assert completed['status_control']['actor'] == {'platform': 'feishu', 'user_id': 'ou_me', 'source': 'card'}
    assert messaging.preview(config)['tasks'][0]['status'] == 'completed'
    assert messaging.status(config)['local_state'][original['task_id']]['completed'] == 0
    with sqlite3.connect(config['feedback']['state_file']) as conn:
        assert conn.execute('SELECT count(*) FROM feedback_requests').fetchone()[0] == 1
        assert conn.execute('SELECT status FROM feedback_requests').fetchone()[0] == 'applied'
    published = [json.loads(line) for file in Path(processor_config['storage']['output_dir']).glob('*.jsonl')
                 for line in file.read_text(encoding='utf-8').splitlines()]
    assert any(row['task']['status'] == 'completed' and row['revision'] == completed['revision'] for row in published)

    batch(processor_config, 'followup', event('followup', timestamp=1000))
    def model_update(cfg, rules, group, existing):
        assert existing[0]['task_id'] == original['task_id'] and existing[0]['status'] == 'completed'
        return response(group['primary_message_keys'], identifier=original['task_id'], title='Report details updated')
    monkeypatch.setattr(processing, '_call_model', model_update)
    processing.run_round(processor_config)
    updated = processing.list_tasks(processor_config)[0]
    assert updated['title'] == 'Report details updated' and updated['status'] == 'completed'
    assert updated['revision'] == completed['revision'] + 1
    assert messaging.run_round(config, now=200)['sent'] == 1
    assert calls[-1][0] == 'patch'
    count = len(calls)
    assert messaging.run_round(config, now=10_000_000)['sent'] == 0
    assert len(calls) == count
    assert messaging.preview(config)['tasks'][0]['status'] == 'completed'
