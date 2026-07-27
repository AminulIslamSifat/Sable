// Service worker intentionally disabled.
// The previous version precached root-relative asset paths (/styles.css etc.)
// that 404 against the real /static/ mount, then served stale cache fallbacks
// — intermittently breaking CSS/JS loading across every browser.
//
// This kamikaze version is still served so that clients which already
// registered the broken worker will receive it on their next update check,
// wipe every Sable cache, and unregister themselves. New visitors never
// register a worker (the register() call was removed from index.html).
self.addEventListener("install", () => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys();
      await Promise.all(keys.map((key) => caches.delete(key)));
      await self.registration.unregister();
    })()
  );
});

// No fetch handler on purpose — never intercept network requests.
