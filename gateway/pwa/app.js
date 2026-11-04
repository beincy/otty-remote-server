const STORAGE_KEY = "otty_pwa_state_v1";

const defaultState = {
  token: "",
  instanceId: "",
  activeTabId: "",
  inputMode: "stream", // "stream" (实时流式输入, 默认) | "queue" (排队整发) | "now" (立即插话)
  fontSize: 12,
  wrap: true,
  keypadOpen: false,
  recentCwds: [],
  tabs: [],
  screens: {},
};

function loadState() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    const parsed = raw ? { ...defaultState, ...JSON.parse(raw) } : { ...defaultState };
    if (!["stream", "queue", "now"].includes(parsed.inputMode)) {
      parsed.inputMode = "stream";
    }
    parsed.keypadOpen = false;
    return parsed;
  } catch {
    return { ...defaultState };
  }
}

const state = loadState();
let ws = null;
let wsReconnectTimer = null;
let pollTimer = null;
let toastTimer = null;
let stickToBottom = true;
let loadingHistory = false;

// In-memory per-tab input & synced state so page reloads never re-send stale text
const inputByTab = {};
const syncedByTab = {};
let isComposing = false;
let streamTimer = null;
let streamInFlight = false;
let pendingFlushSubmit = false;

// 语音输入：识别结果作为“草稿”填入输入框，草稿存在期间暂停流式同步（与流式输入互斥），
// 用户修改后点发送才一次性写入终端。
const voiceDraftByTab = {};
let voiceTranscribing = false;

const el = {
  btnOpenDrawer: document.getElementById("btn-open-drawer"),
  headerCenter: document.getElementById("header-center"),
  connDot: document.getElementById("conn-dot"),
  connDotSidebar: document.getElementById("conn-dot-sidebar"),
  connLabel: document.getElementById("conn-label"),
  tabCountBadge: document.getElementById("tab-count-badge"),
  sidebarTabCount: document.getElementById("sidebar-tab-count"),
  activeStateDot: document.getElementById("active-state-dot"),
  activeTitle: document.getElementById("active-title"),
  activeSubtitle: document.getElementById("active-subtitle"),
  btnHeaderWrap: document.getElementById("btn-header-wrap"),
  btnRefresh: document.getElementById("btn-refresh"),
  btnSettings: document.getElementById("btn-settings"),
  viewport: document.getElementById("screen-viewport"),
  historyBar: document.getElementById("history-bar"),
  btnLoadMore: document.getElementById("btn-load-more"),
  emptyState: document.getElementById("empty-state"),
  emptyDesc: document.getElementById("empty-desc"),
  btnEmptySetup: document.getElementById("btn-empty-setup"),
  terminalCard: document.getElementById("terminal-card"),
  screenLines: document.getElementById("screen-lines"),
  btnScrollBottom: document.getElementById("btn-scroll-bottom"),
  keypadDrawer: document.getElementById("keypad-drawer"),
  awaitingBanner: document.getElementById("awaiting-banner"),
  keyBar: document.getElementById("key-bar"),
  btnToggleKeys: document.getElementById("btn-toggle-keys"),
  composerPill: document.getElementById("composer-pill"),
  voiceOverlay: document.getElementById("voice-overlay"),
  voiceTimer: document.getElementById("voice-timer"),
  voiceHint: document.getElementById("voice-hint"),
  btnDelivery: document.getElementById("btn-delivery"),
  msgInput: document.getElementById("msg-input"),
  btnSend: document.getElementById("btn-send"),
  toast: document.getElementById("toast"),
  drawerTabs: document.getElementById("drawer-tabs"),
  tabListCards: document.getElementById("tab-list-cards"),
  btnNewTab: document.getElementById("btn-new-tab"),
  modalNewTab: document.getElementById("modal-new-tab"),
  inputCwd: document.getElementById("input-cwd"),
  recentCwds: document.getElementById("recent-cwds"),
  inputCommand: document.getElementById("input-command"),
  inputTitle: document.getElementById("input-title"),
  btnCreateTab: document.getElementById("btn-create-tab"),
  modalSettings: document.getElementById("modal-settings"),
  btnWrap: document.getElementById("btn-wrap"),
  btnCloseTab: document.getElementById("btn-close-tab"),
  fontsizeGroup: document.getElementById("fontsize-group"),
  inputToken: document.getElementById("input-token"),
  inputInstance: document.getElementById("input-instance"),
  btnSaveSettings: document.getElementById("input-save") || document.getElementById("btn-save-settings"),
};

function saveState() {
  try {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        token: state.token,
        instanceId: state.instanceId,
        activeTabId: state.activeTabId,
        inputMode: state.inputMode,
        fontSize: state.fontSize,
        wrap: state.wrap,
        keypadOpen: state.keypadOpen,
        recentCwds: state.recentCwds.slice(0, 6),
        tabs: state.tabs,
        screens: state.screens,
      })
    );
  } catch {
    // Ignore storage quota errors
  }
}

function importHashCredentials() {
  if (!location.hash || location.hash.length < 2) return;
  const params = new URLSearchParams(location.hash.slice(1));
  const token = (params.get("token") || "").trim();
  const instance = (params.get("instance") || "").trim();
  if (token) {
    state.token = token;
    if (instance) state.instanceId = instance;
    saveState();
    history.replaceState(null, "", location.pathname + location.search);
  }
}

function showToast(message, isError = false) {
  clearTimeout(toastTimer);
  el.toast.textContent = message;
  el.toast.classList.toggle("error", isError);
  el.toast.classList.remove("hidden");
  toastTimer = setTimeout(() => el.toast.classList.add("hidden"), 2200);
}

function setConnectionStatus(mode, label) {
  el.connDot.className = `conn-dot ${mode}`;
  if (el.connDotSidebar) el.connDotSidebar.className = `conn-dot ${mode}`;
  el.connLabel.textContent = label;
}

function apiBasePath() {
  const id = state.instanceId.trim();
  // 相对路径：页面在 /、/otty 或任何反代前缀下都能正确解析
  return id ? `i/${encodeURIComponent(id)}/api/v1` : "api/v1";
}

async function apiRequest(path, { method = "GET", params = null, body = null } = {}) {
  if (!state.token) {
    throw new Error("请先配置实例 Token");
  }
  const url = new URL(apiBasePath() + path, location.href);
  if (params) {
    Object.entries(params).forEach(([k, v]) => {
      if (v !== null && v !== undefined && v !== "") url.searchParams.set(k, v);
    });
  }
  const headers = { Authorization: `Bearer ${state.token}` };
  if (body !== null) headers["Content-Type"] = "application/json";
  const res = await fetch(url, {
    method,
    headers,
    body: body !== null ? JSON.stringify(body) : null,
  });
  let payload = null;
  try {
    payload = await res.json();
  } catch {
    payload = { detail: await res.text() };
  }
  if (!res.ok) {
    throw new Error(payload?.detail || `HTTP ${res.status}`);
  }
  return payload;
}

function currentTab() {
  return state.tabs.find((t) => t.id === state.activeTabId) || null;
}

function shouldStreamTab(tab) {
  if (!tab || !tab.writable || tab.protected || tab.agent_state === "awaiting") {
    return false;
  }
  return state.inputMode === "stream";
}

const STATE_TEXT = {
  idle: "🟢 空闲",
  processing: "🟡 执行中",
  awaiting: "🟠 待确认",
  starting: "🔵 启动中",
  unknown: "⚪ 未知",
};

function tabDisplayName(tab) {
  return tab.title || tab.agent || (tab.cwd ? tab.cwd.split("/").pop() : tab.id);
}

function formatMobileTerminalLines(lines) {
  if (window.innerWidth >= 768) {
    return lines.join("\n");
  }
  return lines
    .map((line) => line.replace(/[─━═]{28,}/g, "────────────────────────"))
    .join("\n");
}

function applyDisplayPrefs() {
  document.documentElement.style.setProperty("--screen-font-size", `${state.fontSize}px`);
  el.screenLines.classList.toggle("wrap", state.wrap);
  el.screenLines.classList.toggle("nowrap", !state.wrap);
  el.btnWrap.classList.toggle("active", state.wrap);
  el.btnWrap.textContent = state.wrap ? "自动折行：开" : "横向滚动：开";
  if (el.btnHeaderWrap) {
    el.btnHeaderWrap.classList.toggle("active", state.wrap);
    el.btnHeaderWrap.textContent = state.wrap ? "自动折行" : "横向滚动";
  }

  const modeLabels = {
    stream: "⚡ 流式",
    queue: "📦 排队",
    now: "🔥 插话",
  };
  el.btnDelivery.textContent = modeLabels[state.inputMode] || "⚡ 流式";
  el.btnDelivery.classList.toggle("stream", state.inputMode === "stream");
  el.btnDelivery.classList.toggle("now", state.inputMode === "now");

  el.fontsizeGroup.querySelectorAll(".seg-btn").forEach((btn) => {
    btn.classList.toggle("active", Number(btn.dataset.size) === state.fontSize);
  });
}

function applyScreenPage(tabId, page) {
  state.screens[tabId] = {
    lines: page.lines || [],
    nextBefore: page.nextBefore || null,
    hasMore: Boolean(page.hasMore),
    capturedAt: page.captured_at,
  };
  const tab = state.tabs.find((t) => t.id === tabId);
  if (tab && page.agent_state !== undefined) {
    tab.agent_state = page.agent_state;
    if (page.agent) tab.agent = page.agent;
    renderTabs();
  }
  if (tabId === state.activeTabId) {
    renderScreen();
  }
  saveState();
}

function computeStreamDiff(synced, target) {
  const a = Array.from(synced);
  const b = Array.from(target);
  let common = 0;
  const minLen = Math.min(a.length, b.length);
  while (common < minLen && a[common] === b[common]) {
    common++;
  }
  return {
    backspaces: a.length - common,
    text: b.slice(common).join(""),
  };
}

function scheduleStreamSync(immediate = false) {
  clearTimeout(streamTimer);
  const active = currentTab();
  if (!shouldStreamTab(active)) return;
  if (voiceDraftByTab[active.id]) return;
  if (isComposing && !immediate) return;
  streamTimer = setTimeout(
    () => {
      pumpStreamQueue();
    },
    immediate ? 0 : 50
  );
}

async function pumpStreamQueue() {
  if (streamInFlight) return;
  const active = currentTab();
  if (!active || !active.writable || active.protected) return;
  const tabId = active.id;

  streamInFlight = true;
  try {
    while (true) {
      const currentSynced = syncedByTab[tabId] || "";
      const target = tabId === state.activeTabId ? el.msgInput.value : inputByTab[tabId] || "";
      const { backspaces, text } = computeStreamDiff(currentSynced, target);
      const shouldSubmit = pendingFlushSubmit && tabId === state.activeTabId;

      // 语音草稿未发送前不做实时同步
      if (voiceDraftByTab[tabId] && !shouldSubmit) {
        break;
      }

      if (backspaces === 0 && !text && !shouldSubmit) {
        break;
      }

      pendingFlushSubmit = false;
      stickToBottom = true;
      const page = await apiRequest(`/tabs/${encodeURIComponent(tabId)}/type`, {
        method: "POST",
        body: {
          backspaces,
          text,
          submit: shouldSubmit,
          settle_ms: 85,
          limit: 60,
        },
      });

      if (shouldSubmit) {
        syncedByTab[tabId] = "";
        inputByTab[tabId] = "";
        delete voiceDraftByTab[tabId];
        if (tabId === state.activeTabId) {
          el.msgInput.value = "";
          autoResizeInput();
        }
        applyScreenPage(tabId, page);
        setTimeout(() => fetchLatestScreen(tabId), 350);
        setTimeout(() => fetchTabs(), 900);
        break;
      } else {
        syncedByTab[tabId] = target;
        applyScreenPage(tabId, page);
      }

      if (isComposing) break;
    }
  } catch (err) {
    pendingFlushSubmit = false;
    showToast(err.message, true);
  } finally {
    streamInFlight = false;
  }
}

async function closeTabById(tab) {
  if (!tab || tab.protected) return;
  if (!confirm(`确定关闭会话 "${tabDisplayName(tab)}" 吗？`)) return;
  try {
    await apiRequest(`/tabs/${encodeURIComponent(tab.id)}`, { method: "DELETE" });
    delete state.screens[tab.id];
    delete inputByTab[tab.id];
    delete syncedByTab[tab.id];
    if (state.activeTabId === tab.id) {
      state.activeTabId = "";
    }
    el.modalSettings.classList.add("hidden");
    await fetchTabs();
    showToast("已关闭会话");
  } catch (err) {
    showToast(err.message, true);
  }
}

function renderTabs() {
  const countStr = String(state.tabs.length || 0);
  el.tabCountBadge.textContent = countStr;
  if (el.sidebarTabCount) el.sidebarTabCount.textContent = countStr;

  if (state.tabs.length && !state.tabs.some((t) => t.id === state.activeTabId)) {
    const preferred = state.tabs.find((t) => t.agent && !t.protected) || state.tabs[0];
    state.activeTabId = preferred.id;
  }

  el.tabListCards.innerHTML = "";
  state.tabs.forEach((tab, idx) => {
    const card = document.createElement("div");
    card.role = "button";
    card.tabIndex = 0;
    card.className = `task-card${tab.id === state.activeTabId ? " active" : ""}`;

    const main = document.createElement("div");
    main.className = "task-card-main";

    const title = document.createElement("div");
    title.className = "task-card-title";
    title.textContent = `${idx + 1}. ${tabDisplayName(tab)}${tab.protected ? " 🔒" : ""}`;

    const meta = document.createElement("div");
    meta.className = "task-card-meta";
    const shortCwd = tab.cwd ? tab.cwd.split("/").slice(-2).join("/") : "";
    meta.textContent = [tab.agent || "Shell", shortCwd].filter(Boolean).join(" · ");

    main.append(title, meta);

    const right = document.createElement("div");
    right.className = "task-card-right";

    const statusPill = document.createElement("span");
    statusPill.className = "task-status-pill";
    statusPill.textContent = STATE_TEXT[tab.agent_state] || (tab.protected ? "宿主" : "终端");
    right.appendChild(statusPill);

    if (!tab.protected) {
      const closeBtn = document.createElement("button");
      closeBtn.type = "button";
      closeBtn.className = "task-close-btn";
      closeBtn.title = "关闭此会话";
      closeBtn.textContent = "✕";
      closeBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        closeTabById(tab);
      });
      right.appendChild(closeBtn);
    }

    card.append(main, right);
    card.addEventListener("click", () => {
      el.drawerTabs.classList.remove("drawer-open");
      selectTab(tab.id);
    });
    el.tabListCards.appendChild(card);
  });

  const active = currentTab();
  if (!active) {
    el.activeStateDot.className = "state-dot";
    el.activeTitle.textContent = state.token ? "暂无会话" : "未连接";
    el.activeSubtitle.textContent = state.token ? "点击新建会话开始工作" : "点击右上角 ⚙ 配置 Token";
    el.keypadDrawer.classList.add("hidden");
    return;
  }

  el.activeStateDot.className = `state-dot ${active.agent_state || ""}`;
  el.activeTitle.textContent = tabDisplayName(active);
  const shortDir = window.innerWidth >= 768
    ? active.cwd || ""
    : active.cwd
      ? active.cwd.split("/").pop()
      : "";
  const stateLabel = STATE_TEXT[active.agent_state] || (active.protected ? "宿主保护" : "终端");
  el.activeSubtitle.textContent = [active.agent || "Shell", shortDir, stateLabel]
    .filter(Boolean)
    .join(" · ");

  const isAwaiting = active.agent_state === "awaiting";
  el.awaitingBanner.classList.toggle("hidden", !isAwaiting);
  const showKeypad = isAwaiting || state.keypadOpen;
  el.keypadDrawer.classList.toggle("hidden", !showKeypad);
  el.btnToggleKeys.classList.toggle("active", showKeypad);

  const canInput = active.writable && !isAwaiting && !voiceTranscribing;
  el.msgInput.disabled = !canInput;
  el.btnSend.disabled = !canInput;
  if (voiceTranscribing) {
    el.msgInput.placeholder = "⏳ 正在识别...";
  } else if (active.protected) {
    el.msgInput.placeholder = "宿主受保护";
  } else if (!active.writable) {
    el.msgInput.placeholder = "只读";
  } else if (isAwaiting) {
    el.msgInput.placeholder = "请点上方按键回应";
  } else {
    el.msgInput.placeholder = "按住说话";
  }
}

function isNearBottom() {
  const { scrollTop, scrollHeight, clientHeight } = el.viewport;
  return scrollHeight - scrollTop - clientHeight < 42;
}

function scrollToBottom() {
  el.viewport.scrollTop = el.viewport.scrollHeight;
  stickToBottom = true;
  el.btnScrollBottom.classList.add("hidden");
}

function renderScreen() {
  if (!state.token) {
    el.emptyState.classList.remove("hidden");
    el.terminalCard.classList.add("hidden");
    el.historyBar.classList.add("hidden");
    return;
  }
  const active = currentTab();
  if (!active) {
    el.emptyState.classList.remove("hidden");
    el.emptyDesc.textContent = "当前没有打开的会话，点击左上角菜单新建一个。";
    el.terminalCard.classList.add("hidden");
    el.historyBar.classList.add("hidden");
    return;
  }

  el.emptyState.classList.add("hidden");
  el.terminalCard.classList.remove("hidden");
  const entry = state.screens[active.id];
  const lines = entry?.lines || [];
  el.screenLines.textContent = lines.length
    ? formatMobileTerminalLines(lines)
    : "（正在读取终端画面...）";
  el.historyBar.classList.toggle("hidden", !entry?.hasMore);
  if (el.btnLoadMore) {
    el.btnLoadMore.textContent = loadingHistory ? "⏳ 正在加载更早记录..." : "↑ 上滑或点击加载更早记录";
  }
  if (stickToBottom) {
    scrollToBottom();
  }
  // PC 大屏下若首次加载的 120 行内容高度不足以撑出滚动条，且仍有历史记录，自动补载一页
  if (entry?.hasMore && !loadingHistory) {
    requestAnimationFrame(() => {
      if (
        entry?.hasMore &&
        !loadingHistory &&
        el.viewport.clientHeight > 0 &&
        el.viewport.scrollHeight <= el.viewport.clientHeight + 80
      ) {
        loadMoreHistory();
      }
    });
  }
}

async function selectTab(tabId) {
  const prev = state.activeTabId;
  if (prev) {
    inputByTab[prev] = el.msgInput.value;
  }
  state.activeTabId = tabId;
  el.msgInput.value = inputByTab[tabId] || "";
  autoResizeInput();
  stickToBottom = true;
  renderTabs();
  renderScreen();
  saveState();
  await fetchLatestScreen(tabId);
}

async function fetchTabs() {
  if (!state.token) return;
  try {
    const data = await apiRequest("/tabs");
    state.tabs = data.tabs || [];
    if (el.connDot.classList.contains("offline")) {
      setConnectionStatus("http", "在线");
    }
    state.tabs
      .map((t) => t.cwd)
      .filter(Boolean)
      .forEach((c) => {
        if (!state.recentCwds.includes(c)) state.recentCwds.unshift(c);
      });
    renderTabs();
    renderScreen();
    saveState();
    if (state.activeTabId) {
      await fetchLatestScreen(state.activeTabId);
    }
  } catch (err) {
    setConnectionStatus("offline", "连接失败");
    showToast(err.message, true);
  }
}

async function fetchLatestScreen(tabId = state.activeTabId) {
  if (!state.token || !tabId) return;
  try {
    const page = await apiRequest(`/tabs/${encodeURIComponent(tabId)}/screen`, {
      params: { limit: 120 },
    });
    applyScreenPage(tabId, page);
  } catch (err) {
    showToast(err.message, true);
  }
}

async function loadMoreHistory() {
  if (loadingHistory) return;
  const active = currentTab();
  if (!active) return;
  const entry = state.screens[active.id];
  if (!entry?.hasMore || !entry?.nextBefore) return;
  loadingHistory = true;
  if (el.btnLoadMore) {
    el.btnLoadMore.textContent = "⏳ 正在加载更早记录...";
  }
  const prevHeight = el.viewport.scrollHeight;
  const prevTop = el.viewport.scrollTop;
  try {
    const page = await apiRequest(`/tabs/${encodeURIComponent(active.id)}/screen`, {
      params: { limit: 120, before: entry.nextBefore },
    });
    entry.lines = [...(page.lines || []), ...entry.lines];
    entry.nextBefore = page.nextBefore || null;
    entry.hasMore = Boolean(page.hasMore);
    stickToBottom = false;
    renderScreen();
    const applyScrollOffset = () => {
      const addedHeight = el.viewport.scrollHeight - prevHeight;
      if (addedHeight > 0) {
        el.viewport.scrollTop = addedHeight + Math.max(prevTop, 0);
      }
    };
    applyScrollOffset();
    requestAnimationFrame(applyScrollOffset);
  } catch (err) {
    showToast(err.message, true);
    await fetchLatestScreen(active.id);
  } finally {
    loadingHistory = false;
    if (el.btnLoadMore) {
      el.btnLoadMore.textContent = "↑ 上滑或点击加载更早记录";
    }
  }
}

async function sendMessage() {
  const active = currentTab();
  if (!active) return;
  const text = el.msgInput.value;
  const synced = syncedByTab[active.id] || "";

  if (shouldStreamTab(active) || synced.length > 0) {
    if (!text && !synced) return;
    pendingFlushSubmit = true;
    await pumpStreamQueue();
    return;
  }

  if (!text.trim()) return;
  el.btnSend.disabled = true;
  try {
    const delivery = state.inputMode === "now" ? "now" : "queue";
    await apiRequest(`/tabs/${encodeURIComponent(active.id)}/messages`, {
      method: "POST",
      body: { text, delivery },
    });
    el.msgInput.value = "";
    inputByTab[active.id] = "";
    syncedByTab[active.id] = "";
    delete voiceDraftByTab[active.id];
    autoResizeInput();
    stickToBottom = true;
    saveState();
    showToast("已发送");
    setTimeout(() => fetchLatestScreen(active.id), 300);
    setTimeout(() => fetchTabs(), 900);
  } catch (err) {
    showToast(err.message, true);
    await fetchTabs();
  } finally {
    renderTabs();
  }
}

async function sendKey(keyName) {
  const active = currentTab();
  if (!active) return;
  if (!active.writable) {
    showToast("当前会话不可写入", true);
    return;
  }
  if (navigator.vibrate) navigator.vibrate(10);
  try {
    await apiRequest(`/tabs/${encodeURIComponent(active.id)}/keys`, {
      method: "POST",
      body: { keys: [keyName] },
    });
    if (["Enter", "Ctrl+C"].includes(keyName)) {
      syncedByTab[active.id] = "";
      inputByTab[active.id] = "";
      delete voiceDraftByTab[active.id];
      el.msgInput.value = "";
      autoResizeInput();
    } else if (keyName === "Backspace") {
      const chars = Array.from(el.msgInput.value);
      chars.pop();
      const next = chars.join("");
      el.msgInput.value = next;
      inputByTab[active.id] = next;
      syncedByTab[active.id] = next;
      autoResizeInput();
    }
    stickToBottom = true;
    setTimeout(() => fetchLatestScreen(active.id), 100);
    setTimeout(() => fetchTabs(), 600);
  } catch (err) {
    showToast(err.message, true);
  }
}

function connectWebSocket() {
  clearTimeout(wsReconnectTimer);
  if (ws) {
    try {
      ws.onclose = null;
      ws.close();
    } catch {
      // Ignore
    }
    ws = null;
  }
  if (!state.token) {
    setConnectionStatus("offline", "未配置 Token");
    return;
  }
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const id = state.instanceId.trim();
  // 相对路径解析，配合 /、/otty 或反代前缀均可
  const wsPath = id ? `i/${encodeURIComponent(id)}/ws` : "ws/client";
  const wsUrl = new URL(wsPath, location.href);
  wsUrl.protocol = proto;
  wsUrl.searchParams.set("token", state.token);
  const url = wsUrl.toString();

  try {
    ws = new WebSocket(url);
  } catch {
    return;
  }

  ws.onopen = () => {
    setConnectionStatus("online", "实时已连接");
  };

  ws.onmessage = (event) => {
    try {
      const msg = JSON.parse(event.data);
      if (msg.type === "connected" && Array.isArray(msg.tabs)) {
        state.tabs = msg.tabs;
        renderTabs();
        renderScreen();
      } else if (msg.type === "event" && msg.event === "tabs.updated" && Array.isArray(msg.tabs)) {
        const prevActive = currentTab();
        state.tabs = msg.tabs;
        const nextActive = currentTab();
        renderTabs();
        if (
          nextActive &&
          (!prevActive ||
            prevActive.agent_state !== nextActive.agent_state ||
            prevActive.process !== nextActive.process)
        ) {
          fetchLatestScreen(nextActive.id);
        }
      } else if (msg.type === "event" && msg.event === "agent.offline") {
        setConnectionStatus("offline", "本地电脑离线");
        showToast("本地 Otty 已断开", true);
      } else if (msg.type === "event" && msg.event === "agent.online") {
        setConnectionStatus("online", "实时已连接");
        fetchTabs();
      }
    } catch {
      // Ignore malformed frame
    }
  };

  ws.onclose = () => {
    ws = null;
    if (state.token) {
      wsReconnectTimer = setTimeout(connectWebSocket, 3500);
    }
  };
}

function startBackgroundLoop() {
  clearInterval(pollTimer);
  pollTimer = setInterval(() => {
    if (document.hidden || !state.token || streamInFlight || loadingHistory || !stickToBottom) return;
    const active = currentTab();
    if (!active) return;
    if (
      active.agent_state === "processing" ||
      active.agent_state === "starting" ||
      !ws ||
      ws.readyState !== WebSocket.OPEN
    ) {
      fetchLatestScreen(active.id);
    }
  }, 2000);
}

function autoResizeInput() {
  if (!el.msgInput.value) {
    el.msgInput.style.height = "30px";
    return;
  }
  el.msgInput.style.height = "30px";
  el.msgInput.style.height = `${Math.min(el.msgInput.scrollHeight, 110)}px`;
}

function openDrawerTabs() {
  if (window.innerWidth >= 768) return;
  renderTabs();
  el.drawerTabs.classList.add("drawer-open");
}

function openSettingsModal() {
  el.inputToken.value = state.token;
  el.inputInstance.value = state.instanceId;
  applyDisplayPrefs();
  el.modalSettings.classList.remove("hidden");
}

function openNewTabModal() {
  el.drawerTabs.classList.remove("drawer-open");
  const active = currentTab();
  if (!el.inputCwd.value && (active?.cwd || state.recentCwds[0])) {
    el.inputCwd.value = active?.cwd || state.recentCwds[0] || "";
  }
  el.recentCwds.innerHTML = "";
  state.recentCwds.slice(0, 5).forEach((cwd) => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "chip-btn";
    chip.textContent = cwd.split("/").slice(-2).join("/") || cwd;
    chip.addEventListener("click", () => {
      el.inputCwd.value = cwd;
    });
    el.recentCwds.appendChild(chip);
  });
  el.modalNewTab.classList.remove("hidden");
}

// Event bindings
el.btnOpenDrawer.addEventListener("click", openDrawerTabs);
el.headerCenter.addEventListener("click", openDrawerTabs);
el.btnSettings.addEventListener("click", openSettingsModal);
el.btnEmptySetup.addEventListener("click", openSettingsModal);
el.btnNewTab.addEventListener("click", openNewTabModal);

el.drawerTabs.addEventListener("click", (e) => {
  if (e.target === el.drawerTabs) el.drawerTabs.classList.remove("drawer-open");
});

[el.modalNewTab, el.modalSettings].forEach((backdrop) => {
  backdrop.addEventListener("click", (e) => {
    if (e.target === backdrop) backdrop.classList.add("hidden");
  });
});

document.querySelectorAll("[data-close]").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.getElementById(btn.dataset.close)?.classList.add("hidden");
  });
});

el.btnToggleKeys.addEventListener("click", () => {
  state.keypadOpen = !state.keypadOpen;
  renderTabs();
  saveState();
});

el.fontsizeGroup.querySelectorAll(".seg-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    state.fontSize = Number(btn.dataset.size) || 12;
    applyDisplayPrefs();
    saveState();
  });
});

el.btnSaveSettings.addEventListener("click", async () => {
  state.token = el.inputToken.value.trim();
  state.instanceId = el.inputInstance.value.trim();
  saveState();
  el.modalSettings.classList.add("hidden");
  connectWebSocket();
  await fetchTabs();
});

document.querySelectorAll("[data-cmd]").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("[data-cmd]").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    el.inputCommand.value = btn.dataset.cmd;
  });
});

el.btnCreateTab.addEventListener("click", async () => {
  const cwd = el.inputCwd.value.trim();
  const command = el.inputCommand.value.trim();
  const title = el.inputTitle.value.trim();
  if (!cwd) {
    showToast("请填写宿主机目录路径 cwd", true);
    return;
  }
  el.btnCreateTab.disabled = true;
  try {
    const body = { cwd };
    if (command) body.command = command;
    if (title) body.title = title;
    const created = await apiRequest("/tabs", { method: "POST", body });
    if (!state.recentCwds.includes(cwd)) state.recentCwds.unshift(cwd);
    el.modalNewTab.classList.add("hidden");
    el.inputTitle.value = "";
    state.activeTabId = created.id;
    await fetchTabs();
    showToast("已启动新会话");
  } catch (err) {
    showToast(err.message, true);
  } finally {
    el.btnCreateTab.disabled = false;
  }
});

function toggleWrap() {
  state.wrap = !state.wrap;
  applyDisplayPrefs();
  saveState();
}

el.btnWrap.addEventListener("click", toggleWrap);
if (el.btnHeaderWrap) {
  el.btnHeaderWrap.addEventListener("click", toggleWrap);
}

el.btnRefresh.addEventListener("click", () => {
  stickToBottom = true;
  fetchTabs();
  showToast("已刷新");
});

el.btnCloseTab.addEventListener("click", () => {
  closeTabById(currentTab());
});

el.btnLoadMore.addEventListener("click", loadMoreHistory);

let lastViewportScrollTop = 0;
let touchPullStartY = null;

el.viewport.addEventListener("scroll", () => {
  const currentTop = el.viewport.scrollTop;
  const scrollingUp = currentTop < lastViewportScrollTop;
  lastViewportScrollTop = currentTop;

  const wasStuck = stickToBottom;
  stickToBottom = isNearBottom();
  el.btnScrollBottom.classList.toggle("hidden", stickToBottom);
  if (stickToBottom && !wasStuck && !scrollingUp && state.activeTabId) {
    fetchLatestScreen(state.activeTabId);
    return;
  }
  if (currentTop <= 140 && (scrollingUp || !stickToBottom)) {
    loadMoreHistory();
  }
});

// 手机端：在顶部继续下拉（手指往下滑）或上滑到顶部时自动加载历史记录
el.viewport.addEventListener(
  "touchstart",
  (e) => {
    if (e.touches.length === 1) {
      touchPullStartY = e.touches[0].clientY;
    }
  },
  { passive: true }
);

el.viewport.addEventListener(
  "touchmove",
  (e) => {
    if (touchPullStartY === null || e.touches.length !== 1) return;
    const pullDownDelta = e.touches[0].clientY - touchPullStartY;
    if (pullDownDelta > 16 && el.viewport.scrollTop <= 140) {
      touchPullStartY = e.touches[0].clientY;
      loadMoreHistory();
    }
  },
  { passive: true }
);

el.viewport.addEventListener(
  "touchend",
  () => {
    touchPullStartY = null;
  },
  { passive: true }
);

el.viewport.addEventListener(
  "wheel",
  (e) => {
    if (e.deltaY < 0 && el.viewport.scrollTop <= 140) {
      loadMoreHistory();
    }
  },
  { passive: true }
);

el.btnScrollBottom.addEventListener("click", () => {
  scrollToBottom();
  if (state.activeTabId) fetchLatestScreen(state.activeTabId);
});

el.keyBar.querySelectorAll("[data-key]").forEach((btn) => {
  btn.addEventListener("click", () => sendKey(btn.dataset.key));
});

el.btnDelivery.addEventListener("click", () => {
  const order = ["stream", "queue", "now"];
  const nextIdx = (order.indexOf(state.inputMode) + 1) % order.length;
  state.inputMode = order[nextIdx];
  applyDisplayPrefs();
  renderTabs();
  saveState();
  const tips = {
    stream: "⚡ 流式输入：边打字边同步到终端",
    queue: "📦 排队模式：写完点发送再放入队列",
    now: "🔥 插话模式：点发送立即打断/引导 Agent",
  };
  showToast(tips[state.inputMode]);
});

el.msgInput.addEventListener("compositionstart", () => {
  isComposing = true;
});

el.msgInput.addEventListener("compositionend", () => {
  isComposing = false;
  if (state.activeTabId) {
    inputByTab[state.activeTabId] = el.msgInput.value;
  }
  scheduleStreamSync(true);
});

el.msgInput.addEventListener("input", () => {
  autoResizeInput();
  if (state.activeTabId) {
    inputByTab[state.activeTabId] = el.msgInput.value;
    // 清空语音草稿后恢复实时流式同步
    if (!el.msgInput.value) delete voiceDraftByTab[state.activeTabId];
  }
  scheduleStreamSync(false);
});

el.msgInput.addEventListener("keydown", (e) => {
  if (isComposing) return;
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  } else if (
    ["ArrowUp", "ArrowDown", "Tab"].includes(e.key) &&
    el.msgInput.value.startsWith("/")
  ) {
    e.preventDefault();
    const keyMap = { ArrowUp: "Up", ArrowDown: "Down", Tab: "Tab" };
    sendKey(keyMap[e.key]);
  }
});

el.btnSend.addEventListener("click", sendMessage);

// ---------------------------------------------------------------------------
// 语音输入：直接长按输入框 → 自动弹出语音浮层开始录音 → 松手识别填入输入框
// 录音在浏览器内编码为 16kHz 单声道 WAV，经鉴权的 /api/v1/asr 接口识别
// ---------------------------------------------------------------------------
const VOICE_SAMPLE_RATE = 16000;
const VOICE_MAX_MS = 60000;
const VOICE_MIN_MS = 500;
const VOICE_CANCEL_DISTANCE = 60;
const LONG_PRESS_MS = 300;
const MOVE_SLOP_PX = 14;

let voiceRec = null;
let voicePress = null;
let holdCandidate = null;

function updateVoiceUi(phase) {
  el.composerPill.classList.remove("recording", "cancel");
  el.voiceOverlay.classList.remove("cancel");
  if (phase === "recording") {
    el.composerPill.classList.add("recording");
    el.voiceHint.textContent = "松开 识别，上滑 取消";
    el.voiceOverlay.classList.remove("hidden");
  } else if (phase === "cancel") {
    el.composerPill.classList.add("cancel");
    el.voiceHint.textContent = "松开手指，取消发送";
    el.voiceOverlay.classList.add("cancel");
    el.voiceOverlay.classList.remove("hidden");
  } else {
    el.voiceOverlay.classList.add("hidden");
  }
}

function formatVoiceTime(ms) {
  const s = Math.floor(ms / 1000);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

async function startVoiceRecording() {
  if (!navigator.mediaDevices?.getUserMedia) {
    showToast("当前环境不支持录音（需 HTTPS 访问）", true);
    return null;
  }
  const AudioCtx = window.AudioContext || window.webkitAudioContext;
  if (!AudioCtx) {
    showToast("当前浏览器不支持录音", true);
    return null;
  }
  const ctx = new AudioCtx();
  ctx.resume().catch(() => {});
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
    });
  } catch {
    ctx.close().catch(() => {});
    showToast("无法使用麦克风，请在浏览器中允许录音权限", true);
    return null;
  }
  const source = ctx.createMediaStreamSource(stream);
  const processor = ctx.createScriptProcessor(4096, 1, 1);
  const rec = {
    ctx,
    stream,
    source,
    processor,
    chunks: [],
    inputRate: ctx.sampleRate,
    startedAt: Date.now(),
    timer: null,
  };
  processor.onaudioprocess = (e) => {
    rec.chunks.push(new Float32Array(e.inputBuffer.getChannelData(0)));
  };
  source.connect(processor);
  processor.connect(ctx.destination);
  return rec;
}

function stopVoiceRecording(rec) {
  clearInterval(rec.timer);
  try {
    rec.processor.disconnect();
    rec.source.disconnect();
  } catch {
    // Ignore
  }
  rec.stream.getTracks().forEach((t) => t.stop());
  rec.ctx.close().catch(() => {});
}

function downsampleBuffer(chunks, inputRate, outputRate) {
  const total = chunks.reduce((n, c) => n + c.length, 0);
  const merged = new Float32Array(total);
  let offset = 0;
  for (const c of chunks) {
    merged.set(c, offset);
    offset += c.length;
  }
  if (inputRate === outputRate) return merged;
  const ratio = inputRate / outputRate;
  const outLen = Math.floor(total / ratio);
  const out = new Float32Array(outLen);
  for (let i = 0; i < outLen; i++) {
    const start = Math.floor(i * ratio);
    const end = Math.min(Math.floor((i + 1) * ratio), total);
    let sum = 0;
    for (let j = start; j < end; j++) sum += merged[j];
    out[i] = end > start ? sum / (end - start) : merged[start] || 0;
  }
  return out;
}

function encodeWav(samples, sampleRate) {
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);
  const writeStr = (pos, s) => {
    for (let i = 0; i < s.length; i++) view.setUint8(pos + i, s.charCodeAt(i));
  };
  writeStr(0, "RIFF");
  view.setUint32(4, 36 + samples.length * 2, true);
  writeStr(8, "WAVE");
  writeStr(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true); // PCM
  view.setUint16(22, 1, true); // mono
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeStr(36, "data");
  view.setUint32(40, samples.length * 2, true);
  let pos = 44;
  for (let i = 0; i < samples.length; i++, pos += 2) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(pos, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return new Uint8Array(buffer);
}

function bytesToBase64(bytes) {
  let binary = "";
  const step = 0x8000;
  for (let i = 0; i < bytes.length; i += step) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + step));
  }
  return btoa(binary);
}

function applyVoiceResult(tabId, text) {
  const base = tabId === state.activeTabId ? el.msgInput.value : inputByTab[tabId] || "";
  const joiner = base && !/\s$/.test(base) ? " " : "";
  const next = base + joiner + text;
  voiceDraftByTab[tabId] = true;
  inputByTab[tabId] = next;
  if (tabId === state.activeTabId) {
    el.msgInput.value = next;
    autoResizeInput();
    el.msgInput.focus();
  }
}

async function finishVoice(cancelled) {
  const press = voicePress;
  voicePress = null;
  const rec = voiceRec;
  voiceRec = null;
  if (!rec) {
    updateVoiceUi("idle");
    return;
  }
  stopVoiceRecording(rec);
  const duration = Date.now() - rec.startedAt;
  if (cancelled) {
    updateVoiceUi("idle");
    showToast("已取消语音输入");
    return;
  }
  if (duration < VOICE_MIN_MS) {
    updateVoiceUi("idle");
    showToast("说话时间太短", true);
    return;
  }

  const tabId = press?.tabId || state.activeTabId;
  voiceTranscribing = true;
  updateVoiceUi("idle");
  renderTabs();
  try {
    const samples = downsampleBuffer(rec.chunks, rec.inputRate, VOICE_SAMPLE_RATE);
    const wav = encodeWav(samples, VOICE_SAMPLE_RATE);
    const result = await apiRequest("/asr", {
      method: "POST",
      body: { audio: bytesToBase64(wav), format: "wav", sample_rate: VOICE_SAMPLE_RATE },
    });
    const text = (result?.text || "").trim();
    if (!text) {
      showToast("没有识别到内容，请再说一次", true);
    } else {
      applyVoiceResult(tabId, text);
      showToast("识别完成，可修改后发送");
    }
  } catch (err) {
    showToast(`语音识别失败：${err.message}`, true);
  } finally {
    voiceTranscribing = false;
    renderTabs();
  }
}

function clearHoldCandidate() {
  if (holdCandidate) {
    clearTimeout(holdCandidate.timer);
    holdCandidate = null;
  }
}

async function triggerLongPressVoice(startY, tabId) {
  if (voiceRec || voicePress || voiceTranscribing) return;
  clearTimeout(streamTimer);
  try {
    window.getSelection()?.removeAllRanges();
    el.msgInput.blur();
  } catch {
    // Ignore
  }
  const press = { startY, cancel: false, released: false, tabId };
  voicePress = press;
  updateVoiceUi("recording");
  el.voiceTimer.textContent = "0:00";
  if (navigator.vibrate) navigator.vibrate(18);

  const rec = await startVoiceRecording();
  if (!rec) {
    voicePress = null;
    updateVoiceUi("idle");
    return;
  }
  if (press.released || voicePress !== press) {
    stopVoiceRecording(rec);
    updateVoiceUi("idle");
    showToast("麦克风已就绪，请重新长按输入框说话");
    return;
  }
  voiceRec = rec;
  rec.timer = setInterval(() => {
    const elapsed = Date.now() - rec.startedAt;
    el.voiceTimer.textContent = formatVoiceTime(elapsed);
    if (elapsed >= VOICE_MAX_MS) {
      showToast("已达到 60 秒上限，自动识别");
      finishVoice(false);
    }
  }, 200);
}

el.msgInput.addEventListener("contextmenu", (e) => {
  if (holdCandidate || voicePress || voiceRec) {
    e.preventDefault();
  }
});

el.msgInput.addEventListener("pointerdown", (e) => {
  if (e.button !== undefined && e.button !== 0) return;
  const active = currentTab();
  if (!active || !active.writable || active.agent_state === "awaiting") return;
  if (voiceRec || voicePress || voiceTranscribing) return;

  clearHoldCandidate();
  const startX = e.clientX;
  const startY = e.clientY;
  const tabId = active.id;
  const timer = setTimeout(() => {
    holdCandidate = null;
    triggerLongPressVoice(startY, tabId);
  }, LONG_PRESS_MS);
  holdCandidate = { startX, startY, timer };
});

window.addEventListener(
  "pointermove",
  (e) => {
    if (holdCandidate) {
      const dx = Math.abs(e.clientX - holdCandidate.startX);
      const dy = Math.abs(e.clientY - holdCandidate.startY);
      if (dx > MOVE_SLOP_PX || dy > MOVE_SLOP_PX) {
        clearHoldCandidate();
      }
      return;
    }
    const press = voicePress;
    if (!press) return;
    const shouldCancel = press.startY - e.clientY > VOICE_CANCEL_DISTANCE;
    if (shouldCancel !== press.cancel) {
      press.cancel = shouldCancel;
      updateVoiceUi(shouldCancel ? "cancel" : "recording");
    }
  },
  { passive: true }
);

function handleVoicePointerEnd(cancelled) {
  if (holdCandidate) {
    clearHoldCandidate();
  }
  const press = voicePress;
  if (!press) return;
  press.released = true;
  if (!voiceRec) {
    voicePress = null;
    updateVoiceUi("idle");
    return;
  }
  finishVoice(cancelled ? true : press.cancel);
}

window.addEventListener("pointerup", () => handleVoicePointerEnd(false));
window.addEventListener("pointercancel", () => handleVoicePointerEnd(true));
window.addEventListener("touchend", () => handleVoicePointerEnd(false), { passive: true });
window.addEventListener("touchcancel", () => handleVoicePointerEnd(true), { passive: true });

document.addEventListener("visibilitychange", () => {
  if (!document.hidden && state.token) {
    if (!ws || ws.readyState !== WebSocket.OPEN) connectWebSocket();
    fetchTabs();
  }
});

let lastIsDesktop = window.innerWidth >= 768;
window.addEventListener("resize", () => {
  const isDesktop = window.innerWidth >= 768;
  if (isDesktop !== lastIsDesktop) {
    lastIsDesktop = isDesktop;
    if (isDesktop) {
      el.drawerTabs.classList.remove("drawer-open");
    }
    renderTabs();
    renderScreen();
  }
});

if (window.visualViewport) {
  let layoutMaxHeight = Math.max(window.innerHeight, window.visualViewport.height);
  const updateViewport = () => {
    const vv = window.visualViewport;
    if (!vv) return;
    if (vv.height > layoutMaxHeight) {
      layoutMaxHeight = vv.height;
    }
    const kbOpen = layoutMaxHeight - vv.height > 110;
    document.documentElement.classList.toggle("keyboard-open", kbOpen);
    document.documentElement.style.setProperty("--vvh", `${Math.round(vv.height)}px`);
    document.documentElement.style.setProperty(
      "--vv-top",
      `${Math.max(0, Math.round(vv.offsetTop))}px`
    );
    if (window.scrollY !== 0 || window.scrollX !== 0) {
      window.scrollTo(0, 0);
    }
    if (stickToBottom) scrollToBottom();
  };
  window.visualViewport.addEventListener("resize", updateViewport);
  window.visualViewport.addEventListener("scroll", updateViewport);
  el.msgInput.addEventListener("focus", () => {
    setTimeout(updateViewport, 60);
    setTimeout(updateViewport, 250);
  });
  el.msgInput.addEventListener("blur", () => {
    setTimeout(updateViewport, 60);
    setTimeout(updateViewport, 250);
  });
  updateViewport();
}

if ("serviceWorker" in navigator) {
  navigator.serviceWorker
    .register("/otty/sw.js")
    .then((reg) => reg.update())
    .catch(() => {});
  if ("caches" in window) {
    caches.keys().then((keys) => keys.forEach((k) => caches.delete(k)));
  }
}

// Init
importHashCredentials();
applyDisplayPrefs();
renderTabs();
renderScreen();
startBackgroundLoop();
if (state.token) {
  connectWebSocket();
  fetchTabs();
}
