from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from messaging import feishu
from messaging.channels import DeliveryRejected, DeliveryUncertain


@pytest.fixture
def case(tmp_path, monkeypatch):
    monkeypatch.setenv('TEST_APP_ID', 'cli_test')
    monkeypatch.setenv('TEST_APP_SECRET', 'never-output-this')
    config = {'enabled': True, 'storage': {'state_file': str(tmp_path / 'state.db'), 'assets_dir': str(tmp_path / 'assets')},
              'feedback': {'state_file': str(tmp_path / 'feedback.db')}}
    target = {'id': 'feishu-assistant', 'kind': 'feishu_app', 'enabled': True, 'recipient_id': 'oc_group',
              'receive_id_type': 'chat_id', 'app_id_env': 'TEST_APP_ID', 'app_secret_env': 'TEST_APP_SECRET',
              'allowed_operator_ids': ['ou_me']}
    envelope = {'delivery_id': 'd1', 'kind': 'upsert', 'task': {'task_id': 't1', 'revision': 1,
        'title': 'Submit report', 'event': 'Send the report', 'priority': 'P1', 'status': 'open',
        'time': {'deadline': '2026-12-01'}, 'people': [], 'resources': [], 'sources': [],
        'action_steps': ['Review the report', 'Send the report']}}
    return config, target, envelope


def fake_client(monkeypatch, *, fail_send=False, fail_file=False):
    calls = []
    def create(req):
        calls.append(('create', req.request_body))
        if fail_send:
            raise TimeoutError('SECRET endpoint')
        return NS(success=lambda: True, data=NS(message_id='om_' + str(len(calls))))
    def patch(req):
        calls.append(('patch', req))
        return NS(success=lambda: True, data=None)
    def upload(req):
        calls.append(('upload', req.request_body))
        if fail_file:
            return NS(success=lambda: False, code=99991672, data=None)
        assert req.request_body.file.read()
        return NS(success=lambda: True, data=NS(file_key='file_key'))
    client = NS(im=NS(v1=NS(message=NS(create=create, patch=patch), file=NS(create=upload))))
    monkeypatch.setattr(feishu, '_client', lambda *args: client)
    return calls


def callback(envelope, card, action='complete'):
    columns = card['body']['elements'][-1]['columns']
    index = 1 if action == 'snooze' else 0
    value = columns[index]['elements'][0]['behaviors'][0]['value']
    return {'header': {'app_id': 'cli_test', 'event_type': 'card.action.trigger', 'event_id': 'evt1'},
            'event': {'operator': {'open_id': 'ou_me'}, 'action': {'tag': 'button', 'value': value},
                      'context': {'open_message_id': 'om_1', 'open_chat_id': 'oc_group'}}}


def test_app_card_create_replay_update_and_old_tokens_expire(case, monkeypatch):
    config, target, envelope = case
    calls = fake_client(monkeypatch)
    receipt = feishu.deliver_app(target, envelope, config)
    card = json.loads(calls[0][1].content)
    event = callback(envelope, card)
    queued = []
    enqueue = lambda *args, **kwargs: queued.append((args, kwargs)) or {'status': 'queued'}
    assert feishu.handle_callback(config, target, event, enqueue=enqueue)['status'] == 'queued'
    assert queued[0][0][3:5] == ('t1', 'complete')
    assert queued[0][1]['expected_revision'] == 1
    assert queued[0][0][5] == {'platform': 'feishu', 'user_id': 'ou_me', 'source': 'card'}
    feishu.handle_callback(config, target, event, enqueue=enqueue)
    assert queued[0][0][2] == queued[1][0][2]
    assert feishu.deliver_app(target, envelope, config)['message_id'] == receipt['message_id']
    assert len(calls) == 1
    envelope['delivery_id'] = 'd2'; envelope['task']['revision'] = 2
    envelope['task']['status'] = 'completed'
    updated = feishu.deliver_app(target, envelope, config, receipt)
    assert calls[-1][0] == 'patch' and updated['message_id'] == receipt['message_id']
    with pytest.raises(ValueError, match='expired'):
        feishu.handle_callback(config, target, event, enqueue=enqueue)


@pytest.mark.parametrize('mutation', ['operator', 'message', 'chat', 'token', 'app', 'extra_action'])
def test_unauthorized_or_tampered_callbacks_never_queue(case, monkeypatch, mutation):
    config, target, envelope = case
    calls = fake_client(monkeypatch)
    feishu.deliver_app(target, envelope, config)
    event = callback(envelope, json.loads(calls[0][1].content))
    if mutation == 'operator': event['event']['operator']['open_id'] = 'ou_stranger'
    if mutation == 'message': event['event']['context']['open_message_id'] = 'om_forwarded'
    if mutation == 'chat': event['event']['context']['open_chat_id'] = 'oc_other'
    if mutation == 'token': event['event']['action']['value']['token'] = 'made-up'
    if mutation == 'app': event['header']['app_id'] = 'cli_other'
    if mutation == 'extra_action': event['event']['action']['value']['action'] = 'reopen'
    with pytest.raises(ValueError):
        feishu.handle_callback(config, target, event, enqueue=lambda *a, **k: pytest.fail('must not enqueue'))


def test_timeout_does_not_repeat_card_send_or_expose_error(case, monkeypatch):
    config, target, envelope = case
    calls = fake_client(monkeypatch, fail_send=True)
    with pytest.raises(DeliveryUncertain, match='outcome_unknown') as error:
        feishu.deliver_app(target, envelope, config)
    assert 'SECRET' not in str(error.value)
    with pytest.raises(DeliveryUncertain, match='prior_outcome_unknown'):
        feishu.deliver_app(target, envelope, config)
    assert len(calls) == 1


def test_alert_has_no_action_tokens(case, monkeypatch):
    config, target, envelope = case
    envelope['kind'] = 'alert'
    calls = fake_client(monkeypatch)
    feishu.deliver_app(target, envelope, config)
    card = json.loads(calls[0][1].content)
    assert 'callback' not in json.dumps(card)
    with feishu._state(config) as conn:
        assert conn.execute('SELECT count(*) FROM feishu_actions').fetchone()[0] == 0


def add_file(case):
    config, target, envelope = case
    root = Path(config['storage']['assets_dir']); root.mkdir()
    path = root / 'source.pdf'; path.write_bytes(b'%PDF-local-fixture')
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    envelope['task']['resources'] = [{'id': sha, 'name': 'report.pdf', 'type': 'file', 'status': 'ready',
                                      'sha256': sha, 'local_path': str(path)}]


def test_file_send_is_persisted_and_not_repeated_for_reminders(case, monkeypatch):
    config, target, envelope = case
    add_file(case)
    calls = fake_client(monkeypatch)
    result = feishu.deliver_app(target, envelope, config)
    assert result['files'][0]['status'] == 'sent'
    assert len([c for c in calls if c[0] == 'upload']) == 1
    envelope['delivery_id'] = 'reminder1'; envelope['kind'] = 'reminder'
    feishu.deliver_app(target, envelope, config)
    assert len([c for c in calls if c[0] == 'upload']) == 1
    assert len([c for c in calls if c[0] == 'create' and c[1].msg_type == 'file']) == 1
    assert all(len(c[1].uuid) <= 50 for c in calls if c[0] == 'create')


def test_file_failure_preserves_successful_card_and_marks_material(case, monkeypatch):
    config, target, envelope = case
    add_file(case)
    calls = fake_client(monkeypatch, fail_file=True)
    with pytest.raises(DeliveryRejected, match='material_delivery_failed') as error:
        feishu.deliver_app(target, envelope, config)
    result = error.value.receipt
    assert result['message_id'] == 'om_1'
    assert result['files'][0]['status'] == 'failed'
    assert not any(c[0] == 'create' and c[1].msg_type == 'file' for c in calls)


def test_card_escape_and_layout(case):
    task = case[2]['task']
    task['action_steps'] = ['<at id=all></at> **fake**']
    card = feishu.card_content(task, {'complete': 'opaque', 'snooze': 'other'})
    assert card['schema'] == '2.0' and not card['config']['enable_forward']
    assert '<at ' not in json.dumps(card)
    assert 2 <= len(card['body']['elements']) <= 5


def test_notes_are_displayed_without_a_review_gate(case):
    task = case[2]['task']
    task.update(needs_review=False, notes=['负责人未说明'], review_reasons=['旧字段内容'])
    card = feishu.card_content(task, {'complete': 'opaque'})
    encoded = json.dumps(card, ensure_ascii=False)
    assert '说明：负责人未说明' in encoded and '旧字段内容' not in encoded
    assert '需要确认' not in encoded and 'opaque' in encoded


def test_check_is_local_and_never_returns_credentials(case, monkeypatch):
    monkeypatch.setattr(feishu, '_client', lambda *a: pytest.fail('no network'))
    result = feishu.check(case[0], case[1])
    assert result['configured'] and result['network_checked'] is False
    assert 'never-output' not in json.dumps(result)


def test_setup_listener_uses_sdk_and_ignores_callback(case, monkeypatch, tmp_path):
    import lark_oapi as lark
    from lark_oapi.ws.client import loop
    config, target, envelope = case
    config['enabled'] = target['enabled'] = False
    captured = []
    class Builder:
        def register_p2_card_action_trigger(self, callback):
            captured.append(callback); return self
        def build(self): return self
    monkeypatch.setattr(lark.EventDispatcherHandler, 'builder', lambda *a: Builder())
    class WS:
        def __init__(self, *a, **k): self._conn = object(); self._auto_reconnect = True
        async def _disconnect(self): self._conn = None
        def start(self):
            result = captured[0](object())
            assert '连接检查' in result.toast.content
            stop.write_text('stop')
            loop.run_until_complete(__import__('asyncio').sleep(5))
    monkeypatch.setattr(lark.ws, 'Client', WS)
    stop = tmp_path / 'stop'
    feishu.listen(config, target, setup=True, stop_file=stop)
    assert not Path(config['feedback']['state_file']).exists()


def test_read_only_chat_and_members_pagination(case, monkeypatch):
    pages = []
    def listing(req):
        pages.append(req)
        if req.page_token:
            return NS(success=lambda: True, data=NS(items=[NS(name='Second', chat_id='oc_2')], has_more=False))
        return NS(success=lambda: True, data=NS(items=[NS(name='First', chat_id='oc_1')], has_more=True, page_token='next'))
    def members(req):
        assert req.member_id_type == 'open_id' and req.chat_id == 'oc_1'
        return NS(success=lambda: True, data=NS(items=[NS(name='Me', member_id='ou_me')], has_more=False))
    client = NS(im=NS(v1=NS(chat=NS(list=listing), chat_members=NS(get=members))))
    monkeypatch.setattr(feishu, '_client', lambda *a: client)
    assert len(feishu.list_chats(case[0], case[1])['items']) == 2
    assert feishu.list_chats(case[0], case[1], chat_id='oc_1')['items'][0]['open_id'] == 'ou_me'


def delivery_config(case, tmp_path, monkeypatch):
    from messaging import core, load_config
    path = tmp_path / 'message.toml'
    path.write_text('''enabled=true
[input]
batches_dir="incoming"
[storage]
state_file="state.db"
assets_dir="assets"
[schedule]
retry_base_seconds=60
max_attempts=2
''', encoding='utf-8')
    config = load_config(path)
    config['feedback'] = {'state_file': str(tmp_path / 'feedback.db'), 'enabled': False}
    config['targets'] = [case[1]]
    config['reminders'] = {p: 0 for p in ('P0', 'P1', 'P2', 'P3', 'P4')}
    incoming = Path(config['input']['batches_dir']); incoming.mkdir()
    task = deepcopy(case[2]['task'])
    event = {'schema_version': 1, 'operation': 'upsert', 'event_id': 'event1',
             'task_id': task['task_id'], 'revision': task['revision'], 'task': task}
    (incoming / 'batch.jsonl').write_text(json.dumps(event), encoding='utf-8')
    monkeypatch.setattr(core, '_package', lambda task, config, prepare=False: deepcopy(case[2]['task']))
    return config


def test_partial_file_failure_retries_only_missing_file_and_keeps_receipt(case, monkeypatch, tmp_path):
    from messaging import core
    add_file(case)
    config = delivery_config(case, tmp_path, monkeypatch)
    first_calls = fake_client(monkeypatch, fail_file=True)
    first = core.run_round(config, now=100)
    assert first['retry'] == 1 and first['sent'] == 0
    with core._connect(config) as conn:
        row = conn.execute('SELECT status,receipt FROM deliveries').fetchone()
        receipt = json.loads(row['receipt'])
        assert row['status'] == 'retry' and receipt['message_id'] == 'om_1'
        assert receipt['files'][0]['status'] == 'failed'
    assert core.run_round(config, now=159)['sent'] == 0
    second_calls = fake_client(monkeypatch)
    second = core.run_round(config, now=160)
    assert second['sent'] == 1 and second['retry'] == 0
    assert len([c for c in first_calls + second_calls if c[0] == 'create' and c[1].msg_type == 'interactive']) == 1
    assert len([c for c in first_calls + second_calls if c[0] == 'create' and c[1].msg_type == 'file']) == 1


def test_partial_file_failure_stops_after_configured_attempt_limit(case, monkeypatch, tmp_path):
    from messaging import core
    add_file(case)
    config = delivery_config(case, tmp_path, monkeypatch)
    calls = fake_client(monkeypatch, fail_file=True)
    assert core.run_round(config, now=100)['retry'] == 1
    assert core.run_round(config, now=160)['failed'] == 1
    count = len(calls)
    assert core.run_round(config, now=10000)['sent'] == 0 and len(calls) == count
    assert core.status(config)['delivery_counts'] == {'failed': 1}


def test_unknown_file_result_blocks_follow_up_and_exposes_unknown_to_core(case, monkeypatch, tmp_path):
    from messaging import core
    add_file(case)
    config = delivery_config(case, tmp_path, monkeypatch)
    calls = fake_client(monkeypatch)
    real_send = feishu._send
    def send(client, target, kind, content, identifier):
        if kind == 'file':
            raise DeliveryUncertain('feishu_app_request_outcome_unknown')
        return real_send(client, target, kind, content, identifier)
    monkeypatch.setattr(feishu, '_send', send)
    result = core.run_round(config, now=100)
    assert result['unknown'] == 1 and result['sent'] == 0
    count = len(calls)
    assert core.run_round(config, now=10000)['sent'] == 0 and len(calls) == count
    with core._connect(config) as conn:
        row = conn.execute('SELECT status,error,receipt FROM deliveries').fetchone()
        assert row['status'] == 'unknown' and row['error'] == 'feishu_material_delivery_unknown'
        assert json.loads(row['receipt'])['files'][0]['status'] == 'unknown'


def test_explicit_retry_releases_unknown_card_without_changing_stable_uuid(case, monkeypatch, tmp_path):
    from messaging import core
    config = delivery_config(case, tmp_path, monkeypatch)
    first = fake_client(monkeypatch, fail_send=True)
    assert core.run_round(config, now=100)['unknown'] == 1
    identifier = core.status(config)['deliveries'][0]['id']
    core.resolve_delivery(config, identifier, 'retry')
    second = fake_client(monkeypatch)
    assert core.run_round(config, now=200)['sent'] == 1
    assert first[0][1].uuid == second[0][1].uuid


@pytest.mark.parametrize('action', ['retry', 'delivered'])
def test_explicit_file_reconciliation_unblocks_existing_receipts(case, monkeypatch, tmp_path, action):
    from messaging import core
    add_file(case)
    config = delivery_config(case, tmp_path, monkeypatch)
    fake_client(monkeypatch)
    real_send = feishu._send
    def uncertain_file(client, target, kind, content, identifier):
        if kind == 'file':
            raise DeliveryUncertain('feishu_app_request_outcome_unknown')
        return real_send(client, target, kind, content, identifier)
    monkeypatch.setattr(feishu, '_send', uncertain_file)
    assert core.run_round(config, now=100)['unknown'] == 1
    identifier = core.status(config)['deliveries'][0]['id']
    core.resolve_delivery(config, identifier, action)
    monkeypatch.setattr(feishu, '_send', real_send)
    calls = fake_client(monkeypatch)
    if action == 'retry':
        assert core.run_round(config, now=200)['sent'] == 1
        assert len(calls) == 1 and calls[0][0] == 'create' and calls[0][1].msg_type == 'file'
    else:
        next_envelope = deepcopy(case[2]); next_envelope['delivery_id'] = 'new-revision'
        next_envelope['task']['revision'] = 2
        result = feishu.deliver_app(case[1], next_envelope, config)
        assert result['files'][0]['status'] == 'sent'
        assert len(calls) == 1 and calls[0][0] == 'patch'


@pytest.mark.parametrize('newer_head', [False, True])
def test_manual_unknown_card_ack_revokes_buttons_without_inventing_message_id(case, monkeypatch, tmp_path, newer_head):
    from messaging import core
    config = delivery_config(case, tmp_path, monkeypatch)
    calls = fake_client(monkeypatch, fail_send=True)
    assert core.run_round(config, now=100)['unknown'] == 1
    old_event = callback(case[2], json.loads(calls[0][1].content))
    identifier = core.status(config)['deliveries'][0]['id']
    if newer_head:
        with feishu._state(config) as conn:
            conn.execute("INSERT OR REPLACE INTO feishu_heads(scope,task_id,generation) SELECT scope,task_id,'newer-generation' FROM feishu_cards WHERE delivery_id=?", (identifier,))
    core.resolve_delivery(config, identifier, 'delivered')
    with feishu._state(config) as conn:
        heads = conn.execute('SELECT generation FROM feishu_heads').fetchall()
        assert [row['generation'] for row in heads] == (['newer-generation'] if newer_head else [])
    with core._connect(config) as conn:
        row = conn.execute('SELECT receipt,body FROM deliveries').fetchone()
        receipt, original = json.loads(row['receipt']), json.loads(row['body'])
        assert receipt['manual_acknowledged'] and receipt['callbacks_unavailable']
        assert 'message_id' not in receipt
    with pytest.raises(ValueError, match='expired'):
        feishu.handle_callback(config, case[1], old_event, enqueue=lambda *a, **k: pytest.fail('old token'))
    later = fake_client(monkeypatch)
    assert feishu.deliver_app(case[1], original, config)['callbacks_unavailable']
    assert later == []
    original['delivery_id'] = 'future'; original['task']['revision'] = 2
    assert feishu.deliver_app(case[1], original, config)['message_id']
    assert len(later) == 1


def test_reconciliation_refuses_a_different_app_or_chat_scope(case, monkeypatch, tmp_path):
    from messaging import core
    config = delivery_config(case, tmp_path, monkeypatch)
    fake_client(monkeypatch, fail_send=True)
    core.run_round(config, now=100)
    identifier = core.status(config)['deliveries'][0]['id']
    config['targets'][0]['recipient_id'] = 'oc_other'
    with pytest.raises(ValueError, match='scope_mismatch'):
        core.resolve_delivery(config, identifier, 'retry')
    assert core.status(config)['delivery_counts'] == {'unknown': 1}


def test_feishu_state_refuses_other_business_database_without_mutation(case):
    import sqlite3
    config = case[0]
    path = Path(config['feedback']['state_file'])
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE tasks(id TEXT)')
    original = path.read_bytes()
    with pytest.raises(ValueError, match='wrong_database'):
        feishu._state(config)
    assert path.read_bytes() == original
