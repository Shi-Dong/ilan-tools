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

/** Parse "45", "30m", "2h", "1d" into seconds. Returns null if unparseable. */
function parseDuration(text) {
  const m = /^\s*(\d+)\s*([smhd]?)\s*$/i.exec(text || '');
  if (!m) return null;
  const n = parseInt(m[1], 10);
  const mult = { '': 1, s: 1, m: 60, h: 3600, d: 86400 }[m[2].toLowerCase()];
  return n * mult;
}

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
    const close = (value) => { backdrop.remove(); resolve(value); };
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
  DONE: { choice: 'undone', label: 'Undone This Task' },
  DISCARDED: { choice: 'undiscard', label: 'Undiscard This Task' },
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
};

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
const ICONS = { send: 'i-send', check: 'i-check', chevron: 'i-chevron' };

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
  // The FABLE tag is deliberately *not* a .meta-detail — it has to survive
  // collapsing, since which tasks are burning the expensive model is worth
  // knowing from the list most of it is only ever seen as. Whether a task
  // carries it is the server's call (see handle_list_tasks): the model ids and
  // the backend rule live in models.py, and this only reads the answer.
  const meta = [
    statusPill(task),
    task.fable ? '<span class="fable">FABLE</span>' : '',
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
        ${TERMINAL_STATUSES.has(task.status) ? '' : `
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

  $('#app').innerHTML = `
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
    <main class="main">${body}</main>`;

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
  if (state.expanded.has(name)) {
    state.expanded.delete(name);
  } else {
    state.expanded.add(name);
  }
  saveExpanded();
  renderList();
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
    $('#app').innerHTML = '<div class="empty">Loading…</div>';
  }
  const { ok, data } = await api.get('/tasks?all=true');
  if (!ok) {
    $('#app').innerHTML = `<div class="empty">Cannot reach the ilan server.<br>
      ${esc(data.error || '')}</div>`;
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
    $('#app').innerHTML = `<div class="empty">${esc(taskResp.data.error || 'Not found')}
      <br><br><button class="btn" onclick="location.hash='#/'">Back</button></div>`;
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

  $('#app').innerHTML = `
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
      <p class="hdr-sub row-meta rs-${esc(status)}">${statusPill(task)}${
        sub ? `<span class="meta-detail">${esc(sub)}</span>` : ''}</p>
      ${hasMore ? `
      <div class="hdr-row">
        <button class="btn btn-sm show-more" id="show-more">Show More</button>
      </div>` : ''}
    </header>
    <main class="main">${body}</main>
    <div class="dock">${footer}</div>`;

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
    options.push({ value: 'replyEvery', label: 'Reply every…' });
    options.push({ value: 'done', label: 'Mark done' });
    options.push({ value: 'discard', label: 'Discard' });
  } else {
    options.push({
      value: task.status === 'DONE' ? 'undone' : 'undiscard',
      label: task.status === 'DONE' ? 'Un-done' : 'Un-discard',
    });
  }
  options.push({ value: task.pinned ? 'unpin' : 'pin', label: task.pinned ? 'Unpin' : 'Pin' });
  options.push({ value: 'unread', label: 'Mark unread' });
  options.push({ value: task.model ? 'unmax' : 'max', label: task.model ? 'Unmax' : 'Max' });
  options.push({ value: 'switch-backend', label: `Switch backend (now ${task.engine || '?'})` });
  options.push({ value: 'rename', label: 'Rename…' });
  options.push({ value: 'alias', label: 'Set alias…' });
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
  'done', 'discard', 'undone', 'undiscard', 'unread',
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
      const raw = await askText('Sleep for how long?', { placeholder: '30m, 2h, 1d' });
      if (raw === null) return;
      const seconds = parseDuration(raw);
      if (!seconds) { toast('Could not read that duration', true); return; }
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

    case 'replyEvery': {
      const raw = await askText('Re-send a message every…',
        { placeholder: '20m minimum, e.g. 1h' });
      if (raw === null) return;
      const seconds = parseDuration(raw);
      if (!seconds) { toast('Could not read that duration', true); return; }
      const message = await askText('Message to re-send', { multiline: true });
      if (!message) return;
      if (await sendReply(task.name, message, { every_seconds: seconds })) back();
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

    case 'alias': {
      const next = await askText('New alias', { value: task.alias || '' });
      if (next === null) return;
      if (await act(`/tasks/${t}/alias`, { alias: next })) back();
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
  $('#app').innerHTML = `
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
    </main>`;

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

async function renderConfig() {
  const [cfg, version] = await Promise.all([api.get('/config'), api.get('/version')]);
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

  $('#app').innerHTML = `
    <header class="hdr">
      <div class="hdr-row">
        ${BACK_BUTTON}
        <h1 class="hdr-title">Settings</h1>
      </div>
      <p class="hdr-sub">ilan ${esc(version.data.version || '?')}
        · ${esc(version.data.commit || '')}</p>
    </header>
    <main class="main"><div class="card">${rows}</div></main>`;

  wireBack();
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

(async function start() {
  const { ok, data } = await api.get('/canned-messages');
  if (ok) state.canned = data;
  await route();
}());
