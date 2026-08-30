/* Assertions for the task card: collapsing, and its three actions.
 *
 * Cards are collapsed by default, so what is stored is the set the user has
 * *expanded*; a task absent from it is collapsed.
 */

import { bootApp, checker, settle, EXPANDED_KEY as STORAGE_KEY } from './harness.mjs';

const { check, clickModal, report } = checker();
const TASKS = [
  { name: 'alpha-task', alias: 'aa', status: 'WORKING', engine: 'claude',
    summary_one_liner: 'alpha summary',
    created_at: '2026-01-01T00:00:00+00:00',
    status_changed_at: '2026-01-01T00:00:00+00:00' },
  { name: 'beta-task', alias: 'ab', status: 'AGENT_FINISHED', engine: 'codex',
    summary_one_liner: 'beta summary',
    created_at: '2026-01-02T00:00:00+00:00',
    status_changed_at: '2026-01-02T00:00:00+00:00' },
  // Pinned, because that is the only way a closed task appears in the default
  // listing at all — and it is the case that has to *not* offer Done.
  { name: 'gamma-task', alias: null, status: 'DONE', engine: 'claude',
    pinned: true, summary_one_liner: 'gamma summary',
    created_at: '2026-01-03T00:00:00+00:00',
    status_changed_at: '2026-01-03T00:00:00+00:00' },
];

const isCardCollapsed = (html, status) =>
  new RegExp(`class="card rs-${status} collapsed"`).test(html);

// ── collapsed by default, toggled by the card body ──────────────────────
const app = bootApp();
app.state.tasks = structuredClone(TASKS);
app.renderList();
const html = app.html;

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

// ── the actions are rendered on every card ─────────────────────────────
check('a Tap button is rendered', html().includes('data-tap="alpha-task"'));
check('a Show Details button is rendered', html().includes('data-details="alpha-task"'));
check('a Done button is rendered', html().includes('data-done="alpha-task"'));
check('the buttons are labelled', html().includes('>Tap</button>')
  && html().includes('>Show Details</button>') && html().includes('>Done</button>'));
// Tap, then Done, then Show Details: the two that act on the task sit
// together, and the one that navigates away is last.
check('the buttons run Tap, Done, Show Details',
  /data-tap="alpha-task"[\s\S]*?data-done="alpha-task"[\s\S]*?data-details="alpha-task"/
    .test(html()));
// The status is humanised for reading only. The class it colours the card
// with still carries the underscored value — humanise a step too early and the
// class becomes `rs-AGENT FINISHED`, which matches nothing, and the card
// silently loses its colour while still looking perfectly fine.
check('the card is classed with the underscored status',
  html().includes('class="card rs-AGENT_FINISHED'), 'the status class was humanised too');
check('the status span is classed with it too',
  html().includes('class="status st-AGENT_FINISHED"'));
check('but the text a reader sees has no underscore',
  html().includes('>AGENT FINISHED<'), 'the label still shows an underscore');
check('no class name picked up a space',
  !/class="[^"]*rs-[A-Z]+ [A-Z]/.test(html()));

check('Tap carries the yellow fill class', html().includes('btn-tap act-tap'));
check('Done carries the green fill class', html().includes('btn-done act-done'));
check('Show Details carries the blue fill class',
  html().includes('btn-primary act-details'));

// A closed task gets only Show Details. Neither of the other two means
// anything once a task is closed: there is no agent left to tap, and closing
// something already DONE is not an action worth offering. The actions sheet
// has always applied the same rule to both.
check('a closed task is not offered Done', !html().includes('data-done="gamma-task"'));
check('a closed task is not offered Tap', !html().includes('data-tap="gamma-task"'),
  'tapping a closed task would message an agent that is no longer running');
check('a closed task still offers Show Details',
  html().includes('data-details="gamma-task"'));

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

// ── Done asks first, then closes ───────────────────────────────────────
const doneApp = bootApp();
doneApp.state.tasks = structuredClone(TASKS);
doneApp.renderList();
doneApp.setFetch(async (path) => ({
  ok: true,
  status: 200,
  json: async () => (path.startsWith('/tasks?') ? { tasks: [] } : { ok: true }),
}));

const doneposts = (name) => doneApp.fetches.filter(
  (f) => f.path === `/tasks/${name}/done`);
const listReads = () => doneApp.fetches.filter((f) => f.path.startsWith('/tasks?'));

doneApp.doneBtn('beta-task').onclick();
await settle();
check('Done opens a confirmation', doneApp.modalOpen());
check('Done closes nothing before it is confirmed', doneposts('beta-task').length === 0,
  `posts=${doneposts('beta-task').length}`);

clickModal(doneApp, '#mc', 'Done must open a confirmation that can be declined');
await settle();
check('declining closes nothing', doneposts('beta-task').length === 0,
  `posts=${doneposts('beta-task').length}`);

const readsBefore = listReads().length;
const expandedBefore = doneApp.storage.get(STORAGE_KEY);
doneApp.doneBtn('beta-task').onclick();
await settle();
clickModal(doneApp, '#mo', 'Done must open a confirmation that can be accepted');
await settle();
await settle();
check('confirming closes the task', doneposts('beta-task').length === 1,
  `posts=${doneposts('beta-task').length}`);
check('it posts against the card it was pressed on',
  doneposts('alpha-task').length === 0);
check('the list is reloaded so the closed task drops out of it',
  listReads().length === readsBefore + 1,
  `reads went ${readsBefore} -> ${listReads().length}`);
// The card itself is gone by now — that is what reloading was for — so what
// is left to check is that pressing Done never toggled anything on the way.
check('pressing Done does not toggle the card',
  doneApp.storage.get(STORAGE_KEY) === expandedBefore,
  `expanded went ${expandedBefore} -> ${doneApp.storage.get(STORAGE_KEY)}`);
check('the closed task is gone from the reloaded list',
  !doneApp.el('app').innerHTML.includes('beta-task'));

// A running task is killed by this, which the confirmation has to say.
const killApp = bootApp();
killApp.state.tasks = structuredClone(TASKS);
killApp.renderList();
killApp.setFetch(async () => ({ ok: true, status: 200, json: async () => ({ ok: true }) }));
killApp.doneBtn('alpha-task').onclick();
await settle();
check('closing a WORKING task warns that the agent is stopped',
  /stops the agent/.test(killApp.modalTitle()),
  `title=${JSON.stringify(killApp.modalTitle())}`);

killApp.doneBtn('beta-task').onclick();
await settle();
check('closing a task with no agent running does not claim to stop one',
  !/stops the agent/.test(killApp.modalTitle()),
  `title=${JSON.stringify(killApp.modalTitle())}`);

// ── persistence still behaves ──────────────────────────────────────────
const restored = bootApp({ expanded: ['beta-task'] });
restored.state.tasks = structuredClone(TASKS);
restored.renderList();
check('an expansion is restored on a fresh load',
  !isCardCollapsed(restored.el('app').innerHTML, 'AGENT_FINISHED'));
check('a task absent from storage is still collapsed',
  isCardCollapsed(restored.el('app').innerHTML, 'WORKING'));

const pruned = bootApp({ expanded: ['beta-task', 'task-that-was-deleted'] });
pruned.state.tasks = structuredClone(TASKS);
pruned.renderList();
pruned.row('alpha-task').onclick();
const saved = JSON.parse(pruned.storage.get(STORAGE_KEY));
check('a deleted task is pruned from storage',
  !saved.includes('task-that-was-deleted'), `saved=${JSON.stringify(saved)}`);
check('a live expanded task is kept', saved.includes('beta-task'),
  `saved=${JSON.stringify(saved)}`);

// ── storage denied ─────────────────────────────────────────────────────
const noStore = bootApp({ storage: 'denied' });
noStore.state.tasks = structuredClone(TASKS);
noStore.renderList();
check('cards still start collapsed with storage denied',
  isCardCollapsed(noStore.el('app').innerHTML, 'WORKING'));
noStore.row('alpha-task').onclick();
check('expanding still works with storage denied',
  !isCardCollapsed(noStore.el('app').innerHTML, 'WORKING'));

report('card');
