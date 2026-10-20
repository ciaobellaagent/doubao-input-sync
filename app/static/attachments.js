(function () {
  "use strict";

  const panel = document.getElementById("attachment-panel");
  if (!panel) return;
  const configuredBase = document.documentElement && document.documentElement.dataset.appBasePath;
  const route = window.location.pathname.match(/^(.*)\/(mobile|pc)\/([^/]+)\/?$/);
  if (!route) return;
  const base = (configuredBase && configuredBase !== "__BASE_PATH__" ? configuredBase : route[1]).replace(/\/+$/, "");
  const role = route[2];
  let room;
  try { room = decodeURIComponent(route[3]); } catch (_) { return; }
  const get = id => document.getElementById(id);
  const details = get("attachment-details");
  const selection = get("attachment-selection");
  const status = get("attachment-status");
  const receiverStatus = get("attachment-receiver-status");
  const uploadButton = get("attachment-upload-button");
  const pasteButton = get("attachment-paste-button");
  const list = get("attachment-list");
  const maxFileBytes = 20 * 1024 * 1024;
  const state = {
    active: true, page: 0, revision: 0, selected: null, items: [],
    limit: maxFileBytes, receiver: null, preparation: null, preparationFailure: null, preparing: false,
    prepareRetryAt: 0, uploading: false, polling: false, queued: null, action: null,
    controllers: new Set(), renderedSignature: null,
  };

  const uuid = () => {
    if (window.crypto.randomUUID) return window.crypto.randomUUID();
    const bytes = window.crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
    const hex = Array.from(bytes, value => value.toString(16).padStart(2, "0")).join("");
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  };
  const query = params => Object.entries({ room_id: room, ...params })
    .filter(([, value]) => value !== undefined && value !== null)
    .map(([key, value]) => `${key}=${encodeURIComponent(value)}`).join("&");
  const url = (path, params) => `${base}/api/attachments${path}${params ? `?${query(params)}` : ""}`;
  const size = bytes => bytes >= 1024 * 1024 ? `${(bytes / (1024 * 1024)).toFixed(1)} MiB` : `${Math.ceil(bytes / 1024)} KiB`;
  function message(text, kind = "waiting") { status.textContent = text; status.dataset.status = kind; }
  const errors = {
    attachment_too_large: "附件超过服务器的大小限制。", attachment_quota_reached: "服务器附件留档容量已满，请先删除不需要的附件。",
    attachment_id_conflict: "附件编号冲突，请刷新留档后重新选择。", invalid_attachment_name: "文件名不被服务器接受，请重命名后重试。",
    attachment_receiver_unavailable: "电脑附件接收器未就绪，请等待连接或更新接收器。", receiver_unavailable: "电脑连接已断开，请等待重连。",
    session_mismatch: "电脑连接已更新，请重新准备附件。", action_in_progress: "电脑仍有粘贴操作在进行，请等待结束。",
    preparation_mismatch: "电脑附件准备已变化，请重新点击准备。", preparation_not_ready: "电脑还未准备好这个附件。",
    attachment_not_found: "这个附件留档已不存在。", attachment_in_use: "附件正在使用，请等待本次操作结束再删除。",
    attachment_action_capacity_reached: "附件指令暂时已满，请稍后再试。", attachment_integrity_failed: "附件校验失败，请重新上传。",
    download_failed: "电脑下载附件失败。", hash_mismatch: "电脑校验附件失败。", cache_full: "电脑附件缓存空间不足。",
    file_too_large: "附件超过电脑接收器的大小限制。", unsupported_attachment: "电脑接收器暂不支持这个附件。",
    preparation_failed: "电脑准备附件失败。", expired: "电脑附件准备已过期。",
  };
  const definiteRejection = error => Boolean(error && error.rejected && error.status >= 400 && error.status < 500);
  const explain = error => errors[error && error.code || error] || "服务器拒绝了这次请求，请刷新后核对。";

  async function request(path, options = {}, params, timeout = 6000) {
    const controller = new AbortController();
    state.controllers.add(controller);
    const timer = window.setTimeout(() => controller.abort(), timeout);
    try {
      const response = await fetch(url(path, params), { cache: "no-store", ...options, signal: controller.signal });
      let payload;
      try { payload = await response.json(); } catch (_) { payload = null; }
      if (!response.ok || !payload || payload.ok === false) {
        const error = new Error("request_failed");
        error.status = response.status; error.code = payload && payload.error; error.rejected = Boolean(payload && payload.ok === false);
        throw error;
      }
      return payload;
    } finally {
      window.clearTimeout(timer);
      state.controllers.delete(controller);
    }
  }
  const post = (path, body) => request(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ room_id: room, ...body }) });

  function controls() {
    const selected = state.selected;
    uploadButton.disabled = !state.active || !selected || !selected.file || state.uploading;
    pasteButton.disabled = !state.active || !selected || !selected.meta || state.uploading || Boolean(state.queued || state.action);
    get("attachment-image-button").disabled = state.uploading;
    get("attachment-file-button").disabled = state.uploading;
    const bridge = window.doubaoAttachmentBridge;
    if (bridge && typeof bridge.setBusy === "function") bridge.setBusy(Boolean(state.queued || state.action));
    receiverStatus.textContent = !state.receiver || !state.receiver.ready || !state.receiver.session_id
      ? "电脑附件接收器未就绪 · 可先点击粘贴，等待最多 15 秒；旧版接收器需更新。"
      : !selected || !selected.meta
        ? "电脑已连接 · 选好附件并上传留档后，可准备粘贴。"
        : preparationReady()
        ? "附件已在电脑准备好 · 将光标放到支持图片或文件的目标应用。"
        : state.preparationFailure
          ? "电脑暂未准备好这个附件 · 留档保留，也可下载后手动使用。"
          : "电脑已连接 · 正在提前准备附件，不会改动剪贴板。";
  }

  function cancelQueue(text, kind = "waiting") {
    if (!state.queued) return;
    window.clearTimeout(state.queued.timer);
    state.queued = null;
    if (text) message(text, kind);
    controls();
  }
  function stopObservation() {
    if (state.action) window.clearTimeout(state.action.timer);
    state.action = null;
  }
  function replaceSelection(next, text) {
    cancelQueue();
    state.revision += 1;
    state.selected = next;
    state.preparation = null;
    state.preparationFailure = null;
    state.preparing = false;
    state.prepareRetryAt = 0;
    selection.textContent = text;
    controls();
    renderList();
  }

  function choose(file) {
    if (!file || !state.active) return;
    details.open = true;
    if (file.size < 0 || file.size > state.limit) {
      replaceSelection(null, "请选择符合大小限制的附件。");
      message(file.size > state.limit ? `附件过大，每个附件最多 ${size(state.limit)}。` : "附件大小无效，请重新选择文件。", "error");
      return;
    }
    replaceSelection({ file, meta: null }, `${file.name || "未命名附件"} · ${size(file.size)} · 尚未上传`);
    message("已选择附件。点击“上传并留档”后，文件本体将发送到服务器；不会自动粘贴。");
  }

  async function upload() {
    const selected = state.selected;
    if (!state.active || state.uploading || !selected || !selected.file) return;
    const page = state.page;
    const revision = state.revision;
    const id = uuid();
    state.uploading = true;
    controls();
    message("正在上传并留档…");
    try {
      const payload = await request("/upload", {
        method: "POST", body: selected.file,
        headers: { "Content-Type": "application/octet-stream", "X-Attachment-Name": encodeURIComponent(selected.file.name || "附件"), "X-Attachment-Type": selected.file.type || "application/octet-stream" },
      }, { attachment_id: id }, 60000);
      if (!current(page, revision)) return;
      selected.file = null;
      selected.meta = payload.attachment;
      selection.textContent = `${selected.meta.filename} · ${size(selected.meta.size)} · 已留档`;
      state.items = [selected.meta, ...state.items.filter(item => item.attachment_id !== selected.meta.attachment_id)].slice(0, 50);
      renderList();
      message("附件已留档。可下载，或点击“粘贴附件到电脑”。", "saved");
    } catch (error) {
      if (current(page, revision)) message(definiteRejection(error)
        ? `上传未完成：${explain(error)} 文件仍保留在手机，未发送粘贴指令。`
        : "上传结果暂未确认，请刷新附件留档核对；不会自动粘贴。", "error");
    } finally {
      if (page === state.page) { state.uploading = false; controls(); void poll(); }
    }
  }

  function current(page, revision) { return state.active && page === state.page && revision === state.revision; }
  function preparationReady() {
    const prep = state.preparation;
    return Boolean(prep && prep.ready && prep.expiresAt > Date.now() && state.selected && state.selected.meta &&
      prep.attachment && prep.attachment.attachment_id === state.selected.meta.attachment_id &&
      state.receiver && prep.session === state.receiver.session_id);
  }
  function applyPreparation(remote) {
    const prep = state.preparation;
    if (!prep || !remote || remote.preparation_id !== prep.preparation_id) return;
    state.preparation = { ...remote, session: prep.session, expiresAt: Date.now() + Math.max(0, Number(remote.remaining_ms) || 0) };
    if (remote.error && !prep.error) preparationFailed(remote.error);
  }
  function preparationFailed(code) {
    state.preparationFailure = code;
    const text = `${explain(code)} 未发送粘贴指令，附件留档保留；可下载后手动使用，或再次点击粘贴重试准备。`;
    if (state.queued) cancelQueue(text, "error");
    else if (!state.action) message(text, "error");
    controls();
  }
  async function prepare() {
    const selected = state.selected;
    if (role !== "mobile" || !state.active || state.preparing || !selected || !selected.meta ||
        !state.receiver || !state.receiver.ready || !state.receiver.session_id || state.action || state.preparationFailure || Date.now() < state.prepareRetryAt) return;
    if (state.preparation && (state.preparation.error || state.preparation.expiresAt > Date.now())) return;
    const page = state.page;
    const revision = state.revision;
    const session = state.receiver.session_id;
    const id = uuid();
    state.preparing = true;
    state.preparation = { preparation_id: id, attachment: selected.meta, ready: false, session, expiresAt: Date.now() + 15000 };
    try {
      const payload = await post("/prepare", { session_id: session, preparation_id: id, attachment_id: selected.meta.attachment_id });
      if (!current(page, revision) || !state.preparation || state.preparation.preparation_id !== id) return;
      applyPreparation(payload.preparation || payload);
      controls();
      advance();
    } catch (error) {
      if (current(page, revision) && state.preparation && state.preparation.preparation_id === id) {
        if (definiteRejection(error)) {
          state.preparation.error = error.code || "preparation_failed";
          preparationFailed(state.preparation.error);
        } else {
          state.preparation = null;
          state.prepareRetryAt = Date.now() + 3000;
        }
      }
    } finally {
      if (current(page, revision)) state.preparing = false;
    }
  }

  function beginPaste() {
    if (!state.active || state.queued || state.action || !state.selected || !state.selected.meta) return;
    const bridge = window.doubaoAttachmentBridge;
    if (bridge && typeof bridge.isTextBusy === "function" && bridge.isTextBusy()) {
      message("文字粘贴仍在进行，请等待这次文字操作结束，再粘贴附件。");
      return;
    }
    if (state.preparationFailure || state.preparation && state.preparation.error) { state.preparation = null; state.preparationFailure = null; state.prepareRetryAt = 0; }
    const intent = { revision: state.revision, page: state.page, deadline: Date.now() + 15000, timer: null,
      session: state.receiver && state.receiver.ready ? state.receiver.session_id : null };
    state.queued = intent;
    intent.timer = window.setTimeout(() => {
      if (state.queued === intent) cancelQueue("等待电脑准备已超时，未发送粘贴指令；附件留档保留，可下载后手动粘贴。");
    }, 15000);
    message("已记住这次点击，等待电脑准备，最多 15 秒。修改文字、换附件或离开页面会取消等待。");
    controls();
    advance();
    void poll();
  }

  function advance() {
    const intent = state.queued;
    if (!intent || !current(intent.page, intent.revision)) return;
    if (Date.now() >= intent.deadline) { cancelQueue("等待电脑准备已超时，未发送粘贴指令；附件留档保留。"); return; }
    const session = state.receiver && state.receiver.ready && state.receiver.session_id;
    if (session) {
      if (intent.session && intent.session !== session) {
        cancelQueue("电脑接收器连接已更新，已取消等待中的附件粘贴；留档保留，请重新点击。", "error");
        return;
      }
      if (!intent.session) intent.session = session;
    }
    if (!preparationReady()) { void prepare(); return; }
    const id = uuid();
    const prep = state.preparation;
    window.clearTimeout(intent.timer);
    state.queued = null; // Consume intent before HTTP: uncertain responses must never resend it.
    state.action = { id, page: state.page, revision: state.revision, timer: null };
    const action = state.action;
    action.timer = window.setTimeout(() => {
      if (state.action !== action) return;
      stopObservation();
      if (action.revision === state.revision) message("暂未收到粘贴回执，附件留档保留；不会自动重发。请先检查电脑，避免重复粘贴。", "error");
      controls();
    }, 15000);
    controls();
    message("已发送一次粘贴指令，等待电脑回执…");
    void sendAction(action, prep);
  }

  async function sendAction(action, prep) {
    try {
      const payload = await post("/action", { session_id: prep.session, preparation_id: prep.preparation_id, action_id: action.id });
      if (state.action !== action || !state.active || action.page !== state.page) return;
      showAction(payload.action || payload);
    } catch (error) {
      if (state.action === action && state.active && action.page === state.page) {
        if (definiteRejection(error)) {
          stopObservation();
          state.preparation = null;
          if (action.revision === state.revision) message(`服务器未接受这次附件粘贴：${explain(error)} 留档保留，不会自动重发。`, "error");
          controls();
        } else if (action.revision === state.revision) message("指令结果暂未确认，正在查询回执；不会重新发送。请先检查电脑。", "error");
      }
    }
    if (state.action === action) void poll();
  }

  function showAction(remote) {
    const action = state.action;
    if (!action || !remote || remote.action_id !== action.id) return;
    if (action.revision !== state.revision) {
      if (["executed", "expired", "cancelled", "failed"].includes(remote.status)) { stopObservation(); controls(); }
      return;
    }
    if (remote.status === "executed") {
      stopObservation();
      message("已调用粘贴，请在电脑确认附件。目标应用需支持图片或文件；附件留档和文字草稿均已保留。", "saved");
    } else if (["expired", "cancelled", "failed"].includes(remote.status)) {
      stopObservation();
      message("附件粘贴未确认，留档已保留，不会自动重发。请先检查电脑；目标应用需支持图片或文件，也可下载后手动使用。", "error");
    } else {
      message(remote.status === "delivered" ? "电脑已收到指令，等待粘贴回执…" : "指令已受理，等待电脑…");
    }
    controls();
  }

  async function poll() {
    if (!state.active || state.polling) return;
    const page = state.page;
    const actionId = state.action && state.action.id;
    state.polling = true;
    try {
      const payload = await request("", {}, { action_id: actionId });
      if (!state.active || page !== state.page) return;
      state.items = Array.isArray(payload.items) ? payload.items.slice(0, 50) : [];
      if (payload.limits && Number(payload.limits.max_file_bytes) > 0) state.limit = Math.min(maxFileBytes, Number(payload.limits.max_file_bytes));
      get("attachment-limit").textContent = `每次一个附件，最大 ${size(state.limit)}${payload.limits && payload.limits.max_total_bytes ? ` · 留档容量 ${size(payload.limits.max_total_bytes)}` : ""}`;
      if (!state.receiver || !payload.receiver || state.receiver.session_id !== payload.receiver.session_id) state.preparation = null;
      state.receiver = payload.receiver || null;
      applyPreparation(payload.preparation);
      renderList();
      if (state.action && state.action.id === actionId) showAction(payload.action);
      if (status.textContent === "正在读取附件留档…" || status.textContent === "附件服务暂不可用，请稍后刷新；已有文字功能可继续使用。") message("图片和文件单独留档，不会进入文字输入框。");
      controls();
      advance();
      if (!state.queued) void prepare();
    } catch (_) {
      if (state.active && page === state.page) {
        state.receiver = null;
        state.preparation = null;
        controls();
        if (!state.queued && !state.action && !state.uploading) message("附件服务暂不可用，请稍后刷新；已有文字功能可继续使用。", "error");
      }
    } finally {
      if (page === state.page) state.polling = false;
    }
  }

  function renderList() {
    const signature = JSON.stringify([state.selected && state.selected.meta && state.selected.meta.attachment_id, state.items.map(item => [item.attachment_id, item.filename, item.size, item.created_at, item.media_type])]);
    if (signature === state.renderedSignature) return;
    state.renderedSignature = signature;
    list.replaceChildren();
    get("attachment-count").textContent = state.items.length ? `最近 ${state.items.length} 个附件` : "附件留档";
    if (!state.items.length) {
      const empty = document.createElement("p"); empty.className = "attachment-empty"; empty.textContent = "这个 room 还没有附件留档。"; list.appendChild(empty); return;
    }
    state.items.forEach(meta => {
      const row = document.createElement("article");
      row.className = "attachment-item" + (state.selected && state.selected.meta && state.selected.meta.attachment_id === meta.attachment_id ? " is-selected" : "");
      const icon = document.createElement("span"); icon.className = "attachment-icon"; icon.textContent = String(meta.media_type || "").startsWith("image/") ? "图" : "FILE"; icon.setAttribute("aria-hidden", "true");
      const title = document.createElement("p"); title.className = "attachment-name"; title.textContent = meta.filename;
      const info = document.createElement("div");
      const detail = document.createElement("p"); detail.className = "attachment-meta";
      const time = new Date(meta.created_at); detail.textContent = `${size(Number(meta.size) || 0)} · ${Number.isNaN(time.getTime()) ? "已留档" : time.toLocaleString()}`;
      info.appendChild(title); info.appendChild(detail);
      const actions = document.createElement("div"); actions.className = "attachment-item-actions";
      const download = document.createElement("a"); download.textContent = "下载附件"; download.href = url("/blob", { attachment_id: meta.attachment_id }); download.setAttribute("download", meta.filename);
      download.addEventListener("click", () => { if (!state.queued && !state.action) message("已交由浏览器下载，请查看浏览器的下载进度。文字草稿已保留。"); }); actions.appendChild(download);
      if (role === "mobile") {
        const select = document.createElement("button"); select.type = "button"; select.textContent = "选择以粘贴";
        select.addEventListener("click", () => {
          replaceSelection({ file: null, meta }, `${meta.filename} · ${size(meta.size)} · 已留档`);
          message("已选择留档附件，点击“粘贴附件到电脑”才会发出指令。"); void prepare();
        }); actions.appendChild(select);
      }
      const remove = document.createElement("button"); remove.type = "button"; remove.className = "attachment-delete"; remove.textContent = "删除留档";
      remove.addEventListener("click", () => { void deleteAttachment(meta, remove); }); actions.appendChild(remove);
      row.appendChild(icon); row.appendChild(info); row.appendChild(actions); list.appendChild(row);
    });
  }

  async function deleteAttachment(meta, button) {
    const page = state.page;
    if (state.selected && state.selected.meta && state.selected.meta.attachment_id === meta.attachment_id) cancelQueue("已取消等待中的附件粘贴。");
    button.disabled = true;
    try {
      await post("/delete", { attachment_id: meta.attachment_id });
      if (!state.active || page !== state.page) return;
      if (state.selected && state.selected.meta && state.selected.meta.attachment_id === meta.attachment_id) replaceSelection(null, "附件已删除，可选择新的图片或文件。");
      state.items = state.items.filter(item => item.attachment_id !== meta.attachment_id);
      renderList(); message("已删除附件留档。");
    } catch (_) {
      if (state.active && page === state.page) message("删除未确认，请刷新留档核对；正在使用的附件可能暂时无法删除。", "error");
      button.disabled = false;
    }
  }

  panel.hidden = false;
  get("attachment-mobile-controls").hidden = role !== "mobile";
  get("attachment-refresh-button").addEventListener("click", () => { if (!state.queued && !state.action && !state.uploading) message("正在读取附件留档…"); void poll(); });
  details.addEventListener("toggle", () => { if (details.open) void poll(); });
  if (role === "mobile") {
    [["attachment-image-button", "attachment-image-input"], ["attachment-file-button", "attachment-file-input"]].forEach(([button, input]) => {
      get(button).addEventListener("click", () => get(input).click());
      get(input).addEventListener("change", () => { const file = get(input).files && get(input).files[0]; if (file) choose(file); get(input).value = ""; });
    });
    uploadButton.addEventListener("click", () => { void upload(); });
    pasteButton.addEventListener("click", beginPaste);
    const clear = get("clear-button");
    if (clear) clear.addEventListener("click", () => cancelQueue("输入框已清空，已取消等待中的附件粘贴；附件留档保留。"));
    const editor = get("mobile-editor");
    if (editor) {
      editor.addEventListener("input", () => cancelQueue("文字已修改，已取消等待中的附件粘贴；附件留档保留。"));
      editor.addEventListener("paste", event => {
        const data = event.clipboardData;
        const files = data && Array.from(data.files || []);
        const file = files && files[0] || data && Array.from(data.items || []).filter(item => item.kind === "file").map(item => item.getAsFile()).find(Boolean);
        if (file) { event.preventDefault(); choose(file); }
      });
      editor.addEventListener("dragover", event => {
        const data = event.dataTransfer;
        if (data && (Array.from(data.files || []).length || Array.from(data.items || []).some(item => item.kind === "file"))) event.preventDefault();
      });
      editor.addEventListener("drop", event => {
        const file = event.dataTransfer && event.dataTransfer.files && event.dataTransfer.files[0];
        if (file) { event.preventDefault(); choose(file); }
      });
    }
  }
  function suspend() {
    state.active = false;
    state.page += 1;
    state.polling = false;
    state.preparing = false;
    state.uploading = false;
    cancelQueue("已离开页面，等待中的附件粘贴已取消；请重新点击。 ");
    if (state.action) message("页面已离开，执行回执未确认；请检查电脑，不会自动重发。", "error");
    stopObservation();
    state.preparation = null;
    state.controllers.forEach(controller => controller.abort());
    controls();
  }
  window.addEventListener("pagehide", suspend);
  window.addEventListener("pageshow", () => { state.active = true; controls(); void poll(); });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") cancelQueue("页面已进入后台，等待中的附件粘贴已取消；请重新点击。");
    else if (state.active) void poll();
  });
  window.addEventListener("online", () => { if (state.active) void poll(); });
  window.setInterval(() => { if (state.active && document.visibilityState !== "hidden") void poll(); }, 2000);
  controls();
  void poll();
})();
