/* Assertions for the max-model tag on a task card.
 *
 * The card renders a string the server computes — the tag of the max model a
 * task is pinned to, but only on the backend that will run it there — so what
 * is checked here is the rendering of that string, not the rule behind it. The
 * rule has its own tests against models.py and the server.
 *
 * The one property worth more than a glance is that the tag survives
 * collapsing. Most of the list is only ever seen collapsed on a phone, and
 * which tasks are burning the expensive model is exactly the kind of thing
 * worth knowing from there.
 */

import { bootApp, checker } from './harness.mjs';

const { check, report } = checker();

const TASKS = [
  { name: 'maxed-task', alias: 'aa', status: 'WORKING', engine: 'claude', max_tag: 'FABLE',
    model: 'claude-fable-5-1',
    created_at: '2026-01-01T00:00:00+00:00', status_changed_at: '2026-01-01T00:00:00+00:00' },
  // The other backend's max model, tagged with its own name: which model a
  // task is burning is the whole reason the tag is there.
  { name: 'maxed-codex', alias: 'ab', status: 'WORKING', engine: 'codex', max_tag: 'ASTRA',
    model: 'gpt-6-astra',
    created_at: '2026-01-02T00:00:00+00:00', status_changed_at: '2026-01-02T00:00:00+00:00' },
  { name: 'plain-task', alias: 'ac', status: 'AGENT_FINISHED', engine: 'claude', max_tag: null,
    created_at: '2026-01-03T00:00:00+00:00', status_changed_at: '2026-01-03T00:00:00+00:00' },
  // A codex task still pinned to Fable: the server sends no tag, and the card
  // must not second-guess that from the model it can also see.
  { name: 'codex-pinned', alias: 'ad', status: 'AGENT_FINISHED', engine: 'codex', max_tag: null,
    model: 'claude-fable-5-1',
    created_at: '2026-01-04T00:00:00+00:00', status_changed_at: '2026-01-04T00:00:00+00:00' },
  // A row from a server that predates the field: absent has to read as no tag.
  { name: 'old-row', alias: 'af', status: 'AGENT_FINISHED', engine: 'claude',
    created_at: '2026-01-05T00:00:00+00:00', status_changed_at: '2026-01-05T00:00:00+00:00' },
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

const tagFor = (t) => `<span class="max-tag">${t}</span>`;

// ── expanded ────────────────────────────────────────────────────────────
const open = listWith(TASKS.map((t) => t.name));
for (const [name, tag] of [['maxed-task', 'FABLE'], ['maxed-codex', 'ASTRA']]) {
  check(`a task maxed on ${tag} carries the tag`, card(open, name).includes(tagFor(tag)),
    card(open, name));
  check(`${tag}: it sits directly after the status pill`,
    new RegExp(`</span><span class="max-tag">${tag}</span>`).test(card(open, name))
      && /class="status st-[A-Z_]+">[^<]*<\/span><span class="max-tag">/.test(card(open, name)),
    'the tag is somewhere other than beside the status');
  check(`${tag}: it is inside the meta row`,
    /<span class="row-meta">[\s\S]*?<span class="max-tag">/.test(card(open, name)));
  check(`${tag}: the tag is rendered exactly once`,
    (card(open, name).match(new RegExp(`>${tag}<`, 'g')) || []).length === 1);
}

check('a plain task has no tag', !card(open, 'plain-task').includes('class="max-tag"'));
check('a codex task pinned to Fable has no tag either',
  !card(open, 'codex-pinned').includes('class="max-tag"'),
  'the card tagged a task the server sent no tag for');
check('a row from before the field has no tag',
  !card(open, 'old-row').includes('class="max-tag"'));

// ── the backend word is gone, the backend colour is not ─────────────────
for (const name of ['maxed-task', 'plain-task', 'codex-pinned']) {
  const c = card(open, name);
  check(`${name}: the backend is not spelled out in the meta row`,
    !/<span class="meta-detail">(claude|codex)<\/span>/.test(c), c.match(/<span class="row-meta">[\s\S]*?<\/span>\s*<\/button>/)?.[0]);
}
check('the name is still coloured by backend',
  card(open, 'maxed-task').includes('engine-claude')
    && card(open, 'codex-pinned').includes('engine-codex'));
check('the age is still shown', /<span class="meta-detail">[^<]* ago<\/span>/.test(card(open, 'maxed-task')));

// ── collapsed ───────────────────────────────────────────────────────────
const shut = listWith([]);
check('the card really is collapsed', /class="card rs-WORKING collapsed"/.test(card(shut, 'maxed-task')));
for (const [name, tag] of [['maxed-task', 'FABLE'], ['maxed-codex', 'ASTRA']]) {
  check(`${tag}: the tag is still rendered when collapsed`, card(shut, name).includes(tagFor(tag)),
    'the tag went with the metadata a collapsed card drops');
  check(`${tag}: and it is not tagged as something collapsing hides`,
    !new RegExp(`<span class="meta-detail[^"]*">${tag}`).test(card(shut, name))
      && !/class="max-tag meta-detail"|class="meta-detail max-tag"/.test(card(shut, name)));
}


// ── the task's own page shows the same tag beside the name ──────────────
// Same string from the server, same class, same rule: a task tagged on the
// list is tagged in its title, and one that is not is not.
{
  const { settle } = await import('./harness.mjs');
  const openTask = (task) => {
    const app = bootApp();
    app.setFetch(async (path) => {
      const json = (d) => ({ ok: true, status: 200, json: async () => d });
      if (path.includes('/tail')) return json({ entries: [] });
      return json({ task });
    });
    return app;
  };
  const title = (app) => (app.html().match(/<h1 class="hdr-title[^"]*">([\s\S]*?)<\/h1>/) || [])[1] || '';
  const statusLine = (app) => (app.html().match(/<p class="hdr-sub[^"]*"[^>]*>([\s\S]*?)<\/p>/) || [])[1] || '';

  for (const task of TASKS.filter((t) => t.max_tag)) {
    const app = openTask(task);
    await app.renderDetail(task.name);
    await settle();
    // Directly after the status pill, as on the card — the same container
    // class carries both, so the two surfaces render it identically.
    check(`${task.name}: its page carries ${task.max_tag} beside the status`,
      new RegExp(`class="status st-[A-Z_]+">[^<]*</span><span class="max-tag">${task.max_tag}</span>`).test(statusLine(app)),
      statusLine(app));
    check(`${task.name}: the title itself carries no tag, so the name keeps its width`,
      !title(app).includes('class="max-tag"'), title(app));
    check(`${task.name}: the status line is the card's own container`,
      /<p class="hdr-sub row-meta rs-[A-Z_]+">/.test(app.html()));
  }
  for (const task of TASKS.filter((t) => !t.max_tag)) {
    const app = openTask(task);
    await app.renderDetail(task.name);
    await settle();
    check(`${task.name}: its page shows no tag`, !app.html().includes('class="max-tag"'), statusLine(app));
  }
}

report('max-model-tag');
