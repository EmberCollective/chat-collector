'use strict';
const $ = id => document.getElementById(id);
let token = '', currentConfig = null, pendingPreview = null, previewShown = null, imageUrl = null, refreshing = false;
const names = {wechat:'微信', qq:'QQ',processor:'待办处理'}, actions = {collect:'正式采集',preview:'筛选预览',replay:'时间段重放',processor_preview:'上下文预览',process:'提取待办'};
const states = {running:'执行中',succeeded:'已完成',failed:'失败',interrupted:'结果未确认'};
const intensities = {metadata:'轻量',standard:'标准',full:'完整'};
const assetStates = {deferred:'按强度暂缓保存',archived:'已归档',missing:'本地未找到',thumbnail_only:'仅缩略图',unverified:'内容待核验',encrypted_only:'仅保留密文',unsupported:'格式未支持'};
const selected = () => $('module').value;
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
function filters(){return {rulesets:ruleValues()};}
function lastDay(){const now=Math.floor(Date.now()/1000);$('start').value=localValue(now-86400);$('end').value=localValue(now);}
function button(text, handler, secondary=true){const node=el('button',text,secondary?'secondary':'');node.addEventListener('click',()=>guard(handler,node));return node;}
async function guard(fn,node){if(node)node.disabled=true;try{await fn();}catch(error){notice(error.message,true);}finally{if(node)node.disabled=false;}}
function metric(title,value,small=false){const node=el('div',undefined,'metric');node.append(el('span',title),el('strong',String(value),small?'small':''));return node;}
function renderStatus(data){
  $('modules').replaceChildren();
  if(data.external_runner)notice(data.external_runner.message+'。运行 tools/handover.cmd 可在轮次之间切换。',true);
  for(const name of ['wechat','qq']){
    const state=data.modules[name],card=el('article',undefined,'module-card'),top=el('div',undefined,'module-top');
    const running=state.running_job?data.jobs.find(j=>j.id===state.running_job):null;
    top.append(el('h2',names[name]),el('span',running?`${actions[running.action]}中`:'手动调试就绪','pill'));
    const metrics=el('div',undefined,'metrics');metrics.append(metric('已采集消息',state.messages??'—'),metric('待发布事件',state.pending_events??'—'),metric('成功时间终点',time(state.checkpoint),true),metric('快照更新时间',time(state.snapshot_updated_at),true));
    const controls=el('div',undefined,'actions');
    const run=button('执行一轮',async()=>{const job=await api(`/modules/${name}/collect`,'POST');notice(`${names[name]}采集已启动，任务 ${job.id.slice(0,8)}`);await refresh();},false);run.disabled=!!running;
    controls.append(run);
    const automatic = el('a','自动任务设置','text-button'); automatic.href='#automation-panel'; controls.append(automatic);
    controls.append(button('刷新与附件强度',async()=>{if(selected()!==name){$('module').value=name;await switchModule();}else{await loadConfig();}$('tab-settings').click();$('cfg-schedule-refresh_interval_seconds').focus();}));
    card.append(top,metrics,el('p',running?`任务耗时 ${Math.max(0,Math.floor(Date.now()/1000-running.started_at))} 秒 · 刷新数据库、筛选或处理附件`:`数据库${state.refresh_interval_seconds?`至少间隔 ${state.refresh_interval_seconds} 秒刷新`:'每轮刷新'} · ${intensities[state.intensity]||'完整'}${state.attachments_enabled?'':' · 附件保存已关闭'} · 定时状态见“自动任务”`,'muted'),controls);
    if(state.state_error)card.append(el('p',state.state_error,'pill error'));
    $('modules').append(card);
  }
  $('collector-path').textContent='关联收集项目：'+data.collector_root;
  $('jobs').replaceChildren();
  for(const job of data.jobs){const tr=el('tr');tr.append(el('td',time(job.started_at)),el('td',`${names[job.module]} / ${actions[job.action]}`),el('td',job.origin==='schedule'?'定时':'手动'),el('td',states[job.status]||job.status));const td=el('td');td.append(button('查看',()=>showJob(job.id)));tr.append(td);$('jobs').append(tr);}
  if(!data.jobs.length){const tr=el('tr'),td=el('td','本控制台尚无任务记录');td.colSpan=5;tr.append(td);$('jobs').append(tr);}
  $('connection').textContent='已连接 · '+time(Date.now()/1000);$('connection').className='muted';
}
async function refresh(){
  if(refreshing)return;refreshing=true;
  try{renderStatus(await api('/status'));if(pendingPreview&&pendingPreview.module===selected()&&previewShown!==pendingPreview.id){const job=await api('/jobs/'+pendingPreview.id);if(job.status!=='running'){previewShown=job.id;if(job.status==='succeeded'){renderMessages(job.result.messages,$('preview-output'),true,pendingPreview.module);$('preview-output').prepend(el('p',`命中 ${job.result.matched} 条，显示最近 ${job.result.messages.length} 条。使用现有快照；未推进断点或归档附件。`));}else{$('preview-output').replaceChildren(el('p',job.error||'预览失败','empty'));notice(job.error||'预览失败',true);}}}}
  catch(error){$('connection').textContent='连接异常：'+error.message;$('connection').className='bad';}
  finally{refreshing=false;}
  await guard(pollProcessor);
}
const fields=[
 ['schedule','refresh_interval_seconds','数据库刷新最小间隔（秒，0 为每轮）','number'],['processing','intensity','附件抓取解码强度','select'],['schedule','initial_lookback_seconds','首次回溯时长（秒）','number'],
 ['source','encrypted_dir','原始加密数据库目录','text'],['source','decrypted_dir','明文快照目录','text'],
 ['source','work_dir','QQ 临时工作目录（建议放 SSD）','text'],
 ['source','media_dir','原始媒体目录','text'],['source','key_file','本机密钥文件路径（不显示内容）','text'],
 ['source','resource_db','微信资源索引数据库','text'],['source','image_cache_dir','微信图片缓存目录','text'],
 ['storage','output_dir','消息批次输出目录','text'],['attachments','archive_dir','附件归档目录','text'],
 ['attachments','allowed_roots','其他允许读取的目录，每行一个','array'],['source','refresh','正式采集时刷新解密快照','checkbox'],['attachments','enabled','正式采集时保存附件','checkbox']
];
async function loadConfig(){
  const module=selected(),data=await api(`/modules/${module}/config`);if(module!==selected())return;currentConfig=data;
  $('workspace-title').textContent=names[module]+' · 调试工作区';$('config-file').textContent=data.file;
  $('filter-rulesets').value=JSON.stringify(data.config.filters?.rulesets||[],null,2);
  $('config-fields').replaceChildren();
  for(const [section,key,title,type] of fields){
    const value=data.config[section]?.[key];if(value===undefined)continue;
    const label=el('label',title),input=el(type==='array'?'textarea':type==='select'?'select':'input');input.id=`cfg-${section}-${key}`;
    if(type==='select'){for(const [key,title] of Object.entries(intensities)){const option=el('option',title);option.value=key;input.append(option);}input.value=value;}
    else if(type==='array'){input.value=value.join('\n');input.rows=2;}
    else{input.type=type;if(type==='checkbox')input.checked=value;else input.value=value;if(type==='number'){input.min=key==='refresh_interval_seconds'?0:key==='interval_seconds'?5:1;if(key!=='refresh_interval_seconds')input.max=key==='interval_seconds'?86400:31*86400;input.step=1;input.required=true;}}
    label.append(input);$('config-fields').append(label);
  }
  $('raw-config').textContent=JSON.stringify(data.config,null,2);
}
async function saveConfig(){
  if(!currentConfig)throw Error('请先加载配置');
  const patch={filters:filters()};
  for(const [section,key,,type]of fields){const input=$(`cfg-${section}-${key}`);if(!input)continue;if(!input.reportValidity())return;const value=type==='checkbox'?input.checked:type==='number'?Number(input.value):type==='array'?input.value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean):input.value.trim();
    if(JSON.stringify(value)!==JSON.stringify(currentConfig.config[section]?.[key]))(patch[section]??={})[key]=value;
  }
  if(!confirm('保存后用于后续采集轮次。旧配置会备份，但不会移动已有文件或回放历史。确定保存？'))return;
  await api(`/modules/${selected()}/config`,'PATCH',{revision:currentConfig.revision,patch});notice('配置已校验并保存，旧版本已备份。');await loadConfig();await refresh();
}
async function showJob(id){const job=await api('/jobs/'+id);$('job-detail').textContent=JSON.stringify(job,null,2);$('detail-dialog').showModal();}
function identitySummary(m){
  const identity=m.identity||{},account=identity.account||{},chat=identity.conversation||{},person=(identity.people||[]).find(p=>p.id===m.sender_id)||{};
  const sender=m.sender||person.label||person.display_name||'名称未获取';
  return [account.label||account.nickname||'账号名称未获取',chat.display_name||chat.remark||chat.name||'会话名称未获取',`${person.is_self?'我 · ':''}${sender}`];
}
function renderMessages(records,target,preview=false,module=selected()){
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
async function openAsset(module,seq,file,inline){
  const response=await fetch(`/api/modules/${module}/messages/${seq}/assets/${encodeURIComponent(file)}?inline=${inline}`,{headers:{'X-Console-Token':token}});
  if(!response.ok)throw Error(errorText((await response.json()).detail));
  const url=URL.createObjectURL(await response.blob());
  if(inline){if(imageUrl)URL.revokeObjectURL(imageUrl);imageUrl=url;$('asset-image').src=url;$('image-dialog').showModal();}
  else{const a=el('a');a.href=url;a.download=file;a.click();setTimeout(()=>URL.revokeObjectURL(url),30000);}
}
function bind(id,fn){$(id).addEventListener('click',()=>guard(fn,$(id)));}
bind('last-day',lastDay);bind('reload-config',loadConfig);bind('save-config',saveConfig);
bind('preview',async()=>{const module=selected(),job=await api(`/modules/${module}/preview`,'POST',{...windowValues(),filters:filters(),limit:20});pendingPreview={id:job.id,module};previewShown=null;$('preview-output').replaceChildren(el('p','正在查询现有快照，可在下方任务记录查看进度…','empty'));await refresh();});
bind('replay',async()=>{const window=windowValues();if(!confirm('重放使用已保存的筛选配置，可能写入新批次、更新修订并保存附件；不会移动定时采集断点。继续？'))return;const job=await api(`/modules/${selected()}/replay`,'POST',window);notice('重放已启动：'+job.id.slice(0,8));await refresh();});
bind('load-messages',async()=>{const module=selected(),params=new URLSearchParams({limit:'20',keyword:$('message-keyword').value,conversation:$('message-conversation').value});const result=await api(`/modules/${module}/messages?${params}`);if(module===selected())renderMessages(result.messages,$('message-output'),false,module);});
bind('copy-token',async()=>{await navigator.clipboard.writeText(token);notice('API 凭证已复制。在 OpenAPI 页面点击 Authorize 粘贴；服务重启后凭证失效。');});
bind('close-detail',()=>$('detail-dialog').close());bind('close-image',()=>$('image-dialog').close());
$('image-dialog').addEventListener('close',()=>{if(imageUrl)URL.revokeObjectURL(imageUrl);imageUrl=null;$('asset-image').removeAttribute('src');});
async function switchModule(){currentConfig=null;pendingPreview=null;$('preview-output').replaceChildren(el('p','已切换模块，请重新预览。','empty'));$('message-output').replaceChildren(el('p','点击“查看最近 20 条”读取消息。','empty'));await loadConfig();}
$('module').addEventListener('change',()=>guard(switchModule));
document.querySelectorAll('[data-tab]').forEach(tab=>tab.addEventListener('click',()=>{document.querySelectorAll('[data-tab]').forEach(other=>{const active=tab===other;other.setAttribute('aria-selected',String(active));$(other.dataset.tab).hidden=!active;});}));
async function init(){lastDay();const response=await fetch('/api/session');if(!response.ok)throw Error('无法建立本机会话');token=(await response.json()).token;await loadConfig();await refresh();await initProcessor();setInterval(refresh,3000);}
init().catch(error=>notice(error.message,true));
