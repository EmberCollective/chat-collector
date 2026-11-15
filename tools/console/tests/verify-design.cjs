'use strict';

// Side-by-side visual evidence for the approved static prototype and the
// production console backed by the local synthetic fixture. This is an
// observational report; pixel differences are not an automated pass/fail.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { pathToFileURL } = require('node:url');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const overviewSnapshot = require('./overview-snapshot.cjs');
const runtimeSnapshot = require('./runtime-monitor-snapshot.cjs');

const base = process.env.CONSOLE_TEST_URL;
assert.ok(base, 'Set CONSOLE_TEST_URL to the local synthetic fixture URL.');
const origin = new URL(base);
assert.ok(['127.0.0.1', 'localhost', '::1'].includes(origin.hostname), 'Only a loopback fixture is permitted.');
const root = path.resolve(__dirname, '../../..');
const approved = path.join(root, 'docs/design/runtime-monitoring/index.html');
const output = path.resolve(process.env.DESIGN_PARITY_OUTPUT || path.join(root, '.runtime/design-parity'));
const compareHelper = path.join(__dirname, 'compare-design-images.py');
const executablePath = process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const devices = [{ name: 'desktop', width: 1440, height: 1000 }, { name: 'mobile', width: 390, height: 844 }];
const states = [
  { name: 'overview', hash: '#overview', designHash: '#overview', panel: null },
  { name: 'automation-collection', hash: '#automation/collection', designHash: '#automation/collection', panel: '#automation-collection' },
  { name: 'automation-channels', hash: '#automation/channels', designHash: '#automation/channels', panel: '#automation-channels' },
  { name: 'automation-background', hash: '#automation/background', designHash: '#automation/background', panel: '#automation-background' },
];
const liveOverview = process.env.DESIGN_PARITY_LIVE_OVERVIEW === '1';
const matchApproved = process.env.DESIGN_PARITY_MATCH_APPROVED === '1';

async function fulfillJson(route, transform) {
  if (route.request().method() !== 'GET') return route.fallback();
  const response = await route.fetch();
  if (!response.ok()) return route.fulfill({ response });
  const data = await response.json();
  transform(data);
  return route.fulfill({ response, body: JSON.stringify(data) });
}

function fileUrl(file, hash) { return `${pathToFileURL(file).href}${hash}`; }

async function go(page, url, state) {
  await page.goto(url, { waitUntil: 'networkidle' });
  await page.waitForFunction(() => document.readyState === 'complete');
  if (state.panel) await page.locator(state.panel).waitFor({ state: 'visible' });
  else await page.locator('#view-overview').waitFor({ state: 'visible' });
  // Allow layout/fonts and the fixture-driven initial data render to settle.
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
}

async function metrics(page) {
  return page.evaluate(() => {
    const selectors = [
      ['sidebar', '#sidebar, .sidebar'],
      ['topbar', '#topbar, .topbar'],
      ['main', 'main, #main'],
      ['overview', '#view-overview'],
      ['automation', '#view-automation'],
      ['automation-status', '.automation-status'],
      ['automation-tabs', '.automation-tabs, [role="tablist"]'],
      ['automation-panel', '#automation-collection:visible, #automation-channels:visible, #automation-background:visible'],
      ['savebar', '.automation-savebar'],
    ];
    const rect = el => {
      const r = el.getBoundingClientRect();
      const s = getComputedStyle(el);
      return { x: +r.x.toFixed(1), y: +r.y.toFixed(1), width: +r.width.toFixed(1), height: +r.height.toFixed(1), display: s.display, position: s.position, padding: s.padding, gap: s.gap };
    };
    const result = {};
    for (const [name, selector] of selectors) {
      // :visible is a Playwright pseudo selector, so use a CSS-only selector
      // for panel candidates and choose the first currently rendered element.
      const candidates = selector.includes(':visible') ? [...document.querySelectorAll('#automation-collection,#automation-channels,#automation-background')] : [...document.querySelectorAll(selector)];
      const el = candidates.find(node => node.getClientRects().length && getComputedStyle(node).visibility !== 'hidden');
      result[name] = el ? rect(el) : null;
    }
    return { viewport: { width: innerWidth, height: innerHeight }, document: { width: document.documentElement.scrollWidth, height: document.documentElement.scrollHeight }, elements: result };
  });
}

async function main() {
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ executablePath, headless: true, args: ['--force-color-profile=srgb'] });
  const contentNotes = [liveOverview ? 'Overview uses the synthetic fixture API response.' : 'Overview API is replaced in the browser with the synthetic snapshot used by verify-overview.cjs.'];
  contentNotes.push(matchApproved ? 'For automation screenshots only, GET catalog/config/status responses are transformed in memory to show the approved three source rows and three target examples, enable demo schedules and listener, and report a running service. Real source IDs are preserved so normal fixture config requests still resolve. Fixture files are not changed; functional suites exercise the unmodified fixture. Operational wording, IDs, health checks and startup support still differ from the prototype.' : 'Automation uses the raw synthetic fixture; its source count, sample rows, channel targets, status, and timestamps differ from the approved static demonstration and contribute to raw pixel mismatch.');
  const report = { generatedAt: new Date().toISOString(), fixture: origin.origin, approvedFile: approved, browser: 'Chrome', timezone: 'Asia/Shanghai', contentNotes, comparisons: [] };
  try {
    const probe = await fetch(`${origin.origin}/__fixture__`, { signal: AbortSignal.timeout(5000) });
    assert.equal(probe.status, 200, 'Synthetic fixture health endpoint must return HTTP 200.');
    const fixtureInfo = await probe.json();
    assert.equal(fixtureInfo.synthetic, true, 'Refuse to capture anything except the explicit synthetic fixture.');
    const page = await browser.newPage({ timezoneId: 'Asia/Shanghai', locale: 'zh-CN', colorScheme: 'light', reducedMotion: 'reduce' });
    const pageErrors = [], outbound = [];
    page.on('pageerror', error => pageErrors.push(error.message));
    await page.route('**/*', route => {
      const url = route.request().url();
      if (/^https?:/.test(url) && new URL(url).origin !== origin.origin) {
        outbound.push(url);
        return route.abort();
      }
      return route.fallback();
    });
    if (!liveOverview) await page.route('**/api/overview*', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(overviewSnapshot()) }));
    if (!liveOverview) await page.route('**/api/runtime-monitor',route=>route.fulfill({json:runtimeSnapshot()}));
    if (matchApproved) {
      await page.route('**/api/catalog', route => fulfillJson(route, catalog => {
        catalog.sources = (catalog.sources || []).filter(source => source.id !== 'qq-work');
        for (const source of catalog.sources) {
          if (source.id === 'external-example') source.name = '外部信息源 · 接入示例';
        }
      }));
      await page.route('**/api/operations/config', route => fulfillJson(route, config => {
        config.automation.enabled = true;
        for (const id of ['wechat', 'qq', 'processor', 'messaging', 'feedback']) if (config.automation.jobs?.[id]) {
          config.automation.jobs[id].enabled = true;
          if (id === 'wechat' || id === 'qq') config.automation.jobs[id].interval_seconds = 60;
        }
        if (config.automation.listener) config.automation.listener.enabled = true;
        config.messaging.enabled = true;
        config.messaging.feedback_enabled = true;
        config.messaging.deadline_reminders.enabled = true;
        config.messaging.deadline_reminders.P1 = [3600, 900, 0];
        config.messaging.targets = [
          { id: '产品协作群', kind: 'feishu_app', enabled: true, timeout_seconds: 30, app_id_env: 'FEISHU_APP_ID', app_secret_env: 'FEISHU_APP_SECRET', recipient_id: 'oc_demo_product_group', receive_id_type: 'chat_id', allowed_operator_ids: ['ou_demo_operator'], snooze_seconds: 3600, upload_files: true, max_file_bytes: 30000000 },
          { id: '项目待办资料库', kind: 'notion', enabled: false, timeout_seconds: 30, token_env: 'NOTION_TOKEN', data_source_id: 'demo-project-source', properties: { title: '名称', status: '状态' } },
          { id: '团队工作流', kind: 'webhook', enabled: false, timeout_seconds: 30, url_env: 'TEAM_WEBHOOK_URL', token_env: 'TEAM_WEBHOOK_TOKEN', mode: 'sync', purpose: '同步正式待办' },
        ];
        const feishu = config.messaging.types?.feishu_app;
        if (feishu) {
          feishu.name = '飞书应用 · feishu-assistant';
          if (feishu.capabilities) Object.assign(feishu.capabilities, { send: false, test: false, files: true, feedback: true, listener: true, recovery: false });
        }
      }));
      await page.route('**/api/operations/status', route => fulfillJson(route, status => {
        status.service = { running: true, state: 'running', verified: true };
        status.automation.enabled = true;
        status.messaging.enabled = true;
        status.feedback.enabled = true;
        if (status.checks) {
          status.checks.enabled = true;
          for (const id of ['wechat', 'qq', 'processor', 'messaging', 'feedback']) if (status.checks.jobs?.[id]) Object.assign(status.checks.jobs[id], { enabled: true, ready: true, reason: '' });
        }
      }));
    }
    page.on('dialog', dialog => dialog.dismiss());
    for (const device of devices) {
      await page.setViewportSize({ width: device.width, height: device.height });
      for (const state of states) {
        const stem = `${state.name}-${device.name}`;
        const designPage = await browser.newPage({ viewport: { width: device.width, height: device.height }, timezoneId: 'Asia/Shanghai', locale: 'zh-CN', colorScheme: 'light', reducedMotion: 'reduce' });
        await designPage.route('**/*', route => /^https?:/.test(route.request().url()) ? route.abort() : route.fallback());
        await go(designPage, fileUrl(approved, state.designHash), state);
        if (state.name === 'overview') {
          await designPage.locator('#kpis .metric').first().waitFor();
          assert.equal(await designPage.locator('#kpis .metric').count(), 3, 'Approved overview must render all three KPI cards.');
        }
        const designMetrics = await metrics(designPage);
        const designViewport = path.join(output, `${stem}-approved-viewport.png`);
        const designFull = path.join(output, `${stem}-approved-full.png`);
        await designPage.screenshot({ path: designViewport });
        await designPage.screenshot({ path: designFull, fullPage: true });
        await designPage.close();

        await go(page, `${origin.origin}/${state.hash}`, state);
        if (state.name === 'overview') {
          await page.locator('#kpis .metric').first().waitFor();
          assert.equal(await page.locator('#kpis .metric').count(), 3, 'Production overview must render all three KPI cards.');
        } else if (state.name === 'automation-collection') {
          await page.waitForFunction(() => document.querySelectorAll('#automation-collection .automation-source-row').length >= 3);
          if (matchApproved) assert.equal(await page.locator('#automation-collection .automation-source-row').count(), 3, 'Matched automation screenshot should show the approved three catalog rows.');
        }
        const productionMetrics = await metrics(page);
        if (matchApproved && state.name !== 'overview') {
          for (const name of ['automation-status', 'automation-tabs', 'automation-panel']) {
            for (const dimension of ['x', 'y', 'width']) assert.ok(Math.abs(productionMetrics.elements[name][dimension] - designMetrics.elements[name][dimension]) < 1, `${stem}: ${name}.${dimension} differs from approval`);
          }
          if (state.name === 'automation-collection') assert.ok(Math.abs(productionMetrics.elements['automation-panel'].height - designMetrics.elements['automation-panel'].height) < 1, `${stem}: collection panel height differs from approval`);
        }
        const productionViewport = path.join(output, `${stem}-production-viewport.png`);
        const productionFull = path.join(output, `${stem}-production-full.png`);
        await page.screenshot({ path: productionViewport });
        await page.screenshot({ path: productionFull, fullPage: true });
        const viewportDiff = path.join(output, `${stem}-viewport-diff.png`);
        const fullDiff = path.join(output, `${stem}-full-diff.png`);
        const comparison = spawnSync(process.env.PYTHON || 'python', [compareHelper, designViewport, productionViewport, designFull, productionFull, viewportDiff, fullDiff], { encoding: 'utf8' });
        assert.equal(comparison.status, 0, `Image comparison helper failed: ${comparison.stderr}`);
        report.comparisons.push({ state: state.name, device, design: designMetrics, production: productionMetrics, pixels: JSON.parse(comparison.stdout) });
      }
    }
    assert.deepEqual(outbound, [], `Unexpected external requests: ${outbound.join(', ')}`);
    assert.deepEqual(pageErrors, [], `Unexpected production page errors: ${pageErrors.join('; ')}`);
    fs.writeFileSync(path.join(output, 'report.json'), `${JSON.stringify(report, null, 2)}\n`);
    console.log(`Wrote visual evidence and report to ${output}`);
    console.log(JSON.stringify(report.comparisons.map(item => ({ state: item.state, device: item.device.name, pixels: item.pixels })), null, 2));
  } finally {
    await browser.close();
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
