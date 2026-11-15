'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const base=process.env.CONSOLE_TEST_URL||'http://127.0.0.1:18447';
(async()=>{
  const browser=await chromium.launch({executablePath:process.env.CHROME_PATH||'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true});
  try{
    const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[],results=[];
    page.on('pageerror',error=>errors.push(error.message));
    assert.equal((await(await page.request.get(base+'/__fixture__')).json()).synthetic,true);
    await page.goto(base+'/#sources');await page.waitForSelector('#rule-list .rule-card');
    for(const [width,height] of [[390,844],[1440,1000],[1920,1080],[2560,1440]]){
      await page.setViewportSize({width,height});await page.locator('#rules-save').scrollIntoViewIfNeeded();
      for(const fraction of [0,.5,1]){
        await page.locator('.filter-scroll').evaluate((node,fraction)=>node.scrollTop=(node.scrollHeight-node.clientHeight)*fraction,fraction);
        const measured=await page.evaluate(()=>{
          const save=document.getElementById('rules-save'),footer=document.querySelector('.filter-footer'),scroll=document.querySelector('.filter-scroll'),a=save.getBoundingClientRect(),b=footer.getBoundingClientRect(),c=scroll.getBoundingClientRect();
          return {inFooter:footer.contains(save),overlap:c.bottom-b.top,hit:save.contains(document.elementFromPoint(a.x+a.width/2,a.y+a.height/2)),root:parseFloat(getComputedStyle(document.documentElement).fontSize),message:parseFloat(getComputedStyle(document.querySelector('.message-text')||document.body).fontSize),main:document.querySelector('main').getBoundingClientRect().width,buttonHeight:a.height,overflow:document.documentElement.scrollWidth>innerWidth+1};
        });
        assert.ok(measured.inFooter&&measured.hit,'save action is obscured at '+width);
        assert.ok(measured.overlap<=1,'scroll content crosses the footer at '+width);
        assert.equal(measured.overflow,false,'horizontal overflow at '+width);
        assert.equal(measured.root,width>=1920?16:14);
        if(width===2560){assert.ok(measured.main>2200);assert.ok(measured.buttonHeight>=40);}
        if(fraction===1)results.push({width,height,...measured});
      }
    }
    let confirmations=0;page.once('dialog',dialog=>{confirmations++;dialog.dismiss();});await page.locator('#rules-save').click();assert.equal(confirmations,1);
    await page.evaluate(()=>location.hash='automation/rules');await page.waitForSelector('#automation-rules-slot #rules-actions');assert.equal(await page.locator('#rules-save').count(),1);
    await page.evaluate(()=>location.hash='sources');await page.waitForSelector('.filter-footer #rules-save');
    assert.equal(await page.locator('#rules-save').count(),1);
    if(process.env.CONSOLE_SCREENSHOTS){fs.mkdirSync(process.env.CONSOLE_SCREENSHOTS,{recursive:true});await page.evaluate(()=>scrollTo(0,0));await page.screenshot({path:path.join(process.env.CONSOLE_SCREENSHOTS,'sources-2k.png'),fullPage:true});}
    for(const view of ['overview','processor','delivery','automation','history','settings']){
      await page.evaluate(view=>location.hash=view,view);await page.waitForFunction(view=>!document.getElementById('view-'+view).hidden,view);
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'2K overflow: '+view);
    }
    const hidpi=await browser.newPage({viewport:{width:2560,height:1440},deviceScaleFactor:2});await hidpi.goto(base+'/#sources');await hidpi.waitForSelector('#rule-list .rule-card');assert.equal(await hidpi.evaluate(()=>parseFloat(getComputedStyle(document.body).fontSize)),16);await hidpi.close();
    assert.deepEqual(errors,[]);console.log('PASS display layout: one save action, no scroll/footer overlap or click-through, source/automation relocation, 390/1440/1920/2560 widths, seven 2K views, DPR 1/2');console.log(JSON.stringify(results));
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
