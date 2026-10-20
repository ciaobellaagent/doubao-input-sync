const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { randomUUID, webcrypto } = require('node:crypto');

const source = fs.readFileSync(path.join(__dirname, '../app/static/attachments.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '../app/static/index.html'), 'utf8');
const settle = async () => { for (let i = 0; i < 12; i++) await new Promise(setImmediate); };
const deferred = () => { let resolve; let reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };
const file = (name = 'sample.png', bytes = 1024, type = 'image/png') => ({ name, size: bytes, type });
const meta = (id = randomUUID(), filename = 'sample.png') => ({ attachment_id: id, filename, size: 1024, sha256: 'a'.repeat(64), media_type: 'image/png', created_at: '2026-10-20T12:00:00Z' });

function boot(options = {}) {
  const elements = new Map();
  const calls = [];
  const timers = new Map();
  const intervals = new Map();
  const events = new Map();
  let nextTimer = 0;
  let now = Date.now();
  class ClockDate extends Date { static now() { return now; } }
  function element(tag = 'div') {
    const listeners = new Map();
    return {
      tagName: tag, textContent: '', className: '', value: '', disabled: false, hidden: false, open: false,
      dataset: {}, children: [], attributes: {}, files: [],
      addEventListener(name, fn) { const list = listeners.get(name) || []; list.push(fn); listeners.set(name, list); },
      dispatch(name, event = {}) { for (const fn of listeners.get(name) || []) fn(event); },
      click() { this.dispatch('click'); },
      appendChild(child) { this.children.push(child); return child; },
      replaceChildren(...children) { this.children = children; },
      setAttribute(name, value) { this.attributes[name] = value; },
    };
  }
  for (const match of html.matchAll(/id="([^"]+)"/g)) elements.set(match[1], element());
  elements.get('mobile-editor').value = '保留原来的文字草稿';
  elements.get('attachment-status').textContent = '正在读取附件留档…';
  const response = (payload, status = 200) => ({ ok: status < 400, status, json: async () => payload });
  const server = {
    ready: options.ready !== false, session: 'attachment-receiver', items: options.items || [], preparation: null, action: null,
    prepReady: options.prepReady !== false,
  };
  const controls = { uploadGate: null, actionGate: null, actionHang: false, statusGate: null, failUpload: false, failStatus: false, uploadRejection: null, actionRejection: null, prepareRejection: null };
  const bridge = { busy: false, textBusy: false, setBusy(value) { this.busy = value; }, isTextBusy() { return this.textBusy; } };
  const fetch = async (url, request = {}) => {
    const parsed = new URL(url, 'https://example.test');
    const endpoint = parsed.pathname;
    const body = typeof request.body === 'string' ? JSON.parse(request.body) : request.body;
    calls.push({ url, endpoint, body, request });
    if (endpoint.endsWith('/api/attachments')) {
      if (controls.statusGate) return controls.statusGate.promise;
      if (controls.failStatus) throw new Error('offline');
      return response({ ok: true, items: server.items, receiver: { ready: server.ready, session_id: server.ready ? server.session : null }, preparation: server.preparation, action: server.action, limits: { max_file_bytes: options.limit || 20 * 1024 * 1024, max_total_bytes: 100 * 1024 * 1024 } });
    }
    if (endpoint.endsWith('/upload')) {
      if (controls.uploadGate) return controls.uploadGate.promise;
      if (controls.failUpload) throw new Error('offline');
      if (controls.uploadRejection) return response({ ok: false, error: controls.uploadRejection.code }, controls.uploadRejection.status);
      const attachment = { ...meta(parsed.searchParams.get('attachment_id'), body.name), size: body.size, media_type: body.type };
      server.items = [attachment, ...server.items];
      return response({ ok: true, attachment });
    }
    if (endpoint.endsWith('/prepare')) {
      if (controls.prepareRejection) return response({ ok: false, error: controls.prepareRejection.code }, controls.prepareRejection.status);
      server.preparation = { preparation_id: body.preparation_id, attachment: server.items.find(item => item.attachment_id === body.attachment_id), ready: server.prepReady, remaining_ms: 30000 };
      return response({ ok: true, preparation: server.preparation });
    }
    if (endpoint.endsWith('/action')) {
      if (controls.actionRejection) return response({ ok: false, error: controls.actionRejection.code }, controls.actionRejection.status);
      if (controls.actionGate) return controls.actionGate.promise;
      if (controls.actionHang) return new Promise((resolve, reject) => request.signal.addEventListener('abort', () => reject(new Error('aborted')), { once: true }));
      server.action = { action_id: body.action_id, status: 'accepted' };
      return response({ ok: true, action: server.action });
    }
    if (endpoint.endsWith('/delete')) { server.items = server.items.filter(item => item.attachment_id !== body.attachment_id); return response({ ok: true }); }
    throw new Error(`unexpected endpoint ${endpoint}`);
  };
  const window = {
    doubaoAttachmentBridge: options.noBridge ? undefined : bridge,
    location: { pathname: `${options.basePath || ''}/${options.role || 'mobile'}/${encodeURIComponent(options.room || 'room')}` },
    crypto: options.noRandomUUID ? { getRandomValues: bytes => webcrypto.getRandomValues(bytes) } : { randomUUID, getRandomValues: bytes => webcrypto.getRandomValues(bytes) },
    addEventListener: (name, fn) => events.set(name, fn),
    setTimeout: (fn, delay) => { const id = ++nextTimer; timers.set(id, { fn, delay }); return id; },
    clearTimeout: id => timers.delete(id),
    setInterval: (fn, delay) => { const id = ++nextTimer; intervals.set(id, { fn, delay }); return id; },
  };
  const document = {
    documentElement: { dataset: { appBasePath: options.basePath || '' } }, visibilityState: 'visible',
    getElementById: id => elements.get(id), createElement: element,
    addEventListener: (name, fn) => events.set(`document:${name}`, fn),
  };
  vm.runInNewContext(source, { window, document, fetch, AbortController, Uint8Array, Date: ClockDate, Map, Set, Promise, Array, Object, Number, JSON, String, Boolean, Error, encodeURIComponent, decodeURIComponent });
  return {
    calls, server, controls, response, document, bridge,
    get: id => elements.get(id),
    click: id => elements.get(id).dispatch('click'),
    event: name => events.get(name)?.(),
    advanceTime: ms => { now += ms; },
    choose(nextFile) { elements.get('attachment-file-input').files = [nextFile]; elements.get('attachment-file-input').dispatch('change'); },
    transfer(name, files = [], items = []) {
      const event = { prevented: false, preventDefault() { this.prevented = true; } };
      event[name === 'paste' ? 'clipboardData' : 'dataTransfer'] = { files, items };
      elements.get('mobile-editor').dispatch(name, event); return event;
    },
    async interval() { for (const timer of intervals.values()) timer.fn(); await settle(); },
    async timeout(delay) { for (const [id, timer] of [...timers]) if (timer.delay === delay) { timers.delete(id); timer.fn(); } await settle(); },
  };
}
const callsTo = (env, end) => env.calls.filter(call => call.endpoint.endsWith(end));
async function uploaded(options = {}) { const env = boot(options); await settle(); env.choose(file()); env.click('attachment-upload-button'); await settle(); return env; }
const rowActions = row => row.children.find(child => child.className === 'attachment-item-actions').children;

test('assets are included and attachment controls preserve the compact text toolbar', () => {
  assert.match(html, /assets\/attachments\.css/);
  assert.match(html, /assets\/attachments\.js/);
  assert.match(html, /<details id="attachment-details">/);
  assert.match(html, /id="attachment-image-input" type="file" accept="image\/\*"/);
  assert.match(html, /id="copy-paste-button"/);
});

test('ordinary text paste and text drop are not intercepted or edited', async () => {
  const env = boot(); await settle();
  assert.equal(env.transfer('paste').prevented, false);
  assert.equal(env.transfer('drop').prevented, false);
  assert.equal(env.transfer('dragover').prevented, false);
  assert.equal(env.get('mobile-editor').value, '保留原来的文字草稿');
  assert.equal(callsTo(env, '/upload').length, 0);
});

test('actual file paste, clipboard items and drop select one file without auto upload or paste', async () => {
  const env = boot(); await settle();
  assert.equal(env.transfer('paste', [file('pasted.png')]).prevented, true);
  assert.match(env.get('attachment-selection').textContent, /pasted\.png/);
  assert.equal(env.transfer('paste', [], [{ kind: 'file', getAsFile: () => file('item.png') }]).prevented, true);
  assert.match(env.get('attachment-selection').textContent, /item\.png/);
  assert.equal(env.transfer('paste', [], [{ kind: 'file', getAsFile: () => null }]).prevented, false);
  assert.equal(env.transfer('drop', [file('drop.pdf', 2048, 'application/pdf')]).prevented, true);
  assert.match(env.get('attachment-selection').textContent, /drop\.pdf/);
  assert.equal(env.get('attachment-details').open, true);
  assert.equal(callsTo(env, '/upload').length, 0);
  assert.equal(callsTo(env, '/action').length, 0);
  assert.equal(env.get('mobile-editor').value, '保留原来的文字草稿');
});

test('explicit upload sends raw file, safe filename headers and room scoped request', async () => {
  const env = boot({ basePath: '/doubao', room: 'room 中文' }); await settle();
  const selected = file('图片 🧪.png'); env.choose(selected);
  assert.equal(callsTo(env, '/upload').length, 0);
  assert.equal(env.get('attachment-paste-button').disabled, true);
  env.click('attachment-upload-button'); await settle();
  const call = callsTo(env, '/upload')[0];
  assert.equal(call.body, selected);
  assert.equal(call.request.headers['Content-Type'], 'application/octet-stream');
  assert.equal(call.request.headers['X-Attachment-Name'], encodeURIComponent(selected.name));
  assert.equal(new URL(call.url, 'https://example.test').searchParams.get('room_id'), 'room 中文');
  assert.match(call.url, /^\/doubao\/api\/attachments\/upload/);
  assert.equal(env.get('attachment-paste-button').disabled, false);
  assert.match(env.get('attachment-status').textContent, /已留档/);
  assert.equal(callsTo(env, '/action').length, 0);
});

test('oversized and server reduced size limits prevent uploading', async () => {
  const env = boot({ limit: 1024 }); await settle();
  env.choose(file('large', 1025)); env.click('attachment-upload-button'); await settle();
  assert.match(env.get('attachment-status').textContent, /附件过大/);
  assert.equal(callsTo(env, '/upload').length, 0);
  const big = boot(); await settle(); big.choose(file('over20', 20 * 1024 * 1024 + 1));
  assert.equal(big.get('attachment-upload-button').disabled, true);
});

test('a zero-byte ordinary file can be uploaded without changing the text draft', async () => {
  const env = boot(); await settle(); env.choose(file('empty.txt', 0, 'text/plain'));
  env.click('attachment-upload-button'); await settle();
  assert.equal(callsTo(env, '/upload').length, 1);
  assert.equal(callsTo(env, '/upload')[0].body.size, 0);
  assert.match(env.get('attachment-selection').textContent, /empty\.txt/);
  assert.equal(env.get('mobile-editor').value, '保留原来的文字草稿');
});

test('ready attachment sends one action despite repeated clicks and polls, retains text and archive', async () => {
  const env = await uploaded();
  env.click('attachment-paste-button'); env.click('attachment-paste-button'); await settle();
  assert.equal(callsTo(env, '/action').length, 1);
  const actionCall = callsTo(env, '/action')[0];
  assert.deepEqual(Object.keys(actionCall.body).sort(), ['action_id', 'preparation_id', 'room_id', 'session_id']);
  assert.match(actionCall.body.action_id, /^[0-9a-f-]{36}$/);
  await env.interval(); await env.interval();
  assert.equal(callsTo(env, '/action').length, 1);
  env.server.action.status = 'executed'; await env.interval();
  assert.match(env.get('attachment-status').textContent, /已调用粘贴，请在电脑确认附件/);
  assert.match(env.get('attachment-status').textContent, /目标应用需支持/);
  assert.doesNotMatch(env.get('attachment-status').textContent, /粘贴成功/);
  assert.equal(env.get('mobile-editor').value, '保留原来的文字草稿');
  assert.equal(env.get('attachment-list').children.length, 1);
});

test('click waits for receiver ready and then dispatches exactly once', async () => {
  const env = await uploaded({ ready: false });
  assert.equal(env.get('attachment-paste-button').disabled, false);
  env.click('attachment-paste-button'); await settle();
  assert.equal(callsTo(env, '/action').length, 0);
  env.server.ready = true; await env.interval();
  assert.equal(callsTo(env, '/action').length, 1);
  await env.interval(); assert.equal(callsTo(env, '/action').length, 1);
});

test('waiting timeout cancels intent permanently, later readiness never causes paste', async () => {
  const env = await uploaded({ ready: false });
  env.click('attachment-paste-button'); await settle(); await env.timeout(15000);
  assert.match(env.get('attachment-status').textContent, /未发送粘贴指令/);
  env.server.ready = true; await env.interval(); await env.interval();
  assert.equal(callsTo(env, '/action').length, 0);
  assert.equal(env.get('attachment-list').children.length, 1);
});

test('uncertain action HTTP response is queried but never reposted after timeout or online', async () => {
  const env = await uploaded(); env.controls.actionHang = true;
  env.click('attachment-paste-button'); await settle(); await env.timeout(6000);
  assert.match(env.get('attachment-status').textContent, /不会重新发送/);
  env.event('online'); await env.interval(); await env.timeout(15000); await env.interval();
  assert.equal(callsTo(env, '/action').length, 1);
  assert.match(env.get('attachment-status').textContent, /不会自动重发/);
  assert.equal(env.get('mobile-editor').value, '保留原来的文字草稿');
});

test('pagehide and BFCache pageshow never restore a queued paste', async () => {
  const env = await uploaded({ ready: false });
  env.click('attachment-paste-button'); await settle();
  env.event('pagehide'); env.server.ready = true; env.event('pageshow'); await settle(); await env.interval();
  assert.equal(callsTo(env, '/action').length, 0);
  assert.match(env.get('attachment-status').textContent, /等待中的附件粘贴已取消/);
});

test('editing text or entering background cancels only the unsent attachment intent', async () => {
  const env = await uploaded({ ready: false });
  env.click('attachment-paste-button'); await settle(); env.get('mobile-editor').dispatch('input');
  env.server.ready = true; await env.interval(); assert.equal(callsTo(env, '/action').length, 0);
  const hidden = await uploaded({ ready: false }); hidden.click('attachment-paste-button'); await settle();
  hidden.document.visibilityState = 'hidden'; hidden.event('document:visibilitychange');
  hidden.server.ready = true; hidden.document.visibilityState = 'visible'; hidden.event('document:visibilitychange'); await settle();
  assert.equal(callsTo(hidden, '/action').length, 0);
});

test('choosing a new attachment invalidates a queued intent and late action response', async () => {
  const env = await uploaded({ ready: false }); env.click('attachment-paste-button'); await settle();
  env.choose(file('next.png')); env.server.ready = true; await env.interval();
  assert.equal(callsTo(env, '/action').length, 0);
  const sent = await uploaded(); sent.controls.actionGate = deferred(); sent.click('attachment-paste-button'); await settle();
  const actionId = callsTo(sent, '/action')[0].body.action_id;
  sent.choose(file('next.png'));
  assert.equal(sent.bridge.busy, true, 'sent action keeps the clipboard lock until its result is observed');
  sent.controls.actionGate.resolve(sent.response({ ok: true, action: { action_id: actionId, status: 'executed' } })); await settle();
  assert.match(sent.get('attachment-selection').textContent, /next\.png/);
  assert.doesNotMatch(sent.get('attachment-status').textContent, /已调用粘贴/);
  assert.equal(sent.bridge.busy, false);
});

test('late upload response cannot replace a newly selected file', async () => {
  const env = boot(); await settle(); env.controls.uploadGate = deferred();
  env.choose(file('old.png')); env.click('attachment-upload-button'); await settle();
  env.choose(file('new.pdf', 1000, 'application/pdf'));
  env.controls.uploadGate.resolve(env.response({ ok: true, attachment: meta(randomUUID(), 'old.png') })); await settle();
  assert.match(env.get('attachment-selection').textContent, /new\.pdf/);
  assert.equal(env.get('attachment-upload-button').disabled, false);
  assert.equal(env.get('attachment-paste-button').disabled, true);
});

test('PC archives show bounded history and room scoped download URLs without interpreting filenames', async () => {
  const badName = '<img src=x onerror=alert(1)>.svg';
  const items = Array.from({ length: 55 }, (_, i) => meta(randomUUID(), i ? `${i}.txt` : badName));
  const env = boot({ role: 'pc', basePath: '/doubao', room: '私有 房间', items }); await settle();
  assert.equal(env.get('attachment-mobile-controls').hidden, true);
  assert.equal(env.get('attachment-list').children.length, 50);
  const first = env.get('attachment-list').children[0];
  assert.equal(first.children[1].children[0].textContent, badName);
  const [download] = rowActions(first);
  const parsed = new URL(download.href, 'https://example.test');
  assert.equal(parsed.pathname, '/doubao/api/attachments/blob');
  assert.equal(parsed.searchParams.get('room_id'), '私有 房间');
  assert.equal(parsed.searchParams.get('attachment_id'), items[0].attachment_id);
  assert.equal(download.attributes.download, badName);
  assert.equal(rowActions(first).some(item => item.textContent === '选择以粘贴'), false);
  assert.equal(callsTo(env, '/prepare').length, 0);
});

test('failure explicitly preserves attachments, never claims unsupported targets succeeded', async () => {
  const env = await uploaded(); env.click('attachment-paste-button'); await settle();
  env.server.action.status = 'cancelled'; env.server.action.reason = 'target_unsupported'; await env.interval();
  assert.match(env.get('attachment-status').textContent, /目标应用需支持图片或文件/);
  assert.match(env.get('attachment-status').textContent, /留档已保留/);
  assert.equal(callsTo(env, '/action').length, 1);
  const old = await uploaded({ ready: false });
  assert.match(old.get('attachment-receiver-status').textContent, /旧版接收器需更新/);
});

test('deleting a selected attachment cancels its queued paste', async () => {
  const env = await uploaded({ ready: false }); env.click('attachment-paste-button'); await settle();
  const remove = rowActions(env.get('attachment-list').children[0]).find(item => item.textContent === '删除留档');
  remove.click(); await settle(); env.server.ready = true; await env.interval();
  assert.equal(callsTo(env, '/delete').length, 1);
  assert.equal(callsTo(env, '/action').length, 0);
  assert.equal(env.get('attachment-list').children[0].className, 'attachment-empty');
});

test('UUID fallback works in HTTP contexts where randomUUID is unavailable', async () => {
  const env = await uploaded({ noRandomUUID: true });
  const upload = callsTo(env, '/upload')[0];
  assert.match(new URL(upload.url, 'https://example.test').searchParams.get('attachment_id'), /^[0-9a-f-]{8}-[0-9a-f-]{4}-4[0-9a-f-]{3}-[89ab][0-9a-f-]{3}-[0-9a-f-]{12}$/);
});

test('attachment actions coordinate with text busy bridge and release on terminal or cancellation', async () => {
  const env = await uploaded(); env.bridge.textBusy = true;
  env.click('attachment-paste-button'); await settle();
  assert.equal(callsTo(env, '/action').length, 0);
  assert.match(env.get('attachment-status').textContent, /文字粘贴仍在进行/);
  assert.equal(env.bridge.busy, false);
  env.bridge.textBusy = false; env.click('attachment-paste-button'); await settle();
  assert.equal(env.bridge.busy, true);
  env.server.action.status = 'executed'; await env.interval(); assert.equal(env.bridge.busy, false);
  const waiting = await uploaded({ ready: false }); waiting.click('attachment-paste-button'); await settle();
  assert.equal(waiting.bridge.busy, true);
  waiting.event('pagehide'); assert.equal(waiting.bridge.busy, false);
  const independent = await uploaded({ noBridge: true }); independent.click('attachment-paste-button'); await settle();
  assert.equal(callsTo(independent, '/action').length, 1);
});

test('throttled browser timers cannot dispatch a ready attachment after the click deadline', async () => {
  const env = await uploaded({ ready: false }); env.click('attachment-paste-button'); await settle();
  env.advanceTime(15001); env.server.ready = true;
  await env.interval(); await env.interval();
  assert.equal(callsTo(env, '/action').length, 0);
  assert.match(env.get('attachment-status').textContent, /等待电脑准备已超时/);
  assert.equal(env.bridge.busy, false);
});

test('status response started before pagehide cannot revive old readiness after BFCache restore', async () => {
  const env = await uploaded({ ready: false });
  env.click('attachment-paste-button'); await settle();
  const gate = deferred(); env.controls.statusGate = gate;
  await env.interval(); env.event('pagehide');
  env.controls.statusGate = null; env.server.ready = true;
  env.event('pageshow'); await settle();
  gate.resolve(env.response({ ok: true, items: [meta(randomUUID(), 'stale.pdf')], receiver: { ready: true, session_id: 'old-session' }, preparation: null, action: null }));
  await settle(); await env.interval();
  assert.equal(callsTo(env, '/action').length, 0);
  assert.equal(env.get('attachment-list').children[0].children[1].children[0].textContent, 'sample.png');
});

test('a receiver preparation failure cancels an unsent click and retries only on another explicit click', async () => {
  const env = await uploaded({ ready: false }); env.click('attachment-paste-button'); await settle();
  env.server.ready = true; env.server.prepReady = false; await env.interval();
  assert.equal(callsTo(env, '/prepare').length, 1);
  env.server.preparation.error = 'download_failed'; await env.interval();
  assert.match(env.get('attachment-status').textContent, /电脑下载附件失败/);
  assert.match(env.get('attachment-status').textContent, /未发送粘贴指令/);
  assert.equal(env.bridge.busy, false);
  env.advanceTime(70000); await env.interval(); await env.interval();
  assert.equal(callsTo(env, '/prepare').length, 1, 'failure must not silently retry after preparation expiration');
  env.server.ready = false; await env.interval(); env.server.ready = true; env.server.session = 'new-receiver'; await env.interval();
  assert.equal(callsTo(env, '/prepare').length, 1, 'a reconnect must not silently retry a known preparation failure');
  assert.equal(callsTo(env, '/action').length, 0);
  env.server.prepReady = true; env.click('attachment-paste-button'); await settle();
  assert.equal(callsTo(env, '/prepare').length, 2);
  assert.equal(callsTo(env, '/action').length, 1);
});

test('known preparation rejection releases the click and does not retry in polling', async () => {
  const env = await uploaded({ ready: false });
  env.controls.prepareRejection = { code: 'attachment_receiver_unavailable', status: 409 };
  env.click('attachment-paste-button'); await settle(); env.server.ready = true; await env.interval();
  assert.equal(env.bridge.busy, false);
  assert.match(env.get('attachment-status').textContent, /电脑附件接收器未就绪/);
  await env.interval(); await env.interval();
  assert.equal(callsTo(env, '/prepare').length, 1);
  assert.equal(callsTo(env, '/action').length, 0);
});

test('known upload quota or size rejection is reported as a rejection, preserving the local file', async () => {
  for (const [code, expected] of [['attachment_quota_reached', /留档容量已满/], ['attachment_too_large', /大小限制/]]) {
    const env = boot(); await settle(); env.controls.uploadRejection = { code, status: 413 };
    env.choose(file()); env.click('attachment-upload-button'); await settle();
    assert.match(env.get('attachment-status').textContent, expected);
    assert.match(env.get('attachment-status').textContent, /文件仍保留在手机/);
    assert.doesNotMatch(env.get('attachment-status').textContent, /结果暂未确认/);
    assert.equal(env.get('attachment-upload-button').disabled, false);
    assert.equal(callsTo(env, '/action').length, 0);
  }
});

test('known action 4xx rejection immediately releases the lock without automatic action retry', async () => {
  const env = await uploaded();
  env.controls.actionRejection = { code: 'preparation_not_ready', status: 409 };
  env.click('attachment-paste-button'); await settle();
  assert.match(env.get('attachment-status').textContent, /服务器未接受这次附件粘贴/);
  assert.equal(env.bridge.busy, false);
  await env.interval(); await env.interval();
  assert.equal(callsTo(env, '/action').length, 1);
  assert.equal(env.get('attachment-list').children.length, 1);
});

test('action 5xx remains uncertain and is never retried', async () => {
  const env = await uploaded();
  env.controls.actionRejection = { code: 'temporary_server_error', status: 503 };
  env.click('attachment-paste-button'); await settle();
  assert.match(env.get('attachment-status').textContent, /指令结果暂未确认/);
  assert.equal(env.bridge.busy, true);
  await env.interval(); await env.timeout(15000); await env.interval();
  assert.equal(callsTo(env, '/action').length, 1);
  assert.equal(env.bridge.busy, false);
});

test('replacing a known receiver session cancels a queued attachment without dispatching to the new session', async () => {
  const env = await uploaded({ prepReady: false });
  env.click('attachment-paste-button'); await settle();
  assert.equal(env.bridge.busy, true);
  env.server.session = 'replacement-receiver'; env.server.prepReady = true; await env.interval(); await env.interval();
  assert.equal(callsTo(env, '/action').length, 0);
  assert.equal(env.bridge.busy, false);
  assert.match(env.get('attachment-status').textContent, /电脑接收器连接已更新/);
});

test('offline click binds the first ready session, and a second session replacement cancels that binding', async () => {
  const first = await uploaded({ ready: false }); first.click('attachment-paste-button'); await settle();
  first.server.session = 'first-online-receiver'; first.server.ready = true; await first.interval();
  assert.equal(callsTo(first, '/action').length, 1);
  assert.equal(callsTo(first, '/action')[0].body.session_id, 'first-online-receiver');

  const replaced = await uploaded({ ready: false }); replaced.click('attachment-paste-button'); await settle();
  replaced.server.session = 'first-online-receiver'; replaced.server.ready = true; replaced.server.prepReady = false; await replaced.interval();
  assert.equal(callsTo(replaced, '/action').length, 0);
  replaced.server.session = 'second-online-receiver'; replaced.server.prepReady = true; await replaced.interval(); await replaced.interval();
  assert.equal(callsTo(replaced, '/action').length, 0);
  assert.match(replaced.get('attachment-status').textContent, /已取消等待中的附件粘贴/);
});
