/* Assertions for the deferred search: typing records a draft, and nothing is
 * filtered until Search (or the keyboard's Enter) is pressed.
 *
 * Search-as-you-type re-rendered the header on every keystroke, which dropped
 * the caret to the end of the field. These assertions are what stop that
 * coming back.
 */

import { bootApp, checker } from './harness.mjs';

const { check, report } = checker();
const app = bootApp();
const { state, renderList, el } = app;

state.tasks = [
  { name: 'alpha-task', alias: 'aa', status: 'WORKING', engine: 'claude',
    created_at: '2026-01-01T00:00:00+00:00', status_changed_at: '2026-01-01T00:00:00+00:00' },
  { name: 'beta-task', alias: 'ab', status: 'WORKING', engine: 'codex',
    created_at: '2026-01-02T00:00:00+00:00', status_changed_at: '2026-01-02T00:00:00+00:00' },
];

const html = app.html;

// ── initial render ──────────────────────────────────────────────────────
renderList();
check('both tasks listed before searching',
  html().includes('alpha-task') && html().includes('beta-task'));
check('a Search button is rendered', html().includes('id="do-search"'));
check('no Clear button until a search is applied', !html().includes('id="clear-search"'));

// ── typing must NOT filter ──────────────────────────────────────────────
const box = el('q');
box.value = 'alpha';
box.oninput();
check('typing records a draft', state.draft === 'alpha', `draft=${state.draft}`);
check('typing does not set the query', state.query === '', `query=${state.query}`);
check('typing does not filter the list',
  html().includes('alpha-task') && html().includes('beta-task'),
  'beta-task disappeared before Search was pressed');

// ── pressing Search applies it ──────────────────────────────────────────
el('do-search').onclick();
check('Search applies the draft', state.query === 'alpha', `query=${state.query}`);
check('Search filters the list',
  html().includes('alpha-task') && !html().includes('beta-task'));
check('Clear appears once a search is applied', html().includes('id="clear-search"'));

// ── the typed text survives a re-render ─────────────────────────────────
el('q').value = 'beta';
el('q').oninput();
renderList();  // what the 15s auto-refresh does
check('draft survives a re-render', html().includes('value="beta"'),
  'the refresh timer would wipe what the user was typing');

// ── Enter behaves like the button ───────────────────────────────────────
el('q').onkeydown({ key: 'a' });
check('a normal keypress does not search', state.query === 'alpha',
  `query=${state.query}`);
el('q').onkeydown({ key: 'Enter' });
check('Enter applies the draft', state.query === 'beta', `query=${state.query}`);

// ── Clear resets both ───────────────────────────────────────────────────
renderList();
el('clear-search').onclick();
check('Clear empties the draft', state.draft === '', `draft=${state.draft}`);
check('Clear empties the query', state.query === '', `query=${state.query}`);
check('Clear restores the full list',
  html().includes('alpha-task') && html().includes('beta-task'));

// ── whitespace-only input is not a filter ───────────────────────────────
el('q').value = '   ';
el('q').oninput();
el('do-search').onclick();
check('whitespace-only search is treated as empty', state.query === '',
  `query=${JSON.stringify(state.query)}`);

report('search');
