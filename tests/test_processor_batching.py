"""Boundary tests for SQL context packing and local credential loading."""
import datetime as dt
import json
from pathlib import Path
import sqlite3

import pytest

from test_processor import config, event, batch
from processor import core


def selected(key, timestamp, *, previous=None, conversation='room', text='Submit report'):
    record = event(key, timestamp, conversation=conversation, text=text)
    record['message']['attachments'] = []
    record['message']['source']['selection_context'] = {
        'previous_known': previous is not None,
        'previous_message_key': previous,
    }
    return record


def groups(config, *events):
    return core._groups(config, {e['message_key']: e for e in events}, core._rules(config))


def test_sql_related_segment_is_not_split_by_message_count(config):
    config['context'].update(grouping='sql', max_messages=2)
    result = groups(config, selected('a', 100), selected('b', 101, previous='a'), selected('c', 102, previous='b'))
    assert len(result) == 1
    assert result[0]['primary_message_keys'] == ['a', 'b', 'c']
    assert len(result[0]['segments']) == 1


def test_sql_message_gaps_remain_distinct_segments(config):
    config['context']['grouping'] = 'sql'
    result = groups(config, selected('a', 100), selected('c', 102, previous='not-selected-b'))
    assert len(result) == 1
    assert [[e['message_key'] for e in segment] for segment in result[0]['segments']] == [['a'], ['c']]
    assert result[0]['original_conversation_complete'] is False


def test_conversation_char_limit_packs_only_at_complete_segment_boundaries(config):
    config['context']['grouping'] = 'sql'
    first, second = selected('a', 100, text='A' * 500), selected('b', 101, text='B' * 500)
    config['context']['max_conversation_chars'] = core._message_chars(first) + core._message_chars(second) - 1
    result = groups(config, first, second)
    assert len(result) == 2
    assert [g['primary_message_keys'] for g in result] == [['a'], ['b']]
    assert all(sum(core._message_chars(e) for e in g['messages']) <= config['context']['max_conversation_chars'] for g in result)


def test_oversize_related_segment_fails_instead_of_cutting(config):
    config['context']['grouping'] = 'sql'
    first, second = selected('a', 100, text='A' * 500), selected('b', 101, previous='a', text='B' * 500)
    config['context']['max_conversation_chars'] = core._message_chars(first) + core._message_chars(second) - 1
    with pytest.raises(ValueError, match='片段未拆分'):
        groups(config, first, second)


def test_auto_combines_conversations_and_single_mode_separates(config):
    config['context']['grouping'] = 'sql'
    result = groups(config, selected('a', 100, conversation='room-a'), selected('b', 101, conversation='room-b'))
    config['agent']['batching'] = 'auto'
    assert len(core._batch_plan(config, result, core._rules(config))) == 1
    config['agent']['batching'] = 'single_conversation'
    assert len(core._batch_plan(config, result, core._rules(config))) == 2


def test_auto_respects_total_budget_without_splitting_groups(config):
    config['context']['grouping'] = 'sql'
    config['agent']['batching'] = 'auto'
    result = groups(config, *(selected(str(i), 100 + i, conversation=f'room-{i}', text='X' * 500) for i in range(3)))
    rules = core._rules(config)
    needed = sum(core._message_chars(e) for e in result[0]['messages']) + 512
    config['processing']['max_input_chars'] = 2 * needed - 1 + len(rules['text']) + 3000
    plan = core._batch_plan(config, result, rules)
    assert len(plan) == 3
    assert [part[0]['id'] for part in plan] == [g['id'] for g in result]
    assert all(len(part) == 1 for part in plan)


def test_first_start_excludes_history_but_keeps_newly_selected_context(config):
    config['context']['grouping'] = 'sql'
    config['input']['start_at'] = 150
    old_unselected = event('old-unselected', 90)
    old_context = selected('a', 100)
    current = selected('b', 200, previous='a')
    stale_context = selected('stale', 120, conversation='old-room')
    stale_context['collected_at'] = dt.datetime.fromtimestamp(140, dt.timezone.utc).isoformat()
    result = groups(config, old_unselected, old_context, current, stale_context)
    assert len(result) == 1
    assert result[0]['primary_message_keys'] == ['b']
    assert result[0]['history_message_keys'] == ['a']
    assert [e['message_key'] for e in result[0]['messages']] == ['a', 'b']


def test_first_start_retains_exact_boundary_message(config):
    config['context']['grouping'] = 'sql'
    config['input']['start_at'] = 150
    result = groups(config, selected('a', 150))
    assert result[0]['primary_message_keys'] == ['a']


def test_run_batches_uses_one_agent_call_for_auto_and_one_per_conversation_when_forced(config, monkeypatch):
    from processor import context
    config['context']['grouping'] = 'sql'
    config['agent']['batching'] = 'auto'
    result = groups(config, selected('a', 100, conversation='room-a'), selected('b', 101, conversation='room-b'))
    calls = []
    def multiple(cfg, rules, batch, existing, api_key=''):
        calls.append([g['conversation_id'] for g in batch])
        return {g['id']: {'tasks': []} for g in batch}
    monkeypatch.setattr(context, 'process_batch', multiple)
    monkeypatch.setattr(core, '_call_model', lambda cfg, rules, group, existing:
                        multiple(cfg, rules, [group], existing)[group['id']])
    with sqlite3.connect(':memory:') as db:
        db.executescript(core.SCHEMA)
        outputs = list(core._run_batches(config, core._rules(config), result, db))
        assert len(outputs) == 2 and calls == [['room-a', 'room-b']]
        calls.clear()
        config['agent']['batching'] = 'single_conversation'
        assert len(list(core._run_batches(config, core._rules(config), result, db))) == 2
        assert calls == [['room-a'], ['room-b']]


def test_dotenv_reads_only_selected_key_without_interpolation_or_process_mutation(config, monkeypatch):
    env_path = Path(config['_config_path']).parent / 'keys.env'
    env_path.write_text('PI_TEST_LOCAL_KEY="literal-${NOT_EXPANDED}"\nUNRELATED_TEST_KEY=private-other\n', encoding='utf-8')
    config['model'].update(api_key_env='PI_TEST_LOCAL_KEY', env_file=str(env_path))
    monkeypatch.delenv('PI_TEST_LOCAL_KEY', raising=False)
    monkeypatch.delenv('UNRELATED_TEST_KEY', raising=False)
    assert core._api_key(config) == 'literal-${NOT_EXPANDED}'
    import os
    assert 'PI_TEST_LOCAL_KEY' not in os.environ
    assert 'UNRELATED_TEST_KEY' not in os.environ
    public = json.dumps(core.status(config))
    assert 'literal-${NOT_EXPANDED}' not in public and 'private-other' not in public
    monkeypatch.setenv('PI_TEST_LOCAL_KEY', 'environment-wins')
    assert core._api_key(config) == 'environment-wins'


def test_dotenv_cannot_be_the_processor_state_database(config):
    path = Path(config['_config_path'])
    original = path.read_text(encoding='utf-8')
    path.write_text(original.replace('state_file="state.db"', 'state_file="keys.env"')
                    .replace('api_key_file=""', 'api_key_file=""\nenv_file="keys.env"'), encoding='utf-8')
    with pytest.raises(ValueError, match='separate|credential|env'):
        core.load_config(path)


def test_time_compatibility_mode_also_honors_conversation_char_limit(config):
    config['context'].update(grouping='time', max_conversation_chars=1000)
    with pytest.raises(ValueError, match='max_conversation_chars|片段'):
        groups(config, selected('a', 100, text='X' * 1500))


@pytest.mark.parametrize('grouping', ['sql', 'time'])
def test_vision_capability_change_invalidates_previously_processed_group(config, grouping):
    config['context']['grouping'] = grouping
    records = [selected('a', 100)]
    before = groups(config, *records)[0]['fingerprint']
    config['model']['vision'] = True
    after = groups(config, *records)[0]['fingerprint']
    assert before != after
