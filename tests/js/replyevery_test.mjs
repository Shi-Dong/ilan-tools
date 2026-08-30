/* Assertions for the reply-every confirmation challenge.

 * Replying to a task and putting one to sleep both end an active `reply -t`
 * cycle, so the server refuses each with 409 and a `confirm_reply_every` flag
 * rather than acting. The app has to notice the flag, ask, and retry with an
 * override — and a 409 *without* the flag is an ordinary refusal that must not
 * be turned into a question.
 *
 * The two callers used to carry a copy of this each. Nothing covered either
 * copy: the CLI's version of the dance is tested, and the server's, but not
 * the web app's. These assertions exist so the shared helper cannot regress
 * silently.
 */

import { bootApp, checker, settle } from './harness.mjs';

const { check, clickModal, report } = checker();

/** An app whose server answers `first`, then 200 for every retry. */
function serverThatAnswers(first) {
  const app = bootApp();
  app.setFetch(async (path, opts) => {
    const body = JSON.parse((opts || {}).body || '{}');
    if (body.override_reply_every) {
      return { ok: true, status: 200, json: async () => ({ message: 'ok' }) };
    }
    return first;
  });
  return app;
}

const CHALLENGE = {
  ok: false,
  status: 409,
  json: async () => ({ confirm_reply_every: true, reply_every_seconds: 2160 }),
};
const PLAIN_REFUSAL = {
  ok: false,
  status: 409,
  json: async () => ({ error: 'Task is DONE' }),
};

const posts = (app, suffix) => app.fetches.filter((f) => f.path.endsWith(suffix));
const overrides = (app) => app.fetches.filter(
  (f) => JSON.parse((f.opts || {}).body || '{}').override_reply_every === true,
);

// ── replying: the challenge is a question ───────────────────────────────
const declined = serverThatAnswers(CHALLENGE);
const declinedResult = declined.sendReply('busy-task', 'hello');
await settle();
check('a challenged reply asks before overriding', declined.modalOpen());
check('the question names the interval in the same units as the CLI',
  declined.modalTitle().includes('0.6h'),
  `title=${JSON.stringify(declined.modalTitle())}`);
check('the interval is not shown in raw seconds',
  !declined.modalTitle().includes('2160'));

clickModal(declined, '#mc', 'the reply-every challenge must be declinable');
await settle();
check('declining sends no override', overrides(declined).length === 0);
check('declining reports the reply as not sent', await declinedResult === false);

const accepted = serverThatAnswers(CHALLENGE);
const acceptedResult = accepted.sendReply('busy-task', 'hello');
await settle();
clickModal(accepted, '#mo', 'the reply-every challenge must be acceptable');
await settle();
check('accepting retries with the override', overrides(accepted).length === 1,
  `overrides=${overrides(accepted).length}`);
check('the retry carries the original message',
  JSON.parse(overrides(accepted)[0].opts.body).message === 'hello');
check('accepting reports the reply as sent', await acceptedResult === true);

// ── a 409 that is not the challenge ─────────────────────────────────────
const refused = serverThatAnswers(PLAIN_REFUSAL);
const refusedResult = refused.sendReply('closed-task', 'hello');
await settle();
check('a plain 409 is not turned into a question', !refused.modalOpen());
check('a plain 409 is reported as a failure', await refusedResult === false);
// Scoped to the reply path: fetches also holds the /canned-messages request
// the app makes while booting, which never settles.
check('a plain 409 is not retried', posts(refused, '/reply').length === 1,
  `reply requests=${posts(refused, '/reply').length}`);
check("the server's reason is what the user is shown",
  refused.el('toast').textContent === 'Task is DONE',
  `toast=${refused.el('toast').textContent}`);

// ── sleeping runs the same dance ────────────────────────────────────────
const sleeping = serverThatAnswers(CHALLENGE);
sleeping.runAction('sleep', { name: 'busy-task', status: 'WORKING' });
await settle();
// The duration prompt comes first; answer it, then the challenge appears.
sleeping.modal('#mv').value = '30m';
clickModal(sleeping, '#mo', 'sleep must ask for a duration');
await settle();
check('a challenged sleep asks too', sleeping.modalOpen());
clickModal(sleeping, '#mo', 'the sleep challenge must be acceptable');
await settle();
check('the sleep is retried with the override', overrides(sleeping).length === 1,
  `overrides=${overrides(sleeping).length}`);
check('the retry keeps the requested duration',
  JSON.parse(overrides(sleeping)[0].opts.body).seconds === 1800,
  JSON.stringify(overrides(sleeping)[0]?.opts?.body));
check('it posts to the sleep endpoint', posts(sleeping, '/sleep').length === 2,
  `sleep posts=${posts(sleeping, '/sleep').length}`);

report('reply-every');
