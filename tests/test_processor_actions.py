import json
from pathlib import Path

import pytest

from processor import core, context
from processor.task_search import TaskLookup
from processor.pi_bridge import run_agent, runtime_status
from test_processor import batch, config, event, response
from test_pi_bridge import endpoint, send_tool
from test_processor_context import config as context_config, event as context_event, group, task as context_task, pdf_attachment


def test_action_steps_survive_submission_storage_export_and_task_lookup(config, monkeypatch):
    batch(config, 'notice', event('a'))
    groups = core.preview(config)['groups']
    steps = ['填写通知中的申请表', '在 https://example.org/apply 提交申请表']

    def fake(cfg, system, payload, **kwargs):
        item = response(['a'])['tasks'][0]
        item.pop('source_message_keys')
        item.update(sources=['m1'], action_steps=steps, material_refs=['f1'])
        assert kwargs['submission_validator'](tasks=[item]) == {'accepted': True}
        return {'tasks': [item]}

    monkeypatch.setattr(context, 'run_agent', fake)
    core.run_round(config)
    saved = core.list_tasks(config)[0]
    assert saved['action_steps'] == steps
    assert saved['materials'][0]['message_key'] == 'a'
    assert saved['materials'][0]['attachment']['name'] == 'report.pdf'
    assert saved['materials'][0]['attachment']['status'] == 'unavailable'
    assert 'material_refs' not in saved
    exported = [json.loads(line) for file in Path(config['storage']['output_dir']).glob('*.jsonl')
                for line in file.read_text(encoding='utf-8').splitlines()]
    assert exported[0]['task']['action_steps'] == steps
    assert TaskLookup(config, [], {}).find(query='report')['tasks'][0]['action_steps'] == steps
    # Existing task details remain available when a later message updates the task.
    assert context.prepare_input(config, groups, [saved])[-1][0]['action_steps'] == steps


def test_legacy_task_without_action_steps_is_accepted():
    group = {'messages': [{'message_key': 'a'}], 'primary_message_keys': ['a']}
    assert core._validate_response(response(['a']), group, [], 'Asia/Shanghai')[0]['action_steps'] == []


def test_participants_do_not_require_an_assigned_owner():
    group = {'messages': [{'message_key': 'a'}], 'primary_message_keys': ['a']}
    result = response(['a'])
    result['tasks'][0]['people'] = [{'name': '李明', 'role': '参与人'}]
    task = core._validate_response(result, group, [], 'Asia/Shanghai')[0]
    assert task['people'] == [{'name': '李明', 'role': '参与人'}]
    assert not task['needs_review'] and task['review_reasons'] == []


def test_missing_people_does_not_add_approval_or_boilerplate():
    group = {'messages': [{'message_key': 'a'}], 'primary_message_keys': ['a']}
    task = core._validate_response(response(['a']), group, [], 'Asia/Shanghai')[0]
    assert task['notes'] == [] and task['needs_review'] is False


def test_uncertainty_is_saved_as_notes_without_approval(config, monkeypatch):
    batch(config, 'notice', event('a'))
    result = response(['a'])
    result['tasks'][0].update(needs_review=True, review_reasons=['入口未提供'], notes=['入口未提供', '仅有小程序卡片'])
    monkeypatch.setattr(core, '_call_model', lambda *args: result)
    core.run_round(config)
    task = core.list_tasks(config)[0]
    assert task['status'] == 'open' and task['needs_review'] is False
    assert task['notes'] == ['入口未提供', '仅有小程序卡片']
    assert TaskLookup(config, [], {}).find(query='report')['tasks'][0]['notes'] == task['notes']
    from messaging.package import build_package
    packaged = build_package(task, {})
    assert packaged['notes'] == task['notes'] and packaged['needs_review'] is False


def test_notes_only_submission_accepts_absent_legacy_review_fields():
    group = {'messages': [{'message_key': 'a'}], 'primary_message_keys': ['a']}
    result = response(['a'])
    result['tasks'][0].pop('needs_review')
    result['tasks'][0].pop('review_reasons')
    result['tasks'][0]['notes'] = ['入口未提供']
    assert core._validate_response(result, group, [], 'Asia/Shanghai')[0]['notes'] == ['入口未提供']


@pytest.mark.skipif(not runtime_status()['installed'], reason='Pinned Pi runtime is not installed')
def test_native_pi_submission_accepts_action_steps(endpoint):
    config, payload, responder, requests = endpoint
    item = response(['a'])['tasks'][0]
    item.pop('source_message_keys')
    item.pop('needs_review')
    item.pop('review_reasons')
    item.update(sources=['m1'], action_steps=['Fill in the attached form'], material_refs=['f1'], notes=['Submission link was not provided'])
    responder.append(lambda handler, _: send_tool(handler, 'submit_tasks', {'tasks': [item]}))
    result = run_agent(config, 'Extract explicit tasks', payload, api_key='fixture-only')
    assert result['tasks'][0]['action_steps'] == item['action_steps']
    assert result['tasks'][0]['material_refs'] == ['f1']
    assert result['tasks'][0]['notes'] == item['notes']
    assert len(requests) == 1


@pytest.mark.parametrize('steps', [None, 'do this', {}, [None], [True], ['  '], ['x' * 2001], ['x'] * 51])
def test_malformed_or_unbounded_steps_are_rejected(steps):
    group = {'messages': [{'message_key': 'a'}], 'primary_message_keys': ['a']}
    result = response(['a'])
    result['tasks'][0]['action_steps'] = steps
    with pytest.raises(ValueError, match='action_steps'):
        core._validate_response(result, group, [], 'Asia/Shanghai')


def test_only_selected_unread_material_is_mapped_and_model_paths_are_ignored(context_config, monkeypatch):
    selected, private = {'name': 'form.docx'}, {'name': 'private.pdf'}
    record = context_event('notice', attachments=[selected, private])
    def fake(cfg, system, payload, **kwargs):
        item = context_task('m1')
        item.update(material_refs=['f1'], materials=[{'attachment': {'archive_path': 'INVENTED'}}])
        assert kwargs['submission_validator'](tasks=[item]) == {'accepted': True}
        return {'tasks': [item]}
    monkeypatch.setattr(context, 'run_agent', fake)
    result = context.process_batch(context_config, {'text': 'Rules'}, [group(record)], [])
    materials = result['group-id']['tasks'][0]['materials']
    assert len(materials) == 1 and materials[0]['attachment_index'] == 0
    assert materials[0]['attachment']['name'] == selected['name']
    assert materials[0]['attachment']['status'] == 'unavailable'


@pytest.mark.parametrize('refs, sources, other_room', [(['f99'], ['m1'], False),
    (['f2'], ['m1'], False), (['f2'], ['m1'], True), (None, ['m1'], False)])
def test_fabricated_uncited_and_cross_conversation_materials_are_recoverably_rejected(context_config, monkeypatch, refs, sources, other_room):
    a = context_event('a', attachments=[{'name': 'a.pdf'}])
    b = context_event('b', conversation='other' if other_room else 'group-one', attachments=[{'name': 'b.pdf'}])
    groups = [group(a, identifier='a'), group(b, identifier='b')] if other_room else [group(a, b)]
    def fake(cfg, system, payload, **kwargs):
        item = context_task(*sources)
        item['material_refs'] = refs
        assert kwargs['submission_validator'](tasks=[item])['accepted'] is False
        return {'tasks': []}
    monkeypatch.setattr(context, 'run_agent', fake)
    context.process_batch(context_config, {'text': 'Rules'}, groups, [])


def test_cited_read_evidence_automatically_adds_material(context_config, tmp_path, monkeypatch):
    attachment = pdf_attachment(tmp_path)
    record = context_event('notice', attachments=[attachment])
    def fake(cfg, system, payload, **kwargs):
        kwargs['attachment_reader'](ref='f1', pages='1')
        return {'tasks': [context_task('m1', 'f1:p1')]}
    monkeypatch.setattr(context, 'run_agent', fake)
    result = context.process_batch(context_config, {'text': 'Rules'}, [group(record)], [])
    assert result['group-id']['tasks'][0]['materials'] == [
        {'message_key': record['message_key'], 'attachment_index': 0, 'attachment': attachment}]


def test_later_revision_keeps_previous_selected_materials(config, monkeypatch):
    batch(config, 'first', event('a'))
    def extract(cfg, rules, selected, existing):
        key = selected['primary_message_keys'][0]
        result = response([key], identifier=existing[0]['task_id'] if existing else None)
        result['tasks'][0]['materials'] = [{'message_key': key, 'attachment_index': 0,
                                           'attachment': event(key)['message']['attachments'][0]}]
        return result
    monkeypatch.setattr(core, '_call_model', extract)
    core.run_round(config)
    identifier = core.list_tasks(config)[0]['task_id']
    batch(config, 'later', event('b', timestamp=1000))
    core.run_round(config)
    saved = next(task for task in core.list_tasks(config) if task['task_id'] == identifier)
    assert saved['revision'] == 2
    assert {material['message_key'] for material in saved['materials']} == {'a', 'b'}


def test_unread_selected_material_is_prepared_only_after_final_validation(context_config, tmp_path, monkeypatch):
    from providers.registry import AttachmentProviders
    archived = dict(pdf_attachment(tmp_path), quality='original')
    original = {'name': archived['name'], 'type': 'file'}
    record = context_event('notice', attachments=[original, {'name': 'unrelated.pdf'}])
    calls = []
    def resolve(self, event, index):
        calls.append(index)
        return dict(original, **archived)
    monkeypatch.setattr(AttachmentProviders, 'resolve', resolve)
    def forbidden(*args):
        raise AssertionError('Preparing materials must not parse document content')
    monkeypatch.setattr(context.content.AttachmentContext, '_prepare', forbidden)
    def fake(cfg, system, payload, **kwargs):
        item = context_task('m1')
        item['material_refs'] = ['f1']
        assert kwargs['submission_validator'](tasks=[item]) == {'accepted': True}
        assert calls == []
        return {'tasks': [item]}
    monkeypatch.setattr(context, 'run_agent', fake)
    result = context.process_batch(context_config, {'text': 'Rules'}, [group(record)], [])
    task = result['group-id']['tasks'][0]
    assert calls == [0]
    assert task['materials'][0]['attachment']['quality'] == 'original'
    assert task['materials'][0]['attachment']['archive_path'] == archived['archive_path']
    assert task['attachment_evidence'] == []
