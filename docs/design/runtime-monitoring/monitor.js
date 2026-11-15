(() => {
'use strict';
const $=id=>document.getElementById(id);
const node=(tag,text,cls)=>{const el=document.createElement(tag);if(text!=null)el.textContent=text;if(cls)el.className=cls;return el;};
const now=Date.parse('2026-11-14T14:32:06+08:00');
const dateFormatter=new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'});
const stamp=at=>dateFormatter.format(at),clock=at=>stamp(at).slice(11);
const at=seconds=>now+seconds*1000;
// Fixed approval examples, never a fallback for live measurements.
const plans=new Map([['wechat-main',{next:at(1674)}],['qq-main',{next:at(474)}]]);
const disks=[
 {id:'example-disk-0',label:'磁盘 0 · HDD · D: / E:',busy:[18,23,20,26,32,24,21,29,33,58,82,78,74],read:[1.8,2.4,2.1,3.8,4.2,3,2.4,4.1,6.2,14.6,27.4,25.1,23.8],write:[0.4,0.6,0.5,0.8,1.2,0.7,0.8,1.4,2.1,5.2,9.4,8.8,8.2]},
 {id:'example-disk-2',label:'磁盘 2 · HDD · F:',busy:[5,7,4,6,5,8,5,7,6,9,12,10,8],read:[0.7,0.8,0.6,0.8,0.7,1.1,0.7,1,0.8,1.2,1.8,1.6,1.4],write:[0.1,0.2,0.1,0.2,0.1,0.3,0.1,0.2,0.1,0.2,0.4,0.3,0.3]}
];
const cpu=[12,14,16,20,28,32,21,18,22,34,44,41,38];
const sharedCpu=[1.2,1.4,2.1,2.4,3.2,4.2,3.1,2.4,2.6,6.8,21.2,19.1,16.8];
let samples=[],plotRight=884,plotBottom=152,selectedSample=null;
const mode=()=>$('monitor-scenario').value;
const sourceModules=()=>catalog.sources.map(source=>({id:source.id,name:source.name,shared:plans.has(source.id)}));
const modules=()=>[...sourceModules(),{id:'processor',name:'待办处理',shared:true},{id:'messaging',name:'消息投递',shared:true},{id:'feedback',name:'状态回写',shared:true},{id:'listener',name:'渠道监听'},{id:'console',name:'控制台'}];
function options(select,rows){const current=select.value;select.replaceChildren(...rows.map(row=>{const option=node('option',row.name||row.label);option.value=row.id;return option;}));if(rows.some(row=>row.id===current))select.value=current;}
options($('monitor-disk'),disks);
function scheduleEntry(name,job){
 const row=node('div',null,'schedule-entry'),value=node('div');row.append(node('span',name),value);
 if(mode()==='stopped'){value.append(node('span','后台已停止'),node('small','启动后重新计算计划'));}
 else if(mode()==='disabled'){value.append(node('span','定时计划关闭'),node('small','不会按原时间自动执行'));}
 else if(job.running){value.append(node('span','执行中 · 本轮结束后计算'),node('small','当前已运行 12 秒'));}
 else if(job.next!=null){const time=node('time',stamp(job.next));time.dateTime=new Date(job.next).toISOString();time.title=job.retry?'计划重试':'下次执行';value.append(time);}
 else value.append(node('span','未提供下次计划'),node('small','由来源自行调度或尚未计算'));
 return row;
}
function scheduleGroup(row,title,badge,entries){
 if(!row)return;
 const link=row.querySelector('button'),heading=node('div',null,'schedule-heading'),identity=node('div');
 identity.append(node('strong',title),node('span',badge,'pill'+(['stopped','retry'].includes(mode())?' warn':'')));heading.append(identity);
 if(link){if(link.dataset.serviceProblem==='collect')link.textContent='查看上次失败';heading.append(link);}
 row.classList.add('schedule-service');row.replaceChildren(heading,...entries);
}
function renderSchedules(){
 const enabled=catalog.sources.filter(source=>source.capabilities.schedule&&source.backgroundEnabled===true);
 scheduleGroup(document.querySelector('#services [data-service-id="collect"]'),'采集定时',mode()==='disabled'?'已关闭':enabled.length+' 个来源',enabled.map(source=>{
  const plan=plans.get(source.id)||{};
  return scheduleEntry(source.name,{...plan,running:mode()==='running'&&source.id==='qq-main',...(mode()==='retry'&&source.id==='qq-main'?{retry:true,next:at(114)}:{})});
 }));
 scheduleGroup(document.querySelector('#services [data-service-id="process"]'),'待办处理',mode()==='disabled'?'已关闭':mode()==='stopped'?'未运行':'等待下轮',[scheduleEntry('下次处理',{next:at(774)})]);
 const stopped=mode()==='stopped';
 $('system-pill').textContent=stopped?'后台服务已停止':'后台服务运行中';$('system-pill').classList.toggle('warn',stopped);
 if(stopped||mode()==='disabled')for(const id of ['delivery','feedback']){const row=document.querySelector(`#services [data-service-id="${id}"]`);if(!row)continue;row.querySelector('.pill').textContent=stopped?'未运行':'定时计划关闭';row.querySelector('p').textContent=stopped?'后台停止；保留队列与历史结果':'保留现有队列；未安排下一轮定时处理';}
 for(const row of document.querySelectorAll('#collection-table tbody tr')){const id=row.cells[0].querySelector('small')?.textContent;if(!plans.has(id))continue;row.cells[1].textContent=stopped?'后台已停止':mode()==='disabled'?'定时计划关闭':mode()==='running'&&id==='qq-main'?'执行中':mode()==='retry'&&id==='qq-main'?'等待重试':'定时等待';}
}
function renderOverviewSummary(){
 const kpis=$('kpis'),kept=[...kpis.children].filter(item=>['入库消息变更','正式采集成功率'].includes(item.querySelector('span')?.textContent));
 const duration=node('div',null,'metric'),empty=$('scenario').value==='empty';
 duration.append(node('span','平均采集耗时'),node('strong',empty?'—':'30.2 秒'),node('small',empty?'暂无有效样本':'70 个有效结束轮次 · 含失败耗时'));kpis.replaceChildren(...kept,duration);
 const attention=$('attention');attention.hidden=!attention.querySelector('.problem-row')&&!empty;
 const failed=[...attention.querySelectorAll('.problem-row')].find(row=>row.querySelector('strong')?.textContent==='QQ：采集未完成');
 if(failed){failed.querySelector('strong').textContent='QQ：上轮采集失败';failed.querySelector('p').textContent=mode()==='running'?'数据库被占用；本轮正在重试，原失败与断点保留。':mode()==='disabled'?'数据库被占用；定时计划已关闭，原失败与断点保留。':mode()==='stopped'?'数据库被占用；后台已停止，原失败与断点保留。':'数据库被占用；上次成功 14:28，原失败与断点保留。';}
}
function metric(label,value,note){const box=node('div',null,'monitor-metric');box.append(node('span',label),node('strong',value),node('small',note));return box;}
function renderMetrics(disk){
 const stale=mode()==='stale',unavailable=mode()==='unsupported',quiet=['waiting','stopped','disabled'].includes(mode());
 $('monitor-health').hidden=!stale&&!unavailable;$('monitor-health').textContent=stale?'采样已中断 · 最后取得 14:31:51，当前值未知':unavailable?'所选硬盘暂无法读取性能数据，CPU 仍可查看':'';
 $('monitor-asof').textContent='最近 60 秒 · 每 5 秒采样';
 const cpuBox=metric('整机 CPU',stale?'—':quiet?Math.max(4,Math.round(cpu.at(-1)*.3))+'%':'38%',stale?'当前值未知；保留最后有效采样':mode()==='stopped'?'本程序合计 0.3% · 仅控制台':quiet?'本程序合计 0.7% · 按整机总算力':'本程序合计 17.5% · 按整机总算力');
 const busy=quiet?Math.max(2,Math.round(disk.busy.at(-1)*.1)):disk.busy.at(-1),diskBox=metric('所选机械硬盘繁忙度',stale||unavailable?'—':busy+'%',unavailable?'未取得该物理盘的性能计数':stale?'当前值未知，不按 0% 处理':'整块物理盘 · 包含其他程序');
 const io=node('div',null,'monitor-metric');io.append(node('span','所选硬盘读写速度'));
 const values=node('div',null,'monitor-io-values');for(const [label,value]of [['读',quiet?'0.8':disk.read.at(-1).toFixed(1)],['写',quiet?'0.2':disk.write.at(-1).toFixed(1)]]){const part=node('div');part.append(node('small',label+' '),node('strong',stale||unavailable?'—':value),node('small',' MB/s'));values.append(part);}io.append(values,node('small','物理盘吞吐 · 与进程 I/O 分开统计'));
 $('monitor-metrics').replaceChildren(cpuBox,diskBox,io);
 $('monitor-current').textContent=mode()==='running'?'QQ 正式采集执行中 · 已运行 12 秒':mode()==='retry'?'QQ 等待重试 · 计划 14:34:00':stale?'最后采样 14:31:51 · 当前负载未知':mode()==='stopped'?'业务后台已停止 · 整机负载仍单独监测':mode()==='disabled'?'定时计划关闭 · 保留已有队列':mode()==='unsupported'?'示例采样 14:32:06 · 硬盘数据不可用':'暂无正在执行的任务 · 示例采样 14:32:06';
 $('monitor-unavailable-reason').textContent=stale?'采样中断示例：最后有效数据为 14:31:51。右侧缺口保留，恢复后重新建立采样基线，不跨缺口推算。':unavailable?'不可用示例：未能读取所选物理磁盘的性能计数器。不能据此认定硬盘不存在或繁忙度为 0；正式接入后按实际原因提示。':'这些是待审批的采样口径，本页没有启动探针，也未测量实际监测开销。';
}
function svgNode(tag,attrs,text){const el=document.createElementNS('http://www.w3.org/2000/svg',tag);for(const [key,value]of Object.entries(attrs))el.setAttribute(key,String(value));if(text!=null)el.textContent=text;return el;}
function renderChart(disk){
 const quiet=['waiting','stopped','disabled'].includes(mode());
 samples=cpu.map((value,index)=>{const missing=mode()==='stale'&&index>9,diskMissing=missing||mode()==='unsupported',stopped=mode()==='stopped';return {at:at(-60+index*5),cpu:missing?null:quiet?Math.max(4,Math.round(value*.3)):value,disk:diskMissing?null:quiet?Math.max(2,Math.round(disk.busy[index]*.1)):disk.busy[index],read:diskMissing?null:quiet?0.8:disk.read[index],write:diskMissing?null:quiet?0.2:disk.write[index],shared:missing||stopped?null:quiet?0.2:sharedCpu[index],listener:missing||stopped?null:0.2,console:missing?null:quiet?0.3:0.5};});
 const width=Math.max(240,$('monitor-chart').clientWidth),height=180;
 const svg=svgNode('svg',{viewBox:`0 0 ${width} ${height}`,'aria-hidden':'true'}),left=40,right=width-12,top=12,bottom=height-28;plotRight=right;plotBottom=bottom;
 for(const value of [0,25,50,75,100]){const y=bottom-(bottom-top)*value/100;svg.append(svgNode('line',{x1:left,x2:right,y1:y,y2:y,stroke:'#e0e6e2'}),svgNode('text',{x:0,y:y+4},value+'%'));}
 for(const [index,label]of [[0,'60 秒前'],[6,'30 秒前'],[12,'当前']])svg.append(svgNode('text',{x:left+(right-left)*index/12,y:height-4,'text-anchor':index===0?'start':index===12?'end':'middle'},label));
 for(const [key,color]of [['cpu','#12634f'],['disk','#865b16']]){let points=[];const flush=()=>{if(points.length)svg.append(svgNode('polyline',{points:points.join(' '),fill:'none',stroke:color,'stroke-width':2,'vector-effect':'non-scaling-stroke'}));points=[];};for(let i=0;i<samples.length;i++){if(samples[i][key]==null){flush();continue;}points.push(`${left+(right-left)*i/12},${bottom-(bottom-top)*samples[i][key]/100}`);}flush();}
 const marker=svgNode('g',{id:'monitor-marker',visibility:'hidden'});marker.append(svgNode('line',{id:'monitor-crosshair',x1:right,x2:right,y1:top,y2:bottom,stroke:'#7b8981','stroke-dasharray':'3 4'}));
 for(const [key,color]of [['cpu','#12634f'],['disk','#865b16']])marker.append(svgNode('circle',{id:'monitor-point-'+key,cx:right,cy:bottom,r:3,fill:color,stroke:'white','stroke-width':1.5}));svg.append(marker);
 const tooltip=node('div',null,'monitor-tooltip');tooltip.id='monitor-tooltip';tooltip.role='tooltip';tooltip.hidden=true;
 $('monitor-chart').replaceChildren(svg,tooltip);hidePoint();
}
function hidePoint(){selectedSample=null;$('monitor-marker')?.setAttribute('visibility','hidden');for(const key of ['cpu','disk'])$('monitor-point-'+key)?.setAttribute('visibility','hidden');if($('monitor-tooltip'))$('monitor-tooltip').hidden=true;delete $('monitor-chart').dataset.sampleIndex;}
function showPoint(index,announce=false){
 index=Math.max(0,Math.min(samples.length-1,index));const sample=samples[index];if(!sample)return;
 const tooltip=$('monitor-tooltip'),changed=index!==selectedSample;selectedSample=index;$('monitor-chart').dataset.sampleIndex=String(index);
 const percent=value=>value==null?'未取得':value+'%',speed=value=>value==null?'未取得':value.toFixed(1)+' MB/s',x=40+(plotRight-40)*index/12;
 if(changed||tooltip.hidden){
  const time=node('time',clock(sample.at));time.dateTime=new Date(sample.at).toISOString();tooltip.replaceChildren(time);
  for(const [label,key]of [['整机 CPU','cpu'],['硬盘繁忙度','disk']]){const row=node('div');row.append(node('span',label),node('strong',percent(sample[key])));tooltip.append(row);}
  const io=node('div',null,'monitor-tooltip-io');for(const [label,key]of [['读','read'],['写','write']])io.append(node('span',label+' '+speed(sample[key])));tooltip.append(io);
  const total=sample.cpu==null?null:Math.round(((sample.shared||0)+(sample.listener||0)+sample.console)*10)/10,row=node('div');row.append(node('span','本程序 CPU'),node('strong',percent(total)));tooltip.append(row);
  tooltip.append(node('div',sample.cpu==null?'进程采样未取得':mode()==='stopped'?'仅控制台 '+percent(sample.console):`后台 ${percent(sample.shared)} · 监听 ${percent(sample.listener)} · 控制台 ${percent(sample.console)}`,'monitor-tooltip-process'));
  if(mode()==='running'&&index>=10)tooltip.append(node('p','QQ 正式采集中 · 用量含于共享后台'));else if(!['waiting','stopped','disabled','stale'].includes(mode())&&index>=5&&index<=7)tooltip.append(node('p','待办处理执行期间 · 共用后台'));
 }
 tooltip.hidden=false;const tooltipWidth=tooltip.offsetWidth,space=$('monitor-chart').clientWidth;tooltip.style.left=Math.max(8,Math.min(space-tooltipWidth-8,x>space*.6?x-tooltipWidth-12:x+12))+'px';
 const line=$('monitor-crosshair');line.setAttribute('x1',x);line.setAttribute('x2',x);$('monitor-marker').setAttribute('visibility','visible');for(const key of ['cpu','disk']){const point=$('monitor-point-'+key);point.setAttribute('visibility',sample[key]==null?'hidden':'visible');point.setAttribute('cx',x);if(sample[key]!=null)point.setAttribute('cy',plotBottom-(plotBottom-12)*sample[key]/100);}
 if(announce)$('monitor-announcement').textContent=`${clock(sample.at)}，整机 CPU ${percent(sample.cpu)}，硬盘繁忙度 ${percent(sample.disk)}，读 ${speed(sample.read)}，写 ${speed(sample.write)}。${tooltip.querySelector('.monitor-tooltip-process').textContent}`;
}
function showPointer(event){const rect=$('monitor-chart').getBoundingClientRect();showPoint(Math.round((event.clientX-rect.left-40)/(plotRight-40)*12));}
function renderAttribution(){
 const list=modules();options($('monitor-module'),[{id:'all',name:'全部模块'},...list]);
 const selected=$('monitor-module').value,active=!['waiting','stopped','disabled'].includes(mode()),stale=mode()==='stale',stopped=mode()==='stopped';
 const shared=list.filter(item=>item.shared);
 const rows=[
  {id:'shared',name:'共享后台合计',detail:shared.length+' 个模块共用 · 仅计一次',modules:shared.map(item=>item.id),cpu:active?'16.8%':'0.2%',read:active?'22.4':'0.1',write:active?'7.6':'0.0',note:'进程探针可计量合计，无法拆分模块',inactive:stopped},
  ...shared.map(item=>({id:item.id,name:item.name,modules:[item.id],note:stopped?'后台未运行':'无法单独拆分；已含于共享合计',sharedModule:true,inactive:true})),
  {id:'pi',name:'待办处理 · Pi 子进程',detail:'仅统计已确认归属的子进程',modules:['processor'],note:'当前无独立任务进程',inactive:true},
  {id:'listener',name:'渠道监听',detail:'独立进程 · 按已配置渠道识别',modules:['listener'],cpu:'0.2%',read:'0.0',write:'0.0',note:'独立进程，可单独归属',inactive:stopped},
  {id:'console',name:'控制台与探针',detail:'不包含浏览器进程',modules:['console'],cpu:active?'0.5%':'0.3%',read:active?'0.1':'0.0',write:'0.0',note:'独立进程；探针开销待实测'},
  ...list.filter(item=>sourceModules().some(source=>source.id===item.id&&!source.shared)).map(item=>({id:item.id,name:item.name,detail:'外部管理，未确认本机进程归属',modules:[item.id],note:'未纳入采样，不按 0 处理',inactive:true}))
 ].filter(row=>selected==='all'||row.modules.includes(selected));
 $('monitor-process-asof').textContent=stale?'采样中断 · 当前用量未知':'最新采样 14:32:06 · 共享合计只计一次';
 $('monitor-attribution-note').textContent=selected==='all'?'共享后台只计一次。下表为进程 I/O，含缓存和其他磁盘，不等于上方物理盘读写量。':shared.some(item=>item.id===selected)?`${list.find(item=>item.id===selected).name}与其他模块共用后台；以下数值是共享合计，不代表该模块独占。`:'只展示能确认归属的进程；“—”表示未运行或尚未取得数据。';
 const body=$('monitor-processes');body.replaceChildren();
 for(const row of rows){const tr=node('tr'),name=node('td');name.append(node('strong',row.name));if(row.detail)name.append(node('small',row.detail));tr.dataset.probeGroup=row.id;if(row.sharedModule)tr.dataset.sharedModule='';tr.append(name);for(const key of ['cpu','read','write'])tr.append(node('td',stale||row.inactive?'—':row[key]+(key==='cpu'?'':' MB/s')));tr.append(node('td',stale?'采样已中断，当前值未知':row.inactive&&row.id==='shared'?'后台未运行':row.note));body.append(tr);}
}
function render(){renderSchedules();renderOverviewSummary();const disk=disks.find(item=>item.id===$('monitor-disk').value);renderMetrics(disk);renderChart(disk);renderAttribution();}
window.renderMonitorPreview=render;
$('monitor-scenario').addEventListener('change',()=>window.overviewDashboard.render());
$('monitor-disk').addEventListener('change',render);
$('monitor-module').addEventListener('change',renderAttribution);
const chart=$('monitor-chart');
chart.addEventListener('pointermove',event=>{if(event.pointerType!=='touch'||event.buttons)showPointer(event);});
chart.addEventListener('pointerdown',showPointer);
chart.addEventListener('pointerleave',event=>{if(event.pointerType!=='touch')hidePoint();});
chart.addEventListener('pointercancel',hidePoint);
chart.addEventListener('focus',()=>{if(selectedSample===null&&chart.matches(':focus-visible'))showPoint(12,true);});chart.addEventListener('blur',hidePoint);
chart.addEventListener('keydown',event=>{const keys=['ArrowLeft','ArrowRight','Home','End','Escape'];if(!keys.includes(event.key))return;event.preventDefault();if(event.key==='Escape'){hidePoint();return;}showPoint(event.key==='Home'?0:event.key==='End'?12:(selectedSample??12)+(event.key==='ArrowLeft'?-1:1),true);});
document.addEventListener('pointerdown',event=>{if(!chart.contains(event.target))hidePoint();});
render();
let chartWidth=0;new ResizeObserver(entries=>{const width=entries[0].contentRect.width;if(width>0&&width!==chartWidth){chartWidth=width;renderChart(disks.find(item=>item.id===$('monitor-disk').value));}}).observe($('monitor-chart'));
let rateSize='';new ResizeObserver(entries=>{const {width,height}=entries[0].contentRect,size=width+':'+height;if(width>0&&size!==rateSize){rateSize=size;window.overviewDashboard.renderRate();}}).observe($('rate-chart'));
$('show-metric-definition').addEventListener('click',()=>{const details=$('metric-definition');details.open=true;details.querySelector('summary').focus();details.scrollIntoView({block:'start'});});
})();
