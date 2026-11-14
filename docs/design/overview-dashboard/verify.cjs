'use strict';
const path=require('node:path'),assert=require('node:assert/strict'),fs=require('node:fs');
const {pathToFileURL}=require('node:url');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
require('node:child_process').execFileSync(process.execPath,[path.join(__dirname,'build.cjs'),'--check'],{stdio:'inherit'});
(async()=>{
  const browser=await chromium.launch({executablePath:process.env.CHROME_PATH||'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true});
  const page=await browser.newPage({viewport:{width:1600,height:1100}}),errors=[],requests=[];
  page.on('pageerror',error=>errors.push(error.message));page.on('request',r=>{if(/^https?:/.test(r.url()))requests.push(r.url());});
  try{
    await page.goto(pathToFileURL(path.join(__dirname,'index.html')).href);
    assert.match(await page.locator('#kpis').textContent(),/98.6%/);assert.equal(await page.locator('.problem-row').count(),2);
    const duplicateIds=await page.evaluate(()=>{const ids=[...document.querySelectorAll('[id]')].map(node=>node.id);return ids.filter((id,index)=>ids.indexOf(id)!==index);});
    assert.deepEqual(duplicateIds,[],'shared overview introduced duplicate IDs');
    await page.locator('#resource-storage').click();assert.equal(await page.locator('#resource-total').textContent(),'14.0 GB');
    assert.match(await page.locator('#resource-legend').textContent(),/共享/);
    await page.locator('#resource-messages').click();assert.equal(await page.locator('#resource-total').textContent(),'1,284');
    fs.mkdirSync(path.join(__dirname,'previews'),{recursive:true});await page.evaluate(()=>scrollTo(0,0));await page.screenshot({path:path.join(__dirname,'previews/desktop.png'),fullPage:true});
    await page.locator('.resource-panel').screenshot({path:path.join(__dirname,'previews/resources.png')});
    assert.equal(await page.locator('.nav-item[data-view="automation"]').count(),1);
    const enabledBefore=await page.locator('#services .service-row').first().textContent();
    await page.evaluate(()=>openAutomation());
    await page.locator('[data-background-source="qq-main"]').uncheck();
    await page.locator('[data-action="service-save"]').click();await page.locator('#dialog-confirm').click();
    await page.locator('.nav-item[data-view="overview"]').click();
    assert.equal(await page.locator('#services .service-row').first().textContent(),enabledBefore,'unwritten draft changed reported runtime');
    await page.locator('#services [data-service-id="process"] button').click();
    assert.equal(await page.locator('#view-process').isVisible(),true);
    assert.equal(await page.locator('#workflow-run-picker').inputValue(),await page.evaluate(()=>workflowRuns.find(run=>run.status==='running').id));
    assert.match(await page.locator('#workflow-wait').textContent(),/等待模型/);
    await page.locator('.nav-item[data-view="overview"]').click();
    // Every error surface reaches the same concrete record, with its scope intact.
    for(const selector of ['.problem-row button','#rate-chart [role="button"]','.run-ticks .failed','[data-service-problem="collect"]','[data-source-problem="qq-main"]']){
      await page.locator(selector).first().click();
      assert.equal(await page.locator('#run-detail').isVisible(),true,selector);
      const detail=await page.locator('#run-detail').textContent();
      for(const text of ['操作','模块','触发方式','开始时间','耗时','错误与当前结果','本轮上下文','查看日志与原始记录','断点'])assert.ok(detail.includes(text),`${selector}: ${text}`);
      const scope=await page.locator('#run-scope').textContent();
      await page.locator('#back-to-runs').click();
      assert.equal(await page.locator('#run-scope').textContent(),scope);
      if(selector.includes('rate-chart'))assert.equal(await page.locator('#run-result-filter').inputValue(),'failed');
      await page.locator('[data-run-id="collect"]').click();
      assert.equal(await page.locator('#run-scope').textContent(),scope);
      await page.keyboard.press('Escape');
    }
    await page.locator('#rate-chart [role="button"]').focus();await page.keyboard.press('Enter');
    assert.equal(await page.locator('#run-detail').isVisible(),true);
    await page.keyboard.press('Escape');
    await page.locator('#show-records').click();
    await page.locator('#run-result-filter').selectOption('failed');
    assert.equal(await page.locator('#related-runs [data-run-id]').count(),1);
    await page.locator('[data-run-id="collect"]').click();
    await page.locator('#back-to-runs').click();
    assert.equal(await page.locator('#run-result-filter').inputValue(),'failed');
    await page.keyboard.press('Escape');
    // Unknown delivery is never offered a direct retry before the user verifies it.
    await page.locator('[data-service-problem="delivery"]').click();
    assert.equal(await page.locator('#confirm-delivery-retry').count(),0);
    await page.locator('#verify-delivery').click();
    assert.equal(await page.locator('#confirm-received').isDisabled(),true);
    assert.equal(await page.locator('#confirm-delivery-retry').isDisabled(),true);
    await page.locator('#delivery-checked').check();
    await page.screenshot({path:path.join(__dirname,'previews/run-detail.png')});
    await page.locator('#confirm-received').click();
    assert.match(await page.locator('#run-detail').textContent(),/已送达（人工核实 · 演示）/);
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('.problem-row').count(),1);
    assert.match(await page.locator('#kpis .metric').first().textContent(),/1 项/);
    assert.equal(await page.locator('[data-service-problem="delivery"]').count(),0);
    // A fresh example exercises the other outcome without claiming successful delivery.
    await page.reload();
    await page.locator('.problem-row button').last().click();await page.locator('#verify-delivery').click();
    await page.locator('#delivery-checked').check();await page.locator('#confirm-delivery-retry').click();
    assert.match(await page.locator('#run-detail').textContent(),/等待重试（演示）/);
    assert.match(await page.locator('#run-detail').textContent(),/尚未送达/);
    assert.equal(await page.locator('#confirm-delivery-retry').count(),0,'cannot queue the same retry twice');
    await page.keyboard.press('Escape');
    await page.locator('.problem-row button').first().click();await page.locator('#retry-collection').click();
    await page.locator('#confirm-collection-retry').click();
    assert.match(await page.locator('#run-detail').textContent(),/失败 · 已安排新一轮/);
    await page.keyboard.press('Escape');
    assert.match(await page.locator('#kpis').textContent(),/98.6%/,'recovery rewrote historical success rate');
    // The integrated console keeps source context, result filters and recovery state.
    await page.reload();
    await page.locator('[data-source-problem="qq-main"]').click();
    const originScope=await page.locator('#run-scope').textContent();
    await page.getByRole('button',{name:'打开相关配置',exact:true}).click();
    assert.equal(await page.locator('#view-settings').isVisible(),true);
    assert.equal(await page.locator('[aria-label="设置信息源"]').inputValue(),'qq-main');
    await page.locator('#overview-return-run').click();
    assert.equal(await page.locator('#run-detail').isVisible(),true);
    assert.equal(await page.locator('#run-scope').textContent(),originScope);
    await page.locator('#show-history').click();
    assert.equal(await page.locator('#view-history').isVisible(),true);
    assert.match(await page.locator('#history-scope').textContent(),/QQ.*失败记录/);
    assert.equal(await page.locator('#history-rows [data-dashboard-run]').count(),1);
    await page.locator('#history-rows [data-dashboard-run]').click();
    assert.equal(await page.locator('#run-scope').textContent(),originScope);
    await page.keyboard.press('Escape');
    await page.locator('#history-clear-scope').click();
    assert.equal(await page.locator('#history-rows [data-dashboard-run]').count(),4);
    await page.locator('#history-filter').selectOption('succeeded');
    await page.locator('#history-rows [data-dashboard-run]').first().click();
    await page.locator('#back-to-runs').click();
    assert.equal(await page.locator('#run-result-filter').inputValue(),'succeeded');
    await page.locator('#show-history').click();
    assert.equal(await page.locator('#history-filter').inputValue(),'succeeded');
    await page.locator('.nav-item[data-view="overview"]').click();
    await page.locator('[data-service-problem="delivery"]').click();
    await page.getByRole('button',{name:'打开相关配置',exact:true}).click();
    assert.equal(await page.locator('#automation-channels').isVisible(),true);
    await page.locator('#overview-return-run').click();
    await page.locator('#verify-delivery').click();await page.locator('#delivery-checked').check();await page.locator('#confirm-received').click();
    await page.keyboard.press('Escape');
    await page.evaluate(()=>{navigate('delivery');activateTab(document.getElementById('tab-deliveries'));});
    assert.match(await page.locator('#delivery-rows tr').first().textContent(),/已确认送达/);
    assert.equal(await page.evaluate(()=>catalog.deliveries[0].status),'sent');
    await page.locator('.nav-item[data-view="overview"]').click();
    for(const scenario of ['healthy','empty']){
      await page.locator('#scenario').selectOption(scenario);
      assert.equal(await page.locator('#scenario-scope').isVisible(),true);
      assert.match(await page.locator('#scenario-scope').textContent(),/仅改变本页指标示例/);
      assert.equal(await page.evaluate(()=>catalog.deliveries[0].status),'sent','scenario replaced user verification');
    }
    await page.locator('#scenario').selectOption('issues');
    assert.equal(await page.locator('.problem-row').count(),1,'scenario return revived a verified delivery');
    await page.reload();
    await page.evaluate(()=>{navigate('delivery');activateTab(document.getElementById('tab-deliveries'));});
    await page.locator('[data-action="delivery-retry"]').click();await page.locator('#dialog-confirm').click();
    await page.locator('.nav-item[data-view="overview"]').click();
    assert.equal(await page.locator('.problem-row').count(),1);
    await page.locator('.nav-item[data-view="history"]').click();
    assert.match(await page.locator('#history-rows').textContent(),/等待重试（演示）/);
    await page.reload();
    await page.evaluate(()=>{catalog.sources.push({id:'new-feed-v2',kind:'new-kind',name:'新订阅来源',capabilities:{},ui:{}});renderOverview();});
    assert.match(await page.locator('#collection-table').textContent(),/新订阅来源.*new-feed-v2/);
    assert.match(await page.locator('#resource-legend').textContent(),/新订阅来源未统计不按 0 计算/);
    assert.match(await page.locator('#kpis').textContent(),/98.6%/);
    await page.reload();
    for(const scenario of ['healthy','empty','issues']){
      await page.locator('#scenario').selectOption(scenario);
      if(scenario==='empty'){assert.match(await page.locator('#kpis').textContent(),/暂无有效样本/);assert.equal(await page.locator('#rate-chart circle').count(),0);}
      for(const width of [320,390,800,1024,1280,1440,1600]){
        await page.setViewportSize({width,height:1000});
        for(const metric of ['storage','messages']){await page.locator('#resource-'+metric).click();assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),`overflow ${width}/${scenario}/${metric}`);}
      }
    }
    await page.setViewportSize({width:390,height:844});await page.evaluate(()=>{document.activeElement.blur();scrollTo(0,0);});await page.mouse.move(0,0);await page.screenshot({path:path.join(__dirname,'previews/mobile.png'),fullPage:true});await page.screenshot({path:path.join(__dirname,'previews/mobile-first-screen.png')});
    await page.locator('.problem-row button').last().click();assert.equal(await page.locator('#run-dialog').evaluate(n=>n.open),true);await page.locator('#verify-delivery').click();
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'detail overflows on mobile');
    await page.keyboard.press('Escape');
    await page.setViewportSize({width:1440,height:1000});
    await page.locator('.nav-item[data-view="automation"]').click();
    assert.equal(await page.locator('#view-automation').isVisible(),true);assert.equal(await page.locator('#view-automation [role="tab"]').count(),3);
    assert.deepEqual(errors,[]);assert.deepEqual(requests,[]);
    console.log('PASS overview: shared source, all error entry points, scoped configuration/history return, cross-view recovery, unknown source metrics, preserved denominators, scenarios and resources, keyboard and 320–1600px, no network/errors');
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
