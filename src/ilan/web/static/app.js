/* ilan web app.
 *
 * A dependency-free single-page front end over the same HTTP API the CLI
 * drives. Every request is same-origin and relative, so the app works
 * unchanged on localhost, behind a reverse proxy, or over a VPN hostname —
 * there is no server address baked in anywhere.
 *
 * Views are plain functions returning HTML strings. Everything interpolated
 * into that HTML goes through esc(): task prompts and agent replies are
 * arbitrary text, and an unescaped '<' in a log line would otherwise let agent
 * output write markup into the page.
 */
'use strict';

// ── utilities ────────────────────────────────────────────────────────

const $ = (sel) => document.querySelector(sel);

// Single definition, shared with the Markdown renderer in markdown.js, so the
// two can never disagree about what escaping means.
const esc = MD.escapeHtml;

/** Seconds since *iso*, or null if it cannot be read. */
function secondsSince(iso) {
  if (!iso) return null;
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return null;
  return Math.max(0, (Date.now() - then) / 1000);
}

/** Compact age like "4m", "3h", "2d" from an ISO timestamp. */
function ago(iso) {
  const secs = secondsSince(iso);
  if (secs === null) return '';
  if (secs < 60) return `${Math.floor(secs)}s`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h`;
  return `${Math.floor(secs / 86400)}d`;
}

// Mirrors _format_compact_duration in cli.py. Durations render in minutes
// below this and hours at or above it, so a `reply -t` interval reads the same
// on a phone as it does in `ilan dashboard`.
const HOUR_SUFFIX_THRESHOLD_SECONDS = 1800;

/** Render a positive duration compactly: "5m", "30m", "1.5h". */
function formatCompactDuration(seconds) {
  const secs = Math.trunc(seconds);
  const [unit, unitSeconds] = secs >= HOUR_SUFFIX_THRESHOLD_SECONDS
    ? ['h', 3600]
    : ['m', 60];
  // Truncate rather than round, matching the CLI: rounding would show a 1799s
  // interval as 30m. Clamp to one tenth so a duration under 6s still reads as
  // nonzero rather than "0m".
  const tenths = Math.max(1, Math.trunc((secs * 10) / unitSeconds));
  const whole = Math.trunc(tenths / 10);
  const remainder = tenths % 10;
  return `${remainder ? `${whole}.${remainder}` : whole}${unit}`;
}

/** "responding every 36m" for an active `reply -t` cycle, else ''. */
function replyEverySuffix(seconds) {
  if (!seconds || seconds <= 0) return '';
  return `responding every ${formatCompactDuration(seconds)}`;
}

/** Render a duration as hours and minutes only: "12m", "2h38m", "30h5m".
 *
 * Deliberately not formatCompactDuration, which renders a tenth of a unit
 * ("0.6h") — good for a configured interval, useless for a running clock,
 * where "2h38m" is what you want to read. Seconds are dropped entirely: a
 * task that has been working for eleven seconds is, for this purpose, new.
 */
function formatHoursMinutes(seconds) {
  const total = Math.max(0, Math.floor(seconds / 60));
  const hours = Math.floor(total / 60);
  const minutes = total % 60;
  return hours ? `${hours}h${minutes}m` : `${minutes}m`;
}

/** A status with its underscores taken out, for reading rather than matching.
 *
 * Only the label loses them. The value keeps them everywhere else: it is an
 * enum member, it names the `.st-*` and `.rs-*` classes that colour the row,
 * and it is what the server and the CLI both speak. Humanising it any earlier
 * than the moment it is written into the page would quietly break the colour.
 */
function humanStatus(status) {
  return String(status ?? '').replaceAll('_', ' ');
}

/** The status as displayed, with how long a WORKING task has been at it.
 *
 * Measured from status_changed_at, which is when the task entered WORKING —
 * not from created_at, which would count the time it spent waiting for a slot
 * or sitting in an earlier status.
 */
function statusLabel(task) {
  const status = displayStatus(task);
  const label = humanStatus(status);
  if (status !== 'WORKING') return label;
  const secs = secondsSince(task.status_changed_at);
  return secs === null ? label : `${label} (for ${formatHoursMinutes(secs)})`;
}

/** "sleeping for 5m" for an active sleep, else ''. */
function sleepSuffix(seconds) {
  if (!seconds || seconds <= 0) return '';
  return `sleeping for ${formatCompactDuration(seconds)}`;
}

/** What the Sleep sheet offers, as [label, seconds], in the order shown.
 *
 * The labels are the CLI's own duration spellings, so what the sheet says is
 * what `ilan sleep` would be told. Only these are offered — a free-text
 * duration was replaced by this list, and the parser that read it went with
 * it — so adding an option here is the whole change.
 */
const SLEEP_CHOICES = [
  ['15m', 15 * 60], ['30m', 30 * 60], ['1h', 3600],
  ['2h', 2 * 3600], ['4h', 4 * 3600], ['8h', 8 * 3600],
];

/** Render `backticked` spans as inline code, escaping everything else.
 *
 * A toast flashes past, and the thing being looked for in it is usually a task
 * name — which reads much faster set apart from the sentence around it.
 *
 * The escaping order is the whole safety argument. Toast text is not ours: it
 * carries task names, which are arbitrary, and server messages, which are
 * arbitrary too. So the message is escaped first and the markers are matched
 * against the *escaped* text, which means nothing a name or an error string
 * contains can become markup. A stray unpaired backtick is simply left alone.
 */
function toastHtml(message) {
  return esc(message).replace(/`([^`]+)`/g, '<code>$1</code>');
}

/** Mark every occurrence of *name* in *message* as code.
 *
 * For messages the server composes — "Reply sent to my-task. Agent resumed." —
 * where the wording is the server's but the name is something this app already
 * knows, so it can point at it without parsing the sentence.
 *
 * A name containing a backtick is left alone rather than being wrapped into a
 * broken marker; nothing rejects such a name today, and quietly mangling the
 * message would be worse than not styling it.
 */
function withCodeName(message, name) {
  const text = String(message ?? '');
  if (!name || name.includes('`') || !text.includes(name)) return text;
  return text.split(name).join(`\`${name}\``);
}

let toastTimer = null;
function toast(message, isError) {
  const el = $('#toast');
  el.innerHTML = toastHtml(message);
  el.className = isError ? 'toast toast-err' : 'toast';
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, isError ? 5200 : 2600);
}

// ── API ──────────────────────────────────────────────────────────────

const api = {
  async request(method, path, body) {
    const opts = { method, headers: {} };
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    const resp = await fetch(path, opts);
    let data = {};
    try { data = await resp.json(); } catch { /* empty or non-JSON body */ }
    return { ok: resp.ok, status: resp.status, data };
  },
  get(path) { return api.request('GET', path); },
  post(path, body) { return api.request('POST', path, body); },
  del(path) { return api.request('DELETE', path); },
};

/** POST and toast the outcome. Returns true when the call succeeded. */
async function act(path, body, okMessage) {
  const { ok, data } = await api.post(path, body);
  if (!ok) {
    toast(data.error || 'Request failed', true);
    return false;
  }
  toast(okMessage || data.message || 'Done');
  return true;
}

/** POST, and if the server asks before ending a `reply -t` cycle, ask and retry.
 *
 * Both replying and sleeping end an active cycle, so the server answers both
 * with the same 409-and-a-flag rather than acting; the two callers used to
 * carry their own copy of the ask-and-retry dance. A 409 without the flag is a
 * plain refusal and is returned as-is.
 *
 * Returns the final response, or null if the user declined.
 */
async function postConfirmingReplyEvery(path, body, question, okLabel) {
  const resp = await api.post(path, body);
  if (resp.status !== 409 || !resp.data.confirm_reply_every) return resp;
  if (!await askConfirm(question(resp.data.reply_every_seconds), okLabel)) return null;
  return api.post(path, { ...body, override_reply_every: true });
}

// ── modal dialogs ────────────────────────────────────────────────────
// iOS discourages window.prompt/confirm and suppresses them outright in some
// standalone (home-screen) contexts, so the app ships its own.

function modal(innerHtml, wire) {
  return new Promise((resolve) => {
    const backdrop = document.createElement('div');
    backdrop.className = 'sheet-backdrop';
    backdrop.innerHTML = `<div class="sheet">${innerHtml}</div>`;
    const close = (value) => { fadeOutAndRemove(backdrop); resolve(value); };
    backdrop.addEventListener('click', (ev) => {
      if (ev.target === backdrop) close(null);
    });
    document.body.appendChild(backdrop);
    wire(backdrop, close);
  });
}

function askText(title, { value = '', placeholder = '', multiline = false, okLabel = 'OK' } = {}) {
  const field = multiline
    ? `<textarea class="field" id="mv" rows="4" placeholder="${esc(placeholder)}">${esc(value)}</textarea>`
    : `<input class="field" id="mv" value="${esc(value)}" placeholder="${esc(placeholder)}"
         autocapitalize="off" autocorrect="off" spellcheck="false">`;
  return modal(
    `<div class="sheet-title">${esc(title)}</div>
     <div class="stack">
       ${field}
       <div class="split">
         <button class="btn" id="mc">Cancel</button>
         <button class="btn btn-primary" id="mo">${esc(okLabel)}</button>
       </div>
     </div>`,
    (root, close) => {
      const input = root.querySelector('#mv');
      input.focus();
      root.querySelector('#mc').onclick = () => close(null);
      root.querySelector('#mo').onclick = () => close(input.value);
      if (!multiline) {
        input.onkeydown = (ev) => { if (ev.key === 'Enter') close(input.value); };
      }
    },
  );
}

function askConfirm(title, okLabel = 'Confirm', danger = false) {
  return modal(
    `<div class="sheet-title">${esc(title)}</div>
     <div class="split">
       <button class="btn" id="mc">Cancel</button>
       <button class="btn ${danger ? 'btn-danger' : 'btn-primary'}" id="mo">${esc(okLabel)}</button>
     </div>`,
    (root, close) => {
      root.querySelector('#mc').onclick = () => close(false);
      root.querySelector('#mo').onclick = () => close(true);
    },
  );
}

function askChoice(title, options) {
  return modal(
    `<div class="sheet-title">${esc(title)}</div>
     ${options.map((o) => `<button class="btn ${o.danger ? 'btn-danger' : ''}"
        data-value="${esc(o.value)}">${esc(o.label)}</button>`).join('')}
     <button class="btn btn-ghost" data-value="">Cancel</button>`,
    (root, close) => {
      root.querySelectorAll('[data-value]').forEach((btn) => {
        btn.onclick = () => close(btn.dataset.value || null);
      });
    },
  );
}

// ── status helpers ───────────────────────────────────────────────────

// Mirrors display_status() in models.py: a task cycling on `reply -t` shows as
// AGENT_IN_LOOP rather than its stored status, because the timer re-prompts the
// agent and no human is actually being waited on.
const IN_LOOP_STATUSES = new Set(['AGENT_FINISHED', 'NEEDS_ATTENTION']);

// Backends whose task names carry a colour cue, mirroring ENGINE_NAME_STYLE in
// models.py. A task with no engine recorded predates the field and runs on the
// default backend, which is Claude; an engine not listed here gets no class and
// inherits the default text colour, the same fallback the CLI takes.
const ENGINE_CLASS = { claude: 'engine-claude', codex: 'engine-codex' };

function engineClass(task) {
  return ENGINE_CLASS[task.engine || 'claude'] || '';
}

function displayStatus(task) {
  if (task.reply_every_seconds && IN_LOOP_STATUSES.has(task.status)) {
    return 'AGENT_IN_LOOP';
  }
  return task.status;
}

// Marks a task whose `reply -t` timer is still running. Deliberately keyed on
// the cycle itself rather than on the AGENT_IN_LOOP display status: that label
// only replaces AGENT_FINISHED and NEEDS_ATTENTION, while the cycle re-fires
// from any live status, so a WORKING task can be looping without ever showing
// the label. _format_reply_every_suffix in the CLI has the same no-status-filter
// behaviour.
function isLooping(task) {
  return Boolean(task.reply_every_seconds && task.reply_every_seconds > 0);
}

// A sleep is only meaningful while the agent is actually WORKING — the value
// lingers on the task after the agent stops, so showing it on a finished task
// would claim something is asleep when nothing is running. _build_name_cell
// guards on TaskStatus.WORKING for exactly that reason, and unlike the
// reply-every cycle this one is genuinely status-dependent.
function isSleeping(task) {
  return task.status === 'WORKING'
    && Boolean(task.sleep_seconds && task.sleep_seconds > 0);
}

/** The closed statuses, each with the endpoint that reopens it.
 *
 * One table rather than a status list and a separate lookup: they were written
 * out twice and had to be kept in step by hand. Each closed status is reopened
 * by its *own* endpoint — ``undone`` only accepts a DONE task and ``undiscard``
 * only a DISCARDED one — so the label has to name the request it will send
 * rather than offering a generic "revive" that is refused half the time.
 */
const REVIVE_ACTIONS = {
  // `label` is the conversation's bottom bar, which has the width for a
  // sentence; `short` is the card, where the same button shares a row with
  // Details and the sentence wrapped.
  DONE: { choice: 'undone', label: 'Undone This Task', short: 'Undone' },
  DISCARDED: { choice: 'undiscard', label: 'Undiscard This Task', short: 'Undiscard' },
};

const TERMINAL_STATUSES = new Set(Object.keys(REVIVE_ACTIONS));

/** How to bring *task* back, or null if it is not closed. */
function reviveAction(task) {
  return REVIVE_ACTIONS[task.status] || null;
}

/** Whether *task* belongs in the list, mirroring the server's own filter.
 *
 * ``handle_list_tasks`` drops a terminal task from a non-``-a`` listing unless
 * it is pinned — a pin deliberately overrides the filter, so a pinned DONE task
 * stays visible until it is unpinned. The web app filters client-side because
 * it fetches once with ``all=true`` and searches closed tasks without a second
 * request, so it has to reproduce that rule rather than inherit it.
 */
function isVisible(task) {
  return !TERMINAL_STATUSES.has(task.status) || task.pinned;
}

// ── collapsed rows ───────────────────────────────────────────────────

// Cards are collapsed by default, so what is stored is the set the user has
// *opened* — a task absent from it is collapsed. The key names that explicitly
// rather than reusing an older one that held the opposite set, so a leftover
// entry can never be read as meaning the inverse of what it recorded.
const EXPANDED_KEY = 'ilan.expanded';

/** Names of the tasks the user has expanded, restored from a previous visit.
 *
 * Persisted rather than kept in memory because iOS evicts a backgrounded PWA
 * freely: an expansion that only survived until the next launch would be undone
 * constantly. Any storage failure — Private Browsing, a full quota — degrades
 * to expanding that still works for this session.
 */
function loadExpanded() {
  try {
    return new Set(JSON.parse(localStorage.getItem(EXPANDED_KEY)) || []);
  } catch {
    return new Set();
  }
}

function saveExpanded() {
  // Drop names that no longer exist so the entry cannot grow without bound as
  // tasks come and go. Guarded on a non-empty list: pruning against a list
  // that has not loaded yet would throw the whole thing away.
  if (state.tasks.length) {
    const live = new Set(state.tasks.map((t) => t.name));
    for (const name of state.expanded) {
      if (!live.has(name)) state.expanded.delete(name);
    }
  }
  try {
    localStorage.setItem(EXPANDED_KEY, JSON.stringify([...state.expanded]));
  } catch {
    /* see loadExpanded: storage is a nicety, not a requirement */
  }
}

function isCollapsed(task) {
  return !state.expanded.has(task.name);
}

// ── state ────────────────────────────────────────────────────────────

const state = {
  tasks: [],
  // `draft` is what is typed in the box; `query` is what the list is actually
  // filtered by. They are separate because searching only happens on Search,
  // so the two disagree the whole time the user is still typing. `draft` also
  // has to outlive a re-render: the list refreshes on a timer, which rebuilds
  // the header, and a draft kept only in the DOM would be wiped mid-sentence.
  draft: '',
  query: '',
  expanded: loadExpanded(),
  // How many assistant messages the conversation reveals. Reset by route() on
  // every navigation into a task, so re-opening one always starts at the tail;
  // renderDetail leaves it alone, since Show More, the refresh button and a
  // sent reply all re-render without meaning to collapse the view back.
  detailShown: 1,
  canned: { tap: '', cancel: '' },
  pollTimer: null,
  // The text the ask bar is currently offering to quote. Cached when the bar
  // appears rather than read on click; see syncAskBar.
  askSelection: '',
  // How the next view should arrive — 'view-push', 'view-pop' or 'view-fade'
  // — set by route() and consumed by the first showView() after it. Only a
  // navigation sets it, so a poll or an in-place re-render replaces the page
  // without replaying the entrance; see showView.
  entering: null,
  // The hash route() last rendered, which is what tells a push from a pop.
  currentHash: null,
};

// ── motion ───────────────────────────────────────────────────────────
// Every animation here is opt-out: the stylesheet only applies them under
// prefers-reduced-motion: no-preference, and the script-driven ones check the
// same query. Each of them animates opacity, transform or a measured height,
// and none of them delays what the user asked for — a sheet's value resolves
// before its fade-out, a toggled card is already in its new state while the
// height eases.

/** Whether the user has asked for less motion. False where the query does
 *  not exist, which includes the test harness. */
function reduceMotion() {
  return typeof matchMedia === 'function'
    && matchMedia('(prefers-reduced-motion: reduce)').matches;
}

/** Replace the page with *html*, arriving the way route() asked, if it did.
 *
 * The entrance class sits on #app rather than on the view, so the four views
 * share one rule, and it is consumed on the first render after a navigation:
 * the children created by a later re-render — the 15-second poll, a toggled
 * card, a sent reply — would otherwise replay the entrance every time.
 *
 * A transient placeholder passes *consume* as false: the "Loading…" shown
 * while the first list is fetched is not the view the navigation was to, and
 * spending the entrance on it would have the list itself snap in behind it.
 */
function showView(html, consume = true) {
  const app = $('#app');
  app.className = state.entering ? `app ${state.entering}` : 'app';
  if (consume) state.entering = null;
  app.innerHTML = html;
}

/** The entrance for moving from *from* to *to*: deeper is a push, back to the
 *  list is a pop, and anything else — the first load included — is a fade. */
function entranceFor(from, to) {
  if (from === null) return 'view-fade';
  if (from === '#/' && to !== '#/') return 'view-push';
  if (to === '#/' && from !== '#/') return 'view-pop';
  return 'view-fade';
}

/** The card whose body button carries *name*, or null off a real DOM. */
function cardOf(name) {
  const row = document.querySelector(`.row[data-toggle="${name}"]`);
  return row && typeof row.closest === 'function' ? row.closest('.card') : null;
}

/** Ease *el* from *from* pixels tall to its current height.
 *
 * The list is re-rendered on a toggle, so the card is a new element that is
 * already its final size; this plays the change in size over it after the
 * fact. Measured and animated rather than styled: a collapsed card's height
 * depends on its content, so there is no fixed value a stylesheet could
 * transition between.
 */
function animateHeight(el, from) {
  if (!el || typeof el.animate !== 'function' || !from || reduceMotion()) return;
  const to = el.getBoundingClientRect().height;
  if (Math.abs(to - from) < 1) return;
  el.style.overflow = 'hidden';
  const run = el.animate(
    [{ height: `${from}px` }, { height: `${to}px` }],
    { duration: 220, easing: 'cubic-bezier(.2, .8, .2, 1)' },
  );
  const done = () => { el.style.overflow = ''; };
  run.finished.then(done, done);
  // What the expansion revealed fades in with it, so it does not pop into
  // place before the card has finished growing around it.
  if (to > from && typeof el.querySelectorAll === 'function') {
    el.querySelectorAll('.row-actions, .row-sum, .meta-detail').forEach((part) => {
      part.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 220, easing: 'ease-out' });
    });
  }
}

/** Fade *el* out, then remove it — or just remove it where it cannot fade.
 *
 * The caller's promise has already resolved by the time this runs, so the
 * fade delays nothing but the pixels. Pointer events are cut at once, so a
 * second tap during the fade cannot land on a sheet that is already spent.
 */
function fadeOutAndRemove(el) {
  if (typeof el.animate !== 'function' || reduceMotion()) { el.remove(); return; }
  if (el.style) el.style.pointerEvents = 'none';
  const run = el.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 140, easing: 'ease-in' });
  const gone = () => el.remove();
  run.finished.then(gone, gone);
}

// ── shared chrome ────────────────────────────────────────────────────

// Every view except the list is entered from the list and returns to it, so
// each carried its own copy of this button and of the handler below.
const BACK_BUTTON =
  '<button class="btn btn-ghost btn-back" id="back" aria-label="Back">‹</button>';

/** Point the back button at the list. Call after writing a view's markup. */
function wireBack() {
  $('#back').onclick = () => { location.hash = '#/'; };
}

// ── list view ────────────────────────────────────────────────────────

/** One of the glyphs defined by the sprite in index.html.
 *
 * The names are fixed by that sprite, so this takes a key rather than a path:
 * a caller cannot pass an id that does not exist without changing this list
 * too, and a mistyped id is invisible — <use> pointing at nothing renders no
 * glyph and throws nothing.
 *
 * aria-hidden throughout: every one of these sits beside a real text label, so
 * announcing it would repeat the label as a shape.
 */
const ICONS = { send: 'i-send', check: 'i-check', chevron: 'i-chevron', undo: 'i-undo' };

function icon(name) {
  return `<svg class="ico" aria-hidden="true"><use href="#${ICONS[name]}"></use></svg>`;
}

/** The status, as the filled pill both the list and the conversation show.
 *
 * One function rather than the same span written out in two places, because
 * "the same as the list" is the whole requirement here and two copies is how
 * that stops being true. The colour arrives through --row-status, which the
 * rs-* class on the container sets, so a caller has to carry that class as
 * well — without it the pill falls back to the plain border grey rather than
 * failing, which is why there is a browser check on the resolved colour.
 */
function statusPill(task) {
  const status = displayStatus(task);
  return `<span class="status st-${esc(status)}">${esc(statusLabel(task))}</span>`;
}

function taskRow(task) {
  const status = displayStatus(task);
  // `ls -c` shows only the pin, alias, name, unread marker and status, so the
  // age is the part a collapsed row drops. It is tagged rather than omitted so
  // collapsing is a class on the card, not a second rendering path that could
  // drift from this one.
  //
  // No backend name: the task name above is already coloured by backend, the
  // same as in `ilan ls`, so the word repeated what the colour was saying.
  //
  // The max-model tag (FABLE, ASTRA) is deliberately *not* a .meta-detail —
  // it has to survive collapsing, since which tasks are burning the expensive
  // model is worth knowing from the list most of it is only ever seen as. Both
  // whether a task carries one and what it says are the server's call (see
  // handle_list_tasks): the model ids and the backend rule live in models.py,
  // and this only reads the answer.
  const meta = [
    statusPill(task),
    task.max_tag ? `<span class="max-tag">${esc(task.max_tag)}</span>` : '',
    `<span class="meta-detail">${
      esc(ago(task.status_changed_at || task.created_at))} ago</span>`,
  ].filter(Boolean).join('');

  const collapsed = isCollapsed(task);

  return `
    <div class="card rs-${esc(status)}${collapsed ? ' collapsed' : ''}">
      <button class="row" data-toggle="${esc(task.name)}"
              aria-expanded="${collapsed ? 'false' : 'true'}"
              title="${collapsed ? 'Expand' : 'Collapse'} ${esc(task.name)}">
        <span class="row-top">
          ${task.alias ? `<span class="alias">${esc(task.alias)}</span>` : ''}
          <span class="row-name ${engineClass(task)}${
            isLooping(task) ? ' looping' : ''}${
            task.needs_review ? ' unread' : ''}"${
            isLooping(task)
              ? ` title="${esc(replyEverySuffix(task.reply_every_seconds))}"` : ''
            }>${esc(task.name)}</span>
          ${isSleeping(task)
            ? `<span class="sleep">(${esc(sleepSuffix(task.sleep_seconds))})</span>` : ''}
          ${task.pinned ? '<span class="pin">📌</span>' : ''}
        </span>
        ${task.summary_one_liner
          ? `<span class="row-sum">${esc(task.summary_one_liner)}</span>` : ''}
        <span class="row-meta">${meta}</span>
      </button>
      <div class="row-actions">
        ${TERMINAL_STATUSES.has(task.status) ? `
        <button class="act act-revive" data-revive="${esc(task.name)}">
          ${icon('undo')}<span>${esc(reviveAction(task).short)}</span></button>` : `
        <button class="act act-tap" data-tap="${esc(task.name)}">
          ${icon('send')}<span>Tap</span></button>
        <button class="act act-done" data-done="${esc(task.name)}">
          ${icon('check')}<span>Done</span></button>`}
        <button class="act act-details" data-details="${esc(task.name)}">
          <span>Details</span>${icon('chevron')}</button>
      </div>
    </div>`;
}

/** Whether *task* matches *query*, which is matched against its name alone.
 *
 * The alias, the status, the backend, the one-line summary and the task number
 * were all searchable, and between them they made short queries useless: two
 * characters is an alias, so typing the start of a name would pull in whatever
 * task happened to be aliased that way, and any word of a status matched every
 * task sharing it. A search box beside a list of names is read as searching
 * the names.
 */
function matchesQuery(task, query) {
  if (!query) return true;
  return String(task.name ?? '').toLowerCase().includes(query.toLowerCase());
}

/** Apply the typed text as the active filter. Nothing filters until this runs. */
function applySearch() {
  state.query = state.draft.trim();
  renderList();
}

function renderList() {
  // `search` in the CLI always searches closed tasks too, so a query implies
  // -a; without one, honour the toggle.
  const searching = Boolean(state.query);
  const visible = state.tasks
    .filter((t) => searching || isVisible(t))
    .filter((t) => matchesQuery(t, state.query));

  // Rendered flat, in the order /tasks returned: pinned first, then oldest
  // first. `ilan dashboard` passes the same response straight to its table
  // without re-sorting, so leaving the order alone here is what makes the two
  // agree — an earlier version grouped by status and re-sorted each group by
  // status_changed_at descending, which disagreed on both axes at once.
  const body = visible.length
    ? visible.map(taskRow).join('')
    : `<div class="empty">${state.tasks.length
        ? 'No tasks match.' : 'No tasks yet.'}</div>`;

  // Collapse All is offered only when a card on screen is actually open, so it
  // is never a tap that appears to do nothing. Judged on what is *visible*
  // rather than on the stored set: a search can hide an expanded task, and
  // enabling the button for a card the user cannot see would leave them
  // tapping at a list that does not change. What the tap then clears is the
  // whole set rather than the visible part of it, which is what the word "All"
  // says and what leaves the list in one state instead of half of one.
  const anyOpen = visible.some((t) => !isCollapsed(t));

  showView(`
    <header class="hdr">
      <div class="hdr-row">
        <!-- The same icon the home screen uses, so the page a phone opens
             looks like the thing that was tapped to open it. Decorative: the
             word beside it already says what this is, and alt text here would
             have a screen reader announce the name twice. Relative, like every
             other asset the page loads, so nothing assumes a mount path. -->
        <img class="hdr-logo" src="icon-180.png" alt="" width="26" height="26">
        <h1 class="hdr-title hdr-wordmark">ilan</h1>
        <!-- Glyph rather than the word "Refresh", which is what makes room for
             Collapse All beside it, and matches the same control in a
             conversation header. Every glyph-only button in this row carries an
             aria-label and a title: the mark is the whole label now, so
             without one a screen reader announces the character or nothing,
             and a title is what gives a pointer user the same name on hover. -->
        <button class="btn btn-sm" id="do-refresh"
                aria-label="Refresh the list" title="Refresh the list">↻</button>
        <button class="btn btn-sm" id="collapse-all"
                ${anyOpen ? '' : 'disabled'}>Collapse All</button>
        <button class="btn btn-sm" id="go-config"
                aria-label="Settings" title="Settings">⚙</button>
        <button class="btn btn-sm btn-primary" id="go-new"
                aria-label="New task" title="New task">+</button>
      </div>
      <div class="hdr-row">
        <input class="field" id="q" type="search" placeholder="Search task names"
               value="${esc(state.draft)}" autocapitalize="off" autocorrect="off"
               spellcheck="false" enterkeyhint="search">
        <button class="btn btn-sm btn-primary" id="do-search">Search</button>
        ${state.query
          ? '<button class="btn btn-sm" id="clear-search">Clear</button>' : ''}
      </div>
    </header>
    <main class="main">${body}</main>`);

  const q = $('#q');
  // Typing only updates the draft. No filtering, and deliberately no re-render:
  // rebuilding the header mid-keystroke would drop the caret to the end of the
  // field, which is what the previous search-as-you-type version did.
  q.oninput = () => { state.draft = q.value; };
  // iOS labels the keyboard's action key "search" via enterkeyhint above, so
  // pressing it has to do the same thing the button does.
  q.onkeydown = (ev) => { if (ev.key === 'Enter') applySearch(); };
  $('#do-search').onclick = applySearch;
  const clearBtn = $('#clear-search');
  if (clearBtn) {
    clearBtn.onclick = () => {
      state.draft = '';
      state.query = '';
      renderList();
    };
  }
  // Returns the load so a caller can await it; the browser ignores the value.
  $('#do-refresh').onclick = () => refreshList();
  $('#collapse-all').onclick = () => collapseAll();
  $('#go-config').onclick = () => { location.hash = '#/config'; };
  $('#go-new').onclick = () => { location.hash = '#/new'; };
  // The row itself is the disclosure control: tapping anywhere on the card
  // body toggles it, which is a far bigger target than a chevron and is what
  // a card that summarises something is expected to do. The action buttons sit
  // outside this button rather than inside it — a button cannot be nested in
  // another one, and keeping them siblings is also what stops a tap on them
  // from toggling the card.
  document.querySelectorAll('.row').forEach((row) => {
    row.onclick = () => toggleCollapsed(row.dataset.toggle);
  });
  document.querySelectorAll('.act-tap').forEach((btn) => {
    btn.onclick = () => tapFromCard(btn.dataset.tap);
  });
  document.querySelectorAll('.act-details').forEach((btn) => {
    btn.onclick = () => {
      location.hash = `#/t/${encodeURIComponent(btn.dataset.details)}`;
    };
  });
  document.querySelectorAll('.act-done').forEach((btn) => {
    btn.onclick = () => doneFromCard(btn.dataset.done);
  });
  document.querySelectorAll('.act-revive').forEach((btn) => {
    btn.onclick = () => reviveFromCard(btn.dataset.revive);
  });
  updateBadge();
}

/** Ask a task for a status update, after confirming.
 *
 * The confirmation is not ceremony: a tap posts a real message that interrupts
 * the agent, and the button now sits on every expanded card rather than behind
 * the actions sheet, so it is far easier to hit by accident.
 */
/** Reload the list once something has changed a task.
 *
 * Deliberately without a delay. The server persists the new state before it
 * answers — a reply ends in runner.start(), which sets WORKING and writes the
 * task inside the request — so a list fetched the moment the response lands
 * already shows the change. Waiting would only make the card update later than
 * it needs to.
 *
 * Only the list is reloaded, and only while it is the view on screen. Every
 * other view re-renders itself after acting, and route() reloads the list on
 * the way back to it, so a task changed from the conversation is already
 * fresh by the time the list is looked at again.
 */
function refreshListAfterChange() {
  if ((location.hash || '#/') !== '#/') return undefined;
  return loadList(false);
}

async function tapFromCard(name) {
  const ok = await askConfirm(`Ask ${name} for a status update?`, 'Tap');
  if (!ok) return;
  // Only on success: a refused reply changed nothing to show.
  if (await sendReply(name, state.canned.tap)) await refreshListAfterChange();
}

/** Close a task from its card, after confirming.
 *
 * Closing is not a display change: it drops the task's alias, and it kills the
 * agent outright if one is still running. Sitting one tap away on every
 * expanded card, next to two buttons that change nothing, it has to ask first
 * — the same reason Tap does, for a heavier outcome.
 *
 * The list is reloaded rather than patched in place: a closed task leaves the
 * default listing entirely, so the row has to go, and the server is the thing
 * that decides that.
 */
/** Reopen a closed task from its card.
 *
 * No confirmation, matching the same button in the conversation's bottom bar:
 * reopening is not destructive and is undone by marking the task done again.
 * The endpoint comes from REVIVE_ACTIONS, since undone and undiscard are each
 * refused for the other's status — one generic "revive" would 409 half the
 * time. The server's reply carries no message, so the toast is written here,
 * with the name as code the way every other card toast reads.
 */
async function reviveFromCard(name) {
  const task = state.tasks.find((t) => t.name === name);
  const revive = task ? reviveAction(task) : null;
  if (!revive) return;
  const path = `/tasks/${encodeURIComponent(name)}/${revive.choice}`;
  if (await act(path, undefined, `Reopened \`${name}\``)) await refreshListAfterChange();
}

async function doneFromCard(name) {
  const task = state.tasks.find((t) => t.name === name);
  const ok = await askConfirm(
    task && task.status === 'WORKING'
      ? `Mark ${name} as done? This stops the agent that is running.`
      : `Mark ${name} as done?`,
    'Mark done',
  );
  if (!ok) return;
  if (await act(`/tasks/${encodeURIComponent(name)}/done`)) await refreshListAfterChange();
}

/** Expand or collapse one task's card, and remember which.
 *
 * Membership means expanded, since collapsed is the default state. */
function toggleCollapsed(name) {
  // Measured before the re-render replaces the card, so the new one can be
  // eased from the size the old one was.
  const before = cardOf(name);
  const from = before && typeof before.getBoundingClientRect === 'function'
    ? before.getBoundingClientRect().height : 0;
  if (state.expanded.has(name)) {
    state.expanded.delete(name);
  } else {
    state.expanded.add(name);
  }
  saveExpanded();
  renderList();
  animateHeight(cardOf(name), from);
}

/** Close every card, including any the current search is hiding.
 *
 * Written through the same save-and-render pair as toggling one card, so the
 * stored set and the screen cannot disagree — clearing the set without saving
 * would leave the list collapsed until the next reload and then expanded
 * again, which reads as the button having been forgotten.
 */
function collapseAll() {
  state.expanded.clear();
  saveExpanded();
  renderList();
}

/** Fetch the list now, on demand.
 *
 * Confirms with a toast because the result is often visually identical — if
 * nothing has changed since the last poll, a silent button looks broken.
 */
async function refreshList() {
  await loadList(false);
  toast('Refreshed');
}

async function loadList(showSpinner = true) {
  if (showSpinner && !state.tasks.length) {
    showView('<div class="empty">Loading…</div>', false);
  }
  const { ok, data } = await api.get('/tasks?all=true');
  if (!ok) {
    showView(`<div class="empty">Cannot reach the ilan server.<br>
      ${esc(data.error || '')}</div>`);
    return;
  }
  state.tasks = data.tasks || [];
  renderList();
}

// ── asking about a selection ─────────────────────────────────────────
//
// iOS shows its own callout over a selection (Copy, Look Up, Share) and a web
// page cannot add an entry to it — that is a native-app capability. A bubble
// floating by the selection would therefore be fighting that menu for the same
// few square centimetres. The action lives in a bar docked above the composer
// instead: never covered, always in the same place, and within thumb reach.

// Longer selections are quoted with the middle elided. The opening and closing
// words are what identify a passage; a whole screen of quoted text just buries
// the question underneath it.
const QUOTE_MAX_CHARS = 600;

/** Collapse *text* to a single line, eliding the middle past *limit*.
 *
 * Newlines go too. This is a citation, not a reproduction — the agent still
 * has the message it came from, so the quote only has to say which passage is
 * being asked about.
 */
function elide(text, limit = QUOTE_MAX_CHARS) {
  const clean = String(text ?? '').replace(/\s+/g, ' ').trim();
  if (clean.length <= limit) return clean;
  const half = Math.floor((limit - 3) / 2);
  return `${clean.slice(0, half).trimEnd()} … ${clean.slice(-half).trimStart()}`;
}

/** The reply text that quotes *selected* and leaves room for a question.
 *
 * A Markdown blockquote: it is how the agent's own replies are formatted, it
 * survives being read back as plain text, and it keeps the quote visually
 * separate from the question the user is about to type under it.
 */
function quoteForReply(selected) {
  return `> ${elide(selected)}\n\n`;
}

/** The selected text, but only when the selection lies inside a message.
 *
 * Selecting the header, the status line or the composer is not a question
 * about the conversation. Both ends have to be inside a message body: a drag
 * that starts in a reply and ends in the page chrome would otherwise quote
 * whatever the browser decided to include along the way.
 */
function selectedMessageText() {
  const sel = typeof getSelection === 'function' ? getSelection() : null;
  if (!sel || sel.isCollapsed) return '';
  const text = String(sel).trim();
  if (!text) return '';
  const inMessage = (node) => {
    const el = node && (node.nodeType === 1 ? node : node.parentElement);
    return Boolean(el && el.closest && el.closest('.msg-body'));
  };
  return inMessage(sel.anchorNode) && inMessage(sel.focusNode) ? text : '';
}

/** Show or hide the ask bar to match what is currently selected.
 *
 * The text is cached rather than re-read on click: tapping anything can clear
 * the selection before the click handler runs, so reading it at that point is
 * a race the button loses on some browsers.
 */
function syncAskBar() {
  const bar = $('#ask-bar');
  if (!bar) return;
  const text = selectedMessageText();
  state.askSelection = text;
  bar.hidden = !text;
  const preview = $('#ask-preview');
  if (text && preview) preview.textContent = elide(text, 90);
}

/** Put the cached selection into the composer as a quote to ask about. */
function askAboutSelection() {
  const selected = state.askSelection;
  const box = $('#reply');
  if (!selected || !box) return;
  const quote = quoteForReply(selected);
  // Appended, not replacing: a half-typed question is not thrown away, and
  // quoting two passages before asking about both is a reasonable thing to do.
  box.value = box.value.trim() ? `${box.value.trimEnd()}\n\n${quote}` : quote;
  state.askSelection = '';
  const bar = $('#ask-bar');
  if (bar) bar.hidden = true;
  box.focus();
  if (typeof box.setSelectionRange === 'function') {
    box.setSelectionRange(box.value.length, box.value.length);
  }
  if (box.oninput) box.oninput();
}

// ── detail view ──────────────────────────────────────────────────────

function messageHtml(entry) {
  const foot = [
    entry.model, entry.effort, ago(entry.timestamp),
  ].filter(Boolean).join(' · ');
  const isUser = entry.role === 'user';
  // Only agent replies are Markdown, matching `ilan tail --md`. A user message
  // is text the user typed, so showing it back verbatim is the honest thing —
  // and it means an asterisk in a reply never silently becomes emphasis.
  const body = isUser
    ? `<p class="msg-body">${esc(entry.content)}</p>`
    : `<div class="msg-body md">${MD.render(entry.content)}</div>`;
  return `
    <div class="msg msg-${isUser ? 'user' : 'assistant'}">
      <div class="msg-role">${esc(entry.role)}</div>
      ${body}
      ${foot ? `<div class="msg-foot">${esc(foot)}</div>` : ''}
    </div>`;
}

async function renderDetail(name) {
  // ?n= asks the server for the last N assistant messages plus whatever user
  // messages precede each of them — the same slice `ilan tail -n` shows. Show
  // More just increments N, so the reveal rule lives in one place rather than
  // being re-derived here.
  const [taskResp, bodyResp] = await Promise.all([
    api.get(`/tasks/${encodeURIComponent(name)}`),
    api.get(`/tasks/${encodeURIComponent(name)}/tail?n=${state.detailShown}`),
  ]);

  if (!taskResp.ok) {
    showView(`<div class="empty">${esc(taskResp.data.error || 'Not found')}
      <br><br><button class="btn" onclick="location.hash='#/'">Back</button></div>`);
    return;
  }

  const task = taskResp.data.task;
  const status = displayStatus(task);

  const entries = bodyResp.data.entries || [];
  const body = entries.length
    ? entries.map(messageHtml).join('')
    : `<div class="empty">${esc(bodyResp.data.warning || 'No messages yet.')}</div>`;

  // The server returns every entry once N passes the number of assistant
  // messages there are, so fewer than N of them coming back means this is the
  // whole conversation and there is nothing left to reveal.
  const shownAssistants = entries.filter((e) => e.role === 'assistant').length;
  const hasMore = shownAssistants >= state.detailShown;

  // Deliberately no "from <parent>". Where a task was branched from is a fact
  // about how it started, not about what it is doing now, and this line is
  // read to answer the latter. On a phone it is one line competing for the
  // width, and the parent's name is often longer than everything else on it.
  // The API still reports parent_name and `ilan ls` still shows the lineage.
  //
  // The backend is gone from here too, and it is not lost with it: the task
  // name in the title above is already coloured by backend, the same as in the
  // list, so the line was spending width to repeat in a word what the colour
  // was saying anyway. The ••• sheet still names it, on the one entry that
  // changes it.
  const sub = [
    task.model,
    sleepSuffix(task.sleep_seconds),
    replyEverySuffix(task.reply_every_seconds),
  ].filter(Boolean).join(' · ');

  // A closed task has nothing to reply to, so the composer's place along the
  // bottom of the screen goes to the one thing you do want from it: reopening
  // it. Reaching that through the ••• sheet took three taps to run a single
  // unambiguous action.
  const revive = reviveAction(task);
  const footer = revive
    ? `
    <div class="composer">
      <button class="btn btn-primary btn-revive" id="revive">${esc(revive.label)}</button>
    </div>`
    : `
    <div class="ask-bar" id="ask-bar" hidden>
      <span class="ask-preview" id="ask-preview"></span>
      <button class="btn btn-sm btn-primary" id="ask-btn">Ask about this</button>
    </div>
    <div class="composer">
      <div class="composer-field">
        <textarea class="field" id="reply" rows="1"
                  placeholder="Reply to ${esc(task.name)}"></textarea>
        <button class="btn btn-ghost btn-clear" id="clear-reply"
                aria-label="Clear the message" title="Clear the message" disabled>✕</button>
      </div>
      <button class="btn btn-primary" id="send" disabled>Send</button>
    </div>`;

  showView(`
    <header class="hdr">
      <div class="hdr-row">
        ${BACK_BUTTON}
        <h1 class="hdr-title">
          ${task.alias ? `<span class="alias">${esc(task.alias)}</span> ` : ''}<span
            class="${engineClass(task)}${isLooping(task) ? ' looping' : ''}"
            >${esc(task.name)}</span>
        </h1>
        <!-- Named for the same reason as the list's glyph buttons: the mark is
             the whole label, so without one there is nothing to announce. -->
        <button class="btn btn-sm" id="refresh"
                aria-label="Refresh this conversation"
                title="Refresh this conversation">↻</button>
        <button class="btn btn-sm" id="actions"
                aria-label="More actions" title="More actions">•••</button>
      </div>
      <!-- row-meta as well as hdr-sub, and deliberately: it is the same
           component as the card's meta line, so it reuses the card's class
           rather than a header-only copy of it. That is what makes the pill
           identical on both pages instead of merely similar — one rule styles
           both, so there is nothing to keep in step. rs-* is what feeds the
           pill its colour. -->
      <!-- The max-model tag follows the pill here exactly as it does on the
           card: same container class, same rule, same position. Beside the
           status rather than the name because the title is the one line on
           this page that cannot afford to give up width. -->
      <p class="hdr-sub row-meta rs-${esc(status)}">${statusPill(task)}${
        task.max_tag ? `<span class="max-tag">${esc(task.max_tag)}</span>` : ''}${
        sub ? `<span class="meta-detail">${esc(sub)}</span>` : ''}</p>
      ${hasMore ? `
      <div class="hdr-row">
        <button class="btn btn-sm show-more" id="show-more">Show More</button>
      </div>` : ''}
    </header>
    <main class="main">${body}</main>
    <div class="dock">${footer}</div>`);

  wireBack();
  $('#refresh').onclick = () => renderDetail(name);
  $('#actions').onclick = () => showActions(task);
  const more = $('#show-more');
  if (more) {
    // Returns the render so a caller can await it. The browser ignores the
    // value; it is what lets the tests assert on the result of a click rather
    // than on whatever happens to have settled by then.
    more.onclick = () => {
      state.detailShown += 1;
      return renderDetail(name);
    };
  }

  const reviveBtn = $('#revive');
  if (reviveBtn) {
    // runAction posts to the endpoint, reports a refusal, and re-renders on
    // success — which is what puts the reopened status and the reply box on
    // screen. Returning the promise is for the tests, as with Show More.
    reviveBtn.onclick = () => runAction(revive.choice, task);
  }

  const askBtn = $('#ask-btn');
  if (askBtn) {
    // Pressing the bar must not clear the selection before the click lands.
    // The text is cached anyway, but keeping the highlight up while the quote
    // is inserted is also what makes the button feel like it acted on it.
    const keepSelection = (ev) => { if (ev && ev.preventDefault) ev.preventDefault(); };
    const bar = $('#ask-bar');
    if (bar) bar.onmousedown = keepSelection;
    askBtn.onmousedown = keepSelection;
    askBtn.onclick = askAboutSelection;
  }
  // A fresh render replaces the bar, so whatever was selected before it is no
  // longer on screen to point at.
  state.askSelection = '';

  const replyBox = $('#reply');
  if (replyBox) {
    const clearBtn = $('#clear-reply');
    const sendBtn = $('#send');
    // The box's height and both of its buttons follow its contents, so they
    // are decided in one place: anything that puts text in the box calls this
    // rather than remembering to do three things.
    const syncComposer = () => {
      replyBox.style.height = 'auto';
      replyBox.style.height = `${Math.min(replyBox.scrollHeight, 220)}px`;
      // Whitespace is not a message and is not worth clearing, so both buttons
      // turn on the same trimmed test rather than on the raw value.
      const hasDraft = Boolean(replyBox.value.trim());
      if (clearBtn) clearBtn.disabled = !hasDraft;
      // Send goes grey rather than staying blue and refusing the tap: a button
      // that looks live and does nothing reads as the app being broken.
      if (sendBtn) sendBtn.disabled = !hasDraft;
    };
    replyBox.oninput = syncComposer;
    // Once up front, so the button's state is derived from what is in the box
    // rather than assuming it starts empty.
    syncComposer();
    if (clearBtn) {
      clearBtn.onclick = () => {
        replyBox.value = '';
        // Through the same path as typing, so the box shrinks back and the
        // button disables itself rather than being left lit over nothing.
        syncComposer();
        replyBox.focus();
      };
    }
    sendBtn.onclick = async () => {
      const text = replyBox.value.trim();
      if (!text) return;
      // Held down for the round trip so a second tap cannot send twice.
      sendBtn.disabled = true;
      const sent = await sendReply(task.name, text);
      if (sent) {
        replyBox.value = '';
        renderDetail(name);
        return;
      }
      // The send failed, so the draft is still there: let the contents decide
      // whether the button comes back, rather than assuming it should.
      syncComposer();
    };
  }
}

/** Send a reply, handling the server's reply-every confirmation challenge. */
async function sendReply(name, message, extra = {}) {
  const resp = await postConfirmingReplyEvery(
    `/tasks/${encodeURIComponent(name)}/reply`,
    { message, ...extra },
    (seconds) => `This ends the reply-every cycle (${
      formatCompactDuration(seconds)}). Continue?`,
    'Send anyway',
  );
  if (resp === null) return false;
  if (!resp.ok) {
    toast(resp.data.error || 'Reply failed', true);
    return false;
  }
  toast(withCodeName(resp.data.message || 'Reply sent', name));
  return true;
}

// ── task actions ─────────────────────────────────────────────────────

function showActions(task) {
  const terminal = TERMINAL_STATUSES.has(task.status);

  const options = [];
  if (!terminal) {
    options.push({ value: 'tap', label: 'Tap — ask for a status update' });
    options.push({ value: 'cancel', label: 'Cancel — retract my last message' });
    options.push({ value: 'sleep', label: 'Sleep…' });
    options.push({ value: 'done', label: 'Mark done' });
  } else {
    options.push({
      value: task.status === 'DONE' ? 'undone' : 'undiscard',
      label: task.status === 'DONE' ? 'Un-done' : 'Un-discard',
    });
  }
  options.push({ value: task.pinned ? 'unpin' : 'pin', label: task.pinned ? 'Unpin' : 'Pin' });
  options.push({ value: task.model ? 'unmax' : 'max', label: task.model ? 'Unmax' : 'Max' });
  options.push({ value: 'switch-backend', label: `Switch backend (now ${task.engine || '?'})` });
  options.push({ value: 'rename', label: 'Rename…' });
  options.push({ value: 'branch', label: 'Branch…' });
  if (task.gist_url) options.push({ value: 'gist', label: 'Open conversation Gist' });
  if (task.status === 'WORKING') {
    options.push({ value: 'kill', label: 'Kill running agent', danger: true });
  }
  options.push({ value: 'delete', label: 'Delete task', danger: true });

  askChoice(task.name, options).then((choice) => {
    if (choice) runAction(choice, task);
  });
}

// Actions that are nothing but a POST to the endpoint of the same name. This
// was a map from each choice to itself, which read as though the two could
// differ; they never did.
const BARE_POST_ACTIONS = new Set([
  'done', 'undone', 'undiscard',
  'pin', 'unpin', 'max', 'unmax', 'switch-backend',
]);

async function runAction(choice, task) {
  const t = encodeURIComponent(task.name);
  const back = () => renderDetail(task.name);

  if (BARE_POST_ACTIONS.has(choice)) {
    if (await act(`/tasks/${t}/${choice}`)) back();
    return;
  }

  switch (choice) {
    case 'tap':
      if (await sendReply(task.name, state.canned.tap)) back();
      return;

    case 'cancel': {
      if (!await askConfirm('Retract your last message and tell the agent to stop?',
        'Cancel it')) return;
      if (await sendReply(task.name, state.canned.cancel)) back();
      return;
    }

    case 'sleep': {
      // A fixed set rather than a typed duration: on a phone, six taps to
      // choose from beats a keyboard and a format to remember, and every
      // value offered is one the server accepts, so there is no "could not
      // read that" path left to fall into.
      const chosen = await askChoice('Sleep for…',
        SLEEP_CHOICES.map(([label, seconds]) => ({ value: String(seconds), label })));
      if (chosen === null) return;
      const seconds = Number(chosen);
      const resp = await postConfirmingReplyEvery(
        `/tasks/${t}/sleep`,
        { seconds },
        () => 'This ends the reply-every cycle. Continue?',
        'Sleep anyway',
      );
      if (resp === null) return;
      if (!resp.ok) { toast(resp.data.error || 'Failed', true); return; }
      toast(`Sleeping for ${formatCompactDuration(seconds)}`);
      back();
      return;
    }

    case 'rename': {
      const next = await askText('New name', { value: task.name });
      if (!next || next === task.name) return;
      if (await act(`/tasks/${t}/rename`, { new_name: next })) {
        location.hash = `#/t/${encodeURIComponent(next)}`;
      }
      return;
    }

    case 'branch': {
      const message = await askText('Message for the new branch', { multiline: true });
      if (!message) return;
      const newName = await askText('Name for the branch (blank to auto-name)');
      if (newName === null) return;
      const body = newName ? { message, new_name: newName } : { message };
      const { ok, data } = await api.post(`/tasks/${t}/branch`, body);
      if (!ok) { toast(data.error || 'Branch failed', true); return; }
      toast(data.name ? `Branched to \`${data.name}\`` : 'Branched to a new task');
      if (data.name) location.hash = `#/t/${encodeURIComponent(data.name)}`;
      return;
    }

    case 'kill':
      if (!await askConfirm(`Kill the running agent on ${task.name}?`, 'Kill', true)) return;
      if (await act(`/tasks/${t}/kill`)) back();
      return;

    case 'delete': {
      if (!await askConfirm(`Delete ${task.name} and all its logs? This cannot be undone.`,
        'Delete', true)) return;
      const { ok, data } = await api.del(`/tasks/${t}`);
      if (!ok) { toast(data.error || 'Delete failed', true); return; }
      toast(`Deleted \`${task.name}\``);
      location.hash = '#/';
      return;
    }

    case 'gist':
      window.open(task.gist_url, '_blank', 'noopener');
      return;

    default:
      toast(`Unknown action: ${choice}`, true);
  }
}

// ── new task view ────────────────────────────────────────────────────

function renderNew() {
  showView(`
    <header class="hdr">
      <div class="hdr-row">
        ${BACK_BUTTON}
        <h1 class="hdr-title">New task</h1>
      </div>
    </header>
    <main class="main">
      <div class="stack">
        <div class="label">Prompt</div>
        <textarea class="field" id="prompt" rows="8"
          placeholder="What should the agent do?"></textarea>
        <div class="label">Name (blank auto-names a scratch task)</div>
        <input class="field" id="name" placeholder="my-task"
          autocapitalize="off" autocorrect="off" spellcheck="false">
        <div class="label">Backend</div>
        <select class="field" id="agent">
          <option value="">Default</option>
          <option value="claude">claude</option>
          <option value="codex">codex</option>
        </select>
        <label class="kv kv-plain">
          <span class="kv-k">Run on the max model</span>
          <input class="checkbox" type="checkbox" id="max">
        </label>
        <button class="btn btn-primary" id="create">Create task</button>
      </div>
    </main>`);

  wireBack();
  $('#create').onclick = async () => {
    const prompt = $('#prompt').value.trim();
    if (!prompt) { toast('A prompt is required', true); return; }
    const body = { prompt };
    const name = $('#name').value.trim();
    if (name) body.name = name;
    const agent = $('#agent').value;
    if (agent) body.agent = agent;
    if ($('#max').checked) body.max = true;

    $('#create').disabled = true;
    const { ok, data } = await api.post('/tasks', body);
    $('#create').disabled = false;
    if (!ok) { toast(data.error || 'Could not create task', true); return; }
    toast(data.name ? `Created \`${data.name}\`` : 'Created');
    location.hash = data.name ? `#/t/${encodeURIComponent(data.name)}` : '#/';
  };
}

// ── config view ──────────────────────────────────────────────────────

// Editing these from a phone would break the very session doing the editing,
// or leak a secret onto a screen in public. They stay read-only here; the CLI
// still sets them.
const CONFIG_READONLY = new Set(['workdir', 'github-token', 'api-key-claude', 'api-key-codex']);

// How long the app waits for a restarted server to come back: forty looks,
// half a second apart. A restart takes a second or two; twenty seconds is
// long enough that a timeout means something is wrong on the host.
const RESTART_WAIT_MS = 500;
const RESTART_ATTEMPTS = 40;

const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** Poll /version until a *different* process answers. Returns its pid, or
 *  null once the attempts are spent.
 *
 * A different pid is the test, not a live answer: the old server keeps
 * answering for a moment after it has been told to stop, and the new one
 * usually reports the same version and commit. Fetch failures are expected in
 * the gap between the two and are swallowed rather than surfaced — that gap
 * is what this is waiting through.
 *
 * *wait* is injectable so a test can run the loop without real time passing.
 */
async function waitForRestart(oldPid, wait = pause) {
  for (let attempt = 0; attempt < RESTART_ATTEMPTS; attempt += 1) {
    await wait(RESTART_WAIT_MS);
    try {
      const { ok, data } = await api.get('/version');
      if (ok && data.pid && data.pid !== oldPid) return data.pid;
    } catch {
      /* the old server is gone and the new one is not listening yet */
    }
  }
  return null;
}

/** Restart the server from Settings, then wait for it to come back.
 *
 * Asks first: nothing is lost by a restart — agents keep running — but the
 * app goes dark for a second or two, which is worth a deliberate tap.
 */
async function restartServer(oldPid, wait = pause) {
  const ok = await askConfirm(
    'Restart the server? Running agents keep running; the app will reconnect.',
    'Restart',
  );
  if (!ok) return;
  const resp = await api.post('/restart');
  if (!resp.ok) { toast(resp.data.error || 'Restart failed', true); return; }
  toast('Restarting…');
  const pid = await waitForRestart(resp.data.pid ?? oldPid, wait);
  if (pid === null) {
    toast('The server has not come back yet — check it on the host', true);
    return;
  }
  toast(`Server restarted (pid ${pid})`);
  renderConfig();
}

// ── push notifications ──────────────────────────────────────────────
// A phone that has installed the app can be told when a task finishes. The
// server composes and sends the note (see ilan/push.py); this side registers
// the worker that shows it, asks the browser for permission, and hands the
// resulting subscription to the server.

/** Register the notification worker, once, and listen for it steering the
 *  page when a notification is tapped. Harmless where there is no worker
 *  support: the browser simply never shows a notification. */
function registerServiceWorker() {
  if (typeof navigator === 'undefined' || !('serviceWorker' in navigator)) return;
  // Relative, like every other URL the app uses: the worker's scope becomes
  // the app's own mount path, whatever that is.
  navigator.serviceWorker.register('sw.js').catch(() => { /* no worker, no notifications */ });
  navigator.serviceWorker.addEventListener('message', (event) => {
    const msg = event.data || {};
    if (msg.type === 'navigate' && typeof msg.hash === 'string') location.hash = msg.hash;
  });
}

/** Why this device can or cannot receive notifications.
 *
 * 'unsupported' — no service workers at all.
 * 'install'     — workers yes, push no. On iOS this is exactly "the app is
 *                 open in Safari rather than from the Home Screen": Apple
 *                 gives push only to installed web apps.
 * 'blocked'     — the user said no once; only the OS settings can undo that.
 * 'ready'       — can subscribe, or is subscribed.
 */
function pushSupport() {
  if (typeof navigator === 'undefined' || !('serviceWorker' in navigator)) return 'unsupported';
  if (typeof window === 'undefined' || !('PushManager' in window)
      || typeof Notification === 'undefined') return 'install';
  if (Notification.permission === 'denied') return 'blocked';
  return 'ready';
}

/** The application server key the browser wants as bytes, from the base64url
 *  the server hands out. */
function urlBase64ToUint8Array(base64) {
  const padded = base64 + '='.repeat((4 - (base64.length % 4)) % 4);
  const raw = atob(padded.replace(/-/g, '+').replace(/_/g, '/'));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

/** Where this device stands: support, whether it is subscribed, and how many
 *  devices the server knows. Asks the server only when it could matter. */
async function pushState() {
  const support = pushSupport();
  const state = { support, subscribed: false, endpoint: null, devices: null, serverReady: true };
  if (support !== 'ready') return state;
  try {
    const registration = await navigator.serviceWorker.ready;
    const sub = await registration.pushManager.getSubscription();
    if (sub) { state.subscribed = true; state.endpoint = sub.endpoint; }
  } catch { /* an unreadable subscription reads as none */ }
  const { ok, data } = await api.get('/push');
  if (!ok) { state.serverReady = false; return state; }
  state.devices = data.subscriptions ?? null;
  return state;
}

/** Turn notifications on for this device.
 *
 * Permission is asked for *first*, before anything is fetched: iOS only shows
 * the prompt in direct response to a tap, and a network round-trip in between
 * is enough to lose that. The server is asked for its key after, and the
 * subscription the browser produces is handed to it. Nothing is stored on the
 * phone beyond what the browser keeps itself.
 */
async function enablePush() {
  const permission = await Notification.requestPermission();
  if (permission !== 'granted') {
    toast(permission === 'denied'
      ? 'Notifications are blocked for ilan in Settings' : 'Notifications were not enabled', true);
    return false;
  }
  const { ok, data } = await api.get('/push');
  if (!ok || !data.public_key) {
    toast('The server does not support notifications yet — update and restart it', true);
    return false;
  }
  try {
    const registration = await navigator.serviceWorker.ready;
    const sub = await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(data.public_key),
    });
    if (!await act('/push/subscribe', sub.toJSON(), 'Notifications on')) return false;
    return true;
  } catch (err) {
    toast(`Could not subscribe: ${err && err.message ? err.message : err}`, true);
    return false;
  }
}

/** Turn notifications off for this device: forget it on the server, then in
 *  the browser. Server first, so a device the server no longer knows cannot
 *  be left holding a live subscription that nothing will ever use. */
async function disablePush() {
  try {
    const registration = await navigator.serviceWorker.ready;
    const sub = await registration.pushManager.getSubscription();
    if (!sub) return true;
    await api.post('/push/unsubscribe', { endpoint: sub.endpoint });
    await sub.unsubscribe();
    toast('Notifications off');
    return true;
  } catch (err) {
    toast(`Could not unsubscribe: ${err && err.message ? err.message : err}`, true);
    return false;
  }
}

/** The number on the app icon: tasks waiting on you. Cleared when none are.
 *  A no-op wherever the badge API is missing. */
function updateBadge() {
  if (typeof navigator === 'undefined' || typeof navigator.setAppBadge !== 'function') return;
  const waiting = state.tasks.filter(
    (t) => t.needs_review && !TERMINAL_STATUSES.has(t.status)).length;
  const result = waiting
    ? navigator.setAppBadge(waiting)
    : (typeof navigator.clearAppBadge === 'function' ? navigator.clearAppBadge() : navigator.setAppBadge(0));
  if (result && typeof result.catch === 'function') result.catch(() => {});
}

/** The Notifications card on Settings, for the state this device is in. */
function pushCard(push) {
  const note = {
    unsupported: 'This browser cannot receive push notifications.',
    install: 'Add ilan to your Home Screen, then enable notifications from there.',
    blocked: 'Notifications are blocked for ilan. Allow them in Settings to turn them on.',
    ready: push.subscribed
      ? 'This phone is told when a task finishes.'
      : 'Be told when a task finishes: name, outcome, summary.',
  }[push.support];
  const button = push.support !== 'ready' ? ''
    : !push.serverReady
      ? '<span class="kv-v">server needs updating</span>'
      : push.subscribed
        ? '<button class="btn" id="push-off">Disable</button>'
        : '<button class="btn btn-primary" id="push-on">Enable notifications</button>';
  const devices = push.devices === null ? ''
    : `<div class="kv"><span class="kv-k">notifications</span>
         <span class="kv-v">${push.devices} device${push.devices === 1 ? '' : 's'}</span></div>`;
  return `
      <div class="card push-card">
        ${devices}
        <div class="kv">
          <span class="kv-note">${esc(note)}</span>
          ${button}
        </div>
      </div>`;
}

async function renderConfig() {
  const [cfg, version, push] = await Promise.all([api.get('/config'), api.get('/version'), pushState()]);
  const conf = cfg.data.config || {};

  const rows = Object.keys(conf).sort().map((key) => {
    const secret = key.includes('token') || key.includes('api-key');
    const shown = secret && conf[key] ? '••••••' : String(conf[key]);
    return `<div class="kv">
      <span class="kv-k">${esc(key)}</span>
      <span class="kv-v">
        ${esc(shown)}
        ${CONFIG_READONLY.has(key) ? ''
          : `<button class="btn btn-sm kv-edit" data-key="${esc(key)}">Edit</button>`}
      </span>
    </div>`;
  }).join('');

  const pid = version.data.pid;
  showView(`
    <header class="hdr">
      <div class="hdr-row">
        ${BACK_BUTTON}
        <h1 class="hdr-title">Settings</h1>
      </div>
      <p class="hdr-sub">ilan ${esc(version.data.version || '?')}
        · ${esc(version.data.commit || '')}</p>
    </header>
    <main class="main">
      <div class="card">${rows}</div>
      <!-- The server itself, below its settings. The pid is shown because it
           is what changes on a restart, so it is the one thing on this page
           that confirms the restart actually happened. -->
      <div class="card server-card">
        <div class="kv">
          <span class="kv-k">server</span>
          <span class="kv-v">${pid ? `pid ${esc(String(pid))}` : 'running'}</span>
        </div>
        <div class="kv">
          <span class="kv-note">Agents keep running; the app reconnects.</span>
          <!-- Filled and full height, unlike the quiet Edit buttons above it:
               this is the one control on the page that does something to the
               server rather than to a setting, and it should read as such
               before the label is. -->
          <button class="btn btn-primary" id="restart-server">Restart server</button>
        </div>
      </div>${pushCard(push)}
    </main>`);

  wireBack();
  $('#restart-server').onclick = () => restartServer(pid);
  const on = $('#push-on');
  if (on) on.onclick = async () => { if (await enablePush()) renderConfig(); };
  const off = $('#push-off');
  if (off) off.onclick = async () => { if (await disablePush()) renderConfig(); };
  document.querySelectorAll('[data-key]').forEach((btn) => {
    btn.onclick = async () => {
      const key = btn.dataset.key;
      const next = await askText(key, { value: String(conf[key]) });
      if (next === null) return;
      if (await act('/config/set', { key, value: next }, `${key} updated`)) renderConfig();
    };
  });
}

// ── router ───────────────────────────────────────────────────────────

function stopPolling() {
  clearInterval(state.pollTimer);
  state.pollTimer = null;
}

/** True while the search box has focus, i.e. the user is mid-query.
 *
 * A refresh re-renders the whole header, which would take focus and the caret
 * away from someone in the middle of typing. The task list is not so
 * time-critical that it cannot wait until they are done.
 */
function isTypingSearch() {
  const box = $('#q');
  return Boolean(box && document.activeElement === box);
}

/** Whether a background refresh should run right now. */
function canAutoRefresh() {
  return !document.hidden
    && (location.hash || '#/') === '#/'
    && !isTypingSearch();
}

async function route() {
  stopPolling();
  const hash = location.hash || '#/';
  state.entering = entranceFor(state.currentHash, hash);
  state.currentHash = hash;

  if (hash.startsWith('#/t/')) {
    // Entering a conversation always starts at the tail, including when it is
    // the same task that was just left — going back to the list and in again
    // is how you ask for a fresh look at it.
    state.detailShown = 1;
    await renderDetail(decodeURIComponent(hash.slice(4)));
    return;
  }
  if (hash === '#/new') { renderNew(); return; }
  if (hash === '#/config') { await renderConfig(); return; }

  await loadList();
  // Refresh the list on a slow timer so a phone left open reflects agents
  // finishing. Paused while the tab is hidden so it costs no battery in the
  // background.
  state.pollTimer = setInterval(() => {
    if (canAutoRefresh()) loadList(false);
  }, 15000);
}

window.addEventListener('hashchange', route);
// Only the conversation renders an ask bar, so syncAskBar is a no-op on every
// other view and this listener costs nothing there.
document.addEventListener('selectionchange', syncAskBar);
document.addEventListener('visibilitychange', () => {
  if (canAutoRefresh()) loadList(false);
});

registerServiceWorker();

(async function start() {
  const { ok, data } = await api.get('/canned-messages');
  if (ok) state.canned = data;
  await route();
}());
