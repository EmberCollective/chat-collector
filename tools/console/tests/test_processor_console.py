from pathlib import Path
import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import pytest

from test_console import console, wait_job


def test_preview_worker_is_read_only_and_unconfigured_run_is_blocked(console, monkeypatch):
    client, app, root, _ = console
    import urllib.request
    def no_network(*args, **kwargs):
        raise AssertionError('Preview must never request a model')
    monkeypatch.setattr(urllib.request, 'urlopen', no_network)
    batches = {str(path): path.read_bytes() for path in (root/'wechat/batches').glob('*')}
    status = client.get('/api/processor/status').json()
    assert status['model']['configured'] is False
    assert 'pending_groups' in status['counts']
    assert client.get('/api/processor/tasks').json() == {'tasks': []}
    assert client.post('/api/processor/run').status_code == 400
    assert client.post('/api/processor/preview', json={'limit':21}).status_code == 422
    job = client.post('/api/processor/preview', json={'limit':5})
    assert job.status_code == 202, job.text
    result = wait_job(client, job.json()['id'])['result']
    assert result['groups'][0]['messages'][0]['message']['text'].startswith('deadline <img')
    group=result['groups'][0]
    assert group['model_input']['conversations'][0]['segments'][0]['messages'][0]['id'] == 'm1'
    assert group['messages'][0]['message_key'] not in json.dumps(group['model_input'])
    assert group['source_map']['m1']['message_key'] == group['messages'][0]['message_key']
    planned=result['batch_plan'][0]
    assert planned['model_input']['conversations'][0]['segments'][0]['messages'][0]['id'] == 'm1'
    assert group['messages'][0]['message_key'] not in json.dumps(planned['model_input'])
    assert result['pending_groups'] == 1
    assert not (root/'processing/state.db').exists()
    assert not (root/'processing/todos').exists()
    assert batches == {str(path): path.read_bytes() for path in (root/'wechat/batches').glob('*')}
    assert set(app.state.jobs.enabled) == {'wechat', 'qq'}


def test_processing_config_and_rules_backups_conflicts_and_locks(console, monkeypatch):
    client, app, root, _ = console
    base = '/api/processor'
    rules = client.get(base+'/rules').json()
    before = (root/'processing/rules.md').read_bytes()
    body = {'revision':rules['revision'], 'text':'# 新规则\n只提取明确的事项。\n'}
    response = client.put(base+'/rules', json=body)
    assert response.status_code == 200, response.text
    assert Path(response.json()['backup']).read_bytes() == before
    assert client.put(base+'/rules', json=body).status_code == 409
    assert client.put(base+'/rules', json={'revision':response.json()['revision'], 'text':'  '}).status_code == 400
    with app.state.bridge.lock(root/'processing/state.db'):
        assert client.post(base+'/preview', json={'limit':5}).status_code == 409
        assert client.put(base+'/rules', json={'revision':response.json()['revision'], 'text':'busy'}).status_code == 409
    config = client.get(base+'/config').json()
    saved = client.patch(base+'/config', json={'revision':config['revision'], 'patch':{
        'model':{'base_url':'https://example.invalid/v1','model':'fixture-model'},
        'context':{'history_messages':3},'source_descriptions':{'wechat':'我的来源说明'}}})
    assert saved.status_code == 200, saved.text
    assert Path(saved.json()['backup']).read_text(encoding='utf-8').startswith('# processor fixture')
    assert saved.json()['config']['context']['history_messages'] == 3
    assert client.patch(base+'/config', json={'revision':config['revision'],'patch':{}}).status_code == 409
    revision = saved.json()['revision']
    for patch in ({'model':{'api_key':'never-store-a-key'}}, {'model':{'api_key_file':'elsewhere'}},
                  {'storage':{'state_file':'lost.db'}}, {'context':{'max_messages':False}}):
        assert client.patch(base+'/config', json={'revision':revision,'patch':patch}).status_code == 422
    assert client.patch(base+'/config', json={'revision':revision,'patch':{'model':{'base_url':'https://user:secret@example.com'}}}).status_code == 400
    assert client.get(base+'/config').json()['revision'] == revision
    monkeypatch.setenv('CONSOLE_TEST_PROCESSOR_KEY', 'private-env-value')
    (root/'processing/secret.txt').write_text('private-file-value', encoding='utf-8')
    for endpoint in ('config', 'status', 'tasks'):
        result = client.get(base+'/'+endpoint)
        assert result.status_code == 200
        assert 'private-env-value' not in result.text and 'private-file-value' not in result.text
    app.state.jobs.running['processor'] = 'fixture'
    try:
        assert client.patch(base+'/config', json={'revision':revision, 'patch':{}}).status_code == 409
    finally:
        del app.state.jobs.running['processor']


def test_manual_context_time_uses_current_sql_without_changing_defaults_or_state(console):
    client, app, root, now = console
    import tomlkit
    path = app.state.bridge.processor.path
    config = tomlkit.parse(path.read_text(encoding='utf-8'))
    config['input']['start_at'] = now
    path.write_bytes(tomlkit.dumps(config).encode('utf-8'))
    for name in ('wechat', 'qq'):
        module_path = app.state.bridge.paths[name]
        module = tomlkit.parse(module_path.read_text(encoding='utf-8'))
        module['filters']['rulesets'] = [{'file':str(app.state.bridge.root/'rulesets/keyword.sql'),
                                        'params':{'keyword':'deadline' if name=='qq' else 'no-such-text'}}]
        module_path.write_bytes(tomlkit.dumps(module).encode('utf-8'))
    before = {str(p):p.read_bytes() for p in root.rglob('*') if p.is_file() and 'runtime' not in p.parts and p.suffix != '.lock'}
    response = client.post('/api/processor/preview', json={'start':now-60,'end':now,'limit':5})
    assert response.status_code == 202, response.text
    result = wait_job(client, response.json()['id'])['result']
    assert result['debug_window'] == {'start':now-60,'end':now}
    assert result['snapshot_only'] is True and result['checkpoint_advanced'] is False
    assert len(result['groups']) == 1 and result['groups'][0]['platform'] == 'qq'
    assert result['batch_plan'][0]['messages'] == 1
    assert before == {str(p):p.read_bytes() for p in root.rglob('*') if p.is_file() and 'runtime' not in p.parts and p.suffix != '.lock'}
    assert not (root/'processing/state.db').exists()
    default = client.post('/api/processor/preview', json={'limit':5})
    restored = wait_job(client, default.json()['id'])['result']
    assert restored['input']['start_at'] == now and restored['pending_groups'] == 0
    assert 'debug_window' not in restored


def test_manual_context_time_validation_and_empty_range(console):
    client, app, root, now = console
    for body in ({'start':now-60}, {'end':now}, {'start':now,'end':now},
                 {'start':now,'end':now-1}, {'start':True,'end':now},
                 {'start':now-8*86400,'end':now}):
        assert client.post('/api/processor/preview', json=body).status_code == 422
    job = client.post('/api/processor/preview', json={'start':now-100,'end':now-90})
    result = wait_job(client, job.json()['id'])['result']
    assert result['pending_groups'] == 0 and result['batch_plan'] == []


def test_agent_configuration_is_strict_and_enable_point_is_read_only(console):
    client, app, root, _ = console
    url='/api/processor/config'
    before=client.get(url).json()
    assert before['config']['input']['start_at'] == 0
    response=client.patch(url,json={'revision':before['revision'],'patch':{
        'model':{'vision':True,'context_window':8192,'max_output_tokens':1024},
        'agent':{'max_turns':6,'max_tool_calls':9,'batching':'single_conversation','auto_compaction':True,
                 'compaction_reserve_tokens':2048,'compaction_keep_recent_tokens':1024,'max_retries':1},
        'context':{'max_conversation_chars':24000},'processing':{'max_input_chars':80000}}})
    assert response.status_code == 200,response.text
    assert response.json()['config']['model']['vision'] is True
    assert response.json()['config']['agent']['batching'] == 'single_conversation'
    assert response.json()['config']['agent']['max_turns'] == 6
    assert response.json()['config']['agent']['max_tool_calls'] == 9
    assert response.json()['config']['agent']['auto_compaction'] is True
    assert response.json()['config']['agent']['max_retries'] == 1
    assert response.json()['config']['model']['context_window'] == 8192
    assert response.json()['config']['context']['max_conversation_chars'] == 24000
    assert response.json()['config']['processing']['max_input_chars'] == 80000
    revision=response.json()['revision']
    for patch in ({'model':{'vision':'yes'}},{'agent':{'max_turns':False}},
                  {'agent':{'max_tool_calls':-1}},{'input':{'start_at':1}},
                  {'context':{'max_conversation_chars':999}},{'context':{'max_conversation_chars':True}},
                  {'agent':{'batching':'truncate'}},{'processing':{'max_input_chars':1000001}},
                  {'agent':{'auto_compaction':'yes'}},{'agent':{'max_retries':6}},
                  {'model':{'context_window':4095}},{'model':{'max_output_tokens':False}}):
        assert client.patch(url,json={'revision':revision,'patch':patch}).status_code == 422
    for patch in ({'model':{'max_output_tokens':9000}}, {'agent':{'compaction_reserve_tokens':8192}}):
        assert client.patch(url,json={'revision':revision,'patch':patch}).status_code == 400
    assert client.get(url).json()['revision'] == revision


def test_source_jump_resolves_exact_collector_revision_and_reuses_asset_guard(console):
    client, app, root, now = console
    record=client.get('/api/modules/wechat/messages').json()['messages'][0]
    params={'message_key':record['message_key'],'revision':record['revision']}
    response=client.get('/api/modules/wechat/source',params=params)
    assert response.status_code == 200,response.text
    assert response.json() == record
    for bad in ({**params,'revision':record['revision']+100},
                {**params,'message_key':"' OR 1=1 --"}):
        assert client.get('/api/modules/wechat/source',params=bad).status_code == 400
    assert client.get('/api/modules/qq/source',params=params).status_code == 400
    assert client.get('/api/modules/wechat/source',params=params,headers={'X-Console-Token':'bad'}).status_code == 401
    assert client.get('/api/modules/wechat/source',params=params,headers={'Origin':'https://evil.example'}).status_code == 403
    # A source lookup doesn't grant arbitrary file access through the existing asset route.
    assert client.get(f"/api/modules/wechat/messages/{record['seq']}/assets/private.txt").status_code == 400
    from collector.store import Store
    payload=b'local source attachment'
    filename=hashlib.sha256(payload).hexdigest()+'.txt'
    asset=root/'wechat/assets'/filename
    asset.write_bytes(payload)
    store=Store(root/'wechat/state.db','wechat','fixture')
    try:
        store.ingest([{'platform':'wechat','account_id':'fixture','conversation_id':'room',
            'message_id':'attachment-evidence','sender_id':'sender','sent_at':now,'kind':'file','text':'evidence',
            'attachments':[{'type':'file','name':'evidence.txt','archive_path':str(asset)}]}],
            now-1,now,'fixture',advance=False)
    finally:
        store.close()
    evidence=client.get('/api/modules/wechat/messages').json()['messages'][0]
    linked=client.get('/api/modules/wechat/source',params={'message_key':evidence['message_key'],
        'revision':evidence['revision']}).json()
    downloaded=client.get(f"/api/modules/wechat/messages/{linked['seq']}/assets/{filename}")
    assert downloaded.status_code == 200 and downloaded.content == payload


def test_processing_api_authentication_and_structured_sources(console):
    client, app, root, _ = console
    from processor.core import _connect, _save_task
    config = app.state.bridge.processor.config()
    conn = _connect(config, write=True)
    task = {'task_id':'fixture-task','title':'测试事项 <script>','status':'open','priority':'P1',
            'time':{'start':None,'deadline':None,'timezone':'Asia/Shanghai','text':'明天下午'},
            'people':[{'name':'张三','role':'负责人'}],'location':'会议室','event':'提交方案',
            'needs_review':True,'review_reasons':['时间待确认'],'source_message_keys':['fixture-message'],
            'sources':[{'text':'<img src=x onerror=alert(1)>','message_key':'fixture-message'}],
            'updated_at':'2026-11-12T12:00:00+08:00'}
    with conn:
        _save_task(conn, task)
    conn.close()
    result = client.get('/api/processor/tasks').json()['tasks'][0]
    assert result['priority'] == 'P1' and result['sources'] == task['sources']
    for endpoint in ('config','rules','status','tasks'):
        assert client.get('/api/processor/'+endpoint,headers={'X-Console-Token':'wrong'}).status_code == 401
        assert client.get('/api/processor/'+endpoint,headers={'Origin':'https://evil.example'}).status_code == 403
    for action in ('run','preview'):
        assert client.post('/api/processor/'+action,json={},headers={'X-Console-Token':'wrong'}).status_code == 401
    assert client.put('/api/processor/rules',json={'revision':'0'*64,'text':'malicious'},headers={'Origin':'https://evil.example'}).status_code == 403


def test_collection_controls_remain_available_and_qq_work_dir_is_validated(console):
    client, app, root, now = console
    page = client.get('/').text
    assert '无 AI' not in page
    for identifier in ('module','preview','replay','save-config','load-messages','processor-preview','processor-run'):
        assert f'id="{identifier}"' in page
    for module in ('wechat','qq'):
        assert client.get(f'/api/modules/{module}/config').status_code == 200
        assert client.get(f'/api/modules/{module}/messages').status_code == 200
        assert client.post(f'/api/modules/{module}/schedule', json={'enabled':False}).status_code == 200
        for action, body in (('preview',{'start':now-60,'end':now}),('replay',{'start':now-60,'end':now}),('collect',None)):
            response = client.post(f'/api/modules/{module}/{action}', json=body)
            assert response.status_code == 202, response.text
            wait_job(client, response.json()['id'])
    url = '/api/modules/qq/config'
    revision = client.get(url).json()['revision']
    work_dir = root/'ssd-work'
    result = client.patch(url,json={'revision':revision,'patch':{'source':{'work_dir':str(work_dir)}}})
    assert result.status_code == 200,result.text
    assert Path(app.state.bridge.config('qq')['source']['work_dir']) == work_dir
    revision = result.json()['revision']
    result = client.patch(url,json={'revision':revision,'patch':{'source':{'work_dir':str(root/'qq/encrypted/work')}}})
    assert result.status_code == 400,result.text
    assert client.get(url).json()['revision'] == revision


@pytest.mark.parametrize('debug', [False, True])
def test_manual_processing_worker_uses_local_mock_and_preserves_evidence(console, debug):
    client, app, root, now = console
    requests = []
    class MockModel(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            content = next(message['content'] for message in request['messages'] if message['role']=='user')
            if isinstance(content,list):
                content=''.join(item.get('text','') for item in content)
            context = json.loads(content)
            requests.append((self.path, context))
            task = {'task_id':None,'title':'模拟模型提取','event':'提交测试方案','priority':'P2',
                'priority_reason':'合成测试','status':'open','time':{'start':None,'deadline':None,'text':None},
                'people':[],'location':None,'needs_review':True,'review_reasons':['合成结果'],
                'sources':[context['conversations'][0]['segments'][0]['messages'][0]['id']]}
            chunk={'id':'chatcmpl-test','object':'chat.completion.chunk','created':1,'model':'local-mock',
                'choices':[{'index':0,'delta':{'role':'assistant','tool_calls':[{'index':0,'id':'call1',
                'type':'function','function':{'name':'submit_tasks','arguments':json.dumps({'tasks':[task]})}}]},'finish_reason':None}]}
            end={**chunk,'choices':[{'index':0,'delta':{},'finish_reason':'tool_calls'}]}
            response=('data: '+json.dumps(chunk)+'\n\ndata: '+json.dumps(end)+'\n\ndata: [DONE]\n\n').encode()
            self.send_response(200)
            self.send_header('Content-Type','text/event-stream')
            self.send_header('Content-Length',str(len(response)))
            self.end_headers()
            self.wfile.write(response)
    server = ThreadingHTTPServer(('127.0.0.1',0), MockModel)
    thread = threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:
        (root/'processing/secret.txt').write_text('fixture-not-a-real-key',encoding='utf-8')
        revision = client.get('/api/processor/config').json()['revision']
        response = client.patch('/api/processor/config',json={'revision':revision,'patch':{'model':{
            'base_url':f'http://127.0.0.1:{server.server_port}/v1','model':'local-mock'}}})
        assert response.status_code == 200,response.text
        if debug:
            import tomlkit
            module_path = app.state.bridge.paths['qq']
            config = tomlkit.parse(module_path.read_text(encoding='utf-8'))
            config['filters']['rulesets'] = []
            module_path.write_bytes(tomlkit.dumps(config).encode('utf-8'))
        protected = [root/'wechat/state.db',root/'qq/state.db',app.state.bridge.processor.path]
        before = {p:p.read_bytes() for p in protected}
        body = {'start':now-20,'end':now-5} if debug else None
        job = client.post('/api/processor/run', json=body)
        assert job.status_code == 202,job.text
        finished = wait_job(client,job.json()['id'])
        result = finished['result']
        assert any(e.get('tool') == 'submit_tasks' for e in finished['pi_events'])
        assert finished['pi_events'][-1]['type'] == 'run_end'
        assert 'fixture-not-a-real-key' not in json.dumps(finished['pi_events'])
        assert result['processed_groups'] == 1 and result['tasks_upserted'] == 1
        task = result['tasks'][0] if debug else client.get('/api/processor/tasks').json()['tasks'][0]
        if debug:
            assert result['debug_window'] == body
            assert result['formal_state_changed'] is False and result['checkpoint_advanced'] is False
            assert not (root/'processing/state.db').exists()
            assert client.get('/api/processor/tasks').json()['tasks'] == []
            assert before == {p:p.read_bytes() for p in protected}
            output = Path(result['debug_output_dir'])
            assert (output/'result.json').is_file() and (output/'incoming/preview.jsonl').is_file()
            assert list((output/'todos').glob('*.jsonl'))
        assert task['title'] == '模拟模型提取' and task['priority'] == 'P2'
        assert task['sources'][0]['collector_module'] == 'wechat'
        assert '<img src=x' in task['sources'][0]['text']
        assert requests[0][0] == '/v1/chat/completions'
        assert requests[0][1]['conversations'][0]['segments'][0]['messages'][0]['id'] == 'm1'
        assert 'primary_message_keys' not in requests[0][1]
        assert task['sources'][0]['message_key'] not in json.dumps(requests[0][1])
        second = client.post('/api/processor/run', json=body)
        again = wait_job(client,second.json()['id'])['result']
        assert again['processed_groups'] == (1 if debug else 0)
        assert len(requests) == (2 if debug else 1)
        if debug:
            assert again['debug_output_dir'] != result['debug_output_dir']
            assert before == {p:p.read_bytes() for p in protected}
            empty = client.post('/api/processor/run',json={'start':now-100,'end':now-90})
            assert wait_job(client,empty.json()['id'])['result']['processed_groups'] == 0
            assert len(requests) == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_debug_processing_rejects_incomplete_time_and_retains_failure_inputs(console, monkeypatch):
    client, app, root, now = console
    for body in ({'start':now-60}, {'end':now}, {'start':now,'end':now}, {'start':True,'end':now}):
        assert client.post('/api/processor/run', json=body).status_code == 422
    def fail(config):
        assert Path(config['input']['batches_dir']).is_dir()
        raise RuntimeError('synthetic model failure')
    monkeypatch.setattr(app.state.bridge.processor.core(), 'run_round', fail)
    with pytest.raises(RuntimeError, match='输入和已完成结果保留'):
        app.state.bridge.execute('processor','process',{'start':now-60,'end':now})
    assert list((root/'processing/debug').glob('*/incoming/preview.jsonl'))
    assert not (root/'processing/state.db').exists()
