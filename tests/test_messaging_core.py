import json
from pathlib import Path
import sqlite3

import pytest

from messaging import add_result, load_config, preview, resolve_delivery, run_round, set_task_state, status, sync, task_package
from messaging import core, channels


@pytest.fixture
def config(tmp_path, monkeypatch):
    path = tmp_path / 'messaging.toml'
    path.write_text('''enabled = true
[input]
batches_dir = "incoming"
[storage]
state_file = "state.db"
assets_dir = "assets"
[reminders]
P0 = 60
P1 = 120
P2 = 240
P3 = 480
P4 = 0
[[targets]]
id = "notify"
kind = "feishu"
enabled = true
''', encoding='utf-8')
    (tmp_path / 'incoming').mkdir()
    monkeypatch.setattr(core, '_package', lambda task, config, prepare=False: dict(task, resources=[]))
    return load_config(path)


def event(identifier='one', revision=1, *, priority='P0', task_status='open', title='Submit report'):
    return {'schema_version': 1, 'event_id': f'{identifier}-{revision}', 'operation': 'upsert',
            'task_id': identifier, 'revision': revision,
            'task': {'task_id': identifier, 'revision': revision, 'priority': priority, 'status': task_status,
                     'title': title, 'event': 'Submit the report', 'sources': []}}


def batch(config, name, *events):
    path = Path(config['input']['batches_dir']) / f'{name}.jsonl'
    path.write_text(''.join(json.dumps(e) + '\n' for e in events), encoding='utf-8')
    return path


@pytest.fixture
def sent(monkeypatch):
    items = []
    def deliver(target, envelope, config, previous=None):
        items.append((target['id'], envelope, previous))
        return {'remote_id': envelope['delivery_id']}
    monkeypatch.setattr(channels, 'deliver', deliver)
    return items


def test_preview_no_write_no_network_and_sync_never_delivers(config, sent):
    source = batch(config, 'one', event())
    original = source.read_bytes()
    assert preview(config)['pending_events'] == 1
    assert not Path(config['storage']['state_file']).exists()
    assert not Path(config['storage']['assets_dir']).exists()
    assert sync(config)['imported_events'] == 1
    assert sync(config)['imported_events'] == 0
    assert sent == []
    assert source.read_bytes() == original


def test_ingestion_deduplicates_events_and_does_not_roll_back_revision(config):
    batch(config, 'first', event(revision=3), event(revision=1))
    assert sync(config)['imported_events'] == 2
    batch(config, 'second', event(revision=3), event(revision=2))
    assert sync(config)['imported_events'] == 1
    assert preview(config)['tasks'][0]['revision'] == 3


@pytest.mark.parametrize('newline', ['\n', '\r\n'])
def test_unicode_line_separators_preserved_in_task_batches(config, sent, newline):
    title = 'first\u2028second\u2029third\u0085last'
    records = [event('unicode', title=title), event('following')]
    path = Path(config['input']['batches_dir']) / 'unicode.jsonl'
    raw = (newline.join(json.dumps(row, ensure_ascii=False) for row in records) + newline).encode('utf-8')
    path.write_bytes(raw)
    assert sync(config)['imported_events'] == 2
    assert next(task for task in preview(config)['tasks'] if task['task_id'] == 'unicode')['title'] == title
    assert sync(config)['imported_events'] == 0
    assert path.read_bytes() == raw
    assert not sent


@pytest.mark.parametrize('same_id', [True, False])
def test_conflict_does_not_partially_import(config, same_id):
    batch(config, 'one', event())
    sync(config)
    conflicting = event(title='Changed illegally')
    if not same_id:
        conflicting['event_id'] = 'another'
    batch(config, 'two', event('new-task'), conflicting)
    with pytest.raises(ValueError, match='Conflicting'):
        sync(config)
    assert status(config)['tasks'] == 1


def test_changed_immutable_file_rejected(config):
    path = batch(config, 'one', event())
    sync(config)
    path.write_text(json.dumps(event(title='modified batch data')), encoding='utf-8')
    with pytest.raises(ValueError, match='immutable'):
        sync(config)


def test_repeated_rounds_and_intervals_do_not_duplicate_notifications(config, sent):
    batch(config, 'one', event(), event('low', priority='P4'))
    assert run_round(config, now=100)['sent'] == 2
    assert run_round(config, now=100)['sent'] == 0
    assert run_round(config, now=159)['sent'] == 0
    assert run_round(config, now=160)['sent'] == 1
    assert sent[-1][1]['kind'] == 'reminder'
    assert run_round(config, now=160)['sent'] == 0
    assert len({envelope['delivery_id'] for _, envelope, _ in sent}) == len(sent)


def deadline_event(deadline, **kwargs):
    item = event(**kwargs)
    item['task']['time'] = {'deadline': core.dt.datetime.fromtimestamp(deadline, core.dt.timezone.utc).isoformat()}
    return item


@pytest.mark.parametrize('priority,offsets', [('P0', [86400, 21600, 7200, 1800, 0]), ('P1', [21600, 7200, 1800, 0]), ('P2', [7200, 1800, 0]), ('P3', [1800, 0]), ('P4', [0])])
def test_deadline_nodes_are_sent_once_at_each_priority_time(config, sent, priority, offsets):
    deadline = 200000
    batch(config, 'one', deadline_event(deadline, priority=priority))
    assert run_round(config, now=100)['sent'] == 1
    for offset in offsets:
        assert run_round(config, now=deadline - offset - 1)['sent'] == 0
        assert run_round(config, now=deadline - offset)['sent'] == 1
        assert sent[-1][1]['reminder']['offset_seconds'] == offset
        assert run_round(config, now=deadline - offset)['sent'] == 0
    assert run_round(config, now=deadline + 86400 * 10)['sent'] == 0
    assert len(sent) == len(offsets) + 1


def test_late_poll_coalesces_nodes_and_replans_after_deadline_change(config, sent):
    batch(config, 'one', deadline_event(200000))
    run_round(config, now=100)
    assert run_round(config, now=199000)['sent'] == 1
    assert sent[-1][1]['reminder']['offset_seconds'] == 1800
    assert run_round(config, now=199001)['sent'] == 0
    batch(config, 'two', deadline_event(300000, revision=2))
    assert run_round(config, now=199002)['sent'] == 1
    assert run_round(config, now=200000)['sent'] == 0
    assert run_round(config, now=300000 - 86400)['sent'] == 1


def test_completion_stops_deadline_reminders(config, sent):
    batch(config, 'one', deadline_event(200000))
    run_round(config, now=100)
    set_task_state(config, 'one', 'complete')
    run_round(config, now=101)
    count = len(sent)
    assert run_round(config, now=200000)['sent'] == 0
    assert len(sent) == count


def test_later_deadline_node_replaces_failed_older_retry(config, sent, monkeypatch):
    batch(config, 'one', deadline_event(200000))
    run_round(config, now=100)
    deliver = channels.deliver
    monkeypatch.setattr(channels, 'deliver', lambda *a, **kw: (_ for _ in ()).throw(channels.DeliveryRejected('test_rejected')))
    assert run_round(config, now=200000 - 86400)['retry'] == 1
    monkeypatch.setattr(channels, 'deliver', deliver)
    assert run_round(config, now=200000 - 7200)['sent'] == 1
    assert sent[-1][1]['reminder']['offset_seconds'] == 7200
    assert status(config)['delivery_counts']['obsolete'] == 1


def test_date_only_deadline_uses_task_timezone_end_of_day(config):
    task = {'priority': 'P0', 'time': {'deadline': '2026-11-13', 'timezone': 'Asia/Shanghai'}}
    expected = core.dt.datetime(2026, 11, 13, 23, 59, 59, tzinfo=core.ZoneInfo('Asia/Shanghai')).timestamp()
    enabled, node = core._deadline_node(config, task, expected)
    assert enabled and node == {'deadline_at': expected, 'offset_seconds': 0, 'due_at': expected}


def test_initial_notification_can_wait_for_first_future_node(config, sent):
    config['initial_notifications']['P0'] = False
    batch(config, 'one', deadline_event(200000))
    assert run_round(config, now=150000)['sent'] == 0
    assert status(config)['delivery_counts']['deferred'] == 1
    assert run_round(config, now=150001)['sent'] == 0
    assert run_round(config, now=200000 - 21600)['sent'] == 1
    assert sent[-1][1]['kind'] == 'reminder'


def test_enabling_initial_notification_sends_previously_deferred_task(config, sent):
    config['initial_notifications']['P0'] = False
    batch(config, 'one', deadline_event(200000))
    assert run_round(config, now=100)['sent'] == 0
    config['initial_notifications']['P0'] = True
    assert run_round(config, now=101)['sent'] == 1
    assert run_round(config, now=102)['sent'] == 0


def test_digest_uses_today_notification_nodes_instead_of_due_date(config, sent):
    zone = core.ZoneInfo('Asia/Shanghai')
    day = core.dt.datetime(2026, 11, 14, tzinfo=zone).timestamp()
    config['daily_digest']['enabled'] = True
    config['initial_notifications'] = {p: False for p in config['initial_notifications']}
    batch(config, 'one', deadline_event(day + 86400 + 8 * 3600, identifier='tomorrow-p0'),
          deadline_event(day + 12 * 3600, identifier='today-p4', priority='P4'),
          deadline_event(day - 86400, identifier='overdue-p4', priority='P4'),
          deadline_event(day + 86400 + 12 * 3600, identifier='tomorrow-p4', priority='P4'),
          deadline_event(day + 86400 + 8 * 3600, identifier='completed', task_status='completed'))
    run_round(config, now=day - 3600)
    assert not sent
    run_round(config, now=day + 8 * 3600)
    digest = next(e for _, e, _ in sent if e['kind'] == 'daily_digest')
    assert 'task' not in digest
    assert {i['task_id'] for i in digest['digest']['items']} == {'tomorrow-p0', 'today-p4'}
    run_round(config, now=day + 8 * 3600 + 1)
    assert len([e for _, e, _ in sent if e['kind'] == 'daily_digest']) == 1
    assert status(config)['tasks'] == 5


def test_digest_retries_same_id_and_expires_without_late_spam(config, sent, monkeypatch):
    zone = core.ZoneInfo('Asia/Shanghai')
    slot = core.dt.datetime(2026, 11, 14, 8, tzinfo=zone).timestamp()
    config['daily_digest']['enabled'] = True
    rejected = []
    def reject(target, envelope, *args, **kwargs):
        rejected.append(envelope['delivery_id'])
        raise channels.DeliveryRejected('temporary')
    monkeypatch.setattr(channels, 'deliver', reject)
    assert run_round(config, now=slot)['retry'] == 1
    assert run_round(config, now=slot + 61)['retry'] == 1
    assert len(set(rejected)) == 1
    assert run_round(config, now=slot + 3601)['retry'] == 0
    assert status(config)['delivery_counts']['obsolete'] == 1


def test_revisions_update_existing_remote_receipt(config, sent):
    batch(config, 'first', event())
    run_round(config, now=100)
    first_receipt = sent[-1][1]['delivery_id']
    batch(config, 'second', event(revision=2, title='Changed report'))
    assert run_round(config, now=101)['sent'] == 1
    assert sent[-1][1]['kind'] == 'upsert'
    assert sent[-1][2] == {'remote_id': first_receipt}


@pytest.mark.parametrize('target', [{'kind': 'notion'}, {'kind': 'webhook', 'mode': 'sync'}])
def test_sync_only_targets_never_repeat_reminders(config, sent, target):
    config['targets'][0].update(target)
    batch(config, 'one', event())
    run_round(config, now=100)
    assert run_round(config, now=100000)['sent'] == 0
    set_task_state(config, 'one', 'complete')
    assert run_round(config, now=100001)['sent'] == 1
    assert sent[-1][1]['task']['status'] == 'completed'


def test_completion_survives_processor_revision_and_is_idempotent(config, sent):
    batch(config, 'one', event())
    run_round(config, now=100)
    first = set_task_state(config, 'one', 'complete')
    assert set_task_state(config, 'one', 'complete') == first
    run_round(config, now=101)
    assert sent[-1][1]['task']['status'] == 'completed'
    batch(config, 'two', event(revision=2))
    run_round(config, now=102)
    assert sent[-1][1]['task']['status'] == 'completed'
    assert run_round(config, now=10000)['sent'] == 0
    set_task_state(config, 'one', 'reopen')
    assert run_round(config, now=10001)['sent'] == 1
    assert sent[-1][1]['task']['status'] == 'open'


def test_cancelled_and_superseded_tasks_stop_reminders(config, sent):
    batch(config, 'one', event(), event('second'))
    run_round(config, now=100)
    batch(config, 'two', event(revision=2, task_status='cancelled'), event('second', revision=2, task_status='superseded'))
    assert run_round(config, now=101)['sent'] == 2
    assert run_round(config, now=9999)['sent'] == 0
    with pytest.raises(ValueError, match='upstream closed'):
        set_task_state(config, 'one', 'reopen')


def test_snooze_prevents_pending_retry_until_deadline(config, sent, monkeypatch):
    batch(config, 'one', event())
    sync(config)
    monkeypatch.setattr(core.time, 'time', lambda: 100)
    set_task_state(config, 'one', 'snooze', until=200)
    assert run_round(config, now=199)['sent'] == 0
    assert run_round(config, now=200)['sent'] == 1
    assert run_round(config, now=200)['sent'] == 0


@pytest.mark.parametrize('action', ['snooze', 'complete'])
def test_new_local_state_invalidates_pending_retry(config, sent, monkeypatch, action):
    batch(config, 'one', event())
    deliver = channels.deliver
    def rejected(*args, **kwargs):
        raise channels.DeliveryError('rate_limited')
    monkeypatch.setattr(channels, 'deliver', rejected)
    assert run_round(config, now=100)['retry'] == 1
    monkeypatch.setattr(core.time, 'time', lambda: 101)
    set_task_state(config, 'one', action, until=300)
    monkeypatch.setattr(channels, 'deliver', deliver)
    assert run_round(config, now=200)['sent'] == 0
    assert sent == []
    assert status(config)['delivery_counts'] == {'obsolete': 1}


def test_explicit_failure_backs_off_then_retries_with_same_id(config, sent, monkeypatch):
    batch(config, 'one', event())
    deliver = channels.deliver
    def rejected(*args, **kwargs):
        raise channels.DeliveryError('rate_limited', uncertain=False)
    monkeypatch.setattr(channels, 'deliver', rejected)
    assert run_round(config, now=100)['retry'] == 1
    identifier = status(config)['deliveries'][0]['id']
    assert run_round(config, now=159)['retry'] == 0
    monkeypatch.setattr(channels, 'deliver', deliver)
    assert run_round(config, now=160)['sent'] == 1
    assert sent[0][1]['delivery_id'] == identifier


def test_uncertain_failure_blocks_followups_until_manual_resolution(config, sent, monkeypatch):
    batch(config, 'one', event())
    deliver = channels.deliver
    def ambiguous(*args, **kwargs):
        raise channels.DeliveryError('write_timeout', uncertain=True)
    monkeypatch.setattr(channels, 'deliver', ambiguous)
    assert run_round(config, now=100)['unknown'] == 1
    identifier = status(config)['deliveries'][0]['id']
    batch(config, 'two', event(revision=2))
    monkeypatch.setattr(channels, 'deliver', deliver)
    assert run_round(config, now=500)['sent'] == 0
    resolve_delivery(config, identifier, 'retry')
    assert run_round(config, now=501)['sent'] == 1
    assert sent[0][1]['task']['revision'] == 2


def test_interrupted_network_attempt_becomes_unknown_without_resend(config, sent):
    batch(config, 'one', event())
    run_round(config, now=100)
    with sqlite3.connect(config['storage']['state_file']) as conn:
        conn.execute("UPDATE deliveries SET status='sending',receipt=NULL")
    assert run_round(config, now=1000)['sent'] == 0
    assert status(config)['delivery_counts'] == {'unknown': 1}


def test_disabled_global_and_target_never_deliver(config, sent):
    batch(config, 'one', event())
    config['enabled'] = False
    assert run_round(config, now=100)['sent'] == 0
    config['enabled'] = True
    config['targets'][0]['enabled'] = False
    assert run_round(config, now=200)['sent'] == 0
    assert sent == []


def test_misconfigured_state_does_not_modify_another_layers_database(config):
    with sqlite3.connect(config['storage']['state_file']) as conn:
        conn.execute('CREATE TABLE private_messages(body TEXT)')
        conn.execute("INSERT INTO private_messages VALUES('original')")
    original = Path(config['storage']['state_file']).read_bytes()
    with pytest.raises(ValueError, match='another application'):
        sync(config)
    with pytest.raises(ValueError, match='another application'):
        preview(config)
    assert Path(config['storage']['state_file']).read_bytes() == original


def test_result_material_persists_after_completion_and_new_revision(config, monkeypatch):
    from messaging import package
    resource = {'id': 'fixed', 'name': 'result.pdf', 'status': 'ready', 'local_path': 'archived.pdf'}
    monkeypatch.setattr(package, 'archive_result', lambda *args: resource)
    batch(config, 'one', event())
    sync(config)
    add_result(config, 'one', 'selected.pdf', note='Submitted version')
    version = status(config)['local_state']['one']['version']
    add_result(config, 'one', 'selected.pdf', note='Submitted version')
    assert status(config)['local_state']['one']['version'] == version
    set_task_state(config, 'one', 'complete')
    batch(config, 'two', event(revision=2))
    sync(config)
    task = preview(config)['tasks'][0]
    assert task['status'] == 'completed'
    assert task['resources'] == [dict(resource, role='result', note='Submitted version')]
    assert task_package(config, 'one') == task


@pytest.mark.parametrize('field,value', [
    ('time', []), ('time', {'start': 'tomorrow'}), ('time', {'deadline': '2026-11-13T10:00:00'}),
    ('time', {'timezone': 'invalid/timezone'}), ('people', ['person']),
    ('people', [{'name': 'A', 'role': {}}]), ('sources', 'message'), ('sources', [{}]),
    ('sources', [{'message_key': 'm', 'attachments': ['file']}]),
    ('sources', [{'message_key': 'm', 'identity': {'conversation': 'room'}}]),
    ('attachment_evidence', [{'message_key': 'm', 'attachment_index': 0, 'attachment': {}}]),
    ('materials', [{'message_key': 'm', 'attachment_index': 0, 'attachment': {}}]),
    ('action_steps', ['x' * 2001]), ('action_steps', ['step'] * 51), ('action_steps', {}),
    ('unknown_metadata', float('nan')), ('review_reasons', [None]), ('needs_review', 1),
])
def test_invalid_nested_task_rejected_before_persisting(config, field, value):
    malformed = event('invalid')
    malformed['task'][field] = value
    batch(config, 'one', event(), malformed)
    with pytest.raises(ValueError):
        sync(config)
    assert status(config)['tasks'] == 0


def test_valid_nested_contract_accepts_null_dates_and_material_references(config):
    item = event()
    item['task'].update(time={'start': None, 'deadline': '2026-11-13', 'timezone': 'Asia/Shanghai'},
                        people=[{'name': 'A', 'role': 'owner'}], action_steps=['Fill form'],
                        sources=[{'message_key': 'm', 'attachments': [{'name': 'form.pdf'}]}],
                        materials=[{'message_key': 'm', 'attachment_index': 0, 'attachment': {'name': 'form.pdf'}}])
    batch(config, 'one', item)
    assert sync(config)['imported_events'] == 1


def test_unserializable_or_excessive_json_rejected():
    malformed = event()
    malformed['task']['extension'] = object()
    with pytest.raises(ValueError, match='JSON values'):
        core._event(malformed)
    deep = {}
    for _ in range(34):
        deep = {'child': deep}
    malformed['task']['extension'] = deep
    with pytest.raises(ValueError, match='structure limits'):
        core._event(malformed)


def test_material_availability_changes_trigger_sync_without_processor_revision(config, sent, monkeypatch):
    config['targets'][0]['kind'] = 'notion'
    resource = {'id': 'asset', 'name': 'form.pdf', 'status': 'unavailable'}
    monkeypatch.setattr(core, '_package', lambda task, config, prepare=False: dict(task, resources=[dict(resource)]))
    batch(config, 'one', event())
    assert run_round(config, now=100)['sent'] == 1
    resource.update(status='ready', local_path='A:/assets/form.pdf', sha256='a' * 64)
    assert run_round(config, now=101)['sent'] == 1
    assert sent[-1][1]['kind'] == 'upsert'
    resource['local_path'] = 'B:/assets/form.pdf'
    assert run_round(config, now=102)['sent'] == 0
    resource.update(status='unavailable')
    assert run_round(config, now=103)['sent'] == 1
    resource.update(status='ready')
    assert run_round(config, now=104)['sent'] == 1
    assert len({entry[1]['delivery_id'] for entry in sent}) == 4


def test_material_change_invalidates_pending_stale_package(config, sent, monkeypatch):
    resource = {'id': 'asset', 'status': 'unavailable'}
    monkeypatch.setattr(core, '_package', lambda task, config, prepare=False: dict(task, resources=[dict(resource)]))
    batch(config, 'one', event())
    deliver = channels.deliver
    def rejected(*args, **kwargs):
        raise channels.DeliveryError('rate_limited')
    monkeypatch.setattr(channels, 'deliver', rejected)
    assert run_round(config, now=100)['retry'] == 1
    resource['status'] = 'ready'
    monkeypatch.setattr(channels, 'deliver', deliver)
    assert run_round(config, now=101)['sent'] == 1
    assert sent[-1][1]['task']['resources'][0]['status'] == 'ready'
    assert status(config)['delivery_counts'] == {'obsolete': 1, 'sent': 1}


@pytest.mark.parametrize('target', [{'kind': 'notion'}, {'kind': 'webhook', 'mode': 'sync'}])
def test_completed_before_enabling_still_syncs_to_knowledge_target(config, sent, target):
    config['targets'][0].update(target)
    batch(config, 'one', event())
    sync(config)
    set_task_state(config, 'one', 'complete')
    assert run_round(config, now=100)['sent'] == 1
    assert sent[-1][1]['task']['status'] == 'completed'
    assert run_round(config, now=1000)['sent'] == 0


def test_own_state_schema_migrates_package_hash_column(config):
    sync(config)
    with sqlite3.connect(config['storage']['state_file']) as conn:
        conn.execute('ALTER TABLE deliveries DROP COLUMN package_hash')
    sync(config)
    with sqlite3.connect(config['storage']['state_file']) as conn:
        assert 'package_hash' in {row[1] for row in conn.execute('PRAGMA table_info(deliveries)')}


@pytest.mark.parametrize('field,value', [('enabled', 'true'), ('schedule.interval_seconds', 0), ('reminders.P0', -1), ('storage.state_file', 'incoming/state.db')])
def test_invalid_config_rejected(tmp_path, field, value):
    section, _, key = field.rpartition('.')
    text = ('[' + section + ']\n' if section else '') + key + '=' + json.dumps(value)
    if field == 'storage.state_file':
        text += '\n[input]\nbatches_dir="incoming"\n'
    path = tmp_path / 'bad.toml'
    path.write_text(text, encoding='utf-8')
    with pytest.raises(ValueError):
        load_config(path)
