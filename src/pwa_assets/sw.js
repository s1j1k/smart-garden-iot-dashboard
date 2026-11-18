// Smart garden service worker: installability, an offline page, and notification taps.
// Live data comes over Streamlit's websocket, which a service worker never sees, so
// nothing else is cached — a stale garden reading is worse than an honest "offline".
const CACHE = "smart-garden-v1";
const OFFLINE = "/pwa/offline.html";

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll([OFFLINE, "/pwa/icon-192.png"])));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  if (event.request.mode !== "navigate") return;
  event.respondWith(fetch(event.request).catch(() => caches.match(OFFLINE)));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    self.clients.matchAll({type: "window", includeUncontrolled: true}).then((wins) => {
      for (const win of wins) {
        if ("focus" in win) return win.navigate ? win.navigate(target).then((w) => w && w.focus())
                                                : win.focus();
      }
      return self.clients.openWindow(target);
    }),
  );
});
