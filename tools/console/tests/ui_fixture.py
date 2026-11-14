import sys, tempfile, json, time
from pathlib import Path
project=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(project/'tools/console'),str(project/'tools/console/tests'),str(project)]
import argparse
import tomlkit, uvicorn
parser=argparse.ArgumentParser(description='Run the console using synthetic data only')
parser.add_argument('--port',type=int,default=18447)
args=parser.parse_args()
from test_console import console
from test_messaging_console import setup
from app import create_app
fixture_dir=Path(tempfile.mkdtemp(prefix='loose-ends-ui-'))
gen=console.__wrapped__(fixture_dir)
fixture=next(gen)
_,root=setup(fixture)
settings=tomlkit.parse((root/'console.toml').read_text(encoding='utf8'))
second=tomlkit.parse((root/'qq/config.toml').read_text(encoding='utf8'))
second['source']['account_id']='second-fixture'
second['storage']['state_file']=str(root/'qq-second-state.db')
second_path=root/'qq-second.toml';second_path.write_text(tomlkit.dumps(second),encoding='utf8')
settings['sources']={'qq-work':{'kind':'qq','config':str(second_path),'name':'工作账号'},'external-example':{'kind':'ticket-stream','name':'外部工单','description':'由外部收集器管理，只发布标准消息。'}}
settings['messaging_config']=str(root/'messaging.toml')
(root/'messaging.toml').write_text((root/'messaging.toml').read_text(encoding='utf8')+'\n[[targets]]\nid="workflow-sample"\nkind="webhook"\nenabled=false\nurl_env="UI_TEST_WEBHOOK"\n\n[[targets]]\nid="future"\nkind="new-provider"\nenabled=false\nsecret="never-output-this"\n\n[[targets]]\nid="reserved-kind"\nkind="constructor"\nenabled=false\n',encoding='utf8')
settings['automation_config']=str(root/'automation.toml')
(root/'automation.toml').write_text('enabled=false\n[storage]\nstate_file="automatic/state.db"\n[jobs.processor]\nconfig="processing/config.toml"\n[jobs.messaging]\nconfig="messaging.toml"\n[jobs.feedback]\nconfig="messaging.toml"\n',encoding='utf8')
(root/'console.toml').write_text(tomlkit.dumps(settings),encoding='utf8')
app=create_app(root/'console.toml',root/'runtime-ui')
@app.middleware('http')
async def fixture_only(request, call_next):
    # This review server can modify synthetic files only; never start services, install startup, or send externally.
    if request.method != 'GET' and (request.url.path.startswith('/api/operations/service/')
        or request.url.path == '/api/operations/startup'
        or request.url.path.startswith('/api/operations/jobs/')
        or request.url.path.endswith('/test')):
        from fastapi.responses import JSONResponse
        return JSONResponse({'detail':'合成数据预览不执行后台启动、开机自启或外部发送'},403)
    return await call_next(request)
@app.get('/__fixture__')
def fixture_marker(): return {'synthetic':True}
# Tests cannot operate a real Windows service or startup item.
from tools.install import startup
startup.startup_status=lambda _: {'supported':False,'enabled':False}
from automation import control
control.status=lambda _: {'running':False,'state':'stopped','verified':True}
now=time.time();folder=app.state.jobs.directory/('a'*32);folder.mkdir()
(folder/'job.json').write_text(json.dumps({'id':folder.name,'module':'processor','action':'process','origin':'manual','status':'succeeded','started_at':now-50,'finished_at':now-10}),encoding='utf8')
events=[{'type':'input_prepared','body':{'system':'合成规则：保留证据','payload':{'messages':[{'text':'请提交报告'}]}}},{'type':'request_prepared','body':{'messages':[{'role':'user','content':'请提交报告'}],'tools':[{'name':'future_tool'}]}},{'type':'model_output','body':{'content':[{'type':'text','text':'正在核对相关材料'}]}},{'type':'tool_execution_start','tool':'future_tool','call_id':'call-1','body':{'arguments':{'query':'报告'}}},{'type':'tool_execution_end','tool':'future_tool','call_id':'call-1','body':{'result':{'text':'找到报告材料'}}},{'type':'compaction_end','body':{'summary':'保留提交报告的待办与来源'}},{'type':'auto_retry_start','body':{'attempt':1}},{'type':'result_received','body':{'saved':False,'task_count':1}}]
for i in range(60):events.append({'type':'tool_execution_end','tool':'future_tool','call_id':f'call-{i+2}','body':{'result':{'text':'后续记录 '+str(i)}}})
(folder/'pi-workflow.jsonl').write_text(''.join(json.dumps(dict(e,seq=i+1,run_id='batch-fixture',time='2026-11-14T08:00:00Z'),ensure_ascii=False)+'\n' for i,e in enumerate(events)),encoding='utf8')
for index, (module,action,origin,status,duration) in enumerate([
    ('qq','collect','schedule','failed',14),('wechat','replay','manual','succeeded',70),
    ('qq-work','preview','manual','succeeded',2),('processor','process','cli','interrupted',None)
],start=1):
    entry=app.state.jobs.directory/f'{index:032x}';entry.mkdir()
    (entry/'job.json').write_text(json.dumps({'id':entry.name,'module':module,'action':action,'origin':origin,
        'status':status,'started_at':now-100-index,'finished_at':None if duration is None else now-100-index+duration,
        'error':'fixture database busy' if status=='failed' else None}),encoding='utf8')
print('Synthetic console fixture: '+str(root),flush=True)
try:uvicorn.run(app,host='127.0.0.1',port=args.port,log_level='warning')
finally:gen.close()
