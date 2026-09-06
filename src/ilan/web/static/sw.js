/* ilan service worker — notifications only.
 *
 * This worker exists so the phone can show a push notification while the app
 * is closed, and open the right task when it is tapped. That is all it does.
 *
 * Deliberately no fetch handler and no cache: a worker that served pages from
 * a cache would keep serving the previous version of the app after the server
 * had moved on, which is exactly the staleness the app has been built to avoid.
 * Every request still goes to the server as if this file did not exist.
 */

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));

self.addEventListener('push', (event) => {
  let note = {};
  try {
    note = event.data ? event.data.json() : {};
  } catch {
    note = { title: 'ilan', body: event.data ? event.data.text() : '' };
  }
  // The server composes the note; this only shows it. tag replaces an earlier
  // notification for the same task rather than stacking a second one.
  event.waitUntil(self.registration.showNotification(note.title || 'ilan', {
    body: note.body || '',
    tag: note.tag || undefined,
    data: { url: note.url || '#/' },
    icon: 'icon-180.png',
  }));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const hash = (event.notification.data && event.notification.data.url) || '#/';
  // Relative to the worker's scope, like every other URL the app uses, so a
  // server mounted under a prefix still opens the right page.
  const target = new URL('./' + hash, self.registration.scope).href;
  event.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true })
    .then((clients) => {
      const open = clients.find((c) => 'focus' in c);
      if (open) {
        // Steer the page that is already open rather than opening a second
        // one; the app listens for this message and changes its route.
        open.postMessage({ type: 'navigate', hash });
        return open.focus();
      }
      return self.clients.openWindow(target);
    }));
});
