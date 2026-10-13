const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { randomUUID, webcrypto } = require('node:crypto');

const source = fs.readFileSync(path.join(__dirname, '../app/static/signal-trial.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '../app/static/signal-trial.html'), 'utf8');
const settle = async () => { for (let i = 0; i < 8; i++) await new Promise(setImmediate); };
const deferred = () => { let resolve; let reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };

function storage(seed = {}) {
  const data = new Map(Object.entries(seed));
  return {
    data, failWrites: false,
    get length() { return data.size; },
    key(i) { return Array.from(data.keys())[i] || null; },
    getItem(key) { return data.get(key) ?? null; },
    setItem(key, value) { if (this.failWrites) throw new Error('quota'); data.set(key, String(value)); },
    removeItem(key) { data.delete(key); },
  };
}

function boot(options = {}) {
  const calls = [];
  const elements = new Map();
  const timers = new Map();
  const intervals = new Map();
  const listeners = new Map();
  const localStorage = options.storage || storage();
  const server = { ready: options.ready !== false, session_id: 'receiver-one', action: null };
  const controls = { copyGate: null, copyFailure: false, archiveFailure: false, actionFailure: false, statusGate: null, actionGate: null, actionHang: false };
  let nextTimer = 0;
  const response = (payload, status = 200) => ({ ok: status < 400, status, json: async () => payload });
  for (const match of html.matchAll(/id="([^"]+)"/g)) {
    const events = new Map();
    elements.set(match[1], {
      value: '', textContent: '', disabled: false, dataset: {}, selectionStart: 0, selectionEnd: 0,
      addEventListener: (name, fn) => events.set(name, fn),
      dispatch: (name, event = {}) => events.get(name)?.(event),
      focus() {}, select() {}, setSelectionRange() {},
    });
  }
  const fetch = async (url, request = {}) => {
    const body = request.body ? JSON.parse(request.body) : null;
    const endpoint = url.split('?')[0];
    calls.push({ kind: 'fetch', url, endpoint, body });
    if (endpoint.endsWith('/api/signal/status')) {
      if (controls.statusGate) return controls.statusGate.promise;
      return response({ ok: true, ...JSON.parse(JSON.stringify(server)) });
    }
    if (endpoint.endsWith('/api/signal/action')) {
      if (controls.actionHang) return new Promise((resolve, reject) => request.signal.addEventListener('abort', () => reject(new Error('aborted')), { once: true }));
      if (controls.actionGate) return controls.actionGate.promise;
      if (typeof controls.actionFailure === 'number') return response({ ok: false, error: 'failed' }, controls.actionFailure);
      if (controls.actionFailure) throw new Error('offline');
      server.action = { action_id: body.action_id, status: 'accepted' };
      return response({ ok: true, action: { ...server.action } }, 202);
    }
    if (endpoint.endsWith('/api/copy-archive')) {
      if (typeof controls.archiveFailure === 'number') return response({ ok: false, error: 'invalid_text' }, controls.archiveFailure);
      if (controls.archiveFailure) throw new Error('offline');
      return response({ ok: true });
    }
    throw new Error(`unexpected endpoint ${endpoint}`);
  };
  const window = {
    __APP_BASE_PATH__: options.basePath,
    location: { pathname: `${options.basePath || ''}/signal/${options.room || 'room'}` },
    localStorage, crypto: { randomUUID, getRandomValues: array => webcrypto.getRandomValues(array) },
    setTimeout: (fn, delay) => { const id = ++nextTimer; timers.set(id, { fn, delay }); return id; },
    clearTimeout: id => timers.delete(id),
    setInterval: (fn, delay) => { const id = ++nextTimer; intervals.set(id, { fn, delay }); return id; },
    addEventListener: (name, fn) => listeners.set(name, fn),
  };
  const doc = {
    visibilityState: 'visible', getElementById: id => elements.get(id),
    addEventListener: (name, fn) => listeners.set(`document:${name}`, fn),
    execCommand: () => { calls.push({ kind: 'legacy-clipboard' }); return true; },
  };
  const navigator = {
    clipboard: options.legacy ? undefined : {
      writeText: text => {
        calls.push({ kind: 'clipboard', text });
        if (controls.copyGate) return controls.copyGate.promise;
        if (controls.copyFailure) return Promise.reject(new Error('denied'));
        return Promise.resolve();
      },
    },
  };
  vm.runInNewContext(source, { window, document: doc, navigator, fetch, AbortController, Uint8Array, Date, Map, Set, Promise, Array, JSON, String, Boolean, Error, encodeURIComponent, decodeURIComponent });
  return {
    calls, controls, server, storage: localStorage, response,
    get: id => elements.get(id),
    input(text) { elements.get('draft').value = text; elements.get('draft').dispatch('input'); },
    click(id) { elements.get(id).dispatch('click'); },
    event(name) { listeners.get(name)?.(); },
    async interval(delay) { for (const timer of intervals.values()) if (timer.delay === delay) timer.fn(); await settle(); },
    async timeout(delay) {
      const entries = Array.from(timers.entries()).filter(([, timer]) => timer.delay === delay);
      for (const [id, timer] of entries) { timers.delete(id); timer.fn(); }
      await settle();
    },
  };
}

const actions = env => env.calls.filter(c => c.kind === 'fetch' && c.endpoint.endsWith('/api/signal/action'));
const archives = env => env.calls.filter(c => c.kind === 'fetch' && c.endpoint.endsWith('/api/copy-archive'));
const queued = env => Array.from(env.storage.data.keys()).filter(key => key.includes(':signal-outbox:'));

test('real gesture begins clipboard write synchronously before either network request', async () => {
  const env = boot({ basePath: '/doubao' }); await settle();
  env.controls.copyGate = deferred(); env.input('  手机正文 🌱\n第二行  ');
  env.calls.length = 0;
  env.click('copy-paste');
  assert.deepEqual(env.calls, [{ kind: 'clipboard', text: '  手机正文 🌱\n第二行  ' }]);
  await settle(); assert.equal(actions(env).length, 0); assert.equal(archives(env).length, 0);
  env.controls.copyGate.resolve(); await settle();
  assert.equal(actions(env).length, 1); assert.equal(archives(env).length, 1);
  assert.deepEqual(Object.keys(actions(env)[0].body).sort(), ['action_id', 'room_id', 'session_id']);
  assert.match(actions(env)[0].body.action_id, /^[0-9a-f-]{36}$/);
  assert.equal(actions(env)[0].endpoint, '/doubao/api/signal/action');
  assert.equal(archives(env)[0].body.text, '  手机正文 🌱\n第二行  ');
  assert.equal(env.get('draft').value, archives(env)[0].body.text);
});

test('clipboard denial emits neither signal nor archive and preserves draft', async () => {
  const env = boot(); await settle(); env.input('保留这段'); env.controls.copyFailure = true;
  env.click('copy-paste'); await settle();
  assert.equal(actions(env).length, 0); assert.equal(archives(env).length, 0);
  assert.equal(queued(env).length, 0); assert.equal(env.get('draft').value, '保留这段');
  assert.match(env.get('action-status').textContent, /复制未完成/);
  assert.equal(env.get('copy-paste').disabled, false);
});

test('archive failure is independent of paste result; retry uses original copy id only', async () => {
  const env = boot(); await settle(); env.controls.archiveFailure = true; env.input('留档延后');
  env.click('copy-paste'); await settle();
  env.server.action.status = 'executed'; await env.interval(2000);
  assert.match(env.get('action-status').textContent, /粘贴指令已执行/);
  assert.match(env.get('archive-status').textContent, /待留档/);
  assert.equal(queued(env).length, 1);
  env.controls.archiveFailure = false; env.event('online'); await settle();
  assert.equal(actions(env).length, 1); assert.equal(archives(env).length, 2);
  assert.equal(archives(env)[0].body.copy_id, archives(env)[1].body.copy_id);
  assert.equal(queued(env).length, 0); assert.equal(env.get('archive-status').textContent, '已留档');
});

test('reload restores only draft and durable archive outbox, never pending actions', async () => {
  const env = boot(); await settle(); env.controls.archiveFailure = true; env.input('reload 后还在');
  env.click('copy-paste'); await settle();
  const id = archives(env)[0].body.copy_id;
  const next = boot({ storage: env.storage }); await settle();
  assert.equal(next.get('draft').value, 'reload 后还在');
  assert.equal(archives(next).length, 1); assert.equal(archives(next)[0].body.copy_id, id);
  assert.equal(actions(next).length, 0); assert.equal(queued(next).length, 0);
  assert.equal(next.get('archive-status').textContent, '已留档');
  next.event('online'); next.event('document:visibilitychange'); await next.interval(2000); await next.interval(5000);
  assert.equal(actions(next).length, 0);
});

test('signal timeout never retries, while archive succeeds and observation can query execution', async () => {
  const env = boot(); await settle(); env.controls.actionHang = true; env.input('指令超时');
  env.click('copy-paste'); await settle(); await env.timeout(6000);
  assert.match(env.get('action-status').textContent, /不会重新发送/);
  assert.equal(env.get('archive-status').textContent, '已留档');
  env.event('online'); env.event('document:visibilitychange'); await env.interval(2000); await env.interval(5000);
  assert.equal(actions(env).length, 1);
  env.server.action = { action_id: actions(env)[0].body.action_id, status: 'executed' };
  await env.interval(2000);
  assert.match(env.get('action-status').textContent, /粘贴指令已执行/);
});

test('server error leaves action outcome uncertain and never resends the signal', async () => {
  const env = boot(); await settle(); env.controls.actionFailure = 502; env.input('网关出错');
  env.click('copy-paste'); await settle();
  assert.match(env.get('action-status').textContent, /不会重新发送/);
  assert.doesNotMatch(env.get('action-status').textContent, /未被接受/);
  assert.equal(env.get('copy-paste').disabled, true);
  env.event('online'); await env.interval(2000); await env.interval(5000);
  assert.equal(actions(env).length, 1);
  await env.timeout(15000); assert.equal(env.get('copy-paste').disabled, false);
});

test('without a live receiver only copy-and-archive is enabled, including legacy copy', async () => {
  const env = boot({ ready: false, legacy: true }); await settle(); env.input('离线仍可留档');
  assert.equal(env.get('copy-paste').disabled, true); assert.equal(env.get('copy-only').disabled, false);
  env.click('copy-paste'); await settle(); assert.equal(actions(env).length, 0);
  env.click('copy-only'); assert.equal(env.calls.at(-1).kind, 'legacy-clipboard'); await settle();
  assert.equal(actions(env).length, 0); assert.equal(archives(env).length, 1);
  assert.match(env.get('action-status').textContent, /本次未发送/);
});

test('pending action blocks both buttons, preventing clipboard replacement and double paste', async () => {
  const env = boot(); await settle(); env.input('第一次'); env.click('copy-paste'); await settle();
  env.input('第二次');
  assert.equal(env.get('copy-paste').disabled, true); assert.equal(env.get('copy-only').disabled, true);
  env.click('copy-paste'); env.click('copy-only'); await settle();
  assert.equal(actions(env).length, 1); assert.equal(archives(env).length, 1);
  assert.equal(env.calls.filter(c => c.kind === 'clipboard').length, 1);
  env.server.action.status = 'executed'; await env.interval(2000);
  assert.equal(env.get('copy-paste').disabled, false); assert.equal(env.get('copy-only').disabled, false);
});

test('stale status response for a previous action cannot replace a new action result', async () => {
  const env = boot(); await settle(); env.input('第一次'); env.click('copy-paste'); await settle();
  const old = { ...env.server.action };
  await env.timeout(15000);
  const gate = deferred(); env.controls.statusGate = gate; await env.interval(2000);
  env.input('第二次'); env.click('copy-paste'); await settle();
  gate.resolve(env.response({ ok: true, ready: true, session_id: 'receiver-one', action: { ...old, status: 'executed' } }));
  await settle(); env.controls.statusGate = null;
  assert.equal(actions(env).length, 2);
  assert.doesNotMatch(env.get('action-status').textContent, /粘贴指令已执行/);
  env.server.action.status = 'executed'; await env.interval(2000);
  assert.match(env.get('action-status').textContent, /粘贴指令已执行/);
});

test('expired receipt does not claim text failed to paste when execution ack may be lost', async () => {
  const env = boot(); await settle(); env.input('核对结果'); env.click('copy-paste'); await settle();
  env.server.action.status = 'expired'; await env.interval(2000);
  assert.match(env.get('action-status').textContent, /执行结果未确认/);
  assert.doesNotMatch(env.get('action-status').textContent, /未粘贴|正确粘贴/);
  assert.equal(env.get('copy-paste').disabled, false);
});

test('unknown result observation timeout unblocks without resending or late reply overwrite', async () => {
  const env = boot(); await settle(); env.input('未知');
  const gate = deferred(); env.controls.actionGate = gate; env.click('copy-paste'); await settle();
  await env.timeout(15000);
  assert.match(env.get('action-status').textContent, /结果未知/);
  assert.equal(env.get('copy-only').disabled, false);
  gate.resolve(env.response({ ok: true, action: { action_id: actions(env)[0].body.action_id, status: 'accepted' } }, 202));
  await settle(); assert.match(env.get('action-status').textContent, /结果未知/);
  assert.equal(actions(env).length, 1);
});

test('storage exhaustion is visible, and transient archive remains retryable without new action', async () => {
  const env = boot(); await settle(); env.storage.failWrites = true; env.controls.archiveFailure = true;
  env.input('储存满了'); env.click('copy-only'); await settle();
  assert.match(env.get('draft-hint').textContent, /储存不可用/);
  assert.match(env.get('archive-status').textContent, /保持本页打开/);
  assert.equal(queued(env).length, 0);
  env.controls.archiveFailure = false; await env.interval(5000);
  assert.equal(env.get('archive-status').textContent, '已留档'); assert.equal(actions(env).length, 0);
});

test('permanent archive rejection retains original record but does not loop requests', async () => {
  const env = boot(); await settle(); env.controls.archiveFailure = 413; env.input('被拒留档'); env.click('copy-only'); await settle();
  assert.equal(queued(env).length, 1); assert.match(env.get('archive-status').textContent, /被服务器拒绝/);
  await env.interval(5000); env.event('online'); await settle();
  assert.equal(archives(env).length, 1); assert.equal(actions(env).length, 0);
});

test('room encoding remains in URL only, while archive and action share decoded room', async () => {
  const env = boot({ basePath: '/tools/doubao', room: '%E8%AF%95%E7%94%A8' }); await settle();
  env.input('中文 room'); env.click('copy-paste'); await settle();
  assert.equal(actions(env)[0].body.room_id, '试用'); assert.equal(archives(env)[0].body.room_id, '试用');
  assert.ok(env.calls.some(call => call.url?.includes('room=%E8%AF%95%E7%94%A8')));
  assert.equal(actions(env)[0].endpoint, '/tools/doubao/api/signal/action');
});
