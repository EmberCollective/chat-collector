'use strict';
const path=require('node:path'),assert=require('node:assert/strict'),fs=require('node:fs');
const {pathToFileURL}=require('node:url');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
(async()=>{
  const browser=await chromium.launch({executablePath:process.env.CHROME_PATH||'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true});
  const page=await browser.newPage({viewport:{width:1600,height:1100}}),errors=[],requests=[];
  page.on('pageerror',error=>errors.push(error.message));page.on('request',r=>{if(/^https?:/.test(r.url()))requests.push(r.url());});
  try{
    await page.goto(pathToFileURL(path.join(__dirname,'index.html')).href);
    assert.match(await page.locator('#kpis').textContent(),/98.6%/);assert.equal(await page.locator('.problem-row').count(),2);
    await page.locator('#resource-storage').click();assert.equal(await page.locator('#resource-total').textContent(),'14.0 GB');
    assert.match(await page.locator('#resource-legend').textContent(),/共享/);
    await page.locator('#resource-messages').click();assert.equal(await page.locator('#resource-total').textContent(),'1,284');
    fs.mkdirSync(path.join(__dirname,'previews'),{recursive:true});await page.evaluate(()=>scrollTo(0,0));await page.screenshot({path:path.join(__dirname,'previews/desktop.png'),fullPage:true});
    await page.locator('.resource-panel').screenshot({path:path.join(__dirname,'previews/resources.png')});
    assert.equal(await page.locator('nav a[href="../console-redesign/index.html#automation"]').count(),1);
    // Every error surface reaches the same concrete record, with its scope intact.
    for(const selector of ['.problem-row button','#rate-chart [role="button"]','.run-ticks .failed','[data-service-problem="collect"]','[data-source-problem="work-chat"]']){
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
    await page.reload();
    for(const scenario of ['healthy','empty','issues']){
      await page.locator('#scenario').selectOption(scenario);
      if(scenario==='empty'){assert.match(await page.locator('#kpis').textContent(),/暂无有效样本/);assert.equal(await page.locator('#rate-chart circle').count(),0);}
      for(const width of [320,390,800,1024,1280,1440,1600]){
        await page.setViewportSize({width,height:1000});
        for(const metric of ['storage','messages']){await page.locator('#resource-'+metric).click();assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),`overflow ${width}/${scenario}/${metric}`);}
      }
    }
    await page.setViewportSize({width:390,height:844});await page.screenshot({path:path.join(__dirname,'previews/mobile.png'),fullPage:true});
    await page.locator('.problem-row button').last().click();assert.equal(await page.locator('#run-dialog').evaluate(n=>n.open),true);await page.locator('#verify-delivery').click();
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'detail overflows on mobile');
    await page.keyboard.press('Escape');
    await page.setViewportSize({width:1440,height:1000});
    await page.locator('nav a[href="../console-redesign/index.html#automation"]').click();
    assert.equal(await page.locator('#view-automation').isVisible(),true);assert.equal(await page.locator('#view-automation [role="tab"]').count(),3);
    assert.deepEqual(errors,[]);assert.deepEqual(requests,[]);
    console.log('PASS overview: all error entry points, scoped details and return, safe recovery outcomes, preserved historical metrics, scenarios and resource switch, keyboard and 320–1600px, no network/errors');
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
