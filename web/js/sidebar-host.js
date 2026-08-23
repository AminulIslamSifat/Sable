/**
 * sidebar-host.js — Dynamic sidebar content host & panel position manager
 *
 * Makes the left sidebar a swappable content area. Chat history is the default.
 * Panels (terminal, etc.) can be hosted in the sidebar via rail-switch events
 * or programmatic API. When a panel leaves, chat history returns automatically.
 *
 * Also provides position-switcher for panels: left / bottom / floating.
 * Acts as the single authority for panel open/close/toggle when position != bottom.
 */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const POS_KEY = 'sable.panel.pos';

  /* ── State ── */
  let currentHosted = null;   // panel id currently in sidebar (null = chat)
  let chatContent = null;     // reference to chat sections we moved out
  let sidebarSlot = null;     // the #sidebarContent wrapper
  let terminalOpen = false;   // tracks whether terminal is visibly open (any position)

  /* ── Helpers ── */
  function getPositions() {
    try { return JSON.parse(localStorage.getItem(POS_KEY)) || {}; } catch { return {}; }
  }
  function savePosition(panelId, pos) {
    const all = getPositions();
    all[panelId] = pos;
    localStorage.setItem(POS_KEY, JSON.stringify(all));
  }
  function getPosition(panelId) {
    return getPositions()[panelId] || 'bottom';
  }

  /* ── Rail button sync ── */
  function setRailActive(railTarget, active) {
    const rail = document.getElementById('activityRail');
    if (!rail) return;
    const btn = rail.querySelector(`.rail-btn[data-rail="${railTarget}"]`);
    if (btn) btn.classList.toggle('active', active);
  }

  function setTermToggleActive(active) {
    const btn = $('terminalToggle');
    if (btn) btn.classList.toggle('active', active);
  }

  /* ── Sidebar slot setup ── */
  function ensureSlot() {
    if (sidebarSlot) return sidebarSlot;
    const sidebar = document.querySelector('aside.sidebar');
    if (!sidebar) return null;

    sidebarSlot = document.createElement('div');
    sidebarSlot.id = 'sidebarContent';
    sidebarSlot.className = 'sidebar-content-slot';

    const chatsSection = sidebar.querySelector('.sidebar-chats');
    if (chatsSection) sidebarSlot.appendChild(chatsSection);

    sidebar.appendChild(sidebarSlot);
    return sidebarSlot;
  }

  /* ── Ensure sidebar shows chat (clean state) ── */
  function ensureChatVisible() {
    const slot = ensureSlot();
    if (!slot) return;
    const chats = slot.querySelector('.sidebar-chats');
    if (chats) chats.hidden = false;
    // Remove any leftover hosted panels from sidebar DOM
    const hosted = slot.querySelector('.sidebar-hosted');
    if (hosted) {
      hosted.classList.remove('sidebar-hosted');
      returnTerminalToOriginal(hosted);
    }
    currentHosted = null;
    chatContent = null;
    document.body.removeAttribute('data-sidebar-host');
  }

  /* ── Host a panel in the sidebar ── */
  function hostInSidebar(panelId) {
    const slot = ensureSlot();
    if (!slot) return;

    if (currentHosted === panelId) { unhostFromSidebar(); return; }
    if (currentHosted) unhostFromSidebar();

    const panel = $(panelId === 'terminal' ? 'terminalPanel' : panelId);
    if (!panel) return;

    // Ensure sidebar is visible (un-collapse if needed)
    document.body.classList.remove('sidebar-collapsed');

    // Hide chat
    const chatsSection = slot.querySelector('.sidebar-chats');
    if (chatsSection) { chatContent = chatsSection; chatsSection.hidden = true; }

    // Move panel in — clear all floating inline styles
    panel.classList.remove('hidden', 'floating-panel');
    panel.classList.add('sidebar-hosted');
    clearFloatStyles(panel);
    slot.appendChild(panel);
    currentHosted = panelId;

    document.body.setAttribute('data-sidebar-host', panelId);
    panel.dispatchEvent(new CustomEvent('sidebar-hosted', { detail: { position: 'left' } }));

    if (panelId === 'terminal') {
      if (window.ensureTerminalSession) window.ensureTerminalSession();
      requestAnimationFrame(() => { if (window.fitTerminalActive) window.fitTerminalActive(); });
    }
  }

  /* ── Remove panel from sidebar, restore chat ── */
  function unhostFromSidebar() {
    if (!currentHosted) return;
    const slot = ensureSlot();
    if (!slot) return;

    const panel = $(currentHosted === 'terminal' ? 'terminalPanel' : currentHosted);
    if (panel) {
      panel.classList.remove('sidebar-hosted');
      if (currentHosted === 'terminal') returnTerminalToOriginal(panel);
      panel.dispatchEvent(new CustomEvent('sidebar-unhosted'));
    }

    if (chatContent) { chatContent.hidden = false; chatContent = null; }
    currentHosted = null;
    document.body.removeAttribute('data-sidebar-host');
  }

  /* ── Return terminal to <main> before inputArea ── */
  function returnTerminalToOriginal(panel) {
    const main = document.querySelector('main.main');
    const inputArea = $('inputArea');
    if (main && inputArea) {
      main.insertBefore(panel, inputArea);
    } else if (main) {
      main.appendChild(panel);
    }
    panel.classList.add('hidden');
    panel.classList.remove('floating-panel');
  }

  /* ── Floating drag & resize state ── */
  let floatDragState = null;   // { startX, startY, origLeft, origTop }
  let floatResizeState = null;  // { startX, startY, origW, origH }
  let floatGripEl = null;

  function ensureFloatGrip(panel) {
    if (panel.querySelector('.float-resize-grip')) return;
    const grip = document.createElement('div');
    grip.className = 'float-resize-grip';
    panel.appendChild(grip);
  }

  function attachFloatHandlers(panel) {
    // ── Drag via terminal-bar ──
    const bar = panel.querySelector('.terminal-bar');
    if (!bar || bar._floatDragBound) return;
    bar._floatDragBound = true;

    bar.addEventListener('mousedown', (e) => {
      if (!panel.classList.contains('floating-panel')) return;
      // Don't drag when clicking interactive elements
      if (e.target.closest('button, select, input, .pos-switcher-menu')) return;
      e.preventDefault();
      const rect = panel.getBoundingClientRect();
      floatDragState = {
        startX: e.clientX,
        startY: e.clientY,
        origLeft: rect.left,
        origTop: rect.top,
      };
      document.body.classList.add('term-resizing');
    });

    // ── Resize via grip ──
    ensureFloatGrip(panel);
    const grip = panel.querySelector('.float-resize-grip');
    if (grip && !grip._floatResizeBound) {
      grip._floatResizeBound = true;
      grip.addEventListener('mousedown', (e) => {
        if (!panel.classList.contains('floating-panel')) return;
        e.preventDefault();
        e.stopPropagation();
        floatResizeState = {
          startX: e.clientX,
          startY: e.clientY,
          origW: panel.offsetWidth,
          origH: panel.offsetHeight,
        };
        document.body.classList.add('term-resizing');
      });
    }
  }

  // Global mousemove/mouseup for float drag & resize
  window.addEventListener('mousemove', (e) => {
    if (floatDragState) {
      const dx = e.clientX - floatDragState.startX;
      const dy = e.clientY - floatDragState.startY;
      const newLeft = floatDragState.origLeft + dx;
      const newTop = floatDragState.origTop + dy;
      const panel = $('terminalPanel');
      if (panel) {
        panel.style.left = newLeft + 'px';
        panel.style.top = newTop + 'px';
        panel.style.transform = 'none'; // remove centering transform once dragged
      }
    }
    if (floatResizeState) {
      const dw = e.clientX - floatResizeState.startX;
      const dh = e.clientY - floatResizeState.startY;
      const panel = $('terminalPanel');
      if (panel) {
        const newW = Math.max(320, floatResizeState.origW + dw);
        const newH = Math.max(180, floatResizeState.origH + dh);
        panel.style.width = newW + 'px';
        panel.style.height = newH + 'px';
      }
    }
  });

  window.addEventListener('mouseup', () => {
    const panel = $('terminalPanel');
    if (floatDragState || floatResizeState) {
      document.body.classList.remove('term-resizing');
      if (panel && panel.classList.contains('floating-panel')) {
        saveFloatGeometry(panel);
        requestAnimationFrame(() => { if (window.fitTerminalActive) window.fitTerminalActive(); });
      }
    }
    floatDragState = null;
    floatResizeState = null;
  });

  /* ── Place terminal for floating ── */
  function placeTerminalFloating(panel) {
    panel.classList.remove('sidebar-hosted', 'hidden');
    panel.classList.add('floating-panel');
    document.body.appendChild(panel);
    // Restore saved float geometry or use defaults
    const savedFloat = getFloatGeometry();
    panel.style.width = savedFloat.w + 'px';
    panel.style.height = savedFloat.h + 'px';
    if (savedFloat.x !== null) {
      panel.style.left = savedFloat.x + 'px';
      panel.style.top = savedFloat.y + 'px';
      panel.style.transform = 'none';
    } else {
      panel.style.left = '50%';
      panel.style.top = '80px';
      panel.style.transform = 'translateX(-50%)';
    }
    // Clear any inputArea push from previous bottom state
    const inputArea = $('inputArea');
    if (inputArea) inputArea.style.bottom = '';
    attachFloatHandlers(panel);
    requestAnimationFrame(() => { if (window.fitTerminalActive) window.fitTerminalActive(); });
  }

  /* ── Float geometry persistence ── */
  const FLOAT_KEY = 'sable.term.float';
  function getFloatGeometry() {
    try {
      const d = JSON.parse(localStorage.getItem(FLOAT_KEY));
      if (d && typeof d.w === 'number') return d;
    } catch {}
    return { w: 600, h: 400, x: null, y: null };
  }
  function saveFloatGeometry(panel) {
    if (!panel || !panel.classList.contains('floating-panel')) return;
    const rect = panel.getBoundingClientRect();
    localStorage.setItem(FLOAT_KEY, JSON.stringify({
      w: panel.offsetWidth,
      h: panel.offsetHeight,
      x: rect.left,
      y: rect.top,
    }));
  }

  /* ── Clear all floating inline styles ── */
  function clearFloatStyles(panel) {
    panel.style.position = '';
    panel.style.top = '';
    panel.style.left = '';
    panel.style.width = '';
    panel.style.height = '';
    panel.style.transform = '';
  }

  /* ── Place terminal in right sidebar ── */
  function placeTerminalRight(panel) {
    panel.classList.remove('sidebar-hosted', 'floating-panel', 'hidden');
    clearFloatStyles(panel);
    panel.classList.add('right-sidebar');
    // Append to body so it's a fixed overlay like diff-sidebar
    document.body.appendChild(panel);
    // Push main content
    document.body.classList.add('term-right-open');
    // Clear inputArea push (not bottom dock)
    const inputArea = $('inputArea');
    if (inputArea) inputArea.style.bottom = '';
    requestAnimationFrame(() => { if (window.fitTerminalActive) window.fitTerminalActive(); });
  }

  /* ── Remove terminal from right sidebar ── */
  function removeTerminalRight(panel) {
    panel.classList.remove('right-sidebar');
    document.body.classList.remove('term-right-open');
    // Move back to main
    returnTerminalToOriginal(panel);
  }

  /* ── Place terminal for bottom dock ── */
  function placeTerminalBottom(panel) {
    panel.classList.remove('sidebar-hosted', 'floating-panel', 'right-sidebar');
    // Clear ALL floating inline styles before re-docking
    clearFloatStyles(panel);
    // Ensure in <main> before inputArea
    const main = document.querySelector('main.main');
    const inputArea = $('inputArea');
    if (main && inputArea && panel.parentNode !== main) {
      main.insertBefore(panel, inputArea);
    }
    panel.classList.remove('hidden');
    const savedH = localStorage.getItem('sable.term.h') || '260';
    panel.style.height = savedH + 'px';
    // Restore inputArea push
    if (inputArea) inputArea.style.bottom = savedH + 'px';
    requestAnimationFrame(() => { if (window.fitTerminalActive) window.fitTerminalActive(); });
  }

  /* ════════════════════════════════════════════
     Unified terminal open / close / toggle
     These are THE entry points — called by rail,
     header toggle, keyboard shortcut, and close btn.
     ════════════════════════════════════════════ */

  function openTerminal() {
    const panel = $('terminalPanel');
    if (!panel) return;
    const pos = getPosition('terminal');

    if (window.ensureTerminalSession) window.ensureTerminalSession();

    if (pos === 'left') {
      hostInSidebar('terminal');
    } else if (pos === 'right') {
      ensureChatVisible();
      placeTerminalRight(panel);
    } else if (pos === 'floating') {
      ensureChatVisible();
      placeTerminalFloating(panel);
    } else {
      // bottom
      ensureChatVisible();
      placeTerminalBottom(panel);
    }

    terminalOpen = true;
    setTermToggleActive(true);
    setRailActive('terminal', true);
  }

  function closeTerminal() {
    const panel = $('terminalPanel');
    if (!panel) return;

    // Save float geometry before leaving floating mode
    if (panel.classList.contains('floating-panel')) saveFloatGeometry(panel);

    if (currentHosted === 'terminal') {
      // Mark hidden BEFORE unhosting so sidebar-unhosted event doesn't restore height
      panel.classList.add('hidden');
      unhostFromSidebar();
    } else if (panel.classList.contains('right-sidebar')) {
      panel.classList.add('hidden');
      removeTerminalRight(panel);
    } else {
      panel.classList.add('hidden');
      panel.classList.remove('floating-panel');
      // Clear all floating inline styles
      clearFloatStyles(panel);
      // If floating, move back to main so it doesn't linger in body
      if (panel.parentNode === document.body) {
        returnTerminalToOriginal(panel);
      }
      // Clear inputArea push
      const inputArea = $('inputArea');
      if (inputArea) inputArea.style.bottom = '';
    }

    terminalOpen = false;
    setTermToggleActive(false);
    setRailActive('terminal', false);
  }

  function toggleTerminal() {
    terminalOpen ? closeTerminal() : openTerminal();
  }

  /* ── Position switcher UI ── */
  function createPositionSwitcher(panelId, panelEl) {
    if (panelEl.querySelector('.pos-switcher-wrap')) return;

    const wrap = document.createElement('div');
    wrap.className = 'pos-switcher-wrap';

    const btn = document.createElement('button');
    btn.className = 'term-bar-btn pos-switcher-btn';
    btn.title = 'Move panel';
    btn.innerHTML = '<span class="icon-emoji">⊞</span><i data-lucide="layout-template" class="icon-lucide"></i>';

    const menu = document.createElement('div');
    menu.className = 'pos-switcher-menu';
    menu.hidden = true;

    const positions = [
      { id: 'left', label: 'Left Sidebar', icon: 'panel-left' },
      { id: 'right', label: 'Right Sidebar', icon: 'panel-right' },
      { id: 'bottom', label: 'Bottom Dock', icon: 'panel-bottom' },
      { id: 'floating', label: 'Floating', icon: 'app-window' },
    ];

    for (const p of positions) {
      const opt = document.createElement('button');
      opt.dataset.pos = p.id;
      opt.innerHTML = `<i data-lucide="${p.icon}"></i>${p.label}`;
      menu.appendChild(opt);
    }

    wrap.appendChild(btn);
    wrap.appendChild(menu);

    const actions = panelEl.querySelector('.terminal-bar-actions');
    if (actions) {
      const closeBtn = actions.querySelector('#terminalClose');
      actions.insertBefore(wrap, closeBtn || actions.firstChild);
    }

    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      menu.hidden = !menu.hidden;
      const cur = currentHosted === panelId ? 'left' : getPosition(panelId);
      menu.querySelectorAll('button').forEach(b => {
        b.classList.toggle('active', b.dataset.pos === cur);
      });
      if (window.lucide) window.lucide.createIcons({ nodes: [menu] });
    });

    document.addEventListener('click', (e) => {
      if (!wrap.contains(e.target)) menu.hidden = true;
    });

    menu.addEventListener('click', (e) => {
      const opt = e.target.closest('[data-pos]');
      if (!opt) return;
      menu.hidden = true;
      movePanel(panelId, opt.dataset.pos);
    });

    if (window.lucide) window.lucide.createIcons({ nodes: [wrap] });
  }

  /* ── Move panel to target position (live, while open) ── */
  function movePanel(panelId, position) {
    const panel = $(panelId === 'terminal' ? 'terminalPanel' : panelId);
    if (!panel) return;

    savePosition(panelId, position);

    if (panelId === 'terminal') {
      // Save float geometry before leaving floating mode
      if (panel.classList.contains('floating-panel')) saveFloatGeometry(panel);
      // First, cleanly remove from current location
      if (currentHosted === 'terminal') unhostFromSidebar();
      // Remove right sidebar state if present
      if (panel.classList.contains('right-sidebar')) {
        panel.classList.remove('right-sidebar');
        document.body.classList.remove('term-right-open');
      }
      panel.classList.remove('floating-panel', 'sidebar-hosted', 'hidden');
      clearFloatStyles(panel);
      // Clear inputArea push during transition
      const inputArea = $('inputArea');
      if (inputArea) inputArea.style.bottom = '';

      if (position === 'left') {
        // Ensure sidebar is visible before hosting
        document.body.classList.remove('sidebar-collapsed');
        const sidebar = document.querySelector('aside.sidebar');
        if (sidebar) sidebar.style.visibility = '';
        hostInSidebar('terminal');
        setRailActive('terminal', true);
        setTermToggleActive(true);
      } else if (position === 'right') {
        ensureChatVisible();
        placeTerminalRight(panel);
        setRailActive('terminal', true);
        setTermToggleActive(true);
      } else if (position === 'floating') {
        ensureChatVisible();
        placeTerminalFloating(panel);
      } else {
        ensureChatVisible();
        placeTerminalBottom(panel);
      }
    }
  }

  /* ── Rail-switch listener ── */
  window.addEventListener('rail-switch', (e) => {
    const target = e.detail?.target;

    if (!target || target === 'chat') {
      // Null target = user toggled off the active rail button
      // Close terminal regardless of current position (not just sidebar)
      if (terminalOpen) {
        closeTerminal();
      }
      // Sync rail button to match actual state (mode.js may have toggled it)
      requestAnimationFrame(() => setRailActive('terminal', terminalOpen));
      return;
    }

    if (target === 'terminal') {
      toggleTerminal();
      // Override mode.js's class toggle — our state is authoritative
      requestAnimationFrame(() => setRailActive('terminal', terminalOpen));
      return;
    }

    // Switching to a non-terminal rail → close terminal if open
    if (terminalOpen) closeTerminal();
  });

  /* ── Hook terminal header toggle & close button ── */
  function hookTerminalControls() {
    const toggleBtn = $('terminalToggle');
    const closeBtn = document.getElementById('terminalClose');

    // Intercept header toggle — always route through our unified toggle
    if (toggleBtn) {
      toggleBtn.addEventListener('click', (e) => {
        e.stopImmediatePropagation();
        toggleTerminal();
      }, true);
    }

    // Intercept close button inside terminal bar
    if (closeBtn) {
      closeBtn.addEventListener('click', (e) => {
        e.stopImmediatePropagation();
        closeTerminal();
      }, true);
    }

    // Intercept keyboard shortcut (Ctrl+` / Ctrl+J)
    window.addEventListener('keydown', (e) => {
      if ((e.ctrlKey || e.metaKey) && (e.key === '`' || e.key.toLowerCase() === 'j')) {
        e.preventDefault();
        e.stopImmediatePropagation();
        toggleTerminal();
      }
    }, true);
  }

  /* ── Init ── */
  function init() {
    ensureSlot();
    hookTerminalControls();

    const termPanel = $('terminalPanel');
    if (termPanel) createPositionSwitcher('terminal', termPanel);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  /* ════════════════════════════════════════════
     Generic panel registry — any module can
     register itself to be hostable in sidebar
     with the same open/close/move semantics.
     ════════════════════════════════════════════ */
  const panelRegistry = new Map();

  /**
   * Register a panel for sidebar hosting.
   * @param {string} id - rail target name (e.g. 'search', 'research')
   * @param {Object} opts
   * @param {string|Function} opts.panelId - DOM element ID or fn returning it
   * @param {Function} [opts.onOpen] - called after panel is placed & shown
   * @param {Function} [opts.onClose] - called after panel is hidden
   * @param {Function} [opts.onMove] - called when position changes (pos) => {}
   */
  function registerPanel(id, opts) {
    panelRegistry.set(id, opts);
  }

  function getPanelEl(id) {
    const reg = panelRegistry.get(id);
    if (!reg) return null;
    return typeof reg.panelId === 'function' ? reg.panelId() : $(reg.panelId);
  }

  /** Generic open for registered panels */
  function openPanel(id) {
    const reg = panelRegistry.get(id);
    const el = getPanelEl(id);
    if (!el) return;

    const pos = getPosition(id);
    if (pos === 'left') {
      hostInSidebar(id);
    } else {
      ensureChatVisible();
      el.classList.remove('hidden');
    }
    if (reg?.onOpen) reg.onOpen(el, pos);
    setRailActive(id, true);
  }

  /** Generic close for registered panels */
  function closePanel(id) {
    const reg = panelRegistry.get(id);
    const el = getPanelEl(id);
    if (!el) return;

    if (currentHosted === id) {
      unhostFromSidebar();
    } else {
      el.classList.add('hidden');
    }
    if (reg?.onClose) reg.onClose(el);
    setRailActive(id, false);
  }

  // Extend rail-switch to handle registered panels
  window.addEventListener('rail-switch', (e) => {
    const target = e.detail?.target;
    if (!target || target === 'chat') return;
    if (target === 'terminal') return; // handled above

    if (panelRegistry.has(target)) {
      // Toggle
      const el = getPanelEl(target);
      if (el && !el.classList.contains('hidden')) {
        closePanel(target);
      } else {
        // Close any other open panel first
        panelRegistry.forEach((_, id) => {
          if (id !== target) closePanel(id);
        });
        if (terminalOpen) closeTerminal();
        openPanel(target);
      }
    }
  });

  // Expose API
  window.sidebarHost = {
    host: hostInSidebar,
    unhost: unhostFromSidebar,
    move: movePanel,
    getCurrent: () => currentHosted,
    // Terminal-specific
    openTerminal,
    closeTerminal,
    toggleTerminal,
    isTerminalOpen: () => terminalOpen,
    // Generic panel API
    register: registerPanel,
    openPanel,
    closePanel,
    getPosition,
    savePosition,
  };
})();
