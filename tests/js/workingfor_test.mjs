/* Assertions for the WORKING duration shown beside the status.
 *
 * The formatter is checked directly, then the label it feeds, then that the
 * label survives both card states — a duration that only appeared on an
 * expanded card would be invisible in normal use, since cards are collapsed by
 * default.
 */

import { bootApp, checker } from './harness.mjs';

const { check, report } = checker();
const app = bootApp();

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
const label = (task) => app.statusLabel(task);

check('a WORKING task reports how long it has been working',
  label({ status: 'WORKING', status_changed_at: ago(12 * 60) }) === 'WORKING (for 12m)',
  label({ status: 'WORKING', status_changed_at: ago(12 * 60) }));

check('hours and minutes together',
  label({ status: 'WORKING', status_changed_at: ago(2 * 3600 + 38 * 60) })
    === 'WORKING (for 2h38m)');

check('a finished task gets no duration',
  label({ status: 'AGENT_FINISHED', status_changed_at: ago(600) }) === 'AGENT_FINISHED');

check('a looping task keeps its own label',
  label({ status: 'AGENT_FINISHED', reply_every_seconds: 3600, status_changed_at: ago(600) })
    === 'AGENT_IN_LOOP');

check('an unreadable timestamp degrades to the bare status',
  label({ status: 'WORKING', status_changed_at: 'not-a-date' }) === 'WORKING');
check('a missing timestamp degrades to the bare status',
  label({ status: 'WORKING' }) === 'WORKING');

// ── visible collapsed and expanded ──────────────────────────────────────
app.state.tasks = [{
  name: 'busy-task', alias: 'aa', status: 'WORKING', engine: 'claude',
  summary_one_liner: 'a summary',
  created_at: ago(9 * 3600), status_changed_at: ago(2 * 3600 + 38 * 60),
}];

app.state.expanded = new Set();
app.renderList();
check('shown on a collapsed card', app.html().includes('WORKING (for 2h38m)'),
  app.html().slice(0, 400));

app.state.expanded = new Set(['busy-task']);
app.renderList();
check('shown on an expanded card', app.html().includes('WORKING (for 2h38m)'));

report('working-for');
