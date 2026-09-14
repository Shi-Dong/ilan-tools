/* Render task rows through the real list and report each SLEEPING progress bar.
 * Cases come from Python so its duration formatter can verify the visible text.
 */

import { bootApp } from './harness.mjs';

const app = bootApp();

// [label, status, sleep_seconds, elapsed_seconds]
const CASES = JSON.parse(process.argv[2]);

const out = {};
let oldSuffixPresent = false;
for (const [label, status, sleepSeconds, elapsedSeconds] of CASES) {
  const started = new Date(Date.now() - elapsedSeconds * 1000).toISOString();
  app.state.tasks = [{
    name: 'demo-task',
    alias: 'aa',
    status,
    engine: 'claude',
    sleep_seconds: sleepSeconds,
    created_at: '2026-01-01T00:00:00+00:00',
    status_changed_at: started,
  }];
  app.state.query = '';
  app.state.draft = '';
  app.renderList();

  const html = app.html();
  oldSuffixPresent ||= html.includes('sleeping for');
  if (!html.includes('class="sleep-progress"')) {
    out[label] = null;
    continue;
  }
  out[label] = {
    label: html.match(/class="sleep-progress-track"[^>]*aria-label="([^"]*)"/)?.[1] ?? null,
    max: Number(html.match(/class="sleep-progress-track" max="(\d+)"/)?.[1]),
    now: Number(html.match(/class="sleep-progress-track"[^>]*value="(\d+)"/)?.[1]),
    time: html.match(/class="sleep-progress-time">([^<]*)</)?.[1] ?? null,
  };
}
out._old_suffix_present = oldSuffixPresent;
console.log(JSON.stringify(out));
