'use strict';
const $ = id => document.getElementById(id);
let token = '', currentConfig = null, pendingPreview = null, previewShown = null, imageUrl = null, refreshing = false;
const names = Object.assign(Object.create(null),{processor:'待办处理',messaging:'消息投递',feedback:'状态回写'}), actions = {collect:'正式采集',preview:'筛选预览',replay:'时间段重放',processor_preview:'上下文预览',process:'提取待办',deliver:'发送与提醒',feedback:'同步状态'};
const states = {running:'执行中',retry:'等待重试',succeeded:'已完成',warning:'完成但有异常',failed:'失败',interrupted:'结果未确认'};
const intensities = {metadata:'轻量',standard:'标准',full:'完整'};
const assetStates = {deferred:'按强度暂缓保存',archived:'已归档',missing:'本地未找到',thumbnail_only:'仅缩略图',unverified:'内容待核验',encrypted_only:'仅保留密文',unsupported:'格式未支持'};
let sourceCatalog=[], lastStatus=null, sourceSignature='', sourceGeneration=0;
const sourceDrafts=new Map();
const selected = () => $('module').value;
const sourceInfo = (id=selected()) => sourceCatalog.find(source=>source.id===id);
function requireSource(capability){if(!sourceInfo()?.capabilities?.[capability])throw Error('此信息源不支持该操作');}
const sourcePath=(id=selected())=>'/modules/'+encodeURIComponent(id);
function fields(){return sourceInfo()?.fields||[];}
function rememberSource(){
  const id=$('module').dataset.loaded;if(!id)return;
  const values={};for(const input of document.querySelectorAll('#query input,#query textarea,#rulesets-editor input,#rulesets-editor textarea,#messages input,#config-fields input,#config-fields textarea,#config-fields select'))values[input.id]={value:input.value,checked:input.checked};
  sourceDrafts.set(id,{values,config:currentConfig,pending:pendingPreview,shown:previewShown,preview:[...$('preview-output').childNodes],messages:[...$('message-output').childNodes]});
}
async function loadCatalog(){
  const catalog=await api('/catalog');sourceCatalog=catalog.sources;
  for(const id of ['module','settings-module','automation-module']){$(id).replaceChildren();for(const source of sourceCatalog){names[source.id]=source.name;const option=el('option',source.name);option.value=source.id;$(id).append(option);}}
}

function el(tag, text, className) { const node=document.createElement(tag); if(text!==undefined)node.textContent=text; if(className)node.className=className; return node; }
function notice(text, bad=false) {$('notice').textContent=text;$('notice').className=bad?'error':'';$('notice').hidden=false;}
function errorText(detail){return typeof detail==='string'?detail:JSON.stringify(detail);}
async function api(path, method='GET', data) {
  const response=await fetch('/api'+path,{method,headers:{'X-Console-Token':token,...(data===undefined?{}:{'Content-Type':'application/json'})},body:data===undefined?undefined:JSON.stringify(data)});
  const result=await response.json(); if(!response.ok)throw Error(errorText(result.detail||result)); return result;
}
function time(value){return value?new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(new Date(value*1000)):'尚无记录';}
function localValue(sec){return new Date(sec*1000+8*3600000).toISOString().slice(0,19);}
function windowValues(){const start=Date.parse($('start').value+'+08:00')/1000,end=Date.parse($('end').value+'+08:00')/1000;if(!Number.isInteger(start)||!Number.isInteger(end)||end<=start)throw Error('请填写有效的开始与结束时间');return {start,end};}
function ruleValues(){let rules;try{rules=JSON.parse($('filter-rulesets').value||'[]');}catch{throw Error('规则集注册表必须是有效 JSON');}if(!Array.isArray(rules))throw Error('规则集注册表必须是数组');return rules;}
function filters(){validateRuleInputs();return {rulesets:ruleValues()};}
function lastDay(){const now=Math.floor(Date.now()/1000);$('start').value=localValue(now-86400);$('end').value=localValue(now);}
function button(text, handler, secondary=true){const node=el('button',text,secondary?'secondary':'');node.addEventListener('click',()=>guard(handler,node));return node;}
async function guard(fn,node){if(node)node.disabled=true;try{await fn();}catch(error){notice(error.message,true);}finally{if(node)node.disabled=false;}}
function metric(title,value,small=false){const node=el('div',undefined,'metric');node.append(el('span',title),el('strong',String(value),small?'small':''));return node;}
function sourceControls(id,state,compact=false){
  const source=sourceInfo(id),controls=el('div',undefined,'actions'),running=!!state.running_job;
  if(source.capabilities.collect){const run=button('执行一轮',async()=>{if(!sourceInfo(id)?.capabilities.collect)throw Error('此来源不支持采集');const job=await api(sourcePath(id)+'/collect','POST');notice(`${source.name}采集已启动：${job.id.slice(0,8)}`);await refresh();},false);run.disabled=running;controls.append(run);}
  if(source.capabilities.schedule){const link=el('a','自动化设置','button secondary');link.href='#automation';controls.append(link);}
  if(!compact)controls.append(button('查看来源',async()=>{$('module').value=id;await switchModule();location.hash='sources';}));
  return controls;
}
function updateSourceStatus(){
  const id=selected(),source=sourceInfo(),state=lastStatus?.modules[id]||{};
  if(!source)return;
  $('workspace-title').textContent=source.name;
  $('source-summary').textContent=source.readonly?(source.description||'此来源尚未提供控制台适配能力'):`${state.messages??'—'} 条已采集 · 快照 ${time(state.snapshot_updated_at)}${state.state_error?' · '+state.state_error:''}`;
  const signature=JSON.stringify([id,state.scheduler_enabled,state.running_job]);
  if(signature!==sourceSignature){sourceSignature=signature;$('source-controls').replaceChildren(...sourceControls(id,state,true).children);}
}
function renderStatus(data){
  lastStatus=data;
  if(data.external_runner)notice(data.external_runner.message+'。运行 tools/handover.cmd 可在轮次之间切换。',true);
  for(const source of sourceCatalog){
    const id=source.id,state=data.modules[id]||{},signature=JSON.stringify({...state,next_run_in:undefined});let card=$('source-card-'+id);
    if(!card){card=el('article',undefined,'module-card');card.id='source-card-'+id;$('modules').append(card);}
    if(card.dataset.signature===signature)continue;card.dataset.signature=signature;card.replaceChildren();
    const top=el('div',undefined,'module-top');top.append(el('h2',source.name),el('span',source.readonly?'只读来源':state.running_job?'执行中':'手动调试就绪','pill'));card.append(top);
    if(!source.readonly){const metrics=el('div',undefined,'metrics');metrics.append(metric('已采集消息',state.messages??'—'),metric('待发布事件',state.pending_events??'—'),metric('成功时间终点',time(state.checkpoint),true),metric('快照更新时间',time(state.snapshot_updated_at),true));card.append(metrics);}
    card.append(el('p',source.readonly?source.description:`${intensities[state.intensity]||state.intensity||'—'} · ${state.refresh_interval_seconds?`数据库至少间隔 ${state.refresh_interval_seconds} 秒刷新`:'每轮刷新数据库'}${state.attachments_enabled?'':' · 附件保存关闭'} · 计划见自动化`,'muted'),sourceControls(id,state));
    if(state.state_error||state.scheduler_error){const issue=button((state.state_error||state.scheduler_error)+' · 查看相关记录',()=>openHistoryFor(id));issue.className='text-button error';card.append(issue);}
  }
  if(!sourceCatalog.length)$('modules').replaceChildren(el('p','尚未配置控制台来源。可先查看待办处理或在本机配置中接入来源。','empty'));
  else $('modules').querySelector(':scope>p')?.remove();
  updateSourceStatus();
  $('collector-path').textContent='关联项目：'+data.collector_root;
  $('connection').textContent='已连接 · '+time(Date.now()/1000);$('connection').className='muted';
}
async function refresh(){
  if(refreshing)return;refreshing=true;
  try{
    renderStatus(await api('/status'));
    const pending=pendingPreview,generation=sourceGeneration;
    if(pending&&pending.module===selected()&&previewShown!==pending.id){
      const job=await api('/jobs/'+encodeURIComponent(pending.id));
      if(generation===sourceGeneration&&pendingPreview?.id===pending.id&&pending.module===selected()&&job.status!=='running'){
        previewShown=job.id;
        if(job.status==='succeeded'){renderMessages(job.result.messages,$('preview-output'),true,pending.module);$('result-scope').textContent='现有快照 · 只读预览';$('preview-output').prepend(el('p',`命中 ${job.result.matched} 条，显示最近 ${job.result.messages.length} 条。使用现有快照；未推进断点或归档附件。`));}
        else{$('preview-output').replaceChildren(el('p',job.error||'预览失败','empty'));notice(job.error||'预览失败',true);}
      }
    }
  }catch(error){$('connection').textContent='连接异常：'+error.message;$('connection').className='bad';}
  finally{refreshing=false;}
  await guard(pollProcessor);
}
function renderRuleSummary(){
  renderRules();
}
async function loadConfig(generation=sourceGeneration){
  if(!sourceInfo()?.capabilities.configure){currentConfig=null;$('config-fields').replaceChildren(el('p','此来源未提供可编辑配置。','scope'));$('raw-config').textContent='未提供配置描述';$('config-file').textContent='';return;}
  const module=selected(),data=await api(sourcePath()+'/config');if(module!==selected()||generation!==sourceGeneration)return;currentConfig={...data,source_id:module};
  $('config-file').textContent=data.file;$('filter-rulesets').value=JSON.stringify(data.config.filters?.rulesets||[],null,2);$('config-fields').replaceChildren();
  for(const {section,key,label:title,type,options,min,max} of fields()){
    const value=data.config[section]?.[key];if(value===undefined)continue;
    const label=el('label',title),input=el(type==='array'?'textarea':type==='select'?'select':'input');input.id=`cfg-${section}-${key}`;
    if(type==='select'){for(const [value,title] of Object.entries(options||{})){const option=el('option',title);option.value=value;input.append(option);}input.value=value;}
    else if(type==='array'){input.value=value.join('\n');input.rows=2;}
    else{input.type=type;if(type==='checkbox')input.checked=value;else input.value=value;if(type==='number'){if(min!=null)input.min=min;if(max!=null)input.max=max;input.step=1;input.required=true;}}
    label.append(input);$('config-fields').append(label);
  }
  $('raw-config').textContent=JSON.stringify(data.config,null,2);renderRuleSummary();await discoverRules(generation);
}
async function saveConfig(){
  requireSource('configure');if(!currentConfig)throw Error('请先加载配置');
  const patch={filters:filters()};
  for(const {section,key,type}of fields()){const input=$(`cfg-${section}-${key}`);if(!input)continue;if(!input.reportValidity())return;const value=type==='checkbox'?input.checked:type==='number'?Number(input.value):type==='array'?input.value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean):input.value.trim();
    if(JSON.stringify(value)!==JSON.stringify(currentConfig.config[section]?.[key]))(patch[section]??={})[key]=value;
  }
  if(!confirm('保存后用于后续采集轮次。旧配置会备份，但不会移动已有文件或回放历史。确定保存？'))return;
  const id=selected(),saved=await api(sourcePath(id)+'/config','PATCH',{revision:currentConfig.revision,patch});
  const config={...saved,source_id:id};
  if(selected()===id&&currentConfig?.source_id===id){currentConfig=config;$('raw-config').textContent=JSON.stringify(saved.config,null,2);}
  const draft=sourceDrafts.get(id);if(draft)draft.config=config;
  notice(`${names[id]||id}配置已保存并备份；保存期间的新编辑保留在表单中。`);await refresh();
}
async function showJob(id,source){
  const job=await api(source==='automation'?'/activity/automation/'+encodeURIComponent(id):'/activity/console/'+encodeURIComponent(id)),box=$('job-detail');box.replaceChildren();
  box.append(el('h3',`${names[job.module]||job.module} · ${actions[job.action]||job.action}`),el('p',`${originLabels[job.origin]||job.origin||'未注明'} · ${states[job.status]||job.status} · ${durationText(job.duration_seconds,!!job.finished_at)}`));
  for(const attempt of job.attempts||[]){const section=el('section',undefined,'module-card');section.append(el('h4',`第 ${attempt.number} 次 · ${states[attempt.status]||attempt.status}`),el('p',`${time(attempt.started_at)} → ${attempt.finished_at?time(attempt.finished_at):'尚无结束时间'}`,'muted'));if(attempt.error)section.append(el('p',attempt.error,'error'));if(attempt.result)section.append(el('pre',JSON.stringify(attempt.result,null,2)));if(attempt.details){const detail=el('details');detail.append(el('summary','错误位置'),el('pre',JSON.stringify(attempt.details,null,2)));section.append(detail);}box.append(section);}
  const controls=el('div',undefined,'actions');controls.append(button('复制完整记录',()=>navigator.clipboard.writeText(JSON.stringify(job,null,2))));
  const configure=el('a','打开相关配置','button secondary');configure.href=source==='automation'?'#automation':job.module==='processor'?'#settings/model':'#settings/source';configure.addEventListener('click',()=>{$('detail-dialog').close();if(source!=='automation'&&sourceInfo(job.module)){$('module').value=job.module;guard(switchModule);}});controls.append(configure);
  if(job.action==='process'&&source!=='automation')controls.append(button('查看 Pi 工作流',async()=>{$('detail-dialog').close();await selectWorkflow(job.id);}));
  for(const run of job.workflow_runs||[])controls.append(button(`查看第 ${run.attempt||'?'} 次 Pi 工作流`,async()=>{$('detail-dialog').close();await selectWorkflow(run.id);}));
  box.append(controls);
  if(job.log_tail)box.append(el('h4','运行日志'),el('pre',job.log_tail));const raw=el('details');raw.append(el('summary','完整记录'),el('pre',JSON.stringify(job,null,2)));box.append(raw);if(!$('detail-dialog').open)$('detail-dialog').showModal();
}
function identitySummary(m){
  const identity=m.identity||{},account=identity.account||{},chat=identity.conversation||{},person=(identity.people||[]).find(p=>p.id===m.sender_id)||{};
  const sender=m.sender||person.label||person.display_name||'名称未获取';
  return [account.label||account.nickname||'账号名称未获取',chat.display_name||chat.remark||chat.name||'会话名称未获取',`${person.is_self?'我 · ':''}${sender}`];
}
function renderMessageDetails(records,target,preview=false,module=selected()){
  target.replaceChildren();if(!records.length){target.append(el('p','此范围内没有匹配的消息。','empty'));return;}
  for(const record of records){const m=preview?record:record.message,box=el('article',undefined,'message'),head=el('div',undefined,'message-head');
    head.append(el('span',time(m.sent_at)),el('span',m.kind+(m.mentions_all?' · @所有人':'')),...identitySummary(m).map(value=>el('span',value)));
    box.append(head,el('div',m.text||'此消息没有可显示的文本','message-text'));
    const matchedRules=record.matched_rules||m.matched_rules;
    box.append(el('p',matchedRules?.length?'命中规则：'+matchedRules.map(rule=>rule.file).join('、'):'历史记录未标记命中规则','muted'));
    for(const a of m.attachments||[]){const item=el('div',undefined,'asset');item.append(el('strong',`${a.name||a.type} · ${assetStates[a.status]||'仅元数据'}`));
      item.append(el('div',[a.reason,a.quality,a.dimensions?`${a.dimensions.join(' × ')} px`:null,a.byte_size?`${a.byte_size.toLocaleString()} bytes`:null,a.message_match_verified===false?'关联/原件内容尚未核实':null].filter(Boolean).join(' · ')));
      if(a.archive_path)item.append(el('div',a.archive_path));
      if(a.archive_path&&!preview){const file=a.archive_path.split(/[\\/]/).pop(),controls=el('div',undefined,'actions');
        if(a.type==='image'&&/\.(jpg|jpeg|png|gif|webp|bmp)$/.test(file))controls.append(button('预览图片',()=>openAsset(module,record.seq,file,true)));
        controls.append(button('保存副本',()=>openAsset(module,record.seq,file,false)));item.append(controls);
      }
      box.append(item);
    }
    const detail=el('details'),summary=el('summary','完整结构与附件变体');detail.append(summary,el('pre',JSON.stringify(record,null,2)));box.append(detail);target.append(box);
  }
}
function inspectMessage(record,preview,module,row){
  document.querySelectorAll('.message-row').forEach(n=>n.setAttribute('aria-pressed',String(n===row)));
  if(matchMedia('(max-width:1180px)').matches){renderMessageDetails([record],$('message-dialog-body'),preview,module);$('message-dialog').showModal();}
  else renderMessageDetails([record],$('message-inspector'),preview,module);
}
function renderMessages(records,target,preview=false,module=selected()){
  if(!['preview-output','message-output'].includes(target.id)){renderMessageDetails(records,target,preview,module);return;}
  target.replaceChildren();
  if(!records?.length){target.append(el('p','此范围内没有匹配的消息。','empty'));return;}
  for(const record of records){const m=preview?record:record.message,row=el('button',undefined,'message-row'),head=el('div',undefined,'message-head');row.type='button';row.setAttribute('aria-pressed','false');head.append(el('span',time(m.sent_at)),el('span',identitySummary(m)[1]),el('span',identitySummary(m)[2]));row.append(head,el('div',m.text||'此消息没有可显示的文本','excerpt'),el('small',`${m.kind||'消息'}${m.attachments?.length?' · '+m.attachments.length+' 个附件':''}`));row.dataset.search=[m.text,...identitySummary(m),m.conversation_id].join(' ').toLowerCase();row.dataset.attachment=String(!!m.attachments?.length);row.addEventListener('click',()=>inspectMessage(record,preview,module,row));target.append(row);}
  filterResults();
}
function filterResults(){
  const query=$('result-search').value.toLowerCase(),attachment=$('result-attachment').value;
  for(const target of [$('preview-output'),$('message-output')]){let count=0;for(const row of target.querySelectorAll('.message-row')){row.hidden=!row.dataset.search.includes(query)||(attachment&&row.dataset.attachment!==String(attachment==='yes'));if(!row.hidden)count++;}target.querySelector('.filter-empty')?.remove();if(target.querySelector('.message-row')&&!count)target.append(el('p','当前搜索没有匹配项，调整关键词或附件条件。','empty filter-empty'));}
}
async function openAsset(module,seq,file,inline){
  const response=await fetch(`/api/modules/${encodeURIComponent(module)}/messages/${seq}/assets/${encodeURIComponent(file)}?inline=${inline}`,{headers:{'X-Console-Token':token}});
  if(!response.ok)throw Error(errorText((await response.json()).detail));
  const url=URL.createObjectURL(await response.blob());
  if(inline){if(imageUrl)URL.revokeObjectURL(imageUrl);imageUrl=url;$('asset-image').src=url;$('image-dialog').showModal();}
  else{const a=el('a');a.href=url;a.download=file;a.click();setTimeout(()=>URL.revokeObjectURL(url),30000);}
}
function bind(id,fn){$(id).addEventListener('click',()=>guard(fn,$(id)));}
bind('last-day',lastDay);bind('reload-config',async()=>{if(confirm('重新加载会丢弃当前来源尚未保存的编辑，继续？'))await loadConfig();});bind('save-config',saveConfig);
bind('rules-refresh',()=>discoverRules());bind('rules-save',saveConfig);
bind('preview',async()=>{
  requireSource('query');const window=windowValues();if(window.end-window.start>7*86400)throw Error('预览每次最多 7 天，请缩小范围');
  const module=selected(),generation=sourceGeneration,job=await api(sourcePath(module)+'/preview','POST',{...window,...(sourceInfo()?.capabilities.sql?{filters:filters()}:{}),limit:20});
  const pending={id:job.id,module};
  if(module!==selected()||generation!==sourceGeneration){const draft=sourceDrafts.get(module);if(draft){draft.pending=pending;draft.shown=null;}return;}
  pendingPreview=pending;previewShown=null;$('preview-output').replaceChildren(el('p','正在查询现有快照，可在运行记录查看进度…','empty'));await refresh();
});
bind('replay',async()=>{requireSource('replay');const window=windowValues();if(!confirm('重放使用已保存的筛选配置，可能写入新批次、更新修订并保存附件；不会移动定时采集断点。继续？'))return;const job=await api(sourcePath()+'/replay','POST',window);notice('重放已启动：'+job.id.slice(0,8));await refresh();});
bind('load-messages',async()=>{requireSource('messages');const module=selected(),params=new URLSearchParams({limit:'20',keyword:$('message-keyword').value,conversation:$('message-conversation').value});const result=await api(sourcePath()+'/messages?'+params);if(module===selected())renderMessages(result.messages,$('message-output'),false,module);});
bind('copy-token',async()=>{await navigator.clipboard.writeText(token);notice('API 凭证已复制。在 OpenAPI 页面点击 Authorize 粘贴；服务重启后凭证失效。');});
bind('close-detail',()=>$('detail-dialog').close());bind('close-image',()=>$('image-dialog').close());
$('image-dialog').addEventListener('close',()=>{if(imageUrl)URL.revokeObjectURL(imageUrl);imageUrl=null;$('asset-image').removeAttribute('src');});
async function switchModule(){
  rememberSource();const generation=++sourceGeneration;$('module').dataset.loaded='';$('config-fields').inert=true;$('rulesets-editor').inert=true;document.querySelector('.filter-scroll').inert=true;
  const id=selected(),source=sourceInfo(),draft=sourceDrafts.get(id);currentConfig=null;pendingPreview=null;previewShown=null;
  $('settings-module').value=id;$('automation-module').value=id;sourceSignature='';updateSourceStatus();
  $('processing-help').hidden=!source?.capabilities.configure;
  $('source-readonly').hidden=!source?.readonly;$('source-readonly').textContent=source?.description||'此来源未提供控制台查询能力';
  for(const [id,cap]of [['preview','query'],['replay','replay'],['load-messages','messages'],['save-config','configure'],['reload-config','configure']])$(id).hidden=!source?.capabilities?.[cap];
  $('filter-rulesets').disabled=!source?.capabilities.sql;
  document.querySelector('[data-panel="query"]').hidden=!source?.capabilities.query;document.querySelector('[data-panel="messages"]').hidden=!source?.capabilities.messages;
  $('query').hidden=!source?.capabilities.query;$('messages').hidden=!source?.capabilities.messages;
  await loadConfig(generation);if(generation!==sourceGeneration||id!==selected())return;
  if(draft?.config)currentConfig=draft.config;
  $('module').dataset.loaded=id;$('config-fields').inert=false;$('rulesets-editor').inert=false;document.querySelector('.filter-scroll').inert=false;
  if(draft){for(const [id,value]of Object.entries(draft.values)){if($(id)){$(id).value=value.value;$(id).checked=value.checked;}}pendingPreview=draft.pending;previewShown=draft.shown;$('preview-output').replaceChildren(...draft.preview);$('message-output').replaceChildren(...draft.messages);}
  else{lastDay();$('message-keyword').value='';$('message-conversation').value='';$('preview-output').replaceChildren(el('p',source?.capabilities.query?'选择时间与条件，预览现有快照。':'此来源没有查询能力。','empty'));$('message-output').replaceChildren(el('p','点击“查看最近 20 条”读取消息。','empty'));}
  renderRuleSummary();$('message-inspector').replaceChildren(el('p','选择一条消息，核对来源与附件。','empty'));
  if(draft)for(const [id,value]of Object.entries(draft.values))if(/^rule-(param|recent)-/.test(id)&&$(id))$(id).value=value.value;
  if(source?.capabilities.query)activatePanel('query');else if(source?.capabilities.messages)activatePanel('messages');else{$('preview-output').hidden=false;$('message-output').hidden=true;document.querySelector('.filter-footer').hidden=true;}
}
$('module').addEventListener('change',()=>guard(switchModule));
$('settings-module').addEventListener('change',()=>{$('module').value=$('settings-module').value;guard(switchModule);});
$('automation-module').addEventListener('change',()=>{$('module').value=$('automation-module').value;guard(switchModule);});
function markQueryChanged(){if($('preview-output').querySelector('.message-row'))$('result-scope').textContent='条件已修改 · 结果待更新';}
for(const id of ['start','end','filter-rulesets'])$(id).addEventListener('input',markQueryChanged);
$('last-day').addEventListener('click',markQueryChanged);
$('filter-rulesets').addEventListener('input',renderRuleSummary);
$('result-search').addEventListener('input',filterResults);$('result-attachment').addEventListener('change',filterResults);
bind('close-message',()=>$('message-dialog').close());
async function init(){lastDay();const response=await fetch('/api/session');if(!response.ok)throw Error('无法建立本机会话');token=(await response.json()).token;await loadCatalog();await switchModule();await refresh();await initProcessor();await initWorkflow();await initHistory();setInterval(refresh,3000);}
init().catch(error=>notice(error.message,true));
