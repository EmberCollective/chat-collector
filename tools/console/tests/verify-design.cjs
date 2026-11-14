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

const base = process.env.CONSOLE_TEST_URL;
assert.ok(base, 'Set CONSOLE_TEST_URL to the local synthetic fixture URL.');
const origin = new URL(base);
assert.ok(['127.0.0.1', 'localhost', '::1'].includes(origin.hostname), 'Only a loopback fixture is permitted.');
const root = path.resolve(__dirname, '../../..');
const approved = path.join(root, 'docs/design/console-redesign/index.html');
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

function safeName(s) { return s.replace(/[^a-z0-9-]/gi, '-').toLowerCase(); }
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
  const report = { generatedAt: new Date().toISOString(), fixture: origin.origin, approvedFile: approved, browser: 'Chrome', timezone: 'Asia/Shanghai', contentNotes: ['Overview API is replaced in the browser with the same synthetic overview snapshot used by verify-overview.cjs so its figures and charts are comparable.', 'Automation remains backed by the read-only fixture; its source count, sample rows and timestamps differ from the approved static demonstration and therefore contribute to raw pixel mismatch.'], comparisons: [] };
  try {
    const probe = await fetch(`${origin.origin}/__fixture__`, { signal: AbortSignal.timeout(5000) });
    assert.equal(probe.status, 200, 'Synthetic fixture health endpoint must return HTTP 200.');
    const fixtureInfo = await probe.json();
    assert.equal(fixtureInfo.synthetic, true, 'Refuse to capture anything except the explicit synthetic fixture.');
    const page = await browser.newPage({ timezoneId: 'Asia/Shanghai', locale: 'zh-CN', colorScheme: 'light', reducedMotion: 'reduce' });
    await page.route('**/*', route => {
      const url = route.request().url();
      if (/^https?:/.test(url) && new URL(url).origin !== origin.origin) {
        outbound.push(url);
        return route.abort();
      }
      return route.fallback();
    });
    await page.route('**/api/overview*', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(overviewSnapshot()) }));
    const outbound = [];
    page.on('dialog', dialog => dialog.dismiss());
    for (const device of devices) {
      await page.setViewportSize({ width: device.width, height: device.height });
      for (const state of states) {
        const stem = `${state.name}-${device.name}`;
        const designPage = await browser.newPage({ viewport: { width: device.width, height: device.height }, timezoneId: 'Asia/Shanghai', locale: 'zh-CN', colorScheme: 'light', reducedMotion: 'reduce' });
        await designPage.route('**/*', route => /^https?:/.test(route.request().url()) ? route.abort() : route.fallback());
        await go(designPage, fileUrl(approved, state.designHash), state);
        const designMetrics = await metrics(designPage);
        const designViewport = path.join(output, `${stem}-approved-viewport.png`);
        const designFull = path.join(output, `${stem}-approved-full.png`);
        await designPage.screenshot({ path: designViewport });
        await designPage.screenshot({ path: designFull, fullPage: true });
        await designPage.close();

        await go(page, `${origin.origin}/${state.hash}`, state);
        const productionMetrics = await metrics(page);
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
    fs.writeFileSync(path.join(output, 'report.json'), `${JSON.stringify(report, null, 2)}\n`);
    console.log(`Wrote visual evidence and report to ${output}`);
    console.log(JSON.stringify(report.comparisons.map(item => ({ state: item.state, device: item.device.name, pixels: item.pixels })), null, 2));
  } finally {
    await browser.close();
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
