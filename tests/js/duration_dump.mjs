/* Print the web app's duration formatting as JSON, for cross-checking against
 * the CLI's _format_compact_duration.
 *
 * Reads its inputs from argv so the Python side owns the list, and can build
 * the expectations from the CLI's own helpers rather than from literals.
 */

import { bootApp } from './harness.mjs';

const app = bootApp();

const SECONDS = JSON.parse(process.argv[2]);
const out = {};
for (const s of SECONDS) {
  out[s] = {
    compact: app.formatCompactDuration(s),
    replyEvery: app.replyEverySuffix(s),
    sleep: app.sleepSuffix(s),
    looping: app.isLooping({ reply_every_seconds: s }),
  };
}
console.log(JSON.stringify(out));
