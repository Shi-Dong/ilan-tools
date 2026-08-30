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

/** Compact age like "4m", "3h", "2d" from an ISO timestamp. */
function ago(iso) {
  if (!iso) return '';
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return '';
  const secs = Math.max(0, (Date.now() - then) / 1000);
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

let toastTimer = null;
function toast(message, isError) {
  const el = $('#toast');
  el.textContent = message;
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

const TERMINAL_STATUSES = new Set(['DONE', 'DISCARDED']);

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
  // How many assistant messages the conversation currently reveals, and which
  // task that count belongs to — opening a different task has to start from
  // one again rather than inherit however far the last one was expanded.
  // How many assistant messages the conversation reveals. Reset by route() on
  // every navigation into a task, so re-opening one always starts at the tail;
  // renderDetail leaves it alone, since Show More, the refresh button and a
  // sent reply all re-render without meaning to collapse the view back.
  detailShown: 1,
  canned: { tap: '', cancel: '' },
  pollTimer: null,
};

// ── list view ────────────────────────────────────────────────────────

function taskRow(task) {
  const status = displayStatus(task);
  // `ls -c` shows only the pin, alias, name, unread marker and status, so the
  // engine and the age are the parts a collapsed row drops. They are tagged
  // rather than omitted so collapsing is a class on the card, not a second
  // rendering path that could drift from this one.
  const meta = [
    `<span class="status st-${esc(status)}">${esc(status)}</span>`,
    task.engine ? `<span class="meta-detail">${esc(task.engine)}</span>` : '',
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
        <button class="btn btn-sm act-tap" data-tap="${esc(task.name)}">Tap</button>
        <button class="btn btn-sm act-details"
                data-details="${esc(task.name)}">Show Details</button>
      </div>
    </div>`;
}

function matchesQuery(task, query) {
  if (!query) return true;
  const hay = [
    task.name, task.alias, task.status, task.engine, task.summary_one_liner,
    task.number,
  ].filter(Boolean).join(' ').toLowerCase();
  return hay.includes(query.toLowerCase());
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

  $('#app').innerHTML = `
    <header class="hdr">
      <div class="hdr-row">
        <h1 class="hdr-title">ilan</h1>
        <button class="btn btn-sm" id="do-refresh">Refresh</button>
        <button class="btn btn-sm" id="go-config">⚙</button>
        <button class="btn btn-sm btn-primary" id="go-new">+</button>
      </div>
      <div class="hdr-row">
        <input class="field" id="q" type="search" placeholder="Search name, alias, status"
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
}

/** Ask a task for a status update, after confirming.
 *
 * The confirmation is not ceremony: a tap posts a real message that interrupts
 * the agent, and the button now sits on every expanded card rather than behind
 * the actions sheet, so it is far easier to hit by accident.
 */
async function tapFromCard(name) {
  const ok = await askConfirm(`Ask ${name} for a status update?`, 'Tap');
  if (!ok) return;
  await sendReply(name, state.canned.tap);
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

  const sub = [
    status, task.engine, task.model,
    task.parent_name ? `from ${task.parent_name}` : '',
    sleepSuffix(task.sleep_seconds),
    replyEverySuffix(task.reply_every_seconds),
  ].filter(Boolean).join(' · ');

  const isTerminal = TERMINAL_STATUSES.has(task.status);

  $('#app').innerHTML = `
    <header class="hdr">
      <div class="hdr-row">
        <button class="btn btn-ghost btn-back" id="back" aria-label="Back">‹</button>
        <h1 class="hdr-title">
          ${task.alias ? `<span class="alias">${esc(task.alias)}</span> ` : ''}<span
            class="${engineClass(task)}${isLooping(task) ? ' looping' : ''}"
            >${esc(task.name)}</span>
        </h1>
        <button class="btn btn-sm" id="refresh">↻</button>
        <button class="btn btn-sm" id="actions">•••</button>
      </div>
      <p class="hdr-sub st-${esc(status)}">${esc(sub)}</p>
      ${hasMore ? `
      <div class="hdr-row">
        <button class="btn btn-sm show-more" id="show-more">Show More</button>
      </div>` : ''}
    </header>
    <main class="main">${body}</main>
    ${isTerminal ? '' : `
    <div class="composer">
      <textarea class="field" id="reply" rows="1" placeholder="Reply to ${esc(task.name)}"></textarea>
      <button class="btn btn-primary" id="send">Send</button>
    </div>`}`;

  $('#back').onclick = () => { location.hash = '#/'; };
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

  const replyBox = $('#reply');
  if (replyBox) {
    replyBox.oninput = () => {
      replyBox.style.height = 'auto';
      replyBox.style.height = `${Math.min(replyBox.scrollHeight, 220)}px`;
    };
    $('#send').onclick = async () => {
      const text = replyBox.value.trim();
      if (!text) return;
      $('#send').disabled = true;
      const sent = await sendReply(task.name, text);
      $('#send').disabled = false;
      if (sent) { replyBox.value = ''; renderDetail(name); }
    };
  }
}

/** Send a reply, handling the server's reply-every confirmation challenge. */
async function sendReply(name, message, extra = {}) {
  const path = `/tasks/${encodeURIComponent(name)}/reply`;
  let resp = await api.post(path, { message, ...extra });

  // 409 arrives for two different reasons; only one of them is a question.
  if (resp.status === 409 && resp.data.confirm_reply_every) {
    const proceed = await askConfirm(
      `This ends the reply-every cycle (${
        formatCompactDuration(resp.data.reply_every_seconds)}). Continue?`,
      'Send anyway',
    );
    if (!proceed) return false;
    resp = await api.post(path, { message, ...extra, override_reply_every: true });
  }
  if (!resp.ok) {
    toast(resp.data.error || 'Reply failed', true);
    return false;
  }
  toast(resp.data.message || 'Reply sent');
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

async function runAction(choice, task) {
  const t = encodeURIComponent(task.name);
  const back = () => renderDetail(task.name);
  const simple = {
    done: 'done', discard: 'discard', undone: 'undone', undiscard: 'undiscard',
    unread: 'unread', pin: 'pin', unpin: 'unpin', max: 'max', unmax: 'unmax',
    'switch-backend': 'switch-backend',
  };

  if (simple[choice]) {
    if (await act(`/tasks/${t}/${simple[choice]}`)) back();
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
      const resp = await api.post(`/tasks/${t}/sleep`, { seconds });
      if (resp.status === 409 && resp.data.confirm_reply_every) {
        if (!await askConfirm('This ends the reply-every cycle. Continue?', 'Sleep anyway')) return;
        const retry = await api.post(`/tasks/${t}/sleep`,
          { seconds, override_reply_every: true });
        if (!retry.ok) { toast(retry.data.error || 'Failed', true); return; }
      } else if (!resp.ok) {
        toast(resp.data.error || 'Failed', true);
        return;
      }
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
      toast(`Branched to ${data.name || 'new task'}`);
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
      toast(`Deleted ${task.name}`);
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
        <button class="btn btn-ghost btn-back" id="back" aria-label="Back">‹</button>
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
        <label class="kv" style="border:0;padding-left:0">
          <span class="kv-k">Run on the max model</span>
          <input type="checkbox" id="max" style="width:24px;height:24px">
        </label>
        <button class="btn btn-primary" id="create">Create task</button>
      </div>
    </main>`;

  $('#back').onclick = () => { location.hash = '#/'; };
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
    toast(`Created ${data.name || ''}`);
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
          : `<button class="btn btn-sm" data-key="${esc(key)}"
               style="margin-left:8px">Edit</button>`}
      </span>
    </div>`;
  }).join('');

  $('#app').innerHTML = `
    <header class="hdr">
      <div class="hdr-row">
        <button class="btn btn-ghost btn-back" id="back" aria-label="Back">‹</button>
        <h1 class="hdr-title">Settings</h1>
      </div>
      <p class="hdr-sub">ilan ${esc(version.data.version || '?')}
        · ${esc(version.data.commit || '')}</p>
    </header>
    <main class="main"><div class="card">${rows}</div></main>`;

  $('#back').onclick = () => { location.hash = '#/'; };
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
document.addEventListener('visibilitychange', () => {
  if (canAutoRefresh()) loadList(false);
});

(async function start() {
  const { ok, data } = await api.get('/canned-messages');
  if (ok) state.canned = data;
  await route();
}());
