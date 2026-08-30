/* Behavioural assertions for deferred search in the web app.
 *
 * app.js is a browser script, so it is evaluated here against a DOM stub small
 * enough to be obvious and large enough to run renderList: element lookups
 * return persistent stubs whose handlers the test can call, and #app records
 * the HTML that was assigned to it.
 *
 * The point is to exercise the real handlers rather than grep the source, so a
 * regression back to search-as-you-type actually fails this.
 */

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(
  join(here, '..', '..', 'src', 'ilan', 'web', 'static', 'app.js'), 'utf8',
);

const HARNESS = `
  const MD = { escapeHtml: (v) => String(v ?? '')
    .replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;')
    .replaceAll('"','&quot;').replaceAll("'",'&#39;') };
  const __els = new Map();
  function __el(key) {
    if (!__els.has(key)) {
      __els.set(key, {
        id: key, value: '', innerHTML: '', hidden: true, className: '',
        onclick: null, oninput: null, onkeydown: null,
        focus() {}, classList: { add() {} }, style: {},
      });
    }
    return __els.get(key);
  }
  const document = {
    hidden: false,
    activeElement: null,
    querySelector: (sel) => __el(sel.replace('#', '')),
    querySelectorAll: () => [],
    addEventListener: () => {},
    body: { appendChild: () => {} },
    createElement: () => ({ addEventListener: () => {}, classList: { add() {} } }),
  };
  const window = { addEventListener: () => {} };
  const location = { hash: '#/' };
  const fetch = () => new Promise(() => {});
  const setInterval = () => 0;
  const clearInterval = () => {};
  const setTimeout = () => 0;
  const clearTimeout = () => {};
`;

const app = new Function(
  `${HARNESS}\n${source}\n;return { state, renderList, applySearch, el: __el, document };`,
)();

const { state, renderList, el } = app;

state.tasks = [
  { name: 'alpha-task', alias: 'aa', status: 'WORKING', engine: 'claude',
    created_at: '2026-01-01T00:00:00+00:00', status_changed_at: '2026-01-01T00:00:00+00:00' },
  { name: 'beta-task', alias: 'ab', status: 'WORKING', engine: 'codex',
    created_at: '2026-01-02T00:00:00+00:00', status_changed_at: '2026-01-02T00:00:00+00:00' },
];

const failures = [];
function check(name, condition, detail = '') {
  if (!condition) failures.push(`FAIL  ${name}${detail ? `\n        ${detail}` : ''}`);
}

const html = () => el('app').innerHTML;

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

if (failures.length) {
  console.log(failures.join('\n'));
  console.log(`\n${failures.length} search assertion(s) FAILED`);
  process.exit(1);
}
console.log('all search assertions passed');
