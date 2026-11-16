(() => {
'use strict';
const $=id=>document.getElementById(id),node=(tag,text,cls)=>{const el=document.createElement(tag);if(text!=null)el.textContent=text;if(cls)el.className=cls;return el;};
const metricExamples=[
{id:'wechat-main',name:'微信',kind:'wechat',status:'定时等待',last:'14:31:42',emitted:947,duplicates:240,success:36,failed:0,skipped:5,unknown:0,average:18.4,p95:38.1,recent:Array(20).fill('success')},
{id:'qq-main',name:'QQ',kind:'qq',status:'等待重试',last:'14:28:10',emitted:337,duplicates:72,success:33,failed:1,skipped:3,unknown:0,average:42.6,p95:68.3,recent:[...Array(19).fill('success'),'failed']},
{id:'external-example',name:'外部信息源 · 接入示例',kind:'external',status:'由外部管理',capabilities:{metrics:false},description:'当前未提供运行统计'}
];
const issues=[{id:'collect',severity:'失败',name:'QQ：采集未完成',detail:'数据库被占用；上次成功 14:28，下轮等待重试。已保存断点未推进。',at:'14:29:04',operation:'正式采集',module:'QQ',origin:'定时',duration:'14.0 秒'},
{id:'delivery',severity:'待确认',name:'工作群：一条投递结果未知',detail:'先核对目标群是否收到，再决定是否重试，避免重复发送。',at:'14:30:12',operation:'外部投递',module:'消息投递',origin:'定时',duration:'30.0 秒'}];

const runs=[
 {id:'collect',sourceId:'qq-main',at:'14:29:04',operation:'正式采集',module:'QQ',origin:'后台定时',duration:'14.0 秒',severity:'失败',error:'数据库被其他进程占用，刷新失败。本轮未推进断点。',context:{source_id:'qq-main',window:'14:28:10 — 14:29:04',checkpoint:'保留 14:28:10',rules:['mentions-all.sql','files.sql'],attempt:1},logs:['14:29:04 开始正式采集','14:29:07 等待来源数据库释放','14:29:18 刷新超时，断点未推进'],config:'settings',next:'先关闭重复采集任务或等待数据库释放，检查已保存断点后再安排新一轮。'},
 {id:'delivery',targetId:'feishu-assistant',at:'14:30:12',operation:'外部投递',module:'消息投递',origin:'后台定时',duration:'30.0 秒',severity:'结果未知',error:'请求已发出，目标确认超时。无法判断目标端是否已经收到。',context:{target_id:'feishu-assistant',target:'产品协作群',task:'反馈 v0.8 联调结果',request_id:'demo-delivery-143012',attempt:1,automatic_retry:false},logs:['14:30:12 请求已提交到目标渠道','14:30:42 等待确认超时；没有自动重发'],config:'automation/channels',next:'先在目标端按内容与时间核实。已收到则人工确认；确定未收到后才可安排重试。'},
 {id:'work-ok',sourceId:'qq-main',at:'14:28:10',operation:'正式采集',module:'QQ',origin:'后台定时',duration:'42.6 秒',severity:'成功',error:'无错误',context:{source_id:'qq-main',emitted:12,duplicates:2,checkpoint:'14:28:10'},logs:['14:27:28 读取来源快照','14:28:10 本轮完成，断点已保存'],config:'settings',next:'本轮已完成；后续失败请查看对应轮次。'},
 {id:'collect-ok',sourceId:'wechat-main',at:'14:31:24',operation:'正式采集',module:'微信',origin:'后台定时',duration:'18.4 秒',severity:'成功',error:'无错误',context:{source_id:'wechat-main',emitted:24,duplicates:3,checkpoint:'14:31:42'},logs:['14:31:24 读取来源快照','14:31:42 已归档消息与附件，更新断点'],config:'settings',next:'本轮已完成，等待下一次后台计划。'}
];
const recoveryState=new Map();
let historyScope=null, returnContext=null;
const sourceExamples=()=>catalog.sources.map(source=>({...metricExamples.find(item=>item.id===source.id),id:source.id,name:source.name,kind:source.kind,status:metricExamples.find(item=>item.id===source.id)?.status||'未提供统计'}));
const deliveryExample=()=>catalog.deliveries.find(item=>item.id==='delivery-demo-unknown');
function syncDelivery(){const delivery=deliveryExample();if(!delivery||delivery.status==='unknown')return;if(!recoveryState.has('delivery'))recoveryState.set('delivery',{label:delivery.status==='queued'?'等待重试（演示）':'已送达（人工核实 · 演示）',note:delivery.resolution||'已核对示例结果；未连接真实渠道。'});}
function resolveDelivery(state){const delivery=deliveryExample();if(delivery){delivery.status=state.label.startsWith('等待重试')?'queued':'sent';delivery.resolution=state.note;}renderDeliveries();}
function goRelated(run){
 returnContext={id:run.id,scope:{...runScope,ids:[...runScope.ids]},result:$('run-result-filter').value};
 $('run-dialog').close();if(run.sourceId&&catalog.sources.some(source=>source.id===run.sourceId))switchSource(run.sourceId);
 if(run.config==='automation/channels')openAutomation('channels');else openSettings('source');
 $('overview-return-context').hidden=false;$('overview-return-label').textContent='来自运行：'+run.module+' · '+run.operation+' · '+run.at;
}
function restoreRun(){if(!returnContext)return;const context=returnContext;$('overview-return-context').hidden=true;navigate('overview');openRuns(null,{ids:context.scope.ids,label:context.scope.label,result:context.result});showRun(context.id);}

let runScope={ids:[],label:''};
function liveIssues(){return $('scenario').value==='issues'?issues.filter(issue=>!recoveryState.has(issue.id)):[];}
function resultLabel(run){return recoveryState.get(run.id)?.label||run.severity;}
function openRuns(problem=null,options={}){
 const ids=options.ids||(problem?[problem.id]:$('scenario').value==='empty'?[]:runs.filter(run=>run.sourceId&&($('scenario').value==='issues'||run.severity==='成功')).map(run=>run.id));
 runScope={ids,label:options.label||(problem?problem.name:'正式采集 · 所有来源')};
 $('run-result-filter').value=options.result||'all';
 $('run-scope').textContent='最近 24 小时 · 11-13 14:32 — 11-14 14:32 · '+runScope.label;
 renderRunList();if(!$('run-dialog').open)$('run-dialog').showModal();
 if(problem)showRun(problem.id);
}
function renderRunList(){
 $('dialog-heading').textContent='相关运行 · 示例';$('run-list').hidden=false;$('run-detail').hidden=true;
 const filter=$('run-result-filter').value,records=runs.filter(run=>runScope.ids.includes(run.id)&&(filter==='all'||(filter==='failed'&&run.severity==='失败')||(filter==='succeeded'&&run.severity==='成功')||(filter==='unknown'&&resultLabel(run)==='结果未知')));
 const table=node('table'),head=node('tr');for(const value of ['开始时间','模块','操作','触发方式','结果','耗时','详情'])head.append(node('th',value));table.append(head);
 for(const item of records){const row=node('tr');for(const key of ['at','module','operation','origin'])row.append(node('td',item[key]));row.append(node('td',resultLabel(item)),node('td',item.duration));const cell=node('td'),button=node('button','查看详情','text-button');button.dataset.runId=item.id;button.addEventListener('click',()=>showRun(item.id));cell.append(button);row.append(cell);table.append(row);}
 $('related-runs').replaceChildren(records.length?table:node('p','此范围没有符合条件的运行；筛选仍保留。','empty'));
}
function showRun(id){
 const run=runs.find(item=>item.id===id);if(!run)return;
 $('dialog-heading').textContent=run.module+' · '+run.operation;$('run-list').hidden=true;
 const detail=$('run-detail');detail.hidden=false;detail.replaceChildren();
 const back=node('button','返回相关运行','text-button');back.id='back-to-runs';back.addEventListener('click',()=>{renderRunList();document.querySelector(`[data-run-id="${id}"]`)?.focus();});detail.append(back);
 detail.append(node('p','静态演示：以下操作只改变本页示例，未连接后台或发送消息。','run-notice'));
 const facts=node('dl',null,'run-facts');for(const [label,value]of [['操作',run.operation],['模块',run.module],['触发方式',run.origin],['开始时间','2026-11-14 '+run.at],['耗时',run.duration],['结果',resultLabel(run)],['运行编号','demo-'+run.id]])facts.append(node('dt',label),node('dd',value));detail.append(facts,node('h3','错误与当前结果'),node('p',run.error),node('h3','下一步'),node('p',recoveryState.get(id)?.note||run.next));
 const actions=node('div',null,'actions'),config=node('button','打开相关配置','secondary');config.addEventListener('click',()=>goRelated(run));actions.append(config);
 if(id==='delivery'&&!recoveryState.has(id)){const verify=node('button','核对目标端结果…','secondary');verify.id='verify-delivery';verify.addEventListener('click',()=>showVerification(run));actions.prepend(verify);}
 if(id==='collect'&&!recoveryState.has(id)){const retry=node('button','演示安排下一轮…','secondary');retry.id='retry-collection';retry.addEventListener('click',()=>{const block=node('div',null,'run-notice');block.append(node('p','先核对数据库占用和实际输出。本演示只安排新一轮，不把原失败改成成功；断点保持不变。'));const confirm=node('button','演示安排新一轮','secondary');confirm.id='confirm-collection-retry';confirm.addEventListener('click',()=>{recoveryState.set(id,{label:'失败 · 已安排新一轮（演示）',note:'示例已安排新一轮，尚未开始或成功。原失败与断点证据保留，真实后台没有变化。'});render();showRun(id);});block.append(confirm);retry.after(block);retry.disabled=true;});actions.prepend(retry);}
 detail.append(actions,node('h3','本轮上下文'),node('pre',JSON.stringify(run.context,null,2)));
 const logs=node('details'),summary=node('summary','查看日志与原始记录');logs.append(summary,node('pre',run.logs.join('\n')),node('pre',JSON.stringify({...run,resolution:recoveryState.get(id)||null,demo:true},null,2)));detail.append(logs);detail.focus({preventScroll:true});
}
function showVerification(run){
 const detail=$('run-detail');if($('delivery-verification'))return;
 const box=node('section',null,'run-notice');box.id='delivery-verification';box.append(node('h3','先核实是否送达'),node('p','目标：产品协作群；内容：反馈 v0.8 联调结果；发起时间：14:30:12。请在目标端按这些信息核对。此稿无法查询真实送达情况。'));
 const label=node('label'),check=node('input');check.type='checkbox';check.id='delivery-checked';label.append(check,document.createTextNode('我已按上述内容核对目标端（演示）'));box.append(label);
 const received=node('button','演示确认已收到','secondary'),retry=node('button','演示核实未达后重试','secondary');received.id='confirm-received';retry.id='confirm-delivery-retry';received.disabled=retry.disabled=true;
 check.addEventListener('change',()=>{received.disabled=retry.disabled=!check.checked;});
 for(const [button,state]of [[received,{label:'已送达（人工核实 · 演示）',note:'示例已人工确认送达，没有重新发送；真实投递记录未改变。'}],[retry,{label:'等待重试（演示）',note:'示例已核实未收到并安排重试，尚未送达；真实渠道没有收到请求。'}]])button.addEventListener('click',()=>{if(!check.checked)return;recoveryState.set(run.id,state);resolveDelivery(state);render();showRun(run.id);});
 const actions=node('div',null,'actions');actions.append(received,retry);box.append(actions);detail.insertBefore(box,detail.querySelector('.actions'));check.focus();
}
function render(){
syncDelivery();$('scenario-scope').hidden=$('scenario').value==='issues';
const mode=$('scenario').value,empty=mode==='empty',bad=mode==='issues',active=liveIssues();const sources=sourceExamples().map(source=>({...source,...(!bad&&source.id==='qq-main'?{failed:0,success:34,status:'定时等待',unknown:0,last:'14:31:18',recent:Array(20).fill('success')}:{}),...(empty&&source.emitted!=null?{emitted:0,duplicates:0,success:0,failed:0,skipped:0,unknown:0,last:'尚无成功',average:null,p95:null,recent:[],status:'等待首次运行'}:{})}));
const attention=$('attention');attention.classList.toggle('good',!active.length);attention.replaceChildren();const heading=node('div',null,'alert-heading'),title=node('h2',empty?'等待首次采集':active.length?active.length+' 项问题需要处理':'当前没有待处理异常');if(active.length)title.prepend(node('span','● '));heading.append(title,node('span',empty?'尚不能判断运行质量':active.length?'先核对失败与未知结果':bad?'已演示安排后续处理；历史失败仍保留':'示例：服务与已启用任务正常','muted'));attention.append(heading);
if(bad)for(const issue of active){const row=node('div',null,'problem-row'),text=node('div');text.append(node('strong',issue.name),node('p',issue.detail));row.append(node('span',issue.severity,'problem-tag'+(issue.id==='delivery'?' warn':'')),text,node('time',issue.at));const button=node('button','查看相关记录','secondary');button.addEventListener('click',()=>openRuns(issue));row.append(button);attention.append(row);}
if(empty){const p=node('p','没有有效采集轮次时，条数显示 0，成功率与耗时显示“—”；保留已启用的定时状态。','scope');attention.append(p);}
const measured=sources.filter(s=>s.emitted!=null),success=measured.reduce((n,s)=>n+s.success,0),failed=measured.reduce((n,s)=>n+s.failed,0),emitted=measured.reduce((n,s)=>n+s.emitted,0),duplicates=measured.reduce((n,s)=>n+s.duplicates,0);
const metrics=[['待处理问题',active.length+' 项',active.length?'仍需处理 '+active.map(issue=>issue.severity).join(' / '):'包含故障与结果未确认',active.length>0],['后台服务','运行中',empty?'任务已启用 · 等待首轮':'采集 / 处理 / 投递分别核对'],['入库消息变更',emitted.toLocaleString()+' 条','含新增与修订 · 重复跳过 '+duplicates.toLocaleString()+' 条'],['正式采集成功率',success+failed?(100*success/(success+failed)).toFixed(1)+'%':'—',success+failed?`${success} 成功 / ${success+failed} 个有效结束轮次`:'暂无有效样本']];$('kpis').replaceChildren();for(const [label,value,detail,isBad]of metrics){const metric=node('div',null,'metric');metric.append(node('span',label),node('strong',value,isBad?'bad':''),node('small',detail));$('kpis').append(metric);}
const table=node('table'),thead=node('thead'),tr=node('tr');for(const title of ['信息源','当前状态','入库变更','平均 / P95 耗时','成功率 · 最近 20 次','最近成功','问题'])tr.append(node('th',title));thead.append(tr);table.append(thead);const body=node('tbody');
for(const source of sources){const row=node('tr'),name=node('td');name.append(node('strong',source.name),node('small',source.id));const recovery=runs.find(run=>run.sourceId===source.id&&recoveryState.has(run.id));row.append(name,node('td',recovery?'新一轮已安排（演示）':source.status));if(source.emitted==null){for(const text of ['—','—','未提供统计','—','—'])row.append(node('td',text));}
else{const count=node('td',source.emitted.toLocaleString()),duration=node('td',source.average==null?'—':source.average.toFixed(1)+' / '+source.p95.toFixed(1)+' 秒'),rate=node('td',null,'success-cell'),total=source.success+source.failed;duration.append(node('small',total+' 个有效样本'));rate.append(node('span',total?(100*source.success/total).toFixed(1)+'%':'—','rate'+(source.failed?' warn':'')));const ticks=node('div',null,'run-ticks');ticks.setAttribute('role','group');ticks.setAttribute('aria-label',`最近 ${source.recent.length} 次有效轮次，${source.recent.filter(s=>s==='failed').length} 次失败`);for(const outcome of source.recent){const tick=node('button',null,outcome);tick.title=source.name+' · '+(outcome==='failed'?'失败':'成功')+' · 查看运行详情';tick.setAttribute('aria-label',tick.title);tick.addEventListener('click',()=>{const run=runs.find(item=>item.sourceId===source.id&&item.severity===(outcome==='failed'?'失败':'成功'));if(run)openRuns({id:run.id,name:source.name+' · 最近 20 次有效轮次'},{ids:runs.filter(item=>item.sourceId===source.id).map(item=>item.id)});});ticks.append(tick);}rate.append(ticks,node('small',`${source.success}/${total} 成功 · 跳过 ${source.skipped}${source.unknown?' · 未确认 '+source.unknown:''}`));row.append(count,duration,rate,node('td',source.last));const problem=node('td',source.failed?'1 项':'—');if(source.failed){problem.replaceChildren();const link=node('button',recovery?'已安排 · 查看':'1 项 · 查看','text-button');link.dataset.sourceProblem=source.id;link.addEventListener('click',()=>openRuns(runs.find(item=>item.sourceId===source.id&&item.severity==='失败'),{ids:runs.filter(item=>item.sourceId===source.id&&item.severity==='失败').map(item=>item.id),label:source.name+' · 失败记录'}));problem.append(link);}row.append(problem);}body.append(row);}table.append(body);$('collection-table').replaceChildren(table);
$('system-pill').textContent='后台服务运行中';$('services').replaceChildren();
const services=[
 ['collect','采集定时',catalog.sources.filter(source=>source.capabilities.schedule&&source.backgroundEnabled===true).length+' / '+catalog.sources.filter(source=>source.capabilities.schedule).length+' 已启用',active.some(item=>item.id==='collect')?'1 个来源等待重试；另 1 个按计划运行':'按来源配置的间隔运行'],
 ['process','待办处理',empty?'等待消息':'正在处理',empty?'待处理分组：尚未计算':'待处理 8 组 · 可打开 Pi 工作流'],
 ['delivery','消息投递',active.some(item=>item.id==='delivery')?'需核对结果':empty?'等待待办':'队列正常',active.some(item=>item.id==='delivery')?'1 条结果未知，未自动重发':'正式待办的投递队列'],
 ['feedback','状态回写','监听运行中','最近确认 14:31:56 · 操作仍需等待处理层保存']
];
for(const [id,name,state,note]of services){
 const row=node('div',null,'service-row'),text=node('div');row.dataset.serviceId=id;text.append(node('strong',name),node('p',note));
 const status=node('div',null,'service-state');status.append(node('span',state,'pill'+(active.some(item=>item.id===id)?' warn':'')));
 const issue=active.find(item=>item.id===id);
 if(issue){const button=node('button','查看问题','text-button');button.dataset.serviceProblem=issue.id;button.addEventListener('click',()=>openRuns(issue));status.append(button);}
 else{const link=node('button',id==='process'?'查看 Pi 工作流':'自动化设置','text-button');link.addEventListener('click',()=>{if(id==='process'){const run=workflowRuns.find(run=>run.status==='running');if(!empty&&run){workflowRunId=run.id;$('workflow-filter').value='all';$('workflow-search').value='';renderWorkflow();}actions['workflow-open']();}else openAutomation(['delivery','feedback'].includes(id)?'channels':'collection');});status.append(link);}
 row.append(text,status);$('services').append(row);
}
renderChart(empty,bad);renderResources();renderHistory();
}
function renderChart(empty,bad){const bins=[[5,0],[6,0],[6,0],[5,0],[6,0],[6,0],[6,0],[5,0],[6,0],[6,0],[6,0],[bad?6:7,bad?1:0]];const width=640,height=220,left=44,right=20,top=20,bottom=36,plot=height-top-bottom;const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox',`0 0 ${width} ${height}`);svg.setAttribute('role','group');svg.setAttribute('aria-label',empty?'尚无成功率样本':'最近24小时正式采集成功率，纵轴0到100%');const add=(tag,attrs,text)=>{const el=document.createElementNS(svg.namespaceURI,tag);for(const [key,value]of Object.entries(attrs))el.setAttribute(key,value);if(text!=null)el.textContent=text;svg.append(el);return el;};for(const pct of [0,25,50,75,100]){const y=top+plot*(1-pct/100);add('line',{x1:left,y1:y,x2:width-right,y2:y,stroke:'#e0e6e2','stroke-width':1});add('text',{x:left-8,y:y+4,'text-anchor':'end',fill:'#68756e','font-size':13},pct+'%');}for(let i=0;i<bins.length;i+=2)add('text',{x:left+i*(width-left-right)/11,y:height-10,'text-anchor':'middle',fill:'#68756e','font-size':13},['14:32','18:32','22:32','02:32','06:32','10:32'][i/2]);if(empty)add('text',{x:width/2,y:height/2,'text-anchor':'middle',fill:'#68756e','font-size':13},'尚无有效轮次，等待首次采集');else{const points=bins.map(([success,failed],i)=>`${left+i*(width-left-right)/11},${top+plot*(1-success/(success+failed))}`).join(' ');add('polyline',{points,fill:'none',stroke:'#12634f','stroke-width':2});bins.forEach(([success,failed],i)=>{const circle=add('circle',{cx:left+i*(width-left-right)/11,cy:top+plot*(1-success/(success+failed)),r:failed?5:3,fill:failed?'#a43b32':'#12634f'});const title=document.createElementNS(svg.namespaceURI,'title');title.textContent=`该时间段：${success} 成功、${failed} 失败，成功率 ${(100*success/(success+failed)).toFixed(1)}%`;circle.append(title);if(failed){circle.setAttribute('role','button');circle.setAttribute('tabindex','0');circle.setAttribute('aria-label','12:32 — 14:32：1 次失败，查看详情');const open=()=>openRuns(issues[0],{ids:['collect'],label:'12:32 — 14:32 · 正式采集失败',result:'failed'});circle.addEventListener('click',open);circle.addEventListener('keydown',event=>{if(['Enter',' '].includes(event.key)){event.preventDefault();open();}});}});}const legend=node('div',null,'legend');for(const [label,bad]of [['成功率',false],['包含失败的时间段',true]]){const span=node('span');span.append(node('i',null,bad?'bad':''),document.createTextNode(label));legend.append(span);}$('rate-chart').replaceChildren(svg,legend);}

let resourceMetric='messages';
function renderResources(){
  const empty=$('scenario').value==='empty',messageMode=resourceMetric==='messages';
  const resourceExamples=new Map([['wechat-main',{messages:947,bytes:4600000000,color:'#12634f'}],['qq-main',{messages:337,bytes:8100000000,color:'#78a592'}]]);
  const rows=catalog.sources.map(source=>{const value=resourceExamples.get(source.id);return {id:source.id,name:source.name,messages:value?(empty?0:value.messages):null,bytes:value?(empty?0:value.bytes):null,color:value?.color||'#c2a673'};});
  rows.splice(Math.min(2,rows.length),0,{id:'shared',name:'共享 / 未归属',messages:null,bytes:empty?0:1300000000,color:'#b3beb7'});
  const messageTotal=rows.reduce((sum,r)=>sum+(r.messages||0),0),byteTotal=rows.reduce((sum,r)=>sum+(r.bytes||0),0),total=messageMode?messageTotal:byteTotal;
  $('resource-messages').setAttribute('aria-pressed',String(messageMode));$('resource-storage').setAttribute('aria-pressed',String(!messageMode));
  $('resource-total').textContent=messageMode?total.toLocaleString():formatStorage(total);$('resource-unit').textContent=messageMode?'24 小时入库变更 / 条':'已统计项目文件 / 当前';
  const ns='http://www.w3.org/2000/svg',svg=document.createElementNS(ns,'svg');svg.setAttribute('viewBox','0 0 240 240');svg.setAttribute('role','img');svg.setAttribute('aria-label',messageMode?'已统计来源的消息贡献占比':'已统计项目文件的空间占比');
  const ring=(attrs,title)=>{const circle=document.createElementNS(ns,'circle');for(const [k,v]of Object.entries(attrs))circle.setAttribute(k,v);if(title){const label=document.createElementNS(ns,'title');label.textContent=title;circle.append(label);}svg.append(circle);};
  const radius=92,length=2*Math.PI*radius;ring({cx:120,cy:120,r:radius,fill:'none',stroke:'#edf1ee','stroke-width':28});let offset=0;
  if(total)for(const row of rows){const value=messageMode?row.messages:row.bytes;if(!value)continue;const portion=value/total*length;ring({cx:120,cy:120,r:radius,fill:'none',stroke:row.color,'stroke-width':28,'stroke-dasharray':`${portion} ${length-portion}`,'stroke-dashoffset':-offset,transform:'rotate(-90 120 120)'},`${row.name}：${messageMode?value.toLocaleString()+' 条':formatStorage(value)}，${(100*value/total).toFixed(1)}%`);offset+=portion;}
  $('resource-chart').replaceChildren(svg);
  const table=node('table'),header=node('tr');for(const [label,active]of [['来源',false],['消息贡献 · 24 小时',messageMode],['存储占用 · 当前',!messageMode]])header.append(node('th',label,active?'active':''));table.append(header);
  for(const row of rows){const tr=node('tr'),source=node('td'),dot=node('span',null,'source-dot');dot.style.backgroundColor=row.color;source.append(dot,document.createTextNode(row.name));tr.append(source);for(const [value,sum,mode]of [[row.messages,messageTotal,'messages'],[row.bytes,byteTotal,'storage']]){const cell=node('td',value==null?(row.id==='shared'&&mode==='messages'?'不适用':'未统计'):mode==='messages'?value.toLocaleString()+' 条':formatStorage(value),'number'+(resourceMetric===mode?' active-value':''));cell.append(node('small',value==null?(row.id==='shared'&&mode==='messages'?'不作为消息来源':'不按 0 计算'):sum?(100*value/sum).toFixed(1)+'%':'无可计算占比'));tr.append(cell);}table.append(tr);}
  const help=node('p','消息量反映产出规模，不代表信息价值。相同文件只计一次，多来源共用的目录或文件放入“共享 / 未归属”。','resource-help');$('resource-legend').replaceChildren(table,help);
  $('resource-scope').textContent=messageMode?'消息贡献：最近 24 小时已提供统计的 '+rows.filter(row=>row.messages!==null).length+' 个来源；包含新增与修订，未统计来源不按 0 处理。':'存储占用：示例盘点时间 14:32，仅含状态库、明文快照、消息批次与附件归档；不等同于整块磁盘使用率。物理占用待正式接入时实测。';
}
function formatStorage(bytes){return bytes>=1000000000?(bytes/1000000000).toFixed(1)+' GB':bytes>=1000000?(bytes/1000000).toFixed(1)+' MB':bytes.toLocaleString()+' B';}
$('resource-messages').addEventListener('click',()=>{resourceMetric='messages';renderResources();});$('resource-storage').addEventListener('click',()=>{resourceMetric='storage';renderResources();});

function renderHistory(){
 const filter=$('history-filter').value,records=runs.filter(run=>(!historyScope||historyScope.ids.includes(run.id))&&(filter==='all'||filter==='failed'&&run.severity==='失败'||filter==='succeeded'&&run.severity==='成功'||filter==='unknown'&&resultLabel(run)==='结果未知'));
 $('history-scope').textContent=historyScope?'最近 24 小时 · '+historyScope.label+' · 已保留来源与结果筛选':'代表性运行示例 · 最近 24 小时；不是完整轮次目录';
 $('history-clear-scope').hidden=!historyScope;
 const body=$('history-rows');body.replaceChildren();for(const run of records){const row=node('tr');for(const value of ['11-14 '+run.at,run.module+' / '+run.operation,run.origin,run.duration,resultLabel(run)])row.append(node('td',value));const cell=node('td'),button=node('button','查看','text-btn');button.dataset.dashboardRun=run.id;button.addEventListener('click',()=>{openRuns({id:run.id,name:historyScope?.label||'运行记录'},{ids:records.map(item=>item.id),label:historyScope?.label||'运行记录',result:filter});});cell.append(button);row.append(cell);body.append(row);}if(!records.length){const row=node('tr'),cell=node('td','当前范围没有符合条件的运行，筛选仍保留');cell.colSpan=6;row.append(cell);body.append(row);}
}
$('show-history').addEventListener('click',()=>{historyScope={...runScope,ids:[...runScope.ids]};const result=$('run-result-filter').value;$('run-dialog').close();$('history-filter').value=result;navigate('history');renderHistory();});
$('history-clear-scope').addEventListener('click',()=>{historyScope=null;$('history-filter').value='all';renderHistory();});
$('overview-return-run').addEventListener('click',restoreRun);

window.overviewDashboard={render,renderHistory,examples:{runs},restoreRun};
$('run-result-filter').addEventListener('change',renderRunList);$('scenario').addEventListener('change',render);$('show-records').addEventListener('click',()=>openRuns());$('close-dialog').addEventListener('click',()=>$('run-dialog').close());render();
})();
