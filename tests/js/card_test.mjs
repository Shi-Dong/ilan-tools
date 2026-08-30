/* Behavioural assertions for the task card: collapsing, and its two actions.
 *
 * Cards are collapsed by default, so what is stored is the set the user has
 * *expanded*; a task absent from it is collapsed.
 *
 * This harness clicks things, so querySelectorAll returns *stable* stub
 * elements parsed out of the HTML renderList produced — the same object each
 * time, so the onclick renderList assigns is still there when the test calls
 * it. The modal and fetch are stubbed richly enough to drive the Tap
 * confirmation all the way to the request it posts.
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

  // Requests are recorded; the default implementation never settles, which
  // parks the start() boot sequence instead of letting it run.
  const __fetches = [];
  let __fetchImpl = () => new Promise(() => {});
  const fetch = (path, opts) => { __fetches.push({ path, opts }); return __fetchImpl(path, opts); };
  function __setFetch(fn) { __fetchImpl = fn; }

  const __els = new Map();
  function __el(key) {
    if (!__els.has(key)) {
      __els.set(key, {
        id: key, value: '', innerHTML: '', hidden: true, className: '',
        textContent: '', onclick: null, oninput: null, onkeydown: null,
        dataset: {}, focus() {}, classList: { add() {} }, style: {},
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

  // The dialogs build detached nodes, so createElement returns something with
  // enough surface for modal() and its wire() callbacks.
  const __modalEls = new Map();
  function __modalEl(sel) {
    if (!__modalEls.has(sel)) __modalEls.set(sel, { onclick: null, value: '', focus() {} });
    return __modalEls.get(sel);
  }
  let __modalOpen = false;

  const document = {
    hidden: false,
    activeElement: null,
    querySelector: (sel) => __el(sel.replace('#', '')),
    querySelectorAll: (sel) => {
      const html = __el('app').innerHTML;
      const pick = (re, attr) => [...html.matchAll(re)].map((m) => {
        const el = __listEl(sel, m[1]);
        el.dataset[attr] = m[1];
        return el;
      });
      if (sel === '.row') return pick(/class="row" data-toggle="([^"]+)"/g, 'toggle');
      if (sel === '.act-tap') return pick(/data-tap="([^"]+)"/g, 'tap');
      if (sel === '.act-details') return pick(/data-details="([^"]+)"/g, 'details');
      return [];
    },
    addEventListener: () => {},
    body: { appendChild: () => { __modalOpen = true; } },
    createElement: () => ({
      className: '', innerHTML: '',
      addEventListener: () => {},
      remove: () => { __modalOpen = false; },
      querySelector: (s) => __modalEl(s),
      querySelectorAll: () => [],
    }),
  };
  const window = { addEventListener: () => {} };
  const location = { hash: '#/' };
  const setInterval = () => 0;
  const clearInterval = () => {};
  const setTimeout = () => 0;
  const clearTimeout = () => {};
`;

const TAIL = `;return {
  state, renderList, el: __el,
  row: (name) => __listEl('.row', name),
  tapBtn: (name) => __listEl('.act-tap', name),
  detailsBtn: (name) => __listEl('.act-details', name),
  modal: (sel) => __modalEl(sel),
  modalOpen: () => __modalOpen,
  storage: __store,
  fetches: __fetches,
  setFetch: __setFetch,
  location,
};`;

const STORAGE_KEY = 'ilan.expanded';

function bootApp(seed) {
  const prelude = seed === undefined
    ? HARNESS
    : `${HARNESS}\n__store.set('${STORAGE_KEY}', ${JSON.stringify(JSON.stringify(seed))});`;
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
const isCardCollapsed = (html, status) =>
  new RegExp(`class="card rs-${status} collapsed"`).test(html);
const settle = () => new Promise((r) => process.nextTick(r));

/** Click a dialog button, reporting rather than throwing if none is wired.
 *
 * Without this a regression that skips the confirmation entirely dies with a
 * TypeError on a null handler, which says far less than the assertion that
 * was about to run. */
function clickModal(instance, sel, why) {
  const el = instance.modal(sel);
  if (typeof el.onclick !== 'function') {
    failures.push(`FAIL  ${why}\n        no dialog was open to click ${sel}`);
    return false;
  }
  el.onclick();
  return true;
}

// ── collapsed by default, toggled by the card body ──────────────────────
let app = bootApp();
app.state.tasks = structuredClone(TASKS);
app.renderList();
const html = () => app.el('app').innerHTML;

check('a card starts collapsed', isCardCollapsed(html(), 'WORKING'));
check('every card starts collapsed', isCardCollapsed(html(), 'AGENT_FINISHED'));
check('nothing is stored until the user acts',
  app.storage.get(STORAGE_KEY) === undefined);
check('the card body carries the toggle', html().includes('data-toggle="alpha-task"'));
check('a collapsed card reports aria-expanded=false',
  /class="row" data-toggle="alpha-task"[^>]*aria-expanded="false"/s.test(html()));
check('there is no separate chevron control any more', !html().includes('disclose'));

app.row('alpha-task').onclick();
check('tapping the card body expands it', !isCardCollapsed(html(), 'WORKING'));
check('an expanded card reports aria-expanded=true',
  /class="row" data-toggle="alpha-task"[^>]*aria-expanded="true"/s.test(html()));
check('expanding one card leaves the other collapsed',
  isCardCollapsed(html(), 'AGENT_FINISHED'));

app.row('alpha-task').onclick();
check('tapping it again collapses it', isCardCollapsed(html(), 'WORKING'));
check('the expanded set is stored',
  app.storage.get(STORAGE_KEY) === '[]', `stored=${app.storage.get(STORAGE_KEY)}`);

// ── the two actions are rendered on every card ─────────────────────────
check('a Tap button is rendered', html().includes('data-tap="alpha-task"'));
check('a Show Details button is rendered', html().includes('data-details="alpha-task"'));
check('the buttons are labelled', html().includes('>Tap</button>')
  && html().includes('>Show Details</button>'));

// ── Show Details opens the conversation ────────────────────────────────
app.detailsBtn('beta-task').onclick();
check('Show Details navigates to that task',
  app.location.hash === '#/t/beta-task', `hash=${app.location.hash}`);
check('Show Details does not toggle the card',
  isCardCollapsed(html(), 'AGENT_FINISHED'));

// ── Tap asks first ─────────────────────────────────────────────────────
const tapApp = bootApp();
tapApp.state.tasks = structuredClone(TASKS);
tapApp.state.canned = { tap: 'CANNED TAP TEXT', cancel: 'CANNED CANCEL' };
tapApp.renderList();
tapApp.setFetch(async () => ({ ok: true, status: 200, json: async () => ({ ok: true }) }));

const postsTo = (name) => tapApp.fetches.filter(
  (f) => f.path === `/tasks/${name}/reply`);

tapApp.tapBtn('alpha-task').onclick();
await settle();
check('Tap opens a confirmation', tapApp.modalOpen());
check('Tap sends nothing before it is confirmed', postsTo('alpha-task').length === 0,
  `posts=${postsTo('alpha-task').length}`);

// Decline: nothing should be sent.
clickModal(tapApp, '#mc', 'Tap must open a confirmation that can be declined');
await settle();
check('declining sends nothing', postsTo('alpha-task').length === 0,
  `posts=${postsTo('alpha-task').length}`);

// Confirm: the canned tap message is posted to that task.
tapApp.tapBtn('alpha-task').onclick();
await settle();
clickModal(tapApp, '#mo', 'Tap must open a confirmation that can be accepted');
await settle();
await settle();
const posts = postsTo('alpha-task');
check('confirming posts a reply', posts.length === 1, `posts=${posts.length}`);
if (posts.length) {
  const body = JSON.parse(posts[0].opts.body);
  check('the reply carries the canned tap text',
    body.message === 'CANNED TAP TEXT', `message=${JSON.stringify(body.message)}`);
}
check('tapping does not toggle the card',
  isCardCollapsed(tapApp.el('app').innerHTML, 'WORKING'));

// ── persistence still behaves ──────────────────────────────────────────
const restored = bootApp(['beta-task']);
restored.state.tasks = structuredClone(TASKS);
restored.renderList();
check('an expansion is restored on a fresh load',
  !isCardCollapsed(restored.el('app').innerHTML, 'AGENT_FINISHED'));
check('a task absent from storage is still collapsed',
  isCardCollapsed(restored.el('app').innerHTML, 'WORKING'));

const pruned = bootApp(['beta-task', 'task-that-was-deleted']);
pruned.state.tasks = structuredClone(TASKS);
pruned.renderList();
pruned.row('alpha-task').onclick();
const saved = JSON.parse(pruned.storage.get(STORAGE_KEY));
check('a deleted task is pruned from storage',
  !saved.includes('task-that-was-deleted'), `saved=${JSON.stringify(saved)}`);
check('a live expanded task is kept', saved.includes('beta-task'),
  `saved=${JSON.stringify(saved)}`);

// ── storage denied ─────────────────────────────────────────────────────
const noStore = new Function(`${HARNESS}
  localStorage.getItem = () => { throw new Error('denied'); };
  localStorage.setItem = () => { throw new Error('denied'); };
  ${appSource}${TAIL}`)();
noStore.state.tasks = structuredClone(TASKS);
noStore.renderList();
check('cards still start collapsed with storage denied',
  isCardCollapsed(noStore.el('app').innerHTML, 'WORKING'));
noStore.row('alpha-task').onclick();
check('expanding still works with storage denied',
  !isCardCollapsed(noStore.el('app').innerHTML, 'WORKING'));

if (failures.length) {
  console.log(failures.join('\n'));
  console.log(`\n${failures.length} card assertion(s) FAILED`);
  process.exit(1);
}
console.log('all card assertions passed');
