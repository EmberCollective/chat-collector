'use strict';
const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const makeSnapshot=require('./overview-snapshot.cjs');
const base=process.env.CONSOLE_TEST_URL||'http://127.0.0.1:18447';
(async()=>{
  assert.equal(new URL(base).hostname,'127.0.0.1');
  const browser=await chromium.launch({executablePath:process.env.CHROME_PATH||'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true});
  const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[],mutations=[];
  page.on('pageerror',error=>errors.push(error.message));page.on('request',request=>{if(request.method()!=='GET')mutations.push(request.url());});
  try{
    assert.equal((await (await page.request.get(base+'/__fixture__')).json()).synthetic,true,'Only use an isolated fixture');
    const session=await(await page.request.get(base+'/api/session')).json();
    const response=await page.request.get(base+'/api/overview?hours=24',{headers:{'X-Console-Token':session.token}});assert.equal(response.ok(),true);
    const actual=await response.json();assert.equal(actual.window.hours,24);assert.equal(actual.sources.length,4);
    assert.equal(actual.sources.find(source=>source.id==='external-example').metrics_available,false);
    await page.goto(base+'/#overview');await page.waitForSelector('#collection-table table');
    assert.equal(await page.locator('#kpis .metric').count(),3);assert.doesNotMatch(await page.locator('#overview-coverage').textContent(),/\[object Object\]/);
    assert.match(await page.locator('#collection-table').textContent(),/工作账号/);
    let data=makeSnapshot(Date.now()/1000),fail=false;
    await page.route('**/api/overview?*',route=>route.fulfill({status:fail?503:200,json:fail?{detail:'fixture metrics unavailable'}:data}));
    await page.goto(base+'/#overview');await page.waitForFunction(()=>document.getElementById('kpis').textContent.includes('1,284'));
    assert.match(await page.locator('#kpis').textContent(),/69 成功 \/ 70/);
    assert.match(await page.locator('#collection-table').textContent(),/未提供统计/);
    assert.equal(await page.locator('#rate-chart [role=button]').count(),1);
    await page.locator('#resource-storage').click();assert.match(await page.locator('#resource-total').textContent(),/14.0 GB/);
    assert.match(await page.locator('#resource-scope').textContent(),/逻辑大小/);
    await page.locator('[data-source-problem=qq]').click();await page.waitForSelector('#related-runs [data-run-id]');
    assert.equal(await page.locator('#run-result-filter').inputValue(),'failed');
    await page.locator('#related-runs [data-run-id]').first().click();await page.waitForSelector('#run-detail .run-facts');
    assert.match(await page.locator('#run-detail').textContent(),/fixture database busy/);
    await page.getByRole('button',{name:'打开相关配置',exact:true}).click();
    await page.waitForFunction(()=>location.hash==='#settings/source'&&document.getElementById('module').value==='qq');
    await page.locator('#overview-return-run').click();await page.waitForSelector('#run-detail .run-facts');
    assert.equal(await page.locator('#run-result-filter').inputValue(),'failed');
    await page.locator('#back-to-runs').click();await page.waitForSelector('#related-runs [data-run-id]');
    await page.locator('#show-history').click();await page.waitForFunction(()=>location.hash==='#history');
    assert.equal(await page.locator('#history-module').inputValue(),'qq');assert.equal(await page.locator('#history-action').inputValue(),'collect');assert.equal(await page.locator('#history-filter').inputValue(),'failed');
    // Recovery must verify an unknown delivery, persist through the existing operation, and never treat queueing as delivery.
    const operations=await page.evaluate(()=>api('/operations/status'));
    operations.messaging={...operations.messaging,deliveries:[{id:'fixture-unknown',status:'unknown',target_id:'work-group',title:'报告提交',error:'确认超时'}]};
    await page.route('**/api/operations/status',route=>route.fulfill({json:operations}));
    await page.route('**/api/operations/deliveries/fixture-unknown/delivered',async route=>{await new Promise(resolve=>setTimeout(resolve,250));data.issues=data.issues.filter(issue=>issue.key!=='delivery-unknown');operations.messaging.deliveries[0].status='sent';return route.fulfill({json:{status:'sent'}});});
    await page.evaluate(()=>location.hash='overview');await page.locator('[data-issue-key=delivery-unknown]').click();await page.waitForSelector('#delivery-checked');
    const received=page.getByRole('button',{name:'确认已收到',exact:true});assert.equal(await received.isEnabled(),false);
    await page.locator('#delivery-checked').check();await received.click();
    assert.equal(await page.getByRole('button',{name:'核实未达后重试',exact:true}).isEnabled(),false);assert.equal(await page.locator('#delivery-checked').isEnabled(),false);
    await page.waitForFunction(()=>document.getElementById('run-detail').textContent.includes('已保存人工送达确认'));
    await page.locator('#close-dialog').click();assert.equal(await page.locator('[data-issue-key=delivery-unknown]').count(),0);
    assert.deepEqual(mutations,[base+'/api/operations/deliveries/fixture-unknown/delivered']);
    data.issues.push(makeSnapshot().issues[1]);operations.messaging.deliveries[0].status='unknown';
    await page.locator('#overview-hours').selectOption('48');await page.waitForSelector('[data-issue-key=delivery-unknown]');
    await page.locator('[data-issue-key=delivery-unknown]').click();await page.locator('#delivery-checked').check();await received.click();await page.locator('#close-dialog').click();
    await page.evaluate(()=>openOverviewJob('00000000000000000000000000000001','console',{module:'qq',action:'collect'}));
    await page.waitForTimeout(350);assert.doesNotMatch(await page.locator('#run-detail').textContent(),/已保存人工送达确认/);await page.locator('#close-dialog').click();
    // A deleted source must not borrow the current source or a processor schedule.
    await page.route('**/api/activity/automation/removed-source',route=>route.fulfill({json:{id:'removed-source',source:'automation',module:'removed-account',action:'collect',status:'failed',started_at:1,finished_at:2,attempts:[]}}));
    await page.evaluate(()=>openOverviewJob('removed-source','automation',{label:'历史来源'}));
    assert.equal(await page.getByRole('button',{name:'安排新一轮…',exact:true}).count(),0);await page.locator('#close-dialog').click();
    await page.unroute('**/api/operations/status');
    // Break the success line across missing samples; never join through a missing interval.
    data.success_bins[4].success_rate=null;data.sources[0].name='<img src=x onerror=alert(1)> '+('长名称'.repeat(30));data.sources[0].emitted=null;data.summary.emitted_complete=false;
    await page.locator('#overview-hours').selectOption('24');await page.waitForFunction(()=>document.querySelectorAll('#rate-chart polyline').length===2);
    assert.equal(await page.locator('#collection-table img').count(),0);
    assert.match(await page.locator('#collection-table').textContent(),/<img src=x/);
    for(const width of [320,390,800,1024,1440,1600]){await page.setViewportSize({width,height:844});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'overflow '+width);}
    fail=true;await page.locator('#overview-hours').selectOption('48');await page.waitForFunction(()=>document.getElementById('last-refresh').textContent.includes('数据已过期'));
    assert.match(await page.locator('#attention').textContent(),/无法确认/);assert.match(await page.locator('#kpis').textContent(),/1,284/);
    fail=false;data=makeSnapshot();data.summary={emitted:null,duplicates:null,valid_count:0,success_rate:null};data.issues=[];data.success_bins=[];data.sources=[];data.resources={complete:false,sources:[],shared:{logical_bytes:null}};
    await page.getByRole('button',{name:'重新读取',exact:true}).click();await page.waitForFunction(()=>document.getElementById('attention').textContent.includes('等待首次'));
    await page.locator('#resource-storage').click();assert.equal(await page.locator('#resource-total').textContent(),'—');
    assert.equal(mutations.length,2);assert.deepEqual(errors,[]);
    console.log('PASS: overview real detail/config/return/history scope, missing samples, unknown metrics, stale response, safe text and responsive layout');
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
