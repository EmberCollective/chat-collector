import copy
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from contracts import ingest_batch, validate_event
from processor.context import prepare_input
from test_processor import config, event


def test_publish_validates_whole_batch_and_is_repeatable_and_concurrent(tmp_path):
    incoming = tmp_path/'incoming'
    one = event('one')
    invalid = event('bad'); invalid['message']['attachments'] = ['invalid']
    with pytest.raises(ValueError):
        ingest_batch([one, invalid], incoming)
    assert not incoming.exists()
    with ThreadPoolExecutor(max_workers=3) as pool:
        paths = list(pool.map(lambda _: ingest_batch([one, one], incoming), range(6)))
    assert len(set(paths)) == 1
    assert len(paths[0].read_text(encoding='utf-8').splitlines()) == 1
    assert len(list(incoming.iterdir())) == 1
    assert ingest_batch([], incoming) is None
    paths[0].write_text('tampered',encoding='utf-8')
    with pytest.raises(ValueError,match='modified'):
        ingest_batch([one],incoming)
    assert paths[0].read_text(encoding='utf-8')=='tampered'
    assert not list(incoming.glob('*.tmp'))


def test_same_revision_conflict_is_not_published(tmp_path):
    one = event('one'); conflict = copy.deepcopy(one)
    conflict['event_id'] = 'different-event'; conflict['message']['text'] = 'different'
    with pytest.raises(ValueError, match='Conflicting'):
        ingest_batch([one, conflict], tmp_path/'inbox')
    assert not (tmp_path/'inbox').exists()


def test_public_inbox_reaches_existing_processor_dedup(config, monkeypatch):
    from processor import core
    from test_processor import response
    calls = []
    def model(cfg, rules, group, existing):
        calls.append(group['id'])
        return response(group['primary_message_keys'])
    monkeypatch.setattr(core, '_call_model', model)
    one=event('one')
    one['message'].update(sender='Teacher',context_notes=[{'kind':'thread','text':'Report revision'}])
    ingest_batch([one], config['input']['batches_dir'])
    assert core.run_round(config)['tasks_upserted'] == 1
    source=core.list_tasks(config)[0]['sources'][0]
    assert source['sender']=='Teacher' and source['context_notes']==one['message']['context_notes']
    ingest_batch([one], config['input']['batches_dir'])
    assert core.run_round(config)['tasks_upserted'] == 0
    assert len(calls) == 1


@pytest.mark.parametrize('mutate', [
    lambda e: e.update(schema_version=True),
    lambda e: e['message'].update(mentions_all='false'),
    lambda e: e['message'].update(identity=[]),
    lambda e: e['message'].update(identity={'account': {'id':'wrong'}}),
    lambda e: e['message'].update(identity={'people':[{'id':'unrelated'}]}),
    lambda e: e['message'].update(identity={'people':[{'id':'sender-1','is_self':True}]}),
    lambda e: e['message'].update(mentioned_user_ids=[{}]),
    lambda e: e['message'].update(extensions={'qq':'not an object'}),
    lambda e: e['message'].update(context_notes=[{'kind':'draft','text':'Internal draft'}]),
    lambda e: e['message'].update(context_notes=[{'kind':'thread','text':'x'*501}]),
    lambda e: e.update(window={'start_inclusive':100,'end_exclusive':100}),
])
def test_invalid_provider_data_is_rejected_before_processing(mutate):
    one = event('one'); mutate(one)
    with pytest.raises(ValueError):
        validate_event(one)


def test_only_explicit_facts_and_scoped_people_enter_model(tmp_path):
    one = event('one')
    one['message'].update(sender='Teacher (Alice)',
        context_notes=[{'kind':'conversation_topic','text':'Project A'}],
        extensions={'custom':{'internal_draft':'Do not send this'}},
        identity={'people':[{'id':'sender-1','nickname':'Alice'}]})
    validate_event(one)
    group={'id':'group','platform':'qq','account_id':'fixture','conversation_id':'room',
           'messages':[one],'primary_message_keys':['one']}
    payload,*_=prepare_input({'context':{'timezone':'Asia/Shanghai'},
        'storage':{'state_file':str(tmp_path/'state.db')}},[group])
    row=payload['conversations'][0]['segments'][0]['messages'][0]
    assert row['sender']=='Teacher (Alice)'
    assert row['context_notes']==one['message']['context_notes']
    assert len(payload['conversations'][0]['people'])==1
    assert 'Do not send this' not in json.dumps(payload)
