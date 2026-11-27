// Flight Prices service worker: makes the app installable and keeps the last
// dashboard you saw for when you are offline. Online, it always fetches fresh.
const CACHE = "flight-prices-v1";
const ICONS = ["/icon-192.png", "/icon-512.png", "/apple-touch-icon.png"];

self.addEventListener("install", (event) => {
  self.skipWaiting();
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(ICONS)).catch(() => {}));
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    for (const key of await caches.keys()) if (key !== CACHE) await caches.delete(key);
    await self.clients.claim();
  })());
});

const OFFLINE = `<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Flight Prices</title><body style="margin:0;background:#e7e5e0;font:16px system-ui,sans-serif;color:#161513;display:grid;place-items:center;min-height:100vh;text-align:center">
<div><h1 style="font-size:22px">You're offline</h1><p style="color:#57544e">Prices will load when you're back online.</p></div>`;

self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== self.location.origin) return;

  if (req.mode === "navigate" && url.pathname === "/") {
    event.respondWith((async () => {
      try {
        const res = await fetch(req);
        // Keep a copy only of the real dashboard (not the login page).
        if (res.ok && !res.redirected) {
          const text = await res.clone().text();
          if (text.includes('id="app"') && text.includes('id="data"')) {
            const cache = await caches.open(CACHE);
            await cache.put("/", new Response(text, {headers: {"Content-Type": "text/html; charset=utf-8"}}));
          }
        }
        return res;
      } catch (e) {
        const cached = await caches.match("/");
        return cached || new Response(OFFLINE, {headers: {"Content-Type": "text/html; charset=utf-8"}});
      }
    })());
    return;
  }

  if (ICONS.includes(url.pathname)) {
    event.respondWith(caches.match(req).then((hit) => hit || fetch(req)));
  }
});
