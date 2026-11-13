'use strict';
let processorConfig = null, processorRules = null, processorJob = null, processorBusy = false, processorConfigured = false;
const processorFields = [
  ['model','base_url','模型 API 根地址（OpenAI 兼容，通常以 /v1 结尾）','text'],
  ['model','model','模型名称','text'],
  ['model','api_key_env','密钥环境变量名称','text'],
  ['model','timeout_seconds','每批 Agent 总超时（秒）','number'],
  ['model','vision','模型支持读取图片','checkbox'],
  ['model','context_window','模型上下文容量（tokens）','number'],
  ['model','max_output_tokens','每次回复最多 tokens','number'],
  ['agent','max_turns','每批最多 Agent 回合','number'],
  ['agent','max_tool_calls','每批最多工具调用（含结果提交）','number'],
  ['agent','batching','会话合批方式','select'],
  ['agent','auto_compaction','启用 Pi 原生上下文压缩','checkbox'],
  ['agent','compaction_reserve_tokens','上下文压缩预留 tokens','number'],
  ['agent','compaction_keep_recent_tokens','压缩后保留最近内容 tokens','number'],
  ['agent','max_retries','Pi 原生失败重试次数（0–5）','number'],
  ['context','max_conversation_chars','单会话最大字符预算','number'],
  ['processing','max_input_chars','初始消息批次字符预算（工具历史由 Pi 管理）','number'],
  ['context','gap_seconds','备用时间分组间隔（秒，SQL 模式不使用）','number'],
  ['context','max_messages','辅助消息条数软限（不切断关联片段）','number'],
  ['context','history_messages','每段附带的历史消息数','number'],
  ['context','timezone','解释时间时使用的时区','text'],
  ['schedule','interval_seconds','独立处理程序轮后等待（秒）','number'],
  ['source_descriptions','wechat','微信本地来源描述','textarea'],
  ['source_descriptions','qq','QQ 本地来源描述','textarea']
];
function processorDetails(title,value){const node=el('details');node.append(el('summary',title),el('pre',JSON.stringify(value,null,2)));return node;}
function processorWindowRequest(){
  const start=$('processor-preview-start').value,end=$('processor-preview-end').value;
  if(!start&&!end)return {};
  if(!start||!end)throw Error('请同时填写开始与结束时间，或恢复默认增量');
  const request={start:Math.floor(new Date(start).getTime()/1000),end:Math.floor(new Date(end).getTime()/1000)};
  if(!Number.isFinite(request.start)||!Number.isFinite(request.end)||request.end<=request.start||request.end-request.start>7*86400)throw Error('结束时间必须晚于开始时间，每次最多调试 7 天');
  return request;
}
function bindProcessorRun(id,handler){const node=$(id);node.addEventListener('click',async()=>{node.disabled=true;await guard(handler);node.disabled=processorBusy||(id==='processor-run'&&!processorConfigured);});}
function processorAgentDetails(value){const counts=value?._agent||value||{},seconds=counts.elapsed_seconds??(counts.duration_ms==null?null:counts.duration_ms/1000),parts=['Pi 工具活动'];if(counts.turns!=null)parts.push(`${counts.turns} 回合`);if(counts.tool_calls!=null)parts.push(`${counts.tool_calls} 次工具调用`);if(counts.compactions!=null)parts.push(`${counts.compactions} 次压缩`);if(counts.retries!=null)parts.push(`${counts.retries} 次重试`);if(counts.max_context_chars!=null)parts.push(`最大请求 ${counts.max_context_chars} 字符`);if(seconds!=null)parts.push(`${Number(seconds).toFixed(1)} 秒`);return processorDetails(parts.join(' · '),value);}
function renderPiEvents(events,status){
  const target=$('processor-live'),labels={run_start:'Pi 会话开始',turn_start:'模型回合',request_prepared:'请求已准备',tool_execution_start:'开始调用工具',tool_execution_end:'工具返回',compaction_start:'原生压缩开始',compaction_end:'原生压缩结束',auto_retry_start:'原生重试开始',auto_retry_end:'原生重试结束',run_end:'Pi 会话结束'},tools={read_attachment:'读取附件',find_related_tasks:'查询关联待办',submit_tasks:'提交待办'};
  target.replaceChildren(el('h3','Pi 运行过程'),el('p',status==='running'?'正在执行 · 自动更新':'本轮已结束 · 原生事件记录','muted'));
  if(!events?.length){target.append(el('p','正在准备输入，尚未收到 Pi 事件。','empty'));return;}
  const list=el('ol');
  for(const e of events){const parts=[e.time?new Date(e.time).toLocaleTimeString():'',labels[e.type]||e.type,e.turn?`第 ${e.turn} 回合`:'',tools[e.tool]||e.tool,e.ref,e.pages?`页 ${e.pages}`:'',e.chars!=null?`${e.chars} 字符`:'',e.context_window?`模型容量 ${e.context_window} tokens`:'',e.failed?'失败':'',...(e.warnings||[]),e.type==='run_end'?`${e.tool_calls} 次工具调用 · ${e.compactions} 次压缩 · ${e.retries} 次重试`:''];list.append(el('li',parts.filter(Boolean).join(' · ')));}
  target.append(list);
}
async function loadProcessorConfig(){
  processorConfig=await api('/processor/config');$('processor-config-file').textContent=processorConfig.file;
  $('processor-config-fields').replaceChildren();
  for(const [section,key,title,type] of processorFields){const label=el('label',title),input=el(type==='textarea'?'textarea':type==='select'?'select':'input');input.id=`proc-${section}-${key}`;if(type==='select'){for(const [value,title] of [['auto','自动合批，保留会话边界'],['single_conversation','每批仅一个会话']]){const option=el('option',title);option.value=value;input.append(option);}}else if(type!=='textarea')input.type=type;else input.rows=3;if(type==='checkbox')input.checked=!!processorConfig.config[section]?.[key];else input.value=processorConfig.config[section]?.[key]??'';label.append(input);$('processor-config-fields').append(label);}
  const path=processorConfig.config.paths.api_key_file;
  $('processor-key-file').textContent=path?'本机密钥文件（仅显示路径）：'+path:'尚未指定本机密钥文件；使用密钥环境变量。';
  const start=processorConfig.config.input?.start_at;
  $('processor-start-at').textContent='首次启用点（只读）：'+(typeof start==='number'?time(start):start||'尚未设定')+'。仅处理启用点之后的消息，不自动回扫历史。';
}
async function openProcessorSource(source){
  const platform=source.collector_module||source.platform||source.message?.platform;
  const params=new URLSearchParams({message_key:source.message_key,revision:String(source.revision)});
  const record=await api(`/modules/${platform}/source?${params}`);
  renderMessages([record],$('source-output'),false,platform);$('source-dialog').showModal();
}
async function loadProcessorRules(){processorRules=await api('/processor/rules');$('processor-rules-file').textContent=processorRules.file;$('processor-rules-text').value=processorRules.text;}
function renderProcessorTasks(tasks,target=$('processor-tasks')){
  target.replaceChildren();
  if(!tasks.length){target.append(el('p','暂无待办。尚未执行处理或没有提取到待办时，这里保持为空。','empty'));return;}
  for(const task of tasks){
    const box=el('article',undefined,'message task'),head=el('div',undefined,'message-head');
    head.append(el('span',task.priority||'待定','pill priority-'+(task.priority||'unknown')),el('span',({cancelled:'已取消',open:'待处理',completed:'已完成',superseded:'已替代'})[task.status]||'状态未知'));
    box.append(head,el('h4',task.title||'未命名待办'));
    const when=task.time||{},people=Array.isArray(task.people)?task.people.map(person=>typeof person==='string'?person:[person.name,person.role].filter(Boolean).join(' · ')).join('、'):'';
    const facts=el('dl',undefined,'task-facts');
    for(const [label,value] of [['事件',task.event],['时间',[when.text,when.start?`开始 ${when.start}`:'',when.deadline?`截止 ${when.deadline}`:'',when.timezone].filter(Boolean).join(' · ')],['参与人员',people],['地点',task.location]])facts.append(el('dt',label),el('dd',value||'未明确'));
    box.append(facts);
    if(task.action_steps?.length){const steps=el('ol');for(const step of task.action_steps)steps.append(el('li',step));box.append(el('h4','你需要做'),steps);}
    if(task.materials?.length)box.append(el('p','执行材料：'+task.materials.map(m=>m.attachment?.name||'未命名附件').join('、'),'muted'));
    const notes=task.notes||task.review_reasons||[];if(notes.length)box.append(el('p','说明：'+notes.join('；'),'muted'));
    const sources=el('details');sources.append(el('summary',`来源证据 · ${(task.sources||[]).length} 条`));
    for(const source of task.sources||[]){const m=source.message||source,platform=m.platform||m.collector_module;const node=el('div',undefined,'source-evidence');node.append(el('p',[names[platform]||platform,...identitySummary(m),m.sent_at?time(m.sent_at):null].filter(Boolean).join(' · '),'muted'),el('div',m.text||source.text||'此来源无可显示正文','message-text'));if(!task.debug_run&&['wechat','qq'].includes(platform)&&source.message_key&&source.revision)node.append(button('打开本地原消息与附件',()=>openProcessorSource(source)));node.append(processorDetails('完整本地来源',source));sources.append(node);}
    if(task.attachment_evidence?.length)box.append(processorDetails('附件证据 · 页码/位置与归档信息',task.attachment_evidence));
    const activity=task.processing?.agent_runtime||task.processing?.agent;
    if(activity)box.append(processorAgentDetails(activity));
    box.append(sources,processorDetails('完整待办与处理依据',task));target.append(box);
  }
}
function renderAttachmentReviews(groups,target){
  target.replaceChildren();
  const labels={read:'已读取',partial:'按需读取了部分内容',skipped:'无需读取',not_requested:'未选择读取',unavailable:'读取失败'};
  for(const group of groups||[]){const items=group.attachment_review||[];if(!items.length)continue;
    const box=el('details');box.append(el('summary',`${names[group.conversation?.platform]||group.conversation?.platform} · ${group.tasks_count} 项待办 · ${items.filter(x=>x.status==='unavailable').length} 个附件读取失败`));
    box.append(el('p','结论基于消息与实际读取的内容；未选择的附件不等于已核实。','muted'));
    for(const item of items)box.append(el('p',[item.ref,item.name,labels[item.status]||item.status,item.reason,...(item.warnings||[])].filter(Boolean).join(' · '),'asset'));
    if(group.submission_file)box.append(el('p','原始模型提交（仅本地）：'+group.submission_file,'muted path'));
    target.append(box);
  }
}
async function refreshProcessor(){
  const [state,result,reviews]=await Promise.all([api('/processor/status'),api('/processor/tasks?limit=20'),api('/processor/reviews?limit=20')]);
  renderAttachmentReviews(reviews.groups,$('processor-review-output'));
  const configured=state.model.configured;processorConfigured=configured;processorBusy=!!state.running_job;
  if(state.running_job&&!processorJob)processorJob=state.running_job;
  $('processor-state').textContent=processorBusy?'处理任务执行中':configured?'模型已配置':'模型未配置';
  $('processor-state').className='pill'+(configured?'':' warn');$('processor-run').disabled=!configured||processorBusy;$('processor-preview').disabled=processorBusy;
  $('processor-run').textContent=($('processor-preview-start').value||$('processor-preview-end').value)?'处理所选时间（调试）…':'执行一轮处理…';
  for(const id of ['processor-preview-start','processor-preview-end','processor-preview-reset'])$(id).disabled=processorBusy;
  $('processor-metrics').replaceChildren(metric('已入库消息',state.counts.messages??0),metric('待处理分组',state.counts.pending_groups??'点击预览',state.counts.pending_groups==null),metric('待办数量',state.counts.tasks??0),metric('模型',state.model.model||'尚未配置',true));
  $('processor-engine').textContent=`引擎：${state.model.engine||'待确认'} · 图片读取：${state.model.vision?'已启用':'未启用'} · 附件由 Agent 按需读取`;
  $('processor-warning').textContent=(state.warnings||[]).join('；')||(!configured?'先填写模型连接信息和本机凭据；当前仅可预览，不会生成 AI 待办。':'连接参数已填写；实际可用性在手动执行时验证。');renderProcessorTasks(result.tasks);
}
function renderProcessorPreview(result){
  const batches=Array.isArray(result.batch_plan)?result.batch_plan:null,items=batches||result.groups||[],target=$('processor-preview-output');target.replaceChildren(el('p',`${result.debug_window?'调试范围内':'待处理'} ${result.pending_groups} 组，本次展示 ${items.length} ${batches?'批':'组会话预览'}。按会话保留边界；自动模式可以合批多个会话，超限时优先按会话拆分。`,'muted'));
  if(result.warnings?.length)target.append(el('p',result.warnings.join('；'),'scope'));
  if(result.debug_window)target.append(el('p',`实际预览时间：${new Date(result.debug_window.start*1000).toLocaleString()}（含）— ${new Date(result.debug_window.end*1000).toLocaleString()}（不含）`,'muted'));
  for(const [index,group] of items.entries()){
    const box=el('article',undefined,'message'),description=group.platform?(result.source_descriptions?.[group.platform]||group.platform+' local collector'):'会话边界见初始模型输入；完整来源保留在本地。';box.append(el('h4',batches?`处理批次 ${index+1} · ${group.conversations} 个会话 · ${group.messages} 条消息`:group.conversation_id?`${names[group.platform]||group.platform} · ${group.conversation_id}`:`会话预览 ${index+1}`),el('p',description,'muted'));
    const input=group.actual_model_input||group.model_input;
    if(input){const shown=processorDetails(batches?'本批实际初始模型输入 · 短引用':'单会话输入预览 · 未合批',input);shown.open=true;box.append(shown);
      const attachmentStates={available:'可读取',resolvable:'调用工具时自动准备',ready:'可读取',missing:'本地缺失',blocked:'不允许读取',unsupported:'格式暂不支持',error:'读取异常',pending:'待读取'};
      for(const attachment of input.attachments||[]){const state=attachment.status||attachment.availability;box.append(el('p',[attachment.ref||attachment.id,attachment.name,attachmentStates[state]||state||'状态待确认',attachment.type==='image'?(attachment.vision_enabled?'可供模型看图':'模型图片能力未启用'):'',attachment.reason,...(attachment.warnings||[])].filter(Boolean).join(' · '),'asset'));}
    }else box.append(el('p','处理核心尚未提供实际模型输入；下方只展示本地来源，不代表发送内容。','scope'));
    box.append(processorDetails('完整本地来源 · 不随初始输入发送',{collector_description:description,...(group.source_map?{source_map:group.source_map}:{messages:group.messages})}));target.append(box);
  }
  if(!items.length)target.append(el('p','当前没有待处理的上下文。','empty'));
  if(result.rules?.text)target.append(processorDetails('本轮处理规则（预览时的版本）',result.rules.text));
}
async function pollProcessor(){
  if(!processorJob)return;const job=await api('/jobs/'+processorJob);if(job.action==='process')renderPiEvents(job.pi_events,job.status);if(job.status==='running')return;processorJob=null;
  if(job.status==='succeeded'){
    if(job.action==='processor_preview')renderProcessorPreview(job.result);
    else{notice(job.result?.status==='partial'?'部分分组处理失败；已成功结果已保留，请查看任务详情。':'本轮处理已结束，可查看待办和任务详情。',job.result?.status==='partial');const audit=job.result?.audit||job.result?.agent_runtime;$('processor-audit').replaceChildren(el('p',`本轮处理耗时 ${Math.max(0,(job.finished_at||Date.now()/1000)-job.started_at).toFixed(1)} 秒（含工作进程启动）`,'muted'));if(audit)$('processor-audit').append(processorAgentDetails(audit));}
  }else{notice(job.error||'处理失败，请查看任务详情',true);if(job.action==='processor_preview')$('processor-preview-output').replaceChildren(el('p',job.error||'预览失败','empty'));}
  await refreshProcessor();
  if(job.action==='process'&&job.result?.debug_window){const out=$('processor-debug-output');out.replaceChildren(el('h3','本次调试结果 · 独立保存'),el('p',`范围：${time(job.result.debug_window.start)} — ${time(job.result.debug_window.end)}；剩余 ${job.result.pending_groups} 组。默认配置和正式处理状态未改变。`,'scope'),el('p',job.result.debug_output_dir,'muted path'));const tasks=el('div');out.append(tasks);renderProcessorTasks(job.result.tasks||[],tasks);const review=el('div');out.append(review);renderAttachmentReviews(job.result.group_results,review);}
}
async function initProcessor(){
  bind('processor-reload',refreshProcessor);
  bind('close-source',()=>$('source-dialog').close());
  bind('processor-load-config',async()=>{if(confirm('重新加载会丢弃尚未保存的配置编辑，继续？'))await loadProcessorConfig();});
  bind('processor-load-rules',async()=>{if(confirm('重新加载会丢弃尚未保存的规则编辑，继续？'))await loadProcessorRules();});
  bind('processor-save-config',async()=>{
    if(!processorConfig)throw Error('请先加载处理配置');const patch={};
    for(const [section,key,,type] of processorFields){const input=$(`proc-${section}-${key}`),value=type==='checkbox'?input.checked:type==='number'?Number(input.value):input.value.trim();if(value!==processorConfig.config[section]?.[key])(patch[section]??={})[key]=value;}
    processorConfig=await api('/processor/config','PATCH',{revision:processorConfig.revision,patch});notice('处理配置已保存并备份。');await refreshProcessor();
  });
  bind('processor-save-rules',async()=>{if(!processorRules)throw Error('请先加载规则');processorRules=await api('/processor/rules','PUT',{revision:processorRules.revision,text:$('processor-rules-text').value});notice('规则已保存并备份；下一轮处理使用新版本。');await refreshProcessor();});
  const updatePreviewWindow=async()=>{const custom=!!($('processor-preview-start').value||$('processor-preview-end').value);$('processor-preview-scope').textContent=custom?'手动调试（最多 7 天，浏览器本地时间）：预览与处理均使用所选时间和当前 SQL。处理会调用模型，结果独立保存，不改变默认值或正式断点。':'默认增量：起止时间留空，沿用已保存的处理起点。手动时间不会保存为默认值。';$('processor-preview-output').replaceChildren(el('p','范围已更新，请点击预览。','empty'));await refreshProcessor();};
  for(const id of ['processor-preview-start','processor-preview-end'])$(id).addEventListener('change',()=>updatePreviewWindow().catch(error=>notice(error.message,true)));
  bind('processor-preview-reset',async()=>{$('processor-preview-start').value='';$('processor-preview-end').value='';await updatePreviewWindow();});
  bindProcessorRun('processor-preview',async()=>{const job=await api('/processor/preview','POST',{limit:5,...processorWindowRequest()});processorJob=job.id;$('processor-preview-output').replaceChildren(el('p','正在本机整理上下文，可在任务记录中查看进度…','empty'));await refreshProcessor();});
  bindProcessorRun('processor-run',async()=>{const request=processorWindowRequest(),scope=request.start!=null?`调试 ${time(request.start)} 至 ${time(request.end)}，结果独立保存，不改变正式断点。`:'执行默认增量处理。';if(!confirm(scope+'会发送聊天片段与规则，并由 Pi 按需读取附件文字或图片发送给已配置的模型服务。继续？'))return;const job=await api('/processor/run','POST',request);processorJob=job.id;notice('处理任务已启动；关闭页面不会中断任务。');await refreshProcessor();});
  try{await Promise.all([loadProcessorConfig(),loadProcessorRules(),refreshProcessor()]);}catch(error){$('processor-state').textContent='处理模块未就绪';$('processor-warning').textContent=error.message;}
}
