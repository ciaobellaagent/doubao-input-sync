const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { randomUUID, createHash } = require('node:crypto');

const source = fs.readFileSync(path.join(__dirname, '../app/static/client.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '../app/static/index.html'), 'utf8');
const settle = async () => { for (let i = 0; i < 8; i++) await new Promise(setImmediate); };
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };

function storage(seed = {}) {
  const data = new Map(Object.entries(seed));
  return {
    data, failWrites: false,
    get length() { return data.size; },
    key(i) { return Array.from(data.keys())[i] || null; },
    getItem(key) { return data.has(key) ? data.get(key) : null; },
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
  const streams = [];
  const localStorage = options.storage || storage();
  let nextTimer = 0;
  const startedAt = Date.now();
  let timeOffset = 0;
  class TestDate extends Date { static now() { return startedAt + timeOffset; } }
  let doc;
  class Element {
    constructor(id = '') {
      this.id = id; this.dataset = {}; this.attributes = {}; this.events = new Map(); this.children = [];
      this.value = ''; this.textContent = ''; this.hidden = false; this.disabled = false; this.checked = false;
      this.readOnly = false; this.style = {}; this.selectionStart = 0; this.selectionEnd = 0;
      this.classes = new Set();
      this.classList = { add: c => this.classes.add(c), remove: c => this.classes.delete(c), toggle: (c, yes) => yes ? this.classes.add(c) : this.classes.delete(c) };
    }
    set innerHTML(value) { this.html = value; this.children = []; }
    get innerHTML() { return this.html || ''; }
    setAttribute(key, value) { this.attributes[key] = value; }
    addEventListener(name, cb) { this.events.set(name, cb); }
    dispatch(name, event = {}) { return this.events.get(name)?.(event); }
    appendChild(child) { this.children.push(child); child.parent = this; return child; }
    append(...children) { children.forEach(c => this.appendChild(c)); }
    querySelectorAll(selector) {
      return this.children.flatMap(child => [
        ...(selector === '[data-clipboard-copy]' && child.dataset.clipboardCopy ? [child] : []),
        ...child.querySelectorAll(selector),
      ]);
    }
    focus() { doc.activeElement = this; }
    select() { doc.activeElement = this; }
    setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; }
    remove() { if (this.parent) this.parent.children = this.parent.children.filter(c => c !== this); }
  }
  for (const match of html.matchAll(/id="([^"]+)"/g)) elements.set(match[1], new Element(match[1]));
  if (options.oldHtml) for (const id of ['clipboard-buttons', 'copy-paste-button', 'signal-receiver-status', 'signal-action-status', 'copy-history-refresh-button', 'pc-panel-hint']) elements.delete(id);
  elements.get('archive-idle-input').value = '2.4';
  doc = {
    body: new Element('body'), activeElement: null, visibilityState: 'visible',
    getElementById: id => elements.get(id), createElement: () => new Element(),
    addEventListener: (name, cb) => listeners.set(`document:${name}`, cb),
    execCommand: () => true,
  };
  const server = {
    room_id: options.room || 'room', server_epoch: 'epoch-one', version: 0, text: '', history: [], updated_at: '', source: '',
    settings: { capture_mode: options.mode || 'clipboard', revision: options.revision || 0, archive_idle_seconds: 2.4, theme: 'warm' },
  };
  const archived = (options.archives || []).map(item => ({ ...item }));
  const controls = {
    copyFailure: options.copyFailure, copyGate: null, updateGate: null, clipboardGate: null, hangEndpoints: new Set(),
    signalReady: options.signalReady !== false, signalSession: 'receiver-session', signalAction: null, signalFailure: false,
    protocol: options.protocol || 'clipboard-match-v1', preparation: null, prepareReady: true, prepareGate: null,
    historyFailure: options.historyFailure, historyGate: options.historyGate,
  };
  const payload = () => JSON.parse(JSON.stringify(server));
  const response = (value, status = 200) => ({ ok: status < 400, status, json: async () => value });
  const fetch = async (url, request = {}) => {
    const body = request.body ? JSON.parse(request.body) : null;
    const endpoint = url.split('?')[0];
    calls.push({ kind: 'fetch', endpoint, url, body });
    if (controls.hangEndpoints.has(endpoint)) {
      return new Promise((resolve, reject) => request.signal.addEventListener('abort', () => reject(new Error('aborted')), { once: true }));
    }
    if (endpoint.endsWith('/api/state')) {
      if (options.stateFailure) throw new Error('state unavailable');
      return response(payload());
    }
    if (endpoint.endsWith('/api/server-info')) return response({ archive_idle_seconds: 2.4 });
    if (endpoint.endsWith('/api/claim')) return response({ conflict: false, state: payload() });
    if (endpoint.endsWith('/api/settings')) {
      if (body.capture_mode && body.capture_mode !== server.settings.capture_mode) server.settings.revision++;
      Object.assign(server.settings, body.capture_mode ? { capture_mode: body.capture_mode } : {});
      return response(payload());
    }
    if (endpoint.endsWith('/api/update')) {
      if (controls.updateGate) await controls.updateGate.promise;
      server.text = body.text; server.version++;
      return response(payload());
    }
    if (endpoint.endsWith('/api/capture')) {
      server.history.push({ archive_id: server.history.length + 1, text: server.text, archived_at: 'now', chars: server.text.length });
      return response({ ok: true, state: payload() });
    }
    if (endpoint.endsWith('/api/copy-history')) {
      if (controls.historyGate) await controls.historyGate.promise;
      if (controls.historyFailure) throw new Error('history unavailable');
      return response({ items: [...archived].reverse() });
    }
    if (endpoint.endsWith('/api/signal/status')) return response({ ok: true, ready: controls.signalReady, session_id: controls.signalSession, action: controls.signalAction, protocol: controls.protocol, preparation: controls.preparation });
    if (endpoint.endsWith('/api/signal/prepare')) {
      if (controls.prepareGate) await controls.prepareGate.promise;
      controls.preparation = { preparation_id: body.preparation_id, sha256: body.sha256, ready: controls.prepareReady, remaining_ms: 60000 };
      return response({ ok: true, preparation: controls.preparation });
    }
    if (endpoint.endsWith('/api/signal/action')) {
      if (controls.signalFailure) throw new Error('signal connection lost');
      controls.signalAction = { action_id: body.action_id, status: 'accepted' };
      return response({ ok: true, action: controls.signalAction });
    }
    if (endpoint.endsWith('/api/copy-archive')) {
      if (controls.copyGate) await controls.copyGate.promise;
      if (typeof controls.copyFailure === 'function') {
        const failure = controls.copyFailure(body);
        if (failure instanceof Error) throw failure;
        if (failure) return response({ error: failure.error || 'invalid_text' }, failure.status || 400);
      } else if (controls.copyFailure) throw new Error('offline');
      if (!archived.some(item => item.copy_id === body.copy_id)) archived.push({ ...body, archived_at: '2026-10-08T10:00:00Z' });
      return response({ ok: true, copy_archive: archived.find(item => item.copy_id === body.copy_id) });
    }
    return response({ ok: true });
  };
  const window = {
    __APP_BASE_PATH__: options.basePath || '', location: { pathname: `${options.basePath || ''}/${options.role || 'mobile'}/${encodeURIComponent(options.room || 'room')}` },
    localStorage, isSecureContext: options.secure !== false, crypto: { randomUUID, subtle: { digest: async (algorithm, bytes) => Uint8Array.from(createHash("sha256").update(bytes).digest()).buffer } }, confirm: () => true,
    setTimeout: (fn, delay) => { const id = ++nextTimer; timers.set(id, { fn, delay }); return id; },
    clearTimeout: id => timers.delete(id),
    setInterval: (fn, delay) => { const id = ++nextTimer; intervals.set(id, { fn, delay }); return id; },
    clearInterval: id => intervals.delete(id),
    addEventListener: (name, cb) => listeners.set(name, cb),
  };
  class EventSource {
    constructor(url) { this.url = url; this.events = new Map(); streams.push(this); }
    addEventListener(name, cb) { this.events.set(name, cb); }
    close() {}
  }
  const navigator = {
    userAgent: 'test browser', sendBeacon: () => true,
    clipboard: { writeText: text => { calls.push({ kind: 'clipboard', text }); return controls.clipboardGate ? controls.clipboardGate.promise : options.clipboardFailure ? Promise.reject(new Error('denied')) : Promise.resolve(); } },
  };
  vm.runInNewContext(source, { window, document: doc, navigator, fetch, EventSource, AbortController, Blob, Uint8Array, TextEncoder, Date: TestDate, Set, Map, Promise, Math, Number, String, Array, encodeURIComponent, decodeURIComponent });
  return {
    calls, server, controls, elements, storage: localStorage, archived, timers,
    advance(milliseconds) { timeOffset += milliseconds; },
    get: id => elements.get(id),
    input(text) { const editor = elements.get('mobile-editor'); editor.value = text; editor.dispatch('input'); },
    click(id) { return elements.get(id).dispatch('click'); },
    emit(value) { streams.at(-1).events.get('room_state')({ data: JSON.stringify(value) }); },
    event(name, event = {}) { return listeners.get(name)?.(event); },
    async prepare() { await this.runTimeout(240); await settle(); },
    async tick(delay) { for (const timer of intervals.values()) if (timer.delay === delay) await timer.fn(); await settle(); },
    async runTimeout(delay) {
      const timer = Array.from(timers.entries()).find(([, value]) => value.delay === delay);
      if (timer) { timers.delete(timer[0]); await timer[1].fn(); await settle(); }
      return Boolean(timer);
    },
  };
}

const writes = env => env.calls.filter(c => c.kind === 'fetch' && c.endpoint.endsWith('/api/copy-archive'));
const drafts = env => env.calls.filter(c => c.kind === 'fetch' && /\/api\/(update|capture)$/.test(c.endpoint));
const signals = env => env.calls.filter(c => c.kind === 'fetch' && c.endpoint.endsWith('/api/signal/action'));
const pendingKeys = store => Array.from(store.data.keys()).filter(key => key.startsWith('doubao-input-sync:copy-outbox:'));

test('clipboard input/clear/restore never updates relay, SSE/old desktop ack preserve local text', async () => {
  const env = boot(); await settle();
  env.input('这份草稿只在手机');
  env.emit({ ...env.server, version: 3, text: '', history: [{ archive_id: 1, text: '这份草稿只在手机', desktop_received_at: 'now' }] });
  assert.equal(env.get('mobile-editor').value, '这份草稿只在手机');
  env.click('clear-button'); env.click('restore-last-button'); await settle();
  assert.equal(env.get('mobile-editor').value, '这份草稿只在手机');
  assert.equal(drafts(env).length, 0);
  const reopened = boot({ storage: env.storage }); await settle();
  assert.equal(reopened.get('mobile-editor').value, '这份草稿只在手机');
});

test('copy writes clipboard in the user gesture, then archives exactly that snapshot', async () => {
  const env = boot({ basePath: '/doubao' }); await settle();
  env.controls.clipboardGate = deferred();
  env.input('第一版');
  const before = env.calls.length;
  env.click('copy-archive-button');
  assert.deepEqual(env.calls.slice(before), [{ kind: 'clipboard', text: '第一版' }]);
  env.input('下一版');
  env.controls.clipboardGate.resolve(); await settle();
  assert.equal(writes(env)[0].body.text, '第一版');
  assert.match(writes(env)[0].body.copy_id, /^[0-9a-f-]{36}$/);
  assert.equal(env.get('mobile-editor').value, '下一版');
  assert.match(env.get('copy-status').textContent, /已留档/);
  assert.equal(pendingKeys(env.storage).length, 0);
  assert.equal(drafts(env).length, 0);
});

test('successful archive clears unchanged text and preserves local recovery', async () => {
  const env = boot(); await settle(); env.input('可以恢复'); env.click('copy-archive-button'); await settle();
  assert.equal(env.get('mobile-editor').value, '');
  assert.equal(env.get('restore-last-button').hidden, false);
  env.click('restore-last-button'); await settle();
  assert.equal(env.get('mobile-editor').value, '可以恢复');
  assert.equal(drafts(env).length, 0);
});

test('clipboard denial does not archive or clear and insecure context has a clear error', async () => {
  const env = boot({ clipboardFailure: true, secure: false }); await settle();
  env.input('保留原文'); env.click('copy-archive-button'); await settle();
  assert.equal(env.get('mobile-editor').value, '保留原文');
  assert.equal(writes(env).length, 0);
  assert.match(env.get('copy-status').textContent, /HTTPS/);
});

test('offline archive persists, retries are bounded, reload saves idempotently without clearing restored text', async () => {
  const env = boot({ copyFailure: true }); await settle(); env.input('断网也保留'); env.click('copy-archive-button'); await settle();
  const copyId = writes(env)[0].body.copy_id;
  for (const delay of [1200, 2400, 4800, 9600]) assert.equal(await env.runTimeout(delay), true);
  assert.equal(writes(env).length, 5);
  assert.equal(await env.runTimeout(15000), false);
  assert.equal(pendingKeys(env.storage).length, 1);
  const reopened = boot({ storage: env.storage }); await settle();
  assert.equal(writes(reopened)[0].body.copy_id, copyId);
  assert.equal(reopened.get('mobile-editor').value, '断网也保留');
  assert.equal(pendingKeys(env.storage).length, 0);
});

test('two offline tabs preserve independent queued copies; saving one never removes another', async () => {
  const shared = storage();
  const a = boot({ storage: shared, copyFailure: true }); const b = boot({ storage: shared, copyFailure: true }); await settle();
  a.input('来自 A'); a.click('copy-archive-button'); await settle();
  b.input('来自 B'); b.click('copy-archive-button'); await settle();
  assert.equal(pendingKeys(shared).length, 2);
  a.controls.copyFailure = false; a.click('retry-copy-archive-button'); await settle();
  assert.equal(pendingKeys(shared).length, 1);
  assert.match(shared.getItem(pendingKeys(shared)[0]), /来自 B/);
  const reopened = boot({ storage: shared }); await settle();
  assert.equal(writes(reopened)[0].body.text, '来自 B');
});

test('permanently rejected outbox record remains available without blocking a later valid copy', async () => {
  const env = boot({ copyFailure: body => body.text === '被拒绝' ? { status: 400 } : null }); await settle();
  env.input('被拒绝'); env.click('copy-archive-button'); await settle();
  env.input('正常内容'); env.click('copy-archive-button'); await settle();
  assert.deepEqual(env.archived.map(item => item.text), ['正常内容']);
  assert.equal(pendingKeys(env.storage).length, 1);
  assert.match(env.storage.getItem(pendingKeys(env.storage)[0]), /archiveError/);
});

test('storage quota plus offline failure keeps a visible do-not-close warning', async () => {
  const local = storage(); const env = boot({ storage: local, copyFailure: true }); await settle(); local.failWrites = true;
  env.input('尚未持久化'); env.click('copy-archive-button'); await settle();
  assert.match(env.get('copy-status').textContent, /请保留页面/);
  assert.match(env.get('copy-queue-status').textContent, /请保留页面/);
  assert.equal(env.get('mobile-editor').value, '尚未持久化');
});

test('mode changes use settings before relay draft; stale settings/ack cannot revert clipboard', async () => {
  const env = boot(); await settle(); env.input('切换时的文字');
  await env.click('manual-mode-button'); await settle();
  const relevant = env.calls.filter(c => c.kind === 'fetch' && /\/api\/(settings|update)$/.test(c.endpoint));
  assert.deepEqual(relevant.map(c => c.endpoint), ['/api/settings', '/api/update']);
  assert.equal(relevant[1].body.expected_settings_revision, 1);
  await env.click('clipboard-mode-button'); await settle();
  const count = drafts(env).length;
  env.emit({ ...env.server, version: env.server.version + 1, text: '', settings: { ...env.server.settings, revision: 1, capture_mode: 'auto' }, history: [{ archive_id: 2, text: '切换时的文字', desktop_received_at: 'now' }] });
  assert.equal(env.get('composer-shell').dataset.captureMode, 'clipboard');
  assert.equal(env.get('mobile-editor').value, '切换时的文字');
  assert.equal(drafts(env).length, count);
});

test('server restart accepts new epoch and reset revision; delayed old epoch is ignored', async () => {
  const env = boot({ mode: 'manual', revision: 5 }); await settle();
  env.emit({ ...env.server, version: 9 });
  env.input('重启时的草稿');
  env.server.server_epoch = 'epoch-two'; env.server.version = 0; env.server.settings = { ...env.server.settings, capture_mode: 'clipboard', revision: 0 };
  env.emit(env.server);
  assert.equal(env.get('composer-shell').dataset.captureMode, 'clipboard');
  assert.equal(env.get('mobile-editor').value, '重启时的草稿');
  env.emit({ ...env.server, server_epoch: 'epoch-one', version: 10, settings: { ...env.server.settings, capture_mode: 'manual', revision: 6 } });
  assert.equal(env.get('composer-shell').dataset.captureMode, 'clipboard');
  await env.click('manual-mode-button'); await settle();
  assert.equal(drafts(env).at(-1).body.expected_settings_revision, 1);
});

test('manual capture waits for older autosaves and protects final text during send', async () => {
  const env = boot({ mode: 'manual' }); await settle();
  env.controls.updateGate = deferred(); env.input('最后一版'); await env.runTimeout(180);
  env.click('manual-send-button'); await settle();
  assert.equal(env.get('mobile-editor').readOnly, true);
  assert.equal(env.calls.filter(c => c.endpoint?.endsWith('/api/capture')).length, 0);
  env.controls.updateGate.resolve(); await settle();
  assert.equal(env.calls.filter(c => c.endpoint?.endsWith('/api/capture')).length, 1);
  assert.equal(env.get('mobile-editor').readOnly, false);
});

test('archive acknowledgement after mode round trip never clears the current editor', async () => {
  const env = boot(); await settle(); env.controls.copyGate = deferred();
  env.input('同样的文字'); env.click('copy-archive-button'); await settle();
  await env.click('manual-mode-button'); await settle(); await env.click('clipboard-mode-button'); await settle();
  env.controls.copyGate.resolve(); await settle();
  assert.equal(env.get('mobile-editor').value, '同样的文字');
});

test('blackholed mode settings times out and re-enables clipboard controls', async () => {
  const env = boot(); await settle(); env.input('模式切换失败也保留');
  env.controls.hangEndpoints.add('/api/settings');
  env.click('manual-mode-button'); await settle();
  assert.equal(env.get('copy-archive-button').disabled, true);
  assert.equal(await env.runTimeout(10000), true);
  assert.equal(env.get('composer-shell').dataset.captureMode, 'clipboard');
  assert.equal(env.get('copy-archive-button').disabled, false);
  assert.equal(env.get('clipboard-mode-button').disabled, false);
  assert.equal(env.get('mobile-editor').value, '模式切换失败也保留');
});

test('blackholed old autosave and final draft time out and release manual readonly', async () => {
  const env = boot({ mode: 'manual' }); await settle(); env.controls.hangEndpoints.add('/api/update');
  env.input('网络半开也保留'); await env.runTimeout(180);
  env.click('manual-send-button'); await settle(); assert.equal(env.get('mobile-editor').readOnly, true);
  assert.equal(await env.runTimeout(10000), true);
  assert.equal(await env.runTimeout(10000), true);
  assert.equal(env.get('mobile-editor').readOnly, false);
  assert.equal(env.get('manual-mode-button').disabled, false);
  assert.equal(env.get('mobile-editor').value, '网络半开也保留');
});

test('blackholed capture times out and release manual readonly without losing text', async () => {
  const env = boot({ mode: 'manual' }); await settle(); env.controls.hangEndpoints.add('/api/capture');
  env.input('发送超时也保留'); env.click('manual-send-button'); await settle();
  assert.equal(env.get('mobile-editor').readOnly, true);
  assert.equal(await env.runTimeout(10000), true);
  assert.equal(env.get('mobile-editor').readOnly, false);
  assert.equal(env.get('mobile-editor').value, '发送超时也保留');
});

test('old in-flight relay response after selecting clipboard cannot schedule retry or clear draft', async () => {
  const env = boot({ mode: 'manual' }); await settle(); env.controls.updateGate = deferred();
  env.input('切到剪贴板保留我'); await env.runTimeout(180);
  await env.click('clipboard-mode-button'); await settle();
  const before = drafts(env).length;
  env.controls.updateGate.resolve(); await settle();
  assert.equal(env.get('composer-shell').dataset.captureMode, 'clipboard');
  assert.equal(env.get('mobile-editor').value, '切到剪贴板保留我');
  assert.equal(await env.runTimeout(1200), false);
  assert.equal(drafts(env).length, before);
});

test('main-page paste uses current room, synchronous clipboard gesture and one body-free action', async () => {
  const env = boot({ basePath: '/doubao', room: 'air' }); await settle();
  env.controls.clipboardGate = deferred(); env.input('豆包传递这一版'); await env.prepare();
  assert.equal(env.get('copy-paste-button').disabled, false);
  const before = env.calls.length;
  env.click('copy-paste-button');
  assert.deepEqual(env.calls.slice(before), [{ kind: 'clipboard', text: '豆包传递这一版' }]);
  env.controls.clipboardGate.resolve(); await settle();
  assert.equal(signals(env).length, 1);
  const action = signals(env)[0].body;
  assert.deepEqual(Object.keys(action).sort(), ['action_id', 'preparation_id', 'room_id', 'session_id']);
  assert.equal(action.room_id, 'air'); assert.equal(action.session_id, 'receiver-session');
  assert.match(action.action_id, /^[0-9a-f-]{36}$/);
  assert.equal(writes(env)[0].body.text, '豆包传递这一版');
  assert.equal(writes(env)[0].body.room_id, 'air');
  env.input('继续输入的下一版');
  assert.equal(env.get('mobile-editor').value, '继续输入的下一版');
  assert.equal(drafts(env).length, 0);
  assert.match(env.calls.find(c => c.endpoint?.endsWith('/api/signal/status')).url, /room=air/);
  env.controls.signalAction = { action_id: action.action_id, status: 'executed' };
  await env.tick(2000);
  assert.match(env.get('signal-action-status').textContent, /已执行.*核对/);
  assert.match(env.get('copy-status').textContent, /已留档/);
  await env.prepare();
  assert.equal(env.get('copy-paste-button').disabled, false);
});

test('copy failure cannot send action, archive or erase draft', async () => {
  const env = boot({ clipboardFailure: true }); await settle();
  env.input('保留复制失败的文字'); await env.prepare(); env.click('copy-paste-button'); await settle();
  assert.equal(signals(env).length, 0); assert.equal(writes(env).length, 0);
  assert.equal(env.get('mobile-editor').value, '保留复制失败的文字');
  assert.match(env.get('signal-action-status').textContent, /没有发送粘贴指令/);
});

test('pending paste blocks duplicate/history/PC copies and mode switches, while typing stays local', async () => {
  const env = boot(); await settle();
  env.input('历史记录'); env.click('copy-archive-button'); await settle();
  env.emit({ ...env.server, history: [{ archive_id: 1, text: '发送过的历史', archived_at: 'now', chars: 6 }] });
  env.input('本次待粘贴'); await env.prepare(); env.click('copy-paste-button'); await settle();
  const clipboardCount = env.calls.filter(c => c.kind === 'clipboard').length;
  env.input('后续草稿');
  env.click('copy-paste-button'); env.click('copy-archive-button'); env.click('copy-button');
  env.click('manual-mode-button'); env.click('auto-mode-button');
  for (const id of ['copy-history-list', 'history-list']) {
    for (const button of env.get(id).querySelectorAll('[data-clipboard-copy]')) {
      assert.equal(button.disabled, true); button.dispatch('click');
    }
  }
  // An independently arriving room setting must not turn ongoing input into a relay write.
  env.emit({ ...env.server, settings: { ...env.server.settings, revision: 1, capture_mode: 'auto' } });
  env.input('仍然只存本机'); await settle(); await env.runTimeout(180);
  assert.equal(signals(env).length, 1);
  assert.equal(env.calls.filter(c => c.kind === 'clipboard').length, clipboardCount);
  assert.equal(drafts(env).length, 0);
  assert.equal(env.get('composer-shell').dataset.captureMode, 'clipboard');
  assert.equal(env.get('mobile-editor').readOnly, false);
});

test('archive failure/retry is independent of executed signal and never repeats paste', async () => {
  const env = boot({ copyFailure: true }); await settle();
  env.input('留档稍后重试'); await env.prepare(); env.click('copy-paste-button'); await settle();
  const action = signals(env)[0].body;
  env.controls.signalAction = { action_id: action.action_id, status: 'executed' };
  await env.tick(2000);
  assert.match(env.get('signal-action-status').textContent, /已执行/);
  assert.match(env.get('copy-status').textContent, /待补存/);
  assert.equal(pendingKeys(env.storage).length, 1);
  env.controls.copyFailure = false; env.click('retry-copy-archive-button'); await settle();
  assert.equal(signals(env).length, 1);
  assert.equal(writes(env)[0].body.copy_id, writes(env)[1].body.copy_id);
  assert.match(env.get('copy-status').textContent, /已留档/);
  assert.match(env.get('signal-action-status').textContent, /已执行/);
});

test('uncertain signal times out once; reconnect and reload replay archives only', async () => {
  const env = boot({ copyFailure: true }); await settle();
  env.controls.hangEndpoints.add('/api/signal/action');
  env.input('未确认的动作'); await env.prepare(); env.click('copy-paste-button'); await settle();
  assert.equal(await env.runTimeout(6000), true);
  assert.match(env.get('signal-action-status').textContent, /不明确/);
  env.event('online'); await settle(); await env.tick(2000);
  assert.equal(signals(env).length, 1);
  assert.equal(await env.runTimeout(15000), true);
  assert.match(env.get('signal-action-status').textContent, /结果未知.*不会重发/);
  const reopened = boot({ storage: env.storage }); await settle(); await reopened.tick(2000);
  assert.equal(signals(reopened).length, 0);
  assert.equal(writes(reopened)[0].body.text, '未确认的动作');
  assert.equal(reopened.get('mobile-editor').value, '未确认的动作');
  assert.equal([...env.storage.data.keys()].some(key => /signal/.test(key)), false);
});

test('receiver unavailable still allows copy-only or queueing; PC does not poll signal', async () => {
  const env = boot({ signalReady: false }); await settle(); env.input('手动粘贴');
  assert.equal(env.get('copy-paste-button').disabled, false);
  assert.equal(env.get('copy-archive-button').disabled, false);
  env.click('copy-archive-button'); await settle();
  assert.equal(writes(env).length, 1); assert.equal(signals(env).length, 0);
  const pc = boot({ role: 'pc' }); await settle(); await pc.tick(2000);
  assert.equal(pc.calls.filter(c => c.endpoint?.includes('/api/signal/')).length, 0);
  assert.equal(pc.get('copy-paste-button').hidden, true);
});

test('receiver session changes during clipboard write do not send to new session', async () => {
  const env = boot(); await settle(); env.controls.clipboardGate = deferred();
  env.input('连接切换中'); await env.prepare(); env.click('copy-paste-button');
  env.controls.signalSession = 'replacement-session'; await env.tick(2000);
  env.controls.clipboardGate.resolve(); await settle();
  assert.equal(signals(env).length, 0); assert.equal(writes(env).length, 1);
  assert.match(env.get('signal-action-status').textContent, /状态已变化/);
});

test('pagehide and bfcache resume cannot send a delayed clipboard completion', async () => {
  const env = boot(); await settle(); env.controls.clipboardGate = deferred();
  env.input('离开前点击的文字'); await env.prepare(); env.click('copy-paste-button');
  env.event('pagehide'); env.event('pageshow'); await settle();
  env.controls.clipboardGate.resolve(); await settle();
  assert.equal(signals(env).length, 0); assert.equal(writes(env).length, 1);
});

test('expiry is uncertain and late terminal status cannot overwrite an already finished result', async () => {
  const env = boot(); await settle(); env.input('检查电脑后再试'); await env.prepare(); env.click('copy-paste-button'); await settle();
  const id = signals(env)[0].body.action_id;
  env.controls.signalAction = { action_id: id, status: 'expired' }; await env.tick(2000);
  assert.match(env.get('signal-action-status').textContent, /执行结果未确认/);
  env.controls.signalAction = { action_id: id, status: 'executed' }; await env.tick(2000);
  assert.match(env.get('signal-action-status').textContent, /执行结果未确认/);
  assert.equal(signals(env).length, 1);
});

test('PC /doubao/pc/air immediately reads durable copies even when relay state fails', async () => {
  const env = boot({
    role: 'pc', basePath: '/doubao', room: 'air', stateFailure: true,
    archives: [{ copy_id: 'saved-one', text: '生成的历史样本', copied_at: '2026-10-10T01:00:00Z' }],
  });
  await settle();
  const historyCalls = env.calls.filter(c => c.endpoint?.endsWith('/api/copy-history'));
  assert.equal(historyCalls.length, 1);
  assert.equal(historyCalls[0].url, '/doubao/api/copy-history?room_id=air');
  assert.equal(env.get('pc-output').textContent, '生成的历史样本');
  assert.equal(env.get('copy-history-list').children.length, 1);
  assert.equal(env.get('copy-button').disabled, false);
  assert.equal(signals(env).length, 0);
});

test('PC refresh retrieves new copies and later relay state cannot hide latest copy', async () => {
  const env = boot({ role: 'pc', room: 'air', archives: [{ copy_id: 'one', text: '原有样本', copied_at: '2026-10-10T01:00:00Z' }] });
  await settle();
  env.archived.push({ copy_id: 'two', text: '新复制样本', copied_at: '2026-10-10T02:00:00Z' });
  env.click('refresh-button'); await settle();
  assert.equal(env.get('pc-output').textContent, '新复制样本');
  assert.equal(env.get('copy-history-list').children.length, 2);
  env.emit({ ...env.server, text: '', history: [] });
  assert.equal(env.get('pc-output').textContent, '新复制样本');
  env.click('copy-button'); await settle();
  assert.equal(env.calls.filter(c => c.kind === 'clipboard').at(-1).text, '新复制样本');
});

test('PC first load distinguishes loading, failure and empty, with an explicit retry', async () => {
  const gate = deferred();
  const env = boot({ role: 'pc', historyGate: gate, historyFailure: true }); await settle();
  assert.match(env.get('pc-output').textContent, /正在读取/);
  assert.match(env.get('copy-history-list').innerHTML, /正在读取/);
  assert.equal(env.get('copy-button').disabled, true);
  gate.resolve(); await settle();
  assert.match(env.get('pc-output').textContent, /刷新留档/);
  assert.match(env.get('copy-history-list').innerHTML, /读取失败/);
  assert.doesNotMatch(env.get('copy-history-list').innerHTML, /还没有/);
  env.controls.historyFailure = false; env.controls.historyGate = null;
  env.click('copy-history-refresh-button'); await settle();
  assert.match(env.get('copy-history-list').innerHTML, /还没有/);
  env.archived.push({ copy_id: 'later', text: '重试后能看到', copied_at: '2026-10-10T03:00:00Z' });
  await env.tick(15000);
  assert.equal(env.get('pc-output').textContent, '重试后能看到');
  env.controls.historyFailure = true; env.click('copy-history-refresh-button'); await settle();
  assert.equal(env.get('pc-output').textContent, '重试后能看到');
  assert.match(env.get('copy-queue-status').textContent, /读取失败.*已显示的内容仍保留/);
});

test('new client remains compatible with old HTML during static asset rollout', async () => {
  const env = boot({ oldHtml: true }); await settle();
  env.input('兼容原按钮'); env.click('copy-archive-button'); await settle();
  assert.equal(writes(env).length, 1); assert.equal(signals(env).length, 0);
});

for (const status of ['cancelled', 'expired']) {
  for (const order of ['archive-first', 'action-first']) {
    test(`paste retains unchanged draft with auto-clear enabled: ${status}, ${order}`, async () => {
      const env = boot(); await settle();
      env.get('auto-clear-toggle').checked = true;
      if (order === 'action-first') env.controls.copyGate = deferred();
      env.input('本次粘贴不能因留档清空'); await env.prepare(); env.click('copy-paste-button'); await settle();
      const id = signals(env)[0].body.action_id;
      assert.equal(env.get('mobile-editor').value, '本次粘贴不能因留档清空');
      env.controls.signalAction = { action_id: id, status, reason: 'focus_changed' };
      await env.tick(2000);
      if (env.controls.copyGate) { env.controls.copyGate.resolve(); await settle(); }
      assert.match(env.get('copy-status').textContent, /已留档/);
      assert.match(env.get('signal-action-status').textContent, /草稿已保留/);
      assert.equal(env.get('mobile-editor').value, '本次粘贴不能因留档清空');
      assert.equal(env.storage.getItem('doubao-input-sync:clipboard-draft:room'), '本次粘贴不能因留档清空');
      assert.equal(env.get('manual-mode-button').disabled, false);
      if (status === 'cancelled') {
        env.click('manual-mode-button'); await settle();
        assert.equal(env.get('mobile-editor').value, '本次粘贴不能因留档清空');
        env.click('manual-send-button'); await settle();
        assert.equal(env.server.history.at(-1).text, '本次粘贴不能因留档清空');
      }
    });
  }
}

test('unknown paste retains draft despite successful archive and after reload', async () => {
  const env = boot(); await settle(); env.get('auto-clear-toggle').checked = true;
  env.controls.signalFailure = true;
  env.input('结果不明确仍保留'); await env.prepare(); env.click('copy-paste-button'); await settle();
  await env.runTimeout(15000);
  assert.match(env.get('signal-action-status').textContent, /结果未知.*草稿已保留/);
  assert.equal(env.get('mobile-editor').value, '结果不明确仍保留');
  const reopened = boot({ storage: env.storage }); await settle();
  assert.equal(reopened.get('mobile-editor').value, '结果不明确仍保留');
  assert.equal(signals(reopened).length, 0);
});

test('session change during copy retains draft even when archive succeeds and auto-clear is enabled', async () => {
  const env = boot(); await settle(); env.get('auto-clear-toggle').checked = true;
  env.controls.clipboardGate = deferred();
  env.input('连接换了仍要保留'); await env.prepare(); env.click('copy-paste-button');
  env.controls.signalSession = 'new-session'; await env.tick(2000);
  env.controls.clipboardGate.resolve(); await settle();
  assert.equal(signals(env).length, 0);
  assert.equal(env.get('mobile-editor').value, '连接换了仍要保留');
  assert.match(env.get('signal-action-status').textContent, /草稿已保留.*换气/);
});

test('late paste result and archive cannot clear newer typing with auto-clear enabled', async () => {
  const env = boot(); await settle(); env.get('auto-clear-toggle').checked = true;
  env.controls.copyGate = deferred();
  env.input('已复制的旧段落'); await env.prepare(); env.click('copy-paste-button'); await settle();
  env.input('正在写的新段落');
  const id = signals(env)[0].body.action_id;
  env.controls.signalAction = { action_id: id, status: 'executed' }; await env.tick(2000);
  env.controls.copyGate.resolve(); await settle();
  assert.equal(env.get('mobile-editor').value, '正在写的新段落');
  assert.equal(env.storage.getItem('doubao-input-sync:clipboard-draft:room'), '正在写的新段落');
});

const preparations = env => env.calls.filter(c => c.endpoint?.endsWith('/api/signal/prepare'));

test('typing prepares only exact UTF-8 fingerprint; button is available before receiver readiness', async () => {
  const env = boot(); await settle(); env.controls.prepareReady = false;
  const text = '第一行\n👨‍👩‍👧‍👦 e\u0301\r\n最后一行';
  env.input(text);
  assert.equal(env.get('copy-paste-button').disabled, false);
  assert.equal(preparations(env).length, 0);
  await env.prepare();
  assert.equal(preparations(env).length, 1);
  const body = preparations(env)[0].body;
  assert.deepEqual(Object.keys(body).sort(), ['preparation_id', 'room_id', 'session_id', 'sha256']);
  assert.equal(body.sha256, createHash('sha256').update(text, 'utf8').digest('hex'));
  assert.equal(env.get('copy-paste-button').disabled, false);
  assert.equal(env.calls.filter(c => c.kind === 'clipboard').length, 0);
  assert.equal(signals(env).length, 0); assert.equal(writes(env).length, 0); assert.equal(drafts(env).length, 0);
  env.controls.preparation.ready = true; await env.tick(2000);
  assert.equal(env.get('copy-paste-button').disabled, false);
});

test('legacy receiver cannot dispatch queued paste; timeout releases copy-only', async () => {
  const env = boot({ protocol: 'signal-v1' }); await settle(); env.input('旧接收器'); await env.prepare();
  assert.equal(env.get('copy-paste-button').disabled, false);
  assert.equal(preparations(env).length, 0);
  env.click('copy-paste-button'); await settle();
  assert.equal(signals(env).length, 0);
  assert.equal(writes(env).length, 1);
  await env.runTimeout(15000);
  assert.equal(env.get('copy-archive-button').disabled, false);
  assert.equal(env.get('mobile-editor').value, '旧接收器');
});

test('slow preparation is serialized and stale response cannot prepare a newer draft', async () => {
  const env = boot(); await settle(); env.controls.prepareGate = deferred();
  env.input('先前草稿'); await env.prepare();
  env.input('最新草稿'); await env.prepare();
  assert.equal(preparations(env).length, 1);
  env.controls.prepareGate.resolve(); await settle();
  assert.equal(env.get('copy-paste-button').disabled, false);
  env.controls.prepareGate = null; await env.prepare();
  assert.equal(preparations(env).length, 2);
  assert.equal(preparations(env)[1].body.sha256, createHash('sha256').update('最新草稿').digest('hex'));
  assert.equal(env.get('copy-paste-button').disabled, false);
});

test('preparation mismatch or expiration keeps button available without sending an action', async () => {
  const env = boot(); await settle(); env.input('等待匹配'); await env.prepare();
  env.controls.preparation = { ...env.controls.preparation, preparation_id: randomUUID() };
  await env.tick(2000); assert.equal(env.get('copy-paste-button').disabled, false);
  env.controls.prepareReady = false; await env.prepare();
  assert.equal(env.get('copy-paste-button').disabled, false);
  env.controls.preparation.ready = true; await env.tick(2000);
  assert.equal(env.get('copy-paste-button').disabled, false);
  env.controls.preparation.remaining_ms = 0; await env.tick(2000);
  assert.equal(env.get('copy-paste-button').disabled, false);
});

test('session replacement requires new preparation; old ready state cannot be used', async () => {
  const env = boot(); await settle(); env.input('换接收器'); await env.prepare();
  const old = preparations(env)[0].body;
  env.controls.signalSession = 'receiver-two'; env.controls.prepareReady = false; await env.tick(2000);
  assert.equal(env.get('copy-paste-button').disabled, false);
  await env.prepare();
  const next = preparations(env)[1].body;
  assert.equal(next.session_id, 'receiver-two'); assert.notEqual(next.preparation_id, old.preparation_id);
  assert.equal(env.get('copy-paste-button').disabled, false);
});

test('edited draft while clipboard permission waits archives snapshot but never sends stale action', async () => {
  const env = boot(); await settle(); env.input('点击时草稿'); await env.prepare();
  env.controls.clipboardGate = deferred(); env.click('copy-paste-button');
  env.input('继续写的新草稿'); env.controls.clipboardGate.resolve(); await settle();
  assert.equal(signals(env).length, 0);
  assert.equal(writes(env)[0].body.text, '点击时草稿');
  assert.equal(env.get('mobile-editor').value, '继续写的新草稿');
  assert.match(env.get('signal-action-status').textContent, /没有发送粘贴指令/);
});

test('terminal action consumes preparation; repeated same text requires a new ready preparation', async () => {
  const env = boot(); await settle(); env.input('相同文字再复制'); await env.prepare();
  const first = preparations(env)[0].body.preparation_id;
  env.click('copy-paste-button'); await settle();
  env.controls.signalAction = { action_id: signals(env)[0].body.action_id, status: 'executed' }; await env.tick(2000);
  assert.equal(env.get('copy-paste-button').disabled, true);
  env.input('相同文字再复制');
  await env.prepare();
  assert.notEqual(preparations(env)[1].body.preparation_id, first);
  assert.equal(env.get('copy-paste-button').disabled, false);
});

test('preparation refresh is passive and cannot clear the preserved draft or send a paste', async () => {
  const env = boot(); await settle(); env.get('auto-clear-toggle').checked = true;
  env.input('延长等待'); await env.prepare();
  const first = preparations(env)[0].body.preparation_id;
  env.controls.preparation.remaining_ms = 19000; await env.tick(2000); await env.prepare();
  assert.equal(preparations(env)[1].body.preparation_id, first);
  assert.equal(signals(env).length, 0); assert.equal(writes(env).length, 0);
  assert.equal(env.get('mobile-editor').value, '延长等待');
});

test('older copy-only archive cannot clear a later failed paste of the same draft', async () => {
  const env = boot(); await settle(); env.get('auto-clear-toggle').checked = true;
  env.controls.copyGate = deferred(); env.input('两次使用同一段文字');
  env.click('copy-archive-button'); await settle(); await env.tick(2000); await env.prepare();
  assert.equal(env.get('copy-paste-button').disabled, false);
  env.click('copy-paste-button'); await settle();
  env.controls.signalAction = { action_id: signals(env)[0].body.action_id, status: 'cancelled', reason: 'mismatch' };
  await env.tick(2000);
  env.controls.copyGate.resolve(); await settle();
  assert.equal(writes(env).length, 2);
  assert.equal(env.get('mobile-editor').value, '两次使用同一段文字');
  assert.match(env.get('signal-action-status').textContent, /不匹配.*草稿已保留/);
});

test('passive preparation failure never copies, clears or sends and releases manual fallback', async () => {
  const env = boot(); await settle(); env.controls.hangEndpoints.add('/api/signal/prepare');
  env.get('auto-clear-toggle').checked = true;
  env.input('准备失败保留'); await env.prepare(); await env.runTimeout(6000);
  assert.match(env.get('signal-receiver-status').textContent, /准备失败/);
  assert.equal(env.get('copy-paste-button').disabled, false);
  assert.equal(env.get('manual-mode-button').disabled, false);
  assert.equal(env.get('mobile-editor').value, '准备失败保留');
  assert.equal(signals(env).length, 0); assert.equal(writes(env).length, 0);
  assert.equal(env.calls.filter(c => c.kind === 'clipboard').length, 0);
});

test('passive preparation response after BFCache return is discarded and new generation re-prepares', async () => {
  const env = boot(); await settle(); env.controls.prepareGate = deferred();
  env.input('页面恢复后仍不自动粘贴'); await env.prepare();
  env.event('pagehide'); env.event('pageshow'); await settle();
  env.controls.prepareGate.resolve(); await settle();
  assert.equal(env.get('copy-paste-button').disabled, false);
  env.controls.prepareGate = null; await env.prepare();
  assert.equal(preparations(env).length, 2);
  assert.notEqual(preparations(env)[0].body.preparation_id, preparations(env)[1].body.preparation_id);
  assert.equal(signals(env).length, 0);
});

test('manual fallback preserves retained text against old identical desktop ACK and blank relay state until explicit send', async () => {
  const env = boot(); await settle(); env.get('auto-clear-toggle').checked = true;
  const text = '以前发送过但本次仍需兜底的正文';
  env.server.history = [{ archive_id: 1, text, desktop_received_at: 'old-ack', archived_at: 'old' }];
  env.emit({ ...env.server });
  env.input(text); await env.prepare(); env.click('copy-paste-button'); await settle();
  env.controls.signalAction = { action_id: signals(env)[0].body.action_id, status: 'cancelled', reason: 'mismatch' };
  await env.tick(2000);
  env.controls.updateGate = deferred(); env.click('manual-mode-button'); await settle();
  env.emit({ ...env.server, text: '' });
  assert.equal(env.get('mobile-editor').value, text);
  env.controls.updateGate.resolve(); await settle();
  env.emit({ ...env.server, text: '', version: env.server.version + 1 });
  assert.equal(env.get('mobile-editor').value, text);
  assert.equal(env.server.history.length, 1);
  // Once explicitly sent, a receipt for this new archive may clear normally.
  env.click('manual-send-button'); await settle();
  assert.equal(env.server.history.length, 2);
  assert.equal(env.server.history[1].text, text);
  env.server.history[1].desktop_received_at = 'new-ack'; env.emit({ ...env.server });
  assert.equal(env.get('mobile-editor').value, '');
});

test('reload into manual mode cannot apply old relay receipts to the restored clipboard draft', async () => {
  const local = storage({
    'doubao-input-sync:clipboard-draft:room': '保留的兜底草稿',
    'doubao-input-sync:auto-clear:room': 'true',
  });
  const env = boot({ storage: local, mode: 'manual' }); await settle();
  env.get('auto-clear-toggle').checked = true;
  env.emit({ ...env.server, text: '', history: [{ archive_id: 1, text: '保留的兜底草稿', desktop_received_at: 'old-ack' }] });
  assert.equal(env.get('mobile-editor').value, '保留的兜底草稿');
  assert.equal(signals(env).length, 0);
});

for (const order of ['archive-first', 'action-first']) {
  test(`executed paste clears only its unchanged draft, independent of copy-only toggle: ${order}`, async () => {
    const env = boot(); await settle(); env.get('auto-clear-toggle').checked = false;
    if (order === 'action-first') env.controls.copyGate = deferred();
    env.input('成功后清空这一段'); await env.prepare(); env.click('copy-paste-button'); await settle();
    assert.equal(env.get('mobile-editor').value, '成功后清空这一段');
    const id = signals(env)[0].body.action_id;
    env.controls.signalAction = { action_id: id, status: 'delivered' }; await env.tick(2000);
    assert.equal(env.get('mobile-editor').value, '成功后清空这一段');
    env.controls.signalAction = { action_id: id, status: 'executed' }; await env.tick(2000);
    assert.equal(env.get('mobile-editor').value, '');
    assert.equal(env.storage.getItem('doubao-input-sync:clipboard-draft:room'), '');
    assert.match(env.get('signal-action-status').textContent, /已执行.*已清空/);
    if (env.controls.copyGate) { env.controls.copyGate.resolve(); await settle(); }
    assert.equal(writes(env)[0].body.text, '成功后清空这一段');
    env.click('restore-last-button'); await settle();
    assert.equal(env.get('mobile-editor').value, '成功后清空这一段');
    await env.tick(2000);
    assert.equal(env.get('mobile-editor').value, '成功后清空这一段');
    assert.equal(signals(env).length, 1);
  });
}

test('executed receipt cannot clear text edited away and back to the same value', async () => {
  const env = boot(); await settle();
  env.input('重复内容'); await env.prepare(); env.click('copy-paste-button'); await settle();
  env.input('新的修改'); env.input('重复内容');
  env.controls.signalAction = { action_id: signals(env)[0].body.action_id, status: 'executed' }; await env.tick(2000);
  assert.equal(env.get('mobile-editor').value, '重复内容');
});

test('executed receipt after observation timeout cannot clear a retained draft', async () => {
  const env = boot(); await settle();
  env.input('未知时保留'); await env.prepare(); env.click('copy-paste-button'); await settle();
  await env.runTimeout(15000);
  env.controls.signalAction = { action_id: signals(env)[0].body.action_id, status: 'executed' }; await env.tick(2000);
  assert.equal(env.get('mobile-editor').value, '未知时保留');
});

test('click before preparation copies immediately and queues exactly one action until matching ready', async () => {
  const env = boot(); await settle(); env.controls.prepareReady = false;
  env.input('说完直接点');
  assert.equal(env.get('copy-paste-button').disabled, false);
  const before = env.calls.length;
  env.click('copy-paste-button');
  assert.deepEqual(env.calls.slice(before), [{ kind: 'clipboard', text: '说完直接点' }]);
  env.click('copy-paste-button'); env.click('copy-archive-button'); env.click('copy-button');
  env.click('manual-mode-button'); env.click('auto-mode-button');
  await settle(); await env.prepare();
  assert.equal(env.calls.filter(c => c.kind === 'clipboard').length, 1);
  assert.equal(writes(env).length, 1);
  assert.equal(preparations(env).length, 1);
  assert.equal(signals(env).length, 0);
  assert.equal(env.get('composer-shell').dataset.captureMode, 'clipboard');
  for (const id of ['copy-paste-button', 'copy-archive-button', 'manual-mode-button', 'auto-mode-button']) {
    assert.equal(env.get(id).disabled, true, id);
  }
  assert.equal(env.get('mobile-editor').value, '说完直接点');
  env.controls.preparation.ready = true; await env.tick(2000); await env.tick(2000);
  assert.equal(signals(env).length, 1);
  assert.equal(signals(env)[0].body.preparation_id, preparations(env)[0].body.preparation_id);
  assert.equal(Object.hasOwn(signals(env)[0].body, 'text'), false);
});

test('offline click waits and binds first ready receiver without another clipboard write', async () => {
  const env = boot({ signalReady: false }); await settle();
  env.input('电脑稍后上线'); env.click('copy-paste-button'); await settle(); await env.prepare();
  assert.equal(signals(env).length, 0); assert.equal(preparations(env).length, 0);
  assert.equal(writes(env).length, 1);
  env.controls.signalReady = true; env.controls.signalSession = 'newly-online';
  await env.tick(2000); await env.prepare(); await env.tick(2000);
  assert.equal(signals(env).length, 1);
  assert.equal(signals(env)[0].body.session_id, 'newly-online');
  assert.equal(env.calls.filter(c => c.kind === 'clipboard').length, 1);
});

test('queued timeout preserves draft and never sends on late readiness or reload', async () => {
  const env = boot({ signalReady: false }); await settle();
  env.input('等待超时保留我'); env.click('copy-paste-button'); await settle();
  assert.equal(await env.runTimeout(15000), true);
  assert.equal(env.get('mobile-editor').value, '等待超时保留我');
  assert.equal(env.get('manual-mode-button').disabled, false);
  assert.match(env.get('signal-action-status').textContent, /超时|未就绪/);
  env.controls.signalReady = true; await env.tick(2000); await env.prepare(); await env.tick(2000);
  assert.equal(signals(env).length, 0);
  const reopened = boot({ storage: env.storage }); await settle(); await reopened.prepare(); await reopened.tick(2000);
  assert.equal(signals(reopened).length, 0);
  assert.equal(reopened.get('mobile-editor').value, '等待超时保留我');
  assert.equal(env.calls.filter(c => c.kind === 'clipboard').length, 1);
});

test('editing or clearing queued draft cancels paste even if the original text returns', async () => {
  for (const change of ['edit', 'clear']) {
    const env = boot(); await settle(); env.controls.prepareReady = false;
    env.input('点击时文字'); env.click('copy-paste-button'); await settle(); await env.prepare();
    if (change === 'edit') env.input('新草稿'); else env.click('clear-button');
    await settle();
    env.input('点击时文字');
    env.controls.prepareReady = true; await env.prepare(); await env.tick(2000);
    assert.equal(signals(env).length, 0, change);
    assert.equal(env.get('manual-mode-button').disabled, false, change);
    assert.equal(env.get('mobile-editor').value, '点击时文字');
    assert.equal(writes(env).length, 1);
  }
});

test('pagehide cancels ready-waiting queue; bfcache return cannot resume it', async () => {
  const env = boot(); await settle(); env.controls.prepareReady = false;
  env.input('离开页面取消等待'); env.click('copy-paste-button'); await settle(); await env.prepare();
  env.event('pagehide'); env.event('pageshow'); await settle();
  env.controls.prepareReady = true; await env.prepare(); await env.tick(2000);
  assert.equal(signals(env).length, 0);
  assert.equal(env.get('mobile-editor').value, '离开页面取消等待');
  assert.equal(env.get('manual-mode-button').disabled, false);
});

test('receiver replacement cancels queued preparation and cannot paste into new session', async () => {
  const env = boot(); await settle(); env.controls.prepareReady = false;
  env.input('固定本次接收器'); env.click('copy-paste-button'); await settle(); await env.prepare();
  env.controls.signalSession = 'replacement-session'; env.controls.prepareReady = true;
  await env.tick(2000); await env.prepare(); await env.tick(2000);
  assert.equal(signals(env).length, 0);
  assert.equal(env.get('mobile-editor').value, '固定本次接收器');
  assert.equal(env.get('manual-mode-button').disabled, false);
});

test('clipboard permission resolving after click deadline cannot dispatch even if timer was throttled', async () => {
  const env = boot(); await settle(); env.input('权限等待过久'); await env.prepare();
  env.controls.clipboardGate = deferred(); env.click('copy-paste-button');
  env.advance(15001);
  env.controls.clipboardGate.resolve(); await settle(); await env.tick(2000);
  assert.equal(signals(env).length, 0);
  assert.equal(writes(env).length, 1);
  assert.equal(env.get('mobile-editor').value, '权限等待过久');
  assert.equal(env.get('manual-mode-button').disabled, false);
});

test('late ready callback cannot dispatch an expired queue while browser timer is throttled', async () => {
  const env = boot(); await settle(); env.controls.prepareReady = false;
  env.input('后台计时器延迟'); env.click('copy-paste-button'); await settle(); await env.prepare();
  env.advance(15001);
  env.controls.preparation.ready = true; await env.tick(2000);
  assert.equal(signals(env).length, 0);
  assert.equal(env.get('mobile-editor').value, '后台计时器延迟');
  assert.equal(env.get('manual-mode-button').disabled, false);
});

test('queued click refuses unrelated ready preparation until its exact fingerprint is ready', async () => {
  const env = boot(); await settle(); env.controls.prepareReady = false;
  env.input('只接受本次指纹'); env.click('copy-paste-button'); await settle(); await env.prepare();
  env.controls.preparation = { ...env.controls.preparation, ready: true, preparation_id: randomUUID(), sha256: '0'.repeat(64) };
  await env.tick(2000);
  assert.equal(signals(env).length, 0);
  env.controls.prepareReady = true; await env.prepare(); await env.tick(2000);
  assert.equal(signals(env).length, 1);
  assert.equal(signals(env)[0].body.preparation_id, preparations(env).at(-1).body.preparation_id);
  assert.equal(preparations(env).at(-1).body.sha256, createHash('sha256').update('只接受本次指纹').digest('hex'));
});

test('paste_failed means unconfirmed: delayed archive and late receipt cannot clear or resend', async () => {
  const env = boot(); await settle(); env.get('auto-clear-toggle').checked = true;
  env.controls.copyGate = deferred();
  env.input('先检查电脑再决定是否重试'); await env.prepare(); env.click('copy-paste-button'); await settle();
  const id = signals(env)[0].body.action_id;
  env.controls.signalAction = { action_id: id, status: 'cancelled', reason: 'paste_failed' };
  await env.tick(2000);
  const message = env.get('signal-action-status').textContent;
  assert.match(message, /粘贴结果未确认.*草稿已保留.*不会自动重发.*先检查电脑.*确认没有粘贴后.*重试.*换气/);
  assert.doesNotMatch(message, /粘贴失败|电脑未能执行|已清空/);
  env.controls.copyGate.resolve(); await settle();
  assert.equal(env.get('mobile-editor').value, '先检查电脑再决定是否重试');
  assert.equal(env.storage.getItem('doubao-input-sync:clipboard-draft:room'), '先检查电脑再决定是否重试');
  assert.equal(env.get('manual-mode-button').disabled, false);
  env.event('online'); await settle(); await env.prepare(); await env.tick(2000);
  env.controls.signalAction = { action_id: id, status: 'executed' }; await env.tick(2000);
  assert.equal(env.get('signal-action-status').textContent, message);
  assert.equal(env.get('mobile-editor').value, '先检查电脑再决定是否重试');
  assert.equal(signals(env).length, 1);
  const reopened = boot({ storage: env.storage }); await settle(); await reopened.prepare(); await reopened.tick(2000);
  assert.equal(signals(reopened).length, 0);
  assert.equal(reopened.get('mobile-editor').value, '先检查电脑再决定是否重试');
});
