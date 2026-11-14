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
    for(const scenario of ['healthy','empty','issues']){
      await page.locator('#scenario').selectOption(scenario);
      if(scenario==='empty'){assert.match(await page.locator('#kpis').textContent(),/暂无有效样本/);assert.equal(await page.locator('#rate-chart circle').count(),0);}
      for(const width of [320,390,800,1024,1280,1440,1600]){
        await page.setViewportSize({width,height:1000});
        for(const metric of ['storage','messages']){await page.locator('#resource-'+metric).click();assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),`overflow ${width}/${scenario}/${metric}`);}
      }
    }
    await page.setViewportSize({width:390,height:844});await page.screenshot({path:path.join(__dirname,'previews/mobile.png'),fullPage:true});
    await page.locator('.problem-row button').first().click();assert.equal(await page.locator('#run-dialog').evaluate(n=>n.open),true);await page.keyboard.press('Escape');
    assert.deepEqual(errors,[]);assert.deepEqual(requests,[]);
    console.log('PASS overview: scenarios, metric formulas, pie switch, empty data, dialog, 320–1600px, no network/errors');
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
