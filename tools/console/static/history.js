'use strict';
let historyReady=false,historyCursor=0,historyGeneration=0,historyQuery='',historyRows=[],historyLoading=false;
const originLabels={manual:'手动',schedule:'定时',cli:'命令行',processor:'后台 / 未注明',automation:'后台服务'};
function historyParams(){
  const params=new URLSearchParams({limit:'30',sort:$('history-sort').value});
  for(const [id,key]of Object.entries({'history-filter':'status','history-module':'module','history-action':'action','history-origin':'origin','history-min-duration':'min_duration_seconds','history-max-duration':'max_duration_seconds','history-error':'error'}))if($(id).value)params.set(key,$(id).value);
  for(const [id,key]of [['history-start','start'],['history-end','end']])if($(id).value){const seconds=new Date($(id).value).getTime()/1000;if(!Number.isFinite(seconds))throw Error('请填写有效的起止时间');params.set(key,String(seconds));}
  const min=params.get('min_duration_seconds'),max=params.get('max_duration_seconds');
  if(min!==null&&max!==null&&Number(min)>Number(max))throw Error('最短耗时不能大于最长耗时');
  if(params.has('start')&&params.has('end')&&Number(params.get('start'))>=Number(params.get('end')))throw Error('结束时间必须晚于开始时间');
  return params;
}
function durationText(value,complete=true){
  if(value==null)return '未记录';const seconds=Math.max(0,value),text=seconds>=60?`${Math.floor(seconds/60)} 分 ${(seconds%60).toFixed(1)} 秒`:`${seconds.toFixed(1)} 秒`;
  return (complete?'':'已耗时 ')+text;
}
function renderHistoryRows(){
  $('jobs').replaceChildren();
  for(const job of historyRows){
    const row=el('tr');row.append(el('td',time(job.started_at)),el('td',names[job.module]||job.module||'未注明'),el('td',actions[job.action]||job.action||'未注明'),el('td',originLabels[job.origin]||job.origin||'未注明'),el('td',states[job.status]||job.status||'未注明'));
    const duration=el('td',durationText(job.duration_seconds,job.duration_complete));if(job.status==='running'&&job.started_at)duration.dataset.runningStart=String(job.started_at);row.append(duration);
    row.append(el('td',job.error||'—','history-error-cell'));const controls=el('td'),group=el('div',undefined,'row');group.append(button('详情',()=>showJob(job.id)));if(job.action==='process')group.append(button('Pi 工作流',()=>selectWorkflow(job.id)));controls.append(group);row.append(controls);$('jobs').append(row);
  }
  if(!historyRows.length){const row=el('tr'),cell=el('td','此范围内没有运行记录。调整筛选条件或等待首次执行。','empty');cell.colSpan=8;row.append(cell);$('jobs').append(row);}
}
function historyFacets(facets){
  for(const [key,labels]of [['module',names],['action',actions],['origin',originLabels]]){
    const picker=$('history-'+key),value=picker.value,values=facets[key+'s']||[],existing=[...picker.options].slice(1).map(option=>option.value);
    if(JSON.stringify(values)===JSON.stringify(existing))continue;
    picker.replaceChildren(el('option',({module:'全部模块',action:'全部操作',origin:'全部触发方式'})[key]));picker.firstChild.value='';
    for(const value of values){const option=el('option',labels[value]||value);option.value=value;picker.append(option);}
    if(value&&!values.includes(value)){const option=el('option',labels[value]||value);option.value=value;picker.append(option);}picker.value=value;
  }
}
async function refreshHistory(append=false){
  if(!historyReady)return;
  const params=historyParams(),query=params.toString();
  if(append&&historyLoading)return;
  if(append&&query!==historyQuery)throw Error('筛选条件已变化，请先应用筛选');
  const generation=++historyGeneration;historyLoading=true;params.set('cursor',String(append?historyCursor:0));$('history-summary').textContent='正在读取记录…';
  try{
    const result=await api('/jobs?'+params);if(generation!==historyGeneration)return;
    historyQuery=query;historyCursor=result.next_cursor;historyRows=append?[...historyRows,...result.jobs]:result.jobs;historyFacets(result.facets);renderHistoryRows();
    $('history-summary').textContent=`已显示 ${historyRows.length} / ${result.total} 条 · 耗时包含工作进程启动及本轮操作；执行中显示已耗时，缺少终点不推算总耗时 · 浏览器本地时间`;
    $('history-more').hidden=!result.has_more;
  }finally{if(generation===historyGeneration)historyLoading=false;}
}
async function initHistory(){
  historyReady=true;$('history-form').addEventListener('submit',event=>{event.preventDefault();guard(()=>refreshHistory());});
  bind('history-more',()=>refreshHistory(true));bind('history-reset',async()=>{$('history-form').reset();await refreshHistory();});
  $('history-form').addEventListener('input',()=>{$('history-summary').textContent='筛选条件已修改，点击“应用筛选”更新结果。';});
  window.addEventListener('hashchange',()=>{if(!$('view-history').hidden)guard(()=>refreshHistory());});
  if(!$('view-history').hidden)await refreshHistory();
  setInterval(()=>{for(const cell of document.querySelectorAll('[data-running-start]'))cell.textContent=durationText(Date.now()/1000-Number(cell.dataset.runningStart),false);},1000);
}
