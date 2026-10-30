/* Installable shell only. No offline auth, API cache, write queue or replay. */
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));
// Deliberately no fetch/background-sync handler. Every operation needs authority.
