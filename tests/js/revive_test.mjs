/* Behavioural assertions for the revive control on a closed task.
 *
 * A DONE or DISCARDED task has no reply composer, so the bottom bar carries a
 * button that reopens it instead. The two statuses are reopened by different
 * endpoints, and posting to the wrong one is refused by the server rather than
 * silently doing nothing useful — so the endpoint each label posts to is the
 * thing worth pinning here.
 *
 * The fetch stub is a small stand-in for the server: it holds task state,
 * applies the same status precondition, and serves the re-read that follows a
 * successful revive. That is what lets the test assert on the page the user
 * ends up looking at rather than only on the request that was sent.
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
  // renderDetail only wires up ids that exist in the markup it just wrote, so
  // an absent control has to read as absent rather than as an empty stub.
  function __present(id) {
    return __el('app').innerHTML.includes('id="' + id + '"');
  }

  const document = {
    hidden: false, activeElement: null,
    querySelector: (sel) => {
      const id = sel.replace('#', '');
      if (['revive', 'reply', 'send', 'show-more'].includes(id) && !__present(id)) return null;
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

// ── a stand-in server ───────────────────────────────────────────────────

const TASKS = {
  'done-task': { name: 'done-task', alias: null, status: 'DONE', engine: 'claude' },
  'discarded-task': {
    name: 'discarded-task', alias: 'ab', status: 'DISCARDED', engine: 'codex',
  },
  'live-task': { name: 'live-task', alias: 'ac', status: 'AGENT_FINISHED', engine: 'claude' },
};

// undone only accepts a DONE task and undiscard only a DISCARDED one; the
// server answers 409 otherwise, so the stub does too.
const REQUIRED_STATUS = { undone: 'DONE', undiscard: 'DISCARDED' };

app.setFetch(async (path, opts) => {
  const parts = path.split('?')[0].split('/').filter(Boolean);
  const name = decodeURIComponent(parts[1] || '');
  const tail = parts[2] || '';
  const json = (data, status = 200) => ({ ok: status < 400, status, json: async () => data });

  if ((opts || {}).method === 'POST' && REQUIRED_STATUS[tail]) {
    const task = TASKS[name];
    if (task.status !== REQUIRED_STATUS[tail]) {
      return json({ error: `Task is ${task.status}, not ${REQUIRED_STATUS[tail]}` }, 409);
    }
    // What the server does on a revive: reopen it and hand back an alias.
    task.status = 'NEEDS_ATTENTION';
    task.alias = task.alias || 'aa';
    return json({ ok: true, name });
  }
  if (tail === 'tail') {
    return json({ entries: [
      { role: 'user', content: 'hello', timestamp: '2026-01-01T00:00:00+00:00' },
      { role: 'assistant', content: 'hi', timestamp: '2026-01-01T00:00:00+00:00' },
    ] });
  }
  return json({ task: { ...TASKS[name] } });
});

const failures = [];
function check(name, condition, detail = '') {
  if (!condition) failures.push(`FAIL  ${name}${detail ? `\n        ${detail}` : ''}`);
}
const html = () => app.el('app').innerHTML;
const posts = (suffix) => app.fetches.filter(
  (f) => (f.opts || {}).method === 'POST' && f.path.endsWith(suffix),
);
const hasComposerInput = () => html().includes('id="reply"') && html().includes('id="send"');

/** Let the re-render that follows a successful action finish.
 *
 * The action posts and then re-renders without awaiting it, so the click
 * settles before the new markup exists. Yielding to a macrotask drains the
 * whole microtask queue behind it, which is every step of a render whose
 * requests the stub resolves immediately — no guessing at tick counts.
 */
const settle = () => new Promise((resolve) => setImmediate(resolve));

// ── a DONE task ─────────────────────────────────────────────────────────

await app.renderDetail('done-task');
check('a DONE task offers a revive button', html().includes('id="revive"'));
check('it is labelled Undone This Task', html().includes('>Undone This Task</button>'),
  'the label has to name the action it posts');
check('a DONE task has no reply box', !hasComposerInput());
check('the button sits in the bottom bar, where the composer would be',
  /<div class="composer">\s*<button[^>]*id="revive"/.test(html()));

await app.el('revive').onclick();
await settle();
check('clicking it posts to undone', posts('/undone').length === 1,
  `posts=${JSON.stringify(app.fetches.filter((f) => (f.opts || {}).method === 'POST').map((f) => f.path))}`);
check('it posts against the task it was opened for',
  posts('/undone')[0]?.path === '/tasks/done-task/undone');
check('the page is re-rendered with the reopened status',
  html().includes('NEEDS_ATTENTION'));
check('and the reply box comes back with it', hasComposerInput());
check('the revive button is gone once the task is live', !html().includes('id="revive"'));

// ── a DISCARDED task ────────────────────────────────────────────────────

await app.renderDetail('discarded-task');
check('a DISCARDED task offers a revive button too', html().includes('id="revive"'));
check('it is labelled Undiscard This Task',
  html().includes('>Undiscard This Task</button>'));
check('a DISCARDED task is not offered the DONE wording',
  !html().includes('>Undone This Task</button>'),
  'undone would be refused on a DISCARDED task');

await app.el('revive').onclick();
await settle();
check('clicking it posts to undiscard', posts('/undiscard').length === 1);
check('it does not post to undone', posts('/undone').length === 1,
  'the DONE task above accounts for the only undone post');
check('the discarded task is reopened too', html().includes('NEEDS_ATTENTION'));

// ── a live task is untouched ────────────────────────────────────────────

await app.renderDetail('live-task');
check('a live task keeps its reply box', hasComposerInput());
check('a live task offers no revive button', !html().includes('id="revive"'));

// ── a refusal leaves the page alone ─────────────────────────────────────

// The task is live now, so the server refuses to reopen it. Render the closed
// view once more to get hold of the button, then let the refusal come back.
TASKS['done-task'].status = 'DONE';
await app.renderDetail('done-task');
TASKS['done-task'].status = 'WORKING';  // changed underneath, as a second device would

const before = app.fetches.length;
await app.el('revive').onclick();
await settle();
const after = app.fetches.slice(before);
check('a refused revive sends exactly one request', after.length === 1,
  `sent ${after.length}: ${JSON.stringify(after.map((f) => f.path))}`);
check('a refused revive does not re-render', html().includes('id="revive"'),
  'the failed call must not be reported as success');
check('the refusal is shown to the user',
  app.el('toast').textContent.includes('not DONE'),
  `toast=${app.el('toast').textContent}`);

if (failures.length) {
  console.log(failures.join('\n'));
  console.log(`\n${failures.length} revive assertion(s) FAILED`);
  process.exit(1);
}
console.log('all revive assertions passed');
