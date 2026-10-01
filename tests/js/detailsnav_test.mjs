/* Assertions for getting to a task's page, and getting back out.
 *
 * Tapping Details sometimes did nothing at all. Two faults stacked up. A
 * request that could not reach the server threw rather than answering, so the
 * rejection escaped through route() and no view was ever written — the app sat
 * on the list while the address bar already said `#/t/<name>`. From there the
 * same button was dead for good: assigning a hash that is already in the
 * address bar fires no hashchange, so the router never ran again.
 *
 * Both halves are pinned here: a dropped request has to end somewhere the user
 * can act on, and a tap has to route even when the hash already matches.
 */

import { bootApp, checker, settle } from './harness.mjs';

const { check, report } = checker();

const TASKS = [
  { name: 'alpha-task', alias: 'aa', status: 'AGENT_FINISHED', engine: 'claude',
    summary_one_liner: 'Ran four seeds.',
    created_at: '2026-01-01T00:00:00+00:00', status_changed_at: '2026-01-01T00:00:00+00:00' },
  { name: 'beta-task', alias: 'ab', status: 'WORKING', engine: 'claude',
    created_at: '2026-01-02T00:00:00+00:00', status_changed_at: '2026-01-02T00:00:00+00:00' },
];
const ENTRIES = [
  { role: 'user', content: 'Try four seeds.', timestamp: '2026-01-01T00:00:00+00:00' },
  { role: 'assistant', content: 'Done.', timestamp: '2026-01-01T00:01:00+00:00' },
];

/** A list, rendered, with a server that answers everything. */
function listed(fetchImpl) {
  const app = bootApp();
  app.setFetch(fetchImpl || (async (path) => {
    const json = (d) => ({ ok: true, status: 200, json: async () => d });
    if (path.includes('/tail')) return json({ entries: ENTRIES });
    if (path.startsWith('/tasks?')) return json({ tasks: TASKS });
    return json({ task: TASKS[0] });
  }));
  app.state.tasks = structuredClone(TASKS);
  app.renderList();
  return app;
}
const onDetail = (app) => app.html().includes('id="reply"') || app.html().includes('msg-body');

/** Tap Details the way a browser handles it: the handler assigns the hash, and
 * the browser's hashchange listener runs the router. The stub location fires
 * no events, so that second half is done here — except when the handler
 * routed by itself, which is the whole point of the hash-already-matches
 * case below. */
async function tapDetails(app, name) {
  const before = app.location.hash;
  app.detailsBtn(name).onclick();
  if (app.location.hash !== before) await app.route();
  await settle(); await settle();
}

// ── a dropped request answers instead of throwing ───────────────────────
// fetch rejects when the phone cannot reach the server at all. Before, that
// rejection travelled out of api.get, out of renderDetail and out of route(),
// leaving nothing rendered.
const dropped = listed(async (path) => {
  if (path.startsWith('/tasks?')) return { ok: true, status: 200, json: async () => ({ tasks: TASKS }) };
  throw new TypeError('Load failed');
});
let threw = false;
try {
  await tapDetails(dropped, 'alpha-task');
} catch {
  threw = true;
}
check('a dropped request does not throw out of the tap', !threw,
  'the rejection escaped the router again');
check('it says the server could not be reached',
  dropped.html().includes('Cannot reach the ilan server.'), dropped.html().slice(0, 200));
check('and offers a way back, so the app is never stranded',
  dropped.html().includes("location.hash='#/'"), dropped.html().slice(0, 200));

// ── the hash already points at the task ─────────────────────────────────
// This is where the app lands after any failed render, and it is what made
// the button dead: no hashchange fires, so only routing directly can save it.
const stale = listed();
stale.location.hash = '#/t/alpha-task';
const before = stale.fetches.length;
await tapDetails(stale, 'alpha-task');
check('tapping Details still opens the task when the hash already matches',
  onDetail(stale), stale.html().slice(0, 200));
check('and it really asked the server for it', stale.fetches.length > before,
  `${stale.fetches.length - before} request(s) issued`);

// ── the ordinary path is unchanged ──────────────────────────────────────
const plain = listed();
plain.detailsBtn('beta-task').onclick();
await settle(); await settle();
check('a normal tap sets the hash', plain.location.hash === '#/t/beta-task', plain.location.hash);

// ── the page says it is loading ─────────────────────────────────────────
// Two requests have to land before a conversation can be drawn. With nothing
// on screen until they do, a slow server is indistinguishable from a button
// that did nothing.
let release;
const slow = listed(async (path) => {
  if (path.startsWith('/tasks?')) return { ok: true, status: 200, json: async () => ({ tasks: TASKS }) };
  await new Promise((r) => { release = r; });
  return { ok: true, status: 200, json: async () => ({ task: TASKS[0], entries: ENTRIES }) };
});
slow.detailsBtn('alpha-task').onclick();
slow.route();
await settle();
check('the task page shows it is loading while the server is slow',
  slow.html().includes('Loading'), slow.html().slice(0, 200));
check('and names the task it is opening', slow.html().includes('alpha-task'), slow.html().slice(0, 200));
if (release) release();

report('details-navigation');
