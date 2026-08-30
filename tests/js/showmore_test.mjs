/* Assertions for the conversation's Show More control.
 *
 * The reveal itself is the server's `?n=` slice, so what matters here is that
 * the app asks for the right N: one to begin with, one more per click, and
 * back to one when a different task is opened.
 */

import { bootApp, checker } from './harness.mjs';

const { check, report } = checker();
const app = bootApp();
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

const html = app.html;
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

// ── navigating into a task resets the reveal ───────────────────────────
// Navigation is what resets it, so these go through route() rather than
// calling renderDetail directly — that is the path a tap actually takes.
app.location.hash = '#/t/beta-task';
await app.route();
check('a different task resets the reveal to one', lastN() === 1, `n=${lastN()}`);
check('and shows a single assistant message', shownAssistants() === 1,
  `shown=${shownAssistants()}`);

// ── re-entering the SAME task also resets ──────────────────────────────
// The reported bug: expanding a task, going back, and opening it again used
// to leave it expanded, because the reset keyed off the task name changing.
app.location.hash = '#/t/alpha-task';
await app.route();
await app.el('show-more').onclick();
await app.el('show-more').onclick();
check('the same task can be expanded', lastN() === 3, `n=${lastN()}`);

app.location.hash = '#/';           // back to the list
app.location.hash = '#/t/alpha-task';
await app.route();
check('re-opening the same task starts from the tail again', lastN() === 1,
  `n=${lastN()}`);
check('and shows one assistant message again', shownAssistants() === 1,
  `shown=${shownAssistants()}`);

// ── Show More is reachable without scrolling ───────────────────────────
check('Show More renders inside the sticky header',
  /<header class="hdr">[\s\S]*id="show-more"[\s\S]*<\/header>/.test(html()),
  'it is in the scrolling body, so a long thread hides it');

report('show-more');
