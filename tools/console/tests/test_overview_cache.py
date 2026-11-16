import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from overview import Overview
from test_console import console
from test_overview import save_job, valid


def test_reload_reuses_snapshot_but_new_records_and_results_invalidate(console, monkeypatch):
    client, app, root, now = console
    save_job(app, 'a' * 32, 'wechat', now - 20, valid(4))
    service = app.state.overview
    original, calls = service._snapshot, []
    def counted(hours):
        calls.append(hours)
        return original(hours)
    monkeypatch.setattr(service, '_snapshot', counted)
    service.invalidate()
    first = client.get('/api/overview').json()
    assert first['summary']['emitted'] == 4
    assert client.get('/api/overview').json()['generated_at'] == first['generated_at']
    # The first pass discovers record paths; the next pass stabilizes their versions.
    assert len(calls) <= 2
    folder = save_job(app, 'b' * 32, 'qq', now - 10, valid(3))
    assert client.get('/api/overview').json()['summary']['emitted'] == 7
    (folder / 'result.json').write_text(json.dumps({'status': 'succeeded', 'result': valid(8)}))
    assert client.get('/api/overview').json()['summary']['emitted'] == 12


def test_summary_cache_does_not_retain_message_or_context_bodies(console):
    _, app, _, now = console
    payload = {**valid(2), 'messages': ['PRIVATE CHAT BODY'], 'context': 'PRIVATE MODEL CONTEXT'}
    folder = save_job(app, 'a' * 32, 'wechat', now - 20, payload)
    service = app.state.overview
    projected = service._read_record(folder / 'result.json')
    assert projected['result'] == valid(2)
    assert 'PRIVATE' not in repr(service._records)
    projected['result']['emitted'] = 99
    assert service._read_record(folder / 'result.json')['result']['emitted'] == 2


def test_wal_changes_invalidate_and_concurrent_windows_only_rebuild_once(tmp_path, monkeypatch):
    bridge = SimpleNamespace(root=tmp_path, settings={}, sources={}, paths={}, settings_path=tmp_path / 'console.toml')
    jobs = SimpleNamespace(directory=tmp_path / 'jobs', mutex=threading.RLock(), jobs={})
    service = Overview(bridge, jobs)
    db = tmp_path / 'state.db'
    with sqlite3.connect(db) as conn:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('CREATE TABLE samples(n)')
        conn.commit()
        service._tracked.add(Path(str(db) + '-wal'))
        calls = []
        def snapshot(hours):
            calls.append(hours)
            return {'value': conn.execute('SELECT count(*) FROM samples').fetchone()[0]}
        monkeypatch.setattr(service, '_snapshot', snapshot)
        assert service.snapshot(24)['value'] == 0
        conn.execute('INSERT INTO samples VALUES(1)')
        conn.commit()
        assert service.snapshot(24)['value'] == 1
        assert calls == [24, 24]
    service.invalidate()
    monkeypatch.setattr(service, '_snapshot', lambda hours: calls.append(hours) or {'value': 1})
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert [result['value'] for result in pool.map(service.snapshot, [24] * 8)] == [1] * 8
    assert calls == [24, 24, 24]
    for hours in range(1, 8):
        service.snapshot(hours)
    assert len(service._cache) == 4
