/* Assertions for the web side of push notifications.
 *
 * The harness plays the phone: by default a browser with no service worker,
 * and on request one with a worker, a push manager that records what it is
 * asked, a permission that a test can set, and the badge API. What is asserted
 * is the contract with the browser and the server — which state the Settings
 * card shows for each situation, that permission is asked for *before* any
 * network round-trip (iOS drops the user gesture otherwise), what the
 * subscription posted to the server contains, and what the badge counts.
 */

import { bootApp, checker, settle } from './harness.mjs';

const { check, report } = checker();

function settings(extra = {}) {
  const app = bootApp();
  app.setFetch(async (path, opts) => {
    const json = (d, ok = true, status = 200) => ({ ok, status, json: async () => d });
    if (path === '/config') return json({ config: { workdir: '/tmp' } });
    if (path === '/version') return json({ version: '1', commit: 'abc', pid: 7 });
    if (path === '/push') return extra.noServer ? json({ error: 'not found' }, false, 404)
      : json({ public_key: 'BFakeKey_' + 'A'.repeat(78), subscriptions: extra.devices ?? 0 });
    if (path === '/push/subscribe') return json({ ok: true, subscriptions: 1 });
    if (path === '/push/unsubscribe') return json({ ok: true, removed: true, subscriptions: 0 });
    return json({});
  });
  return app;
}
const card = (app) => (app.html().match(/<div class="card push-card">([\s\S]*?)<\/div>\s*<\/main>/) || [])[1] || '';
const posts = (app, suffix) => app.fetches.filter((f) => (f.opts || {}).method === 'POST' && f.path.endsWith(suffix));

// ── the four situations ─────────────────────────────────────────────────
const none = settings();
check('a browser without service workers is told so', none.pushSupport() === 'unsupported');
await none.renderConfig();
check('Settings says the browser cannot receive them',
  card(none).includes('cannot receive push notifications') && !card(none).includes('id="push-on"'), card(none));
check('and did not ask the server about push', !none.fetches.some((f) => f.path === '/push'),
  'the server was asked about push for a device that can never use it');

const tab = settings();
tab.enablePushSupport({ installed: false });
check('a worker without push is the not-installed case', tab.pushSupport() === 'install');
await tab.renderConfig();
check('Settings says to add the app to the Home Screen first',
  card(tab).includes('Add ilan to your Home Screen') && !card(tab).includes('<button'), card(tab));

const blocked = settings();
blocked.enablePushSupport();
blocked.notification.permission = 'denied';
check('a denied permission is blocked', blocked.pushSupport() === 'blocked');
await blocked.renderConfig();
check('Settings says they are blocked, with no button to press',
  card(blocked).includes('blocked for ilan') && !card(blocked).includes('<button'), card(blocked));

const ready = settings({ devices: 2 });
ready.enablePushSupport();
check('an installed app with permission still askable is ready', ready.pushSupport() === 'ready');
await ready.renderConfig();
check('Settings offers to enable, as the filled primary',
  /<button class="btn btn-primary" id="push-on">Enable notifications<\/button>/.test(card(ready)), card(ready));
check('and says what a notification will say',
  card(ready).includes('name, outcome, summary'), card(ready));
check('and how many devices the server knows', card(ready).includes('2 devices'), card(ready));

const onAlready = settings({ devices: 1 });
onAlready.enablePushSupport({ subscribed: true });
await onAlready.renderConfig();
check('a subscribed phone is offered Disable, quietly',
  /<button class="btn" id="push-off">Disable<\/button>/.test(card(onAlready)) && card(onAlready).includes('1 device<'), card(onAlready));

const old = settings({ noServer: true });
old.enablePushSupport();
await old.renderConfig();
check('a server without push support is named as the reason, with no button',
  card(old).includes('server needs updating') && !card(old).includes('<button'), card(old));

// ── enabling ────────────────────────────────────────────────────────────
const en = settings();
en.enablePushSupport();
en.notification.permission = 'granted';
const before = en.fetches.length;
const enabled = await en.enablePush();
check('enabling succeeds', enabled === true);
check('permission was asked for exactly once', en.push.permissionAsked === 1);
// The gesture rule: the permission prompt has to be the first thing that
// happens, before the server is consulted for its key.
const firstFetchAfter = en.fetches.slice(before)[0];
check('permission was asked before anything was fetched',
  en.push.permissionAsked === 1 && firstFetchAfter && firstFetchAfter.path === '/push',
  JSON.stringify(en.fetches.slice(before).map((f) => f.path)));
check('the browser was subscribed with the server\'s key, user-visible only',
  en.push.subscribeCalls.length === 1 && en.push.subscribeCalls[0].userVisibleOnly === true
    && en.push.subscribeCalls[0].applicationServerKey instanceof Uint8Array
    && en.push.subscribeCalls[0].applicationServerKey.length === 65,
  JSON.stringify(en.push.subscribeCalls.map((c) => [c.userVisibleOnly, c.applicationServerKey && c.applicationServerKey.length])));
check('the subscription the browser produced was handed to the server',
  posts(en, '/push/subscribe').length === 1
    && JSON.parse(posts(en, '/push/subscribe')[0].opts.body).endpoint === 'https://web.push.apple.com/QTestDevice'
    && JSON.parse(posts(en, '/push/subscribe')[0].opts.body).keys.p256dh === 'BPub');
check('it confirms', en.el('toast').textContent === 'Notifications on', en.el('toast').textContent);

const refused = settings();
refused.enablePushSupport();
refused.notification.permission = 'denied';   // the prompt comes back denied
check('a refused prompt enables nothing', await refused.enablePush() === false);
check('and subscribes nothing', refused.push.subscribeCalls.length === 0 && posts(refused, '/push/subscribe').length === 0);
check('and says where to fix it', refused.el('toast').textContent.includes('blocked'), refused.el('toast').textContent);

const stale = settings({ noServer: true });
stale.enablePushSupport();
stale.notification.permission = 'granted';
check('an old server is reported, not subscribed against', await stale.enablePush() === false
  && stale.push.subscribeCalls.length === 0 && stale.el('toast').textContent.includes('update and restart'),
  stale.el('toast').textContent);

// ── disabling ───────────────────────────────────────────────────────────
const dis = settings();
dis.enablePushSupport({ subscribed: true });
check('disabling succeeds', await dis.disablePush() === true);
check('the server is told first, then the browser',
  posts(dis, '/push/unsubscribe').length === 1
    && JSON.parse(posts(dis, '/push/unsubscribe')[0].opts.body).endpoint === 'https://web.push.apple.com/QTestDevice'
    && dis.push.unsubscribeCalls === 1);
check('it confirms', dis.el('toast').textContent === 'Notifications off');

// ── the worker ──────────────────────────────────────────────────────────
const boot = settings();
boot.enablePushSupport();
boot.registerServiceWorker();
check('the worker is registered by a relative URL', JSON.stringify(boot.push.registered) === '["sw.js"]',
  JSON.stringify(boot.push.registered));
const nav = boot.push.swListeners.find((l) => l.type === 'message');
check('the app listens for the worker steering it', Boolean(nav));
nav.fn({ data: { type: 'navigate', hash: '#/t/alpha-task' } });
check('a tapped notification changes the route', boot.location.hash === '#/t/alpha-task', boot.location.hash);
nav.fn({ data: { type: 'other' } });
check('anything else from the worker is ignored', boot.location.hash === '#/t/alpha-task');

// ── the badge ───────────────────────────────────────────────────────────
const badge = settings();
badge.enablePushSupport();
const T = (name, status, needs_review) => ({ name, alias: 'aa', status, engine: 'claude', needs_review,
  created_at: '2026-01-01T00:00:00+00:00', status_changed_at: '2026-01-01T00:00:00+00:00' });
badge.state.tasks = [T('a', 'AGENT_FINISHED', true), T('b', 'NEEDS_ATTENTION', true), T('c', 'WORKING', false), T('d', 'DONE', true)];
badge.renderList();
check('the badge counts tasks waiting on you, not closed ones', badge.push.badges.at(-1) === 2, JSON.stringify(badge.push.badges));
badge.state.tasks = [T('c', 'WORKING', false)];
badge.renderList();
check('and is cleared when none are', badge.push.badges.at(-1) === 0, JSON.stringify(badge.push.badges));
const noBadge = settings();
noBadge.state.tasks = [T('a', 'AGENT_FINISHED', true)];
noBadge.renderList();
check('a browser without the badge API is left alone', noBadge.push.badges.length === 0);

// ── the key conversion ──────────────────────────────────────────────────
const bytes = settings().urlBase64ToUint8Array('BAE_-w');   // 0x04 0x01 0x3f 0xfb
check('base64url decodes to the right bytes', Array.from(bytes).join(',') === '4,1,63,251', Array.from(bytes).join(','));

report('push');
