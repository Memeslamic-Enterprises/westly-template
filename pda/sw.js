// Westly app shell: the page is network-first (1.5s cap, then the cached copy), icons are cache-first.
// /api/* and every non-GET go straight to the network and are never cached.
const CACHE = 'westly-v3';
const SHELL = ['/', '/manifest.json', '/icon.svg', '/icon-180.png', '/icon-512.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener('fetch', e => {
  const req = e.request, url = new URL(req.url);
  if (req.method !== 'GET' || url.origin !== location.origin || url.pathname.startsWith('/api/')) return;
  const nav = req.mode === 'navigate';
  if (nav ? url.pathname !== '/' : !SHELL.includes(url.pathname)) return;
  const key = nav ? '/' : url.pathname;
  const net = fetch(req).then(r => {
    if (r.ok && !r.redirected) {
      const copy = r.clone();
      e.waitUntil(caches.open(CACHE).then(c => c.put(key, copy)));
    }
    return r;
  });
  e.waitUntil(net.catch(() => {}));
  const cache = () => caches.match(key, {cacheName: CACHE});
  if (!nav) return e.respondWith(cache().then(hit => hit || net));
  const cap = new Promise(res => setTimeout(res, 1500, null));
  e.respondWith(Promise.race([net.catch(() => null), cap]).then(r => r || cache().then(hit => hit || net)));
});
