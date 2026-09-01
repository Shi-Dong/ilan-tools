/* Assertions for the FABLE tag on a task card.
 *
 * The card renders the tag from a flag the server computes — whether the task
 * is pinned to Fable *and* on a backend that will run it there — so what is
 * checked here is the rendering of that flag, not the rule behind it. The rule
 * has its own tests against models.py and the server.
 *
 * The one property worth more than a glance is that the tag survives
 * collapsing. Most of the list is only ever seen collapsed on a phone, and
 * which tasks are burning the expensive model is exactly the kind of thing
 * worth knowing from there.
 */

import { bootApp, checker } from './harness.mjs';

const { check, report } = checker();

const TASKS = [
  { name: 'maxed-task', alias: 'aa', status: 'WORKING', engine: 'claude', fable: true,
    model: 'claude-fable-5-1',
    created_at: '2026-01-01T00:00:00+00:00', status_changed_at: '2026-01-01T00:00:00+00:00' },
  { name: 'plain-task', alias: 'ab', status: 'AGENT_FINISHED', engine: 'claude', fable: false,
    created_at: '2026-01-02T00:00:00+00:00', status_changed_at: '2026-01-02T00:00:00+00:00' },
  // A codex task still pinned to Fable: the server says false, and the card
  // must not second-guess that from the model it can also see.
  { name: 'codex-pinned', alias: 'ac', status: 'AGENT_FINISHED', engine: 'codex', fable: false,
    model: 'claude-fable-5-1',
    created_at: '2026-01-03T00:00:00+00:00', status_changed_at: '2026-01-03T00:00:00+00:00' },
  // A row from a server that predates the flag: absent has to read as false.
  { name: 'old-row', alias: 'ad', status: 'AGENT_FINISHED', engine: 'claude',
    created_at: '2026-01-04T00:00:00+00:00', status_changed_at: '2026-01-04T00:00:00+00:00' },
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

const TAG = '<span class="fable">FABLE</span>';

// ── expanded ────────────────────────────────────────────────────────────
const open = listWith(TASKS.map((t) => t.name));
check('a maxed task carries the tag', card(open, 'maxed-task').includes(TAG),
  card(open, 'maxed-task'));
check('it sits directly after the status pill',
  /<\/span><span class="fable">FABLE<\/span>/.test(card(open, 'maxed-task'))
    && /class="status st-WORKING">[^<]*<\/span><span class="fable">/.test(card(open, 'maxed-task')),
  'the tag is somewhere other than beside the status');
check('it is inside the meta row',
  /<span class="row-meta">[\s\S]*?<span class="fable">/.test(card(open, 'maxed-task')));
check('the tag says exactly FABLE', (card(open, 'maxed-task').match(/>FABLE</g) || []).length === 1);

check('a plain task has no tag', !card(open, 'plain-task').includes('class="fable"'));
check('a codex task pinned to Fable has no tag either',
  !card(open, 'codex-pinned').includes('class="fable"'),
  'the card tagged a task the server said is not on Fable');
check('a row without the flag has no tag', !card(open, 'old-row').includes('class="fable"'));

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
check('the tag is still rendered when collapsed', card(shut, 'maxed-task').includes(TAG),
  'the tag went with the metadata a collapsed card drops');
check('and it is not tagged as something collapsing hides',
  !/<span class="meta-detail[^"]*">FABLE/.test(card(shut, 'maxed-task'))
    && !/class="fable meta-detail"|class="meta-detail fable"/.test(card(shut, 'maxed-task')));

report('fable-tag');
