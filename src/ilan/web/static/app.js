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

function esc(value) {
  if (value === null || value === undefined) return '';
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

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

function fmtCost(usd) {
  if (typeof usd !== 'number' || usd <= 0) return '';
  return usd >= 1 ? `$${usd.toFixed(2)}` : `$${usd.toFixed(3)}`;
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

function displayStatus(task) {
  if (task.reply_every_seconds && IN_LOOP_STATUSES.has(task.status)) {
    return 'AGENT_IN_LOOP';
  }
  return task.status;
}

// Order tasks the way attention should flow on a phone: things waiting on you
// first, terminal states last.
const GROUP_ORDER = [
  'NEEDS_ATTENTION', 'ERROR', 'AGENT_FINISHED', 'AGENT_IN_LOOP',
  'WORKING', 'DONE', 'DISCARDED',
];

// ── state ────────────────────────────────────────────────────────────

const state = {
  tasks: [],
  showAll: false,
  query: '',
  detailView: 'tail', // 'tail' | 'logs' | 'prompt'
  canned: { tap: '', cancel: '' },
  pollTimer: null,
};

// ── list view ────────────────────────────────────────────────────────

function taskRow(task) {
  const status = displayStatus(task);
  const meta = [
    `<span class="status st-${esc(status)}">${esc(status)}</span>`,
    task.engine ? `<span>${esc(task.engine)}</span>` : '',
    fmtCost(task.cost_usd) ? `<span>${esc(fmtCost(task.cost_usd))}</span>` : '',
    `<span>${esc(ago(task.status_changed_at || task.created_at))} ago</span>`,
  ].filter(Boolean).join('');

  return `
    <div class="card">
      <button class="row" data-name="${esc(task.name)}">
        <span class="row-top">
          ${task.alias ? `<span class="alias">${esc(task.alias)}</span>` : ''}
          <span class="row-name${task.needs_review ? ' unread' : ''}">${esc(task.name)}</span>
          ${task.pinned ? '<span class="pin">📌</span>' : ''}
        </span>
        ${task.summary_one_liner
          ? `<span class="row-sum">${esc(task.summary_one_liner)}</span>` : ''}
        <span class="row-meta">${meta}</span>
      </button>
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

function renderList() {
  // `search` in the CLI always searches closed tasks too, so a query implies
  // -a; without one, honour the toggle.
  const searching = Boolean(state.query);
  const visible = state.tasks
    .filter((t) => searching || state.showAll
      || !['DONE', 'DISCARDED'].includes(t.status))
    .filter((t) => matchesQuery(t, state.query));

  const groups = new Map();
  for (const task of visible) {
    const key = displayStatus(task);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(task);
  }

  let body = '';
  for (const key of GROUP_ORDER) {
    const rows = groups.get(key);
    if (!rows || !rows.length) continue;
    rows.sort((a, b) => (b.pinned === true) - (a.pinned === true)
      || String(b.status_changed_at).localeCompare(String(a.status_changed_at)));
    body += `<div class="group-label">${esc(key)} · ${rows.length}</div>`;
    body += rows.map(taskRow).join('');
  }
  if (!body) {
    body = `<div class="empty">${state.tasks.length
      ? 'No tasks match.' : 'No tasks yet.'}</div>`;
  }

  $('#app').innerHTML = `
    <header class="hdr">
      <div class="hdr-row">
        <h1 class="hdr-title">ilan</h1>
        <button class="btn btn-sm" id="toggle-all">${state.showAll ? 'Open' : 'All'}</button>
        <button class="btn btn-sm" id="go-config">⚙</button>
        <button class="btn btn-sm btn-primary" id="go-new">+</button>
      </div>
      <div class="hdr-row">
        <input class="field" id="q" type="search" placeholder="Search name, alias, status"
               value="${esc(state.query)}" autocapitalize="off" autocorrect="off"
               spellcheck="false">
      </div>
    </header>
    <main class="main">${body}</main>`;

  const q = $('#q');
  q.oninput = () => { state.query = q.value; renderList(); q.focus(); };
  $('#toggle-all').onclick = () => { state.showAll = !state.showAll; renderList(); };
  $('#go-config').onclick = () => { location.hash = '#/config'; };
  $('#go-new').onclick = () => { location.hash = '#/new'; };
  document.querySelectorAll('.row').forEach((row) => {
    row.onclick = () => { location.hash = `#/t/${encodeURIComponent(row.dataset.name)}`; };
  });
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
    entry.model, entry.effort, fmtCost(entry.cost_usd), ago(entry.timestamp),
  ].filter(Boolean).join(' · ');
  return `
    <div class="msg msg-${entry.role === 'user' ? 'user' : 'assistant'}">
      <div class="msg-role">${esc(entry.role)}</div>
      <p class="msg-body">${esc(entry.content)}</p>
      ${foot ? `<div class="msg-foot">${esc(foot)}</div>` : ''}
    </div>`;
}

async function renderDetail(name) {
  const [taskResp, bodyResp] = await Promise.all([
    api.get(`/tasks/${encodeURIComponent(name)}`),
    state.detailView === 'logs'
      ? api.get(`/tasks/${encodeURIComponent(name)}/logs`)
      : api.get(`/tasks/${encodeURIComponent(name)}/tail`),
  ]);

  if (!taskResp.ok) {
    $('#app').innerHTML = `<div class="empty">${esc(taskResp.data.error || 'Not found')}
      <br><br><button class="btn" onclick="location.hash='#/'">Back</button></div>`;
    return;
  }

  const task = taskResp.data.task;
  const status = displayStatus(task);

  let body;
  if (state.detailView === 'prompt') {
    body = `<div class="pre">${esc(task.prompt || '(no prompt)')}</div>`;
  } else {
    const entries = bodyResp.data.entries || bodyResp.data.logs || [];
    body = entries.length
      ? entries.map(messageHtml).join('')
      : `<div class="empty">${esc(bodyResp.data.warning || 'No messages yet.')}</div>`;
  }

  const sub = [
    status, task.engine, task.model, fmtCost(task.cost_usd),
    task.parent_name ? `from ${task.parent_name}` : '',
    task.sleep_seconds ? `sleeping ${task.sleep_seconds}s` : '',
    task.reply_every_seconds ? `every ${task.reply_every_seconds}s` : '',
  ].filter(Boolean).join(' · ');

  const isTerminal = ['DONE', 'DISCARDED'].includes(task.status);

  $('#app').innerHTML = `
    <header class="hdr">
      <div class="hdr-row">
        <button class="btn btn-sm btn-ghost" id="back">‹</button>
        <h1 class="hdr-title">
          ${task.alias ? `<span class="alias">${esc(task.alias)}</span> ` : ''}${esc(task.name)}
        </h1>
        <button class="btn btn-sm" id="refresh">↻</button>
        <button class="btn btn-sm" id="actions">•••</button>
      </div>
      <p class="hdr-sub st-${esc(status)}">${esc(sub)}</p>
      <div class="hdr-row">
        <div class="chips" style="margin:4px 0 0">
          <button class="btn btn-sm" data-view="tail">Tail</button>
          <button class="btn btn-sm" data-view="logs">Full log</button>
          <button class="btn btn-sm" data-view="prompt">Prompt</button>
        </div>
      </div>
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
  document.querySelectorAll('[data-view]').forEach((btn) => {
    if (btn.dataset.view === state.detailView) btn.classList.add('btn-primary');
    btn.onclick = () => { state.detailView = btn.dataset.view; renderDetail(name); };
  });

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
      `This ends the reply-every cycle (${resp.data.reply_every_seconds}s). Continue?`,
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
  const terminal = ['DONE', 'DISCARDED'].includes(task.status);

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
      toast(`Sleeping ${seconds}s`);
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
        <button class="btn btn-sm btn-ghost" id="back">‹</button>
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
        <button class="btn btn-sm btn-ghost" id="back">‹</button>
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

async function route() {
  stopPolling();
  const hash = location.hash || '#/';

  if (hash.startsWith('#/t/')) {
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
    if (!document.hidden && (location.hash || '#/') === '#/') loadList(false);
  }, 15000);
}

window.addEventListener('hashchange', route);
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && (location.hash || '#/') === '#/') loadList(false);
});

(async function start() {
  const { ok, data } = await api.get('/canned-messages');
  if (ok) state.canned = data;
  await route();
}());
