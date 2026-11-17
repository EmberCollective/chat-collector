'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const base = process.env.CONSOLE_TEST_URL;
assert.ok(base && ['127.0.0.1','localhost'].includes(new URL(base).hostname),'Use a local synthetic console fixture');
const root = path.resolve(__dirname,'../../..');
const output = path.join(root,'.runtime/delivery-acceptance');
fs.mkdirSync(output,{recursive:true});
(async () => {
 const browser = await chromium.launch({executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true});
 const page = await browser.newPage({viewport:{width:1440,height:1100}}),errors = [],writes = [];
 page.on('pageerror',error => errors.push(error.message));
 page.on('request',request => {if(request.method() !== 'GET') writes.push(request.url());});
 try {
  assert.equal((await (await page.request.get(base+'/__fixture__')).json()).synthetic,true);
  await page.goto(base+'/#delivery');
  await page.waitForFunction(() => document.querySelectorAll('#delivery-task-rows tr').length === 1);
  assert.match(await page.locator('#delivery-runtime').textContent(),/外部投递已关闭/);
  await page.locator('#delivery-task-rows [data-task-id]').click();
  await page.locator('#delivery-state-actions').waitFor();
  assert.match(await page.locator('#delivery-detail-body').textContent(),/Complete the form/);
  assert.equal(await page.locator('#delivery-detail-plans .target-plan').count(),3);
  await page.keyboard.press('Escape');
  await page.locator('#delivery-tab-digest').click();
  await page.locator('#delivery-digest-message-title').filter({hasText:'通知安排'}).waitFor();
  assert.match(await page.locator('#delivery-digest-eta').textContent(),/未启用/);

  // Visual/interaction parity uses the approved synthetic snapshot; HTTP projection is checked above and in pytest.
  const approved = await browser.newPage();
  await approved.goto(pathToFileURL(path.join(root,'docs/design/delivery-schedule/index.html')).href);
  const sample = await approved.evaluate(() => {
   const targets = demo.targets.map(t => ({...t,enabled:true,description:t.name.split(' · ')[1]}));
   return {now:Date.parse(demo.now)/1000,targets,rules:{reminders:demo.intervals,deadline_reminders:{enabled:true,...demo.deadlineRules},initial_notifications:{P0:true,P1:true,P2:true,P3:true,P4:true},daily_digest:{enabled:true,time:'08:00',timezone:'Asia/Shanghai',catch_up_minutes:360}},
    tasks:demo.tasks.map(t => ({task_id:t.id,title:t.title,revision:1,priority:t.priority,status:t.status,source_names:[t.source],source_count:t.messages,deadline_at:t.deadline ? Date.parse(t.deadline)/1000 : null,date_only:!!t.dateOnly,
     nodes:t.nodes.map(([at,,state]) => ({due_at:Date.parse(at)/1000,deadline_at:Date.parse(t.deadline)/1000,offset_seconds:(Date.parse(t.deadline)-Date.parse(at))/1000})),
     plans:demo.targets.map(target => {const p=plan(t,target);return {target_id:target.id,state:p.state,due_at:p.next ? Date.parse(p.next)/1000 : null,reason:p.reason,last_sent_at:t.last ? Date.parse(t.targetLast?.[target.id] || t.last)/1000 : null};}),
     attention:['unknown','feedback'].includes(t.state),source:t} ))};
  });
  await page.route('**/api/messaging/schedule?*',async route => {
   const q = new URL(route.request().url()).searchParams, filter = q.get('state'), target = q.get('target_id'), search = q.get('search') || '';
   let tasks = sample.tasks.filter(t => [t.title,t.task_id,...t.source_names].join(' ').includes(search));
   tasks = tasks.filter(t => filter === 'attention' ? t.plans.some(p => (!target || p.target_id === target) && ['unknown','feedback'].includes(p.state)) : filter === 'ended' ? t.status !== 'open' : true);
   tasks.sort((a,b) => Math.min(...a.plans.filter(p=>p.due_at != null).map(p=>p.due_at),Infinity)-Math.min(...b.plans.filter(p=>p.due_at != null).map(p=>p.due_at),Infinity));
   await route.fulfill({json:{generated_at:sample.now,enabled:true,targets:sample.targets,tasks,total_tasks:9,matched_tasks:tasks.length,pending_events:0,attention:2,has_more:false,rules:sample.rules,runtime:{state:'running',interval_seconds:5,next_check_at:sample.now+5}}});
  });
  await page.route('**/api/messaging/digest?*',async route => {
   const q = new URL(route.request().url()).searchParams,day = q.get('date') || '2026-11-16',id = q.get('target_id') || 'team';
   if(day < '2026-11-16') return route.fulfill({status:400,json:{detail:'invalid preview date'}});
   const result = await approved.evaluate(({day,id}) => digestData(day,demo.targets.find(t=>t.id===id)),{day,id});
   await route.fulfill({json:{generated_at:sample.now,date:day,timezone:'Asia/Shanghai',slot:Date.parse(day+'T08:00:00+08:00')/1000,expires_at:Date.parse(day+'T14:00:00+08:00')/1000,enabled:true,target_id:id,targets:sample.targets.filter(t=>t.digest),delivery:null,
    items:result.items.map(({task,notifications})=>({task_id:task.id,title:task.title,priority:task.priority,deadline:task.deadline,notifications:notifications.map(n=>({time:n.at,kind:n.interval?'interval':'deadline'}))})),excluded:result.excluded.map(({task,reason})=>({task_id:task.id,title:task.title,reason}))}});
  });
  await page.route(/\/api\/messaging\/tasks\/TD-\d+$/,async route => {
   const id = new URL(route.request().url()).pathname.split('/').at(-1),task = sample.tasks.find(t=>t.task_id===id),s = task.source;
   await route.fulfill({json:{task_id:id,title:task.title,status:task.status,event:'合成行动说明',action_steps:s.actions,time:{deadline:s.deadline},people:[],sources:[{text:s.evidence}],resources:[],delivery_plan:task}});
  });
  await page.goto(base+'/?delivery-review=1#delivery');
  await page.waitForFunction(() => document.querySelectorAll('#delivery-task-rows tr').length === 9);
  assert.equal(await page.locator('#view-delivery .filters').evaluate(el=>getComputedStyle(el).flexDirection),'row');
  assert.ok((await page.locator('#view-delivery .filters').boundingBox()).height<130,'desktop filters must fit one row');
  await page.locator('#delivery-filter-issues').click();
  await page.waitForFunction(() => document.querySelectorAll('#delivery-task-rows tr').length === 2);
  await page.locator('[data-task="TD-0240"] [data-task-id]').click();
  await page.locator('#delivery-detail-plans .target-plan').first().waitFor();
  assert.match(await page.locator('#delivery-detail-plans').textContent(),/结果未知.*个人提醒.*18:00/s);
  // A changed list refresh can replace the opener while its dialog remains open.
  await page.locator('[data-task="TD-0240"] [data-task-id]').evaluate(button=>button.replaceWith(button.cloneNode(true)));
  await page.keyboard.press('Escape');
  await page.waitForFunction(()=>document.activeElement?.dataset.taskId==='TD-0240');
  await page.locator('#delivery-state-filter').selectOption('all');
  await page.waitForFunction(() => document.querySelectorAll('#delivery-task-rows tr').length === 9);
  await page.locator('#delivery-task-search').fill('not-found');
  await page.locator('#delivery-task-empty').waitFor();
  await page.locator('#delivery-reset-filters').click();
  await page.waitForFunction(() => document.querySelectorAll('#delivery-task-rows tr').length === 9);
  await page.locator('#delivery-task-rows tr').first().evaluate(el=>el.dataset.stable='same-node');
  const refresh = page.waitForResponse(response=>response.url().includes('/api/messaging/schedule?'));
  await page.locator('#messaging-preview').click();await refresh;
  assert.equal(await page.locator('#delivery-task-rows tr').first().getAttribute('data-stable'),'same-node','unchanged refresh should preserve row DOM');
  await page.locator('#delivery-tab-tasks').focus();await page.keyboard.press('ArrowRight');
  await page.waitForFunction(() => document.querySelectorAll('#delivery-digest-items .digest-item').length === 4);
  await page.locator('#delivery-digest-target').selectOption('personal');
  await page.waitForFunction(() => document.querySelector('#delivery-digest-items').textContent.includes('10:00 起按间隔'));
  await page.locator('#delivery-digest-date').fill('2026-11-17');await page.locator('#delivery-digest-date').dispatchEvent('change');
  await page.waitForFunction(() => document.querySelectorAll('#delivery-digest-items .digest-item').length === 1);
  // A digest item can be opened even when it is absent from the current list page.
  await page.locator('#delivery-digest-items [data-task-id]').click();
  await page.locator('#delivery-detail-plans .target-plan').first().waitFor();
  assert.equal(await page.locator('#delivery-detail-plans .target-plan').count(),3);await page.keyboard.press('Escape');
  await page.locator('#delivery-digest-date').fill('2026-11-16');await page.locator('#delivery-digest-date').dispatchEvent('change');
  await page.locator('#delivery-digest-target').selectOption('team');
  for (const width of [320,390,768,1024,1440,2560]) {
   await page.setViewportSize({width,height:1100});
   for (const tab of ['tasks','digest']) {
    await page.locator('#delivery-tab-'+tab).click();
    await page.waitForTimeout(100);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false,`${width}px ${tab} overflows`);
    if ([390,1440,2560].includes(width)) await page.screenshot({path:path.join(output,`${tab}-${width}.png`),fullPage:true});
   }
   await page.locator('#delivery-open-rules').click();
   assert.equal(await page.locator('#delivery-rules-dialog').evaluate(el=>el.scrollWidth>el.clientWidth+1),false,`${width}px rules clips`);
   await page.keyboard.press('Escape');
  }
  assert.deepEqual(writes,[],'Browsing must not send/import/write');
  assert.deepEqual(errors,[]);
  console.log('PASS: real read-only fixture API + approved snapshot interactions, 320–2560px; no writes or script errors.');
 } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
