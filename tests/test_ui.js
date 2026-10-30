// Exercise refresh recovery using the real page script and a stalled local service.
// Run with: node --test tests/test_ui.js
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../plugin/ui/monitor.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const flush = () => new Promise(setImmediate);

function boot(fetch, options = {}) {
  const timers = new Map(), elements = new Map();
  const windowEvents = new Map();
  let timerId = 0;
  function element() {
    const classes = new Set(), attrs = new Map(), child = { textContent: '' };
    const node = {
      children: [], dataset: {},
      textContent: '',
      setAttribute: (key, value) => attrs.set(key, value),
      getAttribute: key => attrs.get(key),
      classList: { add: value => classes.add(value), remove: value => classes.delete(value),
        contains: value => classes.has(value), toggle(value, force) {
          const enabled = force === undefined ? !classes.has(value) : force;
          if (enabled) classes.add(value); else classes.delete(value);
          return enabled;
        } },
      querySelector: () => child,
      addEventListener() {},
      insertBefore(item, next) {
        const previous = this.children.indexOf(item);
        if (previous !== -1) this.children.splice(previous, 1);
        const index = next ? this.children.indexOf(next) : this.children.length;
        this.children.splice(index, 0, item);
        item.remove = () => this.children.splice(this.children.indexOf(item), 1);
      },
    };
    return node;
  }
  const document = { hidden: false, querySelectorAll: () => [], addEventListener() {},
    documentElement: { lang: options.documentLang || '', dataset: {} },
    createElement: () => element(),
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, element());
      return elements.get(id);
    } };
  const window = { openai: options.openai,
    addEventListener: (name, handler) => windowEvents.set(name, handler) };
  window.parent = window;
  const context = vm.createContext({ window, document, fetch, AbortController, URLSearchParams,
    navigator: { language: options.locale || 'zh-CN' },
    location: { hostname: '127.0.0.1' },
    setTimeout(fn, delay) { const id = ++timerId; timers.set(id, { fn, delay }); return id; },
    clearTimeout: id => timers.delete(id) });
  vm.runInContext(script, context, { filename: 'monitor.html' });
  return { context, elements, timers, document,
    event: (name, event) => windowEvents.get(name)?.(event),
    fire(delay) {
    const found = [...timers].find(([, timer]) => timer.delay === delay);
    assert.ok(found, `Expected a ${delay} ms timer`);
    timers.delete(found[0]);
    found[1].fn();
  } };
}

test('a stalled local service times out, releases refresh and retries', async () => {
  let calls = 0;
  const app = boot((_, options) => {
    calls++;
    return new Promise((resolve, reject) => {
      options.signal?.addEventListener('abort', () => reject(new Error('aborted')), { once: true });
    });
  });
  assert.equal(calls, 1);
  assert.equal(app.elements.get('refresh').classList.contains('busy'), true);
  app.fire(7000);
  await flush();
  assert.equal(app.elements.get('refresh').classList.contains('busy'), false);
  assert.match(app.elements.get('liveState').textContent, /响应超时/);
  app.fire(5000);
  assert.equal(calls, 2);
  app.fire(7000);
  await flush();
  assert.ok([...app.timers.values()].some(timer => timer.delay === 10000));
});

test('a backend error keeps refresh usable and schedules another attempt', async () => {
  const app = boot(async () => ({ ok: false }));
  await flush();
  assert.equal(app.elements.get('refresh').classList.contains('busy'), false);
  assert.match(app.elements.get('liveState').textContent, /本地服务连接失败/);
  assert.ok([...app.timers.values()].some(timer => timer.delay === 5000));
});

test('history can move past the first page and return to the latest page', async () => {
  const seen = [];
  const app = boot(async url => {
    if (url === '/api/stats') return new Promise(() => {});
    const second = new URL(url, 'http://localhost').searchParams.has('cursor');
    return { ok: true, json: async () => ({ records: [{ id: second ? 'older' : 'newer' }],
      total: 35, next_cursor: second ? null : 'cursor-next' }) };
  });
  app.context.reconcile = (id, rows) => {
    if (id !== 'requests' || !rows.length) return;
    const ids = rows.map(r => r.id);
    if (JSON.stringify(seen.at(-1)) !== JSON.stringify(ids)) seen.push(ids);
  };
  await app.context.loadHistory();
  assert.equal(app.elements.get('nextPage').disabled, false);
  await app.context.loadHistory(1);
  assert.equal(app.elements.get('pageInfo').textContent, '第 2 页');
  assert.equal(app.elements.get('nextPage').disabled, true);
  assert.equal(app.elements.get('prevPage').disabled, false);
  await app.context.loadHistory(0);
  assert.deepEqual(seen, [['newer'], ['older'], ['newer']]);
});

test('a slow history response cannot overwrite a newly selected period', async () => {
  let finishOld;
  const app = boot(async url => {
    if (url === '/api/stats') return new Promise(() => {});
    if (url.includes('period=today')) return new Promise(resolve => { finishOld = resolve; });
    return { ok: true, json: async () => ({ records: [{ id: 'week' }], total: 1, next_cursor: null }) };
  });
  let current;
  app.context.reconcile = (id, rows) => { if (id === 'requests') current = rows.map(r => r.id).join(','); };
  const old = app.context.loadHistory();
  vm.runInContext("selected='7d';resetHistory()", app.context);
  await app.context.loadHistory();
  finishOld({ ok: true, json: async () => ({ records: [{ id: 'stale' }], total: 1, next_cursor: null }) });
  await old;
  assert.equal(current, 'week');
});

test('effort badges show only the setting and escape model names', () => {
  const app = boot(async () => new Promise(() => {}));
  const markup = app.context.requestMarkup({ model: '<test>', effort: 'xhigh', ts: 1793239200,
    output: 100, duration_ms: 2000, tps: 50 });
  assert.match(markup, />XHIGH<\/span>/);
  assert.doesNotMatch(markup, /思考 ·/);
  assert.match(markup, /&lt;test&gt;/);
});

test('model metrics show missing timing and pricing without inventing zero values', () => {
  const app = boot(async () => new Promise(() => {}));
  const markup = app.context.modelMarkup({ model: '<unknown>', requests: 1,
    total: 1200, input: 1000, output: 200, cached: 800, reasoning: 50,
    token_share: 1, speed_summary: { samples: 0, tps: null },
    api_estimate: { dollars: null } });
  assert.match(markup, /&lt;unknown&gt;/);
  assert.match(markup, /暂无完整计时/);
  assert.match(markup, /价格待确认/);
  assert.match(markup, /未计入参考合计/);
  assert.match(markup, /占总量 100\.0%/);
  assert.doesNotMatch(markup, /\$0\.00|>0\.0<small>/);
});

test('small model costs retain precision and missing backend fields degrade clearly', () => {
  const app = boot(async () => new Promise(() => {}));
  const markup = app.context.modelMarkup({ model: 'test', requests: 1, total: 100,
    input: 90, output: 10, cached: 0, reasoning: 0,
    api_estimate: { dollars: 0.00001, provider: '<vendor>',
      input_per_million: 1, cached_per_million: 0.1, output_per_million: 5 } });
  assert.match(markup, /&lt;\$0\.0001/);
  assert.match(markup, /&lt;vendor&gt;/);
  assert.match(markup, /暂无完整计时/);
  assert.doesNotMatch(markup, /NaN|undefined/);
});

test('English browser locale translates model details, request metadata and empty timing', () => {
  const app = boot(async () => new Promise(() => {}), { locale: 'en-GB' });
  const model = app.context.modelMarkup({ model: '<unknown>', requests: 1, total: 1200,
    input: 1000, output: 200, cached: 800, reasoning: 50, token_share: 1 });
  assert.match(model, /Total tokens/);
  assert.match(model, /1 request<\/span>/);
  assert.match(model, /100.0% of total/);
  assert.match(model, /No complete timings/);
  assert.match(model, /Price unavailable/);
  assert.doesNotMatch(model, /\p{Script=Han}/u);
  const request = app.context.requestMarkup({ ts: 1793239200, output: 1000 });
  assert.match(request, /Not recorded/);
  assert.match(request, /Output 1,000/);
  assert.match(request, /Incomplete timing/);
  assert.doesNotMatch(request, /\p{Script=Han}/u);
  assert.equal(app.document.documentElement.lang, 'en-GB');
});

test('host language wins over browser, manual choice wins over host and auto resumes it', () => {
  const app = boot(async () => new Promise(() => {}));
  app.context.applyHostContext({ locale: 'en-US', theme: 'dark' });
  assert.equal(app.document.documentElement.lang, 'en-US');
  assert.equal(app.document.documentElement.dataset.theme, 'dark');
  app.context.setLanguage('zh');
  app.context.applyHostContext({ locale: 'en-GB' });
  assert.equal(app.document.documentElement.lang, 'zh-CN');
  app.context.setLanguage('auto');
  assert.equal(app.document.documentElement.lang, 'en-GB');
  // Partial theme updates must not erase the known host language.
  app.context.applyHostContext({ theme: 'light' });
  assert.equal(app.document.documentElement.lang, 'en-GB');
});

test('legacy host locale and updates are supported; unsupported languages fall back to English', () => {
  const app = boot(async () => new Promise(() => {}),
    { locale: 'zh-CN', openai: { locale: 'fr-FR' } });
  assert.equal(app.document.documentElement.lang, 'en-US');
  app.event('openai:set_globals', { detail: { globals: { locale: 'zh-TW' } } });
  assert.equal(app.document.documentElement.lang, 'zh-CN');
  app.context.applyHostContext({ locale: '<invalid>' });
  assert.equal(app.document.documentElement.lang, 'zh-CN');
});

test('tool locale is a fallback hint and cannot replace an explicit host language', () => {
  const app = boot(async () => new Promise(() => {}));
  app.context.readToolResult({ _meta: { 'token-monitor/locale': 'en-US' } });
  assert.equal(app.document.documentElement.lang, 'en-US');
  app.context.applyHostContext({ locale: 'zh-CN' });
  app.context.readToolResult({ _meta: { 'token-monitor/locale': 'en-GB' } });
  assert.equal(app.document.documentElement.lang, 'zh-CN');
});

test('date formatting follows the locale while the statistics timezone stays Beijing', () => {
  const app = boot(async () => new Promise(() => {}), { locale: 'en-GB' });
  // Midnight in Beijing, still the previous calendar day in UTC.
  const timestamp = Date.parse('2026-10-29T16:00:00Z') / 1000;
  assert.equal(vm.runInContext(`day(${timestamp})`, app.context), '30/10');
  assert.equal(vm.runInContext(`clock(${timestamp})`, app.context), '00:00:00');
  app.context.applyHostContext({ locale: 'en-US' });
  assert.equal(vm.runInContext(`day(${timestamp})`, app.context), '10/30');
});

test('language selection preserves widget state and relocalizes existing errors', async () => {
  let saved;
  const app = boot(async () => ({ ok: false }), { openai: {
    widgetState: { period: '7d', extra: 'keep' }, setWidgetState: state => { saved = state; },
  } });
  await flush();
  app.context.setLanguage('en');
  assert.equal(saved.period, '7d');
  assert.equal(saved.extra, 'keep');
  assert.equal(saved.language, 'en');
  assert.equal(app.elements.get('liveState').textContent, 'Unable to connect to the local service');
  assert.equal(app.elements.get('connection').querySelector('span').textContent, 'Disconnected');
});

test('network failures are localized and standalone language changes follow the browser', async () => {
  const app = boot(async () => { throw new TypeError('Failed to fetch'); });
  await flush();
  assert.equal(app.elements.get('liveState').textContent, '本地服务连接失败');
  vm.runInContext("navigator.language='en-US'", app.context);
  app.event('languagechange');
  assert.equal(app.elements.get('liveState').textContent, 'Unable to connect to the local service');
  assert.equal(app.document.documentElement.lang, 'en-US');
});

test('all translation pairs have matching interpolation parameters', () => {
  const app = boot(async () => new Promise(() => {}));
  const entries = JSON.parse(vm.runInContext('JSON.stringify(Object.entries(messages))', app.context));
  const params = value => [...value.matchAll(/\{(\w+)\}/g)].map(match => match[1]).sort();
  for (const [key, pair] of entries) {
    assert.equal(pair.length, 2, key);
    assert.deepEqual(params(pair[0]), params(pair[1]), key);
  }
});
