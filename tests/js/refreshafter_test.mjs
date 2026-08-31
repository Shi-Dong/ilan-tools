/* Assertions for reloading the list once a task has been changed.
 *
 * Tapping a card sends a real message and flips the task to WORKING, but the
 * card it was sent from went on showing the old status until the next poll —
 * up to fifteen seconds of the app looking like it had ignored the tap.
 *
 * Two halves are worth pinning: that a change on the list reloads it, and that
 * nothing reloads it when nothing changed. The second is the one that would
 * otherwise turn every declined dialog into a wasted round trip.
 */

import { bootApp, checker, settle } from './harness.mjs';

const { check, clickModal, report } = checker();

const TASKS = [
  { name: 'alpha-task', alias: 'aa', status: 'AGENT_FINISHED', engine: 'claude',
    created_at: '2026-01-01T00:00:00+00:00', status_changed_at: '2026-01-01T00:00:00+00:00' },
  { name: 'beta-task', alias: 'ab', status: 'AGENT_FINISHED', engine: 'codex',
    created_at: '2026-01-02T00:00:00+00:00', status_changed_at: '2026-01-02T00:00:00+00:00' },
];

/** An app on the list, whose server answers `reply` with *replyResponse*. */
function onTheList(replyResponse = { ok: true, status: 200 }) {
  const app = bootApp();
  app.state.tasks = structuredClone(TASKS);
  app.state.canned = { tap: 'CANNED TAP', cancel: 'CANNED CANCEL' };
  app.setFetch(async (path) => {
    const json = (d, ok = true, status = 200) => ({ ok, status, json: async () => d });
    if (path.startsWith('/tasks?')) return json({ tasks: TASKS });
    if (path.endsWith('/reply')) {
      return json(replyResponse.body ?? { message: 'Reply sent' },
                  replyResponse.ok, replyResponse.status);
    }
    return json({ ok: true });
  });
  app.renderList();
  return app;
}

const listReads = (app) => app.fetches.filter((f) => f.path.startsWith('/tasks?')).length;

// ── a tap reloads the list ──────────────────────────────────────────────
const tapped = onTheList();
const before = listReads(tapped);
tapped.tapBtn('alpha-task').onclick();
await settle();
clickModal(tapped, '#mo', 'Tap must open a confirmation that can be accepted');
await settle();
await settle();

check('the tap was actually sent',
  tapped.fetches.some((f) => f.path === '/tasks/alpha-task/reply'));
check('and the list is reloaded straight after',
  listReads(tapped) === before + 1,
  `list reads went ${before} -> ${listReads(tapped)}`);

// No timer is involved: the server has already stored the new status by the
// time it answers, so the reload happens on the response rather than later.
check('the reload does not wait on a timer',
  listReads(tapped) === before + 1,
  'the list was only refreshed after settling a timer');

// ── nothing changed, nothing reloaded ───────────────────────────────────
const declined = onTheList();
const declinedBefore = listReads(declined);
declined.tapBtn('alpha-task').onclick();
await settle();
clickModal(declined, '#mc', 'Tap must open a confirmation that can be declined');
await settle();
await settle();
check('declining sends nothing',
  !declined.fetches.some((f) => f.path.endsWith('/reply')));
check('and does not reload the list', listReads(declined) === declinedBefore,
  `list reads went ${declinedBefore} -> ${listReads(declined)}`);

const refused = onTheList({ ok: false, status: 409, body: { error: 'Task is DONE' } });
const refusedBefore = listReads(refused);
refused.tapBtn('alpha-task').onclick();
await settle();
clickModal(refused, '#mo', 'Tap must open a confirmation that can be accepted');
await settle();
await settle();
check('a refused tap is reported', refused.el('toast').textContent === 'Task is DONE',
  refused.el('toast').textContent);
check('a refused tap does not reload the list', listReads(refused) === refusedBefore,
  `list reads went ${refusedBefore} -> ${listReads(refused)}`);

// ── closing a task still reloads ────────────────────────────────────────
const closed = onTheList();
const closedBefore = listReads(closed);
closed.doneBtn('alpha-task').onclick();
await settle();
clickModal(closed, '#mo', 'Done must open a confirmation that can be accepted');
await settle();
await settle();
check('closing a task reloads the list too', listReads(closed) === closedBefore + 1,
  `list reads went ${closedBefore} -> ${listReads(closed)}`);

// ── off the list, it is a no-op ─────────────────────────────────────────
// Every other view re-renders itself, so a reload here would fetch a list
// nobody is looking at.
const away = onTheList();
away.location.hash = '#/t/alpha-task';
const awayBefore = listReads(away);
await away.refreshListAfterChange();
check('nothing is fetched while another view is on screen',
  listReads(away) === awayBefore,
  `list reads went ${awayBefore} -> ${listReads(away)}`);

// ── and the way back from a conversation reloads it anyway ──────────────
// This is what makes the no-op above safe, so it is asserted rather than
// assumed: a task changed from the conversation is fresh when the list
// returns, because route() loads it on the way in.
const back = bootApp();
back.setFetch(async (path) => {
  const json = (d) => ({ ok: true, status: 200, json: async () => d });
  if (path.startsWith('/tasks?')) return json({ tasks: TASKS });
  if (path.includes('/tail')) return json({ entries: [] });
  return json({ task: TASKS[0] });
});
back.location.hash = '#/';
// Seeded on purpose. Returning to the list always has tasks in memory from
// the last visit, and the failure being guarded against is a router that
// re-renders those instead of refetching — which an empty starting state
// cannot tell apart from a correct one.
back.state.tasks = structuredClone(TASKS);
const backBefore = listReads(back);
await back.route();
await settle();
check('routing to the list fetches it even with tasks already in memory',
  listReads(back) === backBefore + 1,
  `list reads went ${backBefore} -> ${listReads(back)}`);

// ── returning from a task reloads the list ──────────────────────────────
// Three separate things have to hold, and each is checked on its own: the
// back control points at the list, the router is subscribed to the event that
// firing it produces, and routing to the list fetches it. The middle one is
// the join, and it is the one nothing was checking — a test can press back and
// a test can call route(), and both pass while the two are not connected.

const nav = bootApp();
nav.setFetch(async (path) => {
  const json = (d) => ({ ok: true, status: 200, json: async () => d });
  if (path.startsWith('/tasks?')) return json({ tasks: TASKS });
  if (path.includes('/tail')) return json({ entries: [] });
  return json({ task: TASKS[0] });
});

await nav.renderDetail('alpha-task');
await settle();
nav.location.hash = '#/t/alpha-task';
nav.el('back').onclick();
check('the conversation back button points at the list',
  nav.location.hash === '#/', `hash=${nav.location.hash}`);

const wired = nav.listeners().filter((l) => l.on === 'window' && l.type === 'hashchange');
check('the router is subscribed to hashchange', wired.length === 1,
  `${wired.length} hashchange listeners`);
check('and what it subscribed is the router itself',
  wired.length === 1 && wired[0].fn === nav.route,
  'something other than route() handles navigation');

// The same wiring serves the browser's own Back, which produces the same
// event — there is no separate path for it to miss.
const others = nav.listeners().filter((l) => l.type === 'popstate');
check('nothing depends on a second navigation event', others.length === 0);

report('refresh-after-change');
