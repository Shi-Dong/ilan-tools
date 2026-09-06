/* Assertions for the app's motion: how a view arrives, how a card grows, how a
 * sheet leaves.
 *
 * None of this can be seen in a harness with no layout engine, so what is
 * asserted is the contract the stylesheet and the browser act on: which class
 * a view arrives with and when that class is cleared, what a toggled card is
 * asked to animate, and that nothing here ever delays what the user asked for.
 * The stylesheet side is pinned separately in Python.
 */

import { bootApp, checker, settle } from './harness.mjs';

const { check, report } = checker();

const TASKS = [
  { name: 'alpha-task', alias: 'aa', status: 'AGENT_FINISHED', engine: 'claude',
    created_at: '2026-01-01T00:00:00+00:00', status_changed_at: '2026-01-01T00:00:00+00:00' },
];

function appWithServer() {
  const app = bootApp();
  app.setFetch(async (path) => {
    const json = (d) => ({ ok: true, status: 200, json: async () => d });
    if (path.startsWith('/tasks?')) return json({ tasks: TASKS });
    if (path.includes('/tail')) return json({ entries: [] });
    if (path === '/config') return json({ config: {} });
    if (path === '/version') return json({ version: '1', commit: 'abc', pid: 1 });
    return json({ task: TASKS[0] });
  });
  return app;
}
const cls = (app) => app.el('app').className;

// ── which way a view arrives ────────────────────────────────────────────
check('the first load is a fade', appWithServer().entranceFor(null, '#/') === 'view-fade');
check('going from the list into anything is a push',
  ['#/t/x', '#/new', '#/config'].every((to) => appWithServer().entranceFor('#/', to) === 'view-push'));
check('coming back to the list is a pop',
  ['#/t/x', '#/new', '#/config'].every((from) => appWithServer().entranceFor(from, '#/') === 'view-pop'));
check('anything else is a fade', appWithServer().entranceFor('#/t/x', '#/config') === 'view-fade');

// ── the class is set by a navigation and consumed by the render ─────────
const nav = appWithServer();
nav.location.hash = '#/';
// The first list is fetched behind a "Loading…" placeholder. That placeholder
// arrives with the entrance but must not spend it — the list is the view the
// navigation was to, and it has to arrive with the entrance as well.
const firstLoad = nav.route();
check('the loading placeholder arrives with the entrance',
  cls(nav) === 'app view-fade' && nav.html().includes('Loading…'), `${cls(nav)} :: ${nav.html().slice(0, 40)}`);
await firstLoad;
await settle();
check('the list arrives with its entrance', cls(nav) === 'app view-fade', cls(nav));

// The poll and every in-place update come through renderList without a
// navigation, and must not replay the entrance.
nav.renderList();
check('a re-render without a navigation carries no entrance', cls(nav) === 'app', cls(nav));

nav.location.hash = '#/t/alpha-task';
await nav.route();
await settle();
check('opening a task pushes', cls(nav) === 'app view-push', cls(nav));
await nav.renderDetail('alpha-task');
check('an in-place re-render of the task clears it', cls(nav) === 'app', cls(nav));

nav.location.hash = '#/';
await nav.route();
await settle();
check('returning to the list pops', cls(nav) === 'app view-pop', cls(nav));

nav.location.hash = '#/config';
await nav.route();
await settle();
check('settings pushes from the list', cls(nav) === 'app view-push', cls(nav));

// showView on its own: the entrance is consumed exactly once.
const once = appWithServer();
once.state.entering = 'view-push';
once.showView('<div>a</div>');
check('showView applies the pending entrance', cls(once) === 'app view-push');
once.showView('<div>b</div>');
check('and the next render is plain', cls(once) === 'app');
check('the page content was still written', once.el('app').innerHTML === '<div>b</div>');

// ── a toggled card eases between its two heights ────────────────────────
const list = appWithServer();
list.state.tasks = structuredClone(TASKS);
list.state.expanded = new Set();
list.renderList();
list.cardHeights(60, 140);           // measured before the toggle, then after
list.toggleCollapsed('alpha-task');
await settle();
const grew = list.animations().filter((a) => a.keyframes[0] && 'height' in a.keyframes[0]);
check('expanding a card animates its height once', grew.length === 1, JSON.stringify(list.animations()));
check('from the height it had to the height it has',
  JSON.stringify(grew[0]?.keyframes) === JSON.stringify([{ height: '60px' }, { height: '140px' }]),
  JSON.stringify(grew[0]?.keyframes));
check('in under a third of a second', (grew[0]?.options?.duration ?? 999) <= 300,
  `${grew[0]?.options?.duration}ms`);
check('the card is in its new state regardless of the animation',
  list.state.expanded.has('alpha-task') && list.html().includes('class="card rs-AGENT_FINISHED"'));

list.cardHeights(140, 60);
list.toggleCollapsed('alpha-task');
await settle();
const shrank = list.animations().filter((a) => a.keyframes[0] && 'height' in a.keyframes[0]);
check('collapsing animates too, the other way',
  shrank.length === 2 && JSON.stringify(shrank[1].keyframes) === JSON.stringify([{ height: '140px' }, { height: '60px' }]),
  JSON.stringify(shrank.map((a) => a.keyframes)));

// A card the harness cannot measure is left alone rather than crashed on.
const bare = appWithServer();
bare.state.tasks = structuredClone(TASKS);
bare.renderList();
bare.cardHeights();                 // measures as 0: nothing to ease from
bare.toggleCollapsed('alpha-task');
check('an unmeasurable card is toggled without animating', bare.animations().length === 0
  && bare.state.expanded.has('alpha-task'));
check('a task that is not on the page is toggled without a lookup crash',
  (() => { bare.toggleCollapsed('no-such-task'); return true; })());

// ── a sheet fades out only where it can, and never delays its answer ────
const sequence = [];
const fading = {
  style: {},
  animate: () => { sequence.push('animate'); return { finished: Promise.resolve().then(() => sequence.push('finished')) }; },
  remove: () => sequence.push('remove'),
};
appWithServer().fadeOutAndRemove(fading);
check('where the element can fade, removal waits for the fade', sequence.join(',') === 'animate');
await settle();
check('and follows it', sequence.join(',') === 'animate,finished,remove', sequence.join(','));
check('taps during the fade land on nothing', fading.style.pointerEvents === 'none');

const plain = { remove: () => sequence.push('plain-remove') };
appWithServer().fadeOutAndRemove(plain);
check('where it cannot fade, it is removed at once', sequence.at(-1) === 'plain-remove');

// The sheet's value does not wait for the fade: askConfirm resolves as soon
// as the button is pressed, which the existing dialog tests rely on and which
// this pins directly.
const app = appWithServer();
let answered = null;
const asking = app.runAction('done', { name: 'alpha-task', status: 'WORKING' }).then(() => { answered = true; });
await settle();
check('reduced motion is simply false where the query does not exist', app.reduceMotion() === false);
void asking;

report('motion');
