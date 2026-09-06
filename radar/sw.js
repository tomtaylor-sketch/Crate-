// Dig Radar service worker. Bump CACHE when you change index.html so installed copies update.
const CACHE = 'dig-radar-v3';
const SHELL = ['./index.html', './manifest.json', './icon-192.png', './icon-512.png'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (url.pathname.endsWith('radar_targets.json')) {
    // data: network first, fall back to the last copy we saw
    e.respondWith(fetch(e.request).then((r) => { const copy = r.clone(); caches.open(CACHE).then((c) => c.put(url.pathname, copy)); return r; })
      .catch(() => caches.match(url.pathname)));
    return;
  }
  if (e.request.mode === 'navigate' || SHELL.some((p) => url.pathname.endsWith(p.slice(1)))) {
    e.respondWith(caches.match(e.request, { ignoreSearch: true }).then((r) => r || fetch(e.request)));
  }
});
