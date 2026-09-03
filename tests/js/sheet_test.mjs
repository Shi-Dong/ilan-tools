/* Assertions for the ••• sheet on a task's page, and the Sleep sheet under it.
 *
 * Nothing pinned the sheet's contents before: the dialog stub could not see
 * its options, so every entry that was ever added to it went untested. Now that
 * it can, the list is asserted exactly — an entry coming back is as much a
 * regression as one going missing, since the whole point of the trim was that
 * there were too many to choose from.
 *
 * Sleep is the one entry that opens a second sheet, and that sheet is a fixed
 * list rather than a text field: every value it offers is one the server takes,
 * so there is no unparseable input left to handle.
 */

import { bootApp, checker, settle } from './harness.mjs';

const { check, clickModal, report } = checker();

const OPEN = { name: 'live-task', alias: 'aa', status: 'AGENT_FINISHED', engine: 'claude',
  pinned: false, model: null, gist_url: null };
const values = (app) => app.modalOptions().map((o) => o.value);
const labels = (app) => app.modalOptions().map((o) => o.label);

// ── what the sheet offers an open task ──────────────────────────────────
const a = bootApp();
a.showActions(OPEN);
await settle();
check('the sheet opens', a.modalOpen());
check('an open task is offered exactly these, in this order',
  JSON.stringify(values(a)) === JSON.stringify(
    ['tap', 'cancel', 'sleep', 'done', 'pin', 'max', 'switch-backend', 'rename', 'branch', 'delete']),
  JSON.stringify(values(a)));
for (const gone of ['replyEvery', 'unread', 'alias', 'discard']) {
  check(`${gone} is no longer offered`, !values(a).includes(gone));
}
for (const gone of ['Reply every…', 'Mark unread', 'Set alias…', 'Discard']) {
  check(`"${gone}" is no longer on the sheet`, !labels(a).includes(gone));
}
check('only Delete is marked dangerous on a task that is not running',
  JSON.stringify(a.modalOptions().filter((o) => o.danger).map((o) => o.value)) === '["delete"]');
clickModal(a, '[data-value=""]', 'the sheet must be dismissable');
check('cancelling closes it', !a.modalOpen());

// ── a running task adds Kill, a closed one swaps the top for its way back ──
const w = bootApp();
w.showActions({ ...OPEN, status: 'WORKING' });
await settle();
check('a WORKING task is offered Kill, as dangerous',
  w.modalOptions().some((o) => o.value === 'kill' && o.danger));
const c = bootApp();
c.showActions({ ...OPEN, status: 'DISCARDED' });
await settle();
check('a closed task is offered its way back and none of the live-only entries',
  values(c)[0] === 'undiscard' && !values(c).some((v) => ['tap', 'cancel', 'sleep', 'done'].includes(v)),
  JSON.stringify(values(c)));
check('and none of the three removed entries either',
  !values(c).some((v) => ['replyEvery', 'unread', 'alias', 'discard'].includes(v)));

// ── Sleep opens a fixed choice ──────────────────────────────────────────
const s = bootApp();
// After a successful sleep the page re-renders itself, which reads the task
// and its tail back, so the stub has to answer those too.
const answering = async (path) => {
  const json = (d) => ({ ok: true, status: 200, json: async () => d });
  if (path.includes('/tail')) return json({ entries: [] });
  if (path === '/tasks/live-task') return json({ task: OPEN });
  return json({ ok: true });
};
s.setFetch(answering);
s.showActions(OPEN);
await settle();
clickModal(s, '[data-value="sleep"]', 'Sleep must be on the sheet');
await settle();
check('choosing Sleep opens a second sheet', s.modalOpen());
check('it has no text field — the duration is chosen, not typed', !s.modalHasField(),
  'a free-text duration prompt is back');
check('it offers exactly six durations, in ascending order',
  JSON.stringify(labels(s)) === JSON.stringify(['15m', '30m', '1h', '2h', '4h', '8h']),
  JSON.stringify(labels(s)));
check('each is posted as seconds',
  JSON.stringify(values(s)) === JSON.stringify(['900', '1800', '3600', '7200', '14400', '28800']),
  JSON.stringify(values(s)));
check('none of them is dangerous', s.modalOptions().every((o) => !o.danger));

clickModal(s, '[data-value="3600"]', 'the Sleep sheet must offer 1h');
await settle(); await settle();
const sleeps = s.fetches.filter((f) => f.path.endsWith('/sleep'));
check('picking 1h posts one sleep', sleeps.length === 1, `${sleeps.length} sleep posts`);
check('to the task the sheet was opened for', sleeps[0]?.path === '/tasks/live-task/sleep');
check('with the chosen duration in seconds',
  JSON.parse(sleeps[0]?.opts?.body || '{}').seconds === 3600, sleeps[0]?.opts?.body);
check('the confirmation names the duration',
  s.el('toast').textContent === `Sleeping for ${s.formatCompactDuration(3600)}`,
  `toast=${s.el('toast').textContent}`);

// ── cancelling the duration sheet sleeps nothing ────────────────────────
const n = bootApp();
n.setFetch(answering);
n.showActions(OPEN);
await settle();
clickModal(n, '[data-value="sleep"]', 'Sleep must be on the sheet');
await settle();
clickModal(n, '[data-value=""]', 'the Sleep sheet must be cancellable');
await settle();
check('cancelling posts nothing', !n.fetches.some((f) => f.path.endsWith('/sleep')));
check('and closes the sheet', !n.modalOpen());

report('sheet');
