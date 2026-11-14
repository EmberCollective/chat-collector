'use strict';
const assert=require('node:assert/strict');
const path=require('node:path');
const fs=require('node:fs');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const base=process.env.CONSOLE_TEST_URL||'http://127.0.0.1:18447';
const screenshots=process.env.CONSOLE_SCREENSHOTS;
(async()=>{
  assert.equal(new URL(base).hostname,'127.0.0.1');
  const browser=await chromium.launch({executablePath:process.env.CHROME_PATH||'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true});
  const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[],outbound=[];
  page.on('pageerror',error=>errors.push(error.message));
  page.on('request',request=>{if(/^https?:/.test(request.url())&&new URL(request.url()).origin!==new URL(base).origin)outbound.push(request.url());});
  page.on('dialog',dialog=>dialog.dismiss());
  try{
    if(screenshots)fs.mkdirSync(screenshots,{recursive:true});
    assert.equal((await (await page.request.get(base+'/__fixture__')).json()).synthetic,true,'Refuse to mutate a real deployment');
    await page.goto(base);
    await page.waitForSelector('#workflow-matches',{state:'attached'});
    assert.equal(await page.locator('#module option').count(),4);
    assert.deepEqual(await page.evaluate(()=>[...document.querySelectorAll('[id]')].map(n=>n.id).filter((id,i,ids)=>ids.indexOf(id)!==i)),[]);
    await page.locator('[data-view=sources]').click();
    await page.waitForSelector('#rule-list .rule-card');
    const sql=page.locator('#rule-list .rule-card').filter({hasText:'new-rule.sql'});
    assert.equal(await sql.locator('input[type=checkbox]').isChecked(),false);
    const discovered=page.locator('#rule-list .rule-card').filter({hasText:'discovered.sql'});
    assert.equal(await discovered.locator('input[type=checkbox]').isChecked(),false);
    assert.equal((await page.evaluate(()=>ruleValues())).some(rule=>rule.file.endsWith('discovered.sql')),false);
    await sql.locator('input[type=checkbox]').check();
    await sql.locator('[data-rule-parameter=term]').fill('draft-wechat');
    await page.locator('#rules-refresh').click();
    assert.equal(await sql.locator('[data-rule-parameter=term]').inputValue(),'draft-wechat');
    await page.evaluate(()=>location.hash='automation/rules');
    await page.waitForSelector('#automation-rules-slot #rulesets-editor');
    assert.equal(await sql.locator('[data-rule-parameter=term]').inputValue(),'draft-wechat');
    await page.locator('#automation-module').selectOption('qq');
    await page.waitForFunction(()=>document.getElementById('module').dataset.loaded==='qq');
    assert.equal(await sql.locator('input[type=checkbox]').isChecked(),false);
    await sql.locator('input[type=checkbox]').check();await sql.locator('[data-rule-parameter=term]').fill('draft-qq');
    await page.locator('#automation-module').selectOption('wechat');
    await page.waitForFunction(()=>document.getElementById('module').dataset.loaded==='wechat');
    assert.equal(await sql.locator('[data-rule-parameter=term]').inputValue(),'draft-wechat');
    if(screenshots)await page.screenshot({path:path.join(screenshots,'automation-rules.png'),fullPage:true});
    // Clear the test-only rule, leaving the fixture's saved registrations unchanged.
    await sql.locator('input[type=checkbox]').uncheck();
    await page.evaluate(()=>location.hash='sources');
    await page.waitForSelector('#source-rules-slot #rulesets-editor');
    await page.locator('#preview').click();
    await page.waitForSelector('#preview-output .message-row');
    assert.match(await page.locator('#preview-output').textContent(),/<img src=x onerror=alert\(1\)>/);
    assert.equal(await page.locator('#preview-output img').count(),0);
    await page.locator('#preview-output .message-row').click();
    await page.waitForSelector('#message-inspector .message');
    if(screenshots){fs.mkdirSync(screenshots,{recursive:true});await page.screenshot({path:path.join(screenshots,'sources.png'),fullPage:true});}
    await page.locator('#result-search').fill('no-match');
    assert.match(await page.locator('#preview-output .filter-empty').textContent(),/没有匹配/);
    await page.locator('#result-search').fill('');
    await page.locator('#start').evaluate(node=>{node.value='2026-11-13T00:00:00';node.dispatchEvent(new Event('input'));});
    assert.match(await page.locator('#result-scope').textContent(),/待更新/);
    await page.locator('#filter-rulesets').evaluate(node=>node.closest('details').open=true);
    await page.locator('#filter-rulesets').fill('invalid JSON');
    await page.locator('#module').selectOption('qq');
    await page.waitForFunction(()=>document.getElementById('module').dataset.loaded==='qq');
    await page.locator('#module').selectOption('wechat');
    await page.waitForFunction(()=>document.getElementById('module').dataset.loaded==='wechat');
    assert.equal(await page.locator('#filter-rulesets').inputValue(),'invalid JSON');
    // Out-of-order source config responses must never copy another account's draft.
    await page.evaluate(()=>document.getElementById('cfg-source-decrypted_dir').value='A-unsaved-path');
    await page.route('**/api/modules/qq/config',async route=>{const response=await route.fetch();await new Promise(r=>setTimeout(r,250));await route.fulfill({response});});
    await page.locator('#module').selectOption('qq');
    await page.locator('#module').selectOption('qq-work');
    await page.waitForFunction(()=>document.getElementById('module').dataset.loaded==='qq-work');
    await page.waitForTimeout(300);
    await page.locator('#module').selectOption('qq');
    await page.waitForFunction(()=>document.getElementById('module').dataset.loaded==='qq');
    assert.notEqual(await page.locator('#cfg-source-decrypted_dir').inputValue(),'A-unsaved-path');
    await page.unroute('**/api/modules/qq/config');
    // Saving A cannot reload B or discard edits made while the save was in flight.
    await page.locator('#module').selectOption('wechat');await page.waitForFunction(()=>document.getElementById('module').dataset.loaded==='wechat');
    await page.evaluate(()=>{document.getElementById('filter-rulesets').value=JSON.stringify(currentConfig.config.filters.rulesets);document.getElementById('cfg-schedule-refresh_interval_seconds').value='11';document.getElementById('cfg-source-decrypted_dir').value=currentConfig.config.source.decrypted_dir;renderRuleSummary();});
    await page.route('**/api/modules/wechat/config',async route=>{const response=await route.fetch();if(route.request().method()==='PATCH')await new Promise(r=>setTimeout(r,250));await route.fulfill({response});});
    await page.evaluate(()=>{const confirm=window.confirm;window.confirm=()=>true;window.savedConfigCheck=saveConfig().finally(()=>window.confirm=confirm);});
    await page.locator('#module').selectOption('qq');await page.waitForFunction(()=>document.getElementById('module').dataset.loaded==='qq');
    await page.evaluate(()=>document.getElementById('cfg-source-media_dir').value='B-unsaved-draft');await page.evaluate(()=>window.savedConfigCheck);
    assert.equal(await page.locator('#cfg-source-media_dir').inputValue(),'B-unsaved-draft');await page.unroute('**/api/modules/wechat/config');
    await page.locator('#module').selectOption('external-example');
    await page.waitForFunction(()=>document.getElementById('module').dataset.loaded==='external-example');
    assert.equal(await page.locator('#preview').isVisible(),false);
    assert.equal(await page.locator('#replay').isVisible(),false);
    const refused=await page.evaluate(async()=>{try{requireSource('collect');return false;}catch{return true;}});assert.equal(refused,true);
    await page.evaluate(()=>location.hash='processor');
    await page.locator('#tab-pi-workflow').click();
    await page.waitForSelector('.workflow-event');
    assert.equal(await page.locator('.workflow-event').count(),50);
    const tool=page.locator('.workflow-event').filter({hasText:'调用工具 · future_tool'}).first();
    await tool.locator('summary').first().click();
    await page.waitForFunction(()=>[...document.querySelectorAll('.workflow-event[open] pre')].some(n=>n.textContent.includes('报告')));
    await page.locator('#workflow-more').click();await page.waitForFunction(()=>document.querySelectorAll('.workflow-event').length===68);
    assert.equal(await page.locator('.workflow-event').count(),68);
    await page.locator('#workflow-filter').selectOption('tool');
    assert.equal(await page.locator('.workflow-event:not([hidden])').count(),62);
    await page.locator('#workflow-filter').selectOption('');
    await page.locator('#workflow-refresh').click();
    assert.equal(await tool.evaluate(node=>node.open),true);
    if(screenshots)await page.screenshot({path:path.join(screenshots,'pi-workflow.png'),fullPage:true});
    // A delayed old selection cannot relabel a newer run's transcript.
    const race=await page.evaluate(async()=>{
      const original=loadWorkflowRuns;let release;loadWorkflowRuns=()=>new Promise(r=>{release=r;});
      const previous=selectWorkflow('b'.repeat(32),false);await Promise.resolve();
      loadWorkflowRuns=async()=>{const picker=document.getElementById('workflow-run');const option=document.createElement('option');option.value='c'.repeat(32);picker.append(option);};
      const read=loadWorkflowPage;loadWorkflowPage=async()=>{};await selectWorkflow('c'.repeat(32),false);release();await previous;
      const result={run:workflowRun,picker:document.getElementById('workflow-run').value};loadWorkflowRuns=original;loadWorkflowPage=read;return result;
    });assert.equal(race.run,race.picker);
    await page.evaluate(()=>selectWorkflow('a'.repeat(32),false));
    await page.evaluate(()=>location.hash='settings/channels');
    await page.waitForSelector('#ops-targets article');
    assert.match(await page.locator('#ops-targets').textContent(),/当前只读/);
    assert.match(await page.locator('#ops-targets article').filter({hasText:'reserved-kind'}).textContent(),/当前只读/);
    assert.doesNotMatch(await page.content(),/never-output-this/);
    await page.locator('#ops-new-kind').selectOption('webhook');await page.locator('#ops-new-id').fill('second-target');await page.locator('#ops-add-target').click();
    const newTarget=page.locator('#ops-targets article').filter({hasText:'second-target'});
    assert.equal(await newTarget.locator('input[type=checkbox]').first().isChecked(),false);
    await page.locator('#ops-delivery-save').click();
    await page.waitForFunction(()=>document.getElementById('ops-delivery-notice').textContent.startsWith('已保存'));
    assert.match(await page.locator('#ops-targets').textContent(),/future/);
    await page.evaluate(()=>location.hash='history');await page.waitForFunction(()=>document.getElementById('history-summary').textContent.includes('已显示'));
    await page.locator('#history-module').selectOption('qq');await page.locator('#history-action').selectOption('collect');await page.locator('#history-origin').selectOption('schedule');
    await page.locator('#history-min-duration').fill('10');await page.locator('#history-error').fill('busy');await page.locator('#history-apply').click();
    await page.waitForFunction(()=>document.getElementById('history-summary').textContent.startsWith('已显示 1 / 1'));
    assert.match(await page.locator('#jobs').textContent(),/14.0 秒/);assert.match(await page.locator('#jobs').textContent(),/fixture database busy/);
    await page.locator('#jobs button').first().click();await page.waitForSelector('#detail-dialog[open]');await page.locator('#close-detail').click();
    await page.locator('#history-reset').click();
    // Appending during an in-flight replacement cannot invalidate that replacement.
    await page.evaluate(async()=>{const original=api;let release;api=(...args)=>args[0].startsWith('/jobs?')?new Promise(r=>{release=r;}):original(...args);const request=refreshHistory();await Promise.resolve();await refreshHistory(true);release({jobs:[],total:0,facets:{actions:[],modules:[],origins:[]},next_cursor:0,has_more:false});await request;api=original;});
    assert.match(await page.locator('#history-summary').textContent(),/^已显示 0 \/ 0/);

        // Real API version conflict preserves the text being edited.
    await page.evaluate(()=>location.hash='processor');await page.locator('#tab-processor-rules-panel').click();
    const edited='# User draft must survive conflict';await page.locator('#processor-rules-text').fill(edited);
    await page.evaluate(async()=>{const current=await api('/processor/rules');await api('/processor/rules','PUT',{revision:current.revision,text:'# Concurrent revision '+Date.now()});});
    await page.locator('#processor-save-rules').click();await page.waitForFunction(()=>document.getElementById('notice').textContent.includes('变化'));
    assert.equal(await page.locator('#processor-rules-text').inputValue(),edited);
    for(const width of [320,390,800,1024,1280,1440,1600]){
      await page.setViewportSize({width,height:1000});
      for(const view of ['overview','sources','processor','delivery','automation','history','settings']){
        await page.evaluate(view=>location.hash=view,view);
        await page.waitForFunction(view=>!document.getElementById('view-'+view).hidden,view);
        assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),`overflow ${width}/${view}`);
      }
    }
    await page.setViewportSize({width:390,height:844});await page.evaluate(()=>location.hash='sources');
    await page.locator('#menu-toggle').click();assert.equal(await page.locator('#app-body').evaluate(n=>n.inert),true);
    await page.locator('[data-view=sources]').click();assert.equal(await page.locator('#app-body').evaluate(n=>n.inert),false);
    await page.locator('#module').selectOption('wechat');await page.waitForFunction(()=>document.getElementById('module').dataset.loaded==='wechat');
    await page.locator('#tab-messages').click();await page.locator('#load-messages').click();await page.waitForSelector('#message-output .message-row');
    await page.locator('#message-output .message-row').click();await page.waitForSelector('#message-dialog[open]');
    if(screenshots)await page.screenshot({path:path.join(screenshots,'mobile.png'),fullPage:true});
    await page.keyboard.press('Escape');
    assert.deepEqual(errors,[]);assert.deepEqual(outbound,[]);
    console.log('PASS: authenticated UI, provider isolation/races, safe text, workflow paging/filtering, config conflicts, responsive navigation');
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
