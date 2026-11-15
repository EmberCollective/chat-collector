import datetime as dt
import json
from pathlib import Path

import pytest

from messaging import core, channels
from messaging import planning
from test_messaging_core import config as base_config, event, batch


@pytest.fixture
def config(base_config, tmp_path):
    base_config['feedback']['state_file'] = str(tmp_path/'feedback.db')
    return base_config


def stamp(value):
    return dt.datetime.fromisoformat(value).timestamp()


def deadline(config, *, value='2026-11-16T18:00:00+08:00', priority='P1'):
    item = event(priority=priority)
    item['task']['time'] = {'deadline': value, 'timezone': 'Asia/Shanghai'}
    batch(config, 'one', item)
    return item


def test_projection_never_prepares_assets_or_creates_state(config, monkeypatch):
    deadline(config)
    monkeypatch.setattr(core, '_package', lambda *a, **k: pytest.fail('list must not inspect assets'))
    result = planning.schedule(config, now=stamp('2026-11-15T17:00:00+08:00'))
    assert result['tasks'][0]['plans'][0]['state'] == 'waiting_import'
    assert result['pending_events'] == 1
    assert not Path(config['storage']['state_file']).exists()
    assert not Path(config['storage']['assets_dir']).exists()
    assert not Path(config['feedback']['state_file']).exists()


@pytest.mark.parametrize('priority', ['P0','P1','P2','P3','P4'])
def test_projected_deadline_executes_at_the_same_node(config, monkeypatch, priority):
    deadline(config, priority=priority)
    sent = []
    monkeypatch.setattr(channels, 'deliver', lambda target, envelope, *a, **k: sent.append(envelope) or {'remote_id':'ok'})
    start = stamp('2026-11-15T12:00:00+08:00')
    core.run_round(config, now=start)
    for _ in range(7):
        result = planning.schedule(config, now=start)
        p = result['tasks'][0]['plans'][0]
        if p['due_at'] is None:
            assert p['state'] == 'no_reminders'
            break
        before = len(sent)
        assert core.run_round(config, now=p['due_at'] - .01)['sent'] == 0
        assert len(sent) == before
        assert core.run_round(config, now=p['due_at'])['sent'] == 1
        assert sent[-1]['kind'] == 'reminder'
        assert sent[-1]['reminder']['due_at'] == p['due_at']
        start = p['due_at']


def test_initial_off_skips_past_nodes_and_snooze_uses_latest(config, monkeypatch):
    deadline(config, value='2026-11-15T20:00:00+08:00')
    config['initial_notifications']['P1'] = False
    start = stamp('2026-11-15T17:00:00+08:00')
    p = planning.schedule(config, now=start)['tasks'][0]['plans'][0]
    assert p['due_at'] == stamp('2026-11-15T18:00:00+08:00')
    monkeypatch.setattr(channels, 'deliver', lambda *a, **k: {'remote_id':'ok'})
    assert core.run_round(config, now=start)['sent'] == 0
    with core._connect(config, True) as conn, conn:
        conn.execute('INSERT INTO local_state(task_id,snoozed_until,version) VALUES(?,?,0)', ('one',stamp('2026-11-15T19:45:00+08:00')))
    p = planning.schedule(config, now=start)['tasks'][0]['plans'][0]
    assert p['due_at'] == stamp('2026-11-15T19:45:00+08:00')
    assert core.run_round(config, now=p['due_at'])['sent'] == 1
    with core._connect(config) as conn:
        node = json.loads(conn.execute("SELECT body FROM deliveries WHERE status='sent'").fetchone()[0])['reminder']
    assert node['due_at'] == stamp('2026-11-15T19:30:00+08:00')


def test_turning_off_initial_does_not_present_existing_upsert_as_queued(config, monkeypatch):
    deadline(config)
    at = stamp('2026-11-15T17:00:00+08:00')
    def fail(*args, **kwargs):
        raise channels.DeliveryError('rejected', uncertain=False)
    monkeypatch.setattr(channels, 'deliver', fail)
    core.run_round(config, now=at)
    config['initial_notifications']['P1'] = False
    p = planning.schedule(config, now=at)['tasks'][0]['plans'][0]
    assert p['state'] == 'scheduled' and p['due_at'] == stamp('2026-11-16T12:00:00+08:00')
    assert core.run_round(config, now=at)['retry'] == 0


def test_unknown_is_per_target_and_disabled_and_sync_are_distinct(config, monkeypatch):
    deadline(config)
    config['targets'] += [dict(config['targets'][0],id='other'), {'id':'archive','kind':'notion','enabled':True},
                          {'id':'future','kind':'unknown-kind','enabled':False}]
    def deliver(target, envelope, *a, **k):
        if target['id'] == 'notify':
            raise channels.DeliveryError('timeout', uncertain=True)
        return {'remote_id':'ok'}
    monkeypatch.setattr(channels, 'deliver', deliver)
    at = stamp('2026-11-15T17:00:00+08:00')
    core.run_round(config, now=at)
    plans = {p['target_id']:p for p in planning.schedule(config, now=at)['tasks'][0]['plans']}
    assert plans['notify']['state'] == 'unknown' and plans['notify']['due_at'] is None
    assert plans['other']['due_at'] is not None
    assert plans['archive']['state'] == 'synced'
    assert plans['future']['state'] == 'disabled'
    assert planning.schedule(config, now=at, state='attention', target_id='other')['matched_tasks'] == 0


def test_digest_preview_matches_real_queue_and_freezes_sent_content(config, monkeypatch):
    deadline(config)
    config['daily_digest'].update(enabled=True, catch_up_minutes=360)
    day = '2026-11-16'
    at = stamp(day+'T08:00:00+08:00')
    monkeypatch.setattr(channels, 'deliver', lambda *a, **k: {'remote_id':'ok'})
    core.run_round(config, now=at-86400)
    before = Path(config['storage']['state_file']).read_bytes()
    preview = planning.digest_preview(config, day=day, now=at-3600)
    assert Path(config['storage']['state_file']).read_bytes() == before
    assert len(preview['items']) == 1
    core.run_round(config, now=at)
    with core._connect(config) as conn:
        body = json.loads(conn.execute("SELECT body FROM deliveries WHERE kind='daily_digest' AND task_id=?", ('digest:'+day,)).fetchone()[0])
    assert preview['items'] == body['digest']['items']
    item = event(revision=2, task_status='completed')
    batch(config,'changed',item)
    frozen = planning.digest_preview(config, day=day, now=at+60)
    assert frozen['delivery']['frozen'] and frozen['items'] == preview['items']


def test_date_only_interval_and_filters(config, monkeypatch):
    deadline(config, value='2026-11-16', priority='P3')
    batch(config,'two',event('two',priority='P2',title='interval example'))
    monkeypatch.setattr(channels, 'deliver', lambda *a, **k: {'remote_id':'ok'})
    at = stamp('2026-11-15T17:00:00+08:00')
    core.run_round(config, now=at)
    result = planning.schedule(config, now=at)
    by_id = {t['task_id']:t for t in result['tasks']}
    assert by_id['one']['deadline_at'] == stamp('2026-11-16T23:59:59+08:00')
    assert by_id['one']['date_only']
    assert by_id['two']['plans'][0]['due_at'] == at+config['reminders']['P2']
    assert planning.schedule(config, now=at, search='interval')['matched_tasks'] == 1
    assert planning.schedule(config, now=at, limit=1)['has_more']
    assert planning.schedule(config, now=at, task_id='one')['tasks'][0]['task_id'] == 'one'
    with pytest.raises(ValueError):
        planning.digest_preview(config, day='2026-02-30', now=at)
