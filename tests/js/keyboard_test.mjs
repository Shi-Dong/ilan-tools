/* Assertions for keeping the reply bar above the on-screen keyboard.
 *
 * iOS does not shrink the page for the keyboard: it draws it over the bottom
 * of the screen and scrolls or pans to reveal the focused box. trackKeyboard
 * measures the visible region instead and hands the stylesheet --kb, the part
 * of the page the keyboard still covers, --vvh, the visible height, and a
 * kb-open class. These drive it with a stand-in visual viewport whose numbers
 * are what an iPhone reports: a 844px screen and a 336px keyboard (keys plus
 * the bar above them).
 */

import { bootApp, checker } from './harness.mjs';

const { check, report } = checker();
const app = bootApp();

function viewport(height) {
  const on = {};
  return {
    height, offsetTop: 0, scale: 1,
    addEventListener: (type, fn) => { (on[type] ||= []).push(fn); },
    fire(type) { (on[type] || []).forEach((fn) => fn()); },
    listens: (type) => (on[type] || []).length > 0,
  };
}
function page() {
  const props = {}; const classes = new Set();
  return {
    props, classes,
    style: { setProperty: (k, v) => { props[k] = v; } },
    classList: { toggle: (c, on) => { if (on) classes.add(c); else classes.delete(c); } },
  };
}

const SCREEN = 844; const KEYBOARD = 336;
const win = { innerHeight: SCREEN };
const vv = viewport(SCREEN);
const root = page();
const update = app.trackKeyboard(vv, win, root);

// ── no keyboard ─────────────────────────────────────────────────────────
check('it returns its update function', typeof update === 'function');
check('it listens for the visible region resizing', vv.listens('resize'));
check('and for it being panned', vv.listens('scroll'));
check('with no keyboard nothing is covered', root.props['--kb'] === '0px', root.props['--kb']);
check('and the keyboard is not open', !root.classes.has('kb-open'));
check('the visible height is the screen', root.props['--vvh'] === `${SCREEN}px`, root.props['--vvh']);

// ── the keyboard opens and iOS scrolls the page instead of panning ────────
// This is the case that covered the box: the visible region shrinks and does
// not move, so the whole keyboard sits over the bottom of the page.
vv.height = SCREEN - KEYBOARD; vv.fire('resize');
check('the bar is lifted by the whole keyboard', root.props['--kb'] === `${KEYBOARD}px`, root.props['--kb']);
check('the keyboard is open', root.classes.has('kb-open'));
check('the visible height shrinks with it', root.props['--vvh'] === `${SCREEN - KEYBOARD}px`, root.props['--vvh']);

// ── iOS pans the window part of the way ─────────────────────────────────
vv.offsetTop = 120; vv.fire('scroll');
check('a pan lifts the bar only by what is still covered',
  root.props['--kb'] === `${KEYBOARD - 120}px`, root.props['--kb']);

// ── iOS pans all the way ────────────────────────────────────────────────
// Nothing of the page is covered, so the bar needs no lift — but the keyboard
// is still up, so the home-indicator padding must still go.
vv.offsetTop = KEYBOARD; vv.fire('scroll');
check('fully panned, the bar needs no lift', root.props['--kb'] === '0px', root.props['--kb']);
check('but the keyboard still counts as open', root.classes.has('kb-open'));

// ── pinch-zoom is not a keyboard ────────────────────────────────────────
vv.offsetTop = 0; vv.height = SCREEN / 2; vv.scale = 2; vv.fire('resize');
check('zooming in does not lift the bar', root.props['--kb'] === '0px', root.props['--kb']);
check('nor count as the keyboard', !root.classes.has('kb-open'));

// ── a scrollbar's worth of difference is not a keyboard ────────────────
vv.scale = 1; vv.height = SCREEN - 15; vv.fire('resize');
check('a 15px difference does not lift the bar', root.props['--kb'] === '0px', root.props['--kb']);
check('nor count as the keyboard', !root.classes.has('kb-open'));

// ── the keyboard closes ─────────────────────────────────────────────────
vv.height = SCREEN - KEYBOARD; vv.fire('resize');
vv.height = SCREEN; vv.fire('resize');
check('closing the keyboard puts the bar back', root.props['--kb'] === '0px', root.props['--kb']);
check('and clears the class', !root.classes.has('kb-open'));

// ── no visual viewport at all ───────────────────────────────────────────
// An old browser, or the harness's own window: nothing to track, no throw.
check('with no visual viewport it does nothing', app.trackKeyboard(undefined, win, page()) === undefined);

report('keyboard');
