/* One DOM stub for every behavioural harness in this directory.
 *
 * app.js is a browser script, so a test evaluates it against the smallest set
 * of globals that lets it load, then calls its real functions. Every harness
 * needed the same stub, and each had grown its own hand-written copy — which
 * is how they drifted: three spellings of the element registry, two of the
 * fetch recorder, and a per-file list of which ids may legitimately be absent.
 *
 * Two behaviours here are load-bearing rather than incidental:
 *
 *   - fetch never settles by default. The start() IIFE at the bottom of app.js
 *     awaits it, so an unsettled promise parks the boot sequence instead of
 *     letting it run against a fake DOM. A test that wants requests answered
 *     calls setFetch().
 *
 *   - Conditionally rendered ids resolve to null when they are not in the
 *     markup. renderDetail only wires up ids it just wrote, so a stub returned
 *     for an absent control would make a missing button look present.
 */

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const STATIC = join(here, '..', '..', 'src', 'ilan', 'web', 'static');

const APP_SOURCE = readFileSync(join(STATIC, 'app.js'), 'utf8');

export const EXPANDED_KEY = 'ilan.expanded';

// Controls the app renders only in some states. Asking for one of these when
// it is not on the page has to come back null, not a stub.
const CONDITIONAL_IDS = [
  'show-more', 'reply', 'send', 'revive', 'clear-search',
  'ask-bar', 'ask-btn', 'ask-preview',
];

// querySelectorAll targets, and the data- attribute that identifies each match.
// data-toggle is on the card body; the rest are its action buttons.
const LIST_SELECTORS = {
  '.row': 'toggle',
  '.act-tap': 'tap',
  '.act-details': 'details',
  '.act-done': 'done',
};

const PRELUDE = `
  const MD = { escapeHtml: (v) => String(v ?? '')
    .replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;')
    .replaceAll('"','&quot;').replaceAll("'",'&#39;'),
    render: (v) => String(v ?? '') };

  const __store = new Map(__SEED__);
  const localStorage = __DENIED__
    ? { getItem: () => { throw new Error('denied'); },
        setItem: () => { throw new Error('denied'); },
        removeItem: () => { throw new Error('denied'); } }
    : { getItem: (k) => (__store.has(k) ? __store.get(k) : null),
        setItem: (k, v) => { __store.set(k, String(v)); },
        removeItem: (k) => { __store.delete(k); } };

  const __fetches = [];
  let __fetchImpl = () => new Promise(() => {});
  const fetch = (path, opts) => {
    __fetches.push({ path, opts });
    return __fetchImpl(path, opts);
  };
  function __setFetch(fn) { __fetchImpl = fn; }

  // Elements are stable per id, so a handler assigned on one render is still
  // reachable from the test afterwards.
  const __els = new Map();
  function __el(key) {
    if (!__els.has(key)) {
      __els.set(key, {
        id: key, value: '', innerHTML: '', hidden: true, className: '',
        textContent: '', onclick: null, oninput: null, onkeydown: null,
        disabled: false, checked: false, scrollHeight: 0,
        dataset: {}, focus() {}, classList: { add() {} }, style: {},
        setSelectionRange() {},
      });
    }
    return __els.get(key);
  }

  // A stand-in for window.getSelection(). Nodes answer closest('.msg-body')
  // according to where the test says each end of the selection landed, which
  // is the only thing the app asks them.
  let __selection = null;
  const getSelection = () => __selection;
  function __setSelection(text, anchorInside, focusInside) {
    if (!text) { __selection = null; return; }
    const node = (inside) => ({
      nodeType: 1,
      closest: (sel) => (inside && sel === '.msg-body' ? { tagName: 'DIV' } : null),
    });
    __selection = {
      isCollapsed: false,
      anchorNode: node(anchorInside),
      focusNode: node(focusInside),
      toString: () => text,
    };
  }

  const __CONDITIONAL = new Set(__CONDITIONAL_IDS__);
  const __LISTS = __LIST_SELECTORS__;

  // Stable per (selector, key), for the same reason __el is.
  const __lists = new Map();
  function __listEl(sel, key) {
    const id = sel + '|' + key;
    if (!__lists.has(id)) {
      __lists.set(id, { dataset: {}, onclick: null, classList: { add() {} } });
    }
    return __lists.get(id);
  }

  // The dialogs build detached nodes, so createElement returns something with
  // enough surface for modal() and its wire() callbacks.
  const __modalEls = new Map();
  function __modalEl(sel) {
    if (!__modalEls.has(sel)) {
      __modalEls.set(sel, { onclick: null, onkeydown: null, value: '', focus() {} });
    }
    return __modalEls.get(sel);
  }
  let __modalOpen = false;
  let __lastModal = null;

  const document = {
    hidden: false,
    activeElement: null,
    querySelector: (sel) => {
      const id = sel.replace('#', '');
      if (__CONDITIONAL.has(id) && !__el('app').innerHTML.includes('id="' + id + '"')) {
        return null;
      }
      return __el(id);
    },
    querySelectorAll: (sel) => {
      const attr = __LISTS[sel];
      if (!attr) return [];
      const html = __el('app').innerHTML;
      const re = new RegExp('data-' + attr + '="([^"]+)"', 'g');
      return [...html.matchAll(re)].map((m) => {
        const el = __listEl(sel, m[1]);
        el.dataset[attr] = m[1];
        return el;
      });
    },
    addEventListener: () => {},
    body: { appendChild: () => { __modalOpen = true; } },
    createElement: () => {
      __lastModal = {
        className: '', innerHTML: '',
        addEventListener: () => {},
        remove: () => { __modalOpen = false; },
        querySelector: (s) => __modalEl(s),
        querySelectorAll: () => [],
      };
      return __lastModal;
    },
  };
  const window = { addEventListener: () => {}, open: () => {} };
  const location = { hash: '#/' };
  const setInterval = () => 0;
  const clearInterval = () => {};
  const setTimeout = () => 0;
  const clearTimeout = () => {};
`;

const TAIL = `;return {
  state, renderList, renderDetail, renderNew, renderConfig, route,
  // The pure helpers, so a test can exercise a formatter directly instead of
  // reading it back out of rendered markup.
  ago, formatCompactDuration, formatHoursMinutes, statusLabel, sleepSuffix,
  replyEverySuffix, parseDuration, displayStatus, reviveAction, isVisible,
  isLooping, isSleeping,
  sendReply, runAction, postConfirmingReplyEvery,
  elide, quoteForReply, selectedMessageText, syncAskBar, askAboutSelection,
  el: __el,
  row: (name) => __listEl('.row', name),
  tapBtn: (name) => __listEl('.act-tap', name),
  detailsBtn: (name) => __listEl('.act-details', name),
  doneBtn: (name) => __listEl('.act-done', name),
  html: () => __el('app').innerHTML,
  modal: (sel) => __modalEl(sel),
  modalOpen: () => __modalOpen,
  modalTitle: () => {
    const m = ((__lastModal && __lastModal.innerHTML) || '')
      .match(/class="sheet-title">([^<]*)</);
    return m ? m[1] : '';
  },
  /** Pretend the user selected *text*; the flags say whether each end of
   *  the selection landed inside a message body. */
  selectText: (text, anchorInside = true, focusInside = anchorInside) =>
    __setSelection(text, anchorInside, focusInside),
  storage: __store,
  fetches: __fetches,
  setFetch: __setFetch,
  location,
};`;

/** Evaluate app.js against the stub and hand back its innards.
 *
 * `expanded` seeds the persisted set of opened cards; `storage: 'denied'`
 * makes every localStorage call throw, which is what Private Browsing and a
 * full quota look like.
 */
export function bootApp({ expanded, storage = 'ok' } = {}) {
  const seed = expanded === undefined
    ? []
    : [[EXPANDED_KEY, JSON.stringify(expanded)]];
  const prelude = PRELUDE
    .replace('__SEED__', JSON.stringify(seed))
    .replace('__DENIED__', storage === 'denied' ? 'true' : 'false')
    .replace('__CONDITIONAL_IDS__', JSON.stringify(CONDITIONAL_IDS))
    .replace('__LIST_SELECTORS__', JSON.stringify(LIST_SELECTORS));
  return new Function(`${prelude}\n${APP_SOURCE}\n${TAIL}`)();
}

/** Let renders that were started but not awaited finish.
 *
 * Some handlers re-render without returning the promise, so a click settles
 * before the new markup exists. Yielding to a macrotask drains the whole
 * microtask queue behind it — no guessing at tick counts.
 */
export const settle = () => new Promise((resolve) => setImmediate(resolve));

/** Collect assertion failures, so one run reports every regression at once. */
export function checker() {
  const failures = [];
  return {
    check(name, condition, detail = '') {
      if (!condition) {
        failures.push(`FAIL  ${name}${detail ? `\n        ${detail}` : ''}`);
      }
    },
    /** Click a dialog button, reporting rather than throwing if none is wired.
     *
     * Without this a regression that skips a confirmation entirely dies with a
     * TypeError on a null handler, which says far less than the assertion that
     * was about to run.
     */
    clickModal(app, sel, why) {
      const el = app.modal(sel);
      if (typeof el.onclick !== 'function') {
        failures.push(`FAIL  ${why}\n        no dialog was open to click ${sel}`);
        return false;
      }
      el.onclick();
      return true;
    },
    report(label) {
      if (failures.length) {
        console.log(failures.join('\n'));
        console.log(`\n${failures.length} ${label} assertion(s) FAILED`);
        process.exit(1);
      }
      console.log(`all ${label} assertions passed`);
    },
  };
}
