// Requires Playwright; PLAYWRIGHT_MODULE can point to an existing installation.
// Run: node tests/queue-ui.cjs (PLAYWRIGHT_BROWSERS_PATH may select local browsers).
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '..');

async function fixture(browser, mobile = false) {
  const context = await browser.newContext({viewport: mobile ? {width: 390, height: 844} : {width: 1100, height: 1000}, hasTouch: mobile, isMobile: mobile});
  const page = await context.newPage();
  const items = ['a', 'b', 'c', 'd', 'e'].map(id => ({id, text: `대기 작업 ${id}`}));
  const data = {items, requests: [], reject: false};
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  page.setDefaultTimeout(10000);
  await page.route('https://telegram.org/**', route => route.fulfill({body: '', contentType: 'text/javascript'}));
  await page.addInitScript(() => { window.Telegram = {WebApp: {initData: 'fixture', ready(){}, expand(){}}}; });
  await page.route('http://cloudex.test/**', async route => {
    const request = route.request(), url = new URL(request.url()), target = url.pathname;
    const body = request.postDataJSON() || Object.fromEntries(url.searchParams);
    const json = value => route.fulfill({json: value});
    if (target === '/') return route.fulfill({path: path.join(root, 'static/index.html'), contentType: 'text/html'});
    if (['/style.css', '/app.js'].includes(target)) return route.fulfill({path: path.join(root, 'static', target.slice(1)), contentType: target.endsWith('.js') ? 'text/javascript' : 'text/css'});
    if (target === '/api/status') return json({selected: 'main', server: 'fixture', canEditQueuedRequests: true, canReorderQueuedRequests: true, messageModes: ['queue','steer'], sessions: [
      {id: 'main', name: 'Main', queued: data.items.length, active: true, activeTurnId: 'running', effective: {}},
      {id: 'other', name: 'Other', queued: 0, active: false, effective: {}}], approvals: []});
    if (target === '/api/events') { await new Promise(resolve => setTimeout(resolve, 500)); return json({server: 'fixture', events: [], cursor: 0}); }
    if (target === '/api/models' || target === '/api/history') return json({data: []});
    if (target === '/api/account') return json({canLogin: false});
    if (target === '/api/queue') return json({session: body.session, activeTurnId: 'running', items: body.session === 'main' ? data.items : []});
    if (target === '/api/queue/reorder') {
      data.requests.push(body);
      if (data.reject || JSON.stringify(body.expectedIds) !== JSON.stringify(data.items.map(item => item.id))) return route.fulfill({status: 400, json: {error: '대기열이 바뀌었습니다. 다시 이동해주세요.'}});
      data.items = body.ids.map(id => data.items.find(item => item.id === id));
      return json({session: body.session, activeTurnId: 'running', items: data.items});
    }
    if (target === '/api/queue/edit') {
      data.requests.push(body);
      const item = data.items.find(item => item.id === body.id);
      assert.equal(item.text, body.expectedText);
      item.text = body.text.trim();
      return json({session: body.session, activeTurnId: 'running', items: data.items});
    }
    if (target === '/api/command') return json({ok: true, selected: body.session});
    return route.fulfill({status: 404, json: {error: 'Unexpected request ' + target}});
  });
  await page.goto('http://cloudex.test/');
  try { await page.locator('#queue-preview-list li').first().waitFor(); }
  catch (error) { console.error('Fixture load failed:', errors, await page.locator('#error').textContent()); throw error; }
  return {context, page, data, errors};
}
const ids = async (page, list) => page.locator(`${list} > li`).evaluateAll(rows => rows.map(row => row.dataset.id));
async function mouseDrag(page, list, from, to) {
  const handle = page.locator(`${list} [data-id="${from}"] .queue-drag-handle`);
  const target = page.locator(`${list} [data-id="${to}"]`);
  const start = await handle.boundingBox(), finish = await target.boundingBox();
  await page.mouse.move(start.x + start.width / 2, start.y + start.height / 2);
  await page.mouse.down();
  await page.mouse.move(finish.x + 20, finish.y + finish.height - 3, {steps: 12});
  await page.waitForFunction(({list, from, to}) => {
    const ids = Array.from(document.querySelector(list).children).map(row => row.dataset.id);
    return ids.indexOf(from) > ids.indexOf(to);
  }, {list, from, to});
  await page.mouse.up();
}
(async () => {
  const browser = await chromium.launch({headless: true, args: ['--no-sandbox']});
  try {
    let f = await fixture(browser);
    await mouseDrag(f.page, '#queue-preview-list', 'a', 'c');
    await f.page.waitForFunction(() => document.querySelector('#queue-preview-list li').dataset.id === 'b');
    assert.deepEqual(f.data.requests[0], {session: 'main', ids: ['b','c','a','d','e'], expectedIds: ['a','b','c','d','e']});
    assert.deepEqual(await ids(f.page, '#queue-preview-list'), ['b','c','a']);
    assert.deepEqual(f.data.items.map(item => item.id), ['b','c','a','d','e']);
    console.log('PASS mouse drag in preview saves full order and preserves hidden requests');
    await f.page.locator('#queue-status').click();
    await f.page.locator('#queue-list [data-id="e"] .queue-drag-handle').focus();
    await f.page.keyboard.press('Home');
    await f.page.waitForFunction(() => document.querySelector('#queue-list li').dataset.id === 'e');
    assert.equal(await f.page.locator(':focus').getAttribute('aria-label'), '1번째 대기 요청 순서 변경');
    await mouseDrag(f.page, '#queue-list', 'e', 'd');
    await f.page.waitForFunction(() => document.querySelector('#queue-list li:last-child').dataset.id === 'e');
    assert.deepEqual(f.data.items.map(item => item.id), ['b','c','a','d','e']);
    console.log('PASS full-list drag and keyboard movement preserve focus and execution order');
    await f.page.locator('#queue-list [data-id="b"] button', {hasText: '수정'}).click();
    await f.page.locator('#queue-edit-text').fill('수정된 작업');
    await f.page.locator('#queue-edit-save').click();
    await f.page.locator('#queue-edit-modal').waitFor({state: 'hidden'});
    assert.equal(f.data.items[0].text, '수정된 작업');
    console.log('PASS existing request editing still saves after reorder');
    assert.deepEqual(f.errors, []);
    await f.context.close();

    f = await fixture(browser);
    const start = await f.page.locator('#queue-preview-list [data-id="a"] .queue-drag-handle').boundingBox();
    const end = await f.page.locator('#queue-preview-list [data-id="c"]').boundingBox();
    await f.page.mouse.move(start.x + 10, start.y + 10); await f.page.mouse.down();
    await f.page.mouse.move(end.x + 10, end.y + end.height - 2, {steps: 8});
    await f.page.keyboard.press('Escape'); await f.page.mouse.up();
    assert.equal(f.data.requests.length, 0);
    assert.deepEqual(await ids(f.page, '#queue-preview-list'), ['a','b','c']);
    f.data.reject = true;
    await mouseDrag(f.page, '#queue-preview-list', 'a', 'c');
    await f.page.locator('#queue-preview-message').waitFor();
    assert.deepEqual(await ids(f.page, '#queue-preview-list'), ['a','b','c']);
    assert.match(await f.page.locator('#queue-preview-message').textContent(), /대기열이 바뀌었습니다/);
    console.log('PASS Escape cancels drag; stale-order rejection restores server order');
    assert.deepEqual(f.errors, []);
    await f.context.close();

    f = await fixture(browser, true);
    const touchStart = await f.page.locator('#queue-preview-list [data-id="a"] .queue-drag-handle').boundingBox();
    const touchEnd = await f.page.locator('#queue-preview-list [data-id="c"]').boundingBox();
    const cdp = await f.context.newCDPSession(f.page);
    await cdp.send('Input.dispatchTouchEvent', {type: 'touchStart', touchPoints: [{x: touchStart.x + 16, y: touchStart.y + 20}]});
    for (let step = 1; step <= 12; step++) {
      const y = touchStart.y + 20 + (touchEnd.y + touchEnd.height - 3 - touchStart.y - 20) * step / 12;
      await cdp.send('Input.dispatchTouchEvent', {type: 'touchMove', touchPoints: [{x: touchStart.x + 16, y}]});
    }
    await cdp.send('Input.dispatchTouchEvent', {type: 'touchEnd', touchPoints: []});
    await f.page.waitForFunction(() => document.querySelector('#queue-preview-list li').dataset.id === 'b');
    assert.deepEqual(f.data.items.map(item => item.id), ['b','c','a','d','e']);
    assert.equal(await f.page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    console.log('PASS actual touch drag on 390px mobile viewport without horizontal overflow');
    assert.deepEqual(f.errors, []);
    await f.context.close();
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exitCode = 1;});
