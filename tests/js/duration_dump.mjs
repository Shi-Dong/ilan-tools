/* Print the web app's duration formatting as JSON, for cross-checking against
 * the CLI's _format_compact_duration.
 *
 * app.js is a browser script, so it is evaluated here against the smallest
 * stubs that let it load. fetch never settles on purpose: the start() IIFE at
 * the bottom awaits it, which parks the rest of the boot sequence instead of
 * letting it run in a fake DOM.
 */

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(
  join(here, '..', '..', 'src', 'ilan', 'web', 'static', 'app.js'), 'utf8',
);

const STUBS = `
  const MD = { escapeHtml: (v) => String(v ?? '') };
  const document = {
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener: () => {},
    body: { appendChild: () => {} },
    createElement: () => ({ addEventListener: () => {} }),
  };
  const window = { addEventListener: () => {} };
  const location = { hash: '#/' };
  const fetch = () => new Promise(() => {});
  const setInterval = () => 0;
  const clearInterval = () => {};
  const setTimeout = () => 0;
  const clearTimeout = () => {};
`;

const exported = new Function(
  `${STUBS}\n${source}\n;return { formatCompactDuration, replyEverySuffix, sleepSuffix, isLooping };`,
)();

const SECONDS = JSON.parse(process.argv[2]);
const out = {};
for (const s of SECONDS) {
  out[s] = {
    compact: exported.formatCompactDuration(s),
    replyEvery: exported.replyEverySuffix(s),
    sleep: exported.sleepSuffix(s),
    looping: exported.isLooping({ reply_every_seconds: s }),
  };
}
console.log(JSON.stringify(out));
