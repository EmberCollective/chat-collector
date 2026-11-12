import hashlib
import json
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
                'filters':{'conversation_ids':[],'keywords':[],'mentions_all':False,'files':False,'match':'any'}}
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
            db.execute(f'INSERT INTO "{table}" VALUES(1,101,1,2,?,NULL,?,NULL)',(now-10,'deadline <img src=x onerror=alert(1)>'))
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


def test_security_and_local_docs(console):
    client,app,*_=console
    assert client.get('/').status_code==200
    assert "script-src 'self'" in client.get('/').headers['content-security-policy']
    assert '/static/vendor/swagger-ui-bundle.js' in client.get('/docs').text
    assert client.get('/api/status',headers={'X-Console-Token':'wrong'}).status_code==401
    assert client.get('/api/status',headers={'Host':'evil.example'}).status_code==403
    assert client.get('/api/session',headers={'Origin':'https://evil.example'}).status_code==403
    assert client.get('/api/session',headers={'Sec-Fetch-Site':'cross-site'}).status_code==403
    assert client.get('/openapi.json').json()['components']['securitySchemes']['APIKeyHeader']['name']=='X-Console-Token'


@pytest.mark.parametrize('module',['wechat','qq'])
def test_preview_is_read_only_and_replay_deduplicates(console,module):
    client,app,root,now=console
    state=root/module/'state.db'
    before=state.read_bytes()
    checkpoint=app.state.bridge.state(module)['checkpoint']
    batches={str(p):p.read_bytes() for p in (root/module/'batches').glob('*')}
    response=client.post(f'/api/modules/{module}/preview',json={'start':now-60,'end':now,'filters':{'keywords':['deadline']}})
    assert response.status_code==202,response.text
    result=wait_job(client,response.json()['id'])['result']
    assert result['matched']==1 and result['checkpoint_advanced'] is False
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
    patch={'revision':config['revision'],'patch':{'schedule':{'interval_seconds':10},'filters':{'keywords':['review']}}}
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


def test_scheduler_resume_pause_and_window_validation(console):
    client,app,root,now=console
    assert not client.get('/api/status').json()['modules']['qq']['scheduler_enabled']
    assert client.post('/api/modules/qq/preview',json={'start':now,'end':now-1}).status_code==422
    assert client.post('/api/modules/qq/preview',json={'start':0,'end':8*86400}).status_code==422
    client.post('/api/modules/qq/schedule',json={'enabled':True})
    until=time.monotonic()+5
    while not app.state.jobs.jobs and time.monotonic()<until:time.sleep(.1)
    identifier=next(iter(app.state.jobs.jobs))
    client.post('/api/modules/qq/schedule',json={'enabled':False})
    wait_job(client,identifier)
    assert app.state.jobs.jobs[identifier]['origin']=='schedule'
    assert not client.get('/api/status').json()['modules']['qq']['scheduler_enabled']


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
    rule = {'type':'nearby','before':2,'after':2,'condition':{'type':'keywords','values':['deadline']}}
    config = client.get(url).json()
    response = client.patch(url,json={'revision':config['revision'],'patch':{'filters':{'rules':[rule]}}})
    assert response.status_code == 200,response.text
    assert client.get(url).json()['config']['filters']['rules'] == [rule]
    state = root/module/'state.db'
    before = hashlib.sha256(state.read_bytes()).hexdigest()
    # Legacy API clients omitting rules keep the configured extension rules.
    preview = client.post(f'/api/modules/{module}/preview',json={'start':now-60,'end':now,'filters':{'keywords':['deadline']}})
    assert preview.status_code==202,preview.text
    result = wait_job(client,preview.json()['id'])['result']
    assert result['matched']==1 and result['rules']==[rule]
    assert hashlib.sha256(state.read_bytes()).hexdigest()==before
    replay = client.post(f'/api/modules/{module}/replay',json={'start':now-60,'end':now}).json()
    result = wait_job(client,replay['id'])['result']
    assert result['matched']==1 and result['duplicates']==1
    revision = client.get(url).json()['revision']
    response = client.patch(url,json={'revision':revision,'patch':{'filters':{'rules':[dict(rule,before=-1)]}}})
    assert response.status_code==400
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


def test_recorded_external_runner_blocks_double_schedule_and_ignores_reused_pid(console,monkeypatch):
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
    assert client.post('/api/modules/qq/schedule',json={'enabled':True}).status_code==409
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
