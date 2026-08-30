/* Render task rows through the real renderList and report the sleep suffix
 * each one produced, as JSON, for cross-checking against the CLI's
 * _format_sleep_suffix and its WORKING-only rule.
 *
 * Reads its cases from argv so the Python side owns the list, and can build
 * the expectations from the CLI's own helper rather than from a literal.
 */

import { bootApp } from './harness.mjs';

const app = bootApp();

// [label, status, sleep_seconds]
const CASES = JSON.parse(process.argv[2]);

const out = {};
for (const [label, status, sleepSeconds] of CASES) {
  app.state.tasks = [{
    name: 'demo-task',
    alias: 'aa',
    status,
    engine: 'claude',
    sleep_seconds: sleepSeconds,
    created_at: '2026-01-01T00:00:00+00:00',
    status_changed_at: '2026-01-01T00:00:00+00:00',
  }];
  // A closed task is filtered out of the default listing, and searching is
  // what reaches one — the same route the phone takes.
  app.state.query = status === 'DONE' || status === 'DISCARDED' ? 'demo-task' : '';
  app.state.draft = app.state.query;
  app.renderList();
  const match = /<span class="sleep">([^<]*)<\/span>/.exec(app.html());
  out[label] = match ? match[1] : null;
}
console.log(JSON.stringify(out));
