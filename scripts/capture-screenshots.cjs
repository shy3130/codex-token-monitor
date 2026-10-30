#!/usr/bin/env node
// Optional maintainer tool. Starts its own synthetic demo on a temporary port.
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const { createHash } = require('node:crypto');
const { once } = require('node:events');
const fs = require('node:fs/promises');
const net = require('node:net');
const path = require('node:path');
const { setTimeout: delay } = require('node:timers/promises');

const root = path.resolve(__dirname, '..');
const output = path.join(root, 'docs/screenshots');

async function freePort() {
  const socket = net.createServer();
  socket.listen(0, '127.0.0.1');
  await once(socket, 'listening');
  const { port } = socket.address();
  await new Promise((resolve, reject) => socket.close(err => err ? reject(err) : resolve()));
  return port;
}

async function main() {
  const version = JSON.parse(await fs.readFile(path.join(root, 'plugin/plugin.json'), 'utf8')).version;
  const uiSource = await fs.readFile(path.join(root, 'plugin/ui/monitor.html'));
  const files = {
    overviewLight: `dashboard-v${version}-overview-light.png`,
    dashboardLight: `dashboard-v${version}-light.png`,
    dashboardDark: `dashboard-v${version}-dark.png`,
    overviewDark: `dashboard-v${version}-overview-dark.png`,
    mobile: `dashboard-v${version}-mobile.png`,
    modelsLight: `models-v${version}-light.png`,
    modelsDark: `models-v${version}-dark.png`,
    modelsMobile: `models-v${version}-mobile.png`,
  };
  let chromium;
  try {
    ({ chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright'));
  } catch {
    throw new Error('Playwright is required for screenshots. See docs/screenshots/README.md.');
  }
  const port = await freePort();
  const base = `http://127.0.0.1:${port}`;
  const demo = spawn(process.env.PYTHON || 'python3',
    [path.join(root, 'scripts/demo.py'), '--port', String(port)],
    { cwd: root, env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' },
      stdio: ['ignore', 'ignore', 'pipe'] });
  let browser, diagnostic = '', childError;
  demo.stderr.on('data', chunk => { diagnostic += chunk; });
  demo.on('error', error => { childError = error; });
  try {
    let ready = false;
    for (let attempt = 0; attempt < 150; attempt++) {
      if (childError) throw childError;
      if (demo.exitCode !== null) throw new Error(`Demo exited: ${diagnostic}`);
      try {
        const response = await fetch(base + '/health', { signal: AbortSignal.timeout(1000) });
        if (response.ok && (await response.json()).ok) { ready = true; break; }
      } catch { /* Wait for this child process to open its loopback port. */ }
      await delay(100);
    }
    assert.ok(ready, 'Demo did not start within 15 seconds');
    const options = { headless: true };
    if (process.env.SCREENSHOT_BROWSER_CHANNEL) options.channel = process.env.SCREENSHOT_BROWSER_CHANNEL;
    browser = await chromium.launch(options);
    const page = await browser.newPage({ viewport: { width: 1440, height: 1050 },
      deviceScaleFactor: 1, colorScheme: 'light', locale: 'zh-CN', timezoneId: 'Asia/Shanghai' });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(base);
    await page.waitForFunction(() => document.querySelectorAll('#requests .requestrow').length === 10);
    await page.evaluate(() => document.fonts.ready);
    assert.equal(await page.locator('.build-version').textContent(), `v${version}`);
    assert.equal((await (await page.request.get(base + '/health')).json()).version, version);
    assert.equal(await page.locator('#modelBreakdown').evaluate(el => el.open), false);
    assert.equal(await page.locator('#models .modelrow').count(), 3);
    const positions = await page.evaluate(() => ({
      primaryRight: document.querySelector('.primary').getBoundingClientRect().right,
      historyLeft: document.querySelector('#historyCard').getBoundingClientRect().left,
    }));
    assert.ok(positions.historyLeft > positions.primaryRight, 'Desktop should have two columns');
    const screenshot = async name => {
      await page.locator(':focus').evaluateAll(elements => elements.forEach(element => element.blur()));
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false,
        'Page should fit the viewport');
      const header = await page.evaluate(() => {
        const heading = document.querySelector('.overview-label h2').getBoundingClientRect();
        const periods = document.querySelector('.overview-label .periods').getBoundingClientRect();
        return { aligned: Math.abs((heading.top + heading.height / 2)
          - (periods.top + periods.height / 2)) < 2,
          onRight: periods.left > heading.right, height: periods.height, width: periods.width };
      });
      assert.ok(header.aligned && header.onRight, 'Period buttons should sit beside the title on the right');
      assert.ok(header.height <= 34 && header.width < 220, 'Period buttons should form a compact group');
      // Capture in one surface; some Chrome builds repeat viewport tiles in full-page screenshots.
      const viewport = page.viewportSize();
      const height = await page.evaluate(() => document.documentElement.scrollHeight);
      await page.setViewportSize({ width: viewport.width, height });
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      await page.screenshot({ path: path.join(output, name), animations: 'disabled' });
      await page.setViewportSize(viewport);
    };
    await fs.mkdir(output, { recursive: true });
    await screenshot(files.overviewLight);

    await page.locator('#nextPage').click();
    await page.waitForFunction(() => document.querySelector('#pageInfo').textContent === '第 2 页');
    await page.locator('#refresh').click();
    await page.waitForFunction(() => !document.querySelector('#refresh').classList.contains('busy'));
    assert.equal(await page.locator('#pageInfo').textContent(), '第 2 页');

    await page.locator('[data-period="7d"]').click();
    await page.waitForFunction(() => !document.querySelector('#historyCard').matches('[aria-busy="true"]')
      && document.querySelector('#pageInfo').textContent === '第 1 页');
    await page.locator('#modelBreakdown > summary').click();
    assert.equal(await page.locator('#models').isVisible(), true);
    const stats = await (await page.request.get(base + '/api/stats')).json();
    for (const model of stats.periods['7d'].models) {
      const row = page.locator('#models > .modelrow').filter({ has: page.getByRole('heading', { name: model.model, exact: true }) });
      assert.equal(await row.locator('.model-tokens > dd').first().getAttribute('title'),
        new Intl.NumberFormat('zh-CN').format(model.total) + ' tokens');
      assert.equal(await row.locator('.model-speed > dd').first().textContent(),
        model.speed_summary.tps.toFixed(1) + 't/s');
      assert.ok((await row.locator('.model-cost > dd').first().textContent()).startsWith('$'));
    }
    await screenshot(files.dashboardLight);
    await page.locator('#modelBreakdown').screenshot({ path: path.join(output, files.modelsLight), animations: 'disabled' });
    const firstPrice = page.locator('.model-pricing').first();
    await firstPrice.locator('summary').click();
    await page.evaluate(data => {
      data.periods['7d'].models[0].requests += 1;
      render(data);
    }, stats);
    assert.equal(await firstPrice.evaluate(el => el.open), true, 'Price notes should remain open when model stats change');
    await firstPrice.locator('summary').click();
    await page.evaluate(data => render(data), stats);
    await page.emulateMedia({ colorScheme: 'dark' });
    await page.locator('#modelBreakdown').screenshot({ path: path.join(output, files.modelsDark), animations: 'disabled' });
    await screenshot(files.dashboardDark);

    await page.locator('#modelBreakdown > summary').focus();
    await page.keyboard.press('Enter');
    assert.equal(await page.locator('#modelBreakdown').evaluate(el => el.open), false);
    await screenshot(files.overviewDark);
    await page.locator('[data-period="today"]').click();
    await page.waitForFunction(() => !document.querySelector('#historyCard').matches('[aria-busy="true"]'));
    await page.emulateMedia({ colorScheme: 'light' });
    for (const width of [320, 360, 390, 768]) {
      await page.setViewportSize({ width, height: 844 });
      await page.locator('#modelBreakdown').evaluate(el => { el.open = true; });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false,
        `Page should fit at ${width}px`);
      assert.ok(await page.locator('.overview-label').evaluate(el => {
        const title = el.querySelector('h2').getBoundingClientRect();
        const nav = el.querySelector('.periods').getBoundingClientRect();
        return nav.left >= title.right && Math.abs(nav.top + nav.height / 2 - title.top - title.height / 2) < 2;
      }), `Title and period buttons should share one row at ${width}px`);
    }
    await page.setViewportSize({ width: 390, height: 844 });
    await page.locator('#modelBreakdown').screenshot({ path: path.join(output, files.modelsMobile), animations: 'disabled' });
    await page.locator('#modelBreakdown').evaluate(el => { el.open = false; });
    await page.locator('#refresh').focus();
    await page.locator('#refresh').evaluate(el => el.blur());
    await page.evaluate(() => scrollTo(0, 0));
    await screenshot(files.mobile);
    assert.deepEqual(errors, [], 'Dashboard should have no browser script errors');
    assert.deepEqual(await fs.readFile(path.join(root, 'plugin/ui/monitor.html')), uiSource,
      'UI source must not change while screenshots are being captured');
    const images = {};
    for (const name of Object.values(files)) {
      const data = await fs.readFile(path.join(output, name));
      images[name] = { width: data.readUInt32BE(16), height: data.readUInt32BE(20),
        sha256: createHash('sha256').update(data).digest('hex') };
    }
    await fs.writeFile(path.join(output, 'manifest.json'), JSON.stringify({ version,
      capturedAt: new Date().toISOString(), data: 'synthetic',
      uiSource: { path: 'plugin/ui/monitor.html', sha256: createHash('sha256').update(uiSource).digest('hex') },
      images }, null, 2) + '\n');
    console.log(`OK: eight screenshots of v${version}; per-model metrics, responsive layout, pagination, refresh and keyboard checks`);
    console.log(`Saved to ${output}`);
  } finally {
    try {
      if (browser) await browser.close();
    } finally {
      if (demo.exitCode === null && !childError) {
        const exited = once(demo, 'exit');
        demo.kill('SIGTERM');
        await exited;
      }
    }
  }
}

main().catch(error => { console.error(error.message); process.exitCode = 1; });
