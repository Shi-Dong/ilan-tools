/* Assertions for the reasoning level on a task card, on the task's page, and
 * on the ••• sheet that changes it.
 *
 * The level is a line of its own right beneath the status, "Reasoning: low",
 * with only the level's name coloured, on an expanded card and on the task's
 * page. A collapsed card carries the same markup and CSS hides it, as it hides
 * the summary (test_web.py checks that rule) — so what is checked here is that
 * the markup does not change with the card's state. A level the app does not
 * know draws nothing, rather than a line naming a level no one set.
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
  // A level the app does not know, and a row with none: neither may draw a line.
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

const lineFor = (level) => `Reasoning: <span class="rl-${level}">${level}</span>`;

// ── the line, right beneath the status; the same markup either way ──────
const open = listWith(TASKS.map((t) => t.name));
const shut = listWith([]);
check('the card really is collapsed', /class="card rs-WORKING collapsed"/.test(card(shut, 'low-task')));
for (const [state, app] of [['expanded', open], ['collapsed', shut]]) {
  for (const level of LEVELS) {
    const c = card(app, `${level}-task`);
    check(`${state} ${level}: the card reads "Reasoning: ${level}", with only the level coloured`,
      c.includes(`<span class="row-reasoning">${lineFor(level)}</span>`), c);
    check(`${state} ${level}: the line sits right beneath the status row`,
      /<span class="row-meta">[\s\S]*?<\/span>\s*<span class="row-reasoning">/.test(c)
        && !/<span class="row-meta">(?:(?!<\/button>)[\s\S])*<span class="row-meta">/.test(c),
      c);
    check(`${state} ${level}: it is part of the card body, not a second control`,
      c.indexOf('class="row-reasoning"') < c.indexOf('</button>'), c);
    check(`${state} ${level}: the line is drawn once`,
      (c.match(/class="row-reasoning"/g) || []).length === 1);
  }
  check(`${state}: a level the app does not know draws no line`,
    !card(app, 'odd-task').includes('row-reasoning'), card(app, 'odd-task'));
  check(`${state}: a row with no level draws no line`, !card(app, 'bare-task').includes('row-reasoning'));
}
check('the status row itself no longer carries the level',
  !/<span class="row-meta">[^\n]*Reasoning/.test(open.html()));
check('the card offers no control for it — the ••• sheet does',
  !/data-level=|act-level/.test(open.html()));

// ── the task's own page shows the same line beneath its status ──────────
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
const header = (app) => (app.html().match(/<header class="hdr">([\s\S]*?)<\/header>/) || [])[1] || '';

for (const task of TASKS.slice(0, 3)) {
  const app = openTask(task);
  await app.renderDetail(task.name);
  await settle();
  const h = header(app);
  check(`${task.name}: its page reads the same line as its card`,
    h.includes(`<p class="hdr-sub row-reasoning">${lineFor(task.reasoning)}</p>`), h);
  check(`${task.name}: right beneath the status line`,
    /<p class="hdr-sub row-meta rs-[A-Z_]+">[\s\S]*?<\/p>\s*<p class="hdr-sub row-reasoning">/.test(h), h);
  check(`${task.name}: the status line itself does not carry it`,
    !/<p class="hdr-sub row-meta[^>]*>(?:(?!<\/p>)[\s\S])*Reasoning/.test(h), h);
}
{
  const app = openTask(TASKS[4]);
  await app.renderDetail(TASKS[4].name);
  await settle();
  check('a page for a task with no level has no line', !header(app).includes('row-reasoning'));
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
