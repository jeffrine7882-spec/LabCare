/* LabSynch service worker — receives Web Push notifications so alert bells
   ring at the OS/browser level even when the app, the tab, or the browser
   window is closed.

   The service worker is deliberately simple:
     * it does NOT cache app code (the app always fetches fresh from the
       network so updates deploy instantly), and
     * it does nothing but handle pushes and notification clicks.
   Registered only after the signed-in user turns Desktop alerts ON, so nobody
   is asked for permission until they want this feature.
*/

self.addEventListener("install", () => {
  // Take control of any already-open pages immediately.
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("push", (event) => {
  let data = {};
  try {
    if (event.data) data = event.data.json();
  } catch (e) {
    try { data = { body: event.data ? event.data.text() : "" }; } catch (e2) {}
  }
  const title = data.title || "LabSynch";
  const options = {
    body: (data.body || "New alert").slice(0, 200),
    icon: data.icon || "/icons/icon-192.png",
    badge: data.badge || "/icons/icon-192.png",
    tag: data.tag || ("labcare-" + Date.now()),
    renotify: true,   // latest alert always replaces the old one with a fresh ring
    data: Object.assign({ url: "/" }, data.data || {}),
    requireInteraction: false,
  };
  // A push that arrives while the app is open still shows a system
  // notification (and rings), rather than being silently swallowed.
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const w of windows) {
      if (new URL(w.url).pathname === new URL(target, self.location.origin).pathname && "focus" in w) {
        await w.navigate(target);
        return w.focus();
      }
    }
    for (const w of windows) {
      if ("focus" in w) return w.focus();
    }
    return self.clients.openWindow(target);
  })());
});
