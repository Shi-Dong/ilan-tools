/* Assertions for the unsent reply kept per task.
 *
 * The task page is drawn from scratch on every visit and on refresh, Show
 * More and every ••• action, so text typed into the reply box and not sent
 * used to vanish the moment any of those happened. It is now kept per task on
 * the phone until it is sent or cleared.
 *
 * The stub keeps one element per id across renders, which a browser does not:
 * a redraw writes a fresh, empty textarea. Each "visit" below empties the box
 * first, so a draft only survives if the app put it back.
 */

import { bootApp, checker, settle } from './harness.mjs';

const { check, clickModal, report } = checker();

const TASKS = [
  { name: 'alpha-task', alias: 'aa', status: 'AGENT_FINISHED', engine: 'claude' },
  { name: 'beta-task', alias: 'ab', status: 'AGENT_FINISHED', engine: 'claude' },
  { name: 'closed-task', alias: 'ac', status: 'DONE', engine: 'claude' },
];
const ENTRIES = [{ role: 'assistant', content: 'Done.', timestamp: '2026-01-01T00:01:00+00:00' }];

function boot(opts) {
  const app = bootApp(opts);
  const posted = [];
  app.setFetch(async (path, o) => {
    const json = (d, status = 200) => ({ ok: status < 400, status, json: async () => d });
    if ((o || {}).method === 'POST') {
      posted.push({ path, body: JSON.parse((o || {}).body || '{}') });
      if (path.endsWith('/reply') && app.failSends) return json({ error: 'server said no' }, 500);
      return json({ ok: true, message: 'Reply sent' });
    }
    if (path.startsWith('/tasks?')) return json({ tasks: TASKS });
    if (path.includes('/tail')) return json({ entries: ENTRIES });
    const name = decodeURIComponent(path.split('/')[2].split('?')[0]);
    return json({ task: TASKS.find((t) => t.name === name) });
  });
  app.state.tasks = structuredClone(TASKS);
  return { app, posted };
}
/** Open a task's page as a fresh visit: the box starts empty, as a new textarea does. */
async function visit(app, name) {
  app.el('reply').value = '';
  await app.renderDetail(name);
  await settle();
}
function type(app, text) {
  app.el('reply').value = text;
  app.el('reply').oninput();
}

// ── leave and come back ─────────────────────────────────────────────────
const { app, posted } = boot();
await visit(app, 'alpha-task');
type(app, 'half a thought');
app.renderList();
await visit(app, 'alpha-task');
check('an unsent reply is there again after leaving and coming back',
  app.el('reply').value === 'half a thought', `box=${JSON.stringify(app.el('reply').value)}`);
check('and Send is live for it', app.el('send').disabled === false);

// ── a redraw of the same page keeps it too (refresh, Show More, ••• actions) ──
await visit(app, 'alpha-task');
check('a redraw of the page keeps it', app.el('reply').value === 'half a thought');

// ── per task ────────────────────────────────────────────────────────────
await visit(app, 'beta-task');
check('another task starts with its own, empty box', app.el('reply').value === '',
  `box=${JSON.stringify(app.el('reply').value)}`);
type(app, 'beta words');
await visit(app, 'alpha-task');
check('each task keeps its own draft', app.el('reply').value === 'half a thought');
await visit(app, 'beta-task');
check('including the second one', app.el('reply').value === 'beta words');

// ── it survives the app being closed and reopened ───────────────────────
const { app: again } = boot();
again.storage.set('ilan.drafts', app.storage.get('ilan.drafts'));
await visit(again, 'alpha-task');
check('it is kept on the phone, so a reopened app still has it',
  again.el('reply').value === 'half a thought', `stored=${app.storage.get('ilan.drafts')}`);

// ── sending forgets it ──────────────────────────────────────────────────
await visit(app, 'alpha-task');
await app.el('send').onclick();
await settle(); await settle();
check('it is sent as typed', posted.some((p) => p.path === '/tasks/alpha-task/reply' && p.body.message === 'half a thought'),
  JSON.stringify(posted));
await visit(app, 'alpha-task');
check('once sent it is gone', app.el('reply').value === '', `box=${JSON.stringify(app.el('reply').value)}`);
check('and gone from storage', !JSON.parse(app.storage.get('ilan.drafts') || '{}')['alpha-task']);

// ── a failed send keeps it ──────────────────────────────────────────────
await visit(app, 'alpha-task');
type(app, 'try again later');
app.failSends = true;
await app.el('send').onclick();
await settle();
app.failSends = false;
await visit(app, 'alpha-task');
check('a send the server refused keeps the draft', app.el('reply').value === 'try again later');

// ── Clear forgets it ────────────────────────────────────────────────────
app.el('clear-reply').onclick();
await visit(app, 'alpha-task');
check('Clear forgets it too', app.el('reply').value === '');

// ── whitespace is not a draft ───────────────────────────────────────────
type(app, '   ');
check('whitespace alone is not kept', !('alpha-task' in JSON.parse(app.storage.get('ilan.drafts') || '{}')),
  app.storage.get('ilan.drafts'));

// ── an "Ask about this" quote is part of the draft ──────────────────────
await visit(app, 'alpha-task');
app.state.askSelection = 'the line in question';
app.askAboutSelection();
await visit(app, 'alpha-task');
check('a quoted selection is kept like typed text', app.el('reply').value.includes('the line in question'),
  `box=${JSON.stringify(app.el('reply').value)}`);

// ── marking the task done drops it ──────────────────────────────────────
// Done means finished with the task, so a reply half-written to it is moot —
// from the card's Done button and from the ••• sheet alike.
const stored = (a) => JSON.parse(a.storage.get('ilan.drafts') || '{}');
await visit(app, 'beta-task');
type(app, 'about to be done');
app.renderList();
app.doneBtn('beta-task').onclick();
await settle();
clickModal(app, '#mo', 'Done must open a confirmation that can be accepted');
await settle(); await settle();
check('Done on the card drops the draft', !('beta-task' in stored(app)), JSON.stringify(stored(app)));

await visit(app, 'alpha-task');
type(app, 'done from the sheet');
app.showActions({ ...TASKS[0] });
await settle();
clickModal(app, '[data-value="done"]', 'the sheet must offer Mark done');
await settle(); await settle();
check('Mark done on the ••• sheet drops it too', !('alpha-task' in stored(app)), JSON.stringify(stored(app)));

// Declining the confirmation is not marking it done.
await visit(app, 'beta-task');
type(app, 'keep me');
app.renderList();
app.doneBtn('beta-task').onclick();
await settle();
clickModal(app, '#mc', 'Done must open a confirmation that can be declined');
await settle();
check('declining Done keeps the draft', stored(app)['beta-task'] === 'keep me', JSON.stringify(stored(app)));

// Other ••• actions are not done, and keep it.
await visit(app, 'alpha-task');
type(app, 'pinned, not done');
app.showActions({ ...TASKS[0] });
await settle();
clickModal(app, '[data-value="pin"]', 'the sheet must offer Pin');
await settle(); await settle();
check('another ••• action keeps the draft', stored(app)['alpha-task'] === 'pinned, not done', JSON.stringify(stored(app)));

// ── storage that refuses is not an error ────────────────────────────────
const { app: priv } = boot({ storage: 'denied' });
let threw = false;
try {
  await visit(priv, 'alpha-task');
  type(priv, 'private browsing');
} catch { threw = true; }
check('with storage refused, typing still works', !threw && priv.el('reply').value === 'private browsing');

report('reply-draft');
