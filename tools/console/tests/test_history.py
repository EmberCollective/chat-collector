import json
import os
from types import SimpleNamespace

from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
import pytest

import history
from history import register_history_routes


def client_for(tmp_path, processor_history=None):
    directory = tmp_path/'console'
    directory.mkdir()
    jobs = SimpleNamespace(directory=directory, running={}, jobs={})
    app = FastAPI()
    def authenticate(x_console_token: str = Header('')):
        if x_console_token != 'fixture':
            raise HTTPException(401)
    register_history_routes(app, jobs, authenticate, processor_history)
    return TestClient(app, headers={'X-Console-Token': 'fixture'}), jobs


def save_job(root, number=1, **values):
    identifier = f'{number:032x}'
    folder = root/identifier
    folder.mkdir(parents=True)
    record = {'id': identifier, 'module': 'wechat-work', 'action': 'collect', 'origin': 'manual',
              'status': 'succeeded', 'started_at': number, 'finished_at': number+10, **values}
    (folder/'job.json').write_text(json.dumps(record), encoding='utf-8')
    return identifier, folder


def test_authenticated_durable_pagination_is_independent_of_recent_cache(tmp_path):
    client, jobs = client_for(tmp_path)
    assert client.get('/api/jobs', headers={'X-Console-Token': ''}).status_code == 401
    first, _ = save_job(jobs.directory, private='never-output-this', result={'private': 'never-output-this'})
    for number in range(2, 106):
        save_job(jobs.directory, number)
    result = client.get('/api/jobs?cursor=100&limit=5').json()
    assert result['total'] == 105 and len(result['jobs']) == 5
    assert result['jobs'][-1]['id'] == first and result['next_cursor'] == 105 and not result['has_more']
    assert 'never-output-this' not in json.dumps(result)
    assert set(result['jobs'][-1]) == {'id', 'module', 'action', 'origin', 'status', 'started_at',
                                      'finished_at', 'error', 'duration_seconds', 'duration_complete'}
    assert jobs.jobs == {}
    assert client.get('/api/jobs?cursor=500').json()['jobs'] == []


def test_filters_combine_and_facets_keep_values_from_unfiltered_history(tmp_path):
    client, jobs = client_for(tmp_path)
    base = {'module': 'mail-work', 'origin': 'schedule', 'started_at': 100, 'finished_at': 106,
            'status': 'failed', 'error': 'Snapshot TIMEOUT'}
    wanted, folder = save_job(jobs.directory, 1, **base)
    (folder/'result.json').write_text('{"status":"failed","error":"Snapshot TIMEOUT","private":"never-return"}', encoding='utf-8')
    variants = [{'action': 'preview'}, {'origin': 'manual'}, {'module': 'qq-other'}, {'status': 'succeeded'},
                {'finished_at': 110}, {'started_at': 90, 'finished_at': 96}, {'error': 'different failure'}]
    for number, difference in enumerate(variants, 2):
        save_job(jobs.directory, number, **{**base, **difference})
    params = {'action': 'collect', 'module': 'mail-work', 'origin': 'schedule', 'status': 'failed',
              'min_duration_seconds': 4, 'max_duration_seconds': 7, 'start': 100, 'end': 200, 'error': 'timeout'}
    result = client.get('/api/jobs', params=params).json()
    assert result['total'] == 1 and result['jobs'][0]['id'] == wanted
    assert result['facets'] == {'actions': ['collect', 'preview'], 'modules': ['mail-work', 'qq-other'],
                                 'origins': ['manual', 'schedule']}
    assert 'never-return' not in json.dumps(result)
    assert client.get('/api/jobs', params={'start': 90, 'end': 100}).json()['total'] == 1
    assert client.get('/api/jobs', params={'module': '../private'}).json()['total'] == 0


def test_duration_keeps_unknown_end_distinct_from_active_elapsed(tmp_path, monkeypatch):
    monkeypatch.setattr(history.time, 'time', lambda: 1000)
    client, jobs = client_for(tmp_path)
    active, _ = save_job(jobs.directory, 1, started_at=990, finished_at=None, status='running')
    stale, _ = save_job(jobs.directory, 2, started_at=900, finished_at=None, status='running')
    interrupted, _ = save_job(jobs.directory, 3, started_at=901, finished_at=None, status='interrupted')
    unknown, _ = save_job(jobs.directory, 4, started_at=902, finished_at=None, status='new_external_state')
    complete, _ = save_job(jobs.directory, 5, started_at=980, finished_at=985)
    invalid, _ = save_job(jobs.directory, 6, started_at=float('nan'), finished_at=float('inf'))
    backwards, _ = save_job(jobs.directory, 7, started_at=10, finished_at=5)
    jobs.running = {'wechat-work': active}
    result = client.get('/api/jobs?sort=duration_asc').json()
    records = {item['id']: item for item in result['jobs']}
    assert [item['id'] for item in result['jobs'][:2]] == [complete, active]
    assert records[active]['duration_seconds'] == 10 and not records[active]['duration_complete']
    assert records[complete]['duration_seconds'] == 5 and records[complete]['duration_complete']
    for identifier in (stale, interrupted, unknown, invalid, backwards):
        assert records[identifier]['duration_seconds'] is None and not records[identifier]['duration_complete']
    assert records[stale]['status'] == 'interrupted'
    assert records[invalid]['started_at'] is None and records[invalid]['finished_at'] is None
    descending = client.get('/api/jobs?sort=duration_desc').json()['jobs']
    assert [item['id'] for item in descending[:2]] == [active, complete]
    filtered = client.get('/api/jobs?min_duration_seconds=0&max_duration_seconds=10').json()['jobs']
    assert {item['id'] for item in filtered} == {active, complete}


def test_background_runs_are_included_only_from_configured_history(tmp_path, monkeypatch):
    background = tmp_path/'configured-pi-runs'
    client, jobs = client_for(tmp_path, lambda: background)
    console, _ = save_job(jobs.directory, 1)
    automatic, _ = save_job(background, 2, module='processor', action='process', origin='automation',
                             started_at=500, finished_at=None, status='running', pid=123, create_time=100)
    save_job(tmp_path/'not-configured', 3, module='processor')
    import workflow
    monkeypatch.setattr(workflow.psutil, 'Process', lambda pid: SimpleNamespace(
        create_time=lambda: 100, is_running=lambda: True, status=lambda: 'running'))
    result = client.get('/api/jobs').json()
    assert {item['id'] for item in result['jobs']} == {console, automatic}
    item = next(item for item in result['jobs'] if item['id'] == automatic)
    assert item['status'] == 'running' and not item['duration_complete']
    assert 'pid' not in item and 'create_time' not in item


def test_corrupt_records_and_broken_processor_config_do_not_hide_console_history(tmp_path):
    def unavailable(): raise ValueError('processor configuration invalid')
    client, jobs = client_for(tmp_path, unavailable)
    wanted, _ = save_job(jobs.directory)
    _, bad = save_job(jobs.directory, 2)
    (bad/'job.json').write_text('not json', encoding='utf-8')
    (jobs.directory/'not-a-run').mkdir()
    (jobs.directory/'not-a-run'/'job.json').write_text('{"secret":"must not read"}', encoding='utf-8')
    result = client.get('/api/jobs').json()
    assert result['total'] == 1 and result['jobs'][0]['id'] == wanted


def test_colliding_identifiers_fail_instead_of_linking_the_wrong_run(tmp_path):
    background = tmp_path/'pi-runs'
    client, jobs = client_for(tmp_path, background)
    save_job(jobs.directory, 1)
    save_job(background, 1, module='processor')
    assert client.get('/api/jobs').status_code == 409


def test_record_links_outside_run_roots_are_never_returned(tmp_path):
    client, jobs = client_for(tmp_path)
    identifier, folder = save_job(tmp_path/'private', 1)
    try:
        os.symlink(folder, jobs.directory/identifier, target_is_directory=True)
    except OSError:
        pytest.skip('Creating symbolic links requires Windows developer mode')
    assert client.get('/api/jobs').json()['total'] == 0


@pytest.mark.parametrize('query', ['cursor=-1', 'limit=101', 'min_duration_seconds=-1',
    'max_duration_seconds=NaN', 'max_duration_seconds=Infinity', 'min_duration_seconds=8&max_duration_seconds=7',
    'start=20&end=10', 'start=20&end=20', 'sort=unsupported'])
def test_invalid_query_never_reads_history(tmp_path, query):
    client, _ = client_for(tmp_path)
    assert client.get('/api/jobs?'+query).status_code == 422
