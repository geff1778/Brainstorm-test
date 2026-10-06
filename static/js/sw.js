/* BrainStorm service worker.
 *
 * Strategy:
 *   - navigation (HTML shell)  -> network-first, cache as offline fallback
 *   - same-origin static       -> stale-while-revalidate
 *   - API / Socket.IO / CDN    -> never cached
 *
 * Bump CACHE_VERSION on every deploy that changes the shell so clients pick up
 * the new bundle instead of a stale cached copy.
 */
const CACHE_VERSION = "brainstorm-v3";
const SHELL = ["/", "/static/css/style.css", "/static/js/game.js"];

self.addEventListener("install", event => {
  event.waitUntil(
    caches.open(CACHE_VERSION)
      .then(cache => cache.addAll(SHELL))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(
        keys.filter(key => key !== CACHE_VERSION).map(key => caches.delete(key))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", event => {
  const request = event.request;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  const isApi = url.pathname.startsWith("/api/") || url.pathname.startsWith("/socket.io/");
  const isCrossOrigin = url.origin !== self.location.origin;
  if (isApi || isCrossOrigin) return;

  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request)
        .then(response => {
          const clone = response.clone();
          caches.open(CACHE_VERSION).then(cache => cache.put(request, clone));
          return response;
        })
        .catch(() => caches.match(request).then(cached => cached || caches.match("/")))
    );
    return;
  }

  event.respondWith(
    caches.match(request).then(cached => {
      const network = fetch(request)
        .then(response => {
          if (response.ok) {
            const clone = response.clone();
            caches.open(CACHE_VERSION).then(cache => cache.put(request, clone));
          }
          return response;
        })
        .catch(() => cached);
      return cached || network;
    })
  );
});
