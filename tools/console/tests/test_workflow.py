import json
from types import SimpleNamespace

from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from workflow import MAX_LINE, register_workflow_routes


def client_for(tmp_path):
    app = FastAPI()
    def authenticate(x_console_token: str = Header('')):
        if x_console_token != 'fixture':
            raise HTTPException(401)
    register_workflow_routes(app, SimpleNamespace(directory=tmp_path), authenticate)
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
        stream.write('"run_end"}\ninvalid json\n')
    final = client.get(f'/api/jobs/{identifier}/workflow?cursor={second["next_cursor"]}').json()
    assert [event['type'] for event in final['events']] == ['run_end', 'record_unavailable']
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
