'use strict';
(() => {
  const $=id=>document.getElementById(id);
  const node=(tag,text,cls)=>{const el=document.createElement(tag);if(text!=null)el.textContent=text;if(cls)el.className=cls;return el;};
  const valid=value=>typeof value==='number'&&Number.isFinite(value);
  const percent=value=>valid(value)?Number(value.toFixed(1))+'%':'未取得';
  const speed=value=>valid(value)?value.toFixed(1)+' MB/s':'未取得';
  const clock=at=>new Date(at*1000).toLocaleTimeString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false});
  const visible=()=>!document.hidden&&!$('view-overview').hidden;
  let ready=false,loading=false,timer=null,data=null,overview=null,readError='',taskReadError='',taskWarnings=[],tasks=[],selectedSample=null,selectedAt=null,samples=[],plotRight=0,plotBottom=152,plotStart=0,plotEnd=0;
  const performanceActions=new Set(['collect','process']);
  const disks=()=>data?.disks||[];
  const disk=()=>disks().find(item=>item.id===$('monitor-disk').value);
  const latest=()=>data?.samples?.at(-1);
  const stale=()=>!!readError||!latest()||Date.now()/1000-latest().at>15;
  const ioAt=(sample=latest())=>sample?.disks?.find(item=>item.id===disk()?.id);
  const processes=(sample=latest())=>sample?.processes||[];
  function sourceName(module){
    return (overview?.sources||[]).find(item=>item.id===module)?.name||(data?.modules||[]).find(item=>item.id===module)?.name||names[module]||module||'采集来源';
  }
  function taskLabel(task){return task.action==='process'?'待办处理 · AI 处理':sourceName(task.module)+' · 正式采集';}
  function normalizeTasks(records){
    return (Array.isArray(records)?records:[]).flatMap(record=>{
      if(!performanceActions.has(record?.action)||!valid(record?.started_at))return [];
      const open=record.status==='running'&&record.finished_at==null;
      const end=valid(record.finished_at)?record.finished_at:null;
      if(!open&&(end===null||end<record.started_at))return [];
      return [{id:String(record.id||record.started_at),module:String(record.module||''),action:record.action,status:record.status,origin:record.origin,started_at:record.started_at,finished_at:end,running:open}];
    });
  }
  function tasksAt(at){return tasks.filter(task=>task.started_at<=at&&(task.running||at<task.finished_at));}
  function taskColor(task){return task.action==='process'?'#465862':'#12634f';}
  function renderTaskHealth(){
    const health=$('monitor-task-health');if(!health)return;
    health.hidden=!taskReadError&&!taskWarnings.length;
    health.textContent=taskReadError?'性能任务记录暂不可读':taskWarnings.length?'性能任务记录不完整':'';
    const title=taskReadError||taskWarnings.join('；');if(title)health.title=title;else health.removeAttribute('title');
  }
  function programCpu(sample){
    const active=processes(sample).filter(item=>item.status!=='stopped');
    return active.length&&active.every(item=>valid(item.cpu))?active.reduce((sum,item)=>sum+item.cpu,0):null;
  }
  function options(select,rows){const old=select.value;select.replaceChildren(...rows.map(row=>{const option=node('option',row.name);option.value=row.id;return option;}));if(rows.some(row=>row.id===old))select.value=old;}
  function taskCaption(){
    const sample=latest(),probed=sample&&Object.hasOwn(sample,'active_jobs');
    if(probed&&(stale()||sample.active_jobs===null))return '当前任务状态未确认';
    const running=probed?sample.active_jobs:(overview?.jobs||[]).filter(job=>job.state==='running').map(job=>({...job,started_at:job.last_start}));
    if(!running.length)return (probed?processes(sample).some(group=>group.id==='shared'&&group.status==='stopped'):overview?.service?.verified&&overview.service.running===false)?'业务后台已停止 · 整机负载仍单独监测':'暂无正在执行的任务';
    return running.map(job=>{const name=job.name||(overview?.sources||[]).find(source=>source.id===job.source_id)?.name||names[job.module]||job.module;return name+(job.source_id?' 正式采集':'')+'执行中'+(valid(job.started_at)?' · 已运行 '+Math.max(0,Math.floor(Date.now()/1000-job.started_at))+' 秒':'');}).join('；');
  }
  window.setOverviewSnapshot=value=>{overview=value;if(ready)$('monitor-current').textContent=taskCaption();};
  window.updateRuntimeElapsed=()=>{if(ready&&visible())$('monitor-current').textContent=taskCaption();};
  function metric(label,value,note){const box=node('div',null,'monitor-metric');box.append(node('span',label),node('strong',value),node('small',note));return box;}
  function renderMetrics(){
    const sample=latest(),io=ioAt(),unknown=stale(),total=programCpu(sample),kind=disk()?.kind;
    const cpuBox=metric('整机 CPU',!unknown&&valid(sample?.cpu)?percent(sample.cpu):'—',unknown?'当前值未知；保留最后有效采样':valid(total)?'本程序合计 '+percent(total)+' · 按整机总算力':'本程序占用尚未取得');
    const diskBox=metric(kind==='hdd'?'所选机械硬盘繁忙度':'所选物理磁盘繁忙度',!unknown&&valid(io?.busy)?percent(io.busy):'—',unknown?'当前值未知，不按 0% 处理':valid(io?.busy)?'整块物理盘 · 包含其他程序':'未取得该物理盘的性能计数');
    const speeds=node('div',null,'monitor-metric'),values=node('div',null,'monitor-io-values');speeds.append(node('span','所选硬盘读写速度'));
    for(const [label,key]of [['读','read_mbps'],['写','write_mbps']]){const part=node('div');part.append(node('small',label+' '),node('strong',!unknown&&valid(io?.[key])?io[key].toFixed(1):'—'),node('small',' MB/s'));values.append(part);}speeds.append(values,node('small','物理盘吞吐 · 与进程 I/O 分开统计'));
    $('monitor-metrics').replaceChildren(cpuBox,diskBox,speeds);
    $('monitor-current').textContent=taskCaption();$('monitor-asof').textContent='最近 60 秒 · 每 5 秒采样 · 悬停查看性能任务';renderTaskHealth();
    const health=$('monitor-health');health.hidden=false;
    if(readError)health.textContent='采样读取失败 · '+readError+(sample?'；保留 '+clock(sample.at)+' 的历史采样':'；当前负载未知');
    else if(data?.status==='warming'||!sample)health.textContent='正在建立采样基线 · 等待下次采样，暂不按 0 处理';
    else if(unknown)health.textContent='采样已中断 · 最后取得 '+clock(sample.at)+'，当前值未知';
    else if(data?.status==='unavailable')health.textContent='无法取得性能数据 · 当前负载未知';
    else if(!disks().length)health.textContent='未取得可监测的物理磁盘 · CPU 仍可单独查看';
    else if(!valid(io?.busy))health.textContent='所选磁盘性能计数暂不可用 · CPU 仍可单独查看';
    else health.hidden=true;
    const reason=data?.error?({disk_counters_unavailable:'物理磁盘性能计数暂不可用',process_probe_partial:'部分进程无法读取',probe_unavailable:'性能探针暂不可用'}[data.error]||'部分性能计数暂不可用'):'控制台运行期间持续采样；关闭浏览器不影响记录';
    $('monitor-unavailable-reason').textContent=reason+(valid(data?.sample_duration_ms)?'；最近一次采样耗时 '+data.sample_duration_ms.toFixed(1)+' 毫秒':'')+'。首次、恢复和进程变更需重新建立基线，未测得的用量不补 0。';
  }
  function svgNode(tag,attrs,text){const el=document.createElementNS('http://www.w3.org/2000/svg',tag);for(const [key,value]of Object.entries(attrs))el.setAttribute(key,String(value));if(text!=null)el.textContent=text;return el;}
  function renderChart(){
    samples=(data?.samples||[]).slice(-13);plotEnd=samples.at(-1)?.at||data?.generated_at||Date.now()/1000;plotStart=plotEnd-60;
    const width=Math.max(240,$('monitor-chart').clientWidth),height=180,left=40,right=width-12,top=12,bottom=height-28;plotRight=right;plotBottom=bottom;
    const svg=svgNode('svg',{viewBox:`0 0 ${width} ${height}`,'aria-hidden':'true'}),x=sample=>left+(right-left)*(sample.at-plotStart)/60;
    for(const task of tasks){
      const end=task.running?plotEnd:task.finished_at;if(!valid(end))continue;
      const startAt=Math.max(plotStart,task.started_at),endAt=Math.min(plotEnd,end);
      if(endAt<=startAt)continue;
      svg.append(svgNode('rect',{x:left+(right-left)*(startAt-plotStart)/60,y:top,width:Math.max(1,(right-left)*(endAt-startAt)/60),height:bottom-top,fill:taskColor(task),'fill-opacity':'.07','data-task-band':task.action}));
    }
    for(const value of [0,25,50,75,100]){const y=bottom-(bottom-top)*value/100;svg.append(svgNode('line',{x1:left,x2:right,y1:y,y2:y,stroke:'#e0e6e2'}),svgNode('text',{x:0,y:y+4},value+'%'));}
    for(const [part,label]of [[0,'60 秒前'],[0.5,'30 秒前'],[1,'最近采样']])svg.append(svgNode('text',{x:left+(right-left)*part,y:height-4,'text-anchor':part===0?'start':part===1?'end':'middle'},label));
    for(const [key,color]of [['cpu','#12634f'],['disk','#865b16']]){let points=[],previous=null;const flush=()=>{if(points.length)svg.append(svgNode('polyline',{points:points.join(' '),fill:'none',stroke:color,'stroke-width':2,'vector-effect':'non-scaling-stroke'}));points=[];};for(const sample of samples){const value=key==='cpu'?sample.cpu:ioAt(sample)?.busy;if(!valid(value)||sample.at<plotStart){flush();previous=null;continue;}if(previous!=null&&sample.at-previous>7.5)flush();points.push(`${x(sample)},${bottom-(bottom-top)*value/100}`);previous=sample.at;}flush();}
    const marker=svgNode('g',{id:'monitor-marker',visibility:'hidden'});marker.append(svgNode('line',{id:'monitor-crosshair',x1:right,x2:right,y1:top,y2:bottom,stroke:'#7b8981','stroke-dasharray':'3 4'}));
    for(const [key,color]of [['cpu','#12634f'],['disk','#865b16']])marker.append(svgNode('circle',{id:'monitor-point-'+key,cx:right,cy:bottom,r:3,fill:color,stroke:'white','stroke-width':1.5}));svg.append(marker);
    const tooltip=node('div',null,'monitor-tooltip');tooltip.id='monitor-tooltip';tooltip.role='tooltip';tooltip.hidden=true;$('monitor-chart').replaceChildren(svg,tooltip);hidePoint();
  }
  function hidePoint(){selectedSample=null;selectedAt=null;$('monitor-marker')?.setAttribute('visibility','hidden');for(const key of ['cpu','disk'])$('monitor-point-'+key)?.setAttribute('visibility','hidden');if($('monitor-tooltip'))$('monitor-tooltip').hidden=true;delete $('monitor-chart').dataset.sampleIndex;delete $('monitor-chart').dataset.sampleAt;}
  function appendTaskDetails(tooltip,at){
    const section=node('div',null,'monitor-tooltip-tasks');
    if(taskReadError){section.append(node('p','性能任务记录暂不可读，不能据此判断当前没有任务执行。','monitor-tooltip-warning'));tooltip.append(section);return;}
    const active=tasksAt(at),head=node('div',null,'monitor-tooltip-tasks-head');
    head.append(node('strong',active.length?'该时刻 '+active.length+' 个性能任务执行中':'该时刻没有性能任务执行'));
    if(active.length>1)head.append(node('span','并发','monitor-tooltip-parallel'));
    section.append(head);
    const shown=active.length>3||taskWarnings.length?2:3;
    for(const task of active.slice(0,shown)){
      const row=node('div',null,'monitor-tooltip-task'+(task.action==='process'?' ai':'')),title=node('strong',taskLabel(task)),meta=node('small');
      if(task.running){
        const elapsed=Math.max(0,Math.floor(at-task.started_at));meta.textContent=clock(task.started_at)+' → 进行中 · '+elapsed+' 秒';row.title=taskLabel(task)+'：开始 '+clock(task.started_at)+'，当前已运行 '+elapsed+' 秒';
      }else{
        const seconds=Math.max(0,task.finished_at-task.started_at).toFixed(1),outcome={succeeded:'成功',warning:'需检查',failed:'失败',interrupted:'中断'}[task.status]||'';meta.textContent=clock(task.started_at)+' → '+clock(task.finished_at)+' · '+seconds+' 秒';row.title=taskLabel(task)+'：开始 '+clock(task.started_at)+'，结束 '+clock(task.finished_at)+'，耗时 '+seconds+' 秒'+(outcome?'，'+outcome:'');
      }
      row.append(node('i'),title,meta);row.dataset.taskId=task.id;section.append(row);
    }
    if(active.length>shown)section.append(node('p','另有 '+(active.length-shown)+' 个性能任务；可在运行记录中查看。','monitor-tooltip-empty'));
    if(!active.length)section.append(node('p','该时刻没有正式采集或 AI 处理任务执行；负载可能来自其他程序或轻量任务。','monitor-tooltip-empty'));
    if(taskWarnings.length)section.append(node('p','部分后台任务记录暂不可读，当前列表可能不完整。','monitor-tooltip-warning'));
    tooltip.append(section);
  }
  function showAt(at,announce=false){
    if(!samples.length)return;
    at=Math.max(plotStart,Math.min(plotEnd,at));
    let nearest=0;for(let i=1;i<samples.length;i++)if(Math.abs(samples[i].at-at)<Math.abs(samples[nearest].at-at))nearest=i;
    const sample=samples[nearest],tooltip=$('monitor-tooltip'),changed=selectedAt===null||Math.abs(at-selectedAt)>=.05;
    selectedAt=at;selectedSample=nearest;$('monitor-chart').dataset.sampleIndex=String(nearest);$('monitor-chart').dataset.sampleAt=String(at);
    const io=ioAt(sample),x=40+(plotRight-40)*(at-plotStart)/60;
    if(changed||tooltip.hidden){
      const time=node('time',clock(at));time.dateTime=new Date(at*1000).toISOString();time.title='负载数值取最近采样 '+clock(sample.at);tooltip.replaceChildren(time);
      for(const [label,value]of [['整机 CPU',sample.cpu],['硬盘繁忙度',io?.busy]]){const row=node('div');row.append(node('span',label),node('strong',percent(value)));tooltip.append(row);}
      const speeds=node('div',null,'monitor-tooltip-io');for(const [label,key]of [['读','read_mbps'],['写','write_mbps']])speeds.append(node('span',label+' '+speed(io?.[key])));tooltip.append(speeds);
      appendTaskDetails(tooltip,at);
    }
    tooltip.hidden=false;const tooltipWidth=tooltip.offsetWidth,space=$('monitor-chart').clientWidth;tooltip.style.left=Math.max(8,Math.min(space-tooltipWidth-8,x>space*.6?x-tooltipWidth-12:x+12))+'px';
    const line=$('monitor-crosshair');line.setAttribute('x1',x);line.setAttribute('x2',x);$('monitor-marker').setAttribute('visibility','visible');for(const [key,value]of [['cpu',sample.cpu],['disk',io?.busy]]){const point=$('monitor-point-'+key);point.setAttribute('visibility',valid(value)?'visible':'hidden');point.setAttribute('cx',x);if(valid(value))point.setAttribute('cy',plotBottom-(plotBottom-12)*value/100);}
    if(announce){const active=tasksAt(at),taskText=taskReadError?'性能任务记录未确认':active.length?'性能任务 '+active.map(taskLabel).join('、')+' 正在执行':'没有采集或 AI 处理任务执行';$('monitor-announcement').textContent=`${clock(at)}，整机 CPU ${percent(sample.cpu)}，硬盘繁忙度 ${percent(io?.busy)}，读 ${speed(io?.read_mbps)}，写 ${speed(io?.write_mbps)}。${taskText}。`;}
  }
  function showPoint(index,announce=false){const sample=samples[Math.max(0,Math.min(samples.length-1,index))];if(sample)showAt(sample.at,announce);}
  function showPointer(event){if(!samples.length)return;const rect=$('monitor-chart').getBoundingClientRect(),at=plotStart+(event.clientX-rect.left-40)/(plotRight-40)*60;showAt(at);}
  function renderAttribution(){
    const modules=data?.modules||[],groups=processes();options($('monitor-module'),[{id:'all',name:'全部模块'},...modules]);
    const selected=$('monitor-module').value,selection=modules.find(item=>item.id===selected),unknown=stale(),rows=[];
    for(const group of groups){if(selection&&!selection.group_ids.includes(group.id))continue;rows.push({...group,detail:group.id==='shared'?'共用进程 · 仅计一次':'仅统计已核验归属的进程',note:group.id==='shared'?'进程探针可计量合计，无法拆分模块':group.status==='stopped'?'当前未运行':group.status==='warming'?'正在建立计数基线':group.status==='unknown'?'未取得进程用量':'独立进程，可单独归属'});if(group.id==='shared')for(const module of modules.filter(item=>item.attribution==='shared'&&item.group_ids.includes(group.id)&&(selected==='all'||item.id===selected)))rows.push({id:module.id,name:module.name,sharedModule:true,note:group.status==='stopped'?'后台未运行':'无法单独拆分；已含于共享合计'});}
    for(const module of modules.filter(item=>(selected==='all'||item.id===selected)&&item.attribution==='unassigned'))rows.push({id:module.id,name:module.name,note:'未纳入采样，不按 0 处理',detail:'未确认本机进程归属'});
    $('monitor-process-asof').textContent=unknown?'当前用量未知'+(latest()?' · 最后采样 '+clock(latest().at):''):'最新采样 '+clock(latest().at)+' · 共享合计只计一次';
    $('monitor-attribution-note').textContent=selection?.attribution==='shared'?selection.name+'与其他模块共用后台；以下数值是共享合计，不代表该模块独占。':'共享后台只计一次。下表为进程 I/O，含缓存和其他磁盘，不等于上方物理盘读写量。';
    const body=$('monitor-processes');body.replaceChildren();
    for(const row of rows){const tr=node('tr'),name=node('td');name.append(node('strong',row.name));if(row.detail)name.append(node('small',row.detail));tr.dataset.probeGroup=row.id;if(row.sharedModule)tr.dataset.sharedModule='';tr.append(name);for(const key of ['cpu','read_mbps','write_mbps'])tr.append(node('td',unknown||!valid(row[key])?'—':key==='cpu'?percent(row[key]):speed(row[key])));tr.append(node('td',unknown?'当前采样未取得；'+row.note:row.note));body.append(tr);}
    if(!rows.length){const tr=node('tr'),cell=node('td','尚未取得可归属的进程采样');cell.colSpan=5;tr.append(cell);body.append(tr);}
  }
  function render(){const current=$('monitor-disk').value;options($('monitor-disk'),disks().map(item=>({id:item.id,name:item.name+' · '+(item.kind==='hdd'?'HDD':item.kind==='ssd'?'SSD':'类型未确认')+(item.mounts?.length?' · '+item.mounts.join(' / '):'')})));if(!disks().some(item=>item.id===current))$('monitor-disk').value=(disks().find(item=>item.kind==='hdd')||disks()[0])?.id||'';if(!disks().length)options($('monitor-disk'),[{id:'',name:'未取得物理磁盘'}]);$('monitor-disk').disabled=!disks().length;renderMetrics();renderChart();renderAttribution();}
  async function refresh(){
    if(!visible()||loading)return;loading=true;
    try{
      const [runtimeResult,taskResult]=await Promise.allSettled([api('/runtime-monitor'),api('/activity/performance?window_seconds=75')]);
      if(runtimeResult.status==='fulfilled'){data=runtimeResult.value;readError='';}
      else readError=runtimeResult.reason?.message||String(runtimeResult.reason||'读取失败');
      if(taskResult.status==='fulfilled'){
        tasks=normalizeTasks(taskResult.value?.records);taskWarnings=Array.isArray(taskResult.value?.warnings)?taskResult.value.warnings:[];taskReadError='';
      }else{tasks=[];taskWarnings=[];taskReadError=taskResult.reason?.message||String(taskResult.reason||'读取失败');}
    }finally{loading=false;if(visible()){window.setRuntimeSnapshot?.(readError?null:data);render();clearTimeout(timer);timer=setTimeout(refresh,5000);}}
  }
  function resume(){clearTimeout(timer);if(visible())refresh();else hidePoint();}
  window.initRuntimeMonitor=()=>{
    if(ready)return;ready=true;render();
    $('monitor-disk').addEventListener('change',()=>{renderMetrics();renderChart();});$('monitor-module').addEventListener('change',renderAttribution);
    const chart=$('monitor-chart');chart.addEventListener('pointermove',event=>{if(event.pointerType!=='touch'||event.buttons)showPointer(event);});chart.addEventListener('pointerdown',showPointer);chart.addEventListener('pointerleave',event=>{if(event.pointerType!=='touch')hidePoint();});chart.addEventListener('pointercancel',hidePoint);
    chart.addEventListener('focus',()=>{if(selectedSample===null&&chart.matches(':focus-visible'))showPoint(samples.length-1,true);});chart.addEventListener('blur',hidePoint);
    chart.addEventListener('keydown',event=>{if(!['ArrowLeft','ArrowRight','Home','End','Escape'].includes(event.key))return;event.preventDefault();if(event.key==='Escape'){hidePoint();return;}showPoint(event.key==='Home'?0:event.key==='End'?samples.length-1:(selectedSample??samples.length-1)+(event.key==='ArrowLeft'?-1:1),true);});
    document.addEventListener('pointerdown',event=>{if(!chart.contains(event.target))hidePoint();});document.addEventListener('visibilitychange',resume);window.addEventListener('hashchange',resume);
    let width=0;new ResizeObserver(entries=>{const next=entries[0].contentRect.width;if(next>0&&next!==width){width=next;renderChart();}}).observe(chart);resume();
  };
})();
