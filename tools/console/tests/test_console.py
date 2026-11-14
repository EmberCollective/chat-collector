import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

import pytest
import tomlkit
from fastapi.testclient import TestClient

from app import create_app, ROOT


def test_resume_tool_rejects_reused_pid_without_touching_other_process(monkeypatch):
    import runpy
    import psutil
    resume = runpy.run_path(str(ROOT/'tools/resume-skills-manager.py'))['resume']
    calls = []
    class Process:
        def create_time(self): return 123
        def exe(self): return 'manager.exe'
        def resume(self): calls.append('resumed')
    monkeypatch.setattr(psutil,'Process',lambda pid:Process())
    record = {'pid':42,'created':122,'exe':'manager.exe'}
    with pytest.raises(SystemExit):
        resume(record)
    assert calls == []
    resume(dict(record,created=123))
    assert calls == ['resumed']


@pytest.fixture
def console(tmp_path):
    original=tomlkit.parse((ROOT/'console.toml').read_text(encoding='utf-8'))
    settings={'collector_root':str((ROOT/original['collector_root']).resolve()), 'port':10400,'modules':{}}
    now=int(time.time())
    for module in ('wechat','qq'):
        folder=tmp_path/module
        for sub in ('encrypted','decrypted','media','assets'):
            (folder/sub).mkdir(parents=True,exist_ok=True)
        (folder/'key.json').write_text('{}')
        config={'platform':module,'source':{'account_id':'fixture','encrypted_dir':str(folder/'encrypted'),
                'decrypted_dir':str(folder/'decrypted'),'media_dir':str(folder/'media'),
                'key_file':str(folder/'key.json'),'refresh':False},
                'storage':{'state_file':str(folder/'state.db'),'output_dir':str(folder/'batches')},
                'attachments':{'enabled':False,'archive_dir':str(folder/'assets')},
                'schedule':{'interval_seconds':5,'initial_lookback_seconds':3600},
                'filters':{'rulesets':[{'file':str(Path(settings['collector_root'])/'rulesets/all.sql')}]}}
        path=folder/'config.toml'
        path.write_text('# preserve this comment\n'+tomlkit.dumps(config),encoding='utf-8')
        settings['modules'][module]=str(path)
        db=sqlite3.connect(folder/'decrypted'/('nt_msg.db' if module=='qq' else 'message_0.db'))
        if module=='wechat':
            table='Msg_'+hashlib.md5(b'room@chatroom').hexdigest()
            db.execute('CREATE TABLE Name2Id(user_name TEXT)')
            db.executemany('INSERT INTO Name2Id VALUES(?)',[('room@chatroom',),('sender',)])
            db.execute(f'''CREATE TABLE "{table}"(local_id INTEGER PRIMARY KEY,server_id INTEGER,
                local_type INTEGER,real_sender_id INTEGER,create_time INTEGER,source BLOB,message_content BLOB,compress_content BLOB)''')
            db.execute(f'INSERT INTO "{table}" VALUES(1,101,1,2,?,?,?,NULL)',
                       (now-10,'<msgsource><atuserlist>notify@all</atuserlist></msgsource>','deadline <img src=x onerror=alert(1)>'))
        else:
            import sys
            sys.path.insert(0,settings['collector_root'])
            from collector.adapters.qq_vendor.c2c_40800_pb2 import MsgBody
            body=MsgBody();body.content.add(content_type=1,text='deadline',text_attr=1)
            db.execute('''CREATE TABLE group_msg_table("40001" INTEGER PRIMARY KEY,"40050" INTEGER,
                "40030" INTEGER,"40021" TEXT,"40033" INTEGER,"40020" TEXT,"40011" INTEGER,"40800" BLOB)''')
            db.execute('INSERT INTO group_msg_table VALUES(1,?,123,"123",456,"sender",1,?)',(now-10,body.SerializeToString()))
        db.commit();db.close()
    path=tmp_path/'console.toml'
    processing=tmp_path/'processing'
    processing.mkdir()
    (processing/'rules.md').write_text('# 提取待办\n保留来源，不猜测未知时间。\n',encoding='utf-8')
    processor_config={'input':{'batches_dir':str(tmp_path/'wechat/batches'),'start_at':0},
        'storage':{'state_file':str(processing/'state.db'),'output_dir':str(processing/'todos')},
        'rules':{'file':str(processing/'rules.md')},
        'model':{'base_url':'','model':'','api_key_env':'CONSOLE_TEST_PROCESSOR_KEY','api_key_file':str(processing/'secret.txt')},
        'source_descriptions':{'qq':'合成 QQ','wechat':'合成微信'}}
    (processing/'config.toml').write_text('# processor fixture\n'+tomlkit.dumps(processor_config),encoding='utf-8')
    settings['processor_config']=str(processing/'config.toml')
    path.write_text(tomlkit.dumps(settings),encoding='utf-8')
    app=create_app(path,tmp_path/'runtime')
    for name in ('wechat','qq'):
        app.state.bridge.run_round(app.state.bridge.config(name))
    with TestClient(app,base_url='http://127.0.0.1') as client:
        client.headers['X-Console-Token']=client.get('/api/session').json()['token']
        yield client,app,tmp_path,now


def wait_job(client,identifier):
    until=time.monotonic()+20
    while time.monotonic()<until:
        job=client.get('/api/jobs/'+identifier).json()
        if job['status']!='running':
            assert job['status']=='succeeded',job
            return job
        time.sleep(.1)
    raise AssertionError('worker did not finish')


def test_activity_combines_manual_and_automatic_runs_with_retry_details(console):
    from automation import load_config, SchedulerService
    client, app, folder, now = console
    path = folder / 'automation.toml'
    path.write_text('enabled=true\n[storage]\nstate_file="automation.db"\n[jobs.wechat]\nenabled=true\n', encoding='utf-8')
    app.state.bridge.settings['automation_config'] = str(path)
    manual = dict(id='old-manual', module='qq', action='preview', origin='manual',
                  status='succeeded', started_at=now-10, finished_at=now-8)
    app.state.jobs.jobs[manual['id']] = manual
    ticks = [now]
    calls = []
    def collect():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError('PRIVATE_CHAT_OR_CREDENTIAL')
        return {'messages': 4}
    service = SchedulerService(load_config(path), callbacks={'wechat': collect}, clock=lambda: ticks[0])
    service.execute('wechat')
    ticks[0] += 31
    service.execute('wechat')
    data = client.get('/api/activity').json()
    assert not data['warnings']
    assert [r['source'] for r in data['records']] == ['automation', 'console']
    run = data['records'][0]
    assert run['status'] == 'succeeded' and run['attempt_count'] == 2
    assert run['origin'] == 'schedule'
    detail = client.get('/api/activity/automation/' + run['id']).json()
    assert [a['number'] for a in detail['attempts']] == [1, 2]
    assert detail['attempts'][0]['error'] == 'RuntimeError'
    assert detail['attempts'][1]['result']['messages'] == 4
    assert 'PRIVATE_CHAT_OR_CREDENTIAL' not in json.dumps(detail)
    filtered = client.get('/api/activity?module=qq&origin=manual&status=succeeded').json()
    assert [r['id'] for r in filtered['records']] == ['old-manual']
    assert client.get('/api/activity?module=qq&origin=schedule').json()['records'] == []
    assert client.get('/api/activity?limit=1').json()['records'] == [run]
    manual_detail = client.get('/api/activity/console/old-manual').json()
    assert manual_detail['duration_seconds'] == 2
    assert len(manual_detail['attempts']) == 1
    assert client.get('/api/activity/automation/missing').status_code == 404


def test_activity_keeps_manual_records_when_automation_configuration_is_invalid(console):
    client, app, folder, now = console
    path = folder / 'invalid-automation.toml'
    path.write_text('this is not toml', encoding='utf-8')
    app.state.bridge.settings['automation_config'] = str(path)
    app.state.jobs.jobs['manual'] = dict(id='manual', module='wechat', action='collect',
        origin='manual', status='interrupted', started_at=now, finished_at=None)
    response = client.get('/api/activity')
    assert response.status_code == 200
    assert response.json()['records'][0]['duration_seconds'] is None
    assert response.json()['warnings']
    assert client.get('/api/activity/automation/missing').status_code == 503
    assert client.get('/api/activity/console/manual').status_code == 200
    assert client.get('/api/activity?module=invalid').status_code == 422
    assert client.get('/api/activity/invalid/manual').status_code == 422
    client.headers.pop('X-Console-Token')
    assert client.get('/api/activity').status_code == 401
    assert client.get('/api/activity/console/manual').status_code == 401


def test_security_and_local_docs(console):
    client,app,*_=console
    session = client.get('/api/session').json()
    assert session['application'] == 'chat-collector-console'
    assert Path(session['root']) == app.state.bridge.root
    assert session['pid'] == os.getpid()
    assert client.get('/').status_code==200
    assert "script-src 'self'" in client.get('/').headers['content-security-policy']
    assert '/static/vendor/swagger-ui-bundle.js' in client.get('/docs').text
    assert client.get('/api/status',headers={'X-Console-Token':'wrong'}).status_code==401
    assert client.get('/api/status',headers={'Host':'evil.example'}).status_code==403
    assert client.get('/api/session',headers={'Origin':'https://evil.example'}).status_code==403
    assert client.get('/api/session',headers={'Sec-Fetch-Site':'cross-site'}).status_code==403
    assert client.get('/openapi.json').json()['components']['securitySchemes']['APIKeyHeader']['name']=='X-Console-Token'
    page = client.get('/').text
    assert 'filter-rulesets' in page
    for field in ('filter-groups', 'filter-keywords', 'filter-files', 'filter-all', 'filter-match'):
        assert f'id="{field}"' not in page


def test_removed_base_filters_are_rejected_by_api(console):
    client, app, root, now = console
    url = '/api/modules/wechat/config'
    revision = client.get(url).json()['revision']
    for field in ('keywords', 'conversation_ids', 'files', 'mentions_all', 'match', 'sql_file', 'code_file'):
        assert client.patch(url, json={'revision':revision,'patch':{'filters':{field:[]}}}).status_code == 422
        assert client.post('/api/modules/wechat/preview', json={'start':now-60,'end':now,'filters':{field:[]}}).status_code == 422
    assert client.get(url).json()['revision'] == revision


def test_ruleset_enable_flags_save_preview_and_collect(console):
    client, app, root, now = console
    url = '/api/modules/qq/config'
    rule = {'file':str(app.state.bridge.root/'rulesets/mentions-all.sql'),'enabled':False}
    checkpoint = app.state.bridge.state('qq')['checkpoint']
    revision = client.get(url).json()['revision']
    response = client.patch(url,json={'revision':revision,'patch':{'filters':{'rulesets':[rule]}}})
    assert response.status_code == 200, response.text
    preview = client.post('/api/modules/qq/preview',json={'start':now-60,'end':now})
    assert wait_job(client,preview.json()['id'])['result']['matched'] == 0
    collect = client.post('/api/modules/qq/collect')
    assert wait_job(client,collect.json()['id'])['result']['status'] == 'no_enabled_rules'
    assert app.state.bridge.state('qq')['checkpoint'] == checkpoint
    rule['enabled'] = True
    preview = client.post('/api/modules/qq/preview',json={'start':now-60,'end':now,'filters':{'rulesets':[rule]}})
    assert wait_job(client,preview.json()['id'])['result']['matched'] == 1


@pytest.mark.parametrize('module',['wechat','qq'])
def test_preview_is_read_only_and_replay_deduplicates(console,module):
    client,app,root,now=console
    state=root/module/'state.db'
    before=state.read_bytes()
    checkpoint=app.state.bridge.state(module)['checkpoint']
    batches={str(p):p.read_bytes() for p in (root/module/'batches').glob('*')}
    response=client.post(f'/api/modules/{module}/preview',json={'start':now-60,'end':now,'filters':{'rulesets':[{'file':str(app.state.bridge.root/'rulesets/keyword.sql'),'params':{'keyword':'deadline'}}]}})
    assert response.status_code==202,response.text
    result=wait_job(client,response.json()['id'])['result']
    assert result['matched']==1 and result['checkpoint_advanced'] is False
    assert Path(result['messages'][0]['matched_rules'][0]['file']).name == 'keyword.sql'
    assert state.read_bytes()==before
    assert {str(p):p.read_bytes() for p in (root/module/'batches').glob('*')}==batches
    assert list((root/module/'assets').iterdir())==[]
    replay=client.post(f'/api/modules/{module}/replay',json={'start':now-60,'end':now})
    result=wait_job(client,replay.json()['id'])['result']
    assert result['emitted']==0 and result['duplicates']==1
    assert app.state.bridge.state(module)['checkpoint']==checkpoint


def test_config_backup_revision_validation_and_busy_guard(console):
    client,app,root,_=console
    url='/api/modules/wechat/config'
    config=client.get(url).json()
    patch={'revision':config['revision'],'patch':{'schedule':{'interval_seconds':10},'filters':{'rulesets':[{'file':str(app.state.bridge.root/'rulesets/keyword.sql'),'params':{'keyword':'review'}}]}}}
    response=client.patch(url,json=patch)
    assert response.status_code==200,response.text
    assert Path(response.json()['backup']).read_text(encoding='utf-8').startswith('# preserve this comment')
    assert (root/'wechat/config.toml').read_text(encoding='utf-8').startswith('# preserve this comment')
    assert client.patch(url,json=patch).status_code==409
    patch['revision']=response.json()['revision']
    patch['patch']={'schedule':{'interval_seconds':False}}
    assert client.patch(url,json=patch).status_code==422
    patch['patch']={'storage':{'state_file':'lost.db'}}
    assert client.patch(url,json=patch).status_code==422
    patch['patch']={'source':{'decrypted_dir':str(root/'wechat/encrypted')}}
    assert client.patch(url,json=patch).status_code==400
    assert client.get(url).json()['revision']==response.json()['revision']
    with app.state.bridge.lock(root/'wechat/state.db'):
        assert client.post('/api/modules/wechat/collect').status_code==409
        patch['patch']={'schedule':{'interval_seconds':15}}
        assert client.patch(url,json=patch).status_code==409


def test_console_has_no_timer_and_rejects_legacy_schedule(console):
    client,app,root,now=console
    assert not hasattr(app.state.jobs, 'scheduler')
    assert not hasattr(app.state.jobs, 'enabled')
    assert 'scheduler_enabled' not in client.get('/api/status').json()['modules']['qq']
    assert client.post('/api/modules/qq/preview',json={'start':now,'end':now-1}).status_code==422
    assert client.post('/api/modules/qq/preview',json={'start':0,'end':8*86400}).status_code==422
    for enabled in (True, False):
        assert client.post('/api/modules/qq/schedule',json={'enabled':enabled}).status_code==410
    assert not app.state.jobs.jobs
    job=client.post('/api/modules/qq/collect').json()
    assert wait_job(client,job['id'])['status']=='succeeded'
    assert app.state.jobs.jobs[job['id']]['origin']=='manual'


@pytest.mark.parametrize('module', ['wechat', 'qq'])
def test_intensity_and_intervals_save_and_reach_worker(console, module):
    client, app, root, now = console
    url = f'/api/modules/{module}/config'
    assert client.get(url).json()['config']['processing']['intensity'] == 'full'
    for intensity in ('metadata', 'standard', 'full'):
        before = client.get(url).json()
        result = client.patch(url, json={'revision':before['revision'], 'patch':{
            'processing':{'intensity':intensity},
            'schedule':{'interval_seconds':17,'refresh_interval_seconds':123}}})
        assert result.status_code == 200, result.text
        assert app.state.bridge.config(module)['processing']['intensity'] == intensity
        status = client.get('/api/status').json()['modules'][module]
        assert (status['intensity'],status['interval_seconds'],status['refresh_interval_seconds']) == (intensity,17,123)
        job = client.post(f'/api/modules/{module}/replay',json={'start':now-60,'end':now}).json()
        assert wait_job(client,job['id'])['result']['intensity'] == intensity
    revision = client.get(url).json()['revision']
    for patch in ({'processing':{'intensity':'turbo'}}, {'schedule':{'refresh_interval_seconds':-1}},
                  {'schedule':{'interval_seconds':4}}, {'schedule':{'refresh_interval_seconds':True}}):
        assert client.patch(url,json={'revision':revision,'patch':patch}).status_code == 422
    assert client.get(url).json()['revision'] == revision


@pytest.mark.parametrize('module',['wechat','qq'])
def test_context_rule_save_preview_and_replay_share_selection(console,module):
    client,app,root,now = console
    url = f'/api/modules/{module}/config'
    rule = {'file':str(app.state.bridge.root/'rulesets/context.sql'),'recent_count':5}
    config = client.get(url).json()
    response = client.patch(url,json={'revision':config['revision'],'patch':{'filters':{'rulesets':[rule]}}})
    assert response.status_code == 200,response.text
    assert client.get(url).json()['config']['filters']['rulesets'] == [rule]
    state = root/module/'state.db'
    before = hashlib.sha256(state.read_bytes()).hexdigest()
    # Omitting rulesets preserves the saved registration.
    preview = client.post(f'/api/modules/{module}/preview',json={'start':now-60,'end':now,'filters':{}})
    assert preview.status_code==202,preview.text
    result = wait_job(client,preview.json()['id'])['result']
    assert result['matched']==1 and result['rulesets']==[rule]
    assert hashlib.sha256(state.read_bytes()).hexdigest()==before
    replay = client.post(f'/api/modules/{module}/replay',json={'start':now-60,'end':now}).json()
    result = wait_job(client,replay['id'])['result']
    assert result['matched']==1 and result['emitted']==1
    records = client.get(f'/api/modules/{module}/messages').json()['messages']
    assert Path(records[0]['matched_rules'][0]['file']).name == 'context.sql'
    assert 'matched_rules' not in records[0]['message']
    replay = client.post(f'/api/modules/{module}/replay',json={'start':now-60,'end':now}).json()
    assert wait_job(client,replay['id'])['result']['duplicates'] == 1
    revision = client.get(url).json()['revision']
    response = client.patch(url,json={'revision':revision,'patch':{'filters':{'rulesets':[dict(rule,recent_count=-1)]}}})
    assert response.status_code==422
    assert client.get(url).json()['revision']==revision


def test_assets_require_message_reference_and_archive_root(console):
    client,app,root,now=console
    from collector.store import Store
    payload=b'archived bytes'
    filename=hashlib.sha256(payload).hexdigest()+'.html'
    asset=root/'wechat/assets'/filename
    asset.write_bytes(payload)
    with_context=app.state.bridge.config('wechat')
    store=Store(root/'wechat/state.db','wechat','fixture')
    message={'platform':'wechat','account_id':'fixture','conversation_id':'room','message_id':'file1',
        'sender_id':'sender','sent_at':now,'kind':'file','text':'attachment','attachments':[{'archive_path':str(asset),'type':'file'}]}
    store.ingest([message],now-1,now,'fixture',advance=False)
    seq=store.db.execute('SELECT max(seq) FROM outbox').fetchone()[0]
    store.close()
    url=f'/api/modules/wechat/messages/{seq}/assets/{filename}'
    response=client.get(url+'?inline=true')
    assert response.content==payload
    assert response.headers['content-disposition'].startswith('attachment')
    assert response.headers['content-type']=='application/octet-stream'
    asset.unlink()
    assert client.get(url).status_code in {400,404}
    secret=root/'private.txt';secret.write_text('not an attachment')
    assert client.get(f'/api/modules/wechat/messages/{seq}/assets/private.txt').status_code==400
    assert client.get('/api/modules/wechat/messages').json()['messages'][0]['message']['message_id']=='file1'


def test_recorded_external_runner_ignores_reused_pid(console,monkeypatch):
    import datetime
    import psutil
    client,app,root,now=console
    bridge=app.state.bridge
    bridge.root=root
    (root/'data').mkdir()
    (root/'data/collector-process.json').write_text(json.dumps({'pid':4242,
        'started_utc':datetime.datetime.fromtimestamp(now,datetime.timezone.utc).isoformat()}))
    class Process:
        pid=4242
        created=now
        def create_time(self):return self.created
        def cmdline(self):return ['python','-m','collector','run',str(root),*[str(p) for p in bridge.paths.values()]]
    process=Process()
    monkeypatch.setattr(psutil,'Process',lambda pid:process)
    assert bridge.external_runner()['verified'] is True
    assert client.post('/api/modules/qq/schedule',json={'enabled':True}).status_code==410
    process.created=now+100
    assert bridge.external_runner() is None


def test_metadata_disk_failure_does_not_leave_module_permanently_busy(console,monkeypatch):
    import app as app_module
    client,app,root,now=console
    jobs=app.state.jobs
    folder=root/'metadata-fault';folder.mkdir()
    (folder/'result.json').write_text(json.dumps({'status':'succeeded','result':{'emitted':0}}))
    job={'id':'fixture','module':'qq','status':'running','action':'collect','started_at':now}
    jobs.running['qq']='fixture'
    class Process:
        def wait(self,timeout=None):return 0
    def full_disk(*args):raise OSError('disk full')
    monkeypatch.setattr(app_module,'atomic_json',full_disk)
    jobs.finish(job,Process(),folder)
    assert 'qq' not in jobs.running
    assert job['status']=='succeeded' and 'metadata_error' in job
