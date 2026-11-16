'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'C:/Users/Administrator/AppData/Roaming/npm/node_modules/browser-use/node_modules/playwright');

for (const file of ['README.md','../../README.md']) {
 const source=path.resolve(__dirname,file);
 for (const [,url] of fs.readFileSync(source,'utf8').matchAll(/\[[^\]]*\]\(([^)]+)\)/g)) {
  if (!/^[a-z]+:\/\//.test(url)) assert.ok(fs.existsSync(path.resolve(path.dirname(source),url.split('#')[0])),`Missing document link: ${url}`);
 }
}

(async () => {
 const browser = await chromium.launch({executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true});
 const page = await browser.newPage({viewport:{width:1440,height:1100}});
 const errors = [], network = [];
 page.on('pageerror', error => errors.push(error.message));
 page.on('request', request => {if (/^https?:/.test(request.url())) network.push(request.url());});
 const shots = path.join(__dirname,'previews');
 fs.mkdirSync(shots,{recursive:true});
 try {
  await page.goto(pathToFileURL(path.join(__dirname,'index.html')).href);
  assert.equal(await page.locator('#task-rows tr').count(),9);
  assert.match(await page.locator('#task-rows tr').first().textContent(),/17:00:05.*5 秒后.*首次通知/s);
  assert.match(await page.locator('[data-task="TD-0241"]').textContent(),/23:59:59.*按规则补足.*23:29:59/s);
  await page.locator('#filter-issues').click();
  assert.equal(await page.locator('#task-rows tr').count(),2);
  assert.match(await page.locator('[data-task="TD-0240"]').textContent(),/结果未知.*其他目标仍有投递计划/s);
  await page.locator('[data-task="TD-0240"] [data-task-id]').click();
  assert.equal(await page.locator('#task-dialog').isVisible(),true);
  assert.equal(await page.locator('#detail-body .target-plan').count(),3);
  assert.match(await page.locator('[data-target="team"]').textContent(),/结果未知/);
  assert.match(await page.locator('[data-target="personal"]').textContent(),/11\/15 18:00/);
  assert.match(await page.locator('[data-target="archive"]').textContent(),/已同步/);
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('#task-dialog').isVisible(),false);
  assert.equal(await page.locator('[data-task="TD-0240"] [data-task-id]').evaluate(el=>el===document.activeElement),true);
  await page.locator('#state-filter').selectOption('all');
  await page.locator('#target-filter').selectOption('archive');
  assert.equal(await page.locator('#task-rows .eta').filter({hasText:'等待内容变更'}).count(),6);
  await page.locator('#target-filter').selectOption('personal');
  assert.match(await page.locator('[data-task="TD-0238"]').textContent(),/11\/16 10:00/);
  await page.locator('#task-search').fill('this-does-not-exist');
  assert.equal(await page.locator('#task-empty').isVisible(),true);
  await page.locator('#reset-filters').click();
  assert.equal(await page.locator('#task-rows tr').count(),9);
  await page.locator('#task-search').fill('采购');
  assert.equal(await page.locator('#task-rows tr').count(),1);
  await page.locator('#task-search').fill('');
  await page.locator('#processed-title').click();
  await page.screenshot({path:path.join(shots,'tasks-desktop.png'),fullPage:true});

  await page.locator('#tab-tasks').focus();
  await page.keyboard.press('ArrowRight');
  assert.equal(await page.locator('#digest-view').isVisible(),true);
  assert.equal(await page.locator('#digest-items .digest-item').count(),4);
  assert.equal(await page.locator('#digest-target option').count(),2);
  assert.match(await page.locator('#digest-items').textContent(),/09:00.*13:00.*14:30.*15:00/s);
  assert.match(await page.locator('#digest-items').textContent(),/23:29:59.*23:59:59/s);
  assert.match(await page.locator('#digest-items').textContent(),/09:00 起按间隔/);
  assert.match(await page.locator('#digest-exclusions').textContent(),/确认培训参会名单.*等待回写/s);
  assert.equal(await page.locator('#digest-items').textContent().then(t=>t.includes('确认培训参会名单')),false);
  await page.locator('#digest-target').selectOption('personal');
  assert.match(await page.locator('#digest-items').textContent(),/10:00 起按间隔/);
  await page.locator('#digest-date').fill('2026-11-17');
  await page.locator('#digest-date').dispatchEvent('change');
  assert.equal(await page.locator('#digest-items .digest-item').count(),1);
  await page.locator('#digest-date').fill('2026-11-15');
  await page.locator('#digest-date').dispatchEvent('change');
  assert.equal(await page.locator('#digest-date-error').isVisible(),true);
  assert.equal(await page.locator('#digest-preview').isVisible(),false);
  await page.locator('#digest-date').fill('2026-11-16');
  await page.locator('#digest-date').dispatchEvent('change');
  await page.locator('#digest-target').selectOption('team');
  await page.locator('#digest-items [data-task-id="TD-0241"]').click();
  assert.match(await page.locator('#detail-body').textContent(),/按上海时区补足时刻/);
  await page.locator('#detail-to-digest').click();
  assert.equal(await page.locator('#task-dialog').isVisible(),false);
  await page.screenshot({path:path.join(shots,'digest-desktop.png'),fullPage:true});

  await page.locator('#open-rules').click();
  assert.match(await page.locator('#rules-dialog').textContent(),/首次通知.*截止提醒/s);
  assert.equal(await page.locator('#rule-rows tr').count(),5);
  await page.keyboard.press('Escape');
  for (const width of [320,390,768,1024,1440,2560]) {
   await page.setViewportSize({width,height:1100});
   for (const tab of ['tasks','digest']) {
    await page.locator('#tab-'+tab).click();
    const overflow = await page.evaluate(()=>document.documentElement.scrollWidth > innerWidth+1);
    assert.equal(overflow,false,`${width}px ${tab} overflows`);
    const clipped = await page.locator('.panel:visible,.page-heading:visible').evaluateAll(elements=>elements.some(el=>el.scrollWidth>el.clientWidth+1));
    assert.equal(clipped,false,`${width}px ${tab} panel clips content`);
    if(width===390 || width===2560) await page.screenshot({path:path.join(shots,`${tab}-${width}.png`),fullPage:true});
   }
   await page.locator('#tab-tasks').click();
   await page.locator('[data-task="TD-0240"] [data-task-id]').click();
   assert.equal(await page.locator('#task-dialog').evaluate(el=>el.scrollWidth>el.clientWidth+1),false,`${width}px dialog clips content`);
   await page.keyboard.press('Escape');
   await page.locator('#open-rules').click();
   assert.equal(await page.locator('#rules-dialog').evaluate(el=>el.scrollWidth>el.clientWidth+1),false,`${width}px rules dialog clips content`);
   await page.keyboard.press('Escape');
  }
  assert.deepEqual(errors,[]);
  assert.deepEqual(network,[]);
  console.log('PASS: plans, per-target outcomes, digest membership and dates, filters, keyboard, 320–2560px; no HTTP requests or script errors.');
 } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
