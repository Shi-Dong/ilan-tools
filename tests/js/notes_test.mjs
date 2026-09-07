/* Assertions for the note on a card and the button that edits it.
 *
 * `ilan notes` gives a task a line the *user* writes — what the task is for —
 * beside the summary the server generates of what the agent last did. On the
 * list the note sits under that summary on an expanded card, and a Note button
 * in the actions row edits it: the card body is itself a button, so the
 * control cannot sit on the line it edits.
 *
 * The stub server applies the real rules: it strips the note, refuses one over
 * the limit in the server's own words, stores an empty note as none, and
 * serves the list re-read that follows a save so the card the user ends up
 * looking at is what is asserted.
 */

import { bootApp, checker, settle } from './harness.mjs';

const { check, clickModal, report } = checker();

const LIMIT = 128;

function listWith(tasks) {
  const app = bootApp();
  const state = Object.fromEntries(tasks.map((t) => [t.name, { ...t }]));
  const posted = [];
  app.setFetch(async (path, opts) => {
    const json = (d, status = 200) => ({ ok: status < 400, status, json: async () => d });
    if (path.startsWith('/tasks?')) return json({ tasks: Object.values(state) });
    const parts = path.split('/').filter(Boolean);
    const name = decodeURIComponent(parts[1] || ''), tail = parts[2] || '';
    if ((opts || {}).method === 'POST' && tail === 'notes') {
      const body = JSON.parse(opts.body);
      posted.push({ name, body });
      const task = state[name];
      if (!task) return json({ error: `No task named ${name}` }, 404);
      const note = String(body.notes || '').trim();
      if (note.length > LIMIT) {
        return json({ error: `Note is ${note.length} characters; the limit is ${LIMIT}. `
          + 'Shorten it, or keep the detail in the conversation itself.' }, 400);
      }
      task.notes = note || null;
      return json({ ok: true, name, notes: task.notes });
    }
    return json({ ok: true });
  });
  app.state.tasks = structuredClone(tasks);
  app.state.expanded = new Set(tasks.map((t) => t.name));
  // A search is how closed tasks reach the list.
  app.state.query = 'task'; app.state.draft = 'task';
  app.renderList();
  return { app, state, posted };
}

const T = (name, status, extra = {}) => ({
  name, alias: 'aa', status, engine: 'claude', summary_one_liner: `${name} summary`,
  created_at: '2026-01-01T00:00:00+00:00', status_changed_at: '2026-01-01T00:00:00+00:00',
  ...extra,
});
/** One card's markup, from its opening tag through its actions row. */
const card = (app, name) => {
  const m = app.html().match(new RegExp(
    `<div class="card [^"]*">(?:(?!<div class="card )[\\s\\S])*?data-toggle="${name}"[\\s\\S]*?<div class="row-actions">[\\s\\S]*?</div>`));
  return m ? m[0] : '';
};
/** The card's body button alone — what a tap on the card toggles with. */
const body = (app, name) => {
  const m = card(app, name).match(/<button class="row"[\s\S]*?<\/button>/);
  return m ? m[0] : '';
};
const listReads = (app) => app.fetches.filter((f) => f.path.startsWith('/tasks?')).length;
const toastText = (app) => app.el('toast').textContent;
/** Press a card's Note button, reporting rather than throwing if none is wired.
 *
 * A card without the button hands back an element with no handler, and calling
 * that dies with a TypeError before a single assertion is reported — which says
 * far less than the checks that were about to run.
 */
function pressNote(app, name) {
  const btn = app.notesBtn(name);
  if (typeof btn.onclick !== 'function') {
    check(`${name} has a Note button to press`, false, 'no Note button is wired on that card');
    return;
  }
  btn.onclick();
}

// ── where the note is shown ─────────────────────────────────────────────
const { app, posted } = listWith([
  T('noted-task', 'AGENT_FINISHED', { notes: 'why this exists' }),
  T('bare-task', 'WORKING'),
  T('closed-task', 'DONE', { notes: 'closed <b>&</b> noted' }),
]);

check('a note is rendered on its card',
  body(app, 'noted-task').includes('<span class="row-notes">why this exists</span>'),
  body(app, 'noted-task'));
check('it sits under the summary and above the status line',
  /class="row-sum">[\s\S]*?class="row-notes">[\s\S]*?class="row-meta">/.test(body(app, 'noted-task')),
  'the note moved out from between the summary and the status');
check('a task without a note shows no note line', !card(app, 'bare-task').includes('row-notes'),
  'an empty note line is drawn on a card that has no note');
check('the note is escaped',
  body(app, 'closed-task').includes('closed &lt;b&gt;&amp;&lt;/b&gt; noted')
  && !body(app, 'closed-task').includes('<b>'), body(app, 'closed-task'));

// ── the button that edits it ────────────────────────────────────────────
// Offered on every card, closed ones included: writing down what a finished
// task was about is exactly the case the command exists for.
for (const n of ['noted-task', 'bare-task', 'closed-task']) {
  check(`${n} offers a Note button`, card(app, n).includes(`data-notes="${n}"`), card(app, n));
}
check('it is labelled Note', card(app, 'bare-task').includes('<span>Note</span>'));
check('it carries the pencil glyph',
  /data-notes="bare-task">\s*<svg class="ico" aria-hidden="true"><use href="#i-pencil">/
    .test(card(app, 'bare-task')), card(app, 'bare-task'));
check('it is a quiet action, not the filled one',
  card(app, 'bare-task').includes('class="act act-notes"'));
check('on a live card it follows Tap and Done and precedes Details',
  /data-tap="bare-task"[\s\S]*?data-done="bare-task"[\s\S]*?data-notes="bare-task"[\s\S]*?data-details="bare-task"/
    .test(card(app, 'bare-task')), card(app, 'bare-task'));
check('on a closed card it follows the way back and precedes Details',
  /data-revive="closed-task"[\s\S]*?data-notes="closed-task"[\s\S]*?data-details="closed-task"/
    .test(card(app, 'closed-task')), card(app, 'closed-task'));

// ── editing opens on the current note ───────────────────────────────────
pressNote(app, 'noted-task');
await settle();
check('a sheet opens', app.modalOpen());
check('it is titled for the task', app.modalTitle() === 'Note for noted-task',
  `title=${app.modalTitle()}`);
check('it carries a text field', app.modalHasField());
check('the field is a textarea, so the whole note is in view while it is edited',
  app.modalHtml().includes('<textarea class="field" id="mv"'), app.modalHtml());
check('it opens on the current note', app.modalHtml().includes('>why this exists</textarea>'),
  app.modalHtml());
check('the field stops at the server\'s limit', app.modalHtml().includes(` maxlength="${LIMIT}"`),
  app.modalHtml());
check('the confirming button says Save', app.modalHtml().includes('id="mo">Save<'), app.modalHtml());
check('nothing is sent before the sheet is confirmed', posted.length === 0);
check('the card was not toggled by the tap', !card(app, 'noted-task').includes(' collapsed"'));

// ── saving ──────────────────────────────────────────────────────────────
const reads0 = listReads(app);
app.modal('#mv').value = '  now about something else  ';
clickModal(app, '#mo', 'the note sheet must be saveable');
await settle(); await settle();
check('it posts to the notes route, once', posted.length === 1
  && app.fetches.some((f) => f.path === '/tasks/noted-task/notes' && (f.opts || {}).method === 'POST'),
  JSON.stringify(posted));
check('the note is trimmed before it is sent',
  posted[0] && posted[0].body.notes === 'now about something else', JSON.stringify(posted[0]));
check('it is a plain replace, not an append', posted[0] && !posted[0].body.append);
check('the toast names the task, as code',
  toastText(app) === 'Note saved for noted-task'
  && app.el('toast').innerHTML.includes('<code>noted-task</code>'),
  `toast=${app.el('toast').innerHTML}`);
check('the list is reloaded straight after', listReads(app) === reads0 + 1,
  `list reads ${reads0} -> ${listReads(app)}`);
check('the card now shows the new note',
  body(app, 'noted-task').includes('<span class="row-notes">now about something else</span>'),
  body(app, 'noted-task'));
check('the sheet is closed', !app.modalOpen());
check('the search that surfaced the cards survives the reload', app.html().includes('value="task"'));

// ── cancelling, and saving without a change, send nothing ───────────────
const sent = posted.length;
pressNote(app, 'bare-task');
await settle();
app.modal('#mv').value = 'typed, then abandoned';
clickModal(app, '#mc', 'the note sheet must be cancellable');
await settle(); await settle();
check('cancelling sends nothing', posted.length === sent, `${posted.length - sent} request(s)`);
check('and the card is unchanged', !card(app, 'bare-task').includes('row-notes'));

pressNote(app, 'noted-task');
await settle();
app.modal('#mv').value = 'now about something else';
clickModal(app, '#mo', 'the note sheet must be saveable');
await settle(); await settle();
check('saving the note unchanged sends nothing', posted.length === sent,
  `${posted.length - sent} request(s) for a note that did not change`);

// ── a first note, on a card that had none ───────────────────────────────
pressNote(app, 'bare-task');
await settle();
check('a task without a note opens on an empty field',
  app.modalHtml().includes('"></textarea>'), app.modalHtml());
check('and says what goes there', app.modalHtml().includes('placeholder="What is this task about?"'),
  app.modalHtml());
app.modal('#mv').value = 'first note';
clickModal(app, '#mo', 'the note sheet must be saveable');
await settle(); await settle();
check('the first note appears on the card',
  body(app, 'bare-task').includes('<span class="row-notes">first note</span>'), body(app, 'bare-task'));

// ── clearing is saving an empty note ────────────────────────────────────
pressNote(app, 'noted-task');
await settle();
app.modal('#mv').value = '   ';
clickModal(app, '#mo', 'the note sheet must be saveable');
await settle(); await settle();
const last = posted[posted.length - 1];
check('an emptied note is sent as empty, which the server reads as clear',
  last && last.name === 'noted-task' && last.body.notes === '', JSON.stringify(last));
check('the toast says cleared', toastText(app) === 'Note cleared for noted-task',
  `toast=${toastText(app)}`);
check('the note line is gone from the card', !card(app, 'noted-task').includes('row-notes'),
  card(app, 'noted-task'));

// ── a refusal changes nothing on screen ─────────────────────────────────
// The field's maxlength stops this in a browser; the stub has no such field,
// so this is the server's own refusal reaching the user.
const reads1 = listReads(app);
pressNote(app, 'closed-task');
await settle();
app.modal('#mv').value = 'x'.repeat(LIMIT + 1);
clickModal(app, '#mo', 'the note sheet must be saveable');
await settle(); await settle();
check('the refusal is shown in the server\'s words', toastText(app).includes(`the limit is ${LIMIT}`),
  `toast=${toastText(app)}`);
check('a refused save does not reload the list', listReads(app) === reads1,
  `list reads ${reads1} -> ${listReads(app)}`);
check('and the card keeps its old note', body(app, 'closed-task').includes('closed &lt;b&gt;'),
  body(app, 'closed-task'));

// ── a name the list no longer knows opens nothing ───────────────────────
await app.notesFromCard('ghost-task');
await settle();
check('an unknown task opens no sheet', !app.modalOpen());
check('and sends nothing', posted[posted.length - 1] === last || posted[posted.length - 1].name !== 'ghost-task');

report('card-notes');
