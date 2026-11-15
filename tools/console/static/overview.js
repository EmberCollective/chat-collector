'use strict';
(() => {
  const node=(tag,text,cls)=>{const item=document.createElement(tag);if(text!=null)item.textContent=text;if(cls)item.className=cls;return item;};
  const $=id=>document.getElementById(id);
  const statusNames={unknown:'结果未确认',unsupported:'未提供统计',running:'执行中',updating:'更新计划中',retry:'等待重试',paused:'已暂停',disabled:'未启用',waiting:'定时等待',idle:'等待调度',stopped:'已停止',starting:'启动中',stopping:'停止中',restarting:'重新启动中',success:'成功',failed:'失败',sent:'已发送',queued:'已排队',applied:'已回写',conflict:'状态冲突'};
  const count=value=>value==null?'—':value.toLocaleString();
  const rate=value=>value==null?'—':(value*100).toFixed(1)+'%';
  const clock=value=>value==null?'—':new Date(value*1000).toLocaleTimeString('zh-CN',{hour12:false,timeZone:'Asia/Shanghai'});
  const stamp=value=>value==null?'未记录':time(value);
  const state=value=>statusNames[value]||states[value]||value||'状态未知';
  const nextStep=issue=>issue.next_action_label||({verify_delivery:'先按目标、内容和时间核对是否送达，再确认结果或安排重试。',retry_delivery:'核对投递错误和目标配置后，再安排重试。',retry_feedback:'核对待办的当前状态后，再安排回写。',resolve_conflict:'刷新待办后重新操作；旧请求不能覆盖较新的修改。',open_config:'打开相关配置，核对实际运行状态。'})[issue.next_action]||'核对错误记录与相关配置，再决定后续操作。';
  const serviceState=service=>!service.verified||service.state==='unknown'?'状态未知':service.state==='running'&&service.running?'运行中':state(service.state);
  const warningLabels={storage_partial:'部分存储路径尚未完成测量',state_missing:'运行数据库尚不存在',history_not_provided:'旧运行数据库未提供历史记录',not_configured:'尚未配置后台服务',console_records_unreadable:'部分本地运行记录不可读',history_window_truncated:'历史保留或读取上限可能截断当前窗口'};
  const warningText=value=>typeof value==='string'?value:warningLabels[value.code]||((value.module==='automation'?'后台服务':names[value.module]||value.module||'部分')+'数据暂时不可读');
  const issueLabels=new Map([['result_unknown','本轮结果尚未确认，请先核对实际输出。'],['config_changed','已保存配置与运行中服务不一致，请在后台服务中核对。'],['service_stopped','后台服务已停止，已启用的计划暂不执行。'],['service_unknown','无法确认后台进程状态，请先核对。'],['service_duplicate','发现重复后台进程，请在后台服务中检查。'],['retry','任务正在等待重试，请核对上次错误。'],['paused','任务因错误暂停，请核对原因后恢复。'],['failed','任务执行失败，请查看原始记录。'],['unknown','执行结果未知，请先核对实际输出。'],['conflict','回写与当前待办状态冲突，请刷新待办后重新操作。']]);
  const issueText=issue=>issueLabels.get(issue.error)||issue.error||nextStep(issue);
  let snapshot=null,runtime=null,runtimeKey='',ready=false,loading=false,generation=0,metric='messages',scope={},records=[],current=null,returnContext=null,detailGeneration=0,listGeneration=0;
  const duration=seconds=>{seconds=Math.max(0,Math.floor(seconds));return (seconds>=3600?Math.floor(seconds/3600)+' 小时 ':'')+(seconds>=60?Math.floor(seconds/60)%60+' 分 ':'')+seconds%60+' 秒';};
  const liveSample=()=>{const sample=runtime?.samples?.at(-1);return sample&&Array.isArray(sample.active_jobs)&&Date.now()/1000-sample.at<=15&&sample.at>=(snapshot?.generated_at||0)?sample:null;};
  const sameModule=(task,job)=>job.source_id?task.source_id===job.source_id:task.module===job.module;
  function runTimer(job){const timer=node('strong',null,'schedule-elapsed');if(job.last_start!=null){timer.dataset.runStart=job.last_start;timer.dataset.runObserved=liveSample()?.at||snapshot.generated_at;}else timer.textContent='开始时间未记录';return timer;}
  function updateRunTimers(){
    for(const timer of $('services').querySelectorAll('[data-run-start]'))timer.textContent=Date.now()/1000-Number(timer.dataset.runObserved)>15?'运行状态待刷新':'已运行 '+duration(Date.now()/1000-Number(timer.dataset.runStart));
  }
  window.setRuntimeSnapshot=value=>{
    runtime=value;const sample=liveSample(),key=JSON.stringify(sample?.active_jobs??null);
    const changed=sample&&(snapshot?.jobs||[]).some(job=>(job.state==='running')!==sample.active_jobs.some(task=>sameModule(task,job)));
    if(snapshot&&(key!==runtimeKey||changed)){renderServices();if(changed)guard(refreshOverview);}
    else if(sample)for(const timer of $('services').querySelectorAll('[data-run-start]'))timer.dataset.runObserved=sample.at;
    runtimeKey=key;updateRunTimers();
  };

  async function refreshOverview(){
    if(!ready||loading||document.hidden||$('view-overview').hidden)return;
    loading=true;const request=++generation;
    try{
      const result=await api('/overview?hours='+$('overview-hours').value);
      if(request!==generation)return;
      snapshot=result;render();
    }catch(error){
      if(request!==generation)return;
      $('last-refresh').textContent=(snapshot?'数据已过期 · ':'读取失败 · ')+error.message;
      $('attention').hidden=false;$('attention').classList.remove('good');
      const retry=node('button','重新读取','secondary');retry.addEventListener('click',()=>guard(refreshOverview));
      const title=node('div',null,'alert-heading');title.append(node('h2','运行数据暂时无法读取'),retry);
      $('attention').replaceChildren(title,node('p','无法确认当前系统是否正常。'+(snapshot?'下方保留上次成功读取的数据。':'成功率、数量与状态均不按 0 处理。'),'scope'));
    }finally{loading=false;}
  }
  function render(){
    const s=snapshot,summary=s.summary||{},issues=s.issues||[],service=s.service||{};
    $('last-refresh').textContent='刷新于 '+clock(s.generated_at);
    const windowStamp=value=>new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'long',day:'numeric',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).format(new Date(value*1000));
    $('overview-window').textContent=windowStamp(s.window.start)+' — '+windowStamp(s.window.end);$('overview-window').title=stamp(s.window.start)+' — '+stamp(s.window.end);
    const attention=$('attention');attention.hidden=!issues.length&&!!summary.valid_count;attention.replaceChildren();attention.classList.toggle('good',!issues.length);
    const heading=node('div',null,'alert-heading'),title=node('h2',issues.length?issues.length+' 项问题需要处理':summary.valid_count?'当前没有待处理异常':'等待首次有效采集');
    if(issues.length)title.prepend(node('span','● '));
    heading.append(title,node('span',issues.length?'先核对失败与未知结果':summary.valid_count?'结合进程与任务状态核对':'尚不能判断运行质量','muted'));attention.append(heading);
    for(const issue of issues){
      const row=node('div',null,'problem-row'),text=node('div'),unknown=['unknown','delivery_unknown'].includes(issue.kind)||issue.status==='unknown';
      text.append(node('strong',issue.title||(names[issue.source_id||issue.module]||issue.source_id||issue.module||'系统')+'：'+(unknown?'结果待确认':'运行异常')),node('p',issueText(issue)));
      row.append(node('span',unknown?'待确认':issue.severity==='warning'?'需检查':'失败','problem-tag'+(unknown||issue.severity==='warning'?' warn':'')),text,node('time',clock(issue.last_at)));
      const button=node('button','查看相关记录','secondary');button.dataset.issueKey=issue.key;button.addEventListener('click',()=>guard(()=>openIssue(issue)));row.append(button);attention.append(row);
    }
    const metrics=[['入库消息变更',count(summary.emitted)+(summary.emitted==null?'':' 条'),(summary.emitted_complete===false?'统计不完整 · ':'含新增与修订 · ')+'重复跳过 '+count(summary.duplicates)+' 条'],['正式采集成功率',rate(summary.success_rate),summary.valid_count?`${summary.success} 成功 / ${summary.valid_count} 个有效结束轮次`:'暂无有效样本'],['平均采集耗时',summary.avg_seconds==null?'—':summary.avg_seconds.toFixed(1)+' 秒',summary.sample_count?`${count(summary.sample_count)} 个有效结束轮次 · 含失败耗时`:'暂无有效样本']];
    $('kpis').replaceChildren(...metrics.map(([label,value,detail,bad])=>{const item=node('div',null,'metric');item.append(node('span',label),node('strong',value,bad?'bad':''),node('small',detail));return item;}));
    renderSources();renderServices();renderChart();renderResources();window.setOverviewSnapshot?.(s);
    const warnings=s.warnings||[];
    $('overview-coverage').textContent=warnings.length?'统计范围：'+warnings.map(warningText).join('；'):'统计按开始时间归入窗口；仅包含本地仍保留且可读取的记录。空间为当前项目文件逻辑大小，不代表整盘或物理占用。';
  }
  function renderSources(){
    const table=node('table'),head=node('tr'),thead=node('thead'),body=node('tbody');
    for(const label of ['信息源','当前状态','入库变更','平均 / P95 耗时','成功率 · 最近 20 次','最近成功','问题'])head.append(node('th',label));thead.append(head);table.append(thead,body);
    for(const source of snapshot.sources||[]){
      const row=node('tr'),name=node('td');name.append(node('strong',source.name),node('small',source.id));row.append(name,node('td',state(source.status)));
      if(!source.metrics_available){for(const label of ['—','—','未提供统计','—','—'])row.append(node('td',label));}
      else{
        const amount=node('td',count(source.emitted)),duration=node('td',source.avg_seconds==null?'—':source.avg_seconds.toFixed(1)+' / '+(source.p95_seconds==null?'—':source.p95_seconds.toFixed(1))+' 秒'),success=node('td',null,'success-cell');
        if(source.emitted_complete===false)amount.append(node('small','统计不完整'));
        duration.append(node('small',count(source.sample_count)+' 个有效样本'));success.append(node('span',rate(source.success_rate),'rate'+(source.failed?' warn':'')));
        const ticks=node('div',null,'run-ticks');ticks.setAttribute('role','group');ticks.setAttribute('aria-label','最近 '+source.recent.length+' 次有效轮次');
        for(const run of source.recent){const tick=node('button',null,run.outcome==='failed'?'failed':'success');tick.title=source.name+' · '+state(run.outcome)+' · '+stamp(run.started_at);tick.setAttribute('aria-label',tick.title);tick.addEventListener('click',()=>guard(()=>openOverviewJob(run.run_id,run.run_source,{module:source.id,action:'collect',label:source.name+' · 正式采集'})));ticks.append(tick);}
        success.append(ticks,node('small',`${source.success}/${source.valid_count} 成功 · 跳过 ${source.skipped}${source.unknown?' · 未确认 '+source.unknown:''}`));
        const sameDay=source.last_success!=null&&new Date(source.last_success*1000).toLocaleDateString('zh-CN',{timeZone:'Asia/Shanghai'})===new Date(snapshot.generated_at*1000).toLocaleDateString('zh-CN',{timeZone:'Asia/Shanghai'});
        row.append(amount,duration,success,node('td',source.last_success==null?'尚无成功':sameDay?clock(source.last_success):stamp(source.last_success)));
        const cell=node('td','—');
        if(source.failed){const button=node('button',source.failed+' 次失败 · 查看','text-button');button.dataset.sourceProblem=source.id;button.addEventListener('click',()=>guard(()=>openRuns({module:source.id,action:'collect',status:'failed',label:source.name+' · 失败记录'})));cell.replaceChildren(button);}row.append(cell);
      }
      body.append(row);
    }
    if(!body.children.length){const row=node('tr'),cell=node('td','尚未接入信息源','empty');cell.colSpan=7;row.append(cell);body.append(row);}
    $('collection-table').replaceChildren(table);
  }
  function renderServices(){
    const s=snapshot,service=s.service||{},sample=liveSample(),queues=s.queues||{},sources=s.sources||[];
    const jobs=(s.jobs||[]).map(job=>{if(!sample)return job;const active=sample.active_jobs.find(task=>sameModule(task,job));return active?{...job,state:'running',last_start:active.started_at}:job.state==='running'?{...job,state:'updating',next_run:null}:job;});
    $('system-pill').textContent='后台服务'+serviceState(service);
    $('system-pill').classList.toggle('warn',!service.running||service.verified===false);
    const collect=jobs.filter(job=>job.source_id),processor=jobs.find(job=>job.module==='processor'),delivery=jobs.find(job=>job.module==='messaging'),feedback=jobs.find(job=>job.module==='feedback');
    const queueNote=(queue,keys)=>queue?.counts?keys.map(([key,label])=>count(queue.counts[key]??0)+' '+label).join(' · '):'队列状态尚不可读';
    const running=collect.filter(job=>job.state==='running').length;
    const rows=[['collect','信息源采集',collect.filter(job=>job.enabled).length+' 个来源'+(running?' · '+running+' 个执行中':''),''],['process','待办处理',processor?processor.state==='running'?'执行中':processor.enabled?state(processor.state):'已关闭':'状态未知',''],['delivery','消息投递',delivery?delivery.enabled?state(delivery.state):'未启用':'状态未知',queueNote(queues.messaging,[['pending','待投递'],['retry','待重试'],['unknown','结果未知']])],['feedback','状态回写',feedback?feedback.enabled?state(feedback.state):'未启用':'状态未知',queueNote(queues.feedback,[['queued','待回写'],['failed','失败'],['conflict','冲突']])]];
    const serviceRows=[];
    for(const [id,name,value,note]of rows){
      const row=node('div',null,'service-row'),text=node('div'),status=node('div',null,'service-state');row.dataset.serviceId=id;text.append(node('strong',name));if(note)text.append(node('p',note));
      const issue=(s.issues||[]).find(issue=>id==='collect'?!!issue.source_id:issue.module===({process:'processor',delivery:'messaging',feedback:'feedback'})[id]);
      status.append(node('span',value,'pill'+(issue?' warn':'')));
      const button=node('button',issue?'查看问题':id==='process'?'查看 Pi 工作流':'自动化设置','text-button');
      button.addEventListener('click',()=>guard(async()=>{if(issue)return openIssue(issue);location.hash=id==='process'?'pi-workflow':'automation/'+(['delivery','feedback'].includes(id)?'channels':'background');}));
      if(issue)button.dataset.serviceProblem=issue.key;
      if(['collect','process'].includes(id)){
        row.classList.add('schedule-service');const heading=node('div',null,'schedule-heading');text.append(...status.childNodes);heading.append(text,button);row.append(heading);
        const entries=id==='collect'?sources.filter(source=>collect.some(job=>job.source_id===source.id)||sourceInfo(source.id)?.capabilities?.schedule).map(source=>[source.name,collect.find(job=>job.source_id===source.id)]):[[processor?.state==='running'?'当前处理':'下次处理',processor]];
        for(const [label,job]of entries){const entry=node('div',null,'schedule-entry'),value=node('div'),name=node('div',null,'schedule-source');entry.dataset.module=job?.source_id||job?.module||'';
          const executing=job?.state==='running'&&(service.verified&&service.running||job.manual_run_id||sample?.active_jobs.some(task=>sameModule(task,job)));entry.classList.toggle('is-running',!!executing);
          if(executing){const button=node('button',label,'text-button');button.title='查看'+label+'执行记录';button.addEventListener('click',()=>guard(()=>openRuns({module:job.source_id||job.module,status:'running',label:label+' · 执行中'})));name.append(button,node('span','执行中','pill'));}else name.append(node('span',label));entry.append(name,value);
          if(!job)value.append(node('span','未提供下次计划'),node('small','由来源自行调度或尚未计算'));
          else if(executing){value.append(runTimer(job),node('small',job.last_start!=null?clock(job.last_start)+' 开始':'本轮结束后计算下次计划'));}
          else if(!service.verified||service.state==='unknown')value.append(node('span','后台状态未知'),node('small','需核对实际调度状态'));
          else if(!service.running)value.append(node('span','后台已停止'),node('small','启动后重新计算计划'));
          else if(job.state==='updating')value.append(node('span','本轮已结束'),node('small','正在读取下次计划'));
          else if(!job.enabled)value.append(node('span','定时计划关闭'),node('small','不会按原时间自动执行'));
          else if(job.state==='paused')value.append(node('span','错误暂停 · 等待恢复'));
          else if(job.next_run!=null){const time=node('time',new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'}).format(new Date(job.next_run*1000)));time.dateTime=new Date(job.next_run*1000).toISOString();time.title=job.state==='retry'?'计划重试':'下次执行';value.append(node('small',job.state==='retry'?'计划重试':'下次执行'),time);}
          else value.append(node('span','未提供下次计划'),node('small','等待调度器计算'));row.append(entry);
        }
        if(!entries.length)row.append(node('p','尚未启用可调度的信息源','muted'));
      }else{const job=id==='delivery'?delivery:feedback,timer=job?.state==='running'?runTimer(job):node('strong','\u00a0','schedule-elapsed');if(job?.state!=='running')timer.setAttribute('aria-hidden','true');text.append(timer);status.append(button);row.append(text,status);}
      serviceRows.push(row);
    }
    $('services').replaceChildren(...serviceRows);
    updateRunTimers();
  }
  function renderChart(){
    if(!snapshot)return;
    const bins=snapshot.success_bins||[],width=Math.max(200,$('rate-chart').querySelector('svg')?.clientWidth||$('rate-chart').clientWidth-40),height=Math.max(220,$('rate-chart').querySelector('svg')?.clientHeight||220),left=44,right=20,top=20,bottom=36,plot=height-top-bottom;
    const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox',`0 0 ${width} ${height}`);svg.setAttribute('role','group');svg.setAttribute('aria-label','正式采集成功率，纵轴0到100%');
    const add=(tag,attrs,text)=>{const item=document.createElementNS(svg.namespaceURI,tag);for(const [key,value]of Object.entries(attrs))item.setAttribute(key,value);if(text!=null)item.textContent=text;svg.append(item);return item;};
    const x=i=>left+i*(width-left-right)/Math.max(1,bins.length-1),y=bin=>top+plot*(1-bin.success_rate);
    for(const pct of [0,25,50,75,100]){const at=top+plot*(1-pct/100);add('line',{x1:left,y1:at,x2:width-right,y2:at,stroke:'#e0e6e2','stroke-width':1});add('text',{x:left-8,y:at+4,'text-anchor':'end',fill:'#68756e','font-size':13},pct+'%');}
    const step=Math.max(1,Math.ceil(bins.length/6));for(let i=0;i<bins.length;i+=step)add('text',{x:x(i),y:height-10,'text-anchor':'middle',fill:'#68756e','font-size':13},clock(bins[i].start).slice(0,5));
    let segment=[];const flush=()=>{if(segment.length)add('polyline',{points:segment.join(' '),fill:'none',stroke:'#12634f','stroke-width':2});segment=[];};
    bins.forEach((bin,i)=>{if(bin.success_rate==null)flush();else segment.push(x(i)+','+y(bin));});flush();
    if(!bins.some(bin=>bin.success_rate!=null))add('text',{x:width/2,y:height/2,'text-anchor':'middle',fill:'#68756e','font-size':13},'尚无有效轮次，等待首次采集');
    bins.forEach((bin,i)=>{
      if(bin.success_rate==null)return;
      const circle=add('circle',{cx:x(i),cy:y(bin),r:bin.failed?5:3,fill:bin.failed?'#a43b32':'#12634f'}),title=document.createElementNS(svg.namespaceURI,'title');title.textContent=`${clock(bin.start)} — ${clock(bin.end)}：${bin.success} 成功、${bin.failed} 失败，成功率 ${rate(bin.success_rate)}`;circle.append(title);
      if(bin.failed){circle.setAttribute('role','button');circle.setAttribute('tabindex','0');circle.setAttribute('aria-label',title.textContent+'，查看记录');const open=()=>guard(()=>openRuns({action:'collect',status:'failed',start:bin.start,end:bin.end,label:clock(bin.start)+' — '+clock(bin.end)+' · 正式采集失败'}));circle.addEventListener('click',open);circle.addEventListener('keydown',event=>{if(['Enter',' '].includes(event.key)){event.preventDefault();open();}});}
    });
    const legend=node('div',null,'legend');for(const [label,bad]of [['成功率',false],['包含失败的时间段',true]]){const span=node('span');span.append(node('i',null,bad?'bad':''),document.createTextNode(label));legend.append(span);}const area=node('div',null,'rate-plot');area.append(svg);$('rate-chart').replaceChildren(area,legend);
  }
  function formatStorage(value){return value==null?'未统计':value>=1e9?(value/1e9).toFixed(1)+' GB':value>=1e6?(value/1e6).toFixed(1)+' MB':value>=1e3?(value/1e3).toFixed(1)+' KB':value.toLocaleString()+' B';}
  function renderResources(){
    const resources=snapshot.resources||{},messageMode=metric==='messages',palette=['#12634f','#78a592','#c2a673','#7198a5','#9c86ad'];
    const rows=(snapshot.sources||[]).map((source,i)=>{const storage=(resources.sources||[]).find(item=>item.source_id===source.id);return {id:source.id,name:source.name,messages:source.emitted,partial:source.emitted_complete===false,bytes:storage?.logical_bytes,color:palette[i%palette.length]};});
    rows.push({id:'shared',name:'共享 / 未归属',messages:null,bytes:resources.shared?.logical_bytes,color:'#b3beb7'});
    const messageTotal=rows.reduce((sum,row)=>sum+(row.messages||0),0),byteTotal=rows.reduce((sum,row)=>sum+(row.bytes||0),0),total=messageMode?messageTotal:byteTotal,available=rows.some(row=>(messageMode?row.messages:row.bytes)!=null);
    $('resource-messages').setAttribute('aria-pressed',String(messageMode));$('resource-storage').setAttribute('aria-pressed',String(!messageMode));
    $('resource-total').textContent=!available?'—':messageMode?count(total):formatStorage(total);$('resource-unit').textContent=messageMode?snapshot.window.hours+' 小时入库变更 / 条':'已统计项目文件 / 当前';
    const ns='http://www.w3.org/2000/svg',svg=document.createElementNS(ns,'svg');svg.setAttribute('viewBox','0 0 240 240');svg.setAttribute('role','img');svg.setAttribute('aria-label',messageMode?'已统计来源的消息贡献占比':'已统计项目文件的空间占比');
    const ring=(attrs,title)=>{const circle=document.createElementNS(ns,'circle');for(const [key,value]of Object.entries(attrs))circle.setAttribute(key,value);if(title){const label=document.createElementNS(ns,'title');label.textContent=title;circle.append(label);}svg.append(circle);};
    const radius=92,length=2*Math.PI*radius;ring({cx:120,cy:120,r:radius,fill:'none',stroke:'#edf1ee','stroke-width':28});let offset=0;
    if(total)for(const row of rows){const value=messageMode?row.messages:row.bytes;if(!value)continue;const portion=value/total*length;ring({cx:120,cy:120,r:radius,fill:'none',stroke:row.color,'stroke-width':28,'stroke-dasharray':`${portion} ${length-portion}`,'stroke-dashoffset':-offset,transform:'rotate(-90 120 120)'},`${row.name}：${messageMode?count(value)+' 条':formatStorage(value)}，${rate(value/total)}`);offset+=portion;}
    $('resource-chart').replaceChildren(svg);
    const table=node('table'),header=node('tr');for(const [label,active]of [['来源',false],['消息贡献 · '+snapshot.window.hours+' 小时',messageMode],['存储占用 · 当前',!messageMode]])header.append(node('th',label,active?'active':''));table.append(header);
    for(const row of rows){const tr=node('tr'),source=node('td'),dot=node('span',null,'source-dot');dot.style.backgroundColor=row.color;source.append(dot,document.createTextNode(row.name));tr.append(source);for(const [value,sum,mode]of [[row.messages,messageTotal,'messages'],[row.bytes,byteTotal,'storage']]){const cell=node('td',value==null?(row.id==='shared'&&mode==='messages'?'不适用':'未统计'):mode==='messages'?count(value)+' 条':formatStorage(value),'number'+(metric===mode?' active-value':''));cell.append(node('small',value==null?(row.id==='shared'&&mode==='messages'?'不作为消息来源':'不按 0 计算'):(row.partial&&mode==='messages'?'部分已记录 · ':'')+(sum?rate(value/sum):'无可计算占比')));tr.append(cell);}table.append(tr);}
    const help=node('p','消息量反映产出规模，不代表信息价值。相同文件只计一次，多来源共用的目录或文件放入“共享 / 未归属”。','resource-help');$('resource-legend').replaceChildren(table,help);
    $('resource-scope').textContent=messageMode?'消息贡献：最近 '+snapshot.window.hours+' 小时已提供统计的 '+rows.filter(row=>row.messages!=null).length+' 个来源；包含新增与修订，未统计来源不按 0 处理。':'存储占用：盘点于 '+stamp(resources.measured_at)+'，仅含配置指定的状态库、明文快照、消息批次与附件归档；为逻辑大小，不等同于整盘或物理占用。'+(resources.complete===false?'存在不可读或未完成的目录，缺口不按 0 计算。':'');
  }

  function windowScope(options={}){return {start:snapshot?.window.start,end:snapshot?.window.end,label:'正式采集 · 所有来源',...options};}
  async function openRuns(options={}){
    scope=windowScope(options);current=null;$('run-result-filter').value=scope.status||'all';
    if(!$('run-dialog').open)$('run-dialog').showModal();await renderRunList();
  }
  async function renderRunList(){
    const request=++listGeneration;++detailGeneration;
    $('dialog-heading').textContent='相关运行';$('run-list').hidden=false;$('run-detail').hidden=true;$('show-history').hidden=false;
    $('run-scope').textContent=scope.label||'运行记录';$('related-runs').replaceChildren(node('p','正在读取记录…','empty'));
    const params=new URLSearchParams({limit:'30',sort:scope.sort||'latest'});
    for(const key of ['start','end','module','action','origin','error','min_duration_seconds','max_duration_seconds'])if(scope[key]!=null&&scope[key]!=='')params.set(key,scope[key]);
    const filter=$('run-result-filter').value;if(filter!=='all')params.set('status',filter==='unknown'?'interrupted':filter);
    let result;
    try{result=await api('/jobs?'+params);}catch(error){if(request===listGeneration)$('related-runs').replaceChildren(node('p','相关记录读取失败：'+error.message+'。当前筛选已保留，可在运行记录中重新查看。','run-notice'));return;}
    if(request!==listGeneration)return;
    records=result.jobs;const table=node('table'),head=node('tr');for(const value of ['开始时间','模块','操作','触发方式','结果','耗时','详情'])head.append(node('th',value));table.append(head);
    for(const job of records){const row=node('tr');for(const value of [stamp(job.started_at),names[job.module]||job.module,actions[job.action]||job.action,originLabels[job.origin]||job.origin||'未注明',state(job.status),durationText(job.duration_seconds,job.duration_complete)])row.append(node('td',value));const cell=node('td'),button=node('button','查看详情','text-button');button.dataset.runId=job.id;button.addEventListener('click',()=>guard(()=>openOverviewJob(job.id,job.source,scope)));cell.append(button);row.append(cell);table.append(row);}
    $('related-runs').replaceChildren(records.length?table:node('p','此范围没有符合条件的运行；筛选仍保留。','empty'));
    $('run-scope').textContent=(scope.label||'运行记录')+' · '+(scope.start!=null||scope.end!=null?stamp(scope.start)+' — '+stamp(scope.end):'全部时间')+' · '+records.length+' / '+result.total+' 条';
  }
  function jobConfig(job){return {source_id:job.source==='automation'?snapshot?.jobs?.find(item=>item.module===job.module)?.source_id:sourceInfo(job.module)?job.module:null,config_target:job.module==='processor'?'settings/model':job.module==='messaging'||job.module==='feedback'?'automation/channels':job.source==='automation'?'automation/background':'settings/source'};}
  async function openOverviewJob(id,source,options){
    const request=++detailGeneration;++listGeneration;
    if(options){scope=windowScope(options);$('run-result-filter').value=scope.status==='interrupted'?'unknown':scope.status||'all';}else if(!scope.label)scope=windowScope({label:'运行详情'});
    if(!$('run-dialog').open)$('run-dialog').showModal();$('run-list').hidden=true;$('run-detail').hidden=false;$('run-detail').replaceChildren(node('p','正在读取运行详情…','empty'));
    let job;
    try{job=await api('/activity/'+(source==='automation'?'automation':'console')+'/'+encodeURIComponent(id));}
    catch(error){if(request===detailGeneration){const detail=detailHeader('运行详情暂时不可读');detail.append(node('p',error.message+'。运行可能已超出保留范围；未记录的内容无法还原。','run-notice'));const retry=node('button','重新读取','secondary');retry.addEventListener('click',()=>guard(()=>openOverviewJob(id,source,scope)));detail.append(retry);}return;}
    if(request!==detailGeneration)return;
    if(job.duration_seconds==null&&job.finished_at!=null&&job.started_at!=null)job.duration_seconds=Math.max(0,job.finished_at-job.started_at);
    current={kind:'run',id,source,job};renderJob(job);
  }
  function detailHeader(title){$('dialog-heading').textContent=title;$('run-list').hidden=true;$('run-detail').hidden=false;$('show-history').hidden=false;$('run-scope').textContent=scope.label||'运行详情';const detail=$('run-detail');detail.replaceChildren();const back=node('button','返回相关运行','text-button');back.id='back-to-runs';back.addEventListener('click',()=>guard(renderRunList));detail.append(back);return detail;}
  function addFacts(detail,values){const facts=node('dl',null,'run-facts');for(const [label,value]of values)facts.append(node('dt',label),node('dd',value??'未记录'));detail.append(facts);}
  function configButton(target){const button=node('button','打开相关配置','secondary');button.addEventListener('click',()=>guard(()=>goConfig(target)));return button;}
  function renderJob(job){
    const detail=detailHeader((names[job.module]||job.module)+' · '+(actions[job.action]||job.action));
    addFacts(detail,[['操作',actions[job.action]||job.action],['模块',names[job.module]||job.module],['触发方式',originLabels[job.origin]||job.origin],['开始时间',stamp(job.started_at)],['耗时',durationText(job.duration_seconds,!!job.finished_at)],['结果',state(job.status)],['运行编号',job.id]]);
    detail.append(node('h3','错误与当前结果'),node('p',job.error||'没有已记录的错误；保存与送达结果仍以本轮原始结果为准。'),node('h3','下一步'),node('p',job.status==='failed'?'先核对错误位置、输入与实际输出，再决定后续操作。原失败证据会保留。':job.status==='interrupted'?'结果未确认，请先核对实际输出，避免重复执行。':job.status==='running'?'当前任务仍在执行，尚不能确认最终结果。':'核对本轮结果与后续处理记录。'));
    const controls=node('div',null,'actions'),target=jobConfig(job);if(scope.module&&sourceInfo(scope.module))target.source_id=scope.module;controls.append(configButton(target));
    const automatic=target.source_id?snapshot?.jobs?.find(item=>item.source_id===target.source_id):null;
    if(job.action==='collect'&&['failed','interrupted','warning'].includes(job.status)&&automatic&&sourceInfo(target.source_id)?.capabilities.collect){
      const retry=node('button','安排新一轮…','secondary');retry.addEventListener('click',()=>{
        if($('collection-retry-confirm'))return;
        const box=node('section',null,'run-notice');box.id='collection-retry-confirm';box.append(node('p','先核对数据库占用、已保存断点和实际输出。新一轮使用已保存配置，可能补充归档消息与附件；未保存草稿不生效。原失败记录保持原样。'));
        const confirm=node('button','确认安排新一轮','secondary');confirm.addEventListener('click',()=>guard(async()=>{confirm.disabled=true;try{const result=await api('/operations/jobs/'+encodeURIComponent(automatic.module)+'/once','POST');box.replaceChildren(node('p',['queued','starting','running'].includes(result.state)?'请求已受理或已有任务执行，尚未确认本轮成功。':'当前状态：'+state(result.state)+'。请在自动化中核对，未确认新一轮已开始。'));window.dispatchEvent(new Event('operations-updated'));}catch(error){confirm.disabled=false;box.append(node('p','安排失败：'+error.message));}}));box.append(confirm);controls.after(box);
      });controls.prepend(retry);
    }
    const copy=node('button','复制完整记录','secondary');copy.addEventListener('click',()=>guard(()=>navigator.clipboard.writeText(JSON.stringify(job,null,2))));controls.append(copy);
    const workflows=job.workflow_runs||[];
    if(job.action==='process'&&job.source!=='automation')workflows.unshift({id:job.id});
    for(const run of workflows){const button=node('button',run.attempt?'查看第 '+run.attempt+' 次 Pi 工作流':'查看 Pi 工作流','secondary');button.addEventListener('click',()=>guard(async()=>{rememberReturn();$('run-dialog').close();await selectWorkflow(run.id);}));controls.append(button);}detail.append(controls);
    detail.append(node('h3','本轮上下文'),node('p','完整模型上下文、请求与工具结果从关联 Pi 工作流读取。未记录的正文无法还原。','muted'));
    for(const attempt of job.attempts||[]){const block=node('details');block.open=job.attempts.length===1;block.append(node('summary','第 '+attempt.number+' 次 · '+state(attempt.status)+' · '+stamp(attempt.started_at)));if(attempt.error)block.append(node('p',attempt.error));if(attempt.details)block.append(node('pre',JSON.stringify(attempt.details,null,2)));if(attempt.result!=null)block.append(node('pre',JSON.stringify(attempt.result,null,2)));detail.append(block);}
    const raw=node('details');raw.append(node('summary','查看日志与原始记录'));if(job.log_tail)raw.append(node('pre',job.log_tail));raw.append(node('pre',JSON.stringify(job,null,2)));detail.append(raw);detail.focus({preventScroll:true});
  }
  async function openIssue(issue){
    scope=windowScope({module:issue.source_id||issue.module,label:(names[issue.source_id||issue.module]||issue.module||'系统')+' · 当前问题'});
    $('run-result-filter').value='all';
    if(issue.run_id)return openOverviewJob(issue.run_id,issue.run_source,scope);
    const request=++detailGeneration;++listGeneration;current={kind:'issue',issue};
    if(!$('run-dialog').open)$('run-dialog').showModal();
    const detail=detailHeader(issue.title||'问题详情');detail.append(node('h3','错误与当前结果'),node('p',issueText(issue)),node('h3','下一步'),node('p',nextStep(issue)));
    addFacts(detail,[['模块',names[issue.source_id||issue.module]||issue.module],['最后发生',stamp(issue.last_at)],['问题编号',issue.key]]);
    const controls=node('div',null,'actions');controls.append(configButton(issue));detail.append(controls);
    const target=issue.detail_target;
    if(target&&['delivery','feedback'].includes(target.kind)){
      let status;
      try{status=await api('/operations/status');}catch(error){if(request===detailGeneration)detail.append(node('p','恢复状态读取失败：'+error.message+'。请在行动与投递中核对当前记录。','run-notice'));return;}
      if(request!==detailGeneration)return;
      const item=target.kind==='delivery'?(status.messaging?.deliveries||[]).find(item=>String(item.id)===String(target.id)):(status.feedback?.requests||[]).find(item=>String(item.request_id)===String(target.id));
      if(item){detail.append(node('h3',target.kind==='delivery'?'投递记录':'回写记录'),node('pre',JSON.stringify(item,null,2)));addRecovery(detail,target,item);}else detail.append(node('p','该问题已变化或不在当前返回范围，请刷新概览后核对。','run-notice'));
    }
    detail.focus({preventScroll:true});
  }
  function addRecovery(detail,target,item){
    const active=detailGeneration;let busy=false;
    const box=node('section',null,'run-notice');box.append(node('h3',target.kind==='delivery'?'先核实目标端结果':'回写恢复'),node('p',target.kind==='delivery'?'按目标、内容和时间核对是否已收到。重试只重新排队，不代表已经送达。':'排队后仍需处理层确认保存；状态冲突不能覆盖较新的待办。'));
    let checked=null;
    if(target.kind==='delivery'&&item.status==='unknown'){const label=node('label');checked=node('input');checked.type='checkbox';checked.id='delivery-checked';label.append(checked,document.createTextNode('我已按内容与时间核对目标端'));box.append(label);}
    const controls=node('div',null,'actions');
    for(const [action,label]of target.kind==='delivery'?(item.status==='unknown'?[['delivered','确认已收到'],['retry','核实未达后重试']]:item.status==='failed'?[['retry','重新排入投递队列']]:[]):['failed','retry'].includes(item.status)?[['retry','重试回写']]:[]){
      const button=node('button',label,'secondary');button.disabled=!!checked;
      if(checked)checked.addEventListener('change',()=>button.disabled=busy||!checked.checked);
      button.addEventListener('click',()=>guard(async()=>{
        if(busy||checked&&!checked.checked)return;busy=true;
        for(const control of box.querySelectorAll('button,input'))control.disabled=true;
        try{
          await api('/operations/'+(target.kind==='delivery'?'deliveries':'feedback')+'/'+encodeURIComponent(target.id)+'/'+action,'POST');
          await refreshOverview();window.dispatchEvent(new Event('operations-updated'));
          if(active===detailGeneration){detail.append(node('p',action==='delivered'?'已保存人工送达确认。':'已安排重试，等待后续执行确认。','run-notice'));controls.replaceChildren();}
        }catch(error){
          busy=false;if(active!==detailGeneration)return;
          if(checked)checked.disabled=false;for(const control of controls.querySelectorAll('button'))control.disabled=!!checked&&!checked.checked;
          box.append(node('p','操作未完成：'+error.message+'。请刷新并核对当前记录后再试。'));
        }
      }));controls.append(button);
    }
    if(item.status==='conflict')box.append(node('p','请刷新待办后重新操作；旧请求不能覆盖较新的修改。'));box.append(controls);detail.append(box);
  }
  function rememberReturn(){returnContext={current,scope:{...scope},result:$('run-result-filter').value};$('overview-return-context').hidden=false;$('overview-return-label').textContent='来自运行：'+($('dialog-heading').textContent);}
  async function goConfig(target){
    rememberReturn();$('run-dialog').close();
    const id=target.source_id||target.config_target?.id;
    if(id&&sourceInfo(id)){$('module').value=id;await switchModule();}
    const destination=typeof target.config_target==='string'?target.config_target:id&&sourceInfo(id)?'settings/source':id==='processor'?'settings/model':['messaging','feedback'].includes(id)?'automation/channels':'automation/service';
    location.hash=/^(settings\/(source|model)|automation(\/(background|collection|channels|service|rules))?)$/.test(destination)?destination:'automation';
  }
  async function restoreRun(){
    if(!returnContext)return;const context=returnContext;scope=context.scope;$('run-result-filter').value=context.result;$('overview-return-context').hidden=true;location.hash='overview';
    if(context.current?.kind==='run')await openOverviewJob(context.current.id,context.current.source,context.scope);else if(context.current?.kind==='issue')await openIssue(context.current.issue);else await openRuns(context.scope);
  }
  async function showHistory(){
    const filter=$('run-result-filter').value;$('run-dialog').close();$('history-form').reset();
    for(const [key,id]of [['module','history-module'],['action','history-action'],['origin','history-origin']])if(scope[key]){const picker=$(id);if(![...picker.options].some(option=>option.value===scope[key])){const option=node('option',names[scope[key]]||actions[scope[key]]||originLabels[scope[key]]||scope[key]);option.value=scope[key];picker.append(option);}picker.value=scope[key];}
    for(const [key,id]of [['error','history-error'],['min_duration_seconds','history-min-duration'],['max_duration_seconds','history-max-duration'],['sort','history-sort']])if(scope[key]!=null)$(id).value=scope[key];
    $('history-filter').value=filter==='all'?'':filter==='unknown'?'interrupted':filter;
    for(const key of ['start','end'])if(scope[key]!=null){const date=new Date(scope[key]*1000);$('history-'+key).value=new Date(date.getTime()-date.getTimezoneOffset()*60000).toISOString().slice(0,19);}
    location.hash='history';await refreshHistory();
  }
  window.openOverviewJob=openOverviewJob;
  window.initOverview=async()=>{
    if(ready){if(!$('view-overview').hidden&&!document.hidden)await refreshOverview();return;}
    ready=true;
    for(const status of ['running','retry','warning']){const option=node('option',state(status));option.value=status;$('run-result-filter').append(option);}
    $('overview-hours').addEventListener('change',()=>{++generation;loading=false;guard(refreshOverview);});
    $('resource-messages').addEventListener('click',()=>{metric='messages';if(snapshot)renderResources();});$('resource-storage').addEventListener('click',()=>{metric='storage';if(snapshot)renderResources();});
    $('show-records').addEventListener('click',()=>guard(()=>openRuns({action:'collect'})));
    $('run-result-filter').addEventListener('change',()=>{scope.status=$('run-result-filter').value==='all'?'':$('run-result-filter').value;guard(renderRunList);});$('close-dialog').addEventListener('click',()=>{$('run-dialog').close();++detailGeneration;++listGeneration;});
    $('show-history').addEventListener('click',()=>guard(showHistory));$('overview-return-run').addEventListener('click',()=>guard(restoreRun));
    $('show-metric-definition').addEventListener('click',()=>{const details=$('metric-definition');details.open=true;details.querySelector('summary').focus();details.scrollIntoView({block:'start'});});
    let rateSize='';new ResizeObserver(entries=>{const {width,height}=entries[0].contentRect,size=width+':'+height;if(width>0&&size!==rateSize){rateSize=size;renderChart();}}).observe($('rate-chart'));
    window.addEventListener('operations-updated',()=>guard(refreshOverview));
    window.addEventListener('hashchange',()=>{if(!$('view-overview').hidden&&!document.hidden)guard(refreshOverview);});
    document.addEventListener('visibilitychange',()=>{if(!$('view-overview').hidden&&!document.hidden)guard(refreshOverview);});
    window.initRuntimeMonitor?.();
    if(!$('view-overview').hidden&&!document.hidden)await refreshOverview();setInterval(()=>{if(!$('view-overview').hidden&&!document.hidden)guard(refreshOverview);},30000);
    setInterval(()=>{if(!$('view-overview').hidden&&!document.hidden){updateRunTimers();window.updateRuntimeElapsed?.();}},1000);
  };
})();
