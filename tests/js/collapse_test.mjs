/* Behavioural assertions for collapsible task cards.
 *
 * Unlike the other harnesses this one has to click things, so querySelectorAll
 * returns *stable* stub elements parsed out of the HTML renderList produced —
 * the same object each time, so the onclick renderList assigns is still there
 * when the test calls it. That means the wiring is exercised, not bypassed.
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
    .replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;')
    .replaceAll('"','&quot;').replaceAll("'",'&#39;') };

  const __store = new Map();
  const localStorage = {
    getItem: (k) => (__store.has(k) ? __store.get(k) : null),
    setItem: (k, v) => { __store.set(k, String(v)); },
    removeItem: (k) => { __store.delete(k); },
  };

  const __els = new Map();
  function __el(key) {
    if (!__els.has(key)) {
      __els.set(key, {
        id: key, value: '', innerHTML: '', hidden: true, className: '',
        onclick: null, oninput: null, onkeydown: null, dataset: {},
        focus() {}, classList: { add() {} }, style: {},
      });
    }
    return __els.get(key);
  }

  // Stable per (selector, key) so a handler assigned on one render is still
  // reachable afterwards.
  const __lists = new Map();
  function __listEl(sel, key) {
    const id = sel + '|' + key;
    if (!__lists.has(id)) {
      __lists.set(id, { dataset: {}, onclick: null, classList: { add() {} } });
    }
    return __lists.get(id);
  }

  const document = {
    hidden: false,
    activeElement: null,
    querySelector: (sel) => __el(sel.replace('#', '')),
    querySelectorAll: (sel) => {
      const html = __el('app').innerHTML;
      if (sel === '.disclose') {
        return [...html.matchAll(/data-toggle="([^"]+)"/g)].map((m) => {
          const el = __listEl(sel, m[1]);
          el.dataset.toggle = m[1];
          return el;
        });
      }
      if (sel === '.row') {
        return [...html.matchAll(/class="row" data-name="([^"]+)"/g)].map((m) => {
          const el = __listEl(sel, m[1]);
          el.dataset.name = m[1];
          return el;
        });
      }
      return [];
    },
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

const TAIL = `;return {
  state, renderList, el: __el,
  disclose: (name) => __listEl('.disclose', name),
  storage: __store,
};`;

/** A fresh app instance, optionally with localStorage pre-seeded. */
function bootApp(seed) {
  const prelude = seed === undefined
    ? HARNESS
    : `${HARNESS}\n__store.set('ilan.collapsed', ${JSON.stringify(JSON.stringify(seed))});`;
  return new Function(`${prelude}\n${appSource}\n${TAIL}`)();
}

const TASKS = [
  { name: 'alpha-task', alias: 'aa', status: 'WORKING', engine: 'claude',
    summary_one_liner: 'alpha summary',
    created_at: '2026-01-01T00:00:00+00:00',
    status_changed_at: '2026-01-01T00:00:00+00:00' },
  { name: 'beta-task', alias: 'ab', status: 'AGENT_FINISHED', engine: 'codex',
    summary_one_liner: 'beta summary',
    created_at: '2026-01-02T00:00:00+00:00',
    status_changed_at: '2026-01-02T00:00:00+00:00' },
];

const failures = [];
function check(name, condition, detail = '') {
  if (!condition) failures.push(`FAIL  ${name}${detail ? `\n        ${detail}` : ''}`);
}

// ── default is expanded ─────────────────────────────────────────────────
let app = bootApp();
app.state.tasks = structuredClone(TASKS);
app.renderList();
let html = () => app.el('app').innerHTML;

check('cards start expanded', !html().includes('card rs-WORKING collapsed'));
check('summary shown when expanded', html().includes('alpha summary'));
check('a disclosure control is rendered', html().includes('data-toggle="alpha-task"'));
check('expanded control reports aria-expanded=true',
  /data-toggle="alpha-task"[^>]*aria-expanded="true"/s.test(html()));

// ── collapsing one card ─────────────────────────────────────────────────
app.disclose('alpha-task').onclick();
check('collapsing marks that card', html().includes('card rs-WORKING collapsed'));
check('collapsed control reports aria-expanded=false',
  /data-toggle="alpha-task"[^>]*aria-expanded="false"/s.test(html()));
check('collapsing does not touch the other card',
  !html().includes('card rs-AGENT_FINISHED collapsed'));
check('the other summary is still rendered', html().includes('beta summary'));
// The summary stays in the DOM and is hidden by CSS, so assert the class the
// stylesheet keys on rather than the text disappearing.
check('collapsed card carries the class the stylesheet hides content with',
  /class="card rs-WORKING collapsed"/.test(html()));

// ── expanding again ─────────────────────────────────────────────────────
app.disclose('alpha-task').onclick();
check('expanding clears the class', !html().includes('collapsed'));

// ── persistence ─────────────────────────────────────────────────────────
app.disclose('beta-task').onclick();
check('collapse is written to storage',
  app.storage.get('ilan.collapsed') === '["beta-task"]',
  `stored=${app.storage.get('ilan.collapsed')}`);

const restored = bootApp(['beta-task']);
restored.state.tasks = structuredClone(TASKS);
restored.renderList();
check('collapse is restored on a fresh load',
  restored.el('app').innerHTML.includes('card rs-AGENT_FINISHED collapsed'));

// ── stale names are pruned ──────────────────────────────────────────────
const pruned = bootApp(['beta-task', 'task-that-was-deleted']);
pruned.state.tasks = structuredClone(TASKS);
pruned.renderList();
pruned.disclose('alpha-task').onclick();   // any toggle triggers a save
const saved = JSON.parse(pruned.storage.get('ilan.collapsed'));
check('a deleted task is dropped from storage',
  !saved.includes('task-that-was-deleted'), `saved=${JSON.stringify(saved)}`);
check('a live collapsed task is kept', saved.includes('beta-task'),
  `saved=${JSON.stringify(saved)}`);

// ── storage being unavailable must not break collapsing ─────────────────
const noStore = new Function(`${HARNESS}
  localStorage.getItem = () => { throw new Error('denied'); };
  localStorage.setItem = () => { throw new Error('denied'); };
  ${appSource}${TAIL}`)();
noStore.state.tasks = structuredClone(TASKS);
noStore.renderList();
noStore.disclose('alpha-task').onclick();
check('collapsing still works with storage denied',
  noStore.el('app').innerHTML.includes('card rs-WORKING collapsed'));

if (failures.length) {
  console.log(failures.join('\n'));
  console.log(`\n${failures.length} collapse assertion(s) FAILED`);
  process.exit(1);
}
console.log('all collapse assertions passed');
