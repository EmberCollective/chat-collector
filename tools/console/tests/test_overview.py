import json
import os
from pathlib import Path
import sqlite3

import pytest

from test_console import console
from overview import _metrics, _specs
from storage_metrics import StorageMetrics


def record(identifier, status='succeeded', result=None, start=10, end=20, source='console'):
    return {'id': identifier, 'source': source, 'source_id': 'personal', 'module': 'personal',
            'action': 'collect', 'status': status, 'started_at': start, 'finished_at': end,
            'result': result}


def valid(emitted=0):
    return {'start': 1, 'end': 2, 'emitted': emitted, 'duplicates': 0, 'matched': emitted}


def test_one_episode_denominator_excludes_skips_unknown_and_retries():
    runs = [record('zero', result=valid()), record('failed', 'failed', {}),
            record('skip', result={'status': 'no_enabled_rules', 'emitted': 0, 'duplicates': 0}),
            record('legacy-skip', result={'matched': 0, 'emitted': 0, 'duplicates': 0, 'batches': 0}, source='automation'),
            record('retry', 'retry', {}, end=None), record('running', 'running', {}, end=None),
            record('unknown', result=None), record('negative-duration', 'failed', {}, end=5)]
    runs[0]['attempts'] = {'total': 2, 'failed': 1, 'unknown': 0}
    data = _metrics(runs)
    assert (data['success'], data['failed'], data['skipped'], data['unknown']) == (1, 1, 2, 2)
    assert data['running'] == data['retrying'] == 1
    assert data['success_rate'] == .5 and data['valid_count'] == data['sample_count'] == 2
    assert data['avg_seconds'] == data['p95_seconds'] == 10
    assert data['emitted'] == 0 and data['emitted_complete'] is False
    assert data['attempts']['total'] == 2 and data['attempts']['failed'] == 1
    assert {item['run_id'] for item in data['recent']} == {'zero', 'failed'}
    assert _metrics([])['success_rate'] is None
    assert _metrics([], available=False)['emitted'] is None
    durations = [record(str(i), result=valid(), start=0, end=i) for i in range(1, 21)]
    assert _metrics(durations)['p95_seconds'] == 19


def save_job(app, identifier, module, started, result, *, status='succeeded', action='collect', **metadata):
    folder = app.state.jobs.directory / identifier
    folder.mkdir()
    job = {'id': identifier, 'module': module, 'action': action, 'origin': 'manual',
           'status': status, 'started_at': started, 'finished_at': started + 2, **metadata}
    (folder / 'job.json').write_text(json.dumps(job), encoding='utf8')
    if result is not None:
        (folder / 'result.json').write_text(json.dumps({'status': status, 'result': result}), encoding='utf8')
    return folder


def test_overview_is_authenticated_windowed_and_does_not_initialize_state(console, monkeypatch):
    client, app, root, now = console
    bridge = app.state.bridge
    bridge.sources['future'] = {'id': 'future', 'name': '外部信息源', 'kind': 'future', 'capabilities': {}}
    save_job(app, 'a' * 32, 'wechat', now - 20, valid(4))
    save_job(app, 'b' * 32, 'wechat', now - 90000, valid(100))
    save_job(app, 'c' * 32, 'qq', now - 20, None)
    before = {p: p.read_bytes() for p in root.rglob('*.db')}
    response = client.get('/api/overview')
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['summary']['emitted'] == 4 and data['summary']['success'] == 1
    assert data['summary']['emitted_complete'] is False
    assert data['window']['metric_unit'] == 'episode' and len(data['success_bins']) == 12
    assert sum(b['success'] for b in data['success_bins']) == 1
    future = next(s for s in data['sources'] if s['id'] == 'future')
    assert future['metrics_available'] is False and future['emitted'] is None and future['status'] == 'unsupported'
    assert data['queues']['processor']['pending_groups'] is None
    assert data['queues']['messaging']['counts'] is None
    assert not (root / 'processing/state.db').exists()
    assert all(p.read_bytes() == contents for p, contents in before.items())
    assert client.get('/api/overview', headers={'X-Console-Token': 'wrong'}).status_code == 401
    assert client.get('/api/overview', headers={'Origin': 'https://other.example'}).status_code == 403
    for hours in (0, 169, 'bad'):
        assert client.get('/api/overview', params={'hours': hours}).status_code == 422
    assert len(client.get('/api/overview?hours=1').json()['success_bins']) == 1
    assert next(s for s in data['sources'] if s['id'] == 'qq')['unknown'] == 1


def automation_config(app, root):
    path = root / 'automation.toml'
    path.write_text('enabled=true\n[storage]\nstate_file="automatic/state.db"\n[jobs.wechat]\nenabled=true\nconfig="wechat/config.toml"\n', encoding='utf8')
    app.state.bridge.settings['automation_config'] = str(path)
    from automation import load_config
    return load_config(path)


def test_automatic_retry_episode_maps_by_config_path_and_deduplicates_child(console, monkeypatch):
    from automation import SchedulerService, control
    client, app, root, now = console
    cfg = automation_config(app, root)
    source = app.state.bridge.sources.pop('wechat')
    source.update(id='personal', name='个人消息')
    app.state.bridge.sources['personal'] = source
    app.state.bridge.paths['personal'] = app.state.bridge.paths.pop('wechat')
    ticks, calls = [now - 100], []
    def collect():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError('PRIVATE_CONTENT_NOT_FOR_OVERVIEW')
        return valid()
    service = SchedulerService(cfg, callbacks={'wechat': collect}, clock=lambda: ticks[0])
    service.execute('wechat')
    ticks[0] += 31
    service.execute('wechat')
    from automation.history import list_runs
    run = list_runs(cfg)[0]
    save_job(app, 'd' * 32, 'personal', now - 80, valid(100), automation_run_id=run['id'])
    monkeypatch.setattr(control, 'status', lambda cfg: {'enabled': True, 'state': 'running', 'running': True,
        'verified': True, 'jobs': [{'name': 'wechat', 'state': 'idle', 'last_success': now, 'current_run_id': run['id']}], 'config_changed': False})
    data = client.get('/api/overview').json()
    assert data['summary']['success'] == 1 and data['summary']['failed'] == 0
    source = next(s for s in data['sources'] if s['id'] == 'personal')
    assert source['emitted'] == 0 and source['attempts']['total'] == 2 and source['attempts']['failed'] == 1
    assert source['last_success'] == run['finished_at']
    assert source['recent'][0]['run_source'] == 'automation'
    assert source['recent'][0]['source_id'] == 'personal'
    job = next(j for j in data['jobs'] if j['module'] == 'wechat')
    assert job['source_id'] == 'personal' and job['last_success'] == run['finished_at']
    assert 'PRIVATE_CONTENT_NOT_FOR_OVERVIEW' not in json.dumps(data)


def test_retention_cap_marks_window_truncation_and_does_not_claim_all_history(console, monkeypatch):
    from automation import SchedulerService, control
    client, app, root, now = console
    cfg = automation_config(app, root)
    SchedulerService(cfg, callbacks={'wechat': lambda: valid()}, clock=lambda: now - 20).execute('wechat')
    with sqlite3.connect(cfg['storage']['state_file']) as conn:
        conn.execute('DELETE FROM runs')
        conn.executemany('INSERT INTO runs(id,module,action,origin,status,started_at,finished_at,result) VALUES(?,\'wechat\',\'collect\',\'schedule\',\'succeeded\',?,?,?)',
                         [(str(i), now - i - 1, now - i, json.dumps(valid())) for i in range(1000)])
    monkeypatch.setattr(control, 'status', lambda cfg: {'state': 'stopped', 'running': False, 'jobs': []})
    data = client.get('/api/overview').json()
    assert data['truncation']['history'] is True
    coverage = next(c for c in data['coverage']['automation']['modules'] if c['module'] == 'wechat')
    assert coverage['retained'] == 1000 and coverage['retention_at_limit'] is True
    assert data['summary']['success'] == 1000 and data['summary']['emitted_complete'] is False
    assert data['coverage']['automation']['status'] == 'retained_only'


def test_replay_does_not_recover_formal_failure_and_no_finish_unknown_is_visible(console):
    client, app, _, now = console
    save_job(app, '1' * 32, 'wechat', now - 100, valid())
    save_job(app, '2' * 32, 'wechat', now - 80, {}, status='failed')
    save_job(app, '3' * 32, 'wechat', now - 60, valid(100), action='replay')
    folder = save_job(app, '4' * 32, 'qq', now - 30, None, status='interrupted')
    metadata = json.loads((folder / 'job.json').read_text(encoding='utf8'))
    metadata['finished_at'] = None
    (folder / 'job.json').write_text(json.dumps(metadata), encoding='utf8')
    data = client.get('/api/overview').json()
    source = next(s for s in data['sources'] if s['id'] == 'wechat')
    assert source['last_success'] == now - 98
    assert [r['run_id'] for r in source['recent']] == ['1' * 32, '2' * 32]
    assert any(i['run_id'] == '2' * 32 for i in data['issues'])
    unknown = next(i for i in data['issues'] if i['run_id'] == '4' * 32)
    assert unknown['last_at'] == now - 30 and unknown['status'] == 'unknown'


def test_disabled_schedule_retains_failed_history_without_active_retry_issue(console, monkeypatch):
    from automation import SchedulerService, control
    client, app, root, now = console
    cfg = automation_config(app, root)
    def fail():
        raise RuntimeError('failure')
    service = SchedulerService(cfg, callbacks={'wechat': fail}, clock=lambda: now - 20)
    service.execute('wechat')
    path = Path(cfg['_config_path'])
    path.write_text(path.read_text(encoding='utf8').replace('enabled=true\nconfig=', 'enabled=false\nconfig='), encoding='utf8')
    monkeypatch.setattr(control, 'status', lambda cfg: {'state': 'stopped', 'running': False, 'jobs': [
        {'name': 'wechat', 'state': 'retry', 'error': 'RuntimeError', 'last_start': now - 20}]})
    data = client.get('/api/overview').json()
    assert next(s for s in data['sources'] if s['id'] == 'wechat')['retrying'] == 1
    assert not any(i['source_id'] == 'wechat' for i in data['issues'])
    assert next(s for s in data['sources'] if s['id'] == 'wechat')['status'] == 'disabled'


def test_unmapped_and_malformed_manual_records_are_not_assigned_by_brand(console):
    client, app, _, now = console
    save_job(app, '5' * 32, 'other-account', now - 20, valid(99))
    folder = save_job(app, '6' * 32, 'wechat', now - 30, valid())
    metadata = json.loads((folder / 'job.json').read_text(encoding='utf8'))
    metadata.update(module={'bad': 'shape'}, error=['not an error code'], automation_run_id={})
    (folder / 'job.json').write_text(json.dumps(metadata), encoding='utf8')
    data = client.get('/api/overview').json()
    assert data['coverage']['unmapped_collection_records'] == 2
    assert data['summary']['emitted_complete'] is False
    assert all(source['success'] == 0 for source in data['sources'])


def test_queues_read_real_unknown_delivery_and_feedback_without_sending(console):
    from test_messaging_console import setup
    from messaging import load_config
    from messaging.core import _connect
    from messaging.feedback import _connect as feedback_connect
    client, root = setup(console)
    cfg = load_config(root / 'messaging.toml')
    with _connect(cfg, True) as conn:
        conn.execute("INSERT INTO deliveries(id,status,target_id,created_at,error) VALUES('delivery-1','unknown','room',123,'transport_timeout')")
    with feedback_connect(cfg, True) as conn:
        conn.execute("INSERT INTO feedback_requests(request_id,task_id,request,acknowledgement,status,created_at,updated_at,error) VALUES('feedback-1','task','{}','{}','conflict',1,2,'task_status_conflict')")
    data = client.get('/api/overview').json()
    assert data['queues']['messaging']['counts']['unknown'] == 1
    assert data['queues']['feedback']['counts']['conflict'] == 1
    delivery = next(i for i in data['issues'] if i.get('delivery_id') == 'delivery-1')
    assert delivery['detail_target'] == {'kind': 'delivery', 'id': 'delivery-1'}
    assert delivery['next_action'] == 'verify_delivery' and delivery['status'] == 'unknown'
    assert delivery['last_at'] == 123 and delivery['title']
    assert any(i['detail_target'] == {'kind': 'feedback', 'id': 'feedback-1'} for i in data['issues'])
    with _connect(cfg) as conn:
        assert conn.execute("SELECT status FROM deliveries WHERE id='delivery-1'").fetchone()[0] == 'unknown'


def spec(path, *owners):
    return {'path': str(path), 'source_ids': list(owners), 'category': 'output'}


def test_storage_deduplicates_hardlinks_and_overlapping_roots_in_shared_bucket(tmp_path):
    outer = tmp_path / 'outer'
    nested = outer / 'nested'
    nested.mkdir(parents=True)
    (outer / 'a').write_bytes(b'a' * 3)
    (nested / 'b').write_bytes(b'b' * 5)
    elsewhere = tmp_path / 'other'
    elsewhere.mkdir()
    os.link(outer / 'a', elsewhere / 'same-a')
    sources = [{'id': 'a', 'name': 'A'}, {'id': 'b', 'name': 'B'}]
    scanner = StorageMetrics()
    data = scanner.measure([spec(outer, 'a'), spec(nested, 'b'), spec(elsewhere, 'b')], sources)
    assert data['known_logical_bytes'] == data['total_logical_bytes'] == 8
    assert data['shared']['logical_bytes'] == 8
    assert [r['logical_bytes'] for r in data['sources']] == [0, 0]
    assert data['files_counted'] == 2 and data['physical_bytes'] is None
    (outer / 'a').write_bytes(b'a' * 30)
    cached = scanner.measure([spec(outer, 'a'), spec(nested, 'b'), spec(elsewhere, 'b')], sources)
    assert cached['cached'] is True and cached['measured_at'] == data['measured_at'] and cached['known_logical_bytes'] == 8


def test_storage_missing_unreadable_and_budget_are_unknown_not_zero(tmp_path, monkeypatch):
    sources = [{'id': 'a', 'name': 'A'}]
    folder = tmp_path / 'files'
    folder.mkdir()
    (folder / 'one').write_bytes(b'123')
    data = StorageMetrics(max_entries=1).measure([spec(folder, 'a')], sources)
    assert data['truncated'] is True and data['total_logical_bytes'] is None
    assert data['sources'][0]['logical_bytes'] is None
    missing = StorageMetrics().measure([spec(tmp_path / 'missing', 'a')], sources)
    assert missing['scope'][0]['status'] == 'missing' and missing['sources'][0]['logical_bytes'] is None
    import storage_metrics
    original = storage_metrics._stat
    def blocked(path):
        if path == folder:
            raise PermissionError('denied')
        return original(path)
    monkeypatch.setattr(storage_metrics, '_stat', blocked)
    unreadable = StorageMetrics().measure([spec(folder, 'a')], sources)
    assert unreadable['scope'][0]['status'] == 'unreadable' and unreadable['sources'][0]['logical_bytes'] is None
    from types import SimpleNamespace
    def no_identity(path):
        info = original(path)
        return SimpleNamespace(st_mode=info.st_mode, st_ino=0) if path == folder / 'one' else info
    monkeypatch.setattr(storage_metrics, '_stat', no_identity)
    unknown = StorageMetrics().measure([spec(folder, 'a')], sources)
    assert unknown['sources'][0]['logical_bytes'] is None and 'file_identity_unavailable' in unknown['warnings']


def test_storage_does_not_follow_configured_symlink_or_nested_reparse(tmp_path):
    actual = tmp_path / 'actual'
    actual.mkdir()
    (actual / 'secret').write_bytes(b'not in scope')
    link = tmp_path / 'link'
    try:
        link.symlink_to(actual, target_is_directory=True)
    except OSError:
        pytest.skip('Symlink creation unavailable on this Windows installation')
    config = tmp_path / 'config.toml'
    config.write_text('[source]\ndecrypted_dir="link"\n', encoding='utf8')
    specs = _specs(config, {'source': {'decrypted_dir': str(actual)}}, (('source', 'decrypted_dir', 'snapshots'),), 'a')
    data = StorageMetrics().measure(specs, [{'id': 'a', 'name': 'A'}])
    assert data['scope'][0]['status'] == 'blocked' and data['files_counted'] == 0
    data = StorageMetrics().measure([spec(tmp_path, 'a')], [{'id': 'a', 'name': 'A'}])
    assert data['complete'] is False and 'link_or_reparse_point' in data['warnings']


def test_storage_rejects_reparse_roots_and_does_not_measure_resolved_defaults(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import stat
    folder = tmp_path / 'junction'
    folder.mkdir()
    original = Path.lstat
    def lstat(path):
        return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400) if path == folder else original(path)
    monkeypatch.setattr(Path, 'lstat', lstat)
    scanner = StorageMetrics()
    data = scanner.measure([spec(folder, 'a')], [{'id': 'a', 'name': 'A'}])
    assert data['scope'][0]['status'] == 'blocked' and data['files_counted'] == 0
    config = tmp_path / 'config.toml'
    config.write_text('', encoding='utf8')
    items = _specs(config, {'storage': {'state_file': str(tmp_path / 'resolved.db')}}, (('storage', 'state_file', 'state'),))
    data = scanner.measure(items, [])
    assert data['scope'][0]['status'] == 'not_provided' and data['shared']['logical_bytes'] is None
    data = scanner.measure([spec(Path(tmp_path.anchor), 'a')], [{'id': 'a', 'name': 'A'}])
    assert data['scope'][0]['status'] == 'blocked' and data['entries_examined'] == 0
