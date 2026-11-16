'use strict';
const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const overviewSnapshot=require('./overview-snapshot.cjs'),runtimeSnapshot=require('./runtime-monitor-snapshot.cjs');
const base=process.env.CONSOLE_TEST_URL||'http://127.0.0.1:18447';
(async()=>{
  const browser=await chromium.launch({executablePath:process.env.CHROME_PATH||'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true});
  try{
    const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[],mutations=[];
    let paused='qq';
    page.on('pageerror',error=>errors.push(error.message));
    page.on('request',request=>{if(request.method()!=='GET')mutations.push(new URL(request.url()).pathname);});
    assert.equal((await(await page.request.get(base+'/__fixture__')).json()).synthetic,true);
    await page.route('**/api/overview?*',route=>{
      const data=overviewSnapshot(Date.now()/1000),name=paused==='qq'?'QQ':'微信';
      data.issues=paused?[{key:'paused-'+paused,kind:'job',module:paused,source_id:paused,status:'paused',title:name+'采集已暂停',error:'RuntimeError',run_id:'previous-failure',run_source:'automation',next_action:'open_config',next_action_label:'连续失败后已暂停，不会自动继续。修复后点击“已解决，恢复重试”。'}]:[];
      for(const job of data.jobs)job.state=job.module===paused?'paused':'idle';
      return route.fulfill({json:data});
    });
    await page.route('**/api/runtime-monitor',route=>{const data=runtimeSnapshot();for(const sample of data.samples)sample.active_jobs=[];return route.fulfill({json:data});});
    await page.route('**/api/operations/jobs/qq/resume',async route=>{
      await new Promise(resolve=>setTimeout(resolve,250));paused=null;return route.fulfill({json:{job:'qq',resumed:true}});
    });
    await page.goto(base+'/#overview');await page.waitForSelector('[data-issue-key="paused-qq"]');
    const heights=()=>page.locator('#services>.service-row').evaluateAll(nodes=>nodes.map(node=>node.getBoundingClientRect().height));
    for(const width of [390,1440,2560]){
      await page.setViewportSize({width,height:1000});paused=null;await page.evaluate(()=>window.initOverview());const idle=await heights();
      paused='qq';await page.evaluate(()=>window.initOverview());const stopped=await heights();
      assert.ok(stopped.every((height,index)=>Math.abs(height-idle[index])<1),'paused row shifted at '+width);
    }
    await page.setViewportSize({width:1440,height:1000});
    assert.match(await page.locator('#attention').textContent(),/QQ采集已暂停.*不会自动继续/);
    const target=page.locator('.automation-job-extra[data-job="qq"]');
    const checkTarget=async name=>{
      await page.waitForSelector(`.automation-job-focus[data-job="${name}"]`);
      assert.equal(await page.locator('.automation-job-focus').count(),1);
      assert.equal(await page.locator(`.automation-job-focus[data-job="${name}"]`).evaluate(node=>document.activeElement===node),true);
      assert.equal(await page.locator('#automation-collection').isVisible(),true);
      assert.equal(await page.locator(`.automation-job-focus[data-job="${name}"]`).getByRole('button',{name:'已解决，恢复重试',exact:true}).isVisible(),true);
      assert.equal(await page.locator('#run-dialog[open]').count(),0);
    };
    await page.locator('[data-issue-key="paused-qq"]').click();await checkTarget('qq');
    assert.equal(new URL(page.url()).hash,'#automation/collection/qq');assert.deepEqual(mutations,[]);
    await page.evaluate(()=>location.hash='overview');await page.locator('#services [data-module="qq"] .schedule-recovery').click();await checkTarget('qq');assert.deepEqual(mutations,[]);
    const resume=target.getByRole('button',{name:'已解决，恢复重试',exact:true});await resume.click();assert.equal(await resume.isDisabled(),true);
    await page.waitForFunction(()=>document.getElementById('ops-notice').textContent.includes('等待下一轮运行结果'));
    assert.deepEqual(mutations,['/api/operations/jobs/qq/resume']);
    await page.evaluate(()=>location.hash='overview');await page.waitForFunction(()=>!document.querySelector('[data-issue-key]'));
    paused='wechat';await page.evaluate(()=>window.initOverview());await page.locator('[data-issue-key="paused-wechat"]').click();await checkTarget('wechat');
    assert.equal(new URL(page.url()).hash,'#automation/collection/wechat');
    await page.reload();await checkTarget('wechat');
    for(const width of [390,1440,2560]){await page.setViewportSize({width,height:1000});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'overflow '+width);}
    await page.evaluate(()=>location.hash='automation/collection/removed-task');await page.waitForFunction(()=>document.getElementById('ops-notice').textContent.includes('该任务已移除'));
    assert.equal(await page.locator('.automation-job-focus').count(),0);
    await page.evaluate(()=>location.hash='automation/collection/%E0%A4%A');await page.waitForFunction(()=>document.getElementById('ops-notice').textContent.includes('任务链接无效'));
    assert.deepEqual(mutations,['/api/operations/jobs/qq/resume']);assert.deepEqual(errors,[]);
    console.log('PASS issue navigation: named paused source, overview/status links, expanded exact automation task, reloadable route, recovery only at destination, single scoped POST, stale links, responsive layout');
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
