/* Assertions for the plumbing every view shares.
 *
 * These are the pieces that were duplicated per view and are now written once,
 * which is exactly why they need covering: a mistake in one of them no longer
 * breaks a single screen, it breaks every screen at once.
 */

import { bootApp, checker, settle } from './harness.mjs';

const { check, report } = checker();

const TASK = {
  name: 'demo-task', alias: 'aa', status: 'AGENT_FINISHED', engine: 'claude',
  created_at: '2026-01-01T00:00:00+00:00',
  status_changed_at: '2026-01-01T00:00:00+00:00',
};

/** An app whose server says yes to everything. */
function willingServer() {
  const app = bootApp();
  app.setFetch(async (path) => {
    const json = (d) => ({ ok: true, status: 200, json: async () => d });
    if (path.includes('/tail')) return json({ entries: [] });
    if (path.startsWith('/tasks?')) return json({ tasks: [TASK] });
    if (path === '/config') return json({ config: { workdir: '/tmp' } });
    if (path === '/version') return json({ version: '1.0', commit: 'abc' });
    if (path.startsWith('/tasks/')) return json({ task: { ...TASK } });
    return json({});
  });
  return app;
}

// ── the shared back button ──────────────────────────────────────────────
// One handler now serves the conversation, the new-task form and settings.

for (const [view, render] of [
  ['the conversation', (app) => app.renderDetail('demo-task')],
  ['the new-task form', (app) => app.renderNew()],
  ['settings', (app) => app.renderConfig()],
]) {
  const app = willingServer();
  await render(app);
  await settle();
  check(`${view} renders a back button`, app.html().includes('id="back"'));
  app.location.hash = '#/somewhere-else';
  app.el('back').onclick();
  check(`back from ${view} returns to the list`, app.location.hash === '#/',
    `hash=${app.location.hash}`);
}

// ── the actions that are nothing but a POST ─────────────────────────────
// These were a map from each name to itself; losing one would drop it through
// to the unknown-action branch, which only says so in a toast.

const BARE = [
  'done', 'discard', 'undone', 'undiscard', 'unread',
  'pin', 'unpin', 'max', 'unmax', 'switch-backend',
];

for (const choice of BARE) {
  const app = willingServer();
  await app.runAction(choice, { ...TASK });
  await settle();
  const posted = app.fetches.some(
    (f) => (f.opts || {}).method === 'POST' && f.path === `/tasks/demo-task/${choice}`,
  );
  check(`${choice} posts to its own endpoint`, posted,
    `posts=${JSON.stringify(app.fetches.filter((f) => (f.opts || {}).method === 'POST')
      .map((f) => f.path))}`);
  check(`${choice} is not reported as unknown`,
    !app.el('toast').textContent.startsWith('Unknown action'),
    `toast=${app.el('toast').textContent}`);
}

const unknown = willingServer();
await unknown.runAction('not-an-action', { ...TASK });
await settle();
check('an action that really is unknown still says so',
  unknown.el('toast').textContent.startsWith('Unknown action'),
  `toast=${unknown.el('toast').textContent}`);

// ── ago(), which now shares its clock reading with the duration label ───
const app = bootApp();
check('a missing timestamp renders as nothing', app.ago(undefined) === '');
check('an empty timestamp renders as nothing', app.ago('') === '');
check('an unreadable timestamp renders as nothing', app.ago('not-a-date') === '',
  `got ${JSON.stringify(app.ago('not-a-date'))}`);
check('a future timestamp is clamped rather than going negative',
  app.ago(new Date(Date.now() + 60000).toISOString()) === '0s',
  `got ${app.ago(new Date(Date.now() + 60000).toISOString())}`);

const since = (secs) => new Date(Date.now() - secs * 1000).toISOString();
for (const [secs, want] of [[5, '5s'], [300, '5m'], [7200, '2h'], [172800, '2d']]) {
  check(`${secs}s ago renders as ${want}`, app.ago(since(secs)) === want,
    `got ${app.ago(since(secs))}`);
}

report('chrome');
