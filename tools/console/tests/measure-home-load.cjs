'use strict';
// Read-only timing: real data stays in the browser; the report retains only timings and counts.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const base=process.env.CONSOLE_TEST_URL||'http://127.0.0.1:18447';
assert.equal(new URL(base).hostname,'127.0.0.1');
const output=path.resolve(process.env.CONSOLE_LOAD_REPORT||'.runtime/runtime-monitoring-evidence/home-load.json');
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CHROME_PATH||'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true});
 const context=await browser.newContext({viewport:{width:1440,height:1000},serviceWorkers:'block'}),results=[];
 await context.route('**/*',route=>{
  const request=route.request(),url=new URL(request.url());
  if(request.method()!=='GET'||url.origin!==new URL(base).origin)return route.abort('blockedbyclient');
  return route.continue();
 });
 await context.addInitScript(()=>{
  window.__loadLongTasks=[];
  try{new PerformanceObserver(list=>{for(const item of list.getEntries())window.__loadLongTasks.push(Math.round(item.duration));}).observe({type:'longtask',buffered:true});}catch{}
 });
 const page=await context.newPage(),errors=[];
 page.on('pageerror',error=>errors.push(error.message));
 try{
  const identity=await(await page.request.get(base+'/api/session')).json();
  assert.equal(identity.application,'chat-collector-console');
  for(let index=0;index<5;index++){
   const calls=[],pending=new Map(),started=Date.now();
   const onStart=request=>pending.set(request,Date.now());
   const onEnd=request=>{const url=new URL(request.url());if(url.pathname.startsWith('/api/'))calls.push({path:url.pathname.replace(/[a-f0-9]{32}/g,'<id>'),start_ms:pending.get(request)-started,duration_ms:Date.now()-pending.get(request)});};
   page.on('request',onStart);page.on('requestfinished',onEnd);
   if(index===0)await page.goto(base+'/#overview',{waitUntil:'domcontentloaded'});else await page.reload({waitUntil:'domcontentloaded'});
   await page.waitForFunction(()=>!document.getElementById('view-overview').hidden&&document.querySelectorAll('#kpis .metric').length===3&&document.querySelector('#rate-chart svg')&&document.querySelector('#resource-chart svg')&&document.querySelector('#resource-legend table')&&document.querySelector('#collection-table tbody tr')&&document.querySelectorAll('#services .service-row').length>=4&&document.querySelectorAll('#monitor-metrics .monitor-metric').length===3&&document.getElementById('last-refresh').textContent.startsWith('刷新于'),null,{timeout:20000,polling:50});
   await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
   const core_ms=Date.now()-started;
   await page.waitForFunction(()=>document.querySelector('#monitor-metrics strong')?.textContent!=='—',null,{timeout:15000,polling:50});
   await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
   const metrics=await page.evaluate(()=>({paints:performance.getEntriesByType('paint').map(item=>({name:item.name,ms:Math.round(item.startTime)})),long_tasks_ms:window.__loadLongTasks,connection:document.getElementById('connection').textContent}));
   page.off('request',onStart);page.off('requestfinished',onEnd);
   assert.equal(calls.filter(call=>call.path.startsWith('/api/workflow')).length,0,'homepage loaded hidden Pi history');
   assert.equal(calls.filter(call=>call.path==='/api/session').length,1,'homepage repeated its session request');
   results.push({kind:index===0?'first_navigation':'reload',core_ms,with_cpu_sample_ms:Date.now()-started,api_calls:calls,paints:metrics.paints,long_tasks_ms:metrics.long_tasks_ms});
  }
  assert.deepEqual(errors,[]);
  fs.mkdirSync(path.dirname(output),{recursive:true});fs.writeFileSync(output,JSON.stringify({at:new Date().toISOString(),base,read_only:true,service_restarted:false,viewport:'1440x1000',results,errors},null,2));
  console.log(JSON.stringify(results.map(({kind,core_ms,with_cpu_sample_ms,api_calls,long_tasks_ms})=>({kind,core_ms,with_cpu_sample_ms,requests:api_calls.length,long_tasks_ms})),null,2));
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
