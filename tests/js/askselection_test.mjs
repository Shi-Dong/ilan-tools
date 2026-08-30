/* Assertions for asking about a selected passage.
 *
 * Selecting text in a message offers to quote it into the composer, so the
 * user can ask about that passage rather than describing where it was.
 *
 * The parts worth pinning are the ones a browser will not tell you about
 * until it is too late: that a selection outside a message is ignored, that
 * the text is captured when the bar appears rather than read back on click
 * (tapping can clear a selection first), and that a long selection cannot
 * grow the message without bound.
 */

import { bootApp, checker, settle } from './harness.mjs';

const { check, report } = checker();

const CONVERSATION = [
  { role: 'user', content: 'Split the store into a reader and a writer.',
    timestamp: '2026-01-01T00:00:00+00:00' },
  { role: 'assistant', content: 'Done. The reader no longer takes the lock.',
    timestamp: '2026-01-01T00:01:00+00:00' },
];

function openConversation(status = 'AGENT_FINISHED') {
  const app = bootApp();
  app.setFetch(async (path) => {
    const json = (d) => ({ ok: true, status: 200, json: async () => d });
    if (path.includes('/tail')) return json({ entries: CONVERSATION });
    return json({ task: { name: 'demo-task', alias: 'aa', status, engine: 'claude' } });
  });
  return app;
}

// ── the quote itself ────────────────────────────────────────────────────
const plain = bootApp();

check('a selection becomes a Markdown blockquote',
  plain.quoteForReply('the reader no longer takes the lock')
    === '> the reader no longer takes the lock\n\n',
  JSON.stringify(plain.quoteForReply('the reader no longer takes the lock')));

check('the quote ends with a blank line to type the question under',
  plain.quoteForReply('anything').endsWith('\n\n'));

check('a selection spanning lines is collapsed to one',
  plain.quoteForReply('first line\n  second line') === '> first line second line\n\n',
  JSON.stringify(plain.quoteForReply('first line\n  second line')));

check('surrounding whitespace is dropped',
  plain.quoteForReply('   padded   ') === '> padded\n\n');

// A quote is a citation, not a reproduction: the agent still holds the message
// it came from, so a very long selection is elided rather than pasted whole.
const long = 'x'.repeat(400) + 'MIDDLE' + 'y'.repeat(400);
const quoted = plain.quoteForReply(long);
check('a very long selection is shortened', quoted.length < long.length,
  `quote=${quoted.length} selection=${long.length}`);
check('shortening keeps the beginning', quoted.includes('x'.repeat(50)));
check('shortening keeps the end', quoted.includes('y'.repeat(50)));
check('shortening drops the middle', !quoted.includes('MIDDLE'));
check('the elision is marked', quoted.includes('…'));
check('even a shortened quote is a single blockquote line',
  quoted.split('\n').filter((l) => l.startsWith('>')).length === 1);

// ── which selections count ──────────────────────────────────────────────
const app = openConversation();
await app.renderDetail('demo-task');
await settle();

check('the ask bar is rendered with the conversation', app.html().includes('id="ask-bar"'));
check('it starts hidden', app.el('ask-bar').hidden === true);

app.selectText('the reader no longer takes the lock');
app.syncAskBar();
check('selecting inside a message offers the action',
  app.el('ask-bar').hidden === false);
check('the bar previews what will be quoted',
  app.el('ask-preview').textContent.includes('no longer takes the lock'),
  `preview=${app.el('ask-preview').textContent}`);

app.selectText('WORKING · claude', false, false);
app.syncAskBar();
check('selecting the page chrome offers nothing', app.el('ask-bar').hidden === true);

// A drag that begins in a reply and ends outside it would otherwise quote
// whatever the browser decided to sweep up on the way.
app.selectText('half in half out', true, false);
app.syncAskBar();
check('a selection that runs out of the message is ignored',
  app.el('ask-bar').hidden === true);

app.selectText('   ');
app.syncAskBar();
check('a whitespace-only selection offers nothing', app.el('ask-bar').hidden === true);

app.selectText('');
app.syncAskBar();
check('clearing the selection hides the bar again', app.el('ask-bar').hidden === true);

// ── pressing it ─────────────────────────────────────────────────────────
app.selectText('the reader no longer takes the lock');
app.syncAskBar();
// The real button clears the selection on press, so the app must already hold
// the text by now rather than reading it back here.
app.selectText('');
app.el('ask-btn').onclick();

check('the quote lands in the composer',
  app.el('reply').value === '> the reader no longer takes the lock\n\n',
  JSON.stringify(app.el('reply').value));
check('the bar hides once it has been used', app.el('ask-bar').hidden === true);

// ── a half-typed question is not thrown away ────────────────────────────
app.el('reply').value = 'why did you';
app.selectText('the reader no longer takes the lock');
app.syncAskBar();
app.el('ask-btn').onclick();
check('an existing draft is kept above the quote',
  app.el('reply').value === 'why did you\n\n> the reader no longer takes the lock\n\n',
  JSON.stringify(app.el('reply').value));

// ── nothing to reply to, nothing to ask with ────────────────────────────
const closed = openConversation('DONE');
await closed.renderDetail('demo-task');
await settle();
check('a closed task has no ask bar', !closed.html().includes('id="ask-bar"'));
closed.selectText('the reader no longer takes the lock');
closed.syncAskBar();
check('syncing a view with no ask bar is harmless',
  !closed.html().includes('id="ask-bar"'));

report('ask-selection');
