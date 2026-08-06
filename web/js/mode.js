
/**
 * mode.js — Layout mode switching (Agent ↔ IDE)
 * Scaffolded for future IDE mode implementation.
 */
(function () {
  'use strict';

  const body = document.body;
  const STORAGE_KEY = 'sable_layout_mode';

  // Elements
  const layoutAgentBtn = document.getElementById('layoutAgent');
  const layoutIdeBtn = document.getElementById('layoutIde');
  const sidebarToggle = document.getElementById('sidebarToggle');
  const chatCompact = document.getElementById('chatCompact');
  const chatCompactInput = document.getElementById('chatCompactInput');
  const chatCompactSend = document.getElementById('chatCompactSend');

  // ─── Mode Switching ───

  function setLayoutMode(mode) {
    body.setAttribute('data-mode', mode);
    localStorage.setItem(STORAGE_KEY, mode);

    // Update switcher buttons
    if (mode === 'agent') {
      layoutAgentBtn.classList.add('active');
      layoutIdeBtn.classList.remove('active');
    } else {
      layoutAgentBtn.classList.remove('active');
      layoutIdeBtn.classList.add('active');
    }

    // In IDE mode, ensure diff sidebar (file browser) is visible
    if (mode === 'ide') {
      body.classList.add('diff-open');
      body.classList.remove('ide-sidebar-open');
      // Restore last session (folder + file)
      if (window.restoreIdeSession) window.restoreIdeSession();
    } else {
      // Leaving IDE mode: close the right sidebar
      body.classList.remove('diff-open');
      // Clear inline sizes set by IDE resize handles so agent-mode CSS
      // defaults (325px sidebar / 331px main margin) stay in sync —
      // otherwise a custom IDE width leaks into agent mode and creates
      // a gap or overlap between .main and the right panel.
      const diffSidebar = document.getElementById('diffSidebar');
      if (diffSidebar) diffSidebar.style.width = '';
      const chatCompactEl = document.getElementById('chatCompact');
      if (chatCompactEl) {
        chatCompactEl.style.width = '';
        chatCompactEl.style.minWidth = '';
      }
      const main = document.querySelector('.main');
      if (main) main.style.marginRight = '';
    }

    // Re-render lucide icons for newly visible elements
    if (window.lucide) window.lucide.createIcons();
  }

  function getLayoutMode() {
    return localStorage.getItem(STORAGE_KEY) || 'agent';
  }

  // ─── IDE Sidebar Toggle ───
  // In IDE mode, the sidebar toggle opens the full sidebar (chat history)
  // over the compact chat. Closing it returns to compact chat.

  let originalToggleHandler = null;

  function patchSidebarToggle() {
    // Store reference to existing toggle behavior
    // We intercept the click to handle IDE mode differently
    if (!sidebarToggle) return;

    sidebarToggle.addEventListener('click', function (e) {
      const mode = body.getAttribute('data-mode');
      if (mode === 'ide') {
        e.stopPropagation();
        e.preventDefault();
        const isOpen = body.classList.contains('ide-sidebar-open');
        if (isOpen) {
          body.classList.remove('ide-sidebar-open');
        } else {
          body.classList.add('ide-sidebar-open');
        }
      }
      // In agent mode, let the existing handler in app.js do its thing
    }, true); // capture phase so we can intercept before app.js handler
  }

  // ─── Compact Chat Auto-resize ───

  function setupCompactInput() {
    if (!chatCompactInput) return;
    chatCompactInput.addEventListener('input', function () {
      this.style.height = 'auto';
      this.style.height = Math.min(this.scrollHeight, 100) + 'px';
    });
    chatCompactInput.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendCompactMessage();
      }
    });
    if (chatCompactSend) {
      chatCompactSend.addEventListener('click', sendCompactMessage);
    }
  }

  function sendCompactMessage() {
    const mainSend = document.getElementById('send');

    // If streaming, clicking compact send = stop (delegate to main send which aborts)
    if (mainSend && mainSend.classList.contains('stop-mode')) {
      mainSend.click();
      return;
    }

    const text = chatCompactInput.value.trim();
    if (!text) return;

    // Delegate to the main input + send mechanism in app.js
    const mainInput = document.getElementById('input');
    if (mainInput && mainSend) {
      mainInput.value = text;
      mainInput.dispatchEvent(new Event('input', { bubbles: true }));
      mainSend.click();
    }

    chatCompactInput.value = '';
    chatCompactInput.style.height = 'auto';
  }

  // ─── Compact Chat Mirroring ───
  // Watches #chat for new messages and mirrors them into the compact panel
  // Uses rAF throttle to avoid wiping DOM faster than the browser can paint.

  let chatObserver = null;
  let mirrorRafId = null;

  // Maps each compact mirror clone -> its live source message in the main chat.
  // cloneNode(true) strips listeners, so we forward toolbar clicks to the source.
  const cloneToSource = new WeakMap();

  function getActivePane() {
    const chat = document.getElementById('chat');
    if (!chat) return null;
    return chat.querySelector('.tab-pane.active') || chat;
  }

  function syncPendingState(compactMsgs, source) {
    const srcPending = source.querySelector('.pending-indicator');
    const compactPending = compactMsgs.querySelector('.compact-pending');
    if (srcPending && !compactPending) {
      const el = document.createElement('div');
      el.className = 'compact-pending pending-indicator';
      el.innerHTML = '<span class="processing-text">processing\u2026</span>';
      compactMsgs.appendChild(el);
      compactMsgs.scrollTop = compactMsgs.scrollHeight;
    } else if (!srcPending && compactPending) {
      compactPending.remove();
    }
  }

  function mirrorMessages() {
    const compactMsgs = document.getElementById('chatCompactMessages');
    const source = getActivePane();
    if (!compactMsgs || !source) return;

    const messages = Array.from(source.querySelectorAll('.msg, .skill-card, .thinking-wrap'));
    const emptyState = document.getElementById('chatCompactEmpty');
    if (emptyState) {
      emptyState.style.display = messages.length === 0 ? '' : 'none';
    }
    const existingClones = compactMsgs.querySelectorAll('.compact-msg');

    // Fast path: same count — patch the last (streaming) message
    if (messages.length === existingClones.length && messages.length > 0) {
      const lastSrc = messages[messages.length - 1];
      const lastClone = existingClones[existingClones.length - 1];
      cloneToSource.set(lastClone, lastSrc);
      // Sync content
      if (lastSrc.innerHTML !== lastClone.innerHTML) {
        lastClone.innerHTML = lastSrc.innerHTML;
      }
      // ALWAYS strip .streaming from compact clones — the ::after shimmer
      // pseudo-element on .bot.streaming .md-content must never persist
      // in the mirror. Main chat owns the live streaming UX.
      const srcClass = lastSrc.className.replace(/\bstreaming\b/g, '').trim() + ' compact-msg';
      if (lastClone.className !== srcClass) {
        lastClone.className = srcClass;
      }
      compactMsgs.scrollTop = compactMsgs.scrollHeight;
      syncPendingState(compactMsgs, source);
      return;
    }

    // Slow path: count changed — full rebuild
    // Remove only cloned messages and pending indicators, preserve empty-state element
    compactMsgs.querySelectorAll('.compact-msg, .compact-pending').forEach(el => el.remove());
    messages.forEach(msg => {
      const clone = msg.cloneNode(true);
      clone.classList.add('compact-msg');
      clone.classList.remove('streaming');
      cloneToSource.set(clone, msg);
      compactMsgs.appendChild(clone);
    });
    compactMsgs.scrollTop = compactMsgs.scrollHeight;
    syncPendingState(compactMsgs, source);
  }

  function scheduleMirror() {
    // Adaptive debounce: faster during streaming so compact chat feels live,
    // slower when idle to avoid unnecessary rebuilds.
    const mainSend = document.getElementById('send');
    const streaming = mainSend && mainSend.classList.contains('stop-mode');
    const delay = streaming ? 16 : 80;  // ~60fps while streaming, 80ms idle
    clearTimeout(mirrorRafId);
    mirrorRafId = setTimeout(() => {
      mirrorRafId = null;
      if (body.getAttribute('data-mode') === 'ide') {
        mirrorMessages();
        syncPendingIndicator();
      }
    }, delay);
  }

  function startChatObserver() {
    if (chatObserver) chatObserver.disconnect();

    const chat = document.getElementById('chat');
    if (!chat) return;

    chatObserver = new MutationObserver(() => {
      scheduleMirror();
    });

    chatObserver.observe(chat, {
      childList: true,
      subtree: true,
      characterData: true,
      attributes: true,
      attributeFilter: ['class']
    });
  }

  // ─── Stop Button Sync ───
  // Watches main #send for stop-mode class and mirrors to compact send button

  let sendBtnObserver = null;

  function syncStopMode(isStreaming) {
    if (!chatCompactSend) return;
    if (isStreaming) {
      chatCompactSend.classList.add('stop-mode');
      chatCompactSend.title = 'Stop';
      chatCompactSend.querySelector('.icon-emoji').textContent = '\u25A0';
    } else {
      chatCompactSend.classList.remove('stop-mode');
      chatCompactSend.title = 'Send';
      chatCompactSend.querySelector('.icon-emoji').textContent = '\u27A4';
      // Stream ended — immediately kill any lingering compact pending indicator.
      // More reliable than waiting for MutationObserver + debounce timing.
      const compactMsgs = document.getElementById('chatCompactMessages');
      if (compactMsgs) {
        const cp = compactMsgs.querySelector('.compact-pending');
        if (cp) cp.remove();
      }
    }
  }

  function startSendBtnObserver() {
    const mainSend = document.getElementById('send');
    if (!mainSend || sendBtnObserver) return;

    sendBtnObserver = new MutationObserver(() => {
      if (body.getAttribute('data-mode') === 'ide') {
        syncStopMode(mainSend.classList.contains('stop-mode'));
      }
    });

    sendBtnObserver.observe(mainSend, { attributes: true, attributeFilter: ['class'] });
  }

  // ─── Pending Indicator Mirror ───
  // Shows/hides "processing..." in compact panel based on main chat state

  function syncPendingIndicator() {
    const compactMsgs = document.getElementById('chatCompactMessages');
    const source = getActivePane();
    if (!compactMsgs || !source) return;

    const srcPending = source.querySelector('.pending-indicator');
    const compactPending = compactMsgs.querySelector('.compact-pending');

    if (srcPending && !compactPending) {
      const el = document.createElement('div');
      el.className = 'compact-pending pending-indicator';
      el.innerHTML = '<span class="processing-text">processing\u2026</span>';
      compactMsgs.appendChild(el);
      compactMsgs.scrollTop = compactMsgs.scrollHeight;
    } else if (!srcPending && compactPending) {
      compactPending.remove();
    }
  }

  // ─── Public API ───

  // ─── Compact Chat Title Sync ───

  function updateCompactTitle(title) {
    const el = document.getElementById('chatCompactTitle');
    if (el) el.textContent = title || 'New chat';
  }

  window.setLayoutMode = setLayoutMode;
  window.getLayoutMode = getLayoutMode;
  window.updateCompactTitle = updateCompactTitle;

  // ─── Button Click Handlers ───

  function setupSwitcherButtons() {
    if (layoutAgentBtn) {
      layoutAgentBtn.addEventListener('click', function () {
        setLayoutMode('agent');
      });
    }
    if (layoutIdeBtn) {
      layoutIdeBtn.addEventListener('click', function () {
        setLayoutMode('ide');
      });
    }
  }

  // ─── Copy Button Fix for IDE Compact Chat ───
  // cloneNode(true) strips event listeners from mirrored copy buttons.
  // Delegated click handler re-enables them without touching bundled app.js.

  function setupCompactCopyDelegation() {
    const compactMsgs = document.getElementById('chatCompactMessages');
    if (!compactMsgs) return;

    compactMsgs.addEventListener('click', function (e) {
      const btn = e.target.closest('.code-copy-btn');
      if (!btn) return;
      e.preventDefault();
      e.stopPropagation();

      const codeBlock = btn.closest('.code-block') || btn.closest('pre');
      if (!codeBlock) return;

      const pre = codeBlock.tagName === 'PRE' ? codeBlock : codeBlock.querySelector('pre');
      if (!pre) return;

      const text = pre.textContent || '';
      navigator.clipboard.writeText(text).then(() => {
        btn.classList.add('copied');
        const origHTML = btn.innerHTML;
        btn.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg>';
        setTimeout(() => {
          btn.classList.remove('copied');
          btn.innerHTML = origHTML;
        }, 2000);
      }).catch(() => {});
    });
  }

  // ─── Toolbar Button Forwarding for IDE Compact Chat ───
  // cloneNode(true) strips listeners from mirrored .msg-toolbar / .retry-command-bar
  // buttons (Copy, Regenerate, Resend, Resend tool results). Instead of duplicating
  // app.js logic, forward each click to the matching live button in the source message.
  function setupCompactToolbarDelegation() {
    const compactMsgs = document.getElementById('chatCompactMessages');
    if (!compactMsgs) return;

    compactMsgs.addEventListener('click', function (e) {
      // Skill-card Output/Command toggle is a pure local visual toggle — handle
      // it on the visible compact card directly (forwarding to the hidden source
      // would flip a card the user can't see).
      const skillToggle = e.target.closest('.skill-toggle-btn');
      if (skillToggle) {
        e.preventDefault();
        e.stopPropagation();
        const card = skillToggle.closest('.skill-card');
        if (!card) return;
        const showing = card.classList.toggle('show-output');
        skillToggle.textContent = showing ? 'Command' : 'Output';
        return;
      }

      const btn = e.target.closest('.msg-toolbar button, .retry-command-bar button');
      if (!btn) return;
      const cloneMsg = btn.closest('.compact-msg');
      const sourceMsg = cloneMsg && cloneToSource.get(cloneMsg);
      if (!sourceMsg) return;

      const container = btn.closest('.msg-toolbar, .retry-command-bar');
      if (!container) return;
      const selector = container.classList.contains('retry-command-bar') ? '.retry-command-bar' : '.msg-toolbar';

      const btnIndex = Array.from(container.querySelectorAll('button')).indexOf(btn);
      const cloneContainers = Array.from(cloneMsg.querySelectorAll(selector));
      const containerIndex = cloneContainers.indexOf(container);
      const sourceContainers = Array.from(sourceMsg.querySelectorAll(selector));
      const sourceContainer = sourceContainers[containerIndex];
      const sourceBtn = sourceContainer && sourceContainer.querySelectorAll('button')[btnIndex];
      if (!sourceBtn) return;

      e.preventDefault();
      e.stopPropagation();
      sourceBtn.click();
    });
  }

  // ─── Init ───

  function init() {
    const saved = getLayoutMode();
    setLayoutMode(saved);
    setupSwitcherButtons();
    patchSidebarToggle();
    setupCompactInput();
    setupCompactCopyDelegation();
    setupCompactToolbarDelegation();
    startChatObserver();
    startSendBtnObserver();
    // Initial mirror if starting in IDE mode
    if (saved === 'ide') {
      setTimeout(mirrorMessages, 500);
      // Restore IDE session (folder + file) after DOM settles
      setTimeout(() => {
        if (window.restoreIdeSession) window.restoreIdeSession();
      }, 300);
    }
  }

  // ─── CWD + Open File Injection into /api/chat ───
  // Patches fetch to auto-inject IDE context (cwd + open file) into chat requests.
  const _origFetch = window.fetch;
  window.fetch = function (url, opts) {
    if (
      typeof url === 'string' &&
      url === '/api/chat' &&
      opts && opts.method === 'POST' && opts.body
    ) {
      try {
        const body = JSON.parse(opts.body);
        const cwd = window.getIdeCwd ? window.getIdeCwd() : '';
        let changed = false;
        if (cwd && !body.cwd) { body.cwd = cwd; changed = true; }
        // Only inject open_file in IDE mode
        if (document.body.dataset.mode === 'ide') {
          const openFile = window.getIdeOpenFile ? window.getIdeOpenFile() : '';
          if (openFile && !body.open_file) { body.open_file = openFile; changed = true; }
        }
        if (changed) opts.body = JSON.stringify(body);
      } catch { /* non-JSON body, skip */ }
    }
    return _origFetch.apply(this, arguments);
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
