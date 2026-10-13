(function () {
  "use strict";
  const route = window.location.pathname.match(/^(.*)\/signal\/([^/]+)\/?$/);
  const basePath = (window.__APP_BASE_PATH__ || (route && route[1]) || "").replace(/\/+$/, "");
  const roomId = route ? decodeURIComponent(route[2]) : "doubao";
  const draftKey = `doubao-input-sync:signal-draft:${roomId}`;
  const outboxPrefix = `doubao-input-sync:signal-outbox:${roomId}:`;
  const draft = document.getElementById("draft");
  const primary = document.getElementById("copy-paste");
  const secondary = document.getElementById("copy-only");
  const actionStatus = document.getElementById("action-status");
  const archiveStatus = document.getElementById("archive-status");
  const receiverStatus = document.getElementById("receiver-status");
  const state = {
    ready: false, sessionId: null, copying: false, polling: false,
    actionId: null, actionPending: false, actionTimer: null,
    currentCopyId: null, archiveSaved: false, flushing: false, outbox: new Map(),
  };

  function readStorage(key) {
    try { return window.localStorage.getItem(key); } catch (_) { return null; }
  }

  function saveItem(item) {
    try {
      window.localStorage.setItem(outboxPrefix + item.copy_id, JSON.stringify(item));
      item.durable = true;
    } catch (_) { item.durable = false; }
  }

  function renderControls() {
    const empty = !draft.value.trim();
    primary.disabled = empty || state.copying || state.actionPending || !state.ready;
    secondary.disabled = empty || state.copying || state.actionPending;
    document.getElementById("character-count").textContent = `${Array.from(draft.value).length} 字`;
  }

  function renderArchive() {
    const pending = state.outbox.size;
    if (Array.from(state.outbox.values()).some(item => !item.durable)) {
      archiveStatus.textContent = "手机储存不可用；留档成功前请保持本页打开，草稿已保留。";
    } else if (Array.from(state.outbox.values()).some(value => value.rejected)) {
      archiveStatus.textContent = "有留档被服务器拒绝，原文保留在本机；请检查登录或稍后重新复制。";
    } else if (pending) {
      archiveStatus.textContent = `${pending} 条待留档 · 联网后自动补存`;
    } else if (state.currentCopyId || state.archiveSaved) {
      archiveStatus.textContent = "已留档";
    }
  }

  async function request(path, options = {}, timeoutMs = 6000) {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(`${basePath}${path}`, { ...options, signal: controller.signal, cache: "no-store" });
      const payload = await response.json();
      return { response, payload };
    } finally { window.clearTimeout(timer); }
  }

  function uuid() {
    if (window.crypto.randomUUID) return window.crypto.randomUUID();
    const bytes = window.crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 15) | 64;
    bytes[8] = (bytes[8] & 63) | 128;
    const hex = Array.from(bytes, b => b.toString(16).padStart(2, "0")).join("");
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }

  function copyFromGesture(text) {
    if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
      return navigator.clipboard.writeText(text);
    }
    // Legacy copy stays inside the original gesture, without a network round trip.
    const start = draft.selectionStart;
    const end = draft.selectionEnd;
    draft.focus();
    draft.select();
    const copied = document.execCommand("copy");
    draft.setSelectionRange(start, end);
    if (!copied) throw new Error("copy_failed");
    return Promise.resolve();
  }

  function finishAction(id, message) {
    if (state.actionId !== id) return;
    state.actionPending = false;
    window.clearTimeout(state.actionTimer);
    actionStatus.textContent = message;
    renderControls();
  }

  const cancellationReasons = {
    focus_changed: "电脑焦点变了，请放回目标输入框后再试。",
    no_focused_input: "电脑没有可用的输入框，请先放好光标。",
    unsafe_target: "本次试用支持「文本编辑」和「备忘录」，请先点进正文输入处。",
    permission_missing: "电脑粘贴权限未就绪。",
    modifier_held: "电脑有快捷键仍按着，松开后再试。",
    receiver_stopped: "电脑接收器已停止。",
    receiver_expired: "电脑连接已过期。",
    receiver_unregistered: "电脑接收器已断开。",
    superseded: "电脑接收器已更新，请重新确认目标输入框。",
    paste_failed: "电脑未能执行粘贴。",
    expired: "指令已过期，不会再执行。",
  };

  function showAction(action) {
    if (!action || action.action_id !== state.actionId || !state.actionPending) return;
    if (action.status === "executed") {
      finishAction(action.action_id, "粘贴指令已执行 · 请核对电脑里的文字");
    } else if (action.status === "expired") {
      finishAction(action.action_id, "指令已过期，执行结果未确认 · 请先检查电脑，避免重复粘贴");
    } else if (action.status === "cancelled") {
      finishAction(action.action_id, `指令已取消 · ${cancellationReasons[action.reason] || "请检查电脑后再试。"}`);
    } else {
      actionStatus.textContent = action.status === "delivered" ? "电脑已收到指令，等待执行…" : "指令已受理，等待电脑…";
    }
  }

  async function pollStatus() {
    if (state.polling) return;
    state.polling = true;
    const actionId = state.actionId;
    try {
      const query = `?room=${encodeURIComponent(roomId)}${actionId ? `&action_id=${encodeURIComponent(actionId)}` : ""}`;
      const { response, payload } = await request(`/api/signal/status${query}`);
      if (!response.ok || payload.ok === false) throw new Error("status_failed");
      state.ready = Boolean(payload.ready && payload.session_id);
      state.sessionId = state.ready ? payload.session_id : null;
      receiverStatus.textContent = state.ready ? "电脑已连接 · 请确认目标输入框" : "电脑接收器未就绪 · 仍可仅复制并留档";
      receiverStatus.dataset.tone = state.ready ? "ready" : "waiting";
      if (state.actionId === actionId) showAction(payload.action);
    } catch (_) {
      state.ready = false;
      state.sessionId = null;
      receiverStatus.textContent = "连接暂不可用 · 仍可仅复制并留档";
      receiverStatus.dataset.tone = "waiting";
    } finally {
      state.polling = false;
      renderControls();
    }
  }

  async function sendAction(id, sessionId) {
    try {
      const { response, payload } = await request("/api/signal/action", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ room_id: roomId, session_id: sessionId, action_id: id }),
      });
      if (state.actionId !== id || !state.actionPending) return;
      if (!response.ok || payload.ok === false) {
        if (response.status >= 500 || [408, 425, 429].includes(response.status)) throw new Error("action_uncertain");
        finishAction(id, "粘贴指令未被接受 · 复制和留档仍保留，请检查电脑连接");
        void pollStatus();
        return;
      }
      if (payload.action) showAction(payload.action);
      else actionStatus.textContent = "指令已受理，等待电脑…";
      void pollStatus();
    } catch (_) {
      if (state.actionId === id && state.actionPending) {
        actionStatus.textContent = "指令结果暂不明确 · 正在查询，不会重新发送";
      }
    }
  }

  async function flushArchive() {
    if (state.flushing) return;
    state.flushing = true;
    try {
      for (const item of Array.from(state.outbox.values())) {
        if (item.rejected) continue;
        try {
          const { response, payload } = await request("/api/copy-archive", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ room_id: item.room_id, copy_id: item.copy_id, text: item.text, copied_at: item.copied_at }),
          });
          if (!response.ok || payload.ok === false) {
            if (response.status >= 400 && response.status < 500 && ![408, 425, 429].includes(response.status)) {
              item.rejected = true;
              saveItem(item);
              continue;
            }
            throw new Error("archive_failed");
          }
          state.outbox.delete(item.copy_id);
          state.archiveSaved = true;
          try { window.localStorage.removeItem(outboxPrefix + item.copy_id); } catch (_) { /* A later duplicate archive is idempotent. */ }
        } catch (_) { break; }
      }
    } finally {
      state.flushing = false;
      renderArchive();
    }
  }

  function copy(withAction) {
    if (state.copying || state.actionPending || !draft.value.trim() || (withAction && !state.ready)) return;
    const text = draft.value;
    const sessionId = state.sessionId;
    state.copying = true;
    renderControls();
    let operation;
    try {
      // Clipboard write is invoked synchronously from the button gesture, before fetch or await.
      operation = copyFromGesture(text);
    } catch (_) { operation = Promise.reject(new Error("copy_failed")); }
    Promise.resolve(operation).then(() => {
      const id = uuid();
      const item = { room_id: roomId, copy_id: id, text, copied_at: new Date().toISOString() };
      state.currentCopyId = id;
      state.outbox.set(id, item);
      saveItem(item);
      renderArchive();
      if (withAction) {
        state.actionId = id;
        state.actionPending = true;
        actionStatus.textContent = "已复制 · 正在发送一次粘贴指令…";
        // Server action TTL is 5 s. This is a UI observation limit, never a resend timer.
        state.actionTimer = window.setTimeout(() => {
          finishAction(id, "指令结果未知，不会重发 · 请先检查电脑，再决定是否重新复制");
        }, 15000);
        void sendAction(id, sessionId);
      } else if (!state.actionPending) {
        actionStatus.textContent = "已复制到手机 · 本次未发送粘贴指令";
      }
      // Archival and the action are independent. Only archive items enter persistent storage.
      void flushArchive();
    }).catch(() => {
      actionStatus.textContent = "复制未完成 · 没有发送粘贴指令，也没有新增留档，请重新点击";
    }).finally(() => {
      state.copying = false;
      renderControls();
    });
  }

  draft.value = readStorage(draftKey) || "";
  try {
    for (let i = 0; i < window.localStorage.length; i++) {
      const key = window.localStorage.key(i);
      if (!key || !key.startsWith(outboxPrefix)) continue;
      try {
        const item = JSON.parse(readStorage(key));
        if (item && item.room_id === roomId && typeof item.copy_id === "string" && typeof item.text === "string" && typeof item.copied_at === "string") {
          item.durable = true;
          state.outbox.set(item.copy_id, item);
        }
      } catch (_) { /* Leave malformed storage untouched. */ }
    }
  } catch (_) { /* Copy remains usable when storage is unavailable. */ }
  draft.addEventListener("input", () => {
    try {
      window.localStorage.setItem(draftKey, draft.value);
      document.getElementById("draft-hint").textContent = "草稿保存在这台手机，复制后继续保留。";
    } catch (_) {
      document.getElementById("draft-hint").textContent = "手机储存不可用，离开页面前请先复制文字。";
    }
    renderControls();
  });
  primary.addEventListener("click", () => copy(true));
  secondary.addEventListener("click", () => copy(false));
  window.addEventListener("online", () => { void pollStatus(); void flushArchive(); });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") { void pollStatus(); void flushArchive(); }
  });
  window.setInterval(() => { void pollStatus(); }, 2000);
  window.setInterval(() => { void flushArchive(); }, 5000);
  renderControls();
  renderArchive();
  void pollStatus();
  void flushArchive();
}());
