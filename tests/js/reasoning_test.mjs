/* Assertions for the reasoning level on a task card, on the task's page, and
 * on the ••• sheet that changes it.
 *
 * The card mirrors the terminal: an expanded card draws the ladder the
 * `Reasoning` column of `ilan dashboard` draws, `low ⋅ medium ⋅ max` with the
 * task's own level lit, and a collapsed card is `ilan ls -c`, which prints
 * only `{level}`. Both forms are one piece of markup that CSS switches
 * between, so what is checked here is that the markup carries both — the
 * braces inside the lit word, every other part marked as what collapsing
 * hides — and test_web.py checks the rules that do the hiding.
 */

import { bootApp, checker, settle } from './harness.mjs';

const { check, clickModal, report } = checker();

const LEVELS = ['low', 'medium', 'max'];
const STAMP = { created_at: '2026-01-01T00:00:00+00:00', status_changed_at: '2026-01-01T00:00:00+00:00' };
const TASKS = [
  { name: 'low-task', alias: 'aa', status: 'WORKING', engine: 'claude', reasoning: 'low', ...STAMP },
  { name: 'medium-task', alias: 'ab', status: 'AGENT_FINISHED', engine: 'codex', reasoning: 'medium', ...STAMP },
  { name: 'max-task', alias: 'ac', status: 'NEEDS_ATTENTION', engine: 'claude', reasoning: 'max',
    max_tag: 'FABLE', maxed: true, ...STAMP },
  // A level the app does not know, and a row with none: neither may draw a
  // ladder with nothing lit on it.
  { name: 'odd-task', alias: 'ad', status: 'AGENT_FINISHED', engine: 'claude', reasoning: 'xhigh', ...STAMP },
  { name: 'bare-task', alias: 'af', status: 'AGENT_FINISHED', engine: 'claude', ...STAMP },
];

function listWith(open) {
  const app = bootApp();
  app.state.tasks = structuredClone(TASKS);
  app.state.expanded = new Set(open);
  app.renderList();
  return app;
}

/** The card for *name*, as markup. */
function card(app, name) {
  const m = app.html().match(
    new RegExp(`<div class="card [^"]*">(?:(?!<div class="card )[\\s\\S])*?data-toggle="${name}"[\\s\\S]*?<div class="row-actions">`));
  return m ? m[0] : '';
}

/** The reasoning span inside *html*, or ''. */
const ladder = (html) => (html.match(/<span class="reasoning">[\s\S]*?<\/span><\/span>(?=<span class="meta-detail">|<\/span>|<\/p>|$)/) || [])[0] || '';

/** The words of a ladder, in order, as {level, lit}. */
const words = (html) => [...ladder(html).matchAll(/<span class="rl-(on rl-[a-z]+|off)"[^>]*>(?:<span class="rl-brace"[^>]*>\{<\/span>)?([a-z]+)/g)]
  .map((m) => ({ level: m[2], lit: m[1].startsWith('on') }));

// ── an expanded card draws the ladder ───────────────────────────────────
const open = listWith(TASKS.map((t) => t.name));
for (const level of LEVELS) {
  const c = card(open, `${level}-task`);
  const w = words(c);
  check(`${level}: the card draws all three levels, cheapest first`,
    JSON.stringify(w.map((x) => x.level)) === JSON.stringify(LEVELS), JSON.stringify(w));
  check(`${level}: exactly its own level is lit`,
    JSON.stringify(w.filter((x) => x.lit).map((x) => x.level)) === JSON.stringify([level]),
    JSON.stringify(w));
  check(`${level}: the lit word carries its level's colour class`,
    ladder(c).includes(`<span class="rl-on rl-${level}">`), ladder(c));
  check(`${level}: the words are joined by the terminal's dot`,
    (ladder(c).match(/<span class="rl-sep" aria-hidden="true"> ⋅ <\/span>/g) || []).length === 2,
    ladder(c));
  check(`${level}: the braces are inside the lit word, so they take its colour`,
    new RegExp(`<span class="rl-on rl-${level}"><span class="rl-brace" aria-hidden="true">\\{</span>${level}<span class="rl-brace" aria-hidden="true">\\}</span></span>`).test(c),
    ladder(c));
  check(`${level}: the ladder is inside the meta row`,
    /<span class="row-meta">[\s\S]*?<span class="reasoning">/.test(c));
  check(`${level}: it is not tagged as something collapsing hides`,
    !/<span class="reasoning[^"]*meta-detail|meta-detail[^"]*reasoning/.test(c));
  check(`${level}: it comes after the status and before the age`,
    c.indexOf('class="status ') < c.indexOf('class="reasoning"')
      && c.indexOf('class="reasoning"') < c.indexOf('class="meta-detail"'), c);
  check(`${level}: a screen reader hears the level, not the ladder`,
    ladder(c).includes('<span class="sr-only">Reasoning </span>')
      && (ladder(c).match(/<span class="rl-off" aria-hidden="true">/g) || []).length === 2,
    ladder(c));
}
check('the level follows the max-model tag when there is one',
  card(open, 'max-task').indexOf('class="max-tag"') < card(open, 'max-task').indexOf('class="reasoning"'),
  card(open, 'max-task'));
check('a level the app does not know draws no ladder',
  !card(open, 'odd-task').includes('class="reasoning"'), card(open, 'odd-task'));
check('a row with no level draws no ladder', !card(open, 'bare-task').includes('class="reasoning"'));
check('the card offers no control for it — the ••• sheet does',
  !/data-level=|act-level/.test(open.html()));

// ── a collapsed card carries the same markup ────────────────────────────
const shut = listWith([]);
check('the card really is collapsed', /class="card rs-WORKING collapsed"/.test(card(shut, 'low-task')));
for (const level of LEVELS) {
  check(`${level}: the collapsed card still carries the level`,
    ladder(card(shut, `${level}-task`)) === ladder(card(open, `${level}-task`)),
    'collapsing rendered a different ladder instead of leaving it to CSS');
}

// ── the task's own page draws the ladder beside the status ──────────────
const openTask = (task, onPost) => {
  const app = bootApp();
  app.setFetch(async (path, opts) => {
    const json = (d, ok = true) => ({ ok, status: ok ? 200 : 400, json: async () => d });
    if (opts && opts.method === 'POST') return onPost(path, opts);
    if (path.includes('/tail')) return json({ entries: [] });
    return json({ task });
  });
  return app;
};
const statusLine = (app) => (app.html().match(/<p class="hdr-sub[^"]*"[^>]*>([\s\S]*?)<\/p>/) || [])[1] || '';

for (const task of TASKS.slice(0, 3)) {
  const app = openTask(task);
  await app.renderDetail(task.name);
  await settle();
  check(`${task.name}: its page draws the same ladder as its card`,
    ladder(statusLine(app)) !== '' && ladder(statusLine(app)) === ladder(card(open, task.name)),
    statusLine(app));
  check(`${task.name}: after the status, and after the tag when there is one`,
    statusLine(app).indexOf('class="status ') < statusLine(app).indexOf('class="reasoning"')
      && (!task.max_tag || statusLine(app).indexOf('class="max-tag"') < statusLine(app).indexOf('class="reasoning"')),
    statusLine(app));
}

// ── the ••• sheet names the level and changes it ────────────────────────
const TASK = TASKS[1]; // medium, on codex
const posts = [];
const ok = async (path, opts) => {
  posts.push({ path, body: JSON.parse(opts.body || '{}') });
  const level = JSON.parse(opts.body || '{}').level;
  const effort = TASK.engine === 'codex' && level === 'max' ? 'xhigh' : level;
  return { ok: true, status: 200, json: async () => ({ ok: true, name: TASK.name, reasoning: level, effort }) };
};

const s = openTask(TASK, ok);
s.showActions(TASK);
await settle();
const entry = s.modalOptions().find((o) => o.value === 'level');
check('the sheet offers the level', Boolean(entry), JSON.stringify(s.modalOptions()));
check('the entry names the current level', entry?.label === 'Reasoning level… (now medium)', entry?.label);
check('it sits right after Max', (() => {
  const v = s.modalOptions().map((o) => o.value);
  return v.indexOf('level') === v.indexOf('max') + 1;
})(), JSON.stringify(s.modalOptions().map((o) => o.value)));

clickModal(s, '[data-value="level"]', 'the level entry must be on the sheet');
await settle();
check('choosing it opens a second sheet', s.modalOpen());
check('it has no text field — the level is chosen, not typed', !s.modalHasField());
check('it offers exactly the three levels, cheapest first',
  JSON.stringify(s.modalOptions().map((o) => o.value)) === JSON.stringify(LEVELS),
  JSON.stringify(s.modalOptions()));
check('the current level is marked, the others are not',
  JSON.stringify(s.modalOptions().map((o) => o.label)) === JSON.stringify(['low', 'medium (current)', 'max']),
  JSON.stringify(s.modalOptions().map((o) => o.label)));
for (const level of LEVELS) {
  check(`${level}: its choice is drawn in its colour`,
    new RegExp(`class="btn rl-choice rl-${level}"[^>]*data-value="${level}"`).test(s.modalHtml()),
    s.modalHtml());
}
check('none of them is dangerous', s.modalOptions().every((o) => !o.danger));

clickModal(s, '[data-value="max"]', 'the level sheet must offer max');
await settle(); await settle();
check('picking max posts one level change', posts.length === 1, JSON.stringify(posts));
check('to the task the sheet was opened for', posts[0]?.path === '/tasks/medium-task/level', posts[0]?.path);
check('with the chosen level', posts[0]?.body.level === 'max', JSON.stringify(posts[0]?.body));
check('the confirmation says what max means on Codex',
  s.el('toast').textContent === 'Reasoning level set to max (xhigh on codex)', s.el('toast').textContent);
check('and the page is read back afterwards',
  s.fetches.filter((f) => f.path === '/tasks/medium-task').length >= 1);

// A level that means the same on every backend says nothing more.
const plain = openTask(TASKS[0], async (path, opts) => ({
  ok: true, status: 200,
  json: async () => ({ ok: true, reasoning: JSON.parse(opts.body).level, effort: JSON.parse(opts.body).level }),
}));
plain.showActions(TASKS[0]);
await settle();
clickModal(plain, '[data-value="level"]', 'the level entry must be on the sheet');
await settle();
clickModal(plain, '[data-value="medium"]', 'the level sheet must offer medium');
await settle(); await settle();
check('a plain level change says only the level',
  plain.el('toast').textContent === 'Reasoning level set to medium', plain.el('toast').textContent);

// ── cancelling posts nothing; a refusal is reported ─────────────────────
const n = openTask(TASK, ok);
posts.length = 0;
n.showActions(TASK);
await settle();
clickModal(n, '[data-value="level"]', 'the level entry must be on the sheet');
await settle();
clickModal(n, '[data-value=""]', 'the level sheet must be cancellable');
await settle();
check('cancelling posts nothing', posts.length === 0, JSON.stringify(posts));
check('and closes the sheet', !n.modalOpen());

const r = openTask(TASK, async () => ({
  ok: false, status: 400, json: async () => ({ error: "Invalid reasoning level 'x'" }),
}));
r.showActions(TASK);
await settle();
clickModal(r, '[data-value="level"]', 'the level entry must be on the sheet');
await settle();
clickModal(r, '[data-value="low"]', 'the level sheet must offer low');
await settle(); await settle();
check('a refusal is shown as an error', r.el('toast').className === 'toast toast-err'
  && r.el('toast').textContent.includes('Invalid reasoning level'), r.el('toast').textContent);

report('reasoning');
