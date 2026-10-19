(function () {
  const state = {
    roomId: "doubao",
    role: "landing",
    draftSaveTimer: null,
    retryTimer: null,
    lastVersion: 0,
    lastArchiveId: 0,
    eventSource: null,
    flashTimer: null,
    reconnectTimer: null,
    pollTimer: null,
    mobileHydrated: false,
    pendingText: null,
    clientId: null,
    claimedOk: false,
    capturePulseTimer: null,
    claimHeartbeatTimer: null,
    lastAutoClearedArchiveId: 0,
    desktopAckCheckTimer: null,
    desktopAckCheckAttempts: 0,
    captureMode: "clipboard",
    modeChangePending: false,
    manualSendPending: false,
    manualFallbackDraftProtected: false,
    draftRequests: new Set(),
    desiredCaptureMode: null,
    syncGeneration: 0,
    lastSettingsRevision: 0,
    claimConflict: false,
    draftRevision: 0,
    localDraftRestored: false,
    copyPending: false,
    currentCopyId: null,
    copyOutbox: [],
    copyHistory: [],
    copyRetryTimer: null,
    copyFlushPending: false,
    copyRetryAttempts: 0,
    outboxStorageError: false,
    copyHistoryGeneration: 0,
    copyHistoryLoaded: false,
    copyHistoryError: false,
    pcRelayArchive: null,
    signalReady: false,
    signalSessionId: null,
    signalPolling: false,
    signalProtocol: null,
    signalPreparation: null,
    signalPrepareTimer: null,
    signalPreparePending: false,
    signalPrepareGeneration: 0,
    signalQueuedPaste: null,
    signalQueueTimer: null,
    signalActionId: null,
    signalActionPending: false,
    signalActionLate: false,
    signalActionDraft: null,
    signalActionTimer: null,
    signalPageActive: true,
    signalPageGeneration: 0,
    serverEpoch: null,
    retiredServerEpochs: new Set(),
  };
  // The page must also work when a gateway blocks inline scripts. New HTML
  // carries this as data; the pathname fallback supports older cached HTML.
  const htmlBasePath = document.documentElement && document.documentElement.getAttribute("data-app-base-path");
  const routedPath = window.location.pathname.match(/^(.*)\/(?:mobile|pc)\/[^/]+\/?$/);
  const basePath = (htmlBasePath || window.__APP_BASE_PATH__ || (routedPath && routedPath[1]) || "").replace(/\/+$/, "");

  const roomInput = document.getElementById("room-input");
  const reconnectButton = document.getElementById("reconnect-button");
  const archiveIdleInput = document.getElementById("archive-idle-input");
  const saveSettingsButton = document.getElementById("save-settings-button");
  const settingsStatus = document.getElementById("settings-status");
  const mobilePanel = document.getElementById("mobile-panel");
  const pcPanel = document.getElementById("pc-panel");
  const mobileEditor = document.getElementById("mobile-editor");
  const composerShell = document.getElementById("composer-shell");
  const autoModeButton = document.getElementById("auto-mode-button");
  const manualModeButton = document.getElementById("manual-mode-button");
  const clipboardModeButton = document.getElementById("clipboard-mode-button");
  const manualSendButton = document.getElementById("manual-send-button");
  const captureModeHint = document.getElementById("capture-mode-hint");
  const mobilePanelHint = document.getElementById("mobile-panel-hint");
  const clearButton = document.getElementById("clear-button");
  const restoreLastButton = document.getElementById("restore-last-button");
  const autoClearToggle = document.getElementById("auto-clear-toggle");
  const autoClearLabel = document.getElementById("auto-clear-label") || document.createElement("span");
  const saveState = document.getElementById("save-state");
  const output = document.getElementById("pc-output");
  const copyButton = document.getElementById("copy-button");
  const refreshButton = document.getElementById("refresh-button");
  const connectionStatus = document.getElementById("connection-status");
  const versionBadge = document.getElementById("version-badge");
  const updatedAt = document.getElementById("updated-at");
  const sourceLine = document.getElementById("source-line");
  const title = document.getElementById("page-title");
  const subtitle = document.getElementById("page-subtitle");
  const claimWarning = document.getElementById("claim-warning");
  const historyList = document.getElementById("history-list");
  const syncFlash = document.getElementById("sync-flash");
  const archiveHint = document.getElementById("archive-hint");
  const themePicker = document.getElementById("theme-picker");
  const themeStatus = document.getElementById("theme-status");
  const clipboardActions = document.getElementById("clipboard-actions");
  const copyArchiveButton = document.getElementById("copy-archive-button");
  // Static assets deploy separately; detached fallbacks keep an older HTML
  // document usable during that brief transition (or an interrupted refresh).
  const clipboardButtons = document.getElementById("clipboard-buttons") || document.createElement("div");
  const copyPasteButton = document.getElementById("copy-paste-button") || document.createElement("button");
  const signalReceiverStatus = document.getElementById("signal-receiver-status") || document.createElement("p");
  const signalActionStatus = document.getElementById("signal-action-status") || document.createElement("p");
  const copyHistoryRefreshButton = document.getElementById("copy-history-refresh-button") || document.createElement("button");
  const pcPanelHint = document.getElementById("pc-panel-hint") || document.createElement("span");
  const copyStatus = document.getElementById("copy-status");
  const copyHistoryList = document.getElementById("copy-history-list");
  const copyQueueStatus = document.getElementById("copy-queue-status");
  const retryCopyArchiveButton = document.getElementById("retry-copy-archive-button");
  const autoClearStoragePrefix = "doubao-input-sync:auto-clear:";
  const clientIdStoragePrefix = "doubao-input-sync:client-id:";
  const recoveryStoragePrefix = "doubao-input-sync:last-auto-cleared:";
  const clipboardDraftStoragePrefix = "doubao-input-sync:clipboard-draft:";
  const copyOutboxStoragePrefix = "doubao-input-sync:copy-outbox:";
  const claimHeartbeatIntervalMs = 15000;
  const themeOptions = ["warm", "green", "blue", "rose", "slate"];

  function appPath(path) {
    return `${basePath}${path}`;
  }

  async function fetchJsonWithTimeout(url, options = {}, timeoutMs = 10000) {
    const controller = new AbortController();
    const timeout = window.setTimeout(function () { controller.abort(); }, timeoutMs);
    try {
      const response = await fetch(url, { ...options, signal: controller.signal });
      const payload = await response.json();
      return { response, payload };
    } finally {
      window.clearTimeout(timeout);
    }
  }

  function readLocalStorage(key) {
    try {
      return window.localStorage.getItem(key);
    } catch (error) {
      return null;
    }
  }

  function writeLocalStorage(key, value) {
    try {
      window.localStorage.setItem(key, value);
      return true;
    } catch (error) {
      return false;
    }
  }

  function persistClipboardDraft() {
    if (state.role !== "mobile") {
      return;
    }
    if (!writeLocalStorage(`${clipboardDraftStoragePrefix}${state.roomId}`, mobileEditor.value)) {
      saveState.textContent = "本机无法保存草稿；请保留页面，复制留档后再关闭";
      return false;
    }
    return true;
  }

  function loadClipboardStorage() {
    if (state.role !== "mobile") {
      return;
    }
    const draft = readLocalStorage(`${clipboardDraftStoragePrefix}${state.roomId}`);
    if (draft !== null) {
      mobileEditor.value = draft;
      state.localDraftRestored = true;
      state.mobileHydrated = true;
      if (draft) {
        saveState.textContent = "已恢复本机草稿；点复制才会留档";
      }
    }
    reloadCopyOutbox();
    renderCopyHistory();
  }

  function copyItemStorageKey(copyId) {
    return `${copyOutboxStoragePrefix}${encodeURIComponent(state.roomId)}:${copyId}`;
  }

  function persistCopyItem(item) {
    // One key per copy avoids one tab overwriting another tab's offline queue.
    return writeLocalStorage(copyItemStorageKey(item.copy_id), JSON.stringify({
      room_id: item.room_id, copy_id: item.copy_id, text: item.text, copied_at: item.copied_at,
      terminal: Boolean(item.terminal), archiveError: item.archiveError || "",
    }));
  }

  function reloadCopyOutbox() {
    const existing = new Map(state.copyOutbox.map(function (item) { return [item.copy_id, item]; }));
    const items = [];
    try {
      const prefix = `${copyOutboxStoragePrefix}${encodeURIComponent(state.roomId)}:`;
      for (let index = 0; index < window.localStorage.length; index += 1) {
        const key = window.localStorage.key(index);
        if (!key || !key.startsWith(prefix)) {
          continue;
        }
        try {
          const item = JSON.parse(window.localStorage.getItem(key));
          if (!item || item.room_id !== state.roomId || typeof item.copy_id !== "string" || typeof item.text !== "string" || !item.text.trim() || typeof item.copied_at !== "string") {
            throw new Error("invalid_copy_record");
          }
          const memory = existing.get(item.copy_id);
          items.push({ ...item, ...(memory ? {
            clearRevision: memory.clearRevision,
            clearGeneration: memory.clearGeneration,
            clearAfterArchive: memory.clearAfterArchive,
          } : {}) });
        } catch (error) {
          // Preserve malformed keys for recovery instead of overwriting them.
          state.outboxStorageError = true;
        }
      }
      const ids = new Set(items.map(function (item) { return item.copy_id; }));
      existing.forEach(function (item) {
        if (item.storageFailed && !ids.has(item.copy_id)) {
          items.push(item);
        }
      });
      state.copyOutbox = items;
    } catch (error) {
      state.outboxStorageError = true;
    }
  }

  function cancelDraftWork() {
    state.syncGeneration += 1;
    if (state.draftSaveTimer) {
      window.clearTimeout(state.draftSaveTimer);
      state.draftSaveTimer = null;
    }
    if (state.retryTimer) {
      window.clearTimeout(state.retryTimer);
      state.retryTimer = null;
    }
    state.pendingText = null;
    cancelScheduledDesktopAckCheck();
  }

  function setCopyStatus(message, status) {
    copyStatus.textContent = message;
    copyStatus.dataset.status = status || "";
  }

  function writeClipboardFromGesture(text) {
    // This call is deliberately synchronous up to writeText: no network or await
    // can consume Safari's user activation before the clipboard operation starts.
    if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
      try {
        return Promise.resolve(navigator.clipboard.writeText(text));
      } catch (error) {
        return Promise.reject(error);
      }
    }
    if (window.isSecureContext === false) {
      return Promise.reject(new Error("https_required"));
    }
    const previousFocus = document.activeElement;
    const selectionStart = mobileEditor.selectionStart;
    const selectionEnd = mobileEditor.selectionEnd;
    const helper = document.createElement("textarea");
    helper.value = text;
    helper.setAttribute("readonly", "");
    helper.style.position = "fixed";
    helper.style.top = "0";
    helper.style.left = "-9999px";
    document.body.appendChild(helper);
    let copied = false;
    try {
      helper.select();
      copied = Boolean(document.execCommand("copy"));
    } finally {
      helper.remove();
      if (previousFocus && typeof previousFocus.focus === "function") {
        previousFocus.focus({ preventScroll: true });
      }
      if (previousFocus === mobileEditor && typeof mobileEditor.setSelectionRange === "function") {
        mobileEditor.setSelectionRange(selectionStart, selectionEnd);
      }
    }
    return copied ? Promise.resolve() : Promise.reject(new Error("clipboard_unavailable"));
  }

  function copyFailureMessage() {
    return window.isSecureContext === false
      ? "复制失败：请通过 HTTPS 打开此页面。文字已保留，还没有留档。"
      : "复制失败：请允许浏览器访问剪贴板，或长按文字手动复制。文字已保留，还没有留档。";
  }

  function makeCopyId() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return window.crypto.randomUUID();
    }
    const bytes = new Uint8Array(16);
    if (window.crypto && typeof window.crypto.getRandomValues === "function") {
      window.crypto.getRandomValues(bytes);
    } else {
      for (let index = 0; index < bytes.length; index += 1) {
        bytes[index] = Math.floor(Math.random() * 256);
      }
    }
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, function (value) { return value.toString(16).padStart(2, "0"); }).join("");
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }

  function clipboardBusy() {
    return state.copyPending || state.signalActionPending || Boolean(state.signalQueuedPaste);
  }

  const matchProtocol = "clipboard-match-v1";

  function preparationCurrent(preparation = state.signalPreparation) {
    return preparation && preparation.sessionId === state.signalSessionId &&
      preparation.pageGeneration === state.signalPageGeneration &&
      preparation.revision === state.draftRevision && preparation.text === mobileEditor.value;
  }

  function preparationReady() {
    return state.signalReady && state.signalProtocol === matchProtocol &&
      preparationCurrent() && state.signalPreparation.ready && state.signalPreparation.expiresAt > Date.now();
  }

  function invalidatePreparation() {
    state.signalPrepareGeneration += 1;
    state.signalPreparation = null;
    window.clearTimeout(state.signalPrepareTimer);
    state.signalPrepareTimer = null;
  }

  function schedulePreparation() {
    if (state.role !== "mobile" || state.captureMode !== "clipboard" || !state.signalPageActive ||
        !state.signalReady || state.signalProtocol !== matchProtocol || state.copyPending || state.signalActionPending ||
        state.signalPreparePending || state.signalPrepareTimer || !mobileEditor.value.trim()) return;
    if (preparationCurrent() && state.signalPreparation.expiresAt - Date.now() >= 20000) return;
    state.signalPrepareTimer = window.setTimeout(function () {
      state.signalPrepareTimer = null;
      void prepareClipboardMatch();
    }, 240);
  }

  async function prepareClipboardMatch() {
    if (state.signalPreparePending || state.copyPending || state.signalActionPending || !state.signalPageActive || !state.signalReady ||
        state.signalProtocol !== matchProtocol || state.captureMode !== "clipboard" || !mobileEditor.value.trim()) return;
    const generation = state.signalPrepareGeneration;
    const text = mobileEditor.value;
    const revision = state.draftRevision;
    const sessionId = state.signalSessionId;
    const pageGeneration = state.signalPageGeneration;
    const current = () => generation === state.signalPrepareGeneration && text === mobileEditor.value &&
      revision === state.draftRevision && sessionId === state.signalSessionId && pageGeneration === state.signalPageGeneration && state.signalPageActive;
    state.signalPreparePending = true;
    try {
      if (!window.crypto || !window.crypto.subtle) throw new Error("hash_unavailable");
      const digest = await window.crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
      if (!current()) return;
      const sha256 = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, "0")).join("");
      const previous = preparationCurrent() ? state.signalPreparation : null;
      const preparation = { preparation_id: previous ? previous.preparation_id : makeCopyId(), sha256,
        text, revision, sessionId, pageGeneration, ready: false, expiresAt: 0 };
      const { response, payload } = await fetchJsonWithTimeout(appPath("/api/signal/prepare"), {
        method: "POST", headers: { "Content-Type": "application/json" }, cache: "no-store",
        body: JSON.stringify({ room_id: state.roomId, session_id: sessionId, preparation_id: preparation.preparation_id, sha256 }),
      }, 6000);
      if (!current()) return;
      if (!response.ok || payload.ok === false) throw new Error("prepare_failed");
      state.signalPreparation = preparation;
      applyPreparationStatus(payload.preparation);
      renderSignalReadiness();
      void pollSignalStatus();
    } catch (_) {
      if (current()) {
        state.signalPreparation = null;
        signalReceiverStatus.textContent = "电脑准备失败 · 草稿保留，可切换换气发送";
      }
    } finally {
      state.signalPreparePending = false;
      advanceQueuedPaste();
      updateManualSendAvailability();
      // Serialize passive requests: a slow older request must settle before a
      // newer draft is prepared. Network/status polling can retry preparation.
      if (!current()) schedulePreparation();
    }
  }

  function applyPreparationStatus(remote) {
    const local = state.signalPreparation;
    if (!preparationCurrent(local)) return;
    local.ready = Boolean(remote && remote.preparation_id === local.preparation_id && remote.sha256 === local.sha256 && remote.ready);
    local.expiresAt = remote && remote.preparation_id === local.preparation_id
      ? Date.now() + Math.max(0, Number(remote.remaining_ms) || 0) : 0;
  }

  function renderSignalReadiness() {
    signalReceiverStatus.textContent = !state.signalReady
      ? "电脑接收器未就绪 · 可先点复制并粘贴，自动等待就绪"
      : state.signalProtocol !== matchProtocol
        ? "电脑接收器需更新 · 可仅复制，或切换换气发送"
        : preparationReady()
          ? "电脑已提前等待 · 点复制后，文字匹配才粘贴"
          : mobileEditor.value.trim() ? "电脑已连接 · 可先点击，准备好后自动粘贴" : "电脑已连接 · 输入后会提前准备";
    signalReceiverStatus.dataset.status = preparationReady() ? "ready" : "waiting";
  }

  function finishSignalAction(id, message, status = "error") {
    if (state.signalActionId !== id || !state.signalActionPending) return;
    state.signalActionPending = false;
    state.signalActionLate = false;
    state.signalActionDraft = null;
    window.clearTimeout(state.signalActionTimer);
    state.signalActionTimer = null;
    signalActionStatus.textContent = message;
    signalActionStatus.dataset.status = status;
    schedulePreparation();
    updateManualSendAvailability();
  }

  const signalCancellationReasons = {
    focus_changed: "电脑焦点变了，请放回目标输入框。",
    no_focused_input: "请先把电脑光标放进目标普通输入框。",
    unsafe_target: "当前为受保护输入框，请将光标移到目标普通输入框。",
    permission_missing: "电脑粘贴权限未就绪。",
    modifier_held: "请松开电脑上按住的快捷键。",
    clipboard_timeout: "未等到豆包同步本次文字。",
    clipboard_mismatch: "电脑剪贴板内容不匹配。",
    mismatch: "电脑剪贴板内容不匹配。",
    clipboard_changed: "电脑剪贴板已变化。",
    receiver_stopped: "电脑接收器已停止。",
    receiver_expired: "电脑连接已过期。",
    receiver_unregistered: "电脑接收器已断开。",
    superseded: "电脑接收器已更新。",
  };

  function showSignalAction(action) {
    if (!action || action.action_id !== state.signalActionId) return;
    if (!state.signalActionPending) {
      // A readback after the observation timeout may update the message only.
      // The retained/re-edited draft is no longer eligible for automatic clear.
      if (state.signalActionLate && ["executed", "cancelled", "expired"].includes(action.status)) {
        state.signalActionLate = false;
        signalActionStatus.textContent = action.status === "executed"
          ? "已补收到执行回执 · 草稿已保留，请核对电脑，避免重复粘贴"
          : action.reason === "paste_unconfirmed"
            ? "电脑已调用粘贴，但未能确认文字写入 · 草稿已保留，请先检查电脑，避免重复粘贴"
            : "已补收到回执，粘贴结果未确认 · 草稿已保留，请先检查电脑再重试";
        signalActionStatus.dataset.status = action.status === "executed" ? "saved" : "error";
      }
      return;
    }
    if (action.status === "executed") {
      const draft = state.signalActionDraft;
      const canClear = draft && state.role === "mobile" && state.captureMode === "clipboard" &&
        !state.modeChangePending && draft.room === state.roomId &&
        draft.generation === state.syncGeneration && draft.revision === state.draftRevision &&
        mobileEditor.value === draft.text;
      if (canClear) {
        rememberRecoveryDraft(draft.text, 0);
        mobileEditor.value = "";
        state.draftRevision += 1;
        invalidatePreparation();
        persistClipboardDraft();
        saveState.textContent = "电脑已执行粘贴，已清空；可恢复刚才内容";
      }
      finishSignalAction(action.action_id, canClear
        ? "粘贴指令已执行 · 手机输入已清空，可恢复刚才内容"
        : "粘贴指令已执行 · 新输入已保留，请核对电脑里的文字", "saved");
    } else if (action.status === "expired" || (action.status === "cancelled" && action.reason === "expired")) {
      finishSignalAction(action.action_id, "指令已过期，执行结果未确认 · 草稿已保留；请先检查电脑，未粘贴可切换换气发送");
    } else if (action.status === "cancelled" && action.reason === "paste_unconfirmed") {
      finishSignalAction(action.action_id, "电脑已调用粘贴，但未能确认文字写入 · 草稿已保留，请先检查电脑，避免重复粘贴");
    } else if (action.status === "cancelled" && action.reason === "paste_failed") {
      finishSignalAction(action.action_id, "粘贴结果未确认 · 草稿已保留，不会自动重发；请先检查电脑，确认没有粘贴后再重试或切换换气发送");
    } else if (action.status === "cancelled") {
      finishSignalAction(action.action_id, `粘贴失败 · ${signalCancellationReasons[action.reason] || "指令已取消，请检查电脑。"} 草稿已保留，可切换换气发送。`);
    } else {
      signalActionStatus.textContent = action.status === "delivered" ? "电脑已收到指令，等待执行…" : "指令已受理，等待电脑…";
    }
  }

  async function pollSignalStatus() {
    if (state.role !== "mobile" || state.signalPolling || !state.signalPageActive) return;
    state.signalPolling = true;
    const actionId = state.signalActionId;
    const pageGeneration = state.signalPageGeneration;
    try {
      const query = `?room=${encodeURIComponent(state.roomId)}${actionId ? `&action_id=${encodeURIComponent(actionId)}` : ""}`;
      const { response, payload } = await fetchJsonWithTimeout(appPath(`/api/signal/status${query}`), { cache: "no-store" }, 6000);
      if (pageGeneration !== state.signalPageGeneration || !state.signalPageActive) return;
      if (!response.ok || payload.ok === false) throw new Error("signal_status_failed");
      const nextSession = payload.ready && payload.session_id ? payload.session_id : null;
      if (nextSession !== state.signalSessionId || payload.protocol !== state.signalProtocol) invalidatePreparation();
      state.signalProtocol = payload.protocol || null;
      state.signalReady = Boolean(payload.ready && payload.session_id);
      state.signalSessionId = state.signalReady ? payload.session_id : null;
      applyPreparationStatus(payload.preparation);
      renderSignalReadiness();
      schedulePreparation();
      advanceQueuedPaste();
      if (state.signalActionId === actionId) showSignalAction(payload.action);
    } catch (_) {
      if (pageGeneration !== state.signalPageGeneration) return;
      invalidatePreparation();
      state.signalReady = false;
      state.signalSessionId = null;
      signalReceiverStatus.textContent = "电脑连接暂不可用 · 仍可仅复制并留档";
      signalReceiverStatus.dataset.status = "waiting";
    } finally {
      state.signalPolling = false;
      updateManualSendAvailability();
    }
  }

  async function sendSignalAction(id, sessionId, preparationId) {
    // Only a successful gesture copy can queue this one-shot request. It is
    // never persisted, retried, or reconstructed from the archive outbox.
    try {
      const { response, payload } = await fetchJsonWithTimeout(appPath("/api/signal/action"), {
        method: "POST", headers: { "Content-Type": "application/json" }, cache: "no-store",
        body: JSON.stringify({ room_id: state.roomId, session_id: sessionId, action_id: id, preparation_id: preparationId }),
      }, 6000);
      if (state.signalActionId !== id || !state.signalActionPending) return;
      if (!response.ok || payload.ok === false) {
        if (response.status >= 500 || [408, 425, 429].includes(response.status)) throw new Error("signal_uncertain");
        finishSignalAction(id, "粘贴失败：指令未被接受 · 草稿已保留，可切换换气发送");
      } else if (payload.action) {
        showSignalAction(payload.action);
      } else {
        signalActionStatus.textContent = "指令已受理，等待电脑…";
      }
      void pollSignalStatus();
    } catch (_) {
      if (state.signalActionId === id && state.signalActionPending) {
        signalActionStatus.textContent = "粘贴结果暂不明确 · 正在查询，不会重新发送";
      }
    }
  }

  function cancelQueuedPaste(message) {
    if (!state.signalQueuedPaste) return;
    state.signalQueuedPaste = null;
    window.clearTimeout(state.signalQueueTimer);
    state.signalQueueTimer = null;
    signalActionStatus.textContent = message;
    signalActionStatus.dataset.status = "error";
    updateManualSendAvailability();
  }

  function advanceQueuedPaste() {
    const queued = state.signalQueuedPaste;
    if (!queued) return;
    if (Date.now() >= queued.deadline) {
      cancelQueuedPaste("等待电脑就绪超时 · 没有发送粘贴指令；草稿已保留，可切换换气发送");
      return;
    }
    if (!state.signalPageActive || queued.pageGeneration !== state.signalPageGeneration ||
        queued.room !== state.roomId || queued.generation !== state.syncGeneration ||
        queued.revision !== state.draftRevision || queued.text !== mobileEditor.value ||
        state.role !== "mobile" || state.captureMode !== "clipboard" || state.modeChangePending || state.claimConflict ||
        (queued.sessionId && state.signalSessionId && queued.sessionId !== state.signalSessionId)) {
      cancelQueuedPaste("文字或连接状态已变化 · 没有发送粘贴指令；草稿已保留，可切换换气发送");
      return;
    }
    if (!queued.sessionId && state.signalSessionId) queued.sessionId = state.signalSessionId;
    if (!preparationReady()) return;
    const preparation = state.signalPreparation;
    const sessionId = state.signalSessionId;
    // Consume the in-memory intent BEFORE starting any network request. Polls,
    // prepare responses and duplicate clicks cannot dispatch it a second time.
    state.signalQueuedPaste = null;
    window.clearTimeout(state.signalQueueTimer);
    state.signalQueueTimer = null;
    const actionId = makeCopyId();
    invalidatePreparation();
    state.signalActionId = actionId;
    state.signalActionPending = true;
    state.signalActionLate = false;
    state.signalActionDraft = queued;
    signalActionStatus.textContent = "电脑已就绪 · 正在发送一次粘贴指令…";
    signalActionStatus.dataset.status = "pending";
    state.signalActionTimer = window.setTimeout(function () {
      finishSignalAction(actionId, "粘贴结果未知，不会重发 · 草稿已保留；请先检查电脑，未粘贴可切换换气发送");
      state.signalActionLate = state.signalActionId === actionId;
    }, 15000);
    void sendSignalAction(actionId, sessionId, preparation.preparation_id);
    updateManualSendAvailability();
  }

  function copyAndArchive(withAction = false) {
    const textSnapshot = mobileEditor.value;
    if (state.captureMode !== "clipboard" || clipboardBusy() || state.modeChangePending || state.manualSendPending || state.claimConflict || !textSnapshot.trim() ||
        (withAction && (state.role !== "mobile" || !state.signalPageActive))) {
      return;
    }
    if (Array.from(textSnapshot).length > 200000 || new Blob([JSON.stringify({ text: textSnapshot })]).size > 1048000) {
      setCopyStatus("文字超过留档上限，请分成较短的段落再复制。草稿已保留。", "error");
      return;
    }
    // An older copy-only archive may still be in flight. Once the user asks
    // for desktop paste, its acknowledgement must not clear this draft either.
    if (withAction) state.syncGeneration += 1;
    const draftRevision = state.draftRevision;
    const copyGeneration = state.syncGeneration;
    const sessionId = state.signalSessionId;
    const pageGeneration = state.signalPageGeneration;
    const queueDeadline = Date.now() + 15000;
    if (!withAction) invalidatePreparation();
    const clipboardWrite = writeClipboardFromGesture(textSnapshot);
    state.copyPending = true;
    updateManualSendAvailability();
    setCopyStatus("正在复制到手机剪贴板…");
    clipboardWrite.then(function () {
      const item = {
        room_id: state.roomId,
        copy_id: makeCopyId(),
        text: textSnapshot,
        copied_at: new Date().toISOString(),
      };
      state.currentCopyId = item.copy_id;
      state.copyOutbox.push(item);
      // A durable archive does not prove desktop paste succeeded. Action copies
      // clear only on an executed action receipt, never on an archive receipt.
      // Auto-clear is session-only, so a retried record after reload cannot
      // erase a newly restored draft that merely happens to have the same text.
      item.clearRevision = draftRevision;
      item.clearGeneration = copyGeneration;
      item.clearAfterArchive = !withAction && autoClearToggle.checked;
      const durable = persistCopyItem(item);
      item.storageFailed = !durable;
      setCopyStatus(durable
        ? "已复制到手机剪贴板 · 留档中，可去电脑粘贴"
        : "已复制到手机剪贴板 · 本机补存记录保存失败，请保留页面直到留档成功", "pending");
      renderCopyHistory();
      if (withAction) {
        state.signalQueuedPaste = { text: textSnapshot, revision: draftRevision,
          generation: copyGeneration, room: item.room_id, sessionId, pageGeneration, deadline: queueDeadline };
        signalActionStatus.textContent = "已复制 · 等待电脑就绪，就绪后自动粘贴（最多 15 秒）…";
        signalActionStatus.dataset.status = "pending";
        state.signalQueueTimer = window.setTimeout(function () {
          cancelQueuedPaste("等待电脑就绪超时 · 没有发送粘贴指令；草稿已保留，可切换换气发送");
        }, Math.max(0, queueDeadline - Date.now()));
        advanceQueuedPaste();
      } else {
        signalActionStatus.textContent = "已复制到手机 · 本次未发送粘贴指令";
      }
      restartCopyArchiveRetry();
    }).catch(function () {
      setCopyStatus(copyFailureMessage(), "error");
      if (withAction) signalActionStatus.textContent = "复制失败 · 没有发送粘贴指令，也没有新增留档；草稿已保留，可切换换气发送";
    }).finally(function () {
      state.copyPending = false;
      advanceQueuedPaste();
      schedulePreparation();
      updateManualSendAvailability();
    });
  }

  function copyExistingRecord(item) {
    if (clipboardBusy()) return;
    invalidatePreparation();
    const clipboardWrite = writeClipboardFromGesture(item.text);
    state.copyPending = true;
    updateManualSendAvailability();
    clipboardWrite.then(function () {
      setCopyStatus("已重新复制到本机剪贴板，可去电脑粘贴", "copied");
      copyQueueStatus.textContent = "已重新复制到本机剪贴板，可去电脑粘贴。";
    }).catch(function () {
      const message = copyFailureMessage().replace("，还没有留档", "");
      setCopyStatus(message, "error");
      copyQueueStatus.textContent = message;
    }).finally(function () {
      state.copyPending = false;
      updateManualSendAvailability();
    });
  }

  function displayCopyTime(value) {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "时间未知" : date.toLocaleString("zh-CN", { hour12: false });
  }

  function renderPcOutput() {
    if (state.role !== "pc") return;
    // A mode switch does not remove durable copies. If this sending mode has
    // no relay archive yet, keep the saved copy available in the main PC box.
    if (state.captureMode !== "clipboard" && state.pcRelayArchive) {
      pcPanelHint.textContent = `房间 ${state.roomId} · 最新发送留档`;
      output.textContent = state.pcRelayArchive.text || "";
      copyButton.disabled = clipboardBusy() || !output.textContent;
      versionBadge.textContent = `version ${state.lastVersion}`;
      updatedAt.textContent = `发送时间：${displayCopyTime(state.pcRelayArchive.archived_at)}`;
      sourceLine.textContent = `当前房间：${state.roomId}`;
      return;
    }
    const latest = state.copyHistory.slice().sort(function (left, right) {
      return String(right.copied_at || right.archived_at || "").localeCompare(String(left.copied_at || left.archived_at || ""));
    })[0];
    pcPanelHint.textContent = state.captureMode === "clipboard"
      ? `房间 ${state.roomId} · 最新复制留档`
      : `房间 ${state.roomId} · 最新复制留档（尚无发送留档）`;
    output.textContent = latest ? latest.text
      : state.copyHistoryError ? "复制留档暂未读到，请点“刷新留档”重试。"
      : state.copyHistoryLoaded ? `房间 ${state.roomId} 还没有复制留档。请确认与手机页使用同一个房间。`
      : "正在读取复制留档…";
    copyButton.disabled = clipboardBusy() || !latest;
    if (latest) {
      versionBadge.textContent = `最近 ${state.copyHistory.length} 条复制留档`;
      updatedAt.textContent = `留档时间：${displayCopyTime(latest.archived_at || latest.copied_at)}`;
      sourceLine.textContent = `当前房间：${state.roomId}`;
    }
  }

  function renderCopyHistory() {
    const pendingIds = new Set(state.copyOutbox.map(function (item) { return item.copy_id; }));
    const combined = state.copyOutbox.map(function (item) { return { ...item, pending: true }; })
      .concat(state.copyHistory.filter(function (item) { return !pendingIds.has(item.copy_id); }));
    combined.sort(function (left, right) { return String(right.copied_at || right.archived_at || "").localeCompare(String(left.copied_at || left.archived_at || "")); });
    copyHistoryList.innerHTML = "";
    combined.slice(0, 50).forEach(function (item) {
      const wrapper = document.createElement("article");
      wrapper.className = "history-card";
      const header = document.createElement("div");
      header.className = "history-header";
      const badge = document.createElement("strong");
      badge.className = `copy-record-status${item.pending ? " is-pending" : ""}`;
      badge.textContent = item.pending ? (item.terminal ? "待处理" : "待补存") : "已留档";
      const time = document.createElement("span");
      time.textContent = displayCopyTime(item.copied_at || item.archived_at);
      const chars = document.createElement("span");
      chars.textContent = `${Array.from(item.text || "").length} 字`;
      header.append(badge, time, chars);
      const body = document.createElement("div");
      body.className = "history-text";
      body.textContent = item.text;
      if (item.archiveError) {
        const errorHint = document.createElement("p");
        errorHint.className = "copy-status";
        errorHint.textContent = item.archiveError;
        wrapper.appendChild(errorHint);
      }
      const actions = document.createElement("div");
      actions.className = "inline-row actions";
      const button = document.createElement("button");
      button.type = "button";
      button.className = "secondary-button";
      button.dataset.clipboardCopy = "true";
      button.disabled = clipboardBusy();
      button.textContent = "重新复制";
      button.addEventListener("click", function () { copyExistingRecord(item); });
      actions.appendChild(button);
      wrapper.append(header, body, actions);
      copyHistoryList.appendChild(wrapper);
    });
    if (!combined.length) {
      copyHistoryList.innerHTML = `<div class="history-empty">${state.copyHistoryError
        ? "复制留档读取失败，请点“刷新留档”重试。"
        : state.copyHistoryLoaded ? "还没有复制留档。" : "正在读取复制留档…"}</div>`;
    }
    retryCopyArchiveButton.hidden = state.copyOutbox.length === 0;
    retryCopyArchiveButton.disabled = state.copyFlushPending;
    const volatile = state.copyOutbox.some(function (item) { return item.storageFailed; });
    const rejectedCount = state.copyOutbox.filter(function (item) { return item.terminal; }).length;
    copyQueueStatus.textContent = volatile
      ? "部分待补存文字未能保存在本机；请保留页面，直到留档成功。"
      : state.outboxStorageError
      ? "无法读取本机待补存记录；请保留页面，避免丢失尚未留档的文字。"
      : state.copyOutbox.length
        ? rejectedCount
          ? `${state.copyOutbox.length} 条内容待留档，其中 ${rejectedCount} 条需要查看原因；可重新复制或手动重试。`
          : `${state.copyOutbox.length} 条已复制内容待补存。联网后会自动重试，也可手动重试留档。`
        : "已留档的文字可随时重新复制。";
    if (state.copyHistoryError) {
      copyQueueStatus.textContent += " 服务器留档读取失败；已显示的内容仍保留，请点“刷新留档”重试。";
    } else if (!state.copyHistoryLoaded && !state.copyOutbox.length) {
      copyQueueStatus.textContent = "正在读取这个 room 的复制留档…";
    }
    renderPcOutput();
  }

  async function fetchCopyHistory() {
    const generation = ++state.copyHistoryGeneration;
    try {
      const { response, payload } = await fetchJsonWithTimeout(appPath(`/api/copy-history?room_id=${encodeURIComponent(state.roomId)}`), { cache: "no-store" });
      if (!response.ok || !Array.isArray(payload.items)) throw new Error("copy_history_failed");
      if (generation === state.copyHistoryGeneration) {
        state.copyHistory = payload.items;
        state.copyHistoryLoaded = true;
        state.copyHistoryError = false;
        renderCopyHistory();
      }
    } catch (error) {
      if (generation === state.copyHistoryGeneration) {
        state.copyHistoryError = true;
        renderCopyHistory();
      }
      throw error;
    }
  }

  function restartCopyArchiveRetry() {
    state.copyRetryAttempts = 0;
    if (state.copyRetryTimer) {
      window.clearTimeout(state.copyRetryTimer);
      state.copyRetryTimer = null;
    }
    flushCopyOutbox();
  }

  function retryCopyArchivesManually() {
    state.copyOutbox.forEach(function (item) {
      item.terminal = false;
      item.archiveError = "";
      persistCopyItem(item);
    });
    restartCopyArchiveRetry();
  }

  function archiveRejectionMessage(error) {
    const messages = {
      text_too_large: "文字太长，请分段复制；原文已保留。",
      payload_too_large: "文字太长，请分段复制；原文已保留。",
      copy_id_conflict: "记录标识冲突，请重新复制；原文已保留。",
      unauthorized: "登录已过期，请重新登录；原文已保留。",
      forbidden: "当前账号无法留档；原文已保留。",
    };
    return messages[error] || "留档未被接受，文字已保留，可以重复制或重试留档。";
  }

  async function flushCopyOutbox() {
    if (state.copyFlushPending || !state.copyOutbox.length) {
      return;
    }
    state.copyFlushPending = true;
    renderCopyHistory();
    let failed = false;
    try {
      const queue = state.copyOutbox.slice();
      for (const item of queue) {
        if (item.terminal) {
          continue;
        }
        const controller = new AbortController();
        const timeout = window.setTimeout(function () { controller.abort(); }, 10000);
        try {
          const response = await fetch(appPath("/api/copy-archive"), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ room_id: item.room_id, copy_id: item.copy_id, text: item.text, copied_at: item.copied_at }),
            signal: controller.signal,
          });
          const payload = await response.json();
          if (!response.ok || payload.ok === false) {
            if (response.status >= 400 && response.status < 500 && ![408, 425, 429].includes(response.status)) {
              item.terminal = true;
              item.archiveError = archiveRejectionMessage(response.status === 401 ? "unauthorized" : payload.error);
              persistCopyItem(item);
              if (state.currentCopyId === item.copy_id) {
                setCopyStatus("已复制到手机剪贴板 · 留档被拒绝，文字已保留；请查看下方记录", "pending");
              }
              continue;
            }
            throw new Error(payload.error || `HTTP ${response.status}`);
          }
          state.copyOutbox = state.copyOutbox.filter(function (queued) { return queued.copy_id !== item.copy_id; });
          try {
            window.localStorage.removeItem(copyItemStorageKey(item.copy_id));
          } catch (error) {
            // An idempotent retry after reload remains safe if key removal fails.
          }
          state.copyHistory = [{ ...item, archived_at: payload.copy_archive && payload.copy_archive.archived_at }, ...state.copyHistory.filter(function (archived) { return archived.copy_id !== item.copy_id; })];
          if (state.currentCopyId === item.copy_id) {
            setCopyStatus("已复制到手机剪贴板 · 已留档", "saved");
          }
          if (state.captureMode === "clipboard" && autoClearToggle.checked && item.clearAfterArchive && item.clearGeneration === state.syncGeneration && item.clearRevision === state.draftRevision && mobileEditor.value === item.text) {
            rememberRecoveryDraft(item.text, 0);
            mobileEditor.value = "";
            state.draftRevision += 1;
            persistClipboardDraft();
            saveState.textContent = "已留档并清空，可继续输入";
            updateManualSendAvailability();
          }
          renderCopyHistory();
        } finally {
          window.clearTimeout(timeout);
        }
      }
      state.copyRetryAttempts = 0;
      fetchCopyHistory().catch(function () {});
    } catch (error) {
      failed = true;
      if (state.copyOutbox.some(function (item) { return item.copy_id === state.currentCopyId; })) {
        const current = state.copyOutbox.find(function (item) { return item.copy_id === state.currentCopyId; });
        setCopyStatus(current.storageFailed
          ? "已复制到手机剪贴板 · 待补存记录未能存到本机，请保留页面并重试留档"
          : "已复制到手机剪贴板 · 待补存，文字已保留，可点“重试留档”", "pending");
      }
    } finally {
      state.copyFlushPending = false;
      renderCopyHistory();
      if (failed && state.copyOutbox.some(function (item) { return !item.terminal; }) && state.copyRetryAttempts < 4 && !state.copyRetryTimer) {
        const delay = Math.min(15000, 1200 * (2 ** state.copyRetryAttempts));
        state.copyRetryAttempts += 1;
        state.copyRetryTimer = window.setTimeout(function () {
          state.copyRetryTimer = null;
          flushCopyOutbox();
        }, delay);
      } else if (!failed && state.copyOutbox.some(function (item) { return !item.terminal; })) {
        // A new copy may have arrived while this snapshot was being saved.
        flushCopyOutbox();
      }
    }
  }

  function relativeAppPath(pathname) {
    if (basePath && pathname.startsWith(basePath)) {
      return pathname.slice(basePath.length) || "/";
    }
    return pathname;
  }

  function inferRoleFromPath() {
    const parts = relativeAppPath(window.location.pathname).split("/").filter(Boolean);
    if (parts[0] === "mobile") {
      state.role = "mobile";
      state.roomId = decodeURIComponent(parts[1] || "doubao");
      return;
    }
    if (parts[0] === "pc") {
      state.role = "pc";
      state.roomId = decodeURIComponent(parts[1] || "doubao");
      return;
    }
    state.role = "landing";
    state.roomId = "doubao";
  }

  function setRoleView() {
    if (state.role === "mobile") {
      title.textContent = "手机输入区";
      subtitle.textContent = "写好，点一下粘贴到电脑。文字由豆包同步，每次复制都会后台留档。";
      mobilePanel.hidden = false;
      pcPanel.hidden = true;
      return;
    }
    if (state.role === "pc") {
      title.textContent = "PC 同步区";
      subtitle.textContent = "剪贴板模式可直接在电脑粘贴，也能从下方复制留档取回文字。";
      mobilePanel.hidden = true;
      pcPanel.hidden = false;
      return;
    }
    title.textContent = "同步服务已启动";
    subtitle.textContent = "你可以打开 /mobile/<room> 和 /pc/<room> 开始同步。";
    mobilePanel.hidden = false;
    pcPanel.hidden = false;
  }

  function clientIdStorageKey() {
    return `${clientIdStoragePrefix}${state.role}:${state.roomId}`;
  }

  function ensureClientId() {
    if (state.role === "landing") {
      return;
    }
    const storageKey = clientIdStorageKey();
    let clientId = readLocalStorage(storageKey);
    if (!clientId) {
      clientId = `${state.role}-${Math.random().toString(36).slice(2, 10)}`;
      writeLocalStorage(storageKey, clientId);
    }
    state.clientId = clientId;
  }

  function autoClearStorageKey() {
    return `${autoClearStoragePrefix}${state.roomId}`;
  }

  function readRecoveryDraft() {
    try {
      const raw = readLocalStorage(`${recoveryStoragePrefix}${state.roomId}`);
      const recovery = raw ? JSON.parse(raw) : null;
      return recovery && typeof recovery.text === "string" && recovery.text.trim() ? recovery : null;
    } catch (error) {
      return null;
    }
  }

  function updateRecoveryAvailability() {
    const recovery = readRecoveryDraft();
    restoreLastButton.hidden = state.role !== "mobile" || !recovery;
    restoreLastButton.disabled = !recovery || state.manualSendPending || state.modeChangePending || state.claimConflict || clipboardBusy();
    clearButton.disabled = state.manualSendPending;
  }

  function rememberRecoveryDraft(text, archiveId) {
    if (!text || !text.trim()) {
      return;
    }
    writeLocalStorage(`${recoveryStoragePrefix}${state.roomId}`, JSON.stringify({ text, archiveId: archiveId || 0, savedAt: new Date().toISOString() }));
    updateRecoveryAvailability();
  }

  async function restoreArchivedDraft(text, archiveId, source) {
    if (state.role !== "mobile" || state.manualSendPending || state.modeChangePending || state.claimConflict || clipboardBusy() || typeof text !== "string" || !text.trim()) {
      return;
    }
    if (mobileEditor.value.trim() && mobileEditor.value !== text && !window.confirm("当前输入框已有内容。要用这条存档覆盖当前内容吗？")) {
      return;
    }
    cancelDraftWork();
    rememberRecoveryDraft(text, archiveId);
    mobileEditor.value = text;
    state.mobileHydrated = true;
    state.draftRevision += 1;
    invalidatePreparation();
    schedulePreparation();
    persistClipboardDraft();
    if (state.captureMode === "clipboard") {
      saveState.textContent = "已恢复到本机输入框；点复制才会留档";
    } else if (state.captureMode !== "manual") {
      await changeCaptureMode("manual");
    } else {
      await pushDraft(text, { source: source || "mobile-recovery", doneText: "已恢复到换气模式，确认后再点发送", scheduleAutoClear: false });
    }
    updateManualSendAvailability();
    mobileEditor.focus();
    mobileEditor.setSelectionRange(mobileEditor.value.length, mobileEditor.value.length);
  }

  function restoreLastClearedDraft() {
    const recovery = readRecoveryDraft();
    if (recovery) {
      restoreArchivedDraft(recovery.text, recovery.archiveId, "mobile-auto-clear-restore");
    }
  }

  function defaultThemeForRoom() {
    const room = state.roomId || "doubao";
    let hash = 0;
    for (let index = 0; index < room.length; index += 1) {
      hash = (hash * 31 + room.charCodeAt(index)) >>> 0;
    }
    return themeOptions[hash % themeOptions.length];
  }

  function applyTheme(theme, statusText) {
    const nextTheme = themeOptions.includes(theme) ? theme : defaultThemeForRoom();
    document.body.dataset.theme = nextTheme;
    if (themePicker) {
      themePicker.querySelectorAll(".theme-swatch").forEach(function (button) {
        const isSelected = button.dataset.theme === nextTheme;
        button.classList.toggle("is-selected", isSelected);
        button.setAttribute("aria-checked", isSelected ? "true" : "false");
      });
    }
    if (themeStatus && statusText) {
      themeStatus.textContent = statusText;
    }
  }

  function loadLocalPreferences() {
    const storedValue = readLocalStorage(autoClearStorageKey());
    autoClearToggle.checked = storedValue === null ? true : storedValue === "1";
    applyTheme(defaultThemeForRoom(), "同一个 room 的手机端和 PC 端会同步底色。");
    updateRecoveryAvailability();
  }

  function persistLocalPreferences() {
    writeLocalStorage(autoClearStorageKey(), autoClearToggle.checked ? "1" : "0");
    if (!autoClearToggle.checked) {
      cancelScheduledDesktopAckCheck();
    }
  }

  function updateManualSendAvailability() {
    if (!manualSendButton) {
      return;
    }
    const isManual = state.captureMode === "manual";
    manualSendButton.hidden = !isManual;
    manualSendButton.disabled =
      !isManual ||
      state.manualSendPending ||
      clipboardBusy() ||
      state.modeChangePending ||
      state.claimedOk === false ||
      !mobileEditor.value.trim();
    const modeDisabled = state.modeChangePending || state.manualSendPending || state.claimConflict || clipboardBusy();
    autoModeButton.disabled = modeDisabled;
    manualModeButton.disabled = modeDisabled;
    clipboardModeButton.disabled = modeDisabled;
    clipboardActions.hidden = state.captureMode !== "clipboard";
    clipboardButtons.hidden = state.captureMode !== "clipboard";
    copyArchiveButton.hidden = state.captureMode !== "clipboard";
    copyArchiveButton.disabled = state.captureMode !== "clipboard" || clipboardBusy() || state.modeChangePending || state.manualSendPending || state.claimConflict || !mobileEditor.value.trim();
    copyPasteButton.hidden = state.role !== "mobile";
    copyPasteButton.disabled = copyArchiveButton.disabled || !state.signalPageActive || state.role !== "mobile";
    copyPasteButton.textContent = state.signalQueuedPaste ? "等待电脑就绪…" : state.signalActionPending ? "正在粘贴…" : "复制并粘贴";
    copyButton.disabled = clipboardBusy();
    reconnectButton.disabled = clipboardBusy();
    [copyHistoryList, historyList].forEach(function (list) {
      list.querySelectorAll("[data-clipboard-copy]").forEach(function (button) { button.disabled = clipboardBusy(); });
    });
    updateRecoveryAvailability();
    renderPcOutput();
  }

  function renderCaptureMode(mode) {
    const nextMode = ["auto", "manual", "clipboard"].includes(mode) ? mode : "clipboard";
    // A remote mode update must not turn continued typing into relay writes
    // while this page is waiting for a paste of the copied snapshot.
    if (clipboardBusy() && nextMode !== state.captureMode) return;
    if (nextMode !== state.captureMode) {
      if (state.captureMode === "clipboard" && nextMode === "manual" && mobileEditor.value) {
        state.manualFallbackDraftProtected = true;
      }
      cancelDraftWork();
      invalidatePreparation();
      if (nextMode === "clipboard" && state.role === "mobile") {
        persistClipboardDraft();
      }
    }
    state.captureMode = nextMode;
    schedulePreparation();
    if (state.role === "pc" && nextMode !== "clipboard") pcPanelHint.textContent = "发送模式 · 最新发送内容";
    const isManual = nextMode === "manual";
    const isClipboard = nextMode === "clipboard";
    autoClearLabel.textContent = isClipboard ? "仅复制：留档后自动清空" : "电脑收到后自动清空";
    composerShell.dataset.captureMode = nextMode;
    autoModeButton.classList.toggle("is-selected", nextMode === "auto");
    manualModeButton.classList.toggle("is-selected", isManual);
    clipboardModeButton.classList.toggle("is-selected", isClipboard);
    autoModeButton.setAttribute("aria-pressed", nextMode === "auto" ? "true" : "false");
    manualModeButton.setAttribute("aria-pressed", isManual ? "true" : "false");
    clipboardModeButton.setAttribute("aria-pressed", isClipboard ? "true" : "false");

    if (isClipboard) {
      captureModeHint.textContent = "说完即可点复制并粘贴；电脑未就绪时自动等最多 15 秒，匹配后粘贴。等待时修改文字会取消；执行后清空，失败或超时保留，可切换换气发送。";
      mobilePanelHint.textContent = "豆包同步文字，一次点击完成复制与电脑粘贴";
      archiveHint.textContent = "剪贴板模式的记录保存在上方“复制留档”";
    } else if (isManual) {
      captureModeHint.textContent = "放心换气、思考或停顿：草稿会保存，但只有点“说完了，发送”才会进电脑。";
      mobilePanelHint.textContent = "换气模式已开启，停多久都不会自动发送";
      archiveHint.textContent = "换气模式：只在手动发送后入档";
    } else {
      const idleSeconds = getArchiveIdleSeconds();
      captureModeHint.textContent = `当前会在停顿约 ${idleSeconds} 秒后自动发送到电脑。`;
      mobilePanelHint.textContent = "顺口模式下，停顿一段时间就会自动发送";
      archiveHint.textContent = `输入停顿约 ${idleSeconds} 秒后自动入档`;
    }
    updateManualSendAvailability();
  }

  function triggerFlash(message) {
    if (!syncFlash) {
      return;
    }
    syncFlash.textContent = message;
    syncFlash.classList.remove("is-active");
    void syncFlash.offsetWidth;
    syncFlash.classList.add("is-active");
    if (state.flashTimer) {
      window.clearTimeout(state.flashTimer);
    }
    state.flashTimer = window.setTimeout(function () {
      syncFlash.classList.remove("is-active");
    }, 1500);
  }

  function triggerCapturedPulse() {
    if (!mobileEditor) {
      return;
    }
    mobilePanel.classList.remove("capture-flash");
    mobileEditor.classList.remove("sync-captured");
    void mobileEditor.offsetWidth;
    mobilePanel.classList.add("capture-flash");
    mobileEditor.classList.add("sync-captured");
    if (navigator.vibrate) {
      navigator.vibrate(24);
    }
    if (state.capturePulseTimer) {
      window.clearTimeout(state.capturePulseTimer);
    }
    state.capturePulseTimer = window.setTimeout(function () {
      mobilePanel.classList.remove("capture-flash");
      mobileEditor.classList.remove("sync-captured");
    }, 1500);
  }

  function setClaimWarning(message) {
    if (!claimWarning) {
      return;
    }
    if (!message) {
      claimWarning.hidden = true;
      claimWarning.textContent = "";
      return;
    }
    claimWarning.hidden = false;
    claimWarning.textContent = message;
  }

  function renderRoomState(payload) {
    if (payload.server_epoch && payload.server_epoch !== state.serverEpoch) {
      if (state.retiredServerEpochs.has(payload.server_epoch)) {
        return;
      }
      if (state.serverEpoch !== null) {
        state.retiredServerEpochs.add(state.serverEpoch);
        cancelDraftWork();
        state.lastVersion = 0;
        state.lastArchiveId = 0;
        state.lastAutoClearedArchiveId = 0;
        state.lastSettingsRevision = 0;
      }
      state.serverEpoch = payload.server_epoch;
    }
    if (payload.settings) {
      const revision = Number.isInteger(payload.settings.revision) ? payload.settings.revision : 0;
      if (revision >= state.lastSettingsRevision) {
        state.lastSettingsRevision = revision;
        if (payload.settings.archive_idle_seconds) {
          archiveIdleInput.value = payload.settings.archive_idle_seconds;
        }
        // A pending (or unconfirmed offline) mode choice owns the local composer.
        renderCaptureMode(state.desiredCaptureMode || payload.settings.capture_mode || state.captureMode);
        const roomTheme = payload.settings.theme || defaultThemeForRoom();
        applyTheme(roomTheme, `当前 room 底色：${roomTheme}`);
      }
    }
    if (typeof payload.version === "number" && payload.version < state.lastVersion) {
      return;
    }
    const previousVersion = state.lastVersion;
    const nextHistoryCount = (payload.history || []).length;
    const latestArchive = nextHistoryCount ? payload.history[nextHistoryCount - 1] : null;
    state.pcRelayArchive = latestArchive;
    const latestArchiveId = latestArchive ? latestArchive.archive_id : 0;
    const sawNewArchive = latestArchiveId > state.lastArchiveId;
    const shouldClearFromServerState =
      state.role === "mobile" &&
      state.captureMode !== "clipboard" &&
      !state.modeChangePending &&
      !(state.captureMode === "manual" && state.manualFallbackDraftProtected) &&
      autoClearToggle.checked &&
      mobileEditor &&
      payload.text === "" &&
      mobileEditor.value &&
      payload.version >= previousVersion;

    state.lastVersion = payload.version;
    state.lastArchiveId = Math.max(state.lastArchiveId, latestArchiveId);
    versionBadge.textContent = `version ${payload.version}`;
    updatedAt.textContent = payload.updated_at ? `updated at: ${payload.updated_at}` : "尚未收到内容";
    sourceLine.textContent = `source: ${payload.source || "-"}`;
    if (state.role !== "mobile") {
      if (latestArchive && latestArchive.text) {
        output.textContent = latestArchive.text;
      } else if (payload.text) {
        output.textContent = "正在输入，等待稳定归档…";
      } else {
        output.textContent = "等待手机端输入…";
      }
      renderPcOutput();
    }
    if (state.role === "mobile" && !state.mobileHydrated && document.activeElement !== mobileEditor && state.captureMode !== "clipboard" && !state.modeChangePending) {
      mobileEditor.value = payload.text || "";
      state.mobileHydrated = true;
    }
    renderHistory(payload.history || []);

    if (state.role === "mobile" && (state.captureMode === "clipboard" || state.modeChangePending)) {
      updateManualSendAvailability();
      return;
    }

    if (clearMobileEditorAfterDesktopAck(latestArchive)) {
      return;
    }

    if (sawNewArchive) {
      const wasManualCapture = latestArchive && latestArchive.source === "mobile-web-manual";
      saveState.textContent = autoClearToggle.checked ? "已发送，等待电脑端接收" : "已发送到电脑";
      triggerFlash(wasManualCapture ? "换气结束，这批文字已发送" : "已捕捉并同步一批文字");
      if (state.role === "mobile") {
        triggerCapturedPulse();
      }
      return;
    }

    if (shouldClearFromServerState) {
      mobileEditor.value = "";
      saveState.textContent = "已同步空白状态";
    }

    if (payload.version > previousVersion && previousVersion !== 0) {
      saveState.textContent = state.captureMode === "manual" ? "草稿已保存，放心慢慢说" : "草稿已同步，继续等稳定版本";
    }
  }

  function renderHistory(historyItems) {
    if (!historyItems.length) {
      historyList.innerHTML = '<div class="history-empty">还没有归档内容。</div>';
      return;
    }

    historyList.innerHTML = "";
    historyItems
      .slice()
      .reverse()
      .forEach(function (item) {
        const wrapper = document.createElement("article");
        wrapper.className = "history-card";

        const header = document.createElement("div");
        header.className = "history-header";
        header.innerHTML = `
          <strong>#${item.archive_id}</strong>
          <span>${item.archived_at}</span>
          <span>${item.chars} chars</span>
        `;

        const body = document.createElement("div");
        body.className = "history-text";
        body.textContent = item.text;

        const actions = document.createElement("div");
        actions.className = "inline-row actions";

        const copyItemButton = document.createElement("button");
        copyItemButton.type = "button";
        copyItemButton.textContent = "复制这一条";
        copyItemButton.dataset.clipboardCopy = "true";
        copyItemButton.disabled = clipboardBusy();
        copyItemButton.addEventListener("click", function () { copyExistingRecord(item); });

        actions.appendChild(copyItemButton);
        if (state.role === "mobile") {
          const restoreItemButton = document.createElement("button");
          restoreItemButton.type = "button";
          restoreItemButton.className = "secondary-button";
          restoreItemButton.textContent = "恢复到输入框";
          restoreItemButton.addEventListener("click", function () {
            restoreArchivedDraft(item.text, item.archive_id, "mobile-history-restore");
          });
          actions.appendChild(restoreItemButton);
        }
        wrapper.appendChild(header);
        wrapper.appendChild(body);
        wrapper.appendChild(actions);
        historyList.appendChild(wrapper);
      });
  }

  async function fetchState() {
    const response = await fetch(appPath(`/api/state?room_id=${encodeURIComponent(state.roomId)}`));
    const payload = await response.json();
    renderRoomState(payload);
  }

  async function fetchServerInfo() {
    const response = await fetch(appPath("/api/server-info"));
    const payload = await response.json();
    if (payload.archive_idle_seconds) {
      archiveIdleInput.value = payload.archive_idle_seconds;
      renderCaptureMode(state.captureMode);
    }
  }

  function clearMobileEditorAfterDesktopAck(latestArchive) {
    if (
      state.role !== "mobile" ||
      state.captureMode === "clipboard" ||
      state.modeChangePending ||
      (state.captureMode === "manual" && state.manualFallbackDraftProtected) ||
      !autoClearToggle.checked ||
      !mobileEditor ||
      !latestArchive ||
      !latestArchive.desktop_received_at ||
      latestArchive.archive_id <= state.lastAutoClearedArchiveId
    ) {
      return false;
    }

    const archivedText = latestArchive.text || "";
    if (mobileEditor.value && mobileEditor.value !== archivedText) {
      saveState.textContent = "电脑端已接收，新输入未清空";
      state.lastAutoClearedArchiveId = latestArchive.archive_id;
      return false;
    }

    state.lastAutoClearedArchiveId = latestArchive.archive_id;
    rememberRecoveryDraft(archivedText, latestArchive.archive_id);
    mobileEditor.value = "";
    state.draftRevision += 1;
    persistClipboardDraft();
    updateManualSendAvailability();
    pushDraft("", {
      source: "mobile-auto-clear",
      startText: "同步清空状态中…",
      doneText: "电脑端已接收，已自动清空；可恢复刚才内容",
      errorText: "已本地清空，但刚才内容仍可恢复",
      scheduleAutoClear: false,
    });
    return true;
  }

  function cancelScheduledDesktopAckCheck() {
    if (state.desktopAckCheckTimer) {
      window.clearTimeout(state.desktopAckCheckTimer);
      state.desktopAckCheckTimer = null;
    }
    state.desktopAckCheckAttempts = 0;
  }

  function getArchiveIdleSeconds() {
    const value = Number.parseFloat(archiveIdleInput.value);
    return Number.isFinite(value) && value >= 0.5 ? value : 2.0;
  }

  function scheduleDesktopAckCheck(textAtSync, delayMs) {
    if (state.role !== "mobile" || state.captureMode === "clipboard" || state.modeChangePending || !autoClearToggle.checked || !textAtSync.trim()) {
      return;
    }

    if (state.desktopAckCheckAttempts >= 8) {
      return;
    }

    if (state.desktopAckCheckTimer) {
      window.clearTimeout(state.desktopAckCheckTimer);
    }

    state.desktopAckCheckTimer = window.setTimeout(async function () {
      state.desktopAckCheckTimer = null;
      if (state.captureMode === "clipboard" || state.modeChangePending || !autoClearToggle.checked || mobileEditor.value !== textAtSync) {
        return;
      }
      state.desktopAckCheckAttempts += 1;
      try {
        await fetchState();
      } finally {
        if (state.captureMode !== "clipboard" && !state.modeChangePending && mobileEditor.value === textAtSync) {
          saveState.textContent = "已捕捉，等待电脑端接收";
          scheduleDesktopAckCheck(textAtSync, 2000);
        }
      }
    }, delayMs);
  }

  function scheduleDesktopAckFallback(textAtSync) {
    cancelScheduledDesktopAckCheck();
    scheduleDesktopAckCheck(textAtSync, (getArchiveIdleSeconds() + 1) * 1000);
  }

  async function sendDraftRequest(text, options = {}) {
    if (state.captureMode === "clipboard") {
      return null;
    }
    const generation = state.syncGeneration;
    const captureMode = options.captureMode || state.captureMode;
    saveState.textContent = options.startText || "同步中…";
    state.pendingText = text;
    try {
      const { response, payload } = await fetchJsonWithTimeout(appPath("/api/update"), {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          room_id: state.roomId,
          text,
          source: options.source || "mobile-web",
          capture_mode: captureMode,
          expected_settings_revision: state.lastSettingsRevision,
        }),
      });
      if (generation !== state.syncGeneration || state.captureMode === "clipboard") {
        return null;
      }
      if (!response.ok) {
        if (payload.state) {
          renderRoomState(payload.state);
        }
        throw new Error(payload.error || `HTTP ${response.status}`);
      }
      renderRoomState(payload);
      if (generation !== state.syncGeneration || state.captureMode === "clipboard") {
        return null;
      }
      if (state.pendingText === text) {
        state.pendingText = null;
      }
      saveState.textContent = options.doneText || (state.captureMode === "manual" ? "草稿已保存，放心慢慢说" : "已同步");
      if (options.scheduleAutoClear !== false && state.captureMode === "auto") {
        scheduleDesktopAckFallback(text);
      }
      return payload;
    } catch (error) {
      if (generation !== state.syncGeneration || state.captureMode === "clipboard") {
        return null;
      }
      saveState.textContent = options.errorText || "网络中断，自动重试中…";
      if (options.retry !== false) {
        scheduleDraftRetry();
      }
      return null;
    }
  }

  function pushDraft(text, options = {}) {
    let request;
    request = sendDraftRequest(text, options).finally(function () {
      state.draftRequests.delete(request);
    });
    state.draftRequests.add(request);
    return request;
  }

  async function waitForPendingDraftRequests() {
    while (state.draftRequests.size) {
      await Promise.allSettled(Array.from(state.draftRequests));
    }
  }

  function scheduleDraftRetry() {
    if (state.captureMode === "clipboard" || state.modeChangePending || state.manualSendPending || state.retryTimer || state.pendingText === null) {
      return;
    }
    state.retryTimer = window.setTimeout(function () {
      state.retryTimer = null;
      if (state.captureMode !== "clipboard" && !state.modeChangePending && state.pendingText !== null) {
        pushDraft(state.pendingText);
      }
    }, 1200);
  }

  async function changeCaptureMode(nextMode) {
    if (!['auto', 'manual', 'clipboard'].includes(nextMode) || (nextMode === state.captureMode && !state.desiredCaptureMode) || state.modeChangePending || state.manualSendPending || state.claimConflict || clipboardBusy()) {
      return;
    }

    const previousMode = state.captureMode;
    cancelDraftWork();
    state.modeChangePending = true;
    state.desiredCaptureMode = nextMode;
    if (previousMode === "clipboard") {
      // Switching to manual fallback is not a new send. Old relay receipts,
      // even for identical text, cannot erase the retained clipboard draft.
      state.manualFallbackDraftProtected = nextMode === "manual";
      state.lastAutoClearedArchiveId = Math.max(state.lastAutoClearedArchiveId, state.lastArchiveId);
      persistClipboardDraft();
    }
    renderCaptureMode(nextMode);
    saveState.textContent = "正在确认输入模式…";
    try {
      const { response, payload } = await fetchJsonWithTimeout(appPath("/api/settings"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ room_id: state.roomId, capture_mode: nextMode }),
      });
      if (!response.ok || !payload.settings || payload.settings.capture_mode !== nextMode || payload.settings.revision < state.lastSettingsRevision || (state.serverEpoch && payload.server_epoch && state.serverEpoch !== payload.server_epoch)) {
        throw new Error(payload.error || "mode_not_confirmed");
      }
      state.desiredCaptureMode = null;
      renderRoomState(payload);
      state.modeChangePending = false;
      if (nextMode === "clipboard") {
        persistClipboardDraft();
        saveState.textContent = "剪贴板模式已开启；草稿只保存在本机";
      } else {
        await pushDraft(mobileEditor.value, {
          source: "mobile-mode-switch",
          captureMode: nextMode,
          doneText: nextMode === "manual" ? "换气模式已开启，放心慢慢说" : "顺口模式已恢复，停顿后自动发送",
          scheduleAutoClear: nextMode === "auto",
        });
      }
    } catch (error) {
      if (nextMode === "clipboard") {
        // Keep offline clipboard drafts isolated even if the server is unreachable.
        saveState.textContent = "本页已停止发送；服务器模式未确认，请点剪贴板模式重试";
      } else {
        state.desiredCaptureMode = null;
        renderCaptureMode(previousMode);
        saveState.textContent = "模式切换失败，草稿已保留，请再试一次";
      }
    } finally {
      state.modeChangePending = false;
      updateManualSendAvailability();
    }
  }

  async function sendManualCapture() {
    const generation = state.syncGeneration;
    const textAtSend = mobileEditor.value;
    if (state.captureMode !== "manual" || state.manualSendPending || clipboardBusy() || !textAtSend.trim()) {
      return;
    }

    rememberRecoveryDraft(textAtSend, 0);

    if (state.draftSaveTimer) {
      window.clearTimeout(state.draftSaveTimer);
      state.draftSaveTimer = null;
    }
    if (state.retryTimer) {
      window.clearTimeout(state.retryTimer);
      state.retryTimer = null;
    }
    cancelScheduledDesktopAckCheck();
    state.manualSendPending = true;
    mobileEditor.readOnly = true;
    updateManualSendAvailability();

    saveState.textContent = "正在等待前面的草稿同步完成…";
    await waitForPendingDraftRequests();
    if (generation !== state.syncGeneration || state.captureMode !== "manual") {
      state.manualSendPending = false;
      mobileEditor.readOnly = false;
      updateManualSendAvailability();
      return;
    }

    const synced = await pushDraft(textAtSend, {
      source: "mobile-web-manual",
      captureMode: "manual",
      startText: "正在确认最后一版草稿…",
      doneText: "最后一版草稿已保存，正在发送…",
      errorText: "草稿同步失败，还没有发送",
      scheduleAutoClear: false,
      retry: false,
    });

    if (!synced || generation !== state.syncGeneration || state.captureMode !== "manual") {
      state.manualSendPending = false;
      mobileEditor.readOnly = false;
      updateManualSendAvailability();
      return;
    }

    try {
      const { response, payload } = await fetchJsonWithTimeout(appPath("/api/capture"), {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          room_id: state.roomId,
          expected_version: synced.version,
        }),
      });
      if (generation !== state.syncGeneration || state.captureMode !== "manual") {
        return;
      }
      if (!response.ok || !payload.ok) {
        if (payload.state) {
          renderRoomState(payload.state);
        }
        throw new Error(payload.error || `HTTP ${response.status}`);
      }
      state.lastAutoClearedArchiveId = Math.max(state.lastAutoClearedArchiveId, state.lastArchiveId);
      state.manualFallbackDraftProtected = false;
      renderRoomState(payload.state);
      saveState.textContent = autoClearToggle.checked ? "已发送，等待电脑端接收" : "已发送到电脑";
      scheduleDesktopAckCheck(textAtSend, 500);
    } catch (error) {
      if (generation === state.syncGeneration && state.captureMode === "manual") {
        saveState.textContent = "发送没有完成，草稿还在，请再点一次";
      }
    } finally {
      state.manualSendPending = false;
      mobileEditor.readOnly = false;
      updateManualSendAvailability();
    }
  }

  async function saveSettings() {
    settingsStatus.textContent = "保存中…";
    try {
      const response = await fetch(appPath("/api/settings"), {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          room_id: state.roomId,
          archive_idle_seconds: archiveIdleInput.value,
        }),
      });
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.error || `HTTP ${response.status}`);
      }
      renderRoomState(payload);
      settingsStatus.textContent = "已保存";
    } catch (error) {
      settingsStatus.textContent = "保存失败，请重试";
    }
  }

  async function saveTheme(theme) {
    const nextTheme = themeOptions.includes(theme) ? theme : defaultThemeForRoom();
    applyTheme(nextTheme, "同步底色中…");
    try {
      const response = await fetch(appPath("/api/settings"), {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          room_id: state.roomId,
          theme: nextTheme,
        }),
      });
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.error || `HTTP ${response.status}`);
      }
      renderRoomState(payload);
      if (themeStatus) {
        themeStatus.textContent = `已同步 ${state.roomId} 的底色`;
      }
    } catch (error) {
      applyTheme(nextTheme, "底色同步失败，请重试");
    }
  }

  async function claimCurrentRole() {
    if (state.role === "landing") {
      return true;
    }
    ensureClientId();
    try {
      const response = await fetch(appPath("/api/claim"), {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          room_id: state.roomId,
          role: state.role,
          client_id: state.clientId,
          client_label: navigator.userAgent.slice(0, 120),
        }),
      });
      const payload = await response.json();
      if (payload.conflict) {
        state.claimedOk = false;
        state.claimConflict = true;
        setClaimWarning(`这个房间的 ${state.role === "mobile" ? "手机位" : "PC 位"} 已经被另一台设备占用了。请换一个新的 room id 再继续。`);
        if (state.role === "mobile") {
          mobileEditor.disabled = true;
        }
        updateManualSendAvailability();
        return false;
      }
      state.claimedOk = true;
      state.claimConflict = false;
      setClaimWarning("");
      if (state.role === "mobile") {
        mobileEditor.disabled = false;
      }
      updateManualSendAvailability();
      if (payload.state) {
        renderRoomState(payload.state);
      }
      return true;
    } catch (error) {
      setClaimWarning("当前无法确认配对状态，正在自动重试。");
      return false;
    }
  }

  function stopClaimHeartbeat() {
    if (!state.claimHeartbeatTimer) {
      return;
    }
    window.clearInterval(state.claimHeartbeatTimer);
    state.claimHeartbeatTimer = null;
  }

  function startClaimHeartbeat() {
    if (state.role === "landing" || state.claimHeartbeatTimer) {
      return;
    }
    state.claimHeartbeatTimer = window.setInterval(function () {
      claimCurrentRole();
    }, claimHeartbeatIntervalMs);
  }

  function releaseCurrentRole() {
    if (state.role === "landing" || !state.clientId) {
      return;
    }
    const payload = JSON.stringify({
      room_id: state.roomId,
      role: state.role,
      client_id: state.clientId,
    });
    const url = appPath("/api/release");
    stopClaimHeartbeat();
    if (navigator.sendBeacon) {
      const body = new Blob([payload], { type: "application/json" });
      navigator.sendBeacon(url, body);
      return;
    }
    fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: payload,
      keepalive: true,
    }).catch(function () {});
  }

  function scheduleReconnect() {
    if (state.reconnectTimer) {
      return;
    }
    state.reconnectTimer = window.setTimeout(function () {
      state.reconnectTimer = null;
      connectStream();
    }, 1800);
  }

  function ensurePolling() {
    if (state.pollTimer) {
      return;
    }
    state.pollTimer = window.setInterval(function () {
      fetchState().catch(function () {
        connectionStatus.textContent = "已断开，轮询重试中";
      });
    }, 3000);
  }

  function stopPolling() {
    if (!state.pollTimer) {
      return;
    }
    window.clearInterval(state.pollTimer);
    state.pollTimer = null;
  }

  function connectStream() {
    if (state.eventSource) {
      state.eventSource.close();
    }

    connectionStatus.textContent = "连接中";
    let eventSource;
    try {
      eventSource = new EventSource(appPath(`/api/stream?room_id=${encodeURIComponent(state.roomId)}`));
    } catch (error) {
      state.eventSource = null;
      connectionStatus.textContent = "实时连接不可用，自动刷新中";
      ensurePolling();
      startClaimHeartbeat();
      return;
    }
    state.eventSource = eventSource;

    eventSource.addEventListener("open", function () {
      connectionStatus.textContent = "已连接";
      stopPolling();
      claimCurrentRole();
      startClaimHeartbeat();
    });

    eventSource.addEventListener("error", function () {
      connectionStatus.textContent = "已断开，自动重连中";
      if (state.eventSource) {
        state.eventSource.close();
        state.eventSource = null;
      }
      stopClaimHeartbeat();
      ensurePolling();
      scheduleReconnect();
    });

    eventSource.addEventListener("room_state", function (event) {
      const payload = JSON.parse(event.data);
      renderRoomState(payload);
    });
  }

  function rebuildPath() {
    if (clipboardBusy()) return;
    const nextRoom = roomInput.value.trim() || "doubao";
    const prefix = state.role === "pc" ? "/pc/" : "/mobile/";
    persistLocalPreferences();
    releaseCurrentRole();
    if (state.role === "landing") {
      window.location.href = appPath(`/pc/${encodeURIComponent(nextRoom)}`);
      return;
    }
    window.location.href = appPath(`${prefix}${encodeURIComponent(nextRoom)}`);
  }

  function bindEvents() {
    reconnectButton.addEventListener("click", rebuildPath);
    refreshButton.addEventListener("click", function () {
      fetchState().catch(function () { connectionStatus.textContent = "状态读取失败，请重试"; });
      fetchCopyHistory().catch(function () {});
    });
    copyHistoryRefreshButton.addEventListener("click", function () { fetchCopyHistory().catch(function () {}); });
    saveSettingsButton.addEventListener("click", saveSettings);
    autoClearToggle.addEventListener("change", persistLocalPreferences);
    autoModeButton.addEventListener("click", function () {
      changeCaptureMode("auto");
    });
    manualModeButton.addEventListener("click", function () {
      changeCaptureMode("manual");
    });
    clipboardModeButton.addEventListener("click", function () {
      changeCaptureMode("clipboard");
    });
    copyArchiveButton.addEventListener("click", function () { copyAndArchive(false); });
    copyPasteButton.addEventListener("click", function () { copyAndArchive(true); });
    retryCopyArchiveButton.addEventListener("click", retryCopyArchivesManually);
    manualSendButton.addEventListener("click", sendManualCapture);
    if (themePicker) {
      themePicker.querySelectorAll(".theme-swatch").forEach(function (button) {
        button.setAttribute("role", "radio");
        button.addEventListener("click", function () {
          saveTheme(button.dataset.theme);
        });
      });
    }

    copyButton.addEventListener("click", function () {
      if (copyButton.disabled) return;
      const value = output.textContent === "等待手机端输入…" ? "" : output.textContent;
      copyExistingRecord({ text: value });
    });

    clearButton.addEventListener("click", function () {
      if (state.manualSendPending) {
        return;
      }
      rememberRecoveryDraft(mobileEditor.value, 0);
      cancelDraftWork();
      mobileEditor.value = "";
      state.mobileHydrated = true;
      state.draftRevision += 1;
      invalidatePreparation();
      advanceQueuedPaste();
      persistClipboardDraft();
      updateManualSendAvailability();
      if (state.captureMode === "clipboard" || state.modeChangePending) {
        saveState.textContent = "已清空本机输入框；可恢复刚才内容";
        return;
      }
      pushDraft("", {
        source: "mobile-clear",
        scheduleAutoClear: false,
      });
    });
    restoreLastButton.addEventListener("click", restoreLastClearedDraft);

    mobileEditor.addEventListener("input", function () {
      state.mobileHydrated = true;
      state.draftRevision += 1;
      invalidatePreparation();
      advanceQueuedPaste();
      rememberRecoveryDraft(mobileEditor.value, 0);
      cancelDraftWork();
      if (state.captureMode === "clipboard" || state.modeChangePending) {
        const persisted = persistClipboardDraft();
        if (persisted) {
          saveState.textContent = "草稿已保存在本机，点复制才会留档";
        }
        schedulePreparation();
        renderSignalReadiness();
        updateManualSendAvailability();
        return;
      }
      saveState.textContent = state.captureMode === "manual" ? "输入中，草稿保存中；不会自动发送" : "输入中，自动同步中…";
      updateManualSendAvailability();
      cancelScheduledDesktopAckCheck();
      if (state.draftSaveTimer) {
        window.clearTimeout(state.draftSaveTimer);
      }
      state.draftSaveTimer = window.setTimeout(function () {
        state.draftSaveTimer = null;
        if (state.captureMode !== "clipboard" && !state.modeChangePending && state.claimedOk !== false) {
          pushDraft(mobileEditor.value);
        }
      }, 180);
    });

    window.addEventListener("pagehide", function () {
      state.signalPageActive = false;
      state.signalPageGeneration += 1;
      invalidatePreparation();
      advanceQueuedPaste();
      state.signalReady = false;
      state.signalSessionId = null;
      if (state.captureMode === "clipboard") {
        persistClipboardDraft();
      }
      releaseCurrentRole();
    });
    window.addEventListener("pageshow", function () {
      state.signalPageActive = true;
      void pollSignalStatus();
    });
    window.addEventListener("online", function () {
      void pollSignalStatus();
      restartCopyArchiveRetry();
      fetchCopyHistory().catch(function () {});
    });
    window.addEventListener("storage", function (event) {
      if (event.key && event.key.startsWith(`${copyOutboxStoragePrefix}${encodeURIComponent(state.roomId)}:`)) {
        reloadCopyOutbox();
        renderCopyHistory();
        restartCopyArchiveRetry();
      }
    });
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "visible") {
        void pollSignalStatus();
        restartCopyArchiveRetry();
        fetchCopyHistory().catch(function () {});
      }
    });
    window.setInterval(function () {
      if (document.visibilityState === "visible") {
        fetchCopyHistory().catch(function () {});
      }
    }, 15000);
    if (state.role === "mobile") {
      window.setInterval(function () {
        if (document.visibilityState === "visible" || state.signalActionPending || state.signalQueuedPaste) void pollSignalStatus();
      }, 2000);
    }
  }

  inferRoleFromPath();
  setRoleView();
  ensureClientId();
  roomInput.value = state.roomId;
  loadLocalPreferences();
  loadClipboardStorage();
  renderCopyHistory();
  renderCaptureMode("clipboard");
  bindEvents();
  fetchState().catch(function () { connectionStatus.textContent = "未连接；本机复制仍可使用"; });
  fetchServerInfo().catch(function () {});
  claimCurrentRole();
  // Saved copies are independent of receiver/stream support. Start their read
  // before the optional stream so browser policy cannot block PC history.
  fetchCopyHistory().catch(function () {});
  connectStream();
  restartCopyArchiveRetry();
  void pollSignalStatus();
})();
