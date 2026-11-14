"""Synthetic third source through SQL, extraction, delivery and formal feedback."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3

from messaging import load_config as load_messaging, run_round as deliver_round, feedback, channels
from processor import core as processing, service
from providers.query_registry import QUERY_PROVIDERS, register_query_provider
from selection import run_round as select_round
from test_processor import config as processor_config, response
from test_query import SyntheticSnapshot


def test_third_source_to_task_to_channel_feedback(processor_config, tmp_path, monkeypatch):
    database = tmp_path / 'source.db'
    with sqlite3.connect(database) as conn:
        conn.execute('CREATE TABLE entries(ordinal,epoch,room,payload,document)')
        conn.executemany('INSERT INTO entries VALUES(?,?,?,?,?)', [
            (1, 100, 'room', 'Submit report', 1), (2, 101, 'room', 'By tomorrow', 0),
            (3, 101, 'other-room', 'Unrelated conversation', 0)])
    @contextmanager
    def factory(scope):
        snapshot = SyntheticSnapshot(database)
        try:
            yield snapshot
        finally:
            snapshot.db.close()
    monkeypatch.setattr('providers.query_registry.QUERY_PROVIDERS', dict(QUERY_PROVIDERS))
    register_query_provider('synthetic', factory)
    root = Path(__file__).resolve().parents[1]
    config = dict(platform='synthetic', source={'account_id':'account','database':str(database)},
        storage={'state_file':str(tmp_path/'selection.db'),'output_dir':processor_config['input']['batches_dir']},
        schedule={'initial_lookback_seconds':100}, filters={'rulesets':[
            {'file':str(root/'rulesets/files.sql')}, {'file':str(root/'rulesets/context.sql'), 'recent_count':5}]})
    before = database.read_bytes()
    assert select_round(config, 100, 110)['emitted'] == 2
    assert select_round(config, 100, 110)['emitted'] == 0
    events = [json.loads(line) for path in Path(processor_config['input']['batches_dir']).glob('*.jsonl')
              for line in path.read_text(encoding='utf-8').splitlines()]
    trigger = next(event for event in events if event['message']['message_id'] == '1')
    assert len(trigger['matched_rules']) == 2
    calls = []
    def model(config, rules, group, existing):
        calls.append(group['id'])
        assert {e['message']['conversation_id'] for e in group['messages']} == {'room'}
        return response(group['primary_message_keys'])
    monkeypatch.setattr(processing, '_call_model', model)
    assert processing.run_round(processor_config)['tasks_upserted'] == 1
    original = processing.list_tasks(processor_config)[0]
    assert original['conversation']['platform'] == 'synthetic'

    path = tmp_path / 'messaging.toml'
    path.write_text('''enabled=true
[input]
processor_config="processor.toml"
[storage]
state_file="messaging.db"
assets_dir="delivery-assets"
[feedback]
enabled=true
state_file="feedback.db"
processor_config="processor.toml"
[[targets]]
id="mock"
kind="feishu"
enabled=true
''')
    delivery = load_messaging(path)
    sent = []
    monkeypatch.setattr(channels, 'deliver', lambda target, envelope, config, previous=None:
                        (sent.append(envelope) or {'remote_id':'synthetic-card'}))
    assert deliver_round(delivery, now=100)['sent'] == 1
    ack = feedback.enqueue_feedback(delivery, 'mock', 'click-1', original['task_id'], 'complete',
        {'platform':'feishu','user_id':'synthetic-user','source':'verified-card'}, expected_revision=1)
    assert ack['status'] == 'queued'
    assert feedback.process_pending(delivery)['applied'] == 1
    assert service.get_task(processor_config, original['task_id'])['status'] == 'completed'
    assert deliver_round(delivery, now=101)['sent'] == 1
    assert sent[-1]['task']['status'] == 'completed'
    assert deliver_round(delivery, now=10000)['sent'] == 0
    assert len(calls) == 1 and database.read_bytes() == before
