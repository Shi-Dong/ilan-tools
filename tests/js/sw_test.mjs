/* Assertions for the service worker, run for real rather than read.
 *
 * sw.js is evaluated against a fake `self` that records what the worker
 * subscribes to and what it does when a push arrives or a notification is
 * tapped. That is the whole of the worker's job, and the two things worth
 * proving about it are that it shows exactly what the server composed and that
 * a tap lands on the right task without opening a second window when one is
 * already open.
 */

import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(here, '..', '..', 'src', 'ilan', 'web', 'static', 'sw.js'), 'utf8');

const failures = [];
const check = (label, ok, detail = '') => { if (!ok) failures.push(`FAIL  ${label}${detail ? `\n        ${detail}` : ''}`); };

/** A worker global with a given scope and set of open windows. */
function boot({ scope = 'https://host.example/app/', clients = [] } = {}) {
  const shown = [], opened = [], listeners = {};
  const self = {
    addEventListener: (type, fn) => { (listeners[type] ||= []).push(fn); },
    skipWaiting: () => Promise.resolve(),
    registration: { scope, showNotification: async (title, opts) => { shown.push({ title, opts }); } },
    clients: { claim: () => Promise.resolve(), matchAll: async () => clients, openWindow: async (url) => { opened.push(url); } },
  };
  new Function('self', source)(self);
  const fire = async (type, event) => {
    let waited = null;
    event.waitUntil = (p) => { waited = p; };
    for (const fn of listeners[type] || []) fn(event);
    await waited;
  };
  return { listeners, shown, opened, fire };
}

// ── what it subscribes to ───────────────────────────────────────────────
const w = boot();
check('it handles push and notificationclick', 'push' in w.listeners && 'notificationclick' in w.listeners,
  Object.keys(w.listeners).join(','));
check('it never handles fetch', !('fetch' in w.listeners), 'a fetch handler is the door to serving stale app code');

// ── a push shows what the server composed ───────────────────────────────
const note = { title: 'train-on-chess', body: 'Needs attention — Blocked on the shard layout', tag: 'task:train-on-chess', url: '#/t/train-on-chess', status: 'NEEDS_ATTENTION' };
await w.fire('push', { data: { json: () => note, text: () => JSON.stringify(note) } });
check('one notification is shown', w.shown.length === 1);
check('its title is the task name', w.shown[0]?.title === 'train-on-chess');
check('its body is the status words and the summary', w.shown[0]?.opts.body === note.body, w.shown[0]?.opts.body);
check('repeats for the same task replace the earlier one', w.shown[0]?.opts.tag === 'task:train-on-chess');
check('the tap target travels with it', w.shown[0]?.opts.data.url === '#/t/train-on-chess');
check('it carries the app icon', w.shown[0]?.opts.icon === 'icon-180.png');

const odd = boot();
await odd.fire('push', { data: { json: () => { throw new SyntaxError('not json'); }, text: () => 'plain words' } });
check('a non-JSON push is still shown rather than dropped',
  odd.shown[0]?.title === 'ilan' && odd.shown[0]?.opts.body === 'plain words', JSON.stringify(odd.shown));

const empty = boot();
await empty.fire('push', { data: null });
check('a push with no data shows a bare notification', empty.shown[0]?.title === 'ilan' && empty.shown[0]?.opts.body === '');

// ── a tap steers the open app, or opens one ─────────────────────────────
const messages = []; let focused = 0;
const client = { focus: async () => { focused += 1; }, postMessage: (m) => messages.push(m) };
const openApp = boot({ clients: [client] });
await openApp.fire('notificationclick', { notification: { close: () => {}, data: { url: '#/t/train-on-chess' } } });
check('with the app open, the tap steers it to the task',
  messages.length === 1 && messages[0].type === 'navigate' && messages[0].hash === '#/t/train-on-chess', JSON.stringify(messages));
check('and brings it to the front', focused === 1);
check('and opens no second window', openApp.opened.length === 0);

const closedApp = boot({ scope: 'https://host.example/prefix/app/' });
await closedApp.fire('notificationclick', { notification: { close: () => {}, data: { url: '#/t/train-on-chess' } } });
check('with the app closed, the tap opens it on the task, under the worker\'s own scope',
  closedApp.opened[0] === 'https://host.example/prefix/app/#/t/train-on-chess', JSON.stringify(closedApp.opened));

const bare = boot();
await bare.fire('notificationclick', { notification: { close: () => {}, data: undefined } });
check('a tap with no target opens the list', bare.opened[0] === 'https://host.example/app/#/', JSON.stringify(bare.opened));

if (failures.length) { console.log(failures.join('\n')); console.log(`\n${failures.length} service-worker assertion(s) FAILED`); process.exit(1); }
console.log('all service-worker assertions passed');
