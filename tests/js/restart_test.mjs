/* Assertions for restarting the server from Settings.
 *
 * The interesting part is not the POST but what follows it: the old server
 * keeps answering for a moment after it has been told to stop, then nothing
 * answers, then a new process does. The app has to wait through all three
 * without mistaking the first for success or the second for failure, and it
 * has to recognise the third by its pid — version and commit are normally the
 * same on both sides of a restart.
 *
 * The wait between looks is injected so the loop runs without real time.
 */

import { bootApp, checker, settle } from './harness.mjs';

const { check, clickModal, report } = checker();
const noWait = async () => {};

/** A Settings page whose /version answers come from *pids*, in order, with a
 *  thrown fetch for each null; the last answer repeats. */
function settings(pids, restartResponse = { ok: true, status: 200, body: { ok: true, pid: 111 } }) {
  const app = bootApp();
  let versions = 0;
  app.setFetch(async (path, opts) => {
    const json = (d, ok = true, status = 200) => ({ ok, status, json: async () => d });
    if (path === '/config') return json({ config: { workdir: '/tmp' } });
    if (path === '/version') {
      const pid = pids[Math.min(versions, pids.length - 1)];
      versions += 1;
      if (pid === null) throw new TypeError('Failed to fetch');
      return json({ version: '1.0', commit: 'abc', pid });
    }
    if (path === '/restart' && (opts || {}).method === 'POST') {
      return json(restartResponse.body, restartResponse.ok, restartResponse.status);
    }
    return json({});
  });
  return app;
}
const versionLooks = (app) => app.fetches.filter((f) => f.path === '/version').length;
const restarts = (app) => app.fetches.filter((f) => f.path === '/restart').length;
const configLoads = (app) => app.fetches.filter((f) => f.path === '/config').length;

// ── the page ────────────────────────────────────────────────────────────
const page = settings([111]);
await page.renderConfig();
await settle();
check('Settings shows the server pid', page.html().includes('pid 111'), page.html());
check('and a Restart server button', page.html().includes('id="restart-server"')
  && page.html().includes('>Restart server</button>'));
check('the button is wired', typeof page.el('restart-server').onclick === 'function');
// It has to stand out from the row of quiet Edit buttons above it: filled, and
// at the full tap height rather than the small size those use.
check('it is the filled primary style', /id="restart-server"/.test(page.html())
  && /class="btn btn-primary" id="restart-server"/.test(page.html()),
  page.html().match(/<button[^>]*id="restart-server"/)?.[0]);
check('and not the small quiet size the Edit buttons use',
  !/class="[^"]*btn-sm[^"]*" id="restart-server"/.test(page.html()));
check('it says agents keep running', page.html().includes('Agents keep running'),
  'the one fact that makes the tap safe to take is not stated');

// ── declining ───────────────────────────────────────────────────────────
const declined = settings([111]);
await declined.renderConfig();
const declinedRun = declined.restartServer(111, noWait);
await settle();
check('restarting asks first', declined.modalOpen());
clickModal(declined, '#mc', 'the restart confirmation must be declinable');
await declinedRun;
check('declining posts nothing', restarts(declined) === 0);

// ── the full round trip ─────────────────────────────────────────────────
// The page render takes the first answer; then the old server answers once
// more, two fetches fail in the gap, and the new process answers.
const trip = settings([111, 111, null, null, 222]);
await trip.renderConfig();
const before = configLoads(trip);
const looksBefore = versionLooks(trip);
const tripRun = trip.restartServer(111, noWait);
await settle();
clickModal(trip, '#mo', 'the restart confirmation must be acceptable');
await tripRun;
check('accepting posts one restart', restarts(trip) === 1, `${restarts(trip)} posts`);
// Four looks — old pid, gap, gap, new pid — and then one more /version read
// when Settings reloads to show the new pid.
check('it keeps looking while the old pid still answers, and through the gap',
  versionLooks(trip) - looksBefore === 4 + 1, `${versionLooks(trip) - looksBefore} looks at /version`);
check('the new pid is announced', trip.el('toast').textContent === 'Server restarted (pid 222)',
  `toast=${trip.el('toast').textContent}`);
check('Settings is reloaded to show it', configLoads(trip) === before + 1,
  `config loads ${before} -> ${configLoads(trip)}`);

// ── the server never comes back ─────────────────────────────────────────
const stuck = settings([111]);
await stuck.renderConfig();
const stuckLooksBefore = versionLooks(stuck);
const stuckRun = stuck.restartServer(111, noWait);
await settle();
clickModal(stuck, '#mo', 'the restart confirmation must be acceptable');
await stuckRun;
check('it gives up after a bounded number of looks', versionLooks(stuck) - stuckLooksBefore === 40,
  `${versionLooks(stuck) - stuckLooksBefore} looks`);
check('and says so, pointing at the host',
  stuck.el('toast').textContent.includes('check it on the host'),
  `toast=${stuck.el('toast').textContent}`);
check('a live answer from the same pid is never taken as success',
  !stuck.el('toast').textContent.startsWith('Server restarted'));

// ── the server refuses ──────────────────────────────────────────────────
const refused = settings([111], { ok: false, status: 500, body: { error: 'Could not start the restart: boom' } });
await refused.renderConfig();
const refusedLooksBefore = versionLooks(refused);
const refusedRun = refused.restartServer(111, noWait);
await settle();
clickModal(refused, '#mo', 'the restart confirmation must be acceptable');
await refusedRun;
check("the server's reason is shown", refused.el('toast').textContent.includes('boom'),
  `toast=${refused.el('toast').textContent}`);
check('and nothing is polled for', versionLooks(refused) - refusedLooksBefore === 0,
  `${versionLooks(refused) - refusedLooksBefore} looks`);

// ── the wait itself ─────────────────────────────────────────────────────
const w = settings([111, 333]);
check('waitForRestart hands back the new pid', await w.waitForRestart(111, noWait) === 333);
const same = settings([111]);
check('and null when only the old pid ever answers', await same.waitForRestart(111, noWait) === null);

report('restart');
