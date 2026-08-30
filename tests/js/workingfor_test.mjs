/* Assertions for the WORKING duration shown beside the status.
 *
 * The clock is read from Date.now(), so each case pins status_changed_at at a
 * known offset from now rather than at a fixed timestamp.
 */

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const appSource = readFileSync(
  join(here, '..', '..', 'src', 'ilan', 'web', 'static', 'app.js'), 'utf8',
);

const HARNESS = `
  const MD = { escapeHtml: (v) => String(v ?? '')
    .replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;'),
    render: (v) => String(v ?? '') };
  const localStorage = { getItem: () => null, setItem: () => {} };
  const fetch = () => new Promise(() => {});
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
const TAIL = `;return { state, renderList, el: __el,
  formatHoursMinutes, statusLabel };`;

const app = new Function(`${HARNESS}\n${appSource}\n${TAIL}`)();

const failures = [];
function check(n, c, d = '') { if (!c) failures.push(`FAIL  ${n}${d ? `\n        ${d}` : ''}`); }

// ── the formatter ───────────────────────────────────────────────────────
const f = app.formatHoursMinutes;
const cases = [
  [0, '0m'], [11, '0m'], [59, '0m'],
  [60, '1m'], [12 * 60, '12m'], [59 * 60, '59m'],
  [3600, '1h0m'], [2 * 3600 + 38 * 60, '2h38m'],
  [2 * 3600 + 5 * 60, '2h5m'], [30 * 3600 + 5 * 60, '30h5m'],
];
for (const [secs, want] of cases) {
  check(`${secs}s renders as ${want}`, f(secs) === want, `got ${f(secs)}`);
}
check('never renders seconds', !cases.some(([s]) => /\ds\b/.test(f(s))));

// ── the label ───────────────────────────────────────────────────────────
const ago = (secs) => new Date(Date.now() - secs * 1000).toISOString();

check('a WORKING task reports how long it has been working',
  app.statusLabel({ status: 'WORKING', status_changed_at: ago(12 * 60) })
    === 'WORKING (for 12m)',
  app.statusLabel({ status: 'WORKING', status_changed_at: ago(12 * 60) }));

check('hours and minutes together',
  app.statusLabel({ status: 'WORKING', status_changed_at: ago(2 * 3600 + 38 * 60) })
    === 'WORKING (for 2h38m)');

check('a finished task gets no duration',
  app.statusLabel({ status: 'AGENT_FINISHED', status_changed_at: ago(600) })
    === 'AGENT_FINISHED');

check('a looping task keeps its own label',
  app.statusLabel({ status: 'AGENT_FINISHED', reply_every_seconds: 3600,
                    status_changed_at: ago(600) }) === 'AGENT_IN_LOOP');

check('an unreadable timestamp degrades to the bare status',
  app.statusLabel({ status: 'WORKING', status_changed_at: 'not-a-date' }) === 'WORKING');
check('a missing timestamp degrades to the bare status',
  app.statusLabel({ status: 'WORKING' }) === 'WORKING');

// ── visible collapsed and expanded ──────────────────────────────────────
const TASKS = [{
  name: 'busy-task', alias: 'aa', status: 'WORKING', engine: 'claude',
  summary_one_liner: 'a summary',
  created_at: ago(9 * 3600), status_changed_at: ago(2 * 3600 + 38 * 60),
}];

app.state.tasks = structuredClone(TASKS);
app.state.expanded = new Set();
app.renderList();
check('shown on a collapsed card',
  app.el('app').innerHTML.includes('WORKING (for 2h38m)'),
  app.el('app').innerHTML.slice(0, 400));

app.state.expanded = new Set(['busy-task']);
app.renderList();
check('shown on an expanded card',
  app.el('app').innerHTML.includes('WORKING (for 2h38m)'));

if (failures.length) {
  console.log(failures.join('\n'));
  console.log(`\n${failures.length} working-for assertion(s) FAILED`);
  process.exit(1);
}
console.log('all working-for assertions passed');
