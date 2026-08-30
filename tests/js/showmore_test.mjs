/* Behavioural assertions for the conversation's Show More control.
 *
 * The reveal itself is the server's `?n=` slice, so what matters here is that
 * the app asks for the right N: one to begin with, one more per click, and
 * back to one when a different task is opened.
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
    .replaceAll('"','&quot;').replaceAll("'",'&#39;'),
    render: (v) => String(v ?? '') };

  const __store = new Map();
  const localStorage = {
    getItem: (k) => (__store.has(k) ? __store.get(k) : null),
    setItem: (k, v) => { __store.set(k, String(v)); },
  };

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
  // renderDetail only looks up ids that exist in the markup it just wrote, so
  // report a missing #show-more as absent rather than handing back a stub.
  function __present(id) {
    return __el('app').innerHTML.includes('id="' + id + '"');
  }

  const document = {
    hidden: false, activeElement: null,
    querySelector: (sel) => {
      const id = sel.replace('#', '');
      if (['show-more', 'reply', 'send'].includes(id) && !__present(id)) return null;
      return __el(id);
    },
    querySelectorAll: () => [],
    addEventListener: () => {},
    body: { appendChild: () => {} },
    createElement: () => ({ addEventListener: () => {}, classList: { add() {} },
      querySelector: () => ({ onclick: null, focus() {} }) }),
  };
  const window = { addEventListener: () => {} };
  const location = { hash: '#/' };
  const setInterval = () => 0;
  const clearInterval = () => {};
  const setTimeout = () => 0;
  const clearTimeout = () => {};
`;

const TAIL = `;return {
  state, renderDetail, el: __el, fetches: __fetches, setFetch: __setFetch,
};`;

const app = new Function(`${HARNESS}\n${appSource}\n${TAIL}`)();

/** Build a conversation of `pairs` user/assistant exchanges. */
function conversation(pairs) {
  const out = [];
  for (let i = 1; i <= pairs; i += 1) {
    out.push({ role: 'user', content: `user ${i}`, timestamp: '2026-01-01T00:00:00+00:00' });
    out.push({ role: 'assistant', content: `assistant ${i}`, timestamp: '2026-01-01T00:00:00+00:00' });
  }
  return out;
}

const TOTAL_PAIRS = 4;

// Serve the same slice the real endpoint would for a given ?n=.
app.setFetch(async (path) => {
  if (path.startsWith('/tasks/') && path.includes('/tail')) {
    const n = Number(new URL(`http://x${path}`).searchParams.get('n'));
    const all = conversation(TOTAL_PAIRS);
    const asstIdx = all.map((e, i) => (e.role === 'assistant' ? i : -1)).filter((i) => i >= 0);
    const entries = asstIdx.length <= n ? all : all.slice(asstIdx[asstIdx.length - n - 1] + 1);
    return { ok: true, status: 200, json: async () => ({ entries }) };
  }
  return {
    ok: true,
    status: 200,
    json: async () => ({ task: {
      name: path.split('/')[2], alias: 'aa', status: 'AGENT_FINISHED',
      engine: 'claude', prompt: 'p',
    } }),
  };
});

const failures = [];
function check(name, condition, detail = '') {
  if (!condition) failures.push(`FAIL  ${name}${detail ? `\n        ${detail}` : ''}`);
}
const html = () => app.el('app').innerHTML;
const tailCalls = () => app.fetches.filter((f) => f.path.includes('/tail'));
const lastN = () => Number(new URL(`http://x${tailCalls().at(-1).path}`).searchParams.get('n'));
const shownAssistants = () => (html().match(/assistant \d+/g) || []).length;

await app.renderDetail('alpha-task');
check('the first render asks for one assistant message', lastN() === 1, `n=${lastN()}`);
check('one assistant message is shown', shownAssistants() === 1,
  `shown=${shownAssistants()}`);
check('its preceding user message comes with it', html().includes('user 4'));
check('older exchanges are not shown yet', !html().includes('assistant 3'));
check('Show More is offered', html().includes('id="show-more"'));
check('the Prompt view is gone', !html().includes('data-view="prompt"'));
check('the Full log view is gone', !html().includes('>Full log<'));

await app.el('show-more').onclick();
check('one click asks for two', lastN() === 2, `n=${lastN()}`);
check('one more assistant message appears', shownAssistants() === 2,
  `shown=${shownAssistants()}`);
check('and the user message before it', html().includes('user 3'));

await app.el('show-more').onclick();
await app.el('show-more').onclick();
check('three clicks asks for four', lastN() === 4, `n=${lastN()}`);
check('the whole conversation is shown', shownAssistants() === TOTAL_PAIRS,
  `shown=${shownAssistants()}`);
check('the first message is now visible', html().includes('user 1'));

// One further click returns fewer assistants than requested, which is how the
// app learns there is nothing left.
await app.el('show-more').onclick();
check('Show More disappears at the end of the conversation',
  !html().includes('id="show-more"'));

// ── opening another task starts over ───────────────────────────────────
await app.renderDetail('beta-task');
check('a different task resets the reveal to one', lastN() === 1, `n=${lastN()}`);
check('and shows a single assistant message', shownAssistants() === 1,
  `shown=${shownAssistants()}`);

// ── returning to the first task also starts from the tail ──────────────
await app.renderDetail('alpha-task');
check('returning to a task starts from the tail again', lastN() === 1, `n=${lastN()}`);

if (failures.length) {
  console.log(failures.join('\n'));
  console.log(`\n${failures.length} show-more assertion(s) FAILED`);
  process.exit(1);
}
console.log('all show-more assertions passed');
