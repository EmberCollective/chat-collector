import json
from pathlib import Path
import sqlite3

import pytest

from collector.__main__ import InstanceLock
from processor import storage
from processor import (core, update_task_status, search_tasks, TaskStatusConflict, UnknownTask,
                       TaskStatusBusy, TaskStatusExportPending, update_task_priority)
from test_processor import batch, config, event, response


ACTOR = {'platform': 'notion', 'user_id': 'verified-person', 'source': 'task-page'}


def test_manual_priority_survives_model_update_and_keeps_completion(config, saved, monkeypatch):
    first = update_task_priority(config, saved['task_id'], 'P4', request_id='priority:1', actor=ACTOR, expected_revision=1)
    assert first['priority'] == 'P4' and first['revision'] == 2
    assert update_task_priority(config, saved['task_id'], 'P4', request_id='priority:1', actor=ACTOR, expected_revision=1) == first
    with pytest.raises(TaskStatusConflict):
        update_task_priority(config, saved['task_id'], 'P0', request_id='priority:1', actor=ACTOR)
    with pytest.raises(TaskStatusConflict):
        update_task_priority(config, saved['task_id'], 'P0', request_id='priority:2', actor=ACTOR, expected_revision=1)
    update_task_status(config, saved['task_id'], 'completed', request_id='complete:1', actor=ACTOR)
    batch(config, 'later', event('b', timestamp=1000))
    monkeypatch.setattr(core, '_call_model', lambda cfg, rules, group, existing:
        response(group['primary_message_keys'], identifier=saved['task_id'], title='Updated task details'))
    core.run_round(config)
    task = core.list_tasks(config)[0]
    assert task['status'] == 'completed' and task['priority'] == 'P4'
    assert task['priority_control']['request_id'] == first['request_id']
    assert task['priority_control']['actor'] == ACTOR
    assert task['priority_reason'] == '用户手动设置为 P4'


def test_priority_export_failure_replays_without_double_revision(config, saved, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(storage, '_export', lambda *args: (_ for _ in ()).throw(OSError('disk')))
        with pytest.raises(TaskStatusExportPending):
            update_task_priority(config, saved['task_id'], 'P0', request_id='priority:1', actor=ACTOR)
    assert core.list_tasks(config)[0]['priority'] == 'P0'
    result = update_task_priority(config, saved['task_id'], 'P0', request_id='priority:1', actor=ACTOR)
    assert result['revision'] == 2


@pytest.mark.parametrize('priority', ['P5', 'p0', None, {}, True])
def test_invalid_priority_rejected_before_storage(config, priority):
    with pytest.raises(ValueError, match='invalid_task_priority'):
        update_task_priority(config, 't', priority, request_id='priority:1', actor=ACTOR)
    assert not Path(config['storage']['state_file']).exists()


@pytest.fixture
def saved(config, monkeypatch):
    batch(config, 'first', event('a'))
    monkeypatch.setattr(core, '_call_model', lambda cfg, rules, group, existing: response(group['primary_message_keys']))
    core.run_round(config)
    return core.list_tasks(config)[0]


def test_completion_is_audited_published_and_does_not_change_sources(config, saved):
    result = update_task_status(config, saved['task_id'], 'completed', request_id='n:1', actor=ACTOR, expected_revision=1)
    assert result['status'] == 'completed' and result['revision'] == 2
    assert result['previous_status'] == 'open' and result['changed']
    current = core.list_tasks(config)[0]
    for field in ('sources', 'attachment_evidence', 'materials', 'source_message_keys', 'people', 'action_steps'):
        assert current[field] == saved[field]
    with sqlite3.connect(config['storage']['state_file']) as conn:
        request, stored = conn.execute('SELECT request,result FROM task_status_requests').fetchone()
        assert json.loads(request)['actor'] == ACTOR and json.loads(stored) == result
        assert conn.execute('SELECT count(*) FROM outbox WHERE exported=0').fetchone()[0] == 0
    exported = [json.loads(line) for file in Path(config['storage']['output_dir']).glob('*.jsonl')
                for line in file.read_text(encoding='utf-8').splitlines()]
    assert any(row['revision'] == 2 and row['task']['status'] == 'completed' for row in exported)
    assert search_tasks(config, statuses=['completed'])['tasks'][0]['task_id'] == saved['task_id']


def test_replayed_request_ignores_now_stale_expected_revision_without_duplicate_event(config, saved):
    first = update_task_status(config, saved['task_id'], 'completed', request_id='n:1', actor=ACTOR, expected_revision=1)
    assert update_task_status(config, saved['task_id'], 'completed', request_id='n:1', actor=ACTOR, expected_revision=1) == first
    assert core.list_tasks(config)[0]['revision'] == 2
    with sqlite3.connect(config['storage']['state_file']) as conn:
        assert conn.execute('SELECT count(*) FROM outbox').fetchone()[0] == 2
    with pytest.raises(TaskStatusConflict, match='request_id_content_conflict'):
        update_task_status(config, saved['task_id'], 'open', request_id='n:1', actor=ACTOR, expected_revision=1)


def test_revision_conflict_unknown_task_and_superseded_are_explicit(config, saved):
    with pytest.raises(TaskStatusConflict, match='task_revision_conflict'):
        update_task_status(config, saved['task_id'], 'completed', request_id='n:1', actor=ACTOR, expected_revision=99)
    with pytest.raises(UnknownTask):
        update_task_status(config, 'unknown', 'completed', request_id='n:2', actor=ACTOR)
    conn = core._connect(config, write=True)
    with conn:
        saved['status'] = 'superseded'
        core._save_task(conn, saved)
    conn.close()
    with pytest.raises(TaskStatusConflict, match='task_superseded'):
        update_task_status(config, saved['task_id'], 'open', request_id='n:3', actor=ACTOR)
    with sqlite3.connect(config['storage']['state_file']) as conn:
        assert conn.execute('SELECT count(*) FROM task_status_requests').fetchone()[0] == 0


def test_failed_export_is_durable_and_same_request_retries_publication(config, saved, monkeypatch):
    def failed(*args):
        raise OSError('Simulated disk failure')
    with monkeypatch.context() as patch:
        patch.setattr(storage, '_export', failed)
        with pytest.raises(TaskStatusExportPending):
            update_task_status(config, saved['task_id'], 'completed', request_id='n:1', actor=ACTOR, expected_revision=1)
    assert core.list_tasks(config)[0]['status'] == 'completed'
    with sqlite3.connect(config['storage']['state_file']) as conn:
        assert conn.execute('SELECT count(*) FROM outbox WHERE exported=0').fetchone()[0] == 1
    result = update_task_status(config, saved['task_id'], 'completed', request_id='n:1', actor=ACTOR, expected_revision=1)
    assert result['revision'] == 2
    with sqlite3.connect(config['storage']['state_file']) as conn:
        assert conn.execute('SELECT count(*) FROM outbox WHERE exported=0').fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM outbox').fetchone()[0] == 2


@pytest.mark.parametrize('status', ['completed', 'cancelled'])
def test_model_updates_cannot_reopen_explicitly_closed_task(config, saved, monkeypatch, status):
    update_task_status(config, saved['task_id'], status, request_id='n:1', actor=ACTOR)
    batch(config, 'later', event('b', timestamp=1000))
    def model(cfg, rules, group, existing):
        assert existing[0]['status'] == status
        return response(group['primary_message_keys'], identifier=saved['task_id'], title='Updated task details')
    monkeypatch.setattr(core, '_call_model', model)
    core.run_round(config)
    task = core.list_tasks(config)[0]
    assert task['status'] == status and task['title'] == 'Updated task details'
    assert task['status_control']['request_id'] == 'n:1'
    reopened = update_task_status(config, task['task_id'], 'open', request_id='n:2', actor=ACTOR, expected_revision=task['revision'])
    assert reopened['status'] == 'open'


def test_regrouping_does_not_retire_manually_completed_history(config, saved, monkeypatch):
    update_task_status(config, saved['task_id'], 'completed', request_id='n:1', actor=ACTOR)
    Path(config['rules']['file']).write_text('Changed rules', encoding='utf-8')
    monkeypatch.setattr(core, '_call_model', lambda *args: {'tasks': []})
    core.run_round(config)
    assert core.list_tasks(config)[0]['status'] == 'completed'


def test_locked_processor_returns_busy_without_waiting_or_state_change(config, saved):
    with InstanceLock(config['storage']['state_file']):
        with pytest.raises(TaskStatusBusy):
            update_task_status(config, saved['task_id'], 'completed', request_id='n:1', actor=ACTOR)
    assert core.list_tasks(config)[0]['status'] == 'open'


@pytest.mark.parametrize('kwargs', [
    {'task_id': ''}, {'request_id': 'x' * 201}, {'status': 'superseded'}, {'expected_revision': True},
    {'actor': {'platform': 'notion', 'user_id': 'u', 'source': 'page', 'sql': 'DELETE'}},
    {'actor': {'platform': 'notion', 'user_id': '', 'source': 'page'}},
    {'actor': {'platform': 'notion', 'user_id': 'u', 'source': 'line\nbreak'}},
])
def test_invalid_status_request_rejected_before_storage(config, kwargs):
    request = dict(task_id='task', status='completed', request_id='n:1', actor=ACTOR)
    request.update(kwargs)
    with pytest.raises(ValueError):
        update_task_status(config, **request)
    assert not Path(config['storage']['state_file']).exists()
