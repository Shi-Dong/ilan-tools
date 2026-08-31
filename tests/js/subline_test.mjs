/* Assertions for the line under a conversation's title.
 *
 * It answers "what is this task doing now" — the status, plus the model and
 * any sleep or reply-every cycle. Two things are deliberately not on it, and
 * both are asserted as absences with their neighbours checked alongside, since
 * "X is gone" would also be satisfied by the whole line disappearing:
 *
 *   - Where the task was branched from, which is a fact about how it started.
 *   - The backend, which the coloured task name in the title already says.
 *
 * The status is a pill here, the same element the list renders, so what is
 * checked is the markup rather than a string: a pill that lost its classes
 * would still read correctly as text while rendering as plain grey.
 */

import { bootApp, checker, settle } from './harness.mjs';

const { check, report } = checker();

const BRANCHED = {
  name: 'follow-up-task',
  alias: 'ab',
  status: 'WORKING',
  engine: 'codex',
  model: 'gpt-5.6-sol',
  parent_name: 'the-original-investigation',
  sleep_seconds: 300,
  reply_every_seconds: 3600,
  status_changed_at: new Date(Date.now() - 12 * 60 * 1000).toISOString(),
};

function openTask(task) {
  const app = bootApp();
  app.setFetch(async (path) => {
    const json = (d) => ({ ok: true, status: 200, json: async () => d });
    if (path.includes('/tail')) {
      return json({ entries: [{ role: 'assistant', content: 'ok',
                                timestamp: '2026-01-01T00:00:00+00:00' }] });
    }
    return json({ task });
  });
  return app;
}

/** The sub-line's markup, and the text of each of its two parts. */
function subOf(app) {
  const line = app.html().match(/<p class="hdr-sub[^"]*"[^>]*>([\s\S]*?)<\/p>/);
  const html = line ? line[1] : '';
  const open = app.html().match(/<p class="(hdr-sub[^"]*)"/);
  const pill = html.match(/<span class="status[^"]*">([^<]*)<\/span>/);
  const meta = html.match(/<span class="meta-detail">([^<]*)<\/span>/);
  return {
    html,
    classes: open ? open[1] : '',
    pill: pill ? pill[1] : null,
    pillClasses: html.match(/<span class="(status[^"]*)"/)?.[1] ?? '',
    meta: meta ? meta[1] : null,
    text: html.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim(),
  };
}

const app = openTask(BRANCHED);
await app.renderDetail('follow-up-task');
await settle();
const sub = () => subOf(app);

// ── the status is the list's pill, not a run of text ────────────────────
check('the status is rendered as a pill', sub().pill !== null,
  `line=${sub().html}`);
check('the pill carries the status class the list uses',
  sub().pillClasses === 'status st-WORKING', sub().pillClasses);
// The pill takes its fill from --row-status, which only an rs-* class sets.
// Without one it still renders — in the plain border grey — so this is the
// difference between a coloured pill and a grey one, and nothing else here
// would notice.
check('the line carries the rs- class that colours the pill',
  sub().classes.includes('rs-WORKING'), sub().classes);
check('and it reuses the list container so one rule styles both',
  sub().classes.includes('row-meta'), sub().classes);
check('the status reads as words, not as an enum',
  sub().pill === 'WORKING (for 12m)', JSON.stringify(sub().pill));

// ── the lineage is gone ─────────────────────────────────────────────────
check('the parent is not named on the line',
  !sub().text.includes('the-original-investigation'), sub().text);
check('and neither is the word introducing it', !sub().text.includes('from '),
  sub().text);
check('the parent is nowhere in the conversation view',
  !app.html().includes('the-original-investigation'),
  'the lineage moved somewhere else on the page rather than going away');

// ── the backend is gone from the line ───────────────────────────────────
check('the backend is not named on the line', !sub().text.includes('codex'),
  sub().text);
// Checked on the line rather than on the page: the title still carries
// engine-codex as a class, and that is the point — the name is coloured by
// backend, so the word was repeating what the colour already said. Asserting
// its absence page-wide would fail on the very thing that justifies removing
// it.
check('the task name is still coloured by backend',
  app.html().includes('engine-codex'),
  'the backend is not shown as a word and not shown as a colour either');

// ── everything else is still there ──────────────────────────────────────
check('the model is still shown', sub().meta.includes('gpt-5.6-sol'), sub().meta);
check('an active sleep is still shown', sub().meta.includes('sleeping for 5m'),
  sub().meta);
check('a reply-every cycle is still shown', sub().meta.includes('responding every 1h'),
  sub().meta);

// Removing an entry from a joined list is the easy way to leave a stray
// separator behind, which reads as a missing value rather than as none.
// Checked by splitting rather than by matching spaces around the dot: the
// number of spaces is not the point, an empty segment is.
const segments = () => sub().meta.split('·').map((part) => part.trim());
check('no empty segment is left behind', segments().every(Boolean),
  JSON.stringify(segments()));
check('the task itself is still named in the header',
  app.html().includes('follow-up-task'));

// ── the ordinary case: most tasks have no model, sleep or cycle ─────────
// Whatever is absent has to leave no trace. The fixture above fills every
// field, so it cannot tell whether empty entries are being dropped. With the
// backend gone this task has nothing left to say beyond its status, so the
// second half of the line should not be rendered at all rather than rendered
// empty — an empty span still takes the flex gap beside the pill.
const sparse = openTask({
  name: 'plain-task', alias: 'ac', status: 'AGENT_FINISHED', engine: 'claude',
  model: null, parent_name: 'some-parent', sleep_seconds: 0,
  reply_every_seconds: 0,
  status_changed_at: '2026-01-01T00:00:00+00:00',
});
await sparse.renderDetail('plain-task');
await settle();

check('a task with nothing else to say still shows its status',
  subOf(sparse).pill === 'AGENT FINISHED', JSON.stringify(subOf(sparse).pill));
check('and shows nothing beside it rather than an empty run',
  subOf(sparse).meta === null, JSON.stringify(subOf(sparse).meta));
check('the header carries no underscored status either',
  !subOf(sparse).text.includes('_'), subOf(sparse).text);
check('its pill is coloured by its own status',
  subOf(sparse).classes.includes('rs-AGENT_FINISHED'), subOf(sparse).classes);

// ── a task with no parent renders the same way ──────────────────────────
const plain = openTask({ ...BRANCHED, parent_name: null, name: 'standalone-task' });
await plain.renderDetail('standalone-task');
await settle();
check('a branched task and an unbranched one now read identically',
  subOf(plain).html === sub().html,
  `branched=${JSON.stringify(sub().html)} plain=${JSON.stringify(subOf(plain).html)}`);

report('sub-line');
