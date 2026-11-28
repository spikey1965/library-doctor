// Library Doctor home screen app. Keeps ONLY the "can't reach your server" page and its icon.
// Library data, API replies and the pages themselves are never stored.
const CACHE = "ld-offline-v1", KEEP = ["/pwa/offline.html", "/pwa/icon-192.png"];
self.addEventListener("install", e => e.waitUntil((async () => {
  const c = await caches.open(CACHE);
  for (const u of KEEP) {
    const r = await fetch(u, { cache: "no-store" });
    if (!r.ok || r.redirected) throw new Error("not stored: " + u);     // e.g. a proxy's login page
    if (u.endsWith(".html") && !(await r.clone().text()).includes("ld-offline")) throw new Error("not ours: " + u);
    await c.put(u, r);
  }
  await self.skipWaiting();
})()));
self.addEventListener("activate", e => e.waitUntil((async () => {
  for (const k of await caches.keys()) if (k.startsWith("ld-offline-") && k !== CACHE) await caches.delete(k);
  await self.clients.claim();
})()));
// away from home the connection often just hangs: give up after 10 seconds
const inTime = p => Promise.race([p, new Promise((_, no) => setTimeout(() => no(new Error("timeout")), 10000))]);
self.addEventListener("fetch", e => {
  const u = new URL(e.request.url);
  if (u.origin !== location.origin) return;
  if (e.request.mode === "navigate" && ["/", "/index.html", "/login"].includes(u.pathname))
    e.respondWith(inTime(fetch(e.request)).catch(async () => (await caches.match("/pwa/offline.html")) || Response.error()));
  else if (u.pathname === "/pwa/icon-192.png")
    e.respondWith(caches.match("/pwa/icon-192.png").then(r => r || fetch(e.request)));   // the stored copy first: a hung server must not hold up the offline page
});
