/* Assertions for the composer's clear control.
 *
 * A phone keyboard has no comfortable way to select a whole draft and delete
 * it, so the composer carries a button that empties it. What matters is that
 * it cannot fire on an empty box, that it puts the composer back exactly as it
 * was before anything was typed — height included — and that it never sends
 * anything.
 */

import { bootApp, checker, settle } from './harness.mjs';

const { check, report } = checker();

const CONVERSATION = [
  { role: 'user', content: 'Split the store.', timestamp: '2026-01-01T00:00:00+00:00' },
  { role: 'assistant', content: 'Done.', timestamp: '2026-01-01T00:01:00+00:00' },
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

const app = openConversation();
await app.renderDetail('demo-task');
await settle();

const box = () => app.el('reply');
const clear = () => app.el('clear-reply');

// ── it is there, and inert until there is something to clear ────────────
check('the composer offers a clear control', app.html().includes('id="clear-reply"'));
check('it is disabled on an empty box', clear().disabled === true);
check('it has an accessible name, having no text label',
  app.html().includes('aria-label="Clear the message"'));
check('it lives inside the field, not beside it',
  /<div class="composer-field">[\s\S]*?id="reply"[\s\S]*?id="clear-reply"[\s\S]*?<\/div>/
    .test(app.html()),
  'the button is no longer wrapped with the box it clears');
check('Send stays outside the field',
  app.html().indexOf('id="send"') > app.html().indexOf('id="clear-reply"'));

box().value = 'why did you take the lock out?';
box().oninput();
check('typing enables it', clear().disabled === false);

box().value = '    ';
box().oninput();
check('whitespace alone does not enable it', clear().disabled === true,
  'there is nothing worth clearing');

// ── clearing ────────────────────────────────────────────────────────────
box().value = 'a long draft that would be tedious to select and delete by hand';
box().oninput();
clear().onclick();

check('the draft is gone', box().value === '', JSON.stringify(box().value));
check('it disables itself again', clear().disabled === true);
// The box also has to shrink back to one row. There is no layout engine here
// to measure that, so it is checked in a browser instead.
check('focus returns to the box to type the next thing',
  app.focused() === 'reply', `focused=${app.focused()}`);

// ── it must never send ──────────────────────────────────────────────────
const posted = app.fetches.filter((f) => (f.opts || {}).method === 'POST');
check('clearing posts nothing', posted.length === 0,
  `posts=${JSON.stringify(posted.map((f) => f.path))}`);

// ── quoting a passage arms it too ───────────────────────────────────────
// The ask bar puts text in the box without anyone typing, so the button has
// to notice that as well.
app.selectText('Done.');
app.syncAskBar();
app.el('ask-btn').onclick();
check('a quoted passage enables the clear control', clear().disabled === false,
  'text arrived without a keystroke and the button stayed dim');
clear().onclick();
check('and clearing removes the quote', box().value === '');

// ── a closed task has neither ───────────────────────────────────────────
const closed = openConversation('DONE');
await closed.renderDetail('demo-task');
await settle();
check('a closed task has no composer to clear', !closed.html().includes('id="clear-reply"'));

report('clear-draft');
