// Run with: node --test scripts/test_frontend_startup.js
// Execute the actual app script with a small DOM adapter; no npm dependencies.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const root = path.join(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'static/index.html'), 'utf8');
const source = fs.readFileSync(path.join(root, 'static/app.js'), 'utf8');

function storage(initial = {}) {
  const values = new Map(Object.entries(initial));
  return {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: key => values.delete(key),
  };
}
function json(body, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}
function stall(signal) {
  return new Promise((resolve, reject) => {
    signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true });
  });
}
function app(fetchImpl) {
  const elements = new Map();
  for (const tag of html.matchAll(/<[^>]+\bid="([^"]+)"[^>]*>/g)) {
    const classes = new Set((tag[0].match(/class="([^"]*)"/)?.[1] || '').split(/\s+/));
    elements.set('#' + tag[1], {
      textContent: '', disabled: /\bdisabled\b/.test(tag[0]), listeners: {}, dataset: {},
      classList: {
        contains: c => classes.has(c), add: c => classes.add(c), remove: c => classes.delete(c),
        toggle(c, force) { if (force ?? !classes.has(c)) classes.add(c); else classes.delete(c); },
      },
      addEventListener(name, listener) { this.listeners[name] = listener; },
    });
  }
  const requests = [];
  const context = vm.createContext({
    console, URL, AbortController, navigator: {},
    localStorage: storage({ labcare_token: 'saved-session' }), sessionStorage: storage(),
    location: { href: 'https://labcare.insforge.site/?join=example#section', replace(url) { this.replaced = url; } },
    document: { querySelector: s => elements.get(s), querySelectorAll: () => [] },
    // Accelerate network deadlines/backoff while preserving async ordering.
    setTimeout: (fn, ms) => setTimeout(fn, ms >= 8000 ? 20 : 0), clearTimeout,
    setInterval: () => 0,
    fetch: (url, options) => {
      requests.push({ url, options });
      return fetchImpl(url.split('?')[0], options);
    },
  });
  context.window = context;
  // Keep real startup, API client, event wiring and root rendering. Unrelated
  // dashboard/push rendering is excluded to keep the test focused on startup.
  vm.runInContext(source.replace(/boot\(\);\s*$/, `
    renderView = () => {};
    refreshBell = () => {};
    syncPushAlerts = () => {};
    globalThis.started = boot();
  `), context);
  return { context, requests, node: id => elements.get('#' + id), run: code => vm.runInContext(code, context) };
}

test('HTML has visible fallback even if app.js fails to load', () => {
  const login = html.match(/<div id="loginScreen"[^>]*>/)[0];
  assert.doesNotMatch(login, /\bhidden\b/);
  assert.match(html, /id="startupStatus"[^>]*role="status"/);
  assert.match(html, /<noscript>/);
  assert.match(html, /id="loginBtn" disabled/);
});

test('hung session request paints immediately, times out, and keeps saved token', async () => {
  const a = app((url, { signal }) => stall(signal));
  assert.equal(a.node('loginScreen').classList.contains('hidden'), false);
  assert.match(a.node('startupStatus').textContent, /Connecting/);
  assert.equal(a.node('loginBtn').disabled, true);
  await a.context.started;
  assert.match(a.node('startupStatus').textContent, /too long/);
  assert.equal(a.node('startupRetry').classList.contains('hidden'), false);
  assert.equal(a.node('loginBtn').disabled, false);
  assert.equal(a.context.localStorage.getItem('labcare_token'), 'saved-session');
  assert.equal(a.requests.length, 1);
  assert.equal(a.requests[0].options.signal.aborted, true);
});

test('gateway failure offers retry on InsForge and retry restores saved session', async () => {
  let healthy = false;
  const a = app(url => Promise.resolve(url === '/api/version' ? json({ version: '52' }) :
    healthy ? json({ id: 1, role: 'customer', name: 'Test' }) : json({}, 503)));
  await a.context.started;
  assert.match(a.node('startupStatus').textContent, /temporarily unavailable/);
  assert.equal(a.run('API.token'), 'saved-session');
  healthy = true;
  await a.node('startupRetry').listeners.click();
  assert.equal(a.node('app').classList.contains('hidden'), false);
  assert.equal(a.node('loginScreen').classList.contains('hidden'), true);
  assert.equal(a.node('startupStatus').textContent, '');
  assert.equal(a.run('state.user.id'), 1);
});

test('401 clears expired token and unlocks login without an outage warning', async () => {
  const a = app(url => Promise.resolve(url === '/api/me' ? json({ error: 'Sign in' }, 401) : json({ version: '52' })));
  await a.context.started;
  assert.equal(a.context.localStorage.getItem('labcare_token'), null);
  assert.equal(a.node('startupStatus').textContent, '');
  assert.equal(a.node('startupRetry').classList.contains('hidden'), true);
  assert.equal(a.node('loginBtn').disabled, false);
});

test('a non-JSON 401/403 (proxy, captive portal) keeps the saved session and offers retry', async () => {
  for (const status of [401, 403]) {
    let intercepted = true;
    const a = app(url => Promise.resolve(url === '/api/version' ? json({ version: '52' }) :
      intercepted ? new Response('<html>Authorization Required</html>', { status, headers: { 'content-type': 'text/html' } })
        : json({ id: 1, role: 'customer', name: 'Test' })));
    await a.context.started;
    assert.equal(a.context.localStorage.getItem('labcare_token'), 'saved-session', `status ${status}`);
    assert.equal(a.run('API.token'), 'saved-session');
    assert.equal(a.run('state.user'), null);
    assert.notEqual(a.node('startupStatus').textContent, '');
    assert.equal(a.node('startupRetry').classList.contains('hidden'), false);
    // once the intermediary is out of the way the same token signs the user back in
    intercepted = false;
    await a.node('startupRetry').listeners.click();
    assert.equal(a.run('state.user.id'), 1);
    assert.equal(a.node('app').classList.contains('hidden'), false);
  }
});

test('only the API\'s own JSON 401 carries the auth flag', async () => {
  const a = app(url => Promise.resolve(url === '/api/me' ? json({ id: 1, role: 'customer', name: 'Test' }) : json({ version: '52' })));
  await a.context.started;
  a.context.fetch = () => Promise.resolve(json({ error: 'Not authenticated' }, 401));
  await assert.rejects(a.run('API.get("/api/test")'), e => e.status === 401 && e.auth === true);
  a.context.fetch = () => Promise.resolve(new Response('denied', { status: 401, headers: { 'content-type': 'text/plain' } }));
  await assert.rejects(a.run('API.get("/api/test")'), e => e.status === 401 && !e.auth);
  a.context.fetch = () => Promise.resolve(json({ error: 'Forbidden' }, 403));
  await assert.rejects(a.run('API.get("/api/test")'), e => e.status === 403 && !e.auth);
  // the manual sign-out is unaffected: it still discards the token
  a.context.fetch = () => Promise.resolve(json({ ok: true }));
  a.run('logout()');
  assert.equal(a.run('API.token'), null);
  assert.equal(a.context.localStorage.getItem('labcare_token'), null);
});

test('version endpoint stall cannot block a restored session', async () => {
  const a = app((url, { signal }) => url === '/api/me' ? Promise.resolve(json({ id: 1, role: 'customer', name: 'Test' })) : stall(signal));
  await a.context.started;
  assert.equal(a.node('app').classList.contains('hidden'), false);
  assert.equal(a.run('booted'), true);
  assert.equal(a.requests[0].url.split('?')[0], '/api/me');
});

test('stalled response body also times out', async () => {
  const a = app((url, { signal }) => Promise.resolve({
    ok: true, status: 200, headers: { get: () => 'application/json' }, json: () => stall(signal),
  }));
  await a.context.started;
  assert.match(a.node('startupStatus').textContent, /too long/);
  assert.equal(a.node('loginBtn').disabled, false);
});

test('HTML fallback and malformed session JSON never become a signed-in user', async () => {
  for (const response of [() => new Response('<html>Proxy error</html>'), () => json({}),
    () => new Response('{', { headers: { 'content-type': 'application/json' } })]) {
    const a = app(() => Promise.resolve(response()));
    await a.context.started;
    assert.equal(a.run('state.user'), null);
    assert.notEqual(a.node('startupStatus').textContent, '');
    assert.equal(a.node('startupRetry').classList.contains('hidden'), false);
    assert.equal(a.run('API.token'), 'saved-session');
  }
});

test('writes are not retried on timeout or gateway failures', async () => {
  const a = app(url => Promise.resolve(url === '/api/me' ? json({}, 401) : json({ version: '52' })));
  await a.context.started;
  for (const mode of ['timeout', 'gateway']) {
    let count = 0;
    a.context.fetch = (url, { signal }) => { count++; return mode === 'timeout' ? stall(signal) : Promise.resolve(json({}, 503)); };
    await assert.rejects(a.run('API.post("/api/login", {})'));
    assert.equal(count, 1);
  }
});

test('GET retries transient gateway failures but not authentication failures', async () => {
  const a = app(url => Promise.resolve(url === '/api/me' ? json({}, 401) : json({ version: '52' })));
  await a.context.started;
  let count = 0;
  a.context.fetch = () => Promise.resolve(++count < 3 ? json({}, 503) : json({ ok: true }));
  assert.equal((await a.run('API.get("/api/test")')).ok, true);
  assert.equal(count, 3);
  count = 0;
  a.context.fetch = () => { count++; return Promise.resolve(json({}, 401)); };
  await assert.rejects(a.run('API.get("/api/test")'), e => e.status === 401);
  assert.equal(count, 1);
});
