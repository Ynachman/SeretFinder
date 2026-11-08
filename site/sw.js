// Service worker: instant launch + offline.
//  * App shell (page, icons, manifest): network-first with a short timeout, so
//    friends always get the latest app version when online, and the cached
//    copy opens instantly when offline or on a very slow connection.
//  * data.json: network-first (fresh showtimes), falling back to the cached copy
//    when offline or on a very slow connection.
//  * Posters: cache-first (they never change for a given URL).
const VERSION = "v2";
const SHELL = `shell-${VERSION}`;
const DATA = `data-${VERSION}`;
const IMAGES = "posters";
const SHELL_FILES = ["./", "index.html", "manifest.webmanifest", "icon-192.png", "icon-512.png", "apple-touch-icon.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(SHELL_FILES)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil((async () => {
    const keep = new Set([SHELL, DATA, IMAGES]);
    for (const k of await caches.keys()) if (!keep.has(k)) await caches.delete(k);
    await self.clients.claim();
  })());
});

async function networkFirst(req, cacheName, timeoutMs) {
  const cache = await caches.open(cacheName);
  const network = fetch(req).then((res) => {
    if (res.ok) cache.put(req, res.clone());
    return res;
  });
  // Network wins if it answers within timeoutMs. After that, use the cached
  // copy if there is one; with nothing cached (first visit), keep waiting.
  const timedOut = new Promise((resolve) => setTimeout(() => resolve(null), timeoutMs));
  try {
    const first = await Promise.race([network, timedOut]);
    if (first) return first;
  } catch (err) {
    // network failed outright: fall through to the cache
  }
  const hit = await cache.match(req, { ignoreSearch: true });
  if (hit) return hit;
  return network;   // still pending (slow) or rejected (offline, nothing cached)
}

const MAX_POSTERS = 150;

async function cacheFirst(req, cacheName) {
  const cache = await caches.open(cacheName);
  const hit = await cache.match(req);
  if (hit) return hit;
  const res = await fetch(req);
  if (res.ok || res.type === "opaque") {
    await cache.put(req, res.clone());
    const keys = await cache.keys();   // oldest first: trim so it can't grow forever
    for (const k of keys.slice(0, Math.max(0, keys.length - MAX_POSTERS))) await cache.delete(k);
  }
  return res;
}

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.hostname === "image.tmdb.org") return e.respondWith(cacheFirst(req, IMAGES));
  if (url.origin !== location.origin) return;
  if (url.pathname.endsWith("/data.json")) return e.respondWith(networkFirst(req, DATA, 4000));
  e.respondWith(networkFirst(req, SHELL, 2500));
});
