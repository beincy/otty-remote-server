const CACHE_NAME = "otty-pwa-v9";

self.addEventListener("install", () => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", () => {
  // Pass through directly to network without caching HTML/JS/CSS
});

self.addEventListener("push", (event) => {
  let payload = {
    title: "Otty 会话提醒",
    body: "Agent 已从忙碌变为空闲，点击查看最新回复",
    tab_id: "",
    tag: "otty-agent",
  };
  if (event.data) {
    try {
      payload = { ...payload, ...event.data.json() };
    } catch {
      payload.body = event.data.text() || payload.body;
    }
  }

  event.waitUntil(
    self.registration.showNotification(payload.title || "Otty 会话提醒", {
      body: payload.body || "",
      tag: payload.tag || "otty-agent",
      renotify: true,
      icon: "/otty/icon.svg",
      badge: "/otty/icon.svg",
      data: { tab_id: payload.tab_id || "" },
    })
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const tabId = event.notification?.data?.tab_id || "";

  event.waitUntil(
    self.clients
      .matchAll({ type: "window", includeUncontrolled: true })
      .then((clientList) => {
        for (const client of clientList) {
          if ("focus" in client) {
            if (tabId) {
              client.postMessage({ type: "otty_select_tab", tab_id: tabId });
            }
            return client.focus();
          }
        }
        if (self.clients.openWindow) {
          const target = tabId ? `/?tab=${encodeURIComponent(tabId)}` : "/";
          return self.clients.openWindow(target);
        }
        return null;
      })
  );
});
