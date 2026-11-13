import copy
import json
from pathlib import Path
import sys
import types
import threading

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


@pytest.mark.parametrize('status', ['completed', 'open'])
def test_human_edits_finish_during_model_and_survive_stale_submission(config, monkeypatch, status):
    batch(config, 'first', event('a'))
    monkeypatch.setattr(core, '_call_model', lambda *args: response(['a']))
    core.run_round(config)
    saved = core.list_tasks(config)[0]
    batch(config, 'later', event('b', timestamp=1000))
    entered, release = threading.Event(), threading.Event()
    outcomes, errors = [], []
    def slow_model(cfg, rules, group, existing):
        assert existing[0]['revision'] == 1
        entered.set()
        assert release.wait(5), 'test did not release model'
        result = response(group['primary_message_keys'], identifier=saved['task_id'], title='Fresh details')
        result['tasks'][0]['status'] = 'cancelled'
        return result
    def run():
        try:
            outcomes.append(core.run_round(config))
        except BaseException as exc:
            errors.append(exc)
    monkeypatch.setattr(core, '_call_model', slow_model)
    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert entered.wait(5)
        service.update_task_status(config, saved['task_id'], status, request_id='human:status', actor=ACTOR)
        service.update_task_priority(config, saved['task_id'], 'P0', request_id='human:priority', actor=ACTOR)
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive() and not errors and outcomes
    task = service.get_task(config, saved['task_id'])
    assert task['revision'] == 4 and task['status'] == status and task['priority'] == 'P0'
    assert task['title'] == 'Fresh details' and set(task['source_message_keys']) == {'a', 'b'}


def extraction_context(config, existing):
    conn = core._connect(config)
    try:
        rules, messages, _, _, groups, _ = core._snapshot(config, conn)
    finally:
        conn.close()
    group = groups[0]
    return {'group': group, 'existing': existing, 'previous_ids': [], 'messages': messages,
            'rules': rules, 'pending_groups': 0}


def test_extraction_replay_conflicts_and_transaction_rollback(config, monkeypatch):
    batch(config, 'first', event('a'))
    monkeypatch.setattr(core, '_call_model', lambda *args: response(['a']))
    core.run_round(config)
    saved = core.list_tasks(config)[0]
    ctx = extraction_context(config, [saved])
    submission = response(['a'], identifier=saved['task_id'], title='Updated')
    original = copy.deepcopy(submission)
    first = service.apply_extraction(config, submission, request_id='apply:1', context=ctx)
    replay = service.apply_extraction(config, submission, request_id='apply:1', context=ctx)
    assert submission == original and replay['outcome'] == first['outcome']
    assert service.get_task(config, saved['task_id'])['revision'] == 2
    with pytest.raises(service.TaskStatusConflict, match='request_id_content_conflict'):
        service.apply_extraction(config, response(['a'], identifier=saved['task_id'], title='Other'),
                                 request_id='apply:1', context=ctx)
    mixed = response(['a'], title='New before conflict')
    mixed['tasks'].extend(submission['tasks'])
    with pytest.raises(service.TaskStatusConflict, match='task_revision_conflict'):
        service.apply_extraction(config, mixed, request_id='apply:2', context=ctx)
    assert len(service.search_tasks(config)['tasks']) == 1
    assert len(service.read_changes(config)['events']) == 2


def test_export_file_operations_do_not_hold_task_write_lock(config, monkeypatch):
    export = storage._export
    def checked_export(cfg, conn):
        with service._task_connection(cfg):
            pass
        return export(cfg, conn)
    monkeypatch.setattr(storage, '_export', checked_export)
    service.create_tasks(config, [explicit_task()], request_id='create:1', actor=ACTOR, origin=ORIGIN)


def test_legacy_evidence_only_material_is_available_by_stable_id(config, monkeypatch):
    batch(config, 'first', event('a'))
    monkeypatch.setattr(core, '_call_model', lambda *args: response(['a']))
    core.run_round(config)
    task = core.list_tasks(config)[0]
    task['attachment_evidence'] = [{'message_key': 'a', 'attachment_index': 0,
                                   'attachment': {'name': 'legacy.pdf', 'status': 'unavailable'}}]
    with service._task_connection(config) as conn, conn:
        storage._save_task(conn, task)
    selected = service.get_task(config, task['task_id'])['materials'][0]
    monkeypatch.setitem(sys.modules, 'providers.content', types.SimpleNamespace(
        export_material=lambda attachment, *args, **kwargs: attachment))
    assert service.get_material(config, task['task_id'], selected['material_id'])['name'] == 'legacy.pdf'


def test_changing_rules_then_restoring_them_does_not_reuse_an_unrelated_request(config, monkeypatch):
    batch(config, 'first', event('a'))
    monkeypatch.setattr(core, '_call_model', lambda *args: response(['a']))
    core.run_round(config)
    original = core.list_tasks(config)[0]
    rules = Path(config['rules']['file'])
    text = rules.read_text(encoding='utf-8')
    rules.write_text('Different rules', encoding='utf-8')
    core.run_round(config)
    rules.write_text(text, encoding='utf-8')
    core.run_round(config)
    active = core.list_tasks(config)
    assert len(active) == 1 and active[0]['task_id'] != original['task_id']
    assert service.get_task(config, original['task_id'])['status'] == 'superseded'


def test_extraction_rejects_fabricated_or_cross_conversation_input(config, monkeypatch):
    batch(config, 'first', event('a'))
    monkeypatch.setattr(core, '_call_model', lambda *args: response(['a']))
    core.run_round(config)
    saved = core.list_tasks(config)[0]
    ctx = extraction_context(config, [saved])
    ctx['group']['messages'][0]['message']['text'] = 'Fabricated changed message'
    with pytest.raises(ValueError, match='extraction_source_not_persisted_in_conversation'):
        service.apply_extraction(config, response(['a'], identifier=saved['task_id']), request_id='fake', context=ctx)
    assert service.get_task(config, saved['task_id'])['revision'] == 1


def test_external_creation_controls_survive_later_extraction(config, monkeypatch):
    created = service.create_tasks(config, [dict(explicit_task(), priority='P0', status='cancelled')],
                                   request_id='create:1', actor=ACTOR, origin=ORIGIN)['tasks'][0]
    batch(config, 'first', event('a'))
    monkeypatch.setattr(core, '_call_model', lambda *args: response(['a']))
    core.run_round(config)
    ctx = extraction_context(config, [created])
    service.apply_extraction(config, response(['a'], identifier=created['task_id']), request_id='apply:1', context=ctx)
    task = service.get_task(config, created['task_id'])
    assert task['priority'] == 'P0' and task['status'] == 'cancelled' and task['origin']['actor'] == ACTOR


@pytest.mark.parametrize('proof', [None, 'not evidence', {},
    [{'message_key': 'other', 'attachment_index': 0, 'attachment': {}, 'locator': {'page': 1}}],
    [{'message_key': 'a', 'attachment_index': 4, 'attachment': {}, 'locator': {'page': 1}}],
    [{'message_key': 'a', 'attachment_index': 0, 'attachment': {}, 'locator': {'page': 0}}],
])
def test_extraction_rejects_invalid_attachment_proofs(config, monkeypatch, proof):
    batch(config, 'first', event('a'))
    group = core.preview(config)['groups'][0]
    submitted = response(['a'])
    submitted['tasks'][0]['attachment_evidence'] = proof
    with pytest.raises(ValueError, match='Task attachment evidence'):
        core._validate_response(submitted, group, [], config['context']['timezone'])
