/* OpenWings motion for the Lichen look (theme key ow-sage).

   The look's CSS (looks/sage.css) holds every hover, press, window pop and
   the page-load entrance. This file adds only what CSS cannot do on its own:

     1. the entrance gate: the welcome entrance plays once per page load
        (.chat-container.ow-sg-first, removed once it has played), never on
        New Chat;
     2. new turns: a message rises in only when it is really new, never
        when a chat is opened, switched or re-rendered;
     3. view changes: tab and section switches inside windows and Settings
        crossfade with an 8px slide (forward from the right, back from the
        left); the composer slides to its place when the welcome turns into
        a chat; the opened chat's title travels from its sidebar row to the
        top bar; a window morphs into the tile it snaps to; a list re-flows
        smoothly when one of its cards opens or closes in place;
     4. count up: short numbers in windows and the chat's top bar count up
        once per page load when they first come into view;
     5. line drawing: the context ring and small empty-state icons draw
        their strokes once;
     6. a staggered entrance when cards arrive after their window opened.

   Rules it keeps (from the brief and the motion references):
   - It does nothing unless <html data-ow-skin="ow-sage">. Its observers
     run only while the look is on; they start when the look is picked at
     any time and stop at once when it changes.
   - Content is visible without it. It only animates from a start state to
     the state already on screen, with the Web Animations API. It never
     writes styles or classes on app elements, except the one gate class
     above.
   - Transform and opacity only (plus the individual translate and scale
     properties). The documented exceptions: stroke dashes on small SVGs
     (the line drawing the brief asks for) and the count-up text itself.
   - Text is never changed for good: a count up ends on the exact original
     text and stops at once if the app changes that text.
   - It never blocks, delays or re-dispatches the app's own events, and it
     never reads layout inside the app's own task: observers only collect,
     and every measurement runs in one requestAnimationFrame pass (all
     reads, then all writes) before the frame is painted.
   - Nothing runs for keyboard-started changes (except the composer slide,
     which happens once per chat) or for history renders.
   - prefers-reduced-motion: no movement, no count up, no drawing; new
     turns and view changes keep a short fade.
   - Every callback catches its own errors.

   Same-document View Transitions (document.startViewTransition) need the
   DOM change to happen inside their callback. The app makes its changes in
   its own handlers, so wrapping them would mean delaying the app's logic;
   this file uses FLIP (first, last, invert, play) on transform instead and
   only reads the feature. */
(function () {
  'use strict';
  if (window.__owSageMotion) return;
  window.__owSageMotion = true;

  var SKIN = 'ow-sage';
  var root = document.documentElement;
  var hasWAAPI = typeof Element !== 'undefined' && typeof Element.prototype.animate === 'function';
  var hasIO = typeof window.IntersectionObserver === 'function';
  var hasMO = typeof window.MutationObserver === 'function';
  var hasVT = typeof document.startViewTransition === 'function'; // read only, see the note above
  if (!hasWAAPI || !hasMO || typeof requestAnimationFrame !== 'function') return;

  function active() { return root.getAttribute('data-ow-skin') === SKIN; }
  // test hook: set window.__sgDebug = [] before this file loads to see what it does
  function debug(what, el) {
    try { if (window.__sgDebug && window.__sgDebug.push) window.__sgDebug.push(what + ' ' + (el && el.id ? '#' + el.id : (el && el.className && typeof el.className === 'string' ? '.' + el.className.split(' ')[0] : ''))); } catch (e) {}
  }
  // read once, in the first frame callback (see startAll), then kept
  // current by the change event: evaluating a media query can make the
  // browser bring style up to date, which must never happen inside the
  // app's own task or while the page is starting
  var rmq = null, isReduced = null;
  function reduced() {
    if (isReduced !== null) return isReduced;
    try { rmq = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null; } catch (e) { rmq = null; }
    isReduced = !!(rmq && rmq.matches);
    try {
      if (rmq) {
        var onRM = function (e) { isReduced = !!(e && e.matches); tok = null; };
        if (rmq.addEventListener) rmq.addEventListener('change', onRM); else if (rmq.addListener) rmq.addListener(onRM);
      }
    } catch (e) {}
    return isReduced;
  }
  function guard(fn) {
    return function () {
      try { if (active()) return fn.apply(this, arguments); } catch (e) { /* motion must never break the app */ }
    };
  }

  /* ---------------------------------------------------------------
     tokens: read once per activation (and per reduced-motion change),
     never inside the app's own tasks
     --------------------------------------------------------------- */
  var TOKENS = ['--duration-fast', '--duration-normal', '--duration-modal', '--duration-slow', '--duration-draw',
    '--duration-count', '--shift-view', '--rise-support', '--rise-item', '--ease-out', '--ease-draw'];
  var tok = null;
  function readTokens() {
    var t = {};
    try { var cs = getComputedStyle(root); for (var i = 0; i < TOKENS.length; i++) t[TOKENS[i]] = cs.getPropertyValue(TOKENS[i]).trim(); } catch (e) {}
    tok = t;
  }
  function token(name, fallback) { if (!tok) readTokens(); return (tok && tok[name]) || fallback; }
  function ms(name, fallback) {
    var v = token(name, '');
    var n = parseFloat(v);
    if (!isFinite(n)) return fallback;
    return /ms$/.test(v) ? n : (/s$/.test(v) ? n * 1000 : n);
  }
  function px(name, fallback) { var n = parseFloat(token(name, '')); return isFinite(n) ? n : fallback; }
  var EASE_OUT = 'cubic-bezier(0.23, 1, 0.32, 1)';
  var EASE_DRAW = 'cubic-bezier(0.645, 0.045, 0.355, 1)';
  function easeOut() { return token('--ease-out', EASE_OUT); }

  /* ---------------------------------------------------------------
     one frame pass: every read first, then every write
     --------------------------------------------------------------- */
  var frameQ = [];
  var frameReq = 0;
  function onFrame(read) {
    frameQ.push(read);
    if (frameReq) return;
    frameReq = requestAnimationFrame(function () {
      frameReq = 0;
      var q = frameQ; frameQ = [];
      if (!active()) return;
      var writes = [];
      for (var i = 0; i < q.length; i++) {
        try { var w = q[i](); if (typeof w === 'function') writes.push(w); } catch (e) {}
      }
      for (var j = 0; j < writes.length; j++) { try { writes[j](); } catch (e) {} }
    });
  }

  /* every animation this file starts, so a re-trigger or a look change can
     cancel them instead of stacking */
  var running = new Set();
  var ownAnim = new WeakMap(); // element -> its animation from this file (no expandos on app nodes)
  function play(el, frames, opts) {
    if (!el || document.hidden) return null;
    var prev = ownAnim.get(el);
    if (prev) { try { prev.cancel(); } catch (e) {} }
    var a;
    try { a = el.animate(frames, opts); }
    catch (e) {
      // an easing this browser cannot parse (linear()): fall back
      try { opts.easing = EASE_OUT; a = el.animate(frames, opts); } catch (e2) { return null; }
    }
    ownAnim.set(el, a);
    running.add(a);
    var done = function () { running.delete(a); if (ownAnim.get(el) === a) ownAnim.delete(el); };
    a.onfinish = done; a.oncancel = done;
    return a;
  }
  function cancelAll() { running.forEach(function (a) { try { a.cancel(); } catch (e) {} }); running.clear(); }

  /* last input: keyboard-started changes get no animation */
  var lastInput = { type: 'none', t: 0 };
  function noteInput(type) { return function () { lastInput = { type: type, t: Date.now() }; }; }
  document.addEventListener('keydown', noteInput('key'), true);
  document.addEventListener('pointerdown', noteInput('pointer'), true);
  function byPointer(within) { return lastInput.type === 'pointer' && Date.now() - lastInput.t < (within || 1200); }

  // layout reads: only from event handlers or the frame pass, never from an observer
  function visible(el) {
    if (!el || !el.isConnected) return false;
    var r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return false;
    var cs = getComputedStyle(el);
    return cs.display !== 'none' && cs.visibility !== 'hidden';
  }
  function inView(el) {
    if (!visible(el)) return false;
    var r = el.getBoundingClientRect();
    return r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth;
  }
  // a class-only check that needs no layout: is the window around el open?
  function hostOpen(el) {
    if (!el || !el.isConnected) return false;
    var m = el.closest('.modal');
    return !(m && (m.classList.contains('hidden') || m.style.display === 'none'));
  }
  function onReady(fn) {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', fn, { once: true });
    else fn();
  }

  /* ---------------------------------------------------------------
     1. the entrance gate
     --------------------------------------------------------------- */
  var FIRST = 'ow-sg-first';
  function setupEntrance() {
    var body = document.body;
    var container = document.querySelector('.chat-container');
    // the look turned on after start-up, or there is no welcome: no entrance
    if (!body || !container || !active()) return;
    // on the chat area, not on body: removing it later then touches only
    // that subtree's style
    container.classList.add(FIRST);
    var done = false, timer = 0, seenOn = false, seenReady = false;
    var mo = new MutationObserver(function () { try { check(); } catch (e) { end(); } });
    function end() {
      if (done) return;
      done = true; clearTimeout(timer); mo.disconnect();
      document.removeEventListener('animationend', onEnd, true);
      container.classList.remove(FIRST);
      debug('entered', container);
    }
    // the rule under the title is the last thing to finish
    function onEnd(e) {
      // out of the frame that delivers the event: the class change then
      // costs one ordinary style pass in a later frame
      if (e.animationName === 'sg-rule-draw' && e.target && e.target.classList && e.target.classList.contains('welcome-sub')) setTimeout(end, 0);
    }
    function check() {
      var on = container.classList.contains('welcome-active');
      var ready = body.classList.contains('welcome-ready');
      if (on) seenOn = true;
      // the person left the welcome, or the app opened straight on a chat
      if ((seenOn && !on) || (ready && !on)) { end(); return; }
      if (on && ready && !seenReady) { seenReady = true; timer = setTimeout(end, reduced() ? 700 : 1600); }
    }
    mo.observe(body, { attributes: true, attributeFilter: ['class'] });
    mo.observe(container, { attributes: true, attributeFilter: ['class'] });
    document.addEventListener('animationend', onEnd, true);
    check();
    // whatever happens, nothing plays later in this page load
    setTimeout(end, 10000);
  }

  /* ---------------------------------------------------------------
     2. new turns rise in; history renders never do
     --------------------------------------------------------------- */
  function setupTurns() {
    var hist = document.getElementById('chat-history');
    if (!hist) return null;
    var mo = new MutationObserver(guard(function (muts) {
      // the app marks a history render with .no-animate (added, rendered,
      // removed); the old value shows it even when it is already gone
      var rendering = hist.classList.contains('no-animate'), replaced = false, added = [];
      for (var i = 0; i < muts.length; i++) {
        var m = muts[i];
        if (m.type === 'attributes') { if ((m.oldValue || '').indexOf('no-animate') >= 0) rendering = true; continue; }
        for (var r = 0; r < m.removedNodes.length; r++) { var rn = m.removedNodes[r]; if (rn.nodeType === 1 && rn.classList && rn.classList.contains('msg')) replaced = true; }
        for (var a = 0; a < m.addedNodes.length; a++) { var an = m.addedNodes[a]; if (an.nodeType === 1 && an.classList && an.classList.contains('msg')) added.push(an); }
      }
      // three or more at once is a history render; a removal is a re-render
      if (rendering || replaced || !added.length || added.length > 2) return;
      var rm = reduced();
      var rise = rm ? 0 : px('--rise-item', 4);
      added.forEach(function (el, k) {
        debug('turn', el);
        play(el, [
          { opacity: 0, transform: 'translateY(' + rise + 'px)' },
          { opacity: 1, transform: 'none' }
        ], { duration: rm ? ms('--duration-fast', 150) : ms('--duration-normal', 200), delay: k * 60, easing: easeOut(), fill: 'backwards' });
      });
    }));
    return {
      start: function () { mo.observe(hist, { childList: true, attributes: true, attributeFilter: ['class'], attributeOldValue: true }); },
      stop: function () { mo.disconnect(); }
    };
  }

  /* ---------------------------------------------------------------
     3a. view changes inside windows: crossfade with an 8px slide
     --------------------------------------------------------------- */
  var TAB_SEL = '.settings-nav-item, .memory-tab, .gallery-tab, .cookbook-tab, .lib-tab, .admin-tab, .preset-tab, ' +
    '.compare-mode-tab, .theme-tab, [class*="theme-tab-btn"], .research-tab, .doc-tab, .settings-system-logs-tab';
  var PANEL_SEL = '.settings-panels > div, .memory-tab-panel, .research-tab-panel, .theme-tab-panel, .preset-tab-content, ' +
    '.gallery-drafts-view, [role="tabpanel"]';
  var SCOPE_SEL = '.modal-content, .settings-modal-content, .memory-modal-content, .doclib-modal-content, .gallery-modal-content, .preset-modal-content, .modal';
  var pendingSwap = null;
  var swapUntil = 0; // cards that arrive with a tab switch do not also stagger
  function panelsIn(scope) {
    var out = [];
    scope.querySelectorAll(PANEL_SEL).forEach(function (p) { if (visible(p)) out.push(p); });
    return out;
  }
  function tabIndex(tab) {
    var list = tab.parentElement ? tab.parentElement.querySelectorAll(TAB_SEL) : [];
    return Array.prototype.indexOf.call(list, tab);
  }
  function outermost(list) {
    return list.filter(function (el) {
      for (var i = 0; i < list.length; i++) if (list[i] !== el && list[i].contains(el)) return false;
      return true;
    });
  }
  document.addEventListener('pointerdown', guard(function (e) {
    if (pendingSwap && pendingSwap.mo) pendingSwap.mo.disconnect();
    pendingSwap = null;
    var tab = e.target && e.target.closest ? e.target.closest(TAB_SEL) : null;
    if (!tab || tab.classList.contains('active')) return;
    var scope = tab.closest(SCOPE_SEL) || document.body;
    var current = tab.parentElement ? tab.parentElement.querySelector('.active') : null;
    // what the app changes in this window during the click, collected only:
    // containers whose children were replaced, and elements that were
    // hidden before (a state toggle on a button is not a view change)
    var changed = [];
    var HID = /(^|\s)hidden(\s|$)|display:\s*none/;
    var mo = new MutationObserver(function (muts) {
      for (var i = 0; i < muts.length; i++) {
        var m = muts[i];
        if (m.type === 'childList') { if (m.addedNodes.length) changed.push(m.target); }
        else if (m.attributeName === 'hidden' ? m.oldValue !== null : HID.test(m.oldValue || '')) changed.push(m.target);
      }
    });
    mo.observe(scope, { childList: true, subtree: true, attributes: true, attributeFilter: ['class', 'style', 'hidden'], attributeOldValue: true });
    pendingSwap = {
      scope: scope, bar: tab.parentElement, mo: mo, changed: changed,
      before: new Set(panelsIn(scope)), from: current ? tabIndex(current) : -1, to: tabIndex(tab), t: Date.now()
    };
    setTimeout(function () { mo.disconnect(); }, 1500);
  }), true);
  document.addEventListener('click', guard(function (e) {
    var swap = pendingSwap;
    pendingSwap = null;
    if (!swap || Date.now() - swap.t > 1500) { if (swap) swap.mo.disconnect(); return; }
    var tab = e.target && e.target.closest ? e.target.closest(TAB_SEL) : null;
    if (!tab || !byPointer()) { swap.mo.disconnect(); return; }
    swapUntil = Date.now() + 700;
    // let the app's own handlers (and one frame of its work) run first
    requestAnimationFrame(function () { onFrame(function () {
      swap.mo.disconnect();
      var now = panelsIn(swap.scope).filter(function (p) {
        if (swap.before.has(p)) return false;
        // only the outermost newly shown panel animates (one entrance per container)
        for (var n = p.parentElement; n && n !== swap.scope; n = n.parentElement) if (!swap.before.has(n) && n.matches && n.matches(PANEL_SEL) && visible(n)) return false;
        return true;
      });
      if (!now.length) {
        // no panel switched: the app re-rendered or revealed content below
        // the tab bar (Tasks, Cookbook, Compare, Files)
        var bar = swap.bar, seen = new Set(), cand = [];
        swap.changed.forEach(function (t) {
          var el = t.nodeType === 1 ? t : t.parentElement;
          if (!el || seen.has(el) || el === swap.scope || !swap.scope.contains(el)) return;
          seen.add(el);
          if (bar && (bar.contains(el) || el.contains(bar))) return;
          if (el.matches && el.matches(TAB_SEL)) return;
          if (visible(el)) cand.push(el);
        });
        now = outermost(cand);
      }
      if (!now.length || now.length > 3) return;
      var dir = swap.to >= swap.from ? 1 : -1;
      var shift = reduced() ? 0 : px('--shift-view', 8);
      var dur = ms('--duration-normal', 200);
      return function () {
        now.forEach(function (p) {
          debug('swap', p);
          play(p, [
            { opacity: 0, transform: 'translateX(' + (dir * shift) + 'px)' },
            { opacity: 1, transform: 'none' }
          ], { duration: dur, easing: easeOut() });
        });
      };
    }); });
  }), true);

  /* ---------------------------------------------------------------
     3b. the welcome turns into a chat: the composer slides to its place,
     whether the message was sent with Enter or the Send button
     --------------------------------------------------------------- */
  function setupComposer() {
    var container = document.querySelector('.chat-container');
    var bar = document.querySelector('.chat-input-bar');
    if (!container || !bar) return null;
    var first = null, firstT = 0;
    function note() {
      if (!container.classList.contains('welcome-active')) return;
      first = bar.getBoundingClientRect(); firstT = Date.now();
    }
    document.addEventListener('pointerdown', guard(function (e) {
      if (e.target && e.target.closest && e.target.closest('.send-btn')) note();
    }), true);
    document.addEventListener('keydown', guard(function (e) {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && e.target && e.target.id === 'message') note();
    }), true);
    var mo = new MutationObserver(guard(function () {
      if (!first || container.classList.contains('welcome-active')) return;
      var f = first; first = null;
      if (reduced() || Date.now() - firstT > 4000) return;
      onFrame(function () {
        var last = bar.getBoundingClientRect();
        var dy = f.top - last.top;
        if (Math.abs(dy) < 4) return;
        var dur = ms('--duration-slow', 300);
        return function () {
          debug('composer', bar);
          play(bar, [{ transform: 'translateY(' + dy + 'px)' }, { transform: 'none' }], { duration: dur, easing: easeOut() });
        };
      });
    }));
    return {
      start: function () { mo.observe(container, { attributes: true, attributeFilter: ['class'] }); },
      stop: function () { mo.disconnect(); first = null; }
    };
  }

  /* ---------------------------------------------------------------
     3c. the opened chat's title travels from its sidebar row to the top bar
     --------------------------------------------------------------- */
  function setupTitleMorph() {
    var from = null, fromT = 0;
    document.addEventListener('pointerdown', guard(function (e) {
      from = null;
      var t = e.target;
      if (!t || !t.closest) return;
      var row = t.closest('.sidebar .session-item');
      if (!row || t.closest('button, input, .session-star, .item-drag-handle, .session-menu-btn')) return;
      var label = row.querySelector('.grow') || row;
      from = label.getBoundingClientRect(); fromT = Date.now();
    }), true);
    var meta = null;
    var mo = new MutationObserver(guard(function () {
      if (!from || Date.now() - fromT > 2000 || !byPointer(2000) || reduced()) { from = null; return; }
      var f = from; from = null;
      onFrame(function () {
        if (!inView(meta)) return;
        var last = meta.getBoundingClientRect();
        var dx = f.left - last.left, dy = (f.top + f.height / 2) - (last.top + last.height / 2);
        if (Math.abs(dx) < 8 && Math.abs(dy) < 8) return;
        var dur = ms('--duration-slow', 300);
        return function () {
          debug('title', meta);
          play(meta, [
            { transform: 'translate(' + dx + 'px, ' + dy + 'px)', opacity: 0.5 },
            { transform: 'none', opacity: 1 }
          ], { duration: dur, easing: easeOut() });
        };
      });
    }));
    return {
      start: function () {
        meta = document.getElementById('current-meta');
        if (meta) mo.observe(meta, { childList: true, characterData: true, subtree: true });
      },
      stop: function () { mo.disconnect(); from = null; }
    };
  }

  /* ---------------------------------------------------------------
     3d. a window morphs into the tile it snaps to (drag to an edge, or
     double-click its title bar). The app jumps it there; this plays the
     change on translate and scale from where it was.
     --------------------------------------------------------------- */
  var WIN_SEL = '.modal-content, .research-pane';
  var drag = null;
  function snapWatch(content, from) {
    var mo = new MutationObserver(function () {
      try {
        mo.disconnect(); clearTimeout(stop);
        if (!active() || reduced()) return;
        onFrame(function () {
          if (!visible(content)) return;
          var last = content.getBoundingClientRect();
          // a drop that only moved the window is not a snap
          if (Math.abs(last.width - from.width) < 8 && Math.abs(last.height - from.height) < 8) return;
          var sx = from.width / last.width, sy = from.height / last.height;
          var dx = from.left - last.left, dy = from.top - last.top;
          var dur = ms('--duration-modal', 250);
          return function () {
            debug('snap', content);
            play(content, [
              { transformOrigin: '0 0', translate: dx + 'px ' + dy + 'px', scale: sx + ' ' + sy },
              { transformOrigin: '0 0', translate: '0px 0px', scale: '1 1' }
            ], { duration: dur, easing: easeOut() });
          };
        });
      } catch (e) {}
    });
    mo.observe(content, { attributes: true, attributeFilter: ['style', 'class'] });
    var stop = setTimeout(function () { mo.disconnect(); }, 700);
  }
  document.addEventListener('pointerdown', guard(function (e) {
    drag = null;
    var t = e.target;
    if (!t || !t.closest || !t.closest('.modal-header') || t.closest('button, input, select, textarea, a')) return;
    var content = t.closest(WIN_SEL);
    if (content) drag = { content: content, x: e.clientX, y: e.clientY };
  }), true);
  window.addEventListener('pointerup', guard(function (e) {
    var d = drag; drag = null;
    if (!d || Math.abs(e.clientX - d.x) + Math.abs(e.clientY - d.y) < 6) return;
    // capture on window runs before the app's own release handler snaps it
    snapWatch(d.content, d.content.getBoundingClientRect());
  }), true);
  window.addEventListener('dblclick', guard(function (e) {
    var t = e.target;
    if (!t || !t.closest || !t.closest('.modal-header') || t.closest('button, input, select, textarea, a')) return;
    var content = t.closest(WIN_SEL);
    if (content) snapWatch(content, content.getBoundingClientRect());
  }), true);

  /* ---------------------------------------------------------------
     3e. a card opens or closes in place: the rest of the list re-flows
     on transform instead of jumping
     --------------------------------------------------------------- */
  var CARD_SEL = '.doclib-card, .note-card, .skill-card, .task-card, .research-job-card, .gallery-card, .memory-item, .email-card';
  document.addEventListener('pointerdown', guard(function (e) {
    var t = e.target;
    if (reduced() || !t || !t.closest) return;
    var card = t.closest(CARD_SEL);
    if (!card || !card.parentElement || !card.closest('.modal, .notes-pane')) return;
    var sibs = Array.prototype.slice.call(card.parentElement.children, 0, 40);
    var before = new Map();
    sibs.forEach(function (s) { before.set(s, s.getBoundingClientRect().top); });
    var mo = new MutationObserver(function () {
      try {
        mo.disconnect(); clearTimeout(stop);
        if (!active() || !byPointer(1500)) return;
        onFrame(function () {
          var moves = [];
          before.forEach(function (top, s) {
            if (s === card || !s.isConnected) return;
            var dy = top - s.getBoundingClientRect().top;
            if (Math.abs(dy) > 2 && Math.abs(dy) < innerHeight) moves.push([s, dy]);
          });
          if (!moves.length) return;
          var dur = ms('--duration-modal', 250);
          return function () {
            debug('reflow ' + moves.length, card);
            moves.forEach(function (m) { play(m[0], [{ transform: 'translateY(' + m[1] + 'px)' }, { transform: 'none' }], { duration: dur, easing: easeOut() }); });
          };
        });
      } catch (err) {}
    });
    mo.observe(card, { attributes: true, attributeFilter: ['class'], childList: true, subtree: true });
    var stop = setTimeout(function () { mo.disconnect(); }, 1000);
  }), true);

  /* ---------------------------------------------------------------
     4. count up, once per number per page load, when first in view
     --------------------------------------------------------------- */
  var COUNT_SEL = '.modal .memory-count, .modal [class*="stat-value"], .modal [class*="stat-num"], .modal [class*="-stats"] > strong, ' +
    '#current-meta-count, .chat-meta-count, .chat-context-pill .ctx-ring-pct';
  var NUM_RE = /^([^\d]*?)(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?([^\d]*)$/;
  var counted = new WeakSet();
  var countedKeys = new Set();
  var waiting = new WeakSet();
  function keyOf(el) {
    var host = el.closest('.modal, .chat-top-bar, .chat-meta-overlay');
    var cls = typeof el.className === 'string' ? el.className : '';
    var list = host ? host.querySelectorAll(el.tagName) : [];
    return (host ? (host.id || host.className) : 'page') + '|' + cls + '|' + Array.prototype.indexOf.call(list, el);
  }
  function onlyText(el) {
    var t = null;
    for (var n = el.firstChild; n; n = n.nextSibling) {
      if (n.nodeType === 3) { if (n.nodeValue.trim()) { if (t) return null; t = n; } }
      else if (n.nodeType === 1 && n.textContent.trim()) return null;
    }
    return t;
  }
  function parseCount(el) {
    var node = onlyText(el);
    var orig = node ? node.nodeValue : '';
    var m = orig.length <= 28 ? orig.match(NUM_RE) : null;
    var target = m ? parseFloat(m[2].replace(/,/g, '') + (m[3] || '')) : NaN;
    if (!m || !isFinite(target) || target < 2) return null;
    return { node: node, orig: orig, m: m, target: target };
  }
  // from an observer: no layout. A number already there is checked in the
  // frame pass; an empty one waits for the app's first value, which then
  // starts the count before that value is ever painted.
  function considerCount(el) {
    if (counted.has(el) || reduced()) return;
    if (el.closest('[aria-live], [contenteditable="true"], input, textarea, #chat-history')) { counted.add(el); return; }
    if (!parseCount(el)) { waitForValue(el); return; }
    if (!hostOpen(el)) return; // the IntersectionObserver takes it when it shows
    onFrame(function () { if (inView(el)) { if (io) io.unobserve(el); return function () { countUp(el); }; } });
  }
  function waitForValue(el) {
    if (waiting.has(el)) return;
    waiting.add(el);
    var wait = new MutationObserver(guard(function () {
      if (!parseCount(el)) return;
      wait.disconnect(); clearTimeout(stop); waiting.delete(el);
      if (hostOpen(el)) onFrame(function () { if (inView(el)) { if (io) io.unobserve(el); return function () { countUp(el); }; } });
    }));
    wait.observe(el, { childList: true, characterData: true, subtree: true });
    var stop = setTimeout(function () { wait.disconnect(); waiting.delete(el); }, 4000);
  }
  function countUp(el) {
    if (counted.has(el)) return;
    if (reduced() || document.hidden) { counted.add(el); return; }
    var c = parseCount(el);
    if (!c) return;
    counted.add(el);
    var key = keyOf(el);
    if (countedKeys.has(key)) return;
    countedKeys.add(key);
    runCount(el, c.node, c.orig, c.m, c.target);
  }
  function runCount(el, node, orig, m, target) {
    var fmtFor = function (mm) {
      var intPart = mm[2], frac = mm[3] || '';
      var decimals = frac ? frac.length - 1 : 0;
      var grouped = intPart.indexOf(',') >= 0;
      return function (v) {
        var s = v.toFixed(decimals);
        if (grouped) { var parts = s.split('.'); parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ','); s = parts.join('.'); }
        return mm[1] + s + mm[4];
      };
    };
    var fmt = fmtFor(m);
    var dur = Math.min(ms('--duration-count', 700), 800);
    if (dur <= 0) return;
    var start = performance.now();
    var last = orig, value = 0, done = false, adopted = 0;
    var finish = function () { done = true; try { watch.disconnect(); } catch (e) {} };
    // The app may render the same number again as its window opens. A
    // rewrite in the first 400ms is adopted (the count carries on from where
    // it is, on the app's fresh text, before the frame is painted); any
    // later rewrite wins and the count stops.
    var watch = new MutationObserver(function () {
      try {
        if (done) return;
        if (node.isConnected && node.nodeValue === last) return; // our own write
        var c2 = parseCount(el);
        if (adopted < 2 && performance.now() - start < 400 && c2) {
          adopted++;
          node = c2.node; orig = c2.orig; m = c2.m; target = c2.target; fmt = fmtFor(c2.m);
          last = fmt(target * value);
          node.nodeValue = last;
          return;
        }
        finish();
      } catch (e) { finish(); }
    });
    watch.observe(el, { childList: true, characterData: true, subtree: true });
    var step = function (now) {
      try {
        if (done) return;
        if (!active() || !node.isConnected || node.nodeValue !== last) {
          if (node.isConnected && node.nodeValue === last) node.nodeValue = orig;
          finish();
          return;
        }
        // a frame's timestamp can precede the start: clamp, never below zero
        var p = Math.max(0, Math.min(1, (now - start) / dur));
        value = 1 - Math.pow(1 - p, 4);
        if (p >= 1) { node.nodeValue = orig; finish(); return; }
        last = fmt(target * value);
        node.nodeValue = last;
        requestAnimationFrame(step);
      } catch (e) { try { if (node.nodeValue === last) node.nodeValue = orig; } catch (e2) {} finish(); }
    };
    last = fmt(0);
    node.nodeValue = last;
    debug('count ' + orig, el);
    requestAnimationFrame(step);
  }

  /* ---------------------------------------------------------------
     5. line drawing on small SVGs, once per page load (stroke dashes are
     the one documented exception to transform and opacity)
     --------------------------------------------------------------- */
  var drawnKeys = new Set();
  var DRAW_SEL = '.ctx-ring, .chat-context-pill, .modal [class*="empty"] svg, .modal [class*="empty-state"]';
  // runs in the frame pass: reads, then returns the writes
  function drawRead(el) {
    if (reduced() || document.hidden || !inView(el)) return null;
    var svg = el.tagName && el.tagName.toLowerCase() === 'svg' ? el : el.querySelector('svg');
    if (!svg) return null;
    var r = svg.getBoundingClientRect();
    if (r.width > 96 || r.height > 96) return null; // small SVGs only
    var ring = el.closest('.ctx-ring, .chat-context-pill');
    var key = ring ? 'ctx-ring' : 'empty|' + (el.closest('.modal') || {}).id + '|' + (typeof el.className === 'string' ? el.className : '');
    if (drawnKeys.has(key)) return null;
    var dur = ms('--duration-draw', 600);
    if (dur <= 0) return null;
    if (ring) {
      var arc = svg.querySelectorAll('circle')[1];
      var dash = arc && arc.getAttribute('stroke-dasharray');
      if (!dash) return null;
      var parts = dash.split(/[\s,]+/).map(parseFloat);
      if (parts.length < 2 || !(parts[0] > 0)) return null;
      drawnKeys.add(key);
      var total = parts[0] + parts[1];
      return function () {
        debug('draw ring', el);
        // real progress draws at a constant rate
        play(arc, [
          { strokeDasharray: '0 ' + total },
          { strokeDasharray: parts[0] + ' ' + parts[1] }
        ], { duration: dur, easing: 'linear' });
      };
    }
    var shapes = svg.querySelectorAll('path, line, polyline, polygon, circle, rect');
    if (!shapes.length || shapes.length > 12) return null;
    var strokes = [];
    shapes.forEach(function (s) {
      if (typeof s.getTotalLength !== 'function') return;
      var cs = getComputedStyle(s);
      if (cs.stroke === 'none' || cs.strokeWidth === '0px') return;
      var len;
      try { len = s.getTotalLength(); } catch (e) { return; }
      if (len >= 1) strokes.push([s, len]); // hairline dots draw nothing
    });
    if (!strokes.length) return null;
    drawnKeys.add(key);
    var easing = token('--ease-draw', EASE_DRAW);
    return function () {
      debug('draw icon', el);
      // each stroke starts a little before the previous one ends: one gesture
      strokes.forEach(function (sl, i) {
        play(sl[0], [
          { strokeDasharray: sl[1] + ' ' + sl[1], strokeDashoffset: sl[1] },
          { strokeDasharray: sl[1] + ' ' + sl[1], strokeDashoffset: 0 }
        ], { duration: dur, delay: i * dur * 0.35, easing: easing, fill: 'backwards' });
      });
    };
  }
  function considerDraw(el) {
    if (reduced()) return;
    if (!hostOpen(el)) return;
    onFrame(function () { var w = drawRead(el); if (w && io) io.unobserve(el); return w; });
  }

  /* one IntersectionObserver for count up and drawing (its entries carry
     their own geometry, so it never forces layout) */
  var io = null;
  var watched = new WeakSet();
  function makeIO() {
    if (!hasIO) return null;
    return new IntersectionObserver(guard(function (entries) {
      entries.forEach(function (en) {
        if (!en.isIntersecting) return;
        io.unobserve(en.target);
        if (en.target.matches(DRAW_SEL)) onFrame(function () { return drawRead(en.target); });
        else onFrame(function () { return function () { countUp(en.target); }; });
      });
    }), { threshold: 0.6 });
  }
  // from an observer: collect only
  function scan(rootEl) {
    if (!rootEl || !rootEl.querySelectorAll) return;
    var add = function (el, draw) {
      if (watched.has(el)) return;
      watched.add(el);
      if (io) io.observe(el);
      if (draw) considerDraw(el); else considerCount(el);
    };
    if (rootEl.matches && rootEl.matches(DRAW_SEL)) add(rootEl, true);
    else if (rootEl.matches && rootEl.matches(COUNT_SEL)) add(rootEl, false);
    rootEl.querySelectorAll(DRAW_SEL).forEach(function (el) { add(el, true); });
    rootEl.querySelectorAll(COUNT_SEL).forEach(function (el) { add(el, false); });
  }

  /* ---------------------------------------------------------------
     6. staggered entrance for cards that arrive after their window opened
     --------------------------------------------------------------- */
  var openedAt = new WeakMap();
  var batch = [];
  var batchQueued = false;
  // varied steps, not uniform; items after the sixth share its delay
  var STEPS = [0, 40, 75, 105, 130, 150];
  function flushBatch() {
    batchQueued = false;
    var items = batch.splice(0, batch.length);
    if (!items.length || items.length > 60 || reduced() || document.hidden) return;
    items = items.filter(function (el) { return inView(el); });
    if (!items.length) return;
    var lift = px('--rise-support', 8);
    var dur = ms('--duration-slow', 300);
    return function () {
      debug('stagger ' + items.length, items[0]);
      items.forEach(function (el, i) {
        play(el, [
          { opacity: 0, transform: 'translateY(' + lift + 'px)' },
          { opacity: 1, transform: 'none' }
        ], { duration: dur, delay: STEPS[Math.min(i, STEPS.length - 1)], easing: easeOut(), fill: 'backwards' });
      });
    };
  }
  function queueCards(nodes) {
    if (Date.now() < swapUntil) return;
    nodes.forEach(function (n) {
      if (n.nodeType !== 1 || !n.closest) return;
      var host = n.closest('.modal, .notes-pane');
      if (!host) return;
      var t = openedAt.get(host);
      // the first cards of a window (or of the Notes pane) arrive with it:
      // its own entrance covers them, only later arrivals stagger
      if (!t) { openedAt.set(host, Date.now()); return; }
      if (Date.now() - t < 350) return;
      if (n.matches(CARD_SEL)) batch.push(n);
      else if (n.querySelectorAll) n.querySelectorAll(CARD_SEL).forEach(function (c) { batch.push(c); });
    });
    if (batch.length && !batchQueued) { batchQueued = true; onFrame(flushBatch); }
  }

  /* ---------------------------------------------------------------
     wiring: observers run only while the look is on
     --------------------------------------------------------------- */
  var parts = [];
  var bodyMO = new MutationObserver(guard(function (muts) {
    var added = [];
    for (var i = 0; i < muts.length; i++) {
      var mu = muts[i];
      if (mu.type === 'attributes') {
        // class-only checks: a window or the Notes pane opened or closed
        var t = mu.target;
        if (!t.classList) continue;
        if (t.classList.contains('modal')) {
          if (t.classList.contains('hidden')) openedAt.delete(t);
          else if (!openedAt.has(t)) { openedAt.set(t, Date.now()); scan(t); }
        } else if (t.classList.contains('notes-pane')) {
          openedAt.set(t, Date.now());
        }
      } else if (mu.addedNodes.length) {
        // streaming answers add many nodes: the chat history is never scanned
        var tgt = mu.target;
        if (tgt.id === 'chat-history' || (tgt.closest && tgt.closest('#chat-history'))) continue;
        for (var j = 0; j < mu.addedNodes.length; j++) if (mu.addedNodes[j].nodeType === 1) added.push(mu.addedNodes[j]);
      }
    }
    if (added.length) {
      added.forEach(scan);
      queueCards(added);
    }
  }));
  bodyMO.start = function () { if (document.body) bodyMO.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ['class'] }); };
  bodyMO.stop = function () { bodyMO.disconnect(); };

  var on = false;
  function startAll() {
    if (on || !active()) return;
    on = true;
    tok = null;
    io = makeIO();
    watched = new WeakSet();
    parts.forEach(function (p) { try { p.start(); } catch (e) {} });
    // windows already open when the look came on count as opened now
    document.querySelectorAll('.modal:not(.hidden)').forEach(function (m) { openedAt.set(m, Date.now()); });
    // the first look at the page waits for a frame, where the browser
    // computes style anyway, instead of forcing it while the app starts
    requestAnimationFrame(guard(function () { reduced(); scan(document.body); }));
  }
  function stopAll() {
    if (!on) return;
    on = false;
    parts.forEach(function (p) { try { p.stop(); } catch (e) {} });
    if (io) { io.disconnect(); io = null; }
    cancelAll();
  }
  onReady(function () {
    try {
      setupEntrance();
      [bodyMO, setupTurns(), setupComposer(), setupTitleMorph()].forEach(function (p) { if (p) parts.push(p); });
      if (active()) startAll();
    } catch (e) {}
  });

  /* the look switched on or away at runtime */
  new MutationObserver(function () {
    try { if (active()) startAll(); else stopAll(); } catch (e) {}
  }).observe(root, { attributes: true, attributeFilter: ['data-ow-skin'] });
  document.addEventListener('visibilitychange', function () { try { if (document.hidden) cancelAll(); } catch (e) {} });

  // exposed for tests and the parent page; changes nothing on its own
  window.owSageMotion = { active: active, reduced: reduced, viewTransitions: hasVT, cancelAll: cancelAll };
})();
