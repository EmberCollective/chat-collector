"""Real local feedback pipeline with only model and Feishu network calls replaced."""
import json
from pathlib import Path
import sqlite3

from messaging import core as messaging, feedback, feishu, load_config
from processor import core as processing
from test_feishu import callback, fake_client
from test_processor import config as processor_config, batch, event, response


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
    assert messaging.status(config)['local_state'][original['task_id']]['completed'] == 1
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
