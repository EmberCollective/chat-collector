'use strict';
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const base = process.env.CONSOLE_TEST_URL || 'http://127.0.0.1:18447';
(async () => {
  assert.equal(new URL(base).hostname, '127.0.0.1');
  const browser = await chromium.launch({executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true});
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}}), errors = [], outbound = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => {if (/^https?:/.test(request.url()) && new URL(request.url()).origin !== new URL(base).origin) outbound.push(request.url());});
  try {
    assert.equal((await (await page.request.get(base+'/__fixture__')).json()).synthetic, true, 'Refuse to mutate a real deployment');
    await page.goto(base+'/#processor');
    await page.locator('#tab-processor-results').click();
    const card = page.locator('#processor-tasks [data-task-id="ui-open"]');
    await card.waitFor();
    const reviews = page.locator('#processor-review-output > details');
    assert.equal(await reviews.evaluate(node => node.open), false);
    assert.match(await reviews.locator(':scope > summary').textContent(), /20 组 · 1 个附件读取失败/);
    assert.ok((await card.boundingBox()).y < (await reviews.boundingBox()).y);
    assert.equal(await page.locator('[data-task-id="ui-superseded"] .task-actions').count(), 0);
    assert.equal(await page.evaluate(() => {
      const target = document.createElement('div');
      renderProcessorTasks([{task_id:'debug-only',status:'open',priority:'P2',revision:1}], target);
      return target.querySelectorAll('.task-actions').length;
    }), 0);

    let requests = 0, release;
    const gate = new Promise(resolve => {release = resolve;});
    const hold = async route => {if (route.request().method() === 'PATCH') {requests++; await gate;} await route.continue();};
    await page.route('**/api/processor/tasks/ui-open', hold);
    await card.getByRole('button', {name:'标记完成', exact:true}).click();
    await page.waitForFunction(() => document.querySelector('[data-task-id="ui-open"] .task-actions button').disabled);
    assert.equal(await card.getByLabel('优先级').isDisabled(), true);
    await card.locator('.task-actions button').evaluate(node => node.click());
    release();
    await card.getByRole('button', {name:'恢复待办', exact:true}).waitFor();
    assert.equal(requests, 1);
    await page.unroute('**/api/processor/tasks/ui-open', hold);
    assert.match(await card.locator('.task-action-feedback').textContent(), /已标记完成/);
    assert.equal(await card.locator('[data-task-action=status]').evaluate(node => node === document.activeElement), true);
    await card.getByRole('button', {name:'恢复待办', exact:true}).click();
    await card.getByRole('button', {name:'标记完成', exact:true}).waitFor();
    await card.getByLabel('优先级').selectOption('P0');
    await page.waitForFunction(() => document.querySelector('[data-task-id="ui-open"] .message-head .pill').textContent === 'P0');
    await page.reload();
    await page.locator('#tab-processor-results').click();
    await card.waitFor();
    assert.equal(await card.getByLabel('优先级').inputValue(), 'P0');
    await page.evaluate(async () => {
      const task = (await api('/processor/tasks')).tasks.find(task => task.task_id === 'ui-open');
      await api('/processor/tasks/ui-open', 'PATCH', {priority:'P1', expected_revision:task.revision, request_id:crypto.randomUUID()});
    });
    await card.getByRole('button', {name:'标记完成', exact:true}).click();
    await page.waitForFunction(() => document.querySelector('[data-task-id="ui-open"] .task-action-feedback').textContent.includes('已发生变化'));
    assert.equal(await card.getByLabel('优先级').inputValue(), 'P1');
    assert.equal(await card.getByRole('button', {name:'标记完成', exact:true}).isEnabled(), true);

    await reviews.locator(':scope > summary').click();
    assert.equal(await reviews.locator(':scope > details').count(), 20);
    await reviews.locator(':scope > details').last().locator('summary').click();
    await page.evaluate(() => refreshProcessor());
    assert.equal(await reviews.evaluate(node => node.open), true);
    await reviews.locator(':scope > summary').click();
    const screenshots = process.env.CONSOLE_SCREENSHOTS;
    if (screenshots) {fs.mkdirSync(screenshots, {recursive:true}); await page.screenshot({path:path.join(screenshots,'processor-tasks-desktop.png'), fullPage:true});}
    await page.setViewportSize({width:390, height:844});
    await card.scrollIntoViewIfNeeded();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth+1), true);
    assert.equal(await card.getByLabel('优先级').isVisible(), true);
    if (screenshots) await page.screenshot({path:path.join(screenshots,'processor-tasks-mobile.png'), fullPage:true});
    assert.deepEqual(errors, []);
    assert.deepEqual(outbound, []);
    console.log('PASS: collapsed reviews, complete/reopen, priority, persistence, duplicate clicks, conflict recovery, debug/read-only tasks, focus and mobile layout');
  } finally {await browser.close();}
})().catch(error => {console.error(error); process.exitCode = 1;});
