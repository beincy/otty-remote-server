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

// In-memory per-tab input & synced state so page reloads never re-send stale text
const inputByTab = {};
const syncedByTab = {};
let isComposing = false;
let streamTimer = null;
let streamInFlight = false;
let pendingFlushSubmit = false;

const el = {
  btnOpenDrawer: document.getElementById("btn-open-drawer"),
  headerCenter: document.getElementById("header-center"),
  connDot: document.getElementById("conn-dot"),
  connLabel: document.getElementById("conn-label"),
  tabCountBadge: document.getElementById("tab-count-badge"),
  activeStateDot: document.getElementById("active-state-dot"),
  activeTitle: document.getElementById("active-title"),
  activeSubtitle: document.getElementById("active-subtitle"),
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

function renderTabs() {
  el.tabCountBadge.textContent = String(state.tabs.length || 0);

  if (state.tabs.length && !state.tabs.some((t) => t.id === state.activeTabId)) {
    const preferred = state.tabs.find((t) => t.agent && !t.protected) || state.tabs[0];
    state.activeTabId = preferred.id;
  }

  el.tabListCards.innerHTML = "";
  state.tabs.forEach((tab, idx) => {
    const card = document.createElement("button");
    card.type = "button";
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

    const statusPill = document.createElement("span");
    statusPill.className = "task-status-pill";
    statusPill.textContent = STATE_TEXT[tab.agent_state] || (tab.protected ? "宿主" : "终端");

    card.append(main, statusPill);
    card.addEventListener("click", () => {
      el.drawerTabs.classList.add("hidden");
      selectTab(tab.id);
    });
    el.tabListCards.appendChild(card);
  });

  const active = currentTab();
  if (!active) {
    el.activeStateDot.className = "state-dot";
    el.activeTitle.textContent = state.token ? "暂无会话" : "未连接";
    el.activeSubtitle.textContent = state.token ? "点击右上角或此处新建会话" : "点击右上角 ⚙ 配置 Token";
    el.keypadDrawer.classList.add("hidden");
    return;
  }

  el.activeStateDot.className = `state-dot ${active.agent_state || ""}`;
  el.activeTitle.textContent = tabDisplayName(active);
  const shortDir = active.cwd ? active.cwd.split("/").pop() : "";
  const stateLabel = STATE_TEXT[active.agent_state] || (active.protected ? "宿主保护" : "终端");
  el.activeSubtitle.textContent = [active.agent || "Shell", shortDir, stateLabel]
    .filter(Boolean)
    .join(" · ");

  const isAwaiting = active.agent_state === "awaiting";
  el.awaitingBanner.classList.toggle("hidden", !isAwaiting);
  const showKeypad = isAwaiting || state.keypadOpen;
  el.keypadDrawer.classList.toggle("hidden", !showKeypad);
  el.btnToggleKeys.classList.toggle("active", showKeypad);

  const canInput = active.writable && !isAwaiting;
  el.msgInput.disabled = !canInput;
  el.btnSend.disabled = !canInput;
  if (active.protected) {
    el.msgInput.placeholder = "服务宿主 Tab 受保护";
  } else if (!active.writable) {
    el.msgInput.placeholder = "普通终端只读";
  } else if (isAwaiting) {
    el.msgInput.placeholder = "请先点上方按键回应 Agent...";
  } else if (state.inputMode === "stream") {
    el.msgInput.placeholder = `实时流式输入到 ${tabDisplayName(active)}...`;
  } else {
    el.msgInput.placeholder = `发送给 ${tabDisplayName(active)}...`;
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
  if (stickToBottom) {
    scrollToBottom();
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
      params: { limit: 60 },
    });
    applyScreenPage(tabId, page);
  } catch (err) {
    showToast(err.message, true);
  }
}

async function loadMoreHistory() {
  const active = currentTab();
  if (!active) return;
  const entry = state.screens[active.id];
  if (!entry?.nextBefore) return;
  const prevHeight = el.viewport.scrollHeight;
  const prevTop = el.viewport.scrollTop;
  try {
    const page = await apiRequest(`/tabs/${encodeURIComponent(active.id)}/screen`, {
      params: { limit: 60, before: entry.nextBefore },
    });
    entry.lines = [...(page.lines || []), ...entry.lines];
    entry.nextBefore = page.nextBefore || null;
    entry.hasMore = Boolean(page.hasMore);
    stickToBottom = false;
    renderScreen();
    requestAnimationFrame(() => {
      el.viewport.scrollTop = el.viewport.scrollHeight - prevHeight + prevTop;
    });
  } catch (err) {
    showToast(err.message, true);
    await fetchLatestScreen(active.id);
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
    if (document.hidden || !state.token || streamInFlight) return;
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
  el.msgInput.style.height = "auto";
  el.msgInput.style.height = `${Math.min(el.msgInput.scrollHeight, 110)}px`;
}

function openDrawerTabs() {
  renderTabs();
  el.drawerTabs.classList.remove("hidden");
}

function openSettingsModal() {
  el.inputToken.value = state.token;
  el.inputInstance.value = state.instanceId;
  applyDisplayPrefs();
  el.modalSettings.classList.remove("hidden");
}

function openNewTabModal() {
  el.drawerTabs.classList.add("hidden");
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

[el.drawerTabs, el.modalNewTab, el.modalSettings].forEach((backdrop) => {
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

el.btnWrap.addEventListener("click", () => {
  state.wrap = !state.wrap;
  applyDisplayPrefs();
  saveState();
});

el.btnRefresh.addEventListener("click", () => {
  stickToBottom = true;
  fetchTabs();
  showToast("已刷新");
});

el.btnCloseTab.addEventListener("click", async () => {
  const active = currentTab();
  if (!active || active.protected) return;
  if (!confirm(`确定关闭会话 "${tabDisplayName(active)}" 吗？`)) return;
  try {
    await apiRequest(`/tabs/${encodeURIComponent(active.id)}`, { method: "DELETE" });
    delete state.screens[active.id];
    delete inputByTab[active.id];
    delete syncedByTab[active.id];
    state.activeTabId = "";
    el.modalSettings.classList.add("hidden");
    await fetchTabs();
    showToast("已关闭会话");
  } catch (err) {
    showToast(err.message, true);
  }
});

el.btnLoadMore.addEventListener("click", loadMoreHistory);

el.viewport.addEventListener("scroll", () => {
  stickToBottom = isNearBottom();
  el.btnScrollBottom.classList.toggle("hidden", stickToBottom);
});

el.btnScrollBottom.addEventListener("click", scrollToBottom);

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

document.addEventListener("visibilitychange", () => {
  if (!document.hidden && state.token) {
    if (!ws || ws.readyState !== WebSocket.OPEN) connectWebSocket();
    fetchTabs();
  }
});

if (window.visualViewport) {
  const updateViewport = () => {
    document.documentElement.style.setProperty("--vvh", `${window.visualViewport.height}px`);
    if (stickToBottom) scrollToBottom();
  };
  window.visualViewport.addEventListener("resize", updateViewport);
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
