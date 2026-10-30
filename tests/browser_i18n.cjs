// Real iframe bridge checks against our synthetic demo, without controlling Codex itself.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');

module.exports = async function verifyI18n(browser, base) {
  const html = await fs.readFile(path.join(__dirname, '../plugin/ui/monitor.html'), 'utf8');
  const page = await browser.newPage({ locale: 'zh-CN', viewport: { width: 1440, height: 1050 } });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  try {
    await page.route(base + '/__i18n_panel', route => route.fulfill({ contentType: 'text/html', body: html }));
    await page.route(base + '/__i18n_host', route => route.fulfill({ contentType: 'text/html', body: `
      <html lang="en-GB"><body><iframe src="/__i18n_panel" style="width:100%;height:1000px;border:0"></iframe>
      <script>
      window.toolCalls=0;
      addEventListener('message', async event => {
        if(event.source!==document.querySelector('iframe').contentWindow) return;
        const m=event.data;
        if(m.id===undefined) return;
        let result;
        if(m.method==='ui/initialize') result={hostContext:{locale:'en-GB',theme:'light'}};
        else if(m.method==='tools/call') {
          window.toolCalls++;
          const url=m.params.name==='get_request_history'
            ? '/api/requests?'+new URLSearchParams(m.params.arguments) : '/api/stats';
          result={structuredContent:await (await fetch(url)).json()};
        } else return;
        event.source.postMessage({jsonrpc:'2.0',id:m.id,result},location.origin);
      });
      </script></body></html>` }));
    await page.goto(base + '/__i18n_host');
    const frame = page.frames().find(frame => frame.url() === base + '/__i18n_panel');
    assert.ok(frame);
    await frame.waitForFunction(() => document.querySelectorAll('#requests .requestrow').length === 10);
    assert.equal(await frame.locator('html').getAttribute('lang'), 'en-GB', 'Host should override a Chinese browser');
    assert.equal(await frame.locator('.overview-label h2').textContent(), 'Usage overview');
    await frame.locator('[data-period="7d"]').click();
    await frame.waitForFunction(() => document.querySelector('#historyCard').getAttribute('aria-busy') === 'false');
    await frame.locator('#nextPage').click();
    await frame.waitForFunction(() => document.querySelector('#pageInfo').textContent === 'Page 2');
    await frame.locator('#modelBreakdown > summary').click();
    await frame.locator('.model-pricing > summary').first().click();
    const ids = await frame.locator('#requests > li').evaluateAll(rows => rows.map(row => row.dataset.key));
    const total = await frame.locator('#total').textContent();
    const calls = await page.evaluate(() => window.toolCalls);
    const hostLocale = locale => page.evaluate(locale => document.querySelector('iframe').contentWindow.postMessage({
      jsonrpc: '2.0', method: 'ui/notifications/host-context-changed', params: { locale },
    }, location.origin), locale);
    await hostLocale('zh-CN');
    await frame.waitForFunction(() => document.querySelector('#pageInfo').textContent === '第 2 页');
    assert.equal(await frame.locator('.model-cost dt').first().textContent(), 'API 参考费用');
    assert.deepEqual(await frame.locator('#requests > li').evaluateAll(rows => rows.map(row => row.dataset.key)), ids);
    assert.equal(await frame.locator('#total').textContent(), total);
    assert.equal(await frame.locator('#modelBreakdown').evaluate(el => el.open), true);
    assert.equal(await frame.locator('.model-pricing').first().evaluate(el => el.open), true);
    assert.equal(await page.evaluate(() => window.toolCalls), calls, 'Language change should not fetch or reset history');

    await frame.locator('#language').selectOption('en');
    await hostLocale('zh-TW');
    assert.equal(await frame.locator('html').getAttribute('lang'), 'en-US', 'Manual selection should win');
    await frame.locator('#language').selectOption('auto');
    assert.equal(await frame.locator('html').getAttribute('lang'), 'zh-CN', 'Auto should resume the latest host language');
    // Exercise the officially documented document.lang mirror, including repeated changes.
    for (const [locale, expected] of [['en-GB', 'Usage overview'], ['zh-CN', '用量概览'], ['fr-FR', 'Usage overview']]) {
      await frame.evaluate(value => { document.documentElement.lang = value; }, locale);
      await frame.waitForFunction(value => document.querySelector('.overview-label h2').textContent === value, expected);
    }
    await frame.locator('#methodButton').click();
    assert.ok((await frame.locator('#method').textContent()).includes('Beijing time (UTC+8)'));
    assert.equal(await frame.locator('#pageInfo').textContent(), 'Page 2');
    assert.equal(await frame.locator('#refresh').getAttribute('aria-label'), 'Refresh now');
    assert.equal(await frame.locator('#nextPage').textContent(), 'Next');

    // Both languages must keep the compact date group on the card's top-right.
    for (const language of ['en', 'zh']) {
      await frame.locator('#language').selectOption(language);
      for (const width of [320, 360, 390, 768]) {
        await page.locator('iframe').evaluate((el, width) => { el.style.width = width + 'px'; }, width);
        await frame.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
        const layout = await frame.evaluate(() => {
          const title = document.querySelector('.overview-label h2').getBoundingClientRect();
          const group = document.querySelector('.periods').getBoundingClientRect();
          return { overflow: document.documentElement.scrollWidth > innerWidth,
            right: group.left >= title.right, aligned: Math.abs(group.top + group.height / 2 - title.top - title.height / 2) < 2 };
        });
        assert.deepEqual(layout, { overflow: false, right: true, aligned: true }, language + ' at ' + width);
      }
    }
    assert.deepEqual(errors, []);
  } finally { await page.close(); }

  const legacy = await browser.newPage({ locale: 'en-US' });
  try {
    await legacy.route(base + '/__legacy_panel', route => route.fulfill({ contentType: 'text/html', body: html.replace(
      '<script>\n\'use strict\';', `<script>
      window.openai={locale:'zh-CN',widgetState:{extra:'preserve'},
        setWidgetState(state){this.widgetState=state},
        async callTool(name,args){const url=name==='get_request_history'?'/api/requests?'+new URLSearchParams(args):'/api/stats';
          return {structuredContent:await (await fetch(url)).json()}}};
      </script><script>\n'use strict';`) }));
    await legacy.route(base + '/__legacy_host', route => route.fulfill({ contentType: 'text/html', body: `
      <iframe src="/__legacy_panel"></iframe><script>
      addEventListener('message',event=>{if(event.data.method==='ui/initialize')event.source.postMessage({jsonrpc:'2.0',id:event.data.id,error:{code:-32601,message:'Use legacy bridge'}},location.origin)});
      </script>` }));
    await legacy.goto(base + '/__legacy_host');
    const frame = legacy.frames().find(frame => frame.url().endsWith('/__legacy_panel'));
    await frame.waitForFunction(() => document.querySelectorAll('#requests .requestrow').length === 10);
    assert.equal(await frame.locator('html').getAttribute('lang'), 'zh-CN');
    await frame.evaluate(() => {
      window.openai.locale = 'en-US';
      dispatchEvent(new CustomEvent('openai:set_globals', { detail: { globals: { locale: 'en-US' } } }));
    });
    assert.equal(await frame.locator('.overview-label h2').textContent(), 'Usage overview');
    await frame.locator('#language').selectOption('zh');
    assert.equal(await frame.evaluate(() => window.openai.widgetState.extra), 'preserve');
    assert.equal(await frame.evaluate(() => window.openai.widgetState.language), 'zh');
  } finally { await legacy.close(); }
};
