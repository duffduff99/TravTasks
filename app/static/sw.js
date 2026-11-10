// Minimal service worker so the app can be installed as a PWA.
// It caches nothing: every request goes to the server as normal.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
