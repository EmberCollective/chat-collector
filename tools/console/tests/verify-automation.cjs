'use strict';
const assert=require('node:assert/strict'),path=require('node:path'),fs=require('node:fs');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const base=process.env.CONSOLE_TEST_URL||'http://127.0.0.1:18449';
const screenshots=process.env.CONSOLE_SCREENSHOTS;
(async()=>{
 assert.equal(new URL(base).hostname,'127.0.0.1');
 const browser=await chromium.launch({executablePath:process.env.CHROME_PATH||'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true});
 const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[],outbound=[],posts=[],saves=[];
 let accept=true,conflict=false,delaySave=false;
 page.on('pageerror',error=>errors.push(error.message));
 page.on('dialog',dialog=>accept?dialog.accept():dialog.dismiss());
 page.on('request',request=>{if(/^https?:/.test(request.url())&&new URL(request.url()).origin!==new URL(base).origin)outbound.push(request.url());});
 try{
  assert.equal((await(await page.request.get(base+'/__fixture__')).json()).synthetic,true,'Refuse to modify a real deployment');
  const token=(await(await page.request.get(base+'/api/session')).json()).token;
  const get=async endpoint=>{const response=await page.request.get(base+'/api'+endpoint,{headers:{'X-Console-Token':token}});assert.equal(response.ok(),true);return response.json();};
  const initial=await get('/operations/config');
  await page.route('**/api/operations/config',async route=>{
   if(route.request().method()==='PUT'){saves.push(route.request().postDataJSON());if(conflict){conflict=false;return route.fulfill({status:409,json:{detail:'configuration changed'}});}if(delaySave){delaySave=false;await new Promise(resolve=>setTimeout(resolve,300));}}
   return route.continue();
  });
  // The browser exercises real paths without starting a process, modifying startup or sending externally.
  await page.route('**/api/operations/**',async route=>{
   const request=route.request(),url=new URL(request.url());
   if(request.method()==='POST'||url.pathname==='/api/operations/startup'&&request.method()==='PUT'){
    posts.push({path:url.pathname,body:request.postDataJSON()});return route.fulfill({json:{state:'queued',status:'unknown',supported:true,enabled:request.postDataJSON()?.enabled||false}});
   }
   if(url.pathname==='/api/operations/startup')return route.fulfill({json:{supported:true,enabled:false}});
   if(url.pathname==='/api/operations/status'){
    const response=await route.fetch(),snapshot=await response.json();
    snapshot.messaging.deliveries=[{id:'unknown-delivery',task_id:'task-1',target_id:'workflow-sample',title:'合成投递',status:'unknown',error:'confirmation timeout'}];
    snapshot.feedback.requests=[{request_id:'failed-feedback',task_id:'task-1',title:'合成回写',status:'failed',error:'temporary conflict'}];
    return route.fulfill({response,json:snapshot});
   }
   return route.fallback();
  });
  await page.goto(base+'/#automation');
  await page.waitForFunction(()=>document.getElementById('ops-notice').textContent.includes('设置已加载'));
  assert.deepEqual(await page.evaluate(()=>[...document.querySelectorAll('[id]')].map(n=>n.id).filter((id,index,ids)=>ids.indexOf(id)!==index)),[]);
  assert.equal(await page.locator('#automation-collection').isVisible(),true);
  assert.deepEqual(await page.locator('#view-automation [role=tab]').allTextContents(),['采集与处理','投递与提醒','后台服务']);
  assert.equal(await page.locator('.automation-source-row').count(),4);
  assert.equal(await page.locator('.automation-source-row[data-source-id="qq-work"] input').count(),0,'unmatched instance borrowed a different account plan');
  assert.equal(await page.locator('.automation-source-row[data-source-id="qq-work"] button').count(),1,'unmatched SQL source lost its own rule editor');
  assert.equal(await page.locator('.automation-source-row[data-source-id="external-example"] input').count(),0);
  const source=page.locator('.automation-source-row[data-source-id="wechat"]');
  const check=source.locator('input[type=checkbox]'),interval=source.locator('input[type=number]');
  const hit=await check.locator('..').boundingBox();assert.ok(hit.width>=36&&hit.height>=36);
  const state=await page.locator('#ops-service-status').textContent();
  await check.setChecked(!initial.automation.jobs.wechat.enabled);await interval.fill('73');
  assert.equal(await page.locator('#ops-service-status').textContent(),state,'draft changed runtime state');
  await page.locator('#tab-automation-channels').click();
  await page.locator('#ops-reminder-fields').getByText('无截止时间时的间隔提醒',{exact:false}).click();
  const p3=page.locator('#ops-reminder-fields [data-ops-key="P3"][type=number]');await p3.fill('123');
  await page.locator('#ops-open-add').click();await page.locator('#ops-new-kind').selectOption('webhook');
  await page.locator('#ops-new-id').fill('migration-'+Date.now());const newId=await page.locator('#ops-new-id').inputValue();await page.locator('#ops-add-target').click();
  const target=page.locator('#ops-targets details.channel').filter({hasText:newId});assert.equal(await target.evaluate(node=>node.open),true);
  assert.equal(await target.locator('[data-ops-key="enabled"]').isChecked(),false);
  await target.locator('[data-ops-key="url_env"]').fill('MIGRATION_TEST_URL');
  assert.match(await page.locator('#ops-targets details').filter({hasText:'future'}).textContent(),/当前只读/);
  assert.equal(await page.locator('#ops-targets details').filter({hasText:'reserved-kind'}).locator('input').count(),0);
  conflict=true;await page.locator('#ops-save').click();await page.waitForFunction(()=>document.getElementById('ops-notice').textContent.includes('你的编辑已保留'));
  assert.equal(await p3.inputValue(),'123');assert.equal(await target.locator('[data-ops-key="url_env"]').inputValue(),'MIGRATION_TEST_URL');
  delaySave=true;await page.locator('#ops-save').click();assert.equal(await p3.isDisabled(),true,'saving must not discard new edits typed during the request');await page.waitForFunction(()=>document.getElementById('ops-notice').textContent.startsWith('已保存'));
  const saved=await get('/operations/config');
  const expected=structuredClone(initial.automation);expected.jobs.wechat.enabled=!initial.automation.jobs.wechat.enabled;expected.jobs.wechat.interval_seconds=73;
  assert.deepEqual(saved.automation,expected,'hidden job, retry, listener or alert fields changed');
  for(const key of ['enabled','feedback_enabled','deadline_reminders','initial_notifications','daily_digest','schedule','feedback'])assert.deepEqual(saved.messaging[key],initial.messaging[key],key+' lost during shared save');
  assert.equal(saved.messaging.reminders.P3,123);for(const key of ['P0','P1','P2','P4'])assert.equal(saved.messaging.reminders[key],initial.messaging.reminders[key]);
  for(const id of ['future','reserved-kind'])assert.ok(saved.messaging.targets.some(target=>target.id===id),'unknown target was lost');
  assert.ok(saved.messaging.targets.some(target=>target.id===newId&&!target.enabled));
  assert.equal(saves.length,2);assert.ok(!saves[1].messaging.targets.some(target=>target.kind==='constructor'),'unsupported target must remain server managed');
  await page.locator('#tab-automation-collection').click();
  await page.locator('#ops-source-advanced').locator('..').locator('summary').first().click();
  const extra=page.locator('#ops-source-advanced [data-job="wechat"]');
  await extra.getByRole('button',{name:'运行一轮',exact:true}).click();
  await extra.getByRole('button',{name:'恢复重试',exact:true}).click();
  await page.waitForFunction(()=>!document.getElementById('ops-save').disabled);
  assert.ok(posts.some(post=>post.path==='/api/operations/jobs/wechat/once'));assert.ok(posts.some(post=>post.path==='/api/operations/jobs/wechat/resume'));
  await page.locator('#tab-automation-background').click();
  assert.equal(await page.locator('#automation-savebar').isVisible(),false);
  for(const id of ['ops-start','ops-stop','ops-restart','ops-startup-enable','ops-startup-disable']){await page.locator('#'+id).click();await page.waitForFunction(id=>!document.getElementById(id).disabled,id);}
  for(const path of ['service/start','service/stop','service/restart'])assert.ok(posts.some(post=>post.path==='/api/operations/'+path));
  assert.ok(posts.some(post=>post.path==='/api/operations/startup'&&post.body.enabled===true));assert.ok(posts.some(post=>post.path==='/api/operations/startup'&&post.body.enabled===false));
  await page.evaluate(()=>{location.hash='delivery';});await page.waitForSelector('#ops-recovery button');
  await page.locator('#ops-recovery').getByRole('button',{name:'已核实未送达，重试',exact:true}).click();
  await page.locator('#ops-recovery').getByRole('button',{name:'已核实送达',exact:true}).click();
  await page.locator('#ops-recovery').getByRole('button',{name:'重试回写',exact:true}).click();
  await page.waitForFunction(()=>!document.getElementById('ops-save').disabled);
  for(const path of ['deliveries/unknown-delivery/retry','deliveries/unknown-delivery/delivered','feedback/failed-feedback/retry'])assert.ok(posts.some(post=>post.path==='/api/operations/'+path));
  await page.evaluate(()=>{location.hash='automation/channels';});await page.waitForSelector('#ops-targets');
  const known=page.locator('#ops-targets details.channel').filter({hasText:'workflow-sample'});await known.locator('summary').first().click();await known.getByRole('button',{name:'发送测试消息…'}).click();
  await page.waitForFunction(()=>!document.getElementById('ops-save').disabled);
  assert.ok(posts.some(post=>post.path==='/api/operations/targets/workflow-sample/test'));
  // SQL edits stay on the single source editor and its full configuration draft.
  await page.evaluate(()=>{document.getElementById('rulesets-editor').dataset.sharedEditor='retained';location.hash='automation';});
  await page.waitForSelector('.automation-source-row[data-source-id="wechat"] button');await source.getByRole('button',{name:'SQL 规则与参数 →'}).click();
  await page.waitForFunction(()=>document.getElementById('module').dataset.loaded==='wechat');
  assert.equal(await page.locator('#automation-rules-slot #rulesets-editor').getAttribute('data-shared-editor'),'retained');
  await page.locator('#cfg-schedule-refresh_interval_seconds').evaluate(node=>node.value='17');
  await page.locator('#rules-save').click();await page.waitForFunction(()=>currentConfig.config.schedule.refresh_interval_seconds===17);
  await page.evaluate(()=>{location.hash='settings/source';});await page.waitForSelector('#source-rules-slot #rulesets-editor',{state:'attached'});
  assert.equal(await page.locator('#source-rules-slot #rulesets-editor').getAttribute('data-shared-editor'),'retained');
  // Every published link and keyboard tab maps to the same three categories.
  for(const [hash,panel]of [['automation','collection'],['automation/collection','collection'],['automation/tasks','collection'],['automation/background','background'],['automation/service','background'],['automation/channels','channels'],['automation/rules','collection'],['settings/background','background'],['settings/channels','channels']]){
   await page.evaluate(hash=>location.hash=hash,hash);await page.waitForFunction(panel=>!document.getElementById('automation-'+panel).hidden,panel);
   assert.equal(await page.locator('#automation-'+panel).isVisible(),true,hash);
  }
  await page.locator('#tab-automation-collection').focus();await page.keyboard.press('ArrowRight');assert.equal(await page.locator('#automation-channels').isVisible(),true);await page.keyboard.press('End');assert.equal(await page.locator('#automation-background').isVisible(),true);await page.keyboard.press('Home');assert.equal(await page.locator('#automation-collection').isVisible(),true);
  for(const width of [320,390,800,1024,1280,1440,1600]){await page.setViewportSize({width,height:1000});for(const panel of ['collection','channels','background']){await page.evaluate(panel=>activatePanel('automation-'+panel),panel);assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),`overflow ${width}/${panel}`);}}
  await page.setViewportSize({width:390,height:844});await page.evaluate(()=>activatePanel('automation-collection'));assert.equal(await source.locator('.automation-mobile-label').isVisible(),true);
  if(screenshots){fs.mkdirSync(screenshots,{recursive:true});await page.reload();await page.waitForFunction(()=>document.getElementById('ops-notice').textContent.includes('设置已加载'));await page.evaluate(()=>location.hash='automation');await page.setViewportSize({width:1440,height:1000});await page.screenshot({path:path.join(screenshots,'automation.png')});await page.locator('#tab-automation-channels').click();await page.screenshot({path:path.join(screenshots,'automation-channels.png'),fullPage:true});await page.locator('#tab-automation-background').click();await page.screenshot({path:path.join(screenshots,'automation-service.png'),fullPage:true});await page.setViewportSize({width:390,height:844});await page.locator('#tab-automation-collection').click();await page.screenshot({path:path.join(screenshots,'automation-mobile.png')});}
  assert.deepEqual(errors,[]);assert.deepEqual(outbound,[]);
  console.log('PASS automation: shared/versioned save, hidden settings preserved, unknown targets, operations paths, safe recovery, single SQL editor/full source save, routing/keyboard and 320–1600px; no external requests or browser errors');
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
