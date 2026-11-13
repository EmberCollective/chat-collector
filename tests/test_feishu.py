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
        from lark_oapi.api.im.v1 import PatchMessageResponse
        calls.append(('patch', req))
        return PatchMessageResponse({'code': 0})
    def upload(req):
        calls.append(('upload', req.request_body))
        if fail_file:
            return NS(success=lambda: False, code=99991672, data=None)
        assert req.request_body.file.read()
        return NS(success=lambda: True, data=NS(file_key='file_key'))
    client = NS(im=NS(v1=NS(message=NS(create=create, patch=patch), file=NS(create=upload))))
    monkeypatch.setattr(feishu, '_client', lambda *args: client)
    return calls


def test_listener_automatically_selects_enabled_custom_target_and_same_app_groups(case, monkeypatch):
    config, target, _ = case
    first = dict(target, id='my-team')
    monkeypatch.setenv('SAME_APP_ALIAS', 'cli_test')
    config['targets'] = [dict(target, id='disabled', enabled=False, app_id_env='UNSET_APP'), first,
                         dict(target, id='other-group', recipient_id='oc_other', app_id_env='SAME_APP_ALIAS')]
    assert feishu.listener_target(config) == first
    calls = []
    monkeypatch.setattr('messaging.config.load_config', lambda path: config)
    monkeypatch.setattr(feishu, 'listen', lambda config, target, **kw: calls.append(target['id']))
    assert feishu.main(['listen', '--config', 'synthetic.toml']) == 0
    assert calls == ['my-team']


def test_listener_rejects_ambiguous_apps_but_allows_explicit_target(case, monkeypatch, capsys):
    config, target, _ = case
    monkeypatch.setenv('OTHER_APP', 'cli_other')
    config['targets'] = [target, dict(target, id='other-app', app_id_env='OTHER_APP')]
    monkeypatch.setattr('messaging.config.load_config', lambda path: config)
    calls = []
    monkeypatch.setattr(feishu, 'listen', lambda config, target, **kw: calls.append(target['id']))
    assert feishu.main(['listen', '--config', 'synthetic.toml']) == 1
    assert 'feishu_listener_multiple_apps_not_supported' in capsys.readouterr().out
    assert calls == []
    assert feishu.main(['listen', '--config', 'synthetic.toml', '--target', 'other-app']) == 0
    assert calls == ['other-app']


def callback(envelope, card, action='complete', priority='P2'):
    columns = card['body']['elements'][-1]['columns']
    index = 1 if action in {'snooze', 'set_priority'} else 0
    value = columns[index]['elements'][0]['behaviors'][0]['value']
    return {'header': {'app_id': 'cli_test', 'event_type': 'card.action.trigger', 'event_id': 'evt1'},
            'event': {'operator': {'open_id': 'ou_me'}, 'action': {'tag': 'select_static' if action == 'set_priority' else 'button', 'value': value,
                      **({'option': priority} if action == 'set_priority' else {})},
                      'context': {'open_message_id': 'om_1', 'open_chat_id': 'oc_group'}}}


def test_priority_selector_and_authenticated_option_callback(case, monkeypatch):
    config, target, envelope = case
    calls = fake_client(monkeypatch)
    feishu.deliver_app(target, envelope, config)
    card = json.loads(calls[0][1].content)
    selector = card['body']['elements'][-1]['columns'][1]['elements'][0]
    assert selector['tag'] == 'select_static'
    assert selector['placeholder']['content'] == '调整优先级'
    assert [item['value'] for item in selector['options']] == ['P0', 'P1', 'P2', 'P3', 'P4']
    assert '稍后提醒' not in json.dumps(card, ensure_ascii=False)
    queued = []
    enqueue = lambda *args, **kwargs: queued.append((args, kwargs)) or {'status': 'queued'}
    event = callback(envelope, card, 'set_priority', 'P4')
    feishu.handle_callback(config, target, event, enqueue=enqueue)
    feishu.handle_callback(config, target, event, enqueue=enqueue)
    assert queued[0] == queued[1]
    assert queued[0][0][4] == 'set_priority'
    assert queued[0][1] == {'expected_revision': 1, 'priority': 'P4'}
    event['event']['action']['option'] = 'P0'
    feishu.handle_callback(config, target, event, enqueue=enqueue)
    assert queued[-1][0][2] != queued[0][0][2]
    for bad in ('P9', None, {}, True):
        event['event']['action']['option'] = bad
        with pytest.raises(ValueError, match='invalid_priority'):
            feishu.handle_callback(config, target, event, enqueue=enqueue)
    event = callback(envelope, card)
    event['event']['action'].update(tag='select_static', option='P0')
    with pytest.raises(ValueError, match='invalid_action'):
        feishu.handle_callback(config, target, event, enqueue=enqueue)


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


def test_synthetic_card_callback_is_authenticated_and_never_queues_task(case, monkeypatch):
    config, target, envelope = case
    calls = fake_client(monkeypatch)
    with feishu._state(config) as conn:
        conn.execute('INSERT INTO feishu_tests(delivery_id,created_at) VALUES(?,?)', (envelope['delivery_id'], 1))
    feishu.deliver_app(target, envelope, config)
    event = callback(envelope, json.loads(calls[0][1].content))
    enqueue = lambda *a, **k: pytest.fail('synthetic must not enter task queue')
    result = feishu.handle_callback(config, target, event, enqueue=enqueue)
    assert result == {'status': 'test_received', 'test_id': envelope['delivery_id'], 'synthetic': True}
    with feishu._state(config) as conn:
        saved = conn.execute('SELECT * FROM feishu_tests').fetchone()
        assert saved['clicked_at'] and saved['action'] == 'complete'
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='feedback_requests'").fetchone()
    feishu.handle_callback(config, target, event, enqueue=enqueue)
    event['event']['operator']['open_id'] = 'ou_intruder'
    with pytest.raises(ValueError, match='not_authorized'):
        feishu.handle_callback(config, target, event, enqueue=enqueue)


def test_same_app_multi_group_dispatch_uses_saved_target_and_rejects_cross_app(case, monkeypatch):
    config, target, envelope = case
    other = dict(target, id='second-group', recipient_id='oc_second')
    config['targets'] = [target, other]
    calls = fake_client(monkeypatch)
    with feishu._state(config) as conn:
        conn.execute('INSERT INTO feishu_tests(delivery_id,created_at) VALUES(?,?)', (envelope['delivery_id'], 1))
    feishu.deliver_app(other, envelope, config)
    event = callback(envelope, json.loads(calls[0][1].content))
    event['event']['context']['open_chat_id'] = 'oc_second'
    assert feishu.dispatch_callback(config, 'cli_test', event)['status'] == 'test_received'
    with pytest.raises(ValueError, match='not_authorized'):
        feishu.dispatch_callback(config, 'cli_other', event)
    event['event']['context']['open_chat_id'] = 'oc_group'
    with pytest.raises(ValueError, match='expired'):
        feishu.dispatch_callback(config, 'cli_test', event)
    event['event']['context']['open_chat_id'] = 'oc_second'
    other['enabled'] = False
    with pytest.raises(ValueError, match='disabled'):
        feishu.dispatch_callback(config, 'cli_test', event)


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


def test_corrected_filename_reuploads_once_with_a_new_delivery_identity(case, monkeypatch):
    config, target, envelope = case
    add_file(case)
    resource = envelope['task']['resources'][0]
    resource['name'] = '附件'
    calls = fake_client(monkeypatch)
    feishu.deliver_app(target, envelope, config)
    envelope['delivery_id'] = 'corrected-name'
    resource['name'] = '图片-1.jpg'
    feishu.deliver_app(target, envelope, config)
    assert [c[1].file_name for c in calls if c[0] == 'upload'] == ['附件', '图片-1.jpg']
    messages = [c[1] for c in calls if c[0] == 'create' and c[1].msg_type == 'file']
    assert len(messages) == 2 and messages[0].uuid != messages[1].uuid
    envelope['delivery_id'] = 'later-reminder'
    envelope['kind'] = 'reminder'
    feishu.deliver_app(target, envelope, config)
    assert len([c for c in calls if c[0] == 'upload']) == 2


def test_legacy_file_cache_migration_and_unknown_outcome_not_bypassed(case, monkeypatch):
    import sqlite3
    config, target, envelope = case
    with sqlite3.connect(config['feedback']['state_file']) as conn:
        conn.executescript(feishu.SCHEMA.replace(',file_name TEXT', ''))
    add_file(case)
    calls = fake_client(monkeypatch)
    feishu.deliver_app(target, envelope, config)
    with feishu._state(config) as conn:
        assert 'file_name' in {r[1] for r in conn.execute('PRAGMA table_info(feishu_files)')}
        conn.execute("UPDATE feishu_files SET status='unknown'")
    envelope['task']['resources'][0]['name'] = 'renamed.pdf'
    envelope['delivery_id'] = 'renamed'
    with pytest.raises(DeliveryUncertain, match='material_delivery_unknown'):
        feishu.deliver_app(target, envelope, config)
    assert len([c for c in calls if c[0] == 'upload']) == 1


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
    assert card['body']['elements'][-1]['tag'] == 'column_set'


def test_notes_are_displayed_without_a_review_gate(case):
    task = case[2]['task']
    task.update(needs_review=False, notes=['负责人未说明'], review_reasons=['旧字段内容'])
    card = feishu.card_content(task, {'complete': 'opaque'})
    encoded = json.dumps(card, ensure_ascii=False)
    assert '说明：负责人未说明' in encoded and '旧字段内容' not in encoded
    assert '需要确认' not in encoded and 'opaque' in encoded


def test_sources_collapsed_grouped_and_escaped_without_losing_entries(case):
    task = case[2]['task']
    task.update(notes=['<at id=all></at> 补充说明'],
                sources=[{'ref': 'm1', 'platform': 'wechat', 'conversation': '项目群', 'sender': '<at id=all>', 'time': '2026-11-13T10:20:31+08:00', 'text': '请提交报告\n第二行 <at id=all>'},
                         {'ref': 'm2', 'platform': 'wechat', 'conversation': '项目群', 'sender': '李四', 'time': '2026-11-13T10:21:00+08:00'},
                         {'ref': 'm3', 'platform': 'qq', 'conversation': '**伪造群**', 'sender': '王五'}])
    card = feishu.card_content(task, {'complete': 'opaque', 'snooze': 'other'})
    panels = [e for e in card['body']['elements'] if e['tag'] == 'collapsible_panel']
    assert len(panels) == 2 and all(p['expanded'] is False for p in panels)
    assert panels[1]['header']['title']['content'] == '信息来源 · 3 条'
    text = panels[1]['elements'][0]['content']
    assert text.count('项目群') == 1 and all(name in text for name in ('李四', '王五'))
    assert all(ref not in text for ref in ('m1', 'm2', 'm3'))
    assert '微信' in text and 'QQ' in text
    assert '<at ' not in json.dumps(card, ensure_ascii=False) and '**伪造群**' not in text
    assert '2026-11-13 10:20:31（UTC+08:00）' in text
    assert '2026-11-13 10:21（UTC+08:00）' in text
    assert '请提交报告\n第二行' in text
    assert card['body']['elements'][-1]['columns'][0]['elements'][0]['behaviors'][0]['value'] == {'token': 'opaque'}


def test_long_source_original_uses_archive_hint_and_stays_within_card_limit(case):
    _, _, envelope = case
    task = envelope['task']
    task['sources'] = [{'platform': 'wechat', 'text': '<' * 20000}]
    card = feishu.card_content(task, {})
    encoded = json.dumps(card, ensure_ascii=False)
    assert '来源原文.txt' in encoded and len(encoded.encode()) <= 28000


@pytest.mark.parametrize('offset,label', [(86400, '截止前 1 天提醒'), (21600, '截止前 6 小时提醒'), (1800, '截止前 30 分钟提醒'), (0, '已到截止时间')])
def test_deadline_reminder_card_shows_node_and_date_only_policy(case, offset, label):
    config, target, envelope = case
    envelope.update(kind='reminder', reminder={'deadline_at': 200000, 'due_at': 200000 - offset, 'offset_seconds': offset})
    assert feishu.public_envelope(envelope)['reminder']['offset_seconds'] == offset
    card = feishu.card_content(envelope['task'], {}, reminder=envelope['reminder'])
    encoded = json.dumps(card, ensure_ascii=False)
    assert label in encoded and '23:59:59' in encoded


def test_digest_card_has_no_fake_task_actions_and_delivers_complete_large_schedule(case, monkeypatch):
    from messaging.channels import deliver
    config, target, _ = case
    calls = fake_client(monkeypatch)
    envelope = {'schema_version': 1, 'delivery_id': 'daily-test', 'kind': 'daily_digest',
                'digest': {'date': '2026-11-14', 'timezone': 'Asia/Shanghai', 'generated_at': '2026-11-14T08:00:00+08:00',
                           'items': [{'task_id': str(i), 'title': '待办' + str(i), 'priority': 'P1', 'deadline': '2026-11-15',
                                      'notifications': [{'time': '2026-11-14T18:00:00+08:00', 'kind': 'deadline', 'offset_seconds': 21600}]} for i in range(40)]}}
    result = deliver(target, envelope, config)
    assert result['remote_id']
    with feishu._state(config) as db:
        card = json.loads(db.execute('SELECT card FROM feishu_cards WHERE delivery_id=?', ('daily-test',)).fetchone()[0])
        assert db.execute('SELECT count(*) FROM feishu_actions').fetchone()[0] == 0
    assert '[早报]' in card['header']['title']['content']
    assert '今日通知安排.txt' in json.dumps(card, ensure_ascii=False)
    assert any(kind == 'upload' for kind, _ in calls)


@pytest.mark.parametrize(('raw', 'expected'), [
    ('2026-11-13T10:20:00-05:00', '2026-11-13 10:20（UTC-05:00）'),
    ('2026-11-13T10:20:00Z', '2026-11-13 10:20（UTC+00:00）'),
    ('2026-11-13T10:20:00', '2026-11-13 10:20'),
    ('2026-11-13', '2026-11-13'), ('下周一', '下周一'),
])
def test_display_time_preserves_offset_without_guessing(raw, expected):
    assert feishu._display_time(raw) == expected


@pytest.mark.parametrize(('status', 'label'), [('open', '待办'), ('completed', '已完成'), ('cancelled', '已取消'), ('superseded', '已替代')])
def test_card_main_details_localized_and_all_steps_preserved(case, status, label):
    task = case[2]['task']
    task['status'] = status
    task['time'] = {'start': '2026-11-13T09:00:00+08:00', 'deadline': '2026-11-13T12:00:00+08:00'}
    card = feishu.card_content(task, {})
    main = card['body']['elements'][0]['content']
    sections = card['body']['elements']
    details = [item['content'] for item in sections[1:] if item['tag'] == 'markdown']
    assert all(step in main for step in task['action_steps'])
    assert '**状态**\n' + label in details
    assert any(item.startswith('**开始**\n2026-11-13 09:00') for item in details)
    assert any(item.startswith('**截止**\n2026-11-13 12:00') for item in details)
    assert 'background_style' not in json.dumps(card)
    for index, item in enumerate(sections[1:], 1):
        if item['tag'] == 'markdown':
            assert sections[index - 1]['tag'] == 'hr'


@pytest.mark.parametrize(('priority', 'color'), [('P0', 'red'), ('P1', 'orange'), ('P2', 'blue'), ('P3', 'green'), ('P4', 'grey'), ('unexpected', 'grey')])
def test_priority_header_colors(case, priority, color):
    task = case[2]['task']
    task['priority'] = priority
    assert feishu.card_content(task, {})['header']['template'] == color


@pytest.mark.parametrize(('state', 'color', 'prefix'), [(None, 'red', 'ERROR'), ('failure', 'red', 'ERROR'), ('recovery', 'green', '已恢复')])
def test_alert_header_distinguishes_failure_and_recovery(case, state, color, prefix):
    task = case[2]['task']
    if state:
        task['alert_state'] = state
    card = feishu.card_content(task, {'complete': 'must-not-display'}, alert=True)
    assert card['header']['template'] == color
    assert card['header']['title']['content'].startswith('[' + prefix + ']')
    assert 'must-not-display' not in json.dumps(card)


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


@pytest.mark.parametrize('partial', [False, True])
def test_digest_retry_refreshes_prepared_card_or_patches_card_with_failed_material(case, monkeypatch, tmp_path, partial):
    from messaging import core
    from messaging.package import build_package
    config = delivery_config(case, tmp_path, monkeypatch)
    slot = core.dt.datetime(2026, 12, 1, 8, tzinfo=core.ZoneInfo('Asia/Shanghai')).timestamp()
    config['daily_digest']['enabled'] = True
    config['initial_notifications']['P1'] = False
    monkeypatch.setattr(core, '_package', lambda task, config, prepare=False:
                        build_package(task, config, prepare=prepare))
    count = 30 if partial else 1
    def batch(name, revision, status):
        events = []
        for index in range(count):
            task = dict(case[2]['task'], task_id=f't{index}', revision=revision, status=status,
                        title=f'Stale task {index}')
            events.append(dict(schema_version=1, operation='upsert', event_id=f'e{index}-{revision}',
                               task_id=task['task_id'], revision=revision, task=task))
        (Path(config['input']['batches_dir']) / name).write_text('\n'.join(json.dumps(e) for e in events), encoding='utf-8')
    (Path(config['input']['batches_dir']) / 'batch.jsonl').unlink()
    batch('first.jsonl', 1, 'open')
    calls = fake_client(monkeypatch)
    if partial:
        monkeypatch.setattr(feishu, '_file_delivery', lambda *a: {'status': 'failed', 'error': 'temporary'})
    else:
        def rejected(req):
            calls.append(('rejected', req.request_body))
            return NS(success=lambda: False, code=999, data=None)
        original_client = feishu._client(config['targets'][0], config)
        original_create = original_client.im.v1.message.create
        original_client.im.v1.message.create = rejected
    assert core.run_round(config, now=slot)['retry'] == 1
    with feishu._state(config) as conn:
        before = dict(conn.execute('SELECT * FROM feishu_cards').fetchone())
    assert before['status'] == ('sent' if partial else 'prepared')
    batch('completed.jsonl', 2, 'completed')
    if not partial:
        original_client.im.v1.message.create = original_create
    assert core.run_round(config, now=slot + 61)['sent'] == 1
    with feishu._state(config) as conn:
        after = dict(conn.execute('SELECT * FROM feishu_cards').fetchone())
    assert after['delivery_id'] == before['delivery_id'] and after['status'] == 'sent'
    assert 'Stale task' not in json.dumps(json.loads(after['card']), ensure_ascii=False)
    if partial:
        assert after['message_id'] == before['message_id']
        assert [kind for kind, _ in calls] == ['create', 'patch']
        assert json.loads(calls[-1][1].request_body.content) == json.loads(after['card'])
    else:
        assert [kind for kind, _ in calls] == ['rejected', 'create']
        assert calls[0][1].uuid == calls[1][1].uuid
        assert json.loads(calls[-1][1].content) == json.loads(after['card'])
    assert core.run_round(config, now=slot + 62)['sent'] == 0


def test_digest_unknown_card_cache_is_not_refreshed_or_resent(case, monkeypatch):
    from messaging.channels import deliver
    config, target, envelope = case
    envelope.update(kind='daily_digest', digest={'date': '2026-12-01', 'timezone': 'Asia/Shanghai',
                    'generated_at': '2026-12-01T08:00:00+08:00', 'items': []})
    calls = fake_client(monkeypatch, fail_send=True)
    with pytest.raises(DeliveryUncertain):
        deliver(target, envelope, config)
    with feishu._state(config) as conn:
        before = dict(conn.execute('SELECT * FROM feishu_cards').fetchone())
    envelope['digest']['generated_at'] = '2026-12-01T08:01:00+08:00'
    with pytest.raises(DeliveryUncertain, match='prior_outcome_unknown'):
        deliver(target, envelope, config)
    with feishu._state(config) as conn:
        assert dict(conn.execute('SELECT * FROM feishu_cards').fetchone()) == before
    assert len(calls) == 1


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


def test_channel_connection_keeps_legacy_tokens_receipts_and_feedback_rows(case, monkeypatch):
    from messaging import feedback
    config = case[0]
    with feedback._connect(config, write=True) as conn:
        conn.execute("INSERT INTO feedback_requests(request_id,task_id,request,acknowledgement,status,created_at,updated_at) VALUES('q1','t1','{}','{}','queued',1,1)")
    with feishu._state(config) as conn:
        conn.execute("INSERT INTO feishu_cards VALUES('d1','scope','target','t1',3,'g1','om_old',1,'sent','{}')")
        conn.execute("INSERT INTO feishu_actions VALUES('old-token','d1','complete')")
        conn.execute("INSERT INTO feishu_heads VALUES('scope','t1','g1')")
        conn.execute("INSERT INTO feishu_files VALUES('scope','t1','sha','file_key','om_file','unknown',NULL,'original.pdf')")
        expected = {name: [tuple(row) for row in conn.execute('SELECT * FROM ' + name)]
                    for name in ('feishu_cards', 'feishu_actions', 'feishu_heads', 'feishu_files', 'feedback_requests')}

    def forbidden(*args, **kwargs):
        raise AssertionError('channel connection must not call the feedback connector')

    monkeypatch.setattr(feedback, '_connect', forbidden)
    with feishu._state(config) as conn:
        assert conn.execute('PRAGMA database_list').fetchone()['file'] == str(Path(config['feedback']['state_file']).resolve())
        for name, rows in expected.items():
            assert [tuple(row) for row in conn.execute('SELECT * FROM ' + name)] == rows
