/* Assertions for the list header's Refresh button.
 *
 * The All toggle is gone, so closed tasks are reachable only by searching —
 * which is asserted here, since dropping the toggle without that path would
 * make DONE and DISCARDED tasks unreachable from the phone entirely.
 */

import { bootApp, checker } from './harness.mjs';

const TASKS = [
  { name: 'live-task', alias: 'aa', status: 'WORKING', engine: 'claude',
    created_at: '2026-01-01T00:00:00+00:00', status_changed_at: '2026-01-01T00:00:00+00:00' },
  { name: 'closed-task', alias: 'ab', status: 'DONE', engine: 'claude',
    created_at: '2026-01-02T00:00:00+00:00', status_changed_at: '2026-01-02T00:00:00+00:00' },
  { name: 'pinned-closed', alias: 'ac', status: 'DONE', engine: 'claude', pinned: true,
    created_at: '2026-01-03T00:00:00+00:00', status_changed_at: '2026-01-03T00:00:00+00:00' },
];

const { check, report } = checker();
const app = bootApp();
const html = app.html;

app.state.tasks = structuredClone(TASKS);
app.renderList();

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
app.state.query = '';
app.state.draft = '';
app.renderList();
app.setFetch(async () => ({ ok: true, status: 200, json: async () => ({ tasks: TASKS }) }));
const listReads = () => app.fetches.filter((f) => f.path.startsWith('/tasks?')).length;
const before = listReads();
await app.el('do-refresh').onclick();
check('Refresh fetches the list immediately', listReads() === before + 1,
  `before=${before} after=${listReads()}`);
check('it confirms with a toast', app.el('toast').textContent === 'Refreshed',
  `toast=${app.el('toast').textContent}`);

report('refresh');
