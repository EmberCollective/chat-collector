"""Integration checks using real local attachment parsers and a fake Pi boundary."""
import hashlib
import json

import pymupdf
import pytest
from PIL import Image
from docx import Document

from processor import context


@pytest.fixture
def config(tmp_path):
    (tmp_path/'archive').mkdir()
    return {'storage': {'state_file': str(tmp_path/'state.db')},
            'model': {'vision': False}, 'context': {'timezone':'Asia/Shanghai'},
            'processing': {'max_input_chars': 100000},
            'attachments': {'archive_roots': [str(tmp_path/'archive')], 'cache_dir': str(tmp_path/'cache')}}


def pdf_attachment(tmp_path):
    path = tmp_path/'archive/report.pdf'
    with pymupdf.open() as doc:
        doc.new_page().insert_text((72,72), 'Only visible after explicit read')
        doc.save(path)
    return {'name':'report.pdf', 'archive_path':str(path), 'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}


def event(name, *, conversation='group-one', attachments=()):
    return {'message_key': hashlib.sha256(name.encode()).hexdigest(), 'event_id':'real-event-'+name,
            'revision':1, 'message':{'platform':'qq', 'account_id':'real-account',
                'conversation_id':conversation, 'message_id':'real-message-'+name,
                'sender_id':'sender', 'sent_at':100, 'text':'Please handle the notice',
                'attachments':list(attachments)}}


def group(*events, identifier='group-id', primary=None):
    msg = events[0]['message']
    return {'id':identifier, **{key:msg[key] for key in ('platform','account_id','conversation_id')},
            'messages':list(events), 'primary_message_keys':primary or [e['message_key'] for e in events],
            'history_message_keys':[]}


def task(*sources, identifier=None):
    return {'task_id':identifier, 'title':'Read notice', 'event':'Read the attached notice',
            'priority':'P2', 'priority_reason':'Requested action', 'status':'open',
            'time':{'start':None,'deadline':None,'text':None}, 'people':[], 'location':None,
            'needs_review':False, 'review_reasons':[], 'sources':list(sources)}


def run(config, groups, monkeypatch, fake, existing=()):
    monkeypatch.setattr(context, 'run_agent', fake)
    return context.process_batch(config, {'text':'Extract explicit tasks'}, groups, existing)


def test_context_initial_prompt_is_index_only_and_has_no_internal_identifiers(config, tmp_path, monkeypatch):
    attachment = pdf_attachment(tmp_path)
    record = event('one', attachments=[attachment])
    def forbidden(*args):
        raise AssertionError('initial model input must not parse attachment contents')
    monkeypatch.setattr(context.content.AttachmentContext, '_prepare', forbidden)
    payload, *_ = context.prepare_input(config, [group(record)])
    serialized = json.dumps(payload)
    for private in [attachment['sha256'], attachment['archive_path'], record['message_key'],
                    record['event_id'], record['message']['message_id'], 'Only visible after explicit read']:
        assert private not in serialized
    assert payload['attachments'][0]['ref'] == 'f1'
    assert payload['attachments'][0]['content_status'] == 'unread'


def test_attachment_tool_reply_hides_hashes_and_real_message_keys(config, tmp_path, monkeypatch):
    attachment = pdf_attachment(tmp_path)
    record = event('one', attachments=[attachment])
    def fake(cfg, system, payload, **kwargs):
        result = kwargs['attachment_reader'](ref='f1', pages='1')
        serialized = json.dumps(result)
        assert attachment['sha256'] not in serialized
        assert record['message_key'] not in serialized
        assert attachment['archive_path'] not in serialized
        assert 'Only visible after explicit read' in serialized
        return {'tasks':[task('f1:p1')]}
    result = run(config, [group(record)], monkeypatch, fake)
    evidence = result['group-id']['tasks'][0]['attachment_evidence'][0]
    assert evidence['message_key'] == record['message_key']
    assert evidence['attachment_index'] == 0
    assert evidence['locator'] == {'page':1}
    assert evidence['attachment'] == attachment


def test_same_name_missing_attachment_does_not_shift_source_index(config, tmp_path, monkeypatch):
    attachment = pdf_attachment(tmp_path)
    record = event('one', attachments=[{'name':'report.pdf','status':'missing'}, attachment])
    def fake(cfg, system, payload, **kwargs):
        kwargs['attachment_reader'](ref='f2', pages='1')
        return {'tasks':[task('m1','f2:p1')]}
    result = run(config, [group(record)], monkeypatch, fake)['group-id']['tasks'][0]
    assert result['attachment_evidence'][0]['attachment_index'] == 1
    assert result['attachment_evidence'][0]['attachment'] == attachment
    assert result['needs_review'] is False  # An unrelated, unrequested missing file is not a blanket failure.


def test_docx_evidence_keeps_block_locator_without_inventing_page(config, tmp_path, monkeypatch):
    path = tmp_path/'archive/notice.docx'
    document = Document(); document.add_paragraph('Document action'); document.save(path)
    record = event('one', attachments=[{'archive_path':str(path),'name':'notice.docx'}])
    def fake(cfg, system, payload, **kwargs):
        kwargs['attachment_reader'](ref='f1', pages='1')
        return {'tasks':[task('f1:b1')]}
    evidence = run(config, [group(record)], monkeypatch, fake)['group-id']['tasks'][0]['attachment_evidence'][0]
    assert evidence['locator'] == {'block':1}
    assert evidence['attachment_index'] == 0


def test_model_cannot_cite_attachment_page_before_reading(config, tmp_path, monkeypatch):
    record = event('one', attachments=[pdf_attachment(tmp_path)])
    with pytest.raises(ValueError, match='unread or fabricated'):
        run(config, [group(record)], monkeypatch, lambda *args, **kwargs: {'tasks':[task('f1:p1')]})


def test_task_cannot_mix_messages_from_different_conversations(config, monkeypatch):
    groups = [group(event('a'), identifier='a'), group(event('b', conversation='other'), identifier='b')]
    with pytest.raises(ValueError, match='one explicit conversation'):
        run(config, groups, monkeypatch, lambda *args, **kwargs: {'tasks':[task('m1','m2')]})


def test_submission_feedback_rejects_cross_conversation_then_accepts_corrected_tasks(config, monkeypatch):
    groups = [group(event('a'), identifier='a'), group(event('b', conversation='other'), identifier='b')]
    def fake(cfg, system, payload, **kwargs):
        feedback = kwargs['submission_validator'](tasks=[task('m1', 'm2')])
        assert feedback['accepted'] is False
        assert feedback['source_conversations'] == {'m1':['c1'], 'm2':['c2']}
        assert 'one explicit conversation' in feedback['error']
        for g in groups:
            assert g['messages'][0]['message_key'] not in json.dumps(feedback)
        corrected = {'tasks':[task('m1'), task('m2')]}
        assert kwargs['submission_validator'](**corrected) == {'accepted': True}
        return corrected
    result = run(config, groups, monkeypatch, fake)
    assert len(result['a']['tasks']) == len(result['b']['tasks']) == 1


def test_submission_accepts_multiple_segments_in_one_conversation(config, monkeypatch):
    first, second = event('a'), event('b')
    selected = group(first, second)
    selected['segments'] = [[first], [second]]
    def fake(cfg, system, payload, **kwargs):
        submitted = {'tasks':[task('m1', 'm2')]}
        assert kwargs['submission_validator'](**submitted) == {'accepted': True}
        return submitted
    assert len(run(config, [selected], monkeypatch, fake)['group-id']['tasks']) == 1


def test_submission_feedback_includes_business_validation_before_accepting(config, monkeypatch):
    history, current = event('history'), event('current')
    selected = group(history, current, primary=[current['message_key']])
    def fake(cfg, system, payload, **kwargs):
        feedback = kwargs['submission_validator'](tasks=[task('m1')])
        assert feedback['accepted'] is False
        assert 'historical context' in feedback['error']
        return {'tasks':[task('m2')]}
    assert len(run(config, [selected], monkeypatch, fake)['group-id']['tasks']) == 1


def test_attachment_proof_cannot_be_attached_to_another_conversation(config, tmp_path, monkeypatch):
    groups = [group(event('a', attachments=[pdf_attachment(tmp_path)]), identifier='a'),
              group(event('b', conversation='other'), identifier='b')]
    def fake(cfg, system, payload, **kwargs):
        kwargs['attachment_reader'](ref='f1', pages='1')
        return {'tasks':[task('m2','f1:p1')]}
    with pytest.raises(ValueError, match='different conversation'):
        run(config, groups, monkeypatch, fake)


def test_duplicate_file_across_conversations_uses_explicit_message_owner(config, tmp_path, monkeypatch):
    attachment = pdf_attachment(tmp_path)
    first = event('a', attachments=[attachment]); second = event('b', conversation='other', attachments=[attachment])
    def fake(cfg, system, payload, **kwargs):
        kwargs['attachment_reader'](ref='f1', pages='1')
        return {'tasks':[task('m2','f1:p1')]}
    result = run(config, [group(first, identifier='a'),group(second, identifier='b')], monkeypatch, fake)
    evidence = result['b']['tasks'][0]['attachment_evidence']
    assert len(evidence) == 1 and evidence[0]['message_key'] == second['message_key']
    assert result['a']['tasks'] == []


@pytest.mark.parametrize('read', [False, True])
def test_empty_tasks_do_not_require_full_attachment_read(config, tmp_path, monkeypatch, read):
    record = event('one', attachments=[pdf_attachment(tmp_path)])
    def fake(cfg, system, payload, **kwargs):
        if read:
            kwargs['attachment_reader'](ref='f1', pages='1')
        return {'tasks':[]}
    result = run(config, [group(record)], monkeypatch, fake)['group-id']
    assert result['tasks'] == []
    assert result['attachment_review'][0]['status'] == ('read' if read else 'not_requested')
    assert result['review_required'] is False


def test_unread_attachments_do_not_mark_unrelated_tasks_for_review(config, tmp_path, monkeypatch):
    record = event('one', attachments=[pdf_attachment(tmp_path)])
    result = run(config, [group(record)], monkeypatch,
                 lambda *args, **kwargs: {'tasks':[task('m1')]})['group-id']['tasks'][0]
    assert result['needs_review'] is False
    assert result['review_reasons'] == []
    assert result['attachment_evidence'] == []


@pytest.mark.parametrize('vision', [False, True])
def test_image_coverage_controls_empty_task_success(config, tmp_path, monkeypatch, vision):
    config['model']['vision'] = vision
    path = tmp_path/'archive/image.png'; Image.new('RGB',(30,30),'blue').save(path)
    record = event('one', attachments=[{'archive_path':str(path),'name':'image.png'}])
    def fake(cfg, system, payload, **kwargs):
        tool = kwargs['attachment_reader'](ref='f1')
        assert any(block['type']=='image' for block in tool['content']) == vision
        assert bool(tool['details']['evidence']) == vision
        return {'tasks':[]}
    result = run(config, [group(record)], monkeypatch, fake)['group-id']
    assert result['tasks'] == []
    assert result['review_required'] is (not vision)


def test_failed_attachment_is_a_local_review_not_a_batch_failure(config, tmp_path, monkeypatch):
    missing = event('missing', attachments=[{'name':'missing.pdf'}])
    valid = event('valid', conversation='other')
    def fake(cfg, system, payload, **kwargs):
        assert kwargs['attachment_reader'](ref='f1')['details']['status'] == 'needs_review'
        return {'tasks':[task('m2')]}
    result = run(config, [group(missing,identifier='missing'),group(valid,identifier='valid')],monkeypatch,fake)
    assert result['missing']['tasks'] == [] and result['missing']['review_required']
    assert len(result['valid']['tasks']) == 1 and not result['valid']['review_required']
    from pathlib import Path
    audit = json.loads(Path(result['valid']['submission_file']).read_text(encoding='utf-8'))
    assert audit['result']['tasks'][0]['sources'] == ['m2']
    assert 'api_key' not in audit


def test_attachment_search_excerpt_is_partial_not_unavailable(config, tmp_path, monkeypatch):
    path = tmp_path/'archive/long.txt'
    path.write_text('x'*5000+'deadline tomorrow'+'y'*5000,encoding='utf-8')
    record = event('excerpt',attachments=[{'name':'long.txt','archive_path':str(path)}])
    def fake(cfg, system, payload, **kwargs):
        result = kwargs['attachment_reader'](ref='f1',query='deadline')
        assert result['details']['evidence']
        return {'tasks':[]}
    result = run(config,[group(record)],monkeypatch,fake)['group-id']
    assert result['attachment_review'][0]['status']=='partial'
    assert 'text_excerpt_not_full_unit' in result['attachment_review'][0]['warnings']
    assert not result['review_required']


def test_shared_history_does_not_make_current_message_owner_ambiguous(config, monkeypatch):
    first, second = event('first'), event('second')
    groups = [group(first, identifier='first-group'),
              group(first, second, identifier='second-group', primary=[second['message_key']])]
    result = run(config, groups, monkeypatch, lambda *args, **kwargs: {'tasks':[task('m1')]})
    assert len(result['first-group']['tasks']) == 1
    assert result['second-group']['tasks'] == []


def test_task_reschedule_keeps_prior_attachment_page_evidence(config, tmp_path, monkeypatch):
    from processor import core
    (tmp_path/'incoming').mkdir()
    (tmp_path/'rules.md').write_text('Extract explicit actions', encoding='utf-8')
    path = tmp_path/'processor.toml'
    path.write_text('''[input]
batches_dir="incoming"
[storage]
state_file="state.db"
output_dir="todos"
[rules]
file="rules.md"
[model]
base_url="http://127.0.0.1:9/v1"
model="synthetic-only"
api_key_env=""
api_key_file=""
env_file=""
[context]
grouping="time"
[agent]
batching="single_conversation"
[attachments]
archive_roots=["archive"]
cache_dir="cache"
''', encoding='utf-8')
    settings = core.load_config(path)
    attachment = pdf_attachment(tmp_path)
    initial = dict(event('initial', attachments=[attachment]), schema_version=1, operation='upsert')
    (tmp_path/'incoming/one.jsonl').write_text(json.dumps(initial), encoding='utf-8')
    proof = {'ref':'f1:p1','message_key':initial['message_key'],'attachment_index':0,
             'locator':{'page':1},'attachment':attachment}
    calls = []
    def extract(cfg, rules, selected, existing):
        calls.append(selected)
        result = task(identifier=existing[0]['task_id'] if existing else None)
        result.pop('sources')
        result['source_message_keys'] = [selected['primary_message_keys'][-1]]
        result['attachment_evidence'] = [proof] if len(calls) == 1 else []
        result['time']['deadline'] = '2026-12-01' if len(calls) == 1 else '2026-12-02'
        return {'tasks':[result]}
    monkeypatch.setattr(core, '_call_model', extract)
    core.run_round(settings)
    first = core.list_tasks(settings)[0]
    followup = dict(event('rescheduled'), schema_version=1, operation='upsert')
    followup['message'].update(sent_at=110, text='Move the deadline to December 2')
    (tmp_path/'incoming/two.jsonl').write_text(json.dumps(followup), encoding='utf-8')
    core.run_round(settings)
    latest = core.list_tasks(settings)[0]
    assert latest['task_id'] == first['task_id'] and latest['revision'] == 2
    assert latest['time']['deadline'] == '2026-12-02'
    assert latest['attachment_evidence'] == [proof]
    assert set(latest['source_message_keys']) == {initial['message_key'], followup['message_key']}
