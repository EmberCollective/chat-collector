import json
from types import SimpleNamespace

from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from workflow import MAX_LINE, register_workflow_routes


def client_for(tmp_path, processor_history=None):
    app = FastAPI()
    def authenticate(x_console_token: str = Header('')):
        if x_console_token != 'fixture':
            raise HTTPException(401)
    register_workflow_routes(app, SimpleNamespace(directory=tmp_path), authenticate, processor_history)
    return TestClient(app, headers={'X-Console-Token': 'fixture'})


def save_job(tmp_path, number=1):
    identifier = f'{number:032x}'
    folder = tmp_path / identifier
    folder.mkdir()
    (folder / 'job.json').write_text(json.dumps({'id': identifier, 'module': 'processor', 'action': 'process',
        'status': 'succeeded', 'started_at': number}), encoding='utf-8')
    return identifier, folder


def test_auth_and_durable_history_beyond_memory(tmp_path):
    client = client_for(tmp_path)
    identifier, folder = save_job(tmp_path)
    assert client.get('/api/workflow/runs', headers={'X-Console-Token': ''}).status_code == 401
    assert client.get(f'/api/jobs/{identifier}/workflow', headers={'X-Console-Token': ''}).status_code == 401
    for number in range(2, 106):
        save_job(tmp_path, number)
    response = client.get('/api/workflow/runs?cursor=100&limit=5').json()
    assert len(response['runs']) == 5 and response['runs'][-1]['id'] == identifier
    assert not response['has_more']
    missing = client.get(f'/api/jobs/{identifier}/workflow').json()
    assert missing['recorded'] is False and missing['events'] == []
    assert client.get('/api/jobs/not-a-job/workflow').status_code == 404


def test_pi_list_skips_unrelated_result_bodies_before_reading(tmp_path, monkeypatch):
    from pathlib import Path
    identifier, folder = save_job(tmp_path)
    unrelated = tmp_path / ('b' * 32)
    unrelated.mkdir()
    (unrelated / 'job.json').write_text(json.dumps({'module': 'personal', 'action': 'collect', 'status': 'succeeded'}))
    result = unrelated / 'result.json'
    result.write_text('{"status":"succeeded","result":{"messages":["PRIVATE BODY"]}}')
    original = Path.read_text
    def read(path, *args, **kwargs):
        assert path != result, 'Pi list must not parse collection result bodies'
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', read)
    assert client_for(tmp_path).get('/api/workflow/runs').json()['runs'][0]['id'] == identifier


def test_pagination_partial_append_unicode_and_invalid_record(tmp_path):
    client = client_for(tmp_path)
    identifier, folder = save_job(tmp_path)
    path = folder / 'pi-workflow.jsonl'
    events = [{'type': 'model_output', 'run_id': 'batch', 'seq': n, 'body': {'text': '完整上下文'}} for n in range(1, 4)]
    complete = ''.join(json.dumps(event, ensure_ascii=False)+'\n' for event in events)
    path.write_text(complete + '{"type":', encoding='utf-8')
    first = client.get(f'/api/jobs/{identifier}/workflow?limit=2').json()
    assert len(first['events']) == 2 and first['has_more']
    second = client.get(f'/api/jobs/{identifier}/workflow?cursor={first["next_cursor"]}').json()
    assert second['events'][0]['seq'] == 3 and not second['has_more'] and second['pending']
    assert second['next_cursor'] == path.read_bytes().rfind(b'\n') + 1
    with path.open('a', encoding='utf-8') as stream:
        stream.write('"run_end"}\ninvalid json\n{"type":null}\n')
    final = client.get(f'/api/jobs/{identifier}/workflow?cursor={second["next_cursor"]}').json()
    assert [event['type'] for event in final['events']] == ['run_end', 'record_unavailable', 'record_unavailable']
    assert not final['has_more'] and not final['pending']
    assert client.get(f'/api/jobs/{identifier}/workflow?cursor=1').status_code == 422
    assert client.get(f'/api/jobs/{identifier}/workflow?cursor=999999999').status_code == 422


def test_oversized_record_is_explicit_and_later_events_survive(tmp_path):
    client = client_for(tmp_path)
    identifier, folder = save_job(tmp_path)
    (folder / 'pi-workflow.jsonl').write_text('X' * (MAX_LINE + 5) + '\n{"type":"run_end"}\n', encoding='utf-8')
    events = client.get(f'/api/jobs/{identifier}/workflow').json()['events']
    assert events[0]['body']['omitted'] == 'oversized_or_invalid_record'
    assert events[1]['type'] == 'run_end'


def test_workflow_cannot_follow_a_record_link_outside_run(tmp_path):
    import os
    import pytest
    client = client_for(tmp_path)
    identifier, folder = save_job(tmp_path)
    outside = tmp_path / 'private.jsonl'
    outside.write_text('{"private":"contents"}\n', encoding='utf-8')
    try:
        os.symlink(outside, folder / 'pi-workflow.jsonl')
    except OSError:
        pytest.skip('Creating symbolic links requires Windows developer mode')
    assert client.get(f'/api/jobs/{identifier}/workflow').status_code == 404


def test_background_history_survives_console_restart_and_checks_process_identity(tmp_path):
    import os
    import psutil
    background = tmp_path / 'background'
    background.mkdir()
    identifier, folder = save_job(background, 200)
    path = folder / 'job.json'
    metadata = json.loads(path.read_text())
    metadata.update(status='running', origin='cli', pid=os.getpid(), create_time=psutil.Process().create_time())
    path.write_text(json.dumps(metadata), encoding='utf-8')
    (folder / 'pi-workflow.jsonl').write_text('{"type":"model_output","body":{"content":[]}}\n')
    client = client_for(tmp_path, lambda: background)
    response = client.get('/api/workflow/runs').json()
    assert response['runs'][0]['id'] == identifier and response['runs'][0]['status'] == 'running'
    assert response['runs'][0]['origin'] == 'cli'
    assert 'pid' not in response['runs'][0] and str(background) not in json.dumps(response)
    detail = client.get(f'/api/jobs/{identifier}/workflow').json()
    assert detail['recorded'] and detail['events'][0]['type'] == 'model_output'
    assert client.get(f'/api/jobs/{identifier}/workflow', headers={'X-Console-Token': ''}).status_code == 401
    metadata['create_time'] -= 100  # The PID alone must not make an orphaned run look active.
    path.write_text(json.dumps(metadata))
    restarted = client_for(tmp_path, background)
    assert restarted.get(f'/api/jobs/{identifier}/workflow').json()['job']['status'] == 'interrupted'


def test_conflicting_ids_and_unavailable_background_configuration(tmp_path):
    identifier, _ = save_job(tmp_path)
    background = tmp_path / 'background'
    background.mkdir()
    save_job(background)
    client = client_for(tmp_path, background)
    assert client.get('/api/workflow/runs').status_code == 409
    assert client.get(f'/api/jobs/{identifier}/workflow').status_code == 409
    def unavailable():
        raise ValueError('PRIVATE CONFIGURATION PATH')
    response = client_for(tmp_path, unavailable).get('/api/workflow/runs')
    assert response.status_code == 200 and response.json()['runs'][0]['id'] == identifier
    assert 'PRIVATE' not in response.text
