'use strict';
const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const overviewSnapshot=require('./overview-snapshot.cjs'),runtimeSnapshot=require('./runtime-monitor-snapshot.cjs');
const base=process.env.CONSOLE_TEST_URL||'http://127.0.0.1:18447';
(async()=>{
  const browser=await chromium.launch({executablePath:process.env.CHROME_PATH||'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true});
  try{
    const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[],reads=[];
    let active='qq',manual=false;
    let started=Date.now()/1000-72;
    page.on('pageerror',error=>errors.push(error.message));
    page.on('request',request=>{if(new URL(request.url()).pathname.startsWith('/api/'))reads.push(request);});
    assert.equal((await(await page.request.get(base+'/__fixture__')).json()).synthetic,true);
    await page.route('**/api/overview?*',route=>{
      const data=overviewSnapshot(Date.now()/1000);data.issues=[];
      if(manual){data.service.running=false;data.service.state='stopped';}
      for(const job of data.jobs){job.state=job.module===active?'running':'idle';job.last_start=started;job.next_run=job.state==='running'?null:Date.now()/1000+1800;if(manual&&job.module===active){job.enabled=false;job.manual_run_id='manual-fixture';}}
      return route.fulfill({json:data});
    });
    await page.route('**/api/runtime-monitor',route=>{
      const data=runtimeSnapshot();
      for(const sample of data.samples)sample.active_jobs=active?[{module:active,source_id:['qq','wechat'].includes(active)?active:null,name:active,started_at:started}]:[];
      return route.fulfill({json:data});
    });
    await page.goto(base+'/#overview');
    await page.waitForSelector('#services [data-module="qq"].is-running');
    assert.equal(await page.locator('#services [data-module="wechat"].is-running').count(),0);
    assert.match(await page.locator('#services [data-module="qq"]').textContent(),/QQ执行中已运行 1 分.*开始/);
    const before=await page.locator('#services [data-module="qq"] .schedule-elapsed').textContent(),overviewReads=reads.filter(r=>new URL(r.url()).pathname==='/api/overview').length;
    await page.waitForFunction(previous=>document.querySelector('#services [data-module="qq"] .schedule-elapsed').textContent!==previous,before,{timeout:2500});
    assert.equal(reads.filter(r=>new URL(r.url()).pathname==='/api/overview').length,overviewReads,'elapsed ticks must not fetch history');
    await page.locator('#services [data-module="qq"] button').click();await page.waitForSelector('#run-dialog[open]');
    await page.waitForFunction(()=>document.getElementById('run-scope').textContent.includes('QQ'));
    assert.ok(reads.some(r=>{const url=new URL(r.url());return url.pathname==='/api/jobs'&&url.searchParams.get('module')==='qq'&&url.searchParams.get('status')==='running';}));
    await page.locator('#close-dialog').click();
    active='wechat';
    await page.waitForSelector('#services [data-module="wechat"].is-running',{timeout:8000});
    assert.equal(await page.locator('#services [data-module="qq"].is-running').count(),0);
    await page.waitForSelector('#services [data-module="qq"] time');
    active=null;
    await page.waitForFunction(()=>!document.querySelector('#services .schedule-entry.is-running'),null,{timeout:8000});
    await page.waitForSelector('#services [data-module="wechat"] time');
    manual=true;active='qq';
    await page.waitForSelector('#services [data-module="qq"].is-running',{timeout:8000});
    assert.match(await page.locator('#services [data-module="qq"]').textContent(),/已运行/);
    manual=false;
    const geometry=()=>page.evaluate(()=>{
      const rect=node=>{const box=node.getBoundingClientRect();return {top:box.top+scrollY,height:box.height};};
      return {rows:[...document.querySelectorAll('#services>.service-row')].map(rect),health:rect(document.getElementById('overview-health')),next:rect(document.getElementById('runtime-monitor')),scrollY};
    });
    const refresh=()=>page.evaluate(async()=>{window.setRuntimeSnapshot(await api('/runtime-monitor'));await window.initOverview();});
    for(const width of [390,1440,2560]){
      await page.setViewportSize({width,height:1000});active=null;await refresh();
      await page.locator('#runtime-monitor').scrollIntoViewIfNeeded();
      const idle=await geometry();
      for(const module of ['qq','wechat','processor','messaging','feedback',null]){
        active=module;started=Date.now()/1000-3661;await refresh();
        const current=await geometry();
        for(let i=0;i<idle.rows.length;i++)for(const key of ['top','height'])assert.ok(Math.abs(current.rows[i][key]-idle.rows[i][key])<1,`${width}px ${module}: service ${i} ${key} shifted ${idle.rows[i][key]} -> ${current.rows[i][key]}`);
        for(const key of ['top','height'])assert.ok(Math.abs(current.health[key]-idle.health[key])<1,`${width}px ${module}: health ${key} shifted`);
        assert.ok(Math.abs(current.next.top-idle.next.top)<1,`${width}px ${module}: following panel shifted`);
        assert.ok(Math.abs(current.scrollY-idle.scrollY)<1,`${width}px ${module}: page scrolled during refresh`);
        assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),`${width}px ${module}: horizontal overflow`);
      }
    }
    for(const width of [390,1440]){await page.setViewportSize({width,height:1000});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));}
    assert.deepEqual(errors,[]);assert.ok(reads.every(request=>request.method()==='GET'));
    console.log('PASS live schedules: per-source execution, local elapsed, task transitions, next plan, record link, manual task, stable row/panel/scroll geometry across all five modules at 390/1440/2560px');
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
