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
