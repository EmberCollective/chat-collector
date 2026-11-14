'use strict';
let workflowRun='',workflowCursor=0,workflowMore=false,workflowPending=false,workflowBusy=false,workflowGeneration=0,workflowJob=null,workflowRecorded=false,workflowRuns=[],workflowRunCursor=0,workflowRunsMore=false;
const workflowEvents=new Map();
const workflowLabels={runtime_failed:'Pi 执行失败',record_unavailable:'此条记录不可读取',run_created:'批次记录开始',input_prepared:'初始上下文与规则',turn_start:'模型回合开始',request_prepared:'实际模型请求',model_output:'模型可见输出',tool_execution_start:'调用工具',tool_execution_end:'工具返回',compaction_start:'上下文压缩开始',compaction_end:'上下文压缩结束',auto_retry_start:'失败重试开始',auto_retry_end:'失败重试结束',submission_accepted:'结果通过提交校验',run_end:'Pi 批次结束',result_received:'工作进程收到结果',transport_end:'模型通信结束'};
function workflowCategory(type){return type.startsWith('tool_')?'tool':['input_prepared','request_prepared'].includes(type)?'context':type==='model_output'?'model':'system';}
function filterWorkflow(){
  const query=$('workflow-search').value.toLowerCase(),category=$('workflow-filter').value;let visible=0;
  for(const row of $('workflow-feed').children){const event=workflowEvents.get(row.dataset.key);row.hidden=(!JSON.stringify(event).toLowerCase().includes(query))||(category&&workflowCategory(event.type)!==category);if(!row.hidden)visible++;}
  $('workflow-feed').dataset.visible=String(visible);
  $('workflow-matches').textContent=workflowEvents.size?`已读取 ${workflowEvents.size} 条 · 匹配 ${visible} 条`:'尚无可显示事件';
}
function workflowEvent(event){
  const row=el('details',undefined,'workflow-event'),summary=el('summary'),text=el('div'),body=el('div',undefined,'workflow-event-body');
  row.dataset.key=String(event.offset);summary.append(el('span',String(event.seq??workflowEvents.size)));
  text.append(el('strong',(workflowLabels[event.type]||event.type)+(event.tool?' · '+event.tool:'')),el('p',[event.turn?'第 '+event.turn+' 回合':'',event.run_id?'批次 '+event.run_id.slice(0,8):'',event.call_id?'调用 '+event.call_id:'',event.failed?'失败':''].filter(Boolean).join(' · ')));summary.append(text);
  if(event.time){const at=new Date(event.time);if(!Number.isNaN(at.getTime()))summary.append(el('time',at.toLocaleTimeString('zh-CN',{hour12:false})));}
  row.append(summary,body);
  row.addEventListener('toggle',()=>{
    if(!row.open||body.childElementCount)return;
    const value=event.body;
    if(value==null){body.append(el('p','此事件没有记录正文；不能据此还原上下文。','scope'));return;}
    if(event.type==='model_output')for(const block of value.content||[])if(block.type==='text'&&block.text)body.append(el('p',block.text,'message-text'));
    for(const [key,label]of Object.entries({arguments:'工具参数',result:'工具返回',messages:'实际请求消息',tools:'当轮可用工具',summary:'压缩摘要',system:'处理规则',payload:'初始消息与短引用'})){
      if(value[key]===undefined)continue;body.append(el('h4',label),el('pre',typeof value[key]==='string'?value[key]:JSON.stringify(value[key],null,2)));
    }
    body.append(processorDetails('完整安全记录 · 含省略说明',event));
    if(body.childElementCount===1){const detail=body.querySelector('details');detail.open=true;}
  });return row;
}
function updateWorkflowStatus(omissions=[]){
  const job=workflowJob;let status=job?`${time(job.started_at)} · ${states[job.status]||job.status} · ${job.id.slice(0,8)}`:'尚未选择处理运行';
  if(job&&!workflowRecorded)status+=job.status==='running'?' · 正在准备详细记录':' · 此运行未记录详细正文；旧的轻量进度无法还原完整工作流';
  else if(job)status+=' · 记录经过敏感字段投影；图片编码、隐藏推理及超限正文会注明省略';
  if(job?.status==='succeeded')status+=' · 处理任务结束，保存结果请核对待办与任务详情';
  if(omissions.length)status+=' · '+omissions.map(item=>({credentials:'凭据字段',image_bodies:'图片编码',private_reasoning:'隐藏推理',oversized_bodies:'超限正文'})[item]||item).join('、');
  if(workflowPending)status+=job?.status==='running'?' · 末条记录仍在写入':' · 末条记录不完整，无法还原缺失正文';
  if(workflowMore)status+=' · 还有后续记录，可继续读取后再搜索';
  if(job?.error)status+=' · '+job.error;
  $('workflow-status').textContent=status;$('workflow-more').hidden=!workflowMore;
}
async function loadWorkflowPage(){
  if(!workflowRun||workflowBusy)return;workflowBusy=true;
  const run=workflowRun,generation=workflowGeneration;
  try{
    const result=await api('/jobs/'+encodeURIComponent(run)+'/workflow?'+new URLSearchParams({cursor:String(workflowCursor),limit:'50'}));
    if(run!==workflowRun||generation!==workflowGeneration)return;
    workflowCursor=result.next_cursor;workflowMore=result.has_more;workflowPending=result.pending;workflowJob=result.job;workflowRecorded=result.recorded;
    const fragment=document.createDocumentFragment();for(const event of result.events){const key=String(event.offset);if(workflowEvents.has(key))continue;workflowEvents.set(key,event);fragment.append(workflowEvent(event));}$('workflow-feed').append(fragment);
    updateWorkflowStatus(result.omissions);filterWorkflow();
  }finally{workflowBusy=false;}
}
async function loadWorkflowRuns(append=false){
  const result=await api('/workflow/runs?'+new URLSearchParams({cursor:String(append?workflowRunCursor:0),limit:'30'}));
  workflowRunCursor=result.next_cursor;workflowRunsMore=result.has_more;
  const existing=append?workflowRuns:[];workflowRuns=[...new Map([...existing,...result.runs].map(run=>[run.id,run])).values()];
  const picker=$('workflow-run');picker.replaceChildren();
  if(workflowRun&&!workflowRuns.some(run=>run.id===workflowRun)){const option=el('option','选中运行 · '+workflowRun.slice(0,8));option.value=workflowRun;picker.append(option);}
  for(const run of workflowRuns){const option=el('option',`${time(run.started_at)} · ${states[run.status]||run.status}${run.recorded?'':' · 无详细正文'}`);option.value=run.id;picker.append(option);}
  if(workflowRun)picker.value=workflowRun;
  if(!picker.children.length){const option=el('option','尚无处理运行');option.value='';picker.append(option);}
  $('workflow-older').hidden=!workflowRunsMore;
}
async function selectWorkflow(id,navigate=true){
  workflowRun=id;const generation=++workflowGeneration;workflowCursor=0;workflowMore=false;workflowPending=false;workflowJob=null;workflowRecorded=false;workflowEvents.clear();$('workflow-feed').replaceChildren();
  if(navigate){location.hash='processor';activatePanel('pi-workflow');}
  await loadWorkflowRuns();if(generation!==workflowGeneration||id!==workflowRun)return;$('workflow-run').value=id;
  // A previous request may still be finishing; the next polling tick resumes this selection.
  await loadWorkflowPage();
}
async function refreshWorkflow(){
  if(!token)return;
  await loadWorkflowRuns();if(!workflowRun&&workflowRuns.length)await selectWorkflow(workflowRuns[0].id,false);
  else if(workflowRun&&!workflowMore)await loadWorkflowPage();
}
async function initWorkflow(){
  const matches=el('p','', 'scope');matches.id='workflow-matches';$('workflow-status').after(matches);
  const older=button('读取更早的运行',()=>loadWorkflowRuns(true));older.id='workflow-older';older.hidden=true;$('workflow-run').parentElement.append(older);
  $('workflow-run').addEventListener('change',()=>guard(()=>selectWorkflow($('workflow-run').value,false)));
  bind('workflow-refresh',refreshWorkflow);bind('workflow-more',loadWorkflowPage);
  $('workflow-search').addEventListener('input',filterWorkflow);$('workflow-filter').addEventListener('change',filterWorkflow);
  await refreshWorkflow();
  setInterval(()=>{if(!$('view-processor').hidden&&!$('pi-workflow').hidden&&!workflowMore)guard(loadWorkflowPage);},3000);
}
