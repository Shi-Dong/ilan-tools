/* Assertions for the toast, which now renders inline code.
 *
 * The point of the change is small — a task's name set apart from the sentence
 * around it, because that is what a reader is looking for in a message that
 * flashes past. The risk is not: the toast switched from textContent to
 * innerHTML, and its text is never ours. It carries task names, which are
 * arbitrary, and server messages, which are arbitrary too.
 *
 * So most of what is checked here is that nothing in either can become markup.
 */

import { bootApp, checker, settle } from './harness.mjs';

const { check, report } = checker();
const app = bootApp();

const toastHtml = () => app.el('toast').innerHTML;
const toastText = () => app.el('toast').textContent;

// ── the marker becomes inline code ──────────────────────────────────────
app.toast('Reply sent to `my-task`. Agent resumed.');
check('a marked span becomes inline code',
  toastHtml() === 'Reply sent to <code>my-task</code>. Agent resumed.',
  toastHtml());
check('the reader still sees the plain sentence',
  toastText() === 'Reply sent to my-task. Agent resumed.', toastText());

app.toast('Refreshed');
check('a message with no marker is untouched', toastHtml() === 'Refreshed');

app.toast('one `a` two `b`');
check('several markers each become code',
  toastHtml() === 'one <code>a</code> two <code>b</code>', toastHtml());

app.toast('an unpaired ` backtick');
check('an unpaired marker is left alone', toastHtml() === 'an unpaired ` backtick',
  toastHtml());

// ── nothing can become markup ───────────────────────────────────────────
// Both halves matter: escaping has to happen, and it has to happen *before*
// the markers are matched, or a name could close the code element and open
// something else.
app.toast('Deleted `<img src=x onerror=alert(1)>`');
check('a name that looks like markup is escaped',
  !toastHtml().includes('<img'), toastHtml());
check('and is still shown as code',
  toastHtml() === 'Deleted <code>&lt;img src=x onerror=alert(1)&gt;</code>',
  toastHtml());
check('the reader sees the name verbatim',
  toastText() === 'Deleted <img src=x onerror=alert(1)>', toastText());

app.toast('<b>not bold</b>');
check('an unmarked message cannot inject either',
  toastHtml() === '&lt;b&gt;not bold&lt;/b&gt;', toastHtml());

app.toast('an ampersand & a quote " in `a-name`');
check('the other escapes survive too',
  toastHtml() === 'an ampersand &amp; a quote &quot; in <code>a-name</code>',
  toastHtml());

// ── marking a name inside a message the server composed ─────────────────
const mark = app.withCodeName;

check('the name is marked wherever the server put it',
  mark('Reply sent to my-task. Agent resumed.', 'my-task')
    === 'Reply sent to `my-task`. Agent resumed.',
  mark('Reply sent to my-task. Agent resumed.', 'my-task'));

check('every occurrence is marked',
  mark('my-task replied; my-task is busy', 'my-task')
    === '`my-task` replied; `my-task` is busy');

check('a message that never names the task is left alone',
  mark('Interrupted agent and resumed with reply.', 'my-task')
    === 'Interrupted agent and resumed with reply.');

check('no name means no marking', mark('Reply sent', '') === 'Reply sent');
check('a null message does not throw', mark(null, 'my-task') === '');

// A name holding a backtick would produce a broken marker, so it is left
// alone rather than mangling the sentence around it.
check('a name containing a backtick is not marked',
  mark('Deleted odd`name here', 'odd`name') === 'Deleted odd`name here');

// ── end to end: a reply names its task in code ──────────────────────────
const live = bootApp();
live.setFetch(async () => ({
  ok: true, status: 200,
  json: async () => ({ message: 'Reply sent to demo-task. Agent resumed.' }),
}));
await live.sendReply('demo-task', 'hello');
await settle();
check('a sent reply names its task as code',
  live.el('toast').innerHTML === 'Reply sent to <code>demo-task</code>. Agent resumed.',
  live.el('toast').innerHTML);

// The server decides the wording; when it does not name the task, nothing is
// invented.
const quiet = bootApp();
quiet.setFetch(async () => ({
  ok: true, status: 200,
  json: async () => ({ message: 'Interrupted agent and resumed with reply.' }),
}));
await quiet.sendReply('demo-task', 'hello');
await settle();
check('a reply the server words differently is still shown',
  quiet.el('toast').textContent === 'Interrupted agent and resumed with reply.',
  quiet.el('toast').textContent);

// ── an error still reads as an error ────────────────────────────────────
const failed = bootApp();
failed.setFetch(async () => ({
  ok: false, status: 409,
  json: async () => ({ error: 'Task is DONE' }),
}));
const sent = await failed.sendReply('demo-task', 'hello');
await settle();
check('a refused reply reports false', sent === false);
check('the error text is shown', failed.el('toast').textContent === 'Task is DONE',
  failed.el('toast').textContent);
check('and it is styled as an error', failed.el('toast').className.includes('toast-err'),
  failed.el('toast').className);

report('toast');
