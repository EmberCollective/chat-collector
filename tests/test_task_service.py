import copy
import json
from pathlib import Path
import sys
import types

import pytest

from processor import core, service, storage
from test_processor import batch, config, event, response


ACTOR = {'platform': 'third', 'user_id': 'verified-user', 'source': 'callback'}
ORIGIN = {'kind': 'channel', 'platform': 'third', 'account_id': 'account', 'conversation_id': 'room'}


def explicit_task():
    task = response(['request'])['tasks'][0]
    task.pop('source_message_keys')
    return task


def test_external_create_is_atomic_audited_idempotent_and_model_free(config, monkeypatch):
    monkeypatch.setattr(core, '_call_model', lambda *args: pytest.fail('unexpected model'))
    result = service.create_tasks(config, [explicit_task()], request_id='create:1', actor=ACTOR, origin=ORIGIN)
    assert service.create_tasks(config, [explicit_task()], request_id='create:1', actor=ACTOR, origin=ORIGIN) == result
    task = service.get_task(config, result['tasks'][0]['task_id'])
    assert task['origin']['actor'] == ACTOR and task['source_message_keys'] == [] and task['sources'] == []
    assert task['revision'] == 1 and len(service.search_tasks(config)['tasks']) == 1
    changed = dict(explicit_task(), title='different')
    with pytest.raises(service.TaskStatusConflict, match='request_id_content_conflict'):
        service.create_tasks(config, [changed], request_id='create:1', actor=ACTOR, origin=ORIGIN)
    with pytest.raises(ValueError):
        service.create_tasks(config, [explicit_task(), dict(explicit_task(), priority='invalid')],
                             request_id='create:2', actor=ACTOR, origin=ORIGIN)
    assert len(service.read_changes(config)['events']) == 1


@pytest.mark.parametrize('change', [
    {'source_message_keys': ['fabricated']}, {'sources': [{'message_key': 'fabricated'}]},
    {'materials': [{'archive_path': 'secret'}]}, {'task_id': 'other'},
])
def test_external_create_rejects_claimed_chat_evidence_before_opening_database(config, change):
    with pytest.raises(ValueError, match='external_task_cannot_claim_chat_evidence'):
        service.create_tasks(config, [dict(explicit_task(), **change)], request_id='bad', actor=ACTOR, origin=ORIGIN)
    assert not Path(config['storage']['state_file']).exists()


def test_change_stream_is_ordered_stable_and_includes_unpublished_snapshots(config, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(storage, '_export', lambda *args: (_ for _ in ()).throw(OSError('disk')))
        with pytest.raises(service.TaskStatusExportPending):
            service.create_tasks(config, [explicit_task()], request_id='create:1', actor=ACTOR, origin=ORIGIN)
    first = service.read_changes(config, limit=1)
    task = first['events'][0]['task']
    service.update_task_priority(config, task['task_id'], 'P0', request_id='priority:1', actor=ACTOR)
    service.update_task_status(config, task['task_id'], 'completed', request_id='status:1', actor=ACTOR)
    assert service.read_changes(config, limit=1)['events'] == first['events']
    assert first['events'][0]['task']['priority'] == 'P2' and first['events'][0]['task']['status'] == 'open'
    second = service.read_changes(config, json.loads(json.dumps(first['next_cursor'])), limit=1)
    third = service.read_changes(config, second['next_cursor'], limit=1)
    assert second['has_more'] and second['events'][0]['revision'] == 2
    assert not third['has_more'] and third['events'][0]['revision'] == 3
    assert service.read_changes(config, third['next_cursor'])['events'] == []


@pytest.mark.parametrize('cursor', [1, '', {}, {'version': True, 'seq': 0}, {'version': 1, 'seq': -1},
                                    {'version': 2, 'seq': 0}, {'version': 1, 'seq': '0'}])
def test_invalid_cursor_is_rejected(config, cursor):
    with pytest.raises(ValueError, match='invalid_change_cursor'):
        service.read_changes(config, cursor)
    assert not Path(config['storage']['state_file']).exists()


def test_material_access_checks_task_association_then_delegates_without_task_lock(config, monkeypatch):
    batch(config, 'one', event('a'))
    def extract(*args):
        task = response(['a'])
        task['tasks'][0]['materials'] = [{'message_key': 'a', 'attachment_index': 0,
                                        'attachment': {'name': 'report.pdf', 'status': 'unavailable'}}]
        return task
    monkeypatch.setattr(core, '_call_model', extract)
    core.run_round(config)
    task = service.get_task(config, core.list_tasks(config)[0]['task_id'])
    calls = []
    def export(attachment, cfg, **kwargs):
        with service._task_connection(cfg):
            calls.append((attachment, kwargs))
        return {'name': 'report.pdf', 'status': 'available'}
    monkeypatch.setitem(sys.modules, 'providers.content', types.SimpleNamespace(export_material=export))
    with pytest.raises(ValueError, match='task_material_not_found'):
        service.get_material(config, task['task_id'], 'unbound')
    assert calls == []
    result = service.get_material(config, task['task_id'], task['materials'][0]['material_id'])
    assert result['status'] == 'available'
    assert calls[0][1]['event']['message_key'] == 'a' and calls[0][1]['attachment_index'] == 0


def test_wakeup_failure_cannot_undo_committed_task(config, monkeypatch):
    monkeypatch.setitem(sys.modules, 'automation.wakeup', types.SimpleNamespace(
        notify=lambda *args: (_ for _ in ()).throw(OSError('lost signal'))))
    result = service.create_tasks(config, [explicit_task()], request_id='create:1', actor=ACTOR, origin=ORIGIN)
    assert service.get_task(config, result['tasks'][0]['task_id'])['revision'] == 1
