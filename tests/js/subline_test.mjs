/* Assertions for the line under a conversation's title.
 *
 * It answers "what is this task doing now" — status, backend, model, and any
 * sleep or reply-every cycle. Where the task was branched from is a fact about
 * how it started, so it is deliberately not here; on a phone this is one line
 * competing for the width, and a parent's name is often longer than everything
 * else on it.
 *
 * The neighbours are asserted alongside the absence, because "the parent is
 * gone" would also be satisfied by the whole line disappearing.
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

const app = openTask(BRANCHED);
await app.renderDetail('follow-up-task');
await settle();

const sub = () => {
  const m = app.html().match(/<p class="hdr-sub[^"]*">([^<]*)<\/p>/);
  return m ? m[1] : '';
};

// ── the lineage is gone ─────────────────────────────────────────────────
check('the parent is not named on the line', !sub().includes('the-original-investigation'),
  `sub=${sub()}`);
check('and neither is the word introducing it', !sub().includes('from '),
  `sub=${sub()}`);
check('the parent is nowhere in the conversation view',
  !app.html().includes('the-original-investigation'),
  'the lineage moved somewhere else on the page rather than going away');

// ── everything else is still there ──────────────────────────────────────
check('the status is still shown', sub().includes('WORKING'), `sub=${sub()}`);
check('how long it has been working survives', sub().includes('(for 12m)'), `sub=${sub()}`);
check('the backend is still shown', sub().includes('codex'), `sub=${sub()}`);
check('the model is still shown', sub().includes('gpt-5.6-sol'), `sub=${sub()}`);
check('an active sleep is still shown', sub().includes('sleeping for 5m'), `sub=${sub()}`);
check('a reply-every cycle is still shown', sub().includes('responding every 1h'),
  `sub=${sub()}`);

// Removing an entry from a joined list is the easy way to leave a stray
// separator behind, which reads as a missing value rather than as none.
// Checked by splitting rather than by matching spaces around the dot: the
// number of spaces is not the point, an empty segment is.
const segments = () => sub().split('·').map((part) => part.trim());
check('no empty segment is left behind', segments().every(Boolean),
  JSON.stringify(segments()));
check('the task itself is still named in the header',
  app.html().includes('follow-up-task'));

// ── the ordinary case: most tasks have no model, sleep or cycle ─────────
// Whatever is absent has to leave no trace. The fixture above fills every
// field, so it cannot tell whether empty entries are being dropped.
const sparse = openTask({
  name: 'plain-task', alias: 'ac', status: 'AGENT_FINISHED', engine: 'claude',
  model: null, parent_name: 'some-parent', sleep_seconds: 0,
  reply_every_seconds: 0,
  status_changed_at: '2026-01-01T00:00:00+00:00',
});
await sparse.renderDetail('plain-task');
await settle();
const sparseSub = sparse.html().match(/<p class="hdr-sub[^"]*">([^<]*)<\/p>/)[1];
check('a task with only a status and a backend reads cleanly',
  sparseSub.split('·').map((p) => p.trim()).every(Boolean),
  JSON.stringify(sparseSub));
check('it says just those two things',
  sparseSub.trim() === 'AGENT_FINISHED · claude', JSON.stringify(sparseSub));

// ── a task with no parent renders the same way ──────────────────────────
const plain = openTask({ ...BRANCHED, parent_name: null, name: 'standalone-task' });
await plain.renderDetail('standalone-task');
await settle();
const plainSub = plain.html().match(/<p class="hdr-sub[^"]*">([^<]*)<\/p>/);
check('a branched task and an unbranched one now read identically',
  plainSub && plainSub[1] === sub(),
  `branched=${JSON.stringify(sub())} plain=${JSON.stringify(plainSub && plainSub[1])}`);

report('sub-line');
