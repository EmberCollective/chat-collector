from test_console import console


def test_catalog_and_unknown_schedule_are_guarded_before_scheduler_mutation(console):
    client, app, _, _ = console
    assert client.get('/api/catalog',headers={'X-Console-Token':'invalid'}).status_code==401
    sources=client.get('/api/catalog').json()['sources']
    assert {s['id'] for s in sources}=={'wechat','qq'}
    before=dict(app.state.jobs.enabled)
    assert client.post('/api/modules/missing/schedule',json={'enabled':True}).status_code==400
    assert app.state.jobs.enabled==before
    assert client.post('/api/modules/missing/collect').status_code==400


def test_custom_platform_description_roundtrip_and_existing_fields_survive(console):
    client, _, _, _=console
    config=client.get('/api/processor/config').json()
    response=client.patch('/api/processor/config',json={'revision':config['revision'],'patch':{
        'source_descriptions':{'mail.imap':'邮件来源说明','external stream':'外部消息流'}}})
    assert response.status_code==200,response.text
    descriptions=response.json()['config']['source_descriptions']
    assert descriptions['mail.imap']=='邮件来源说明'
    assert descriptions['external stream']=='外部消息流'
    assert descriptions['wechat']=='合成微信'


def test_old_durable_job_detail_survives_memory_eviction(console):
    import json
    client, app, _, now=console
    identifier='d'*32
    folder=app.state.jobs.directory/identifier
    folder.mkdir()
    (folder/'job.json').write_text(json.dumps({'id':identifier,'module':'qq','action':'collect','origin':'manual',
        'status':'succeeded','started_at':now-8,'finished_at':now}),encoding='utf8')
    (folder/'result.json').write_text(json.dumps({'status':'succeeded','result':{'emitted':3}}),encoding='utf8')
    (folder/'worker.log').write_text('local worker result',encoding='utf8')
    assert identifier not in app.state.jobs.jobs
    detail=client.get('/api/jobs/'+identifier).json()
    assert detail['result']['emitted']==3 and detail['log_tail']=='local worker result'
    listing=client.get('/api/jobs?module=qq&action=collect&min_duration_seconds=7').json()
    assert listing['total']==1 and listing['jobs'][0]['duration_seconds']==8
    assert client.get('/api/jobs/'+identifier,headers={'X-Console-Token':'invalid'}).status_code==401
