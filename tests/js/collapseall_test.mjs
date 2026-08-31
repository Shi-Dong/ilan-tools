/* Assertions for the list header's Collapse All button.
 *
 * Two things are worth pinning beyond "it collapses things".
 *
 * When it is offered. Cards are collapsed by default, so on most visits there
 * is nothing to close, and a live button that does nothing is worse than an
 * absent one. It is judged on what is on screen rather than on the stored set,
 * because a search can hide an expanded task — and a button enabled for a card
 * the user cannot see is a tap at a list that does not visibly change.
 *
 * What it clears. The whole set, including whatever a search is hiding, which
 * is what the word "All" says. That asymmetry with the enabled rule above is
 * deliberate and is the part most likely to be "tidied" into agreement later,
 * so both halves are asserted.
 */

import { bootApp, checker, EXPANDED_KEY } from './harness.mjs';

const { check, report } = checker();

const TASKS = [
  { name: 'alpha-task', alias: 'aa', status: 'WORKING', engine: 'claude',
    created_at: '2026-01-01T00:00:00+00:00', status_changed_at: '2026-01-01T00:00:00+00:00' },
  { name: 'beta-task', alias: 'ab', status: 'AGENT_FINISHED', engine: 'codex',
    created_at: '2026-01-02T00:00:00+00:00', status_changed_at: '2026-01-02T00:00:00+00:00' },
  { name: 'gamma-task', alias: 'ac', status: 'NEEDS_ATTENTION', engine: 'claude',
    created_at: '2026-01-03T00:00:00+00:00', status_changed_at: '2026-01-03T00:00:00+00:00' },
];

/** A list with *open* expanded, rendered. */
function listWith(open = []) {
  const app = bootApp();
  app.state.tasks = structuredClone(TASKS);
  app.state.expanded = new Set(open);
  app.renderList();
  return app;
}

const openCards = (app) =>
  (app.html().match(/class="card rs-[A-Z_]+"/g) || []).length;
const collapsedCards = (app) =>
  (app.html().match(/class="card rs-[A-Z_]+ collapsed"/g) || []).length;
const isDisabled = (app) =>
  /id="collapse-all"[^>]*\sdisabled/.test(app.html());

// ── it is there, and it says so ─────────────────────────────────────────
const app = listWith(['alpha-task', 'beta-task']);
check('a Collapse All button is rendered', app.html().includes('id="collapse-all"'));
check('it is labelled in words', app.html().includes('>Collapse All</button>'),
  'the label is not the words that were asked for');
// Beside Refresh, and before the two that navigate away from the list: both
// are things you do *to* the list, so they belong together.
check('it sits next to Refresh',
  /id="do-refresh"[\s\S]*?id="collapse-all"[\s\S]*?id="go-config"/.test(app.html()),
  'the button moved out from beside Refresh');

// ── when it is offered ──────────────────────────────────────────────────
check('it is live while a card is open', !isDisabled(app));

const shut = listWith([]);
check('and dead when every card is already closed', isDisabled(shut),
  'the button invites a tap that would change nothing');
check('the default list really is all closed', openCards(shut) === 0,
  `${openCards(shut)} card(s) open`);

// A stored name that is not on screen must not wake it up.
const hidden = listWith(['gamma-task']);
hidden.state.draft = 'alpha';
hidden.state.query = 'alpha';
hidden.renderList();
check('a search hiding the only open card leaves it dead', isDisabled(hidden),
  'enabled for a card that is not on screen');
check('and the open card really is hidden', !hidden.html().includes('gamma-task'));

// ── what it does ────────────────────────────────────────────────────────
const closing = listWith(['alpha-task', 'beta-task']);
check('two cards start open', openCards(closing) === 2, `${openCards(closing)} open`);
closing.el('collapse-all').onclick();
check('every card is closed afterwards', openCards(closing) === 0,
  `${openCards(closing)} still open`);
check('and they are all still listed', collapsedCards(closing) === 3,
  `${collapsedCards(closing)} collapsed`);
check('the button goes dead once it has nothing left to do', isDisabled(closing),
  'still live with everything closed');

// Written through, not just re-rendered. Without the save the list would come
// back expanded on the next load, which reads as the button being forgotten.
check('the closed state is stored', closing.storage.get(EXPANDED_KEY) === '[]',
  `stored=${closing.storage.get(EXPANDED_KEY)}`);

// ── "All" means all, including what a search is hiding ──────────────────
const filtered = listWith(['alpha-task', 'gamma-task']);
filtered.state.draft = 'alpha';
filtered.state.query = 'alpha';
filtered.renderList();
check('the hidden card is open before the tap',
  filtered.state.expanded.has('gamma-task'));
filtered.el('collapse-all').onclick();
check('a card the search was hiding is closed too',
  !filtered.state.expanded.has('gamma-task'),
  'only the visible cards were closed, leaving the list half collapsed');
check('nothing is left in the stored set',
  filtered.storage.get(EXPANDED_KEY) === '[]',
  `stored=${filtered.storage.get(EXPANDED_KEY)}`);

// ── it is a local action ────────────────────────────────────────────────
// Nothing about it needs the server, and a refetch would make a button meant
// to tidy the view feel like a reload.
const quiet = listWith(['alpha-task']);
const before = quiet.fetches.length;
quiet.el('collapse-all').onclick();
check('it asks the server for nothing', quiet.fetches.length === before,
  `${quiet.fetches.length - before} request(s) issued`);

// It re-renders the header, so a half-typed search has to survive it the same
// way it survives the poll — state.draft is what makes that true.
const typing = listWith(['alpha-task']);
typing.state.draft = 'half-typed';
typing.renderList();
typing.el('collapse-all').onclick();
check('a half-typed search survives the collapse',
  typing.html().includes('value="half-typed"'),
  'the draft was wiped by the re-render');

// ── every glyph-only button in the row has a name ───────────────────────
// Collapse All is the only one in this row that still spells itself out, so
// the other three depend entirely on an attribute for their name.
for (const id of ['do-refresh', 'go-config', 'go-new']) {
  check(`${id} has an accessible name`,
    new RegExp(`id="${id}"[^>]*aria-label="[^"]+"`).test(app.html()),
    'a glyph-only button with no name is announced as its character');
}

report('collapse-all');
