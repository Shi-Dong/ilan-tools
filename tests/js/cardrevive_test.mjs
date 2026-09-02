/* Assertions for reopening a closed task from its card on the list.
 *
 * Closed tasks reach the list through a search (or a pin), and reopening one
 * used to mean opening its conversation first. The card now carries the same
 * button its bottom bar does, in the place Tap and Done occupy on an open card.
 *
 * The stub server applies the real precondition — undone only for DONE,
 * undiscard only for DISCARDED, 409 otherwise — so the endpoint each label
 * posts to is exercised rather than assumed, and it serves the list re-read
 * that follows a success so the card the user ends up looking at is asserted.
 */

import { bootApp, checker, settle } from './harness.mjs';

const { check, report } = checker();

const REQUIRED_STATUS = { undone: 'DONE', undiscard: 'DISCARDED' };

function listWith(tasks) {
  const app = bootApp();
  const state = Object.fromEntries(tasks.map((t) => [t.name, { ...t }]));
  app.setFetch(async (path, opts) => {
    const json = (d, status = 200) => ({ ok: status < 400, status, json: async () => d });
    if (path.startsWith('/tasks?')) return json({ tasks: Object.values(state) });
    const parts = path.split('/').filter(Boolean);
    const name = decodeURIComponent(parts[1] || ''), tail = parts[2] || '';
    if ((opts || {}).method === 'POST' && REQUIRED_STATUS[tail]) {
      const task = state[name];
      if (task.status !== REQUIRED_STATUS[tail]) {
        return json({ error: `Task is ${task.status}, not ${REQUIRED_STATUS[tail]}` }, 409);
      }
      task.status = 'NEEDS_ATTENTION';
      return json({ ok: true, name });
    }
    return json({ ok: true });
  });
  app.state.tasks = structuredClone(tasks);
  app.state.expanded = new Set(tasks.map((t) => t.name));
  // A search is how closed tasks reach the list.
  app.state.query = 'task'; app.state.draft = 'task';
  app.renderList();
  return { app, state };
}

const T = (name, status, extra = {}) => ({
  name, alias: 'aa', status, engine: 'claude',
  created_at: '2026-01-01T00:00:00+00:00', status_changed_at: '2026-01-01T00:00:00+00:00',
  ...extra,
});
const posts = (app, suffix) => app.fetches.filter(
  (f) => (f.opts || {}).method === 'POST' && f.path.endsWith(suffix));
const listReads = (app) => app.fetches.filter((f) => f.path.startsWith('/tasks?')).length;
const card = (app, name) => {
  const m = app.html().match(new RegExp(
    `<div class="card [^"]*">(?:(?!<div class="card )[\\s\\S])*?data-toggle="${name}"[\\s\\S]*?<div class="row-actions">[\\s\\S]*?</div>`));
  return m ? m[0] : '';
};

// ── what each kind of card offers ───────────────────────────────────────
const { app, state } = listWith([
  T('done-task', 'DONE'), T('dropped-task', 'DISCARDED'), T('live-task', 'AGENT_FINISHED'),
]);

check('a DONE card offers Undone', card(app, 'done-task').includes('data-revive="done-task"')
  && card(app, 'done-task').includes('<span>Undone</span>'), card(app, 'done-task'));
check('a DISCARDED card offers Undiscard',
  card(app, 'dropped-task').includes('data-revive="dropped-task"')
  && card(app, 'dropped-task').includes('<span>Undiscard</span>'), card(app, 'dropped-task'));
check('neither closed card offers Tap or Done',
  !/data-(tap|done)=/.test(card(app, 'done-task') + card(app, 'dropped-task')));
check('a live card offers Tap and Done and no way back',
  /data-tap=/.test(card(app, 'live-task')) && /data-done=/.test(card(app, 'live-task'))
  && !card(app, 'live-task').includes('data-revive='));
check('every card still offers Details',
  ['done-task', 'dropped-task', 'live-task'].every((n) => card(app, n).includes(`data-details="${n}"`)));
check('the way back sits before Details, where Tap would be',
  /data-revive="done-task"[\s\S]*?data-details="done-task"/.test(card(app, 'done-task')));
check('it carries the undo glyph', card(app, 'done-task').includes('<use href="#i-undo">'));
check('it is styled as a quiet action, not the filled one',
  card(app, 'done-task').includes('class="act act-revive"'));

// ── reopening a DONE task ───────────────────────────────────────────────
const reads0 = listReads(app);
app.reviveBtn('done-task').onclick();
await settle(); await settle();
check('no confirmation is asked — reopening is not destructive', !app.modalOpen());
check('it posts to undone, once', posts(app, '/undone').length === 1
  && posts(app, '/undone')[0].path === '/tasks/done-task/undone',
  JSON.stringify(posts(app, '/undone').map((f) => f.path)));
check('it names the task in the toast, as code',
  app.el('toast').textContent === 'Reopened done-task'
  && app.el('toast').innerHTML.includes('<code>done-task</code>'),
  `toast=${app.el('toast').innerHTML}`);
check('the list is reloaded straight after', listReads(app) === reads0 + 1,
  `list reads ${reads0} -> ${listReads(app)}`);
check('the reopened card now offers Tap and Done',
  /data-tap="done-task"/.test(card(app, 'done-task')) && !card(app, 'done-task').includes('data-revive='),
  card(app, 'done-task'));
check('the card was not toggled by the click',
  !card(app, 'done-task').includes(' collapsed"'));

// ── reopening a DISCARDED task posts to the other endpoint ──────────────
app.reviveBtn('dropped-task').onclick();
await settle(); await settle();
check('a DISCARDED task posts to undiscard', posts(app, '/undiscard').length === 1
  && posts(app, '/undiscard')[0].path === '/tasks/dropped-task/undiscard');
check('and never to undone', posts(app, '/undone').length === 1);
check('it is reopened too', /data-tap="dropped-task"/.test(card(app, 'dropped-task')));

// ── a refusal changes nothing on screen ─────────────────────────────────
const { app: stale, state: staleState } = listWith([T('gone-task', 'DONE')]);
staleState['gone-task'].status = 'WORKING';   // changed underneath, as another device would
const reads1 = listReads(stale);
stale.reviveBtn('gone-task').onclick();
await settle(); await settle();
check('the refusal is shown', stale.el('toast').textContent.includes('not DONE'),
  `toast=${stale.el('toast').textContent}`);
check('a refused reopen does not reload the list', listReads(stale) === reads1,
  `list reads ${reads1} -> ${listReads(stale)}`);
check('and the card still offers its way back', stale.html().includes('data-revive="gone-task"'));

// ── the search that surfaced the card survives the reload ───────────────
check('the query is still in the box after reopening', app.html().includes('value="task"'));

report('card-revive');
