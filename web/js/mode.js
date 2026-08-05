
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
    const existingClones = compactMsgs.querySelectorAll('.compact-msg');

    // Fast path: same count — patch the last (streaming) message
    if (messages.length === existingClones.length && messages.length > 0) {
      const lastSrc = messages[messages.length - 1];
      const lastClone = existingClones[existingClones.length - 1];
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
    compactMsgs.innerHTML = '';
    messages.forEach(msg => {
      const clone = msg.cloneNode(true);
      clone.classList.add('compact-msg');
      clone.classList.remove('streaming');
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

  window.setLayoutMode = setLayoutMode;
  window.getLayoutMode = getLayoutMode;

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

  // ─── Init ───

  function init() {
    const saved = getLayoutMode();
    setLayoutMode(saved);
    setupSwitcherButtons();
    patchSidebarToggle();
    setupCompactInput();
    setupCompactCopyDelegation();
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
        const openFile = window.getIdeOpenFile ? window.getIdeOpenFile() : '';
        let changed = false;
        if (cwd && !body.cwd) { body.cwd = cwd; changed = true; }
        if (openFile && !body.open_file) { body.open_file = openFile; changed = true; }
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
