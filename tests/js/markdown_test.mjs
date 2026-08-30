/* Assertions for the web app's Markdown renderer.
 *
 * Run directly (`node tests/js/markdown_test.mjs`) or through
 * tests/test_web_markdown.py, which skips when node is unavailable.
 *
 * The renderer turns untrusted agent output into HTML, so roughly half of
 * these cases are escaping and link-scheme checks rather than formatting.
 */

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(
  join(here, '..', '..', 'src', 'ilan', 'web', 'static', 'markdown.js'), 'utf8',
);
// markdown.js is a plain browser script, so evaluate it and lift MD out.
const MD = new Function(`${source}; return MD;`)();

// [name, input, mustContain[], mustNotContain[]]
const CASES = [
  // ── escaping and link safety ───────────────────────────────────────────
  ['raw script tag', '<script>alert(1)</script>', ['&lt;script&gt;'], ['<script']],
  ['img onerror', '<img src=x onerror="alert(1)">', ['&lt;img'], ['<img']],
  ['javascript: link', '[click](javascript:alert(1))', [], ['<a href="javascript:']],
  ['data: link', '[c](data:text/html,<script>alert(1)</script>)', [], ['<a href="data:']],
  ['vbscript: link', '[x](vbscript:msgbox(1))', [], ['<a href="vbscript:']],
  ['mixed-case javascript:', '[x](JaVaScRiPt:alert(1))', [], ['href="JaVaScRiPt:']],
  ['attribute breakout via title', '[x](http://a.com "\\" onmouseover=alert(1))',
    ['<a href="http://a.com"'], []],
  ['script in fenced code', '```\n<script>alert(1)</script>\n```',
    ['&lt;script&gt;'], ['<script']],
  ['script in inline code', '`<script>alert(1)</script>`', ['&lt;script&gt;'], ['<script']],
  ['script in table cell', '| a |\n|---|\n| <script>alert(1)</script> |',
    ['&lt;script&gt;'], ['<script']],
  ['script in heading', '# <script>alert(1)</script>', ['&lt;script&gt;'], ['<script']],
  ['script in list item', '- <script>alert(1)</script>', ['&lt;script&gt;'], ['<script']],
  ['script in blockquote', '> <script>alert(1)</script>', ['&lt;script&gt;'], ['<script']],
  ['quote breakout inside fence', '```\n" onmouseover="alert(1)\n```',
    ['&quot;'], ['onmouseover="alert']],
  ['html inside bold', '**<b>x</b>**', ['&lt;b&gt;'], ['<b>x']],
  ['relative link still allowed', '[x](/app/)', ['<a href="/app/"'], []],

  // ── formatting ────────────────────────────────────────────────────────
  ['bold', '**bold**', ['<strong>bold</strong>'], []],
  ['italic', 'an *ital* word', ['<em>ital</em>'], []],
  ['strikethrough', '~~gone~~', ['<del>gone</del>'], []],
  ['inline code', 'use `foo()` here', ['<code>foo()</code>'], []],
  ['code span keeps asterisks', '`a * b * c`', ['<code>a * b * c</code>'], ['<em>']],
  ['snake_case not italicised', 'call some_var_name now', ['some_var_name'], ['<em>']],
  ['dunder survives intact', 'the __init__ method',
    ['__init__'], ['<em>', '<strong>']],
  ['dunder name in a sentence', 'override __main__ and __name__',
    ['__main__', '__name__'], ['<strong>']],
  ['h1 renders as h3', '# Title', ['<h3>Title</h3>'], ['<h1>']],
  ['h2 renders as h4', '## Sub', ['<h4>Sub</h4>'], []],
  ['link', '[docs](https://example.com)',
    ['<a href="https://example.com"', 'rel="noopener noreferrer"', '>docs</a>'], []],
  ['bare url autolinks', 'see https://example.com/x now',
    ['<a href="https://example.com/x"'], []],
  ['unordered list', '- one\n- two', ['<ul>', '<li>one</li>', '<li>two</li>'], []],
  ['ordered list', '1. one\n2. two', ['<ol>', '<li>one</li>'], []],
  ['nested list', '- top\n  - inner', ['<ul>', 'inner'], []],
  ['task list', '- [ ] todo\n- [x] done', ['type="checkbox"', 'checked', 'todo'], []],
  ['blockquote', '> quoted', ['<blockquote>', 'quoted'], []],
  ['table', '| a | b |\n|---|---|\n| 1 | 2 |',
    ['<table>', '<th>a</th>', '<td>1</td>', 'md-table-wrap'], []],
  ['horizontal rule', 'a\n\n---\n\nb', ['<hr>'], []],
  ['fenced code keeps indentation', '```py\ndef f():\n    return 1\n```',
    ['def f():\n    return 1', 'data-lang="py"'], []],
  ['fence contents are not a list', '```\n- not a list\n```', ['- not a list'], ['<li>']],

  // ── degradation: nothing is ever dropped ──────────────────────────────
  ['plain text survives', 'just a sentence', ['just a sentence'], ['<code>', '<em>']],
  // Agent prose arrives hard-wrapped for a terminal; on a phone those newlines
  // must reflow, or sentences break mid-phrase at a second, wrong place.
  ['soft-wrapped lines reflow', 'line one\nline two', ['line one line two'], ['<br>']],
  ['two-space hard break honoured', 'line one  \nline two', ['<br>'], []],
  ['backslash hard break honoured', 'line one\\\nline two', ['<br>'], []],
  ['reflow does not eat a sentence', 'the first\none actually causes it.',
    ['the first one actually causes it.'], ['<br>']],
  ['unterminated fence keeps content', '```\nstranded text', ['stranded text'], []],
  ['ampersand escaped once', 'a & b', ['a &amp; b'], ['&amp;amp;']],
  ['empty input', '', [], ['undefined', 'null']],
  ['null input', null, [], ['undefined', 'null']],
];

/* Checks applied to EVERY case's output, not just the escaping ones.
 *
 * Per-case substring assertions cannot tell "onerror=" sitting harmlessly in
 * escaped body text from "onerror=" living inside a real tag, so the dangerous
 * shapes are matched structurally here instead: any event-handler attribute
 * inside a tag, any <script>, and any href/src whose scheme is not one the
 * renderer is supposed to allow.
 */
function unsafe(html) {
  const problems = [];
  if (/<script/i.test(html)) problems.push('contains a <script tag');
  if (/<[a-zA-Z][^>]*\son[a-z]+\s*=/i.test(html)) {
    problems.push('has an event-handler attribute inside a tag');
  }
  for (const m of html.matchAll(/(?:href|src)="([^"]*)"/gi)) {
    const scheme = /^([a-zA-Z][a-zA-Z0-9+.-]*):/.exec(m[1]);
    if (scheme && !/^(https?|mailto)$/i.test(scheme[1])) {
      problems.push(`disallowed URL scheme: ${scheme[1]}`);
    }
  }
  return problems;
}

let failed = 0;
for (const [name, input, must, mustNot] of CASES) {
  let html;
  try {
    html = MD.render(input);
  } catch (err) {
    console.log(`THREW  ${name} :: ${err.message}`);
    failed += 1;
    continue;
  }
  const danger = unsafe(html);
  if (danger.length) {
    failed += 1;
    console.log(`UNSAFE ${name}`);
    console.log(`         ${danger.join('; ')}`);
    console.log(`         got: ${JSON.stringify(html)}`);
    continue;
  }
  const missing = must.filter((s) => !html.includes(s));
  const present = mustNot.filter((s) => html.includes(s));
  if (missing.length || present.length) {
    failed += 1;
    console.log(`FAIL   ${name}`);
    if (missing.length) console.log(`         missing: ${JSON.stringify(missing)}`);
    if (present.length) console.log(`         present: ${JSON.stringify(present)}`);
    console.log(`         got: ${JSON.stringify(html)}`);
  }
}

if (failed) {
  console.log(`\n${failed} of ${CASES.length} markdown cases FAILED`);
  process.exit(1);
}
console.log(`all ${CASES.length} markdown cases passed`);
