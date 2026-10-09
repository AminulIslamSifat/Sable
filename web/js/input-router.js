/* ────────────────────────────────────────────────────────────────
 * input-router.js — "just start typing" for Sable.
 *
 * Problem: Sable has a lot of composers (main chat, compact chat, study
 * search / notes / humanizer, research, image-gen, prompt-gen, agent panel,
 * reader AI, plus a pile of modal dialogs). If none of them holds focus when
 * you start typing, the first keystroke just vanishes.
 *
 * Solution: a single capture-phase keydown router. On the first printable
 * character it resolves the front-most *available* composer for the current
 * surface and focuses it, so that keystroke lands where you meant it.
 *
 * Priority (front-most wins):
 *   pinned target → user override → modal/dialog → study reader (chat tab)
 *   → agent panel → full-screen views (search / knowledge / research /
 *   image / promptgen) → study mode (by active tab) → IDE compact →
 *   chat compact → main chat input
 *
 * "The selected one wins": if any composer already has focus we never steal
 * it — so the box you clicked is always the box you type into.
 *
 * Public API (window.inputRouter):
 *   .focus()             focus the resolved composer right now
 *   .target()            the element that would be focused (or null)
 *   .inspect()           { mode, surface, blocked, id, editable }
 *   .pin(selector)       force a specific composer (persisted)
 *   .unpin()             clear the pin
 *   .setPriority([...])  your own selector order (persisted)
 *   .clearPriority()
 *   .setAutoFocus(bool)  focus a composer whenever a surface opens
 *
 * Debug hotkey: Alt+Shift+I logs the current resolution.
 * ──────────────────────────────────────────────────────────────── */
(function () {
  'use strict';

  const VERSION = 1;
  const PIN_KEY = 'sable_input_router_pin';
  const PRIORITY_KEY = 'sable_input_router_priority';
  const AUTOFOCUS_KEY = 'sable_input_router_autofocus';

  // Input types we are willing to route keystrokes into.
  const EDITABLE_TYPES = new Set([
    'text', 'search', 'url', 'tel', 'email', 'password',
    'number', 'date', 'datetime-local', 'month', 'week', 'time',
  ]);

  // Any of these that is visible and on top means "a dialog owns the screen".
  const MODAL_ROOTS = [
    '.sable-dialog-overlay',
    '.sdlg-overlay',
    '.tn-edit-overlay',
    '.sn-picker-overlay',
    '.sn-overlay',
    '.study-modal-overlay',
    '.project-settings-overlay',
    '.cp-modal-overlay',
    '.cal-popup-overlay',
  ];

  // Full-screen overlay shells that aren't dialogs but still own the screen.
  // Order matters: the last visible one is the front-most.
  const OVERLAY_ROOTS = [
    '#settingsOverlay',
    '#libraryOverlay',
    '#fsOverlay',
    '#cheatSheetOverlay',
    '#skillDetailOverlay',
  ];

  // Broad selector used to find "the first field" inside a container.
  const EDITABLE_SEL = [
    'textarea:not([readonly]):not([disabled])',
    'input:not([type=hidden]):not([type=checkbox]):not([type=radio])' +
      ':not([type=file]):not([type=range]):not([type=color])' +
      ':not([type=button]):not([type=submit]):not([type=reset])' +
      ':not([readonly]):not([disabled])',
    '[contenteditable="true"]',
  ].join(', ');

  /* ────────────────────────────── predicates ────────────────────────────── */

  function mode() {
    const b = document.body && document.body.getAttribute('data-mode');
    if (b) return b;
    return document.documentElement.getAttribute('data-mode') || 'agent';
  }

  function hasClass(c) {
    return !!(document.body && document.body.classList.contains(c));
  }

  function isEditable(el) {
    if (!el || el.nodeType !== 1) return false;
    if (el.isContentEditable) return true;
    const tag = el.tagName;
    if (tag === 'TEXTAREA') return !el.disabled && !el.readOnly;
    if (tag === 'INPUT') {
      if (el.disabled || el.readOnly) return false;
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      return EDITABLE_TYPES.has(t);
    }
    return false;
  }

  // Trust layout, not class names: `body[data-mode="ide"] .chat-compact`
  // intentionally beats `.chat-compact.hidden` in the cascade.
  function isVisible(el) {
    if (!el || el.nodeType !== 1) return false;
    if (el.type === 'hidden') return false;
    const rects = el.getClientRects();
    if (!rects || rects.length === 0) return false;
    const r = rects[0];
    if (r.width < 1 || r.height < 1) return false;
    const cs = window.getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') return false;
    return true;
  }

  // Is this element genuinely the thing at its own centre, or is something
  // (a modal, a full-screen overlay) sitting on top of it?
  function isHittable(el) {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return false;
    const x = Math.min(Math.max(r.left + r.width / 2, 1), window.innerWidth - 2);
    const y = Math.min(Math.max(r.top + r.height / 2, 1), window.innerHeight - 2);
    const top = document.elementFromPoint(x, y);
    if (!top) return true; // can't tell — give it the benefit of the doubt
    return top === el || top.contains(el) || el.contains(top);
  }

  function usable(el) {
    return !!el && isEditable(el) && isVisible(el) && isHittable(el);
  }

  function vis(sel) {
    const el = document.querySelector(sel);
    return !!el && isVisible(el);
  }

  function firstMatch(root, selectors) {
    const scope = root || document;
    for (const sel of selectors) {
      let nodes;
      try { nodes = scope.querySelectorAll(sel); } catch (_) { continue; }
      for (const n of nodes) {
        if (usable(n)) return n;
      }
    }
    return null;
  }

  function visibleModals() {
    return Array.from(document.querySelectorAll(MODAL_ROOTS.join(', '))).filter(isVisible);
  }

  function visibleOverlays() {
    return Array.from(document.querySelectorAll(OVERLAY_ROOTS.join(', '))).filter(isVisible);
  }

  /**
   * Auth gate: if a login / setup phase is on screen it owns everything.
   * Without this, an early keystroke can land in the main chat input that
   * happens to sit behind the overlay.
   */
  function pickAuthGate() {
    const phases = ['#phaseAccount', '#phaseSetup'];
    for (const sel of phases) {
      const el = document.querySelector(sel);
      if (!el || !isVisible(el)) continue;
      const field = firstMatch(el, [EDITABLE_SEL]);
      if (field) return field;
    }
    return null;
  }

  function readerChatTabActive() {
    const b = document.getElementById('rdrTabChat');
    if (!b) return false;
    return b.classList.contains('active') || b.getAttribute('aria-selected') === 'true';
  }

  function activeStudyTab() {
    const btn = document.querySelector('#studyTabs .study-tab.active');
    return btn ? (btn.getAttribute('data-study-tab') || '') : '';
  }

  function pickStudy() {
    const tab = activeStudyTab();
    const byTab = {
      library:   ['.study-sidebar-search', '#studyLibSearch', '#ssInput'],
      notes:     ['#snBody', '#snTitle', '#ssInput'],
      humanizer: ['#hzInput'],
      rooms:     ['#ssInput'],
      drive:     ['#ssInput'],
      decks:     ['#ssInput'],
      cover:     ['#ssInput'],
    };
    const list = (byTab[tab] || []).concat([
      '#snBody', '#hzInput', '#studyLibSearch', '.study-sidebar-search',
      '.study-field-newtopic', '#ssInput',
    ]);
    return firstMatch(null, list);
  }

  /* ─────────────────────────────── surfaces ─────────────────────────────── */

  const SURFACES = [
    {
      id: 'reader',
      test: () => hasClass('rdr-open') && readerChatTabActive(),
      pick: () => firstMatch(null, ['[data-sa-input]']),
    },
    {
      id: 'agent-panel',
      test: () => hasClass('agent-panel-open'),
      pick: () => firstMatch(null, ['.agent-panel .ap-input', '.ap-input']),
    },
    {
      id: 'search-view',
      test: () => vis('#searchView'),
      pick: () => firstMatch(null, ['#searchInput']),
    },
    {
      id: 'knowledge',
      test: () => hasClass('knowledge-open'),
      pick: () => firstMatch(null, ['#kbSearchInput', '#kbNodeFilter', '#kbAddKey']),
    },
    {
      id: 'research',
      test: () => hasClass('research-open'),
      pick: () => firstMatch(null, ['#researchQuery']),
    },
    {
      id: 'image',
      test: () => hasClass('image-open'),
      pick: () => firstMatch(null, ['#igPrompt', '#igNegPrompt']),
    },
    {
      id: 'promptgen',
      test: () => hasClass('promptgen-open'),
      pick: () => firstMatch(null, ['#pgQuery']),
    },
    {
      id: 'study',
      test: () => mode() === 'study' && vis('#studyView'),
      pick: pickStudy,
    },
    {
      id: 'ide',
      test: () => mode() === 'ide',
      pick: () => firstMatch(null, ['#chatCompactInput']),
    },
    {
      id: 'chat',
      test: () => mode() === 'chat',
      pick: () => firstMatch(null, ['#chatCompactInput', '#input']),
    },
    {
      id: 'agent',
      test: () => true,
      pick: () => firstMatch(null, ['#input', '#chatCompactInput']),
    },
  ];

  /* ────────────────────────────── resolution ────────────────────────────── */

  function readJSON(key) {
    try { return JSON.parse(localStorage.getItem(key) || 'null'); } catch (_) { return null; }
  }

  function pinnedEl() {
    const sel = localStorage.getItem(PIN_KEY);
    if (!sel) return null;
    return firstMatch(null, [sel]);
  }

  function userPriority() {
    const list = readJSON(PRIORITY_KEY);
    return Array.isArray(list) && list.length ? list : null;
  }

  /**
   * @returns {{surface:string, el:Element|null, blocked?:boolean}}
   */
  function resolve() {
    const pin = pinnedEl();
    if (pin) return { surface: 'pin', el: pin };

    const prio = userPriority();
    if (prio) {
      const el = firstMatch(null, prio);
      if (el) return { surface: 'user', el };
    }

    // Auth gate beats everything — you can't type into the app before login.
    const gate = pickAuthGate();
    if (gate) return { surface: 'auth', el: gate };

    // A dialog owns the screen: type into it, or type nowhere.
    const modals = visibleModals();
    if (modals.length) {
      const front = modals[modals.length - 1];
      const el = firstMatch(front, [EDITABLE_SEL]);
      if (el) return { surface: 'modal', el };
      return { surface: 'modal', el: null, blocked: true };
    }

    // Full-screen overlays (settings, library, file manager, cheat sheet).
    // Don't block when they hold no field — the shell itself may be scrollable.
    const overlays = visibleOverlays();
    if (overlays.length) {
      for (let i = overlays.length - 1; i >= 0; i--) {
        const el = firstMatch(overlays[i], [EDITABLE_SEL]);
        if (el) {
          const name = overlays[i].id || 'overlay';
          return { surface: name, el };
        }
      }
      return { surface: 'overlay', el: null, blocked: true };
    }

    if (document.querySelector('.slb-overlay.slb-open')) {
      return { surface: 'lightbox', el: null, blocked: true };
    }

    for (const s of SURFACES) {
      let ok = false;
      try { ok = s.test(); } catch (_) { ok = false; }
      if (!ok) continue;
      let el = null;
      try { el = s.pick(); } catch (_) { el = null; }
      if (el) return { surface: s.id, el };
    }

    return { surface: 'none', el: null };
  }

  /* ──────────────────────────── focus + insert ──────────────────────────── */

  function focusEl(el, preventScroll) {
    try { el.focus({ preventScroll: !!preventScroll }); }
    catch (_) { try { el.focus(); } catch (__) {} }
  }

  function safeSel(el) {
    try { return [el.selectionStart, el.selectionEnd]; } catch (_) { return [null, null]; }
  }

  /**
   * Focus the target and make sure the character that triggered this lands in
   * it. Chrome/Firefox insert the character after we focus during keydown;
   * Safari sometimes doesn't, so we verify on the next task and patch it in.
   */
  function focusAndInsert(el, e) {
    const ch = e.key;
    const hadValue = typeof el.value === 'string';
    const prev = hadValue ? el.value : null;
    const [start, end] = hadValue ? safeSel(el) : [null, null];

    focusEl(el, false);
    if (!hadValue || ch.length !== 1) return; // contenteditable: browser handles it

    setTimeout(() => {
      if (document.activeElement !== el) return;
      if (el.value !== prev) return; // the browser already inserted it
      const s = start == null ? el.value.length : start;
      const t = end == null ? s : end;
      el.value = el.value.slice(0, s) + ch + el.value.slice(t);
      try { el.selectionStart = el.selectionEnd = s + ch.length; } catch (_) {}
      el.dispatchEvent(new Event('input', { bubbles: true }));
    }, 0);
  }

  /* ──────────────────────────── keystroke guard ─────────────────────────── */

  // Things that own the keyboard themselves and must never be interrupted.
  function ownsKeyboard(el, key) {
    if (!el || el === document.body || el === document.documentElement) return false;
    if (isEditable(el)) return true;          // you're already typing somewhere
    const tag = el.tagName;
    if (tag === 'SELECT') return true;        // letter-jump through options
    if (key === ' ' && (tag === 'BUTTON' || tag === 'SUMMARY')) return true;
    if (tag === 'INPUT') {
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      if (!EDITABLE_TYPES.has(t) && key === ' ') return true; // checkbox / radio
    }
    return false;
  }

  function onKeyDown(e) {
    if (e.defaultPrevented) return;
    if (e.ctrlKey || e.altKey || e.metaKey) return;
    if (e.isComposing || e.keyCode === 229) return;
    if (typeof e.key !== 'string' || e.key.length !== 1) return;
    if (document.querySelector('.shortcut-recording')) return;
    if (ownsKeyboard(document.activeElement, e.key)) return;

    const r = resolve();
    if (!r.el || r.blocked) return;
    if (r.el === document.activeElement) return;
    focusAndInsert(r.el, e);
  }

  /* ───────────────────────────── autofocus ──────────────────────────────── */

  function autoFocusEnabled() {
    return localStorage.getItem(AUTOFOCUS_KEY) !== '0';
  }

  function autoFocus() {
    if (!autoFocusEnabled()) return;
    if (document.querySelector('.shortcut-recording')) return;
    const ae = document.activeElement;
    if (ae && ae !== document.body && ae !== document.documentElement) return;
    const r = resolve();
    if (r.el) focusEl(r.el, true);
  }

  let afTimer = null;
  function scheduleAutoFocus() {
    clearTimeout(afTimer);
    afTimer = setTimeout(autoFocus, 150);
  }

  /* ──────────────────────────────── wiring ──────────────────────────────── */

  document.addEventListener('keydown', onKeyDown, true);

  document.addEventListener('keydown', function (e) {
    if (!e.altKey || !e.shiftKey) return;
    if (e.key !== 'I' && e.key !== 'i') return;
    e.preventDefault();
    e.stopPropagation();
    const info = api.inspect();
    const msg = info.surface + ' → ' + (info.id || 'nothing') + (info.blocked ? ' (blocked)' : '');
    if (typeof window.showToast === 'function') window.showToast('🎯 ' + msg, 'info');
    console.log('[input-router]', info);
  }, true);

  function startObservers() {
    const obs = new MutationObserver(scheduleAutoFocus);
    const body = document.body;
    if (body) obs.observe(body, { attributes: true, attributeFilter: ['class', 'data-mode'] });
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ['data-mode'] });
    ['#phaseAccount', '#phaseSetup', '#settingsOverlay', '#libraryOverlay'].forEach((sel) => {
      const el = document.querySelector(sel);
      if (el) obs.observe(el, { attributes: true, attributeFilter: ['class', 'style'] });
    });
  }

  function boot() {
    startObservers();
    window.addEventListener('sable-mode-changed', scheduleAutoFocus);
    setTimeout(autoFocus, 250);
    setTimeout(autoFocus, 1200);
    console.log('[input-router] v' + VERSION + ' ready');
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }

  /* ─────────────────────────────── public API ───────────────────────────── */

  const api = {
    VERSION,

    focus() {
      const r = resolve();
      if (r.el) focusEl(r.el, false);
      return r.el;
    },

    target() {
      return resolve().el;
    },

    inspect() {
      const r = resolve();
      const el = r.el;
      return {
        mode: mode(),
        surface: r.surface,
        blocked: !!r.blocked,
        id: el ? (el.id || el.className || el.tagName.toLowerCase()) : null,
        editable: el ? isEditable(el) : false,
      };
    },

    surfaces() {
      return SURFACES.map((s) => s.id);
    },

    pin(selector) {
      if (typeof selector !== 'string' || !selector.trim()) return this.unpin();
      localStorage.setItem(PIN_KEY, selector.trim());
      return this.focus();
    },

    unpin() {
      localStorage.removeItem(PIN_KEY);
      return null;
    },

    setPriority(list) {
      if (!Array.isArray(list) || !list.length) return this.clearPriority();
      localStorage.setItem(PRIORITY_KEY, JSON.stringify(list));
      return this.focus();
    },

    clearPriority() {
      localStorage.removeItem(PRIORITY_KEY);
      return null;
    },

    setAutoFocus(on) {
      localStorage.setItem(AUTOFOCUS_KEY, on ? '1' : '0');
      return !!on;
    },

    getAutoFocus() {
      return autoFocusEnabled();
    },
  };

  window.inputRouter = api;
})();
