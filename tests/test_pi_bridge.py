"""Exercise the real Pi SDK subprocess against a local synthetic OpenAI endpoint."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import pytest

from processor.pi_bridge import run_agent, runtime_status


pytestmark = pytest.mark.skipif(not runtime_status()['installed'], reason='Pinned Pi runtime is not installed')


def task(sources):
    return {'task_id': None, 'title': 'Submit report', 'event': 'Submit the report tomorrow',
            'priority': 'P2', 'priority_reason': 'Explicit requirement', 'status': 'open',
            'time': {'start': None, 'deadline': '2026-11-13', 'text': 'tomorrow'},
            'people': [], 'location': None, 'needs_review': True,
            'review_reasons': ['Owner unknown'], 'sources': sources}


def send_tool(handler, name, arguments):
    handler.send_response(200)
    handler.send_header('Content-Type', 'text/event-stream')
    handler.end_headers()
    common = {'id': 'fixture', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'fixture'}
    for delta, reason in [({'role': 'assistant', 'tool_calls': [{'index': 0, 'id': 'fixture-call',
                         'type': 'function', 'function': {'name': name, 'arguments': json.dumps(arguments)}}]}, None),
                         ({}, 'tool_calls')]:
        frame = {**common, 'choices': [{'index': 0, 'delta': delta, 'finish_reason': reason}]}
        handler.wfile.write(('data: ' + json.dumps(frame) + '\n\n').encode())
    handler.wfile.write(b'data: [DONE]\n\n')


@pytest.fixture
def endpoint():
    requests = []
    responder = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append(body)
            responder[0](self, len(requests))
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = {'model': {'base_url': f'http://127.0.0.1:{server.server_port}/v1', 'model': 'fixture',
                       'timeout_seconds': 10, 'vision': False, 'api_key_env': 'PI_FIXTURE_API_KEY'},
              'agent': {'max_turns': 5, 'max_tool_calls': 5}}
    payload = {'conversations': [{'id': 'c1', 'segments': [{'id': 's1', 'messages': [{'id': 'm1',
        'text': 'See report', 'time': '2026-11-12T10:00:00+08:00', 'attachments': ['f1']}]}]}],
        'attachments': [{'id': 'f1', 'name': 'report.pdf', 'page_count': 2}]}
    try:
        yield config, payload, responder, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_real_pi_subprocess_reads_allowed_attachment_and_submits(endpoint, monkeypatch, tmp_path):
    config, payload, responder, requests = endpoint
    progress = tmp_path/'pi-events.jsonl'
    config['_progress_file'] = str(progress)
    responder.append(lambda handler, n: send_tool(handler, 'read_attachment', {'ref': 'f1', 'pages': '2'})
                     if n == 1 else send_tool(handler, 'submit_tasks', {'tasks': [task(['m1', 'f1:p2'])]}))
    seen = []
    def reader(ref, pages=None, query=None):
        assert 'tool_execution_start' in progress.read_text(encoding='utf-8')
        seen.append((ref, pages, query))
        return {'content': [{'type': 'text', 'text': '[f1:p2] Submit report tomorrow'}],
                'details': {'status': 'read', 'evidence': [{'ref': 'f1:p2', 'page': 2}]}}
    monkeypatch.setenv('PI_FIXTURE_API_KEY', 'fixture-secret')
    result = run_agent(config, 'Read the allowed attachment then submit tasks.', payload,
                       api_key='fixture-secret', attachment_reader=reader)
    assert result['tasks'][0]['sources'] == ['m1', 'f1:p2']
    assert result['_agent']['version'] == '1.5.2'
    assert result['_agent']['tool_calls'] == 2
    assert seen == [('f1', '2', None)]
    assert len(requests) == 2
    assert 'fixture-secret' not in json.dumps(requests)
    events = [json.loads(line) for line in progress.read_text(encoding='utf-8').splitlines()]
    assert events[-1]['type'] == 'run_end'
    assert any(e.get('tool') == 'read_attachment' and e.get('pages') == '2' for e in events)
    assert len({e['run_id'] for e in events}) == 1
    assert 'fixture-secret' not in json.dumps(events)
    assert 'Submit report tomorrow' not in json.dumps(events)


def test_attachment_exception_is_redacted_before_returning_to_model(endpoint):
    config, payload, responder, requests = endpoint
    responder.append(lambda handler, n: send_tool(handler, 'read_attachment', {'ref': 'f1'})
                     if n == 1 else send_tool(handler, 'submit_tasks', {'tasks': []}))
    def reader(**kwargs):
        raise RuntimeError('PRIVATE-PATH fixture-secret internal parser contents')
    result = run_agent(config, 'Read attachment and submit.', payload, api_key='fixture-secret', attachment_reader=reader)
    assert result['tasks'] == []  # The caller separately checks unresolved attachment coverage.
    assert 'PRIVATE-PATH' not in json.dumps(requests)
    assert 'fixture-secret' not in json.dumps(requests)


@pytest.mark.parametrize('same_message', [False, True])
def test_native_pi_selects_renamed_duplicate_material_without_changing_its_source(endpoint, tmp_path, same_message):
    from processor import context
    from test_processor_context import event, group
    config, _, responder, requests = endpoint
    archive = tmp_path/'archive'
    archive.mkdir()
    path = archive/'content.txt'
    path.write_text('Submit the report tomorrow.', encoding='utf-8')
    config.update(storage={'state_file': str(tmp_path/'state.db')}, context={'timezone': 'Asia/Shanghai'},
                  processing={'max_input_chars': 100000},
                  attachments={'archive_roots': [str(archive)], 'cache_dir': str(tmp_path/'cache')})
    first = {'name': 'template.txt', 'archive_path': str(path)}
    second = dict(first, name='renamed-deliverable.txt')
    records = ([event('a', attachments=[first, second])] if same_message else
               [event('a', attachments=[first]), event('b', attachments=[second])])
    source = 'm1' if same_message else 'm2'
    responder.append(lambda h, n: send_tool(h, 'read_attachment', {'ref': 'f2'}) if n == 1 else
                     send_tool(h, 'submit_tasks', {'tasks': [dict(task([source, 'f2:b1']), material_refs=['f2'])]}))
    result = context.process_batch(config, {'text': 'Extract explicit tasks.'}, [group(*records)], [])
    selected = result['group-id']['tasks'][0]['materials']
    assert len(selected) == 1 and selected[0]['attachment']['name'] == second['name']
    assert selected[0]['message_key'] == records[-1]['message_key']
    assert selected[0]['attachment_index'] == (1 if same_message else 0)
    prompt = json.loads(requests[0]['messages'][-1]['content'][0]['text'])
    assert [(item['ref'], item['name']) for item in prompt['attachments']] == [
        ('f1', first['name']), ('f2', second['name'])]


def test_unknown_source_rejected_across_python_node_protocol(endpoint):
    config, payload, responder, _requests = endpoint
    responder.append(lambda handler, n: send_tool(handler, 'submit_tasks', {'tasks': [task(['invented'])]}))
    with pytest.raises(RuntimeError, match='unknown source'):
        run_agent(config, 'Submit tasks.', payload, api_key='fixture-secret')


def test_native_pi_can_correct_rejected_submission_without_restarting(endpoint, tmp_path):
    from processor import context
    from test_processor_context import event, group
    config, payload, responder, requests = endpoint
    config.update(storage={'state_file': str(tmp_path/'state.db')},
                  context={'timezone':'Asia/Shanghai'}, processing={'max_input_chars':100000},
                  attachments={'archive_roots':[], 'cache_dir':str(tmp_path/'cache')},
                  _progress_file=str(tmp_path/'events.jsonl'))
    groups = [group(event('a'), identifier='one'),
              group(event('b', conversation='another'), identifier='two')]
    responder.append(lambda handler, n: send_tool(handler, 'submit_tasks',
        {'tasks':[task(['m1','m2'])]} if n == 1 else {'tasks':[task(['m1']),task(['m2'])]}))
    result = context.process_batch(config, {'text':'Extract explicit tasks.'}, groups, [])
    assert len(result['one']['tasks']) == len(result['two']['tasks']) == 1
    assert len(requests) == 2
    feedback = json.dumps(requests[1]['messages'])
    assert 'submission_rejected' in feedback and 'source_conversations' in feedback
    assert all(e['message_key'] not in feedback for g in groups for e in g['messages'])
    events = [json.loads(line) for line in (tmp_path/'events.jsonl').read_text().splitlines()]
    outcomes = [e for e in events if e['type']=='tool_execution_end' and e['tool']=='submit_tasks']
    assert [e['failed'] for e in outcomes] == [True,False]
    assert events[-1]['type']=='run_end' and events[-1]['failed'] is False


def test_live_task_lookup_registers_new_reference_for_native_submission(endpoint):
    config,payload,responder,requests=endpoint
    filters={'platforms':['wechat'],'people':['张老师'],'time_from':'2026-11-01','time_to':'2026-12-01','offset':40}
    calls=[]
    def finder(**args):
        calls.append(args)
        return {'tasks':[dict(task(['m1']),task_id='t71',updatable=True)],'has_more':True,'next_offset':60}
    changed=dict(task(['m1']),task_id='t71')
    responder.append(lambda h,n:send_tool(h,'find_related_tasks',filters) if n==1 else send_tool(h,'submit_tasks',{'tasks':[changed]}))
    result=run_agent(config,'Search then update the matching task.',payload,task_finder=finder)
    assert calls==[filters]
    assert result['tasks'][0]['task_id']=='t71'
    assert 'next_offset' in json.dumps(requests[1])


def test_task_search_failure_is_visible_without_private_details(endpoint):
    config,payload,responder,requests=endpoint
    responder.append(lambda h,n:send_tool(h,'find_related_tasks',{}) if n==1 else send_tool(h,'submit_tasks',{'tasks':[]}))
    def finder(**args):raise RuntimeError('PRIVATE DATABASE PATH')
    run_agent(config,'Search then submit.',payload,task_finder=finder)
    assert 'task_search_failed' in json.dumps(requests[1])
    assert 'PRIVATE DATABASE PATH' not in json.dumps(requests)


def shared_task_batch(endpoint, tmp_path, monkeypatch):
    from processor import core
    from test_processor import batch, config as make_config, event, response
    transport, _, responder, requests = endpoint
    config = make_config.__wrapped__(tmp_path)
    config['model'].update(transport['model'])
    config['model']['api_key_env'] = ''
    config['agent'].update(transport['agent'], batching='auto')
    batch(config, 'initial', event('original', conversation='original-room'))
    with monkeypatch.context() as patch:
        patch.setattr(core, '_call_model', lambda *args: response(['original']))
        core.run_round(config)
    original = core.list_tasks(config)[0]
    archive = tmp_path/'archive'
    archive.mkdir()
    path = archive/'shared.txt'
    path.write_text('Shared task material.', encoding='utf-8')
    config['attachments']['archive_roots'] = [str(archive)]
    records = [event('a', timestamp=1000, conversation='first-room', text='Move the report deadline to 2026-11-20.'),
               event('b', timestamp=2000, platform='wechat', conversation='second-room',
                     text='The same report deadline is now 2026-12-01; replace the earlier date.')]
    for record in records:
        record['message']['attachments'] = [{'name': record['message_key']+'.txt', 'archive_path': str(path)}]
    batch(config, 'followup', *records)
    return config, original, responder, requests


@pytest.mark.parametrize('rejection', ['conflicting_fields', 'same_conversation_duplicate'])
def test_native_pi_merges_shared_task_sources_after_correctable_submission_and_human_edits(endpoint, tmp_path, monkeypatch, rejection):
    from processor import core, service
    config, original, responder, requests = shared_task_batch(endpoint, tmp_path, monkeypatch)
    actor = {'platform': 'test', 'user_id': 'verified', 'source': 'test'}
    final = dict(task(['m1']), task_id='t1', title='Updated shared report', material_refs=['f1'])
    final['time'] = dict(final['time'], deadline='2026-12-01')
    second = dict(final, sources=['m2'], material_refs=['f2'])
    def respond(handler, n):
        if n == 1:
            return send_tool(handler, 'find_related_tasks', {'query': 'report'})
        if n == 2:
            service.update_task_status(config, original['task_id'], 'completed', request_id='human:status', actor=actor)
            service.update_task_priority(config, original['task_id'], 'P0', request_id='human:priority', actor=actor)
            invalid = (dict(second, time=dict(second['time'], deadline='2026-11-20'))
                       if rejection == 'conflicting_fields' else dict(final))
            return send_tool(handler, 'submit_tasks', {'tasks': [final, invalid]})
        send_tool(handler, 'submit_tasks', {'tasks': [final, second]})
    responder.append(respond)
    result = core.run_round(config)
    saved = service.get_task(config, original['task_id'])
    assert result['processed_groups'] == result['tasks_upserted'] == 2
    assert len(core.list_tasks(config)) == 1 and saved['revision'] == 5
    assert saved['status'] == 'completed' and saved['priority'] == 'P0'
    assert set(saved['source_message_keys']) == {'original', 'a', 'b'}
    assert {source['collector_module'] for source in saved['sources']} == {'qq', 'wechat'}
    assert {item['attachment']['name'] for item in saved['materials']} == {'a.txt', 'b.txt'}
    assert saved['title'] == final['title'] and saved['time']['deadline'] == final['time']['deadline']
    assert len(requests) == 3 and 'submission_rejected' in json.dumps(requests[2])
    assert core.run_round(config)['processed_groups'] == 0 and len(requests) == 3


@pytest.mark.parametrize('failure', ['interrupted', 'competing_content_write'])
def test_shared_task_batch_retries_only_uncommitted_group_and_rejects_external_content_change(endpoint, tmp_path, monkeypatch, failure):
    from processor import core, service, storage
    config, original, responder, requests = shared_task_batch(endpoint, tmp_path, monkeypatch)
    final = dict(task(['m1']), task_id='t1', title='Final shared report')
    def respond(handler, n):
        if n % 2:
            return send_tool(handler, 'find_related_tasks', {'query': 'report'})
        send_tool(handler, 'submit_tasks', {'tasks': [final, dict(final, sources=['m2'])] if n == 2 else [final]})
    responder.append(respond)
    apply = service.apply_extraction
    calls = []
    def interrupted(*args, **kwargs):
        calls.append(kwargs['context']['group']['conversation_id'])
        if len(calls) == 2 and failure == 'interrupted':
            raise RuntimeError('injected_interruption')
        result = apply(*args, **kwargs)
        if len(calls) == 1 and failure == 'competing_content_write':
            current = service.get_task(config, original['task_id'])
            current['title'] = 'Independent writer report'
            with service._task_connection(config) as conn, conn:
                storage._save_task(conn, current)
        return result
    with monkeypatch.context() as patch:
        patch.setattr(service, 'apply_extraction', interrupted)
        with pytest.raises((RuntimeError, service.TaskStatusConflict), match='injected_interruption|task_revision_conflict'):
            core.run_round(config)
    first = service.get_task(config, original['task_id'])
    assert set(first['source_message_keys']) == {'original', 'a'} and len(core.list_tasks(config)) == 1
    if failure == 'competing_content_write':
        assert first['title'] == 'Independent writer report'
    result = core.run_round(config)
    saved = service.get_task(config, original['task_id'])
    assert result['processed_groups'] == 1 and saved['revision'] == first['revision'] + 1
    assert set(saved['source_message_keys']) == {'original', 'a', 'b'} and len(core.list_tasks(config)) == 1
    assert len(requests) == 4 and core.run_round(config)['processed_groups'] == 0
