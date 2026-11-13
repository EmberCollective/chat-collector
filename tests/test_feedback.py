from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import time

import pytest

from collector.__main__ import InstanceLock
import processor
from processor import core as processing
from processor import storage as task_storage
from messaging import core, channels, feedback, load_config


ACTOR = {'platform': 'feishu', 'user_id': 'verified-user', 'source': 'card'}


@pytest.fixture
def config(tmp_path, monkeypatch):
    (tmp_path / 'chat').mkdir()
    (tmp_path / 'rules.md').write_text('Rules', encoding='utf-8')
    processor_path = tmp_path / 'processor.toml'
    processor_path.write_text('''[input]
batches_dir="chat"
[storage]
state_file="processor.db"
output_dir="todos"
[rules]
file="rules.md"
''', encoding='utf-8')
    pconfig = processor.load_config(processor_path)
    with processing._connect(pconfig, write=True) as conn:
        processing._save_task(conn, {'task_id': 'task-one', 'status': 'open', 'title': 'Prepare report',
            'event': 'Submit the report', 'priority': 'P0', 'priority_reason': 'Explicit requirement',
            'sources': [], 'time': {}, 'people': [], 'action_steps': [], 'review_reasons': [], 'needs_review': False})
        conn.commit()
        processing._export(pconfig, conn)
    path = tmp_path / 'messaging.toml'
    path.write_text('''enabled=true
[input]
batches_dir="todos"
[storage]
state_file="message.db"
assets_dir="assets"
[feedback]
enabled=true
state_file="feedback.db"
processor_config="processor.toml"
[reminders]
P0=60
[[targets]]
id="cards"
kind="feishu"
enabled=true
snooze_seconds=300
''', encoding='utf-8')
    config = load_config(path)
    monkeypatch.setattr(channels, 'deliver', lambda *args, **kwargs: {'remote_id': 'mock'})
    core.sync(config)
    return config


def enqueue(config, event='click-one', action='complete', **kwargs):
    return feedback.enqueue_feedback(config, 'cards', event, 'task-one', action, ACTOR, **kwargs)


def request_row(config, request_id):
    with sqlite3.connect(config['feedback']['state_file']) as conn:
        conn.row_factory = sqlite3.Row
        return dict(conn.execute('SELECT * FROM feedback_requests WHERE request_id=?', (request_id,)).fetchone())


def test_priority_feedback_publishes_and_keeps_stale_clicks_from_overwriting(config):
    first = enqueue(config, action='set_priority', priority='P4', expected_revision=1)
    assert enqueue(config, action='set_priority', priority='P4', expected_revision=1) == first
    assert feedback.process_pending(config)['applied'] == 1
    assert core.preview(config)['tasks'][0]['priority'] == 'P4'
    enqueue(config, event='old-card', action='set_priority', priority='P0', expected_revision=1)
    assert feedback.process_pending(config)['conflict'] == 1
    assert core.preview(config)['tasks'][0]['priority'] == 'P4'
    enqueue(config, event='new-card', action='set_priority', priority='P2', expected_revision=2)
    assert feedback.process_pending(config)['applied'] == 1
    assert feedback.blocked_task_ids(config) == set()
    assert core.preview(config)['tasks'][0]['priority'] == 'P2'


@pytest.mark.parametrize('kwargs', [dict(action='set_priority'), dict(action='set_priority', priority='P9'), dict(action='complete', priority='P0')])
def test_invalid_priority_feedback_rejected(config, kwargs):
    with pytest.raises(ValueError):
        enqueue(config, **kwargs)


def test_callback_enqueue_is_fast_and_independent_of_both_pipeline_locks(config, monkeypatch):
    def forbidden(*args):
        raise AssertionError('Callback must not import task batches')
    monkeypatch.setattr(core, '_inputs', forbidden)
    with InstanceLock(config['storage']['state_file']), InstanceLock(str(Path(config['feedback']['processor_config']).with_name('processor.db'))):
        start = time.perf_counter()
        first = enqueue(config)
        assert time.perf_counter() - start < 3
        assert enqueue(config) == first
    assert request_row(config, first['request_id'])['status'] == 'queued'
    with pytest.raises(ValueError, match='content_conflict'):
        enqueue(config, action='reopen')


def test_concurrent_duplicate_callbacks_and_feishu_tables_are_preserved(config):
    with sqlite3.connect(config['feedback']['state_file']) as conn:
        conn.execute('CREATE TABLE feishu_cards(token TEXT)')
        conn.execute("INSERT INTO feishu_cards VALUES('opaque')")
    with ThreadPoolExecutor(4) as workers:
        results = list(workers.map(lambda _: enqueue(config), range(4)))
    assert all(result == results[0] for result in results)
    with sqlite3.connect(config['feedback']['state_file']) as conn:
        assert conn.execute('SELECT count(*) FROM feedback_requests').fetchone()[0] == 1
        assert conn.execute('SELECT token FROM feishu_cards').fetchone()[0] == 'opaque'


def test_completed_feedback_updates_processor_published_task_and_local_state(config):
    request = enqueue(config, expected_revision=1)
    result = feedback.process_pending(config)
    assert result['applied'] == 1 and result['pending'] == 0
    pconfig = processor.load_config(config['feedback']['processor_config'])
    saved = processor.list_tasks(pconfig)[0]
    assert saved['status'] == 'completed' and saved['revision'] == 2
    assert core.preview(config)['tasks'][0]['status'] == 'completed'
    assert core.status(config)['local_state']['task-one']['completed'] == 0
    assert request_row(config, request['request_id'])['status'] == 'applied'
    assert enqueue(config, expected_revision=1) == request
    assert feedback.process_pending(config)['applied'] == 0


def test_busy_processor_does_not_consume_failure_attempts_and_preserves_fifo(config, monkeypatch):
    monkeypatch.setattr(feedback.time, 'time', lambda: 100)
    first = enqueue(config, expected_revision=1)
    second = enqueue(config, event='click-two', action='reopen', expected_revision=2)
    state = Path(config['feedback']['processor_config']).with_name('processor.db')
    with InstanceLock(state):
        assert feedback.process_pending(config)['retry'] == 1
    assert request_row(config, first['request_id'])['attempts'] == 0
    assert request_row(config, second['request_id'])['status'] == 'queued'
    monkeypatch.setattr(feedback.time, 'time', lambda: 160)
    assert feedback.process_pending(config)['applied'] == 1
    assert request_row(config, second['request_id'])['status'] == 'queued'
    assert feedback.process_pending(config)['applied'] == 1
    assert core.preview(config)['tasks'][0]['status'] == 'open'
    assert core.status(config)['local_state']['task-one']['completed'] == 0


def test_export_failure_and_interruption_after_processor_commit_recover_without_new_revision(config, monkeypatch):
    monkeypatch.setattr(feedback.time, 'time', lambda: 100)
    request = enqueue(config, expected_revision=1)
    original_sync = core.sync
    def interrupted(*args):
        raise OSError('simulated restart after processor commit')
    monkeypatch.setattr(core, 'sync', interrupted)
    assert feedback.process_pending(config)['retry'] == 1
    pconfig = processor.load_config(config['feedback']['processor_config'])
    assert processor.list_tasks(pconfig)[0]['revision'] == 2
    with sqlite3.connect(config['feedback']['state_file']) as conn:
        conn.execute("UPDATE feedback_requests SET status='applying' WHERE request_id=?", (request['request_id'],))
    monkeypatch.setattr(core, 'sync', original_sync)
    assert feedback.process_pending(config)['applied'] == 1
    assert processor.list_tasks(pconfig)[0]['revision'] == 2
    assert core.preview(config)['tasks'][0]['status'] == 'completed'


def test_pending_export_retries_same_api_request(config, monkeypatch):
    monkeypatch.setattr(feedback.time, 'time', lambda: 100)
    request = enqueue(config, expected_revision=1)
    with monkeypatch.context() as patch:
        patch.setattr(task_storage, '_export', lambda *args: (_ for _ in ()).throw(OSError('disk unavailable')))
        assert feedback.process_pending(config)['retry'] == 1
    assert request_row(config, request['request_id'])['error'] == 'task_status_export_pending'
    feedback.retry_feedback(config, request['request_id'])
    assert feedback.process_pending(config)['applied'] == 1
    pconfig = processor.load_config(config['feedback']['processor_config'])
    assert processor.list_tasks(pconfig)[0]['revision'] == 2


def test_conflict_is_visible_and_new_explicit_action_supersedes_it(config):
    first = enqueue(config, expected_revision=99)
    assert feedback.process_pending(config)['conflict'] == 1
    assert feedback.blocked_task_ids(config) == set()
    second = enqueue(config, event='corrected-click', expected_revision=1)
    result = feedback.process_pending(config)
    assert result['applied'] == 1 and result['conflict'] == 0
    assert request_row(config, first['request_id'])['status'] == 'superseded'
    assert request_row(config, second['request_id'])['status'] == 'applied'
    assert feedback.blocked_task_ids(config) == set()


def test_unknown_task_is_terminal_and_alarm_count_persists(config):
    feedback.enqueue_feedback(config, 'cards', 'unknown-click', 'unknown', 'complete', ACTOR)
    assert feedback.process_pending(config)['failed'] == 1
    assert feedback.process_pending(config)['failed'] == 1


@pytest.mark.parametrize('terminal', ['conflict', 'failed'])
def test_terminal_feedback_refreshes_all_targets_without_overwriting_latest_task(config, monkeypatch, terminal):
    from processor.service import update_task_priority, get_task
    import automation.wakeup
    config['targets'].append(dict(config['targets'][0], id='second-target'))
    sent, wakes = [], []
    monkeypatch.setattr(channels, 'deliver', lambda target, envelope, *a, **k:
                        sent.append((target['id'], envelope)) or {'remote_id': target['id']})
    monkeypatch.setattr(automation.wakeup, 'notify', lambda path, job: wakes.append(job))
    assert core.run_round(config, now=100)['sent'] == 2
    pconfig = processor.load_config(config['feedback']['processor_config'])
    update_task_priority(pconfig, 'task-one', 'P1', request_id='external', actor=ACTOR, expected_revision=1)
    request = enqueue(config, expected_revision=1)
    if terminal == 'failed':
        monkeypatch.setattr(feedback, '_apply', lambda *a: (_ for _ in ()).throw(ValueError('invalid')))
    assert feedback.process_pending(config)[terminal] == 1
    assert wakes[-1] == 'messaging'
    assert feedback.blocked_task_ids(config) == set()
    assert core.run_round(config, now=101)['sent'] == 2
    assert {target for target, envelope in sent[-2:]} == {'cards', 'second-target'}
    assert all(envelope['task']['revision'] == 2 and envelope['task']['priority'] == 'P1'
               for _, envelope in sent[-2:])
    assert core.run_round(config, now=102 + config['reminders']['P1'])['sent'] == 2
    feedback.retry_feedback(config, request['request_id'])
    assert feedback.process_pending(config)[terminal] == 1
    latest = get_task(pconfig, 'task-one')
    assert (latest['revision'], latest['priority'], latest['status']) == (2, 'P1', 'open')


def test_snooze_until_is_fixed_when_duplicate_arrives_later(config, monkeypatch):
    monkeypatch.setattr(feedback.time, 'time', lambda: 100)
    request = enqueue(config, action='snooze')
    assert request_row(config, request['request_id'])['snoozed_until'] == 400
    monkeypatch.setattr(feedback.time, 'time', lambda: 200)
    assert enqueue(config, action='snooze') == request
    assert feedback.process_pending(config)['applied'] == 1
    assert core.status(config)['local_state']['task-one']['snoozed_until'] == 400


def test_stale_snooze_is_rejected_before_changing_local_state(config):
    enqueue(config, action='snooze', expected_revision=99)
    assert feedback.process_pending(config)['conflict'] == 1
    assert core.status(config)['local_state'] == {}


def test_feedback_blocks_new_and_already_queued_reminders(config, monkeypatch):
    monkeypatch.setattr(feedback.time, 'time', lambda: 100)
    sent = []
    def rejected(*args, **kwargs):
        raise channels.DeliveryError('rate_limited')
    monkeypatch.setattr(channels, 'deliver', rejected)
    assert core.run_round(config, now=100)['retry'] == 1
    enqueue(config)
    monkeypatch.setattr(channels, 'deliver', lambda *args, **kwargs: (sent.append(args[1]) or {'remote_id': 'mock'}))
    assert core.run_round(config, now=1000)['sent'] == 0 and sent == []
    assert feedback.process_pending(config)['applied'] == 1
    assert core.run_round(config, now=1001)['sent'] == 0


def test_new_feedback_arriving_after_delivery_queue_was_built_still_blocks_send(config, monkeypatch):
    original = core._queue
    def queue_then_click(*args):
        original(*args)
        enqueue(config)
    monkeypatch.setattr(core, '_queue', queue_then_click)
    assert core.run_round(config, now=100)['sent'] == 0


def test_disabling_feedback_queue_keeps_service_as_formal_status_owner(config):
    config['feedback']['enabled'] = False
    result = feedback.request_task_action(config, 'task-one', 'complete')
    assert result['status'] == 'completed'
    from processor.service import get_task
    assert get_task(processor.load_config(config['feedback']['processor_config']), 'task-one')['status'] == 'completed'
    assert feedback.process_pending(config) == {'applied': 0, 'retry': 0, 'failed': 0, 'conflict': 0, 'pending': 0}


def test_feedback_retry_policy_is_independent_of_delivery_retries(config, monkeypatch):
    config['feedback'].update(retry_base_seconds=15, max_attempts=2)
    config['schedule'].update(retry_base_seconds=99, max_attempts=19)
    monkeypatch.setattr(feedback.time, 'time', lambda: 100)
    monkeypatch.setattr(feedback, '_apply', lambda *args: (_ for _ in ()).throw(OSError('synthetic')))
    request = enqueue(config)
    assert feedback.process_pending(config)['retry'] == 1
    assert request_row(config, request['request_id'])['next_attempt'] == 115
    monkeypatch.setattr(feedback.time, 'time', lambda: 115)
    assert feedback.process_pending(config)['failed'] == 1
    assert request_row(config, request['request_id'])['attempts'] == 2


def test_misconfigured_feedback_database_does_not_modify_processor_tables(config):
    path = Path(config['feedback']['processor_config']).with_name('processor.db')
    before = path.read_bytes()
    config['feedback']['state_file'] = str(path)
    with pytest.raises(ValueError, match='wrong_database'):
        enqueue(config)
    assert path.read_bytes() == before


def test_cli_queues_and_reports_without_sending(config, capsys):
    from messaging.__main__ import main
    path = config['_config_path']
    assert main(['complete', '--config', path, '--task-id', 'task-one', '--event-id', 'cli-one']) == 0
    queued = json.loads(capsys.readouterr().out)
    assert queued['status'] == 'queued'
    assert main(['feedback-status', '--config', path]) == 0
    assert json.loads(capsys.readouterr().out)['counts']['queued'] == 1
