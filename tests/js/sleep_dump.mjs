/* Render task rows through the real renderList and report the sleep suffix
 * each one produced, as JSON, for cross-checking against the CLI's
 * _format_sleep_suffix and its WORKING-only rule.
 *
 * Same DOM stub approach as search_test.mjs: small enough to be obvious, big
 * enough to run renderList, and #app records the HTML it was handed.
 */

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(
  join(here, '..', '..', 'src', 'ilan', 'web', 'static', 'app.js'), 'utf8',
);

const HARNESS = `
  const MD = { escapeHtml: (v) => String(v ?? '')
    .replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;')
    .replaceAll('"','&quot;').replaceAll("'",'&#39;') };
  const __els = new Map();
  function __el(key) {
    if (!__els.has(key)) {
      __els.set(key, {
        id: key, value: '', innerHTML: '', hidden: true, className: '',
        onclick: null, oninput: null, onkeydown: null,
        focus() {}, classList: { add() {} }, style: {},
      });
    }
    return __els.get(key);
  }
  const document = {
    hidden: false,
    activeElement: null,
    querySelector: (sel) => __el(sel.replace('#', '')),
    querySelectorAll: () => [],
    addEventListener: () => {},
    body: { appendChild: () => {} },
    createElement: () => ({ addEventListener: () => {}, classList: { add() {} } }),
  };
  const window = { addEventListener: () => {} };
  const location = { hash: '#/' };
  const fetch = () => new Promise(() => {});
  const setInterval = () => 0;
  const clearInterval = () => {};
  const setTimeout = () => 0;
  const clearTimeout = () => {};
`;

const app = new Function(
  `${HARNESS}\n${source}\n;return { state, renderList, el: __el };`,
)();

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
  app.state.query = '';
  app.state.draft = '';
  app.state.showAll = true;
  app.renderList();
  const html = app.el('app').innerHTML;
  const match = /<span class="sleep">([^<]*)<\/span>/.exec(html);
  out[label] = match ? match[1] : null;
}
console.log(JSON.stringify(out));
