/* Assertions for the list header's Refresh button.
 *
 * The All toggle is gone, so closed tasks are reachable only by searching —
 * which is asserted here, since dropping the toggle without that path would
 * make DONE and DISCARDED tasks unreachable from the phone entirely.
 */

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const appSource = readFileSync(
  join(here, '..', '..', 'src', 'ilan', 'web', 'static', 'app.js'), 'utf8',
);

const HARNESS = `
  const MD = { escapeHtml: (v) => String(v ?? ''), render: (v) => String(v ?? '') };
  const __store = new Map();
  const localStorage = { getItem: () => null, setItem: () => {} };
  const __fetches = [];
  let __fetchImpl = () => new Promise(() => {});
  const fetch = (p, o) => { __fetches.push({ path: p, opts: o }); return __fetchImpl(p, o); };
  function __setFetch(fn) { __fetchImpl = fn; }
  const __els = new Map();
  function __el(k) {
    if (!__els.has(k)) __els.set(k, { id: k, value: '', innerHTML: '', hidden: true,
      className: '', textContent: '', onclick: null, oninput: null, onkeydown: null,
      dataset: {}, focus() {}, classList: { add() {} }, style: {} });
    return __els.get(k);
  }
  const document = {
    hidden: false, activeElement: null,
    querySelector: (s) => __el(s.replace('#', '')),
    querySelectorAll: () => [],
    addEventListener: () => {},
    body: { appendChild: () => {} },
    createElement: () => ({ addEventListener: () => {}, classList: { add() {} },
      querySelector: () => ({ onclick: null, focus() {} }) }),
  };
  const window = { addEventListener: () => {} };
  const location = { hash: '#/' };
  const setInterval = () => 0; const clearInterval = () => {};
  const setTimeout = () => 0; const clearTimeout = () => {};
`;
const TAIL = `;return { state, renderList, refreshList, el: __el,
  fetches: __fetches, setFetch: __setFetch };`;

const app = new Function(`${HARNESS}\n${appSource}\n${TAIL}`)();

const TASKS = [
  { name: 'live-task', alias: 'aa', status: 'WORKING', engine: 'claude',
    created_at: '2026-01-01T00:00:00+00:00', status_changed_at: '2026-01-01T00:00:00+00:00' },
  { name: 'closed-task', alias: 'ab', status: 'DONE', engine: 'claude',
    created_at: '2026-01-02T00:00:00+00:00', status_changed_at: '2026-01-02T00:00:00+00:00' },
  { name: 'pinned-closed', alias: 'ac', status: 'DONE', engine: 'claude', pinned: true,
    created_at: '2026-01-03T00:00:00+00:00', status_changed_at: '2026-01-03T00:00:00+00:00' },
];

const failures = [];
function check(n, c, d = '') { if (!c) failures.push(`FAIL  ${n}${d ? `\n        ${d}` : ''}`); }

app.state.tasks = structuredClone(TASKS);
app.renderList();
const html = () => app.el('app').innerHTML;

check('the All toggle is gone', !html().includes('id="toggle-all"'));
check('a Refresh button is rendered', html().includes('id="do-refresh"'));
check('it is labelled Refresh', html().includes('>Refresh</button>'));
check('live tasks are listed', html().includes('live-task'));
check('closed tasks are hidden by default', !html().includes('>closed-task<'));
check('a pinned closed task is still shown', html().includes('pinned-closed'));

// Closed tasks must remain reachable now that the toggle is gone.
app.state.draft = 'closed-task';
app.state.query = 'closed-task';
app.renderList();
check('searching reaches a closed task', html().includes('closed-task'));

// Refresh issues a request immediately.
app.state.query = ''; app.state.draft = '';
app.renderList();
app.setFetch(async () => ({ ok: true, status: 200, json: async () => ({ tasks: TASKS }) }));
const before = app.fetches.filter((f) => f.path.startsWith('/tasks?')).length;
await app.el('do-refresh').onclick();
const after = app.fetches.filter((f) => f.path.startsWith('/tasks?')).length;
check('Refresh fetches the list immediately', after === before + 1,
  `before=${before} after=${after}`);
check('it confirms with a toast', app.el('toast').textContent === 'Refreshed',
  `toast=${app.el('toast').textContent}`);

if (failures.length) {
  console.log(failures.join('\n'));
  console.log(`\n${failures.length} refresh assertion(s) FAILED`);
  process.exit(1);
}
console.log('all refresh assertions passed');
