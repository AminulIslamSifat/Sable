
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
      // Sync compact status bar from main controls
      setTimeout(syncCompactStatusBar, 100);
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

    // Refresh IDE tab bar visibility on mode switch
    if (window.renderIdeTabBar) window.renderIdeTabBar();

    // Re-render lucide icons for newly visible elements
    if (window.lucide) window.lucide.createIcons();
  }

  // ─── VS Code Embed Detection ───
  // When loaded inside VS Code sidebar, add vscode-embed class for CSS overrides.
  // Forces agent mode + closes diff viewer so only chat is visible.
  let _isVscodeEmbed = false;

  window.addEventListener('message', function(e) {
    if (e.data && e.data.type === 'vscode-embed') {
      if (!_isVscodeEmbed) {
        _isVscodeEmbed = true;
        document.body.classList.add('vscode-embed');
        // Force IDE mode — VS Code sidebar uses compact chat at full width
        setLayoutMode('ide');
      }
      // Always ack so extension stops retrying
      try { window.parent.postMessage({ type: 'vscode-embed-ack' }, '*'); } catch(err) {}
    }
  });

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
      // In agent mode, let the existing handler in chat.js do its thing
    }, true); // capture phase so we can intercept before chat.js handler
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


    // New Chat button in compact header — delegate to sidebar new chat button
    const compactNewChat = document.getElementById('compactNewChat');
    if (compactNewChat) {
      compactNewChat.addEventListener('click', () => {
        const sidebarBtn = document.getElementById('sidebarNewChatBtn');
        if (sidebarBtn) sidebarBtn.click();
      });
    }

    // Agent mode switch button
    const compactAgentSwitch = document.getElementById('compactAgentSwitch');
    if (compactAgentSwitch) {
      compactAgentSwitch.addEventListener('click', () => {
        setLayoutMode('agent');
      });
    }


    // Attach button — delegate to main file input
    const compactAttach = document.getElementById('chatCompactAttach');
    const compactFileInput = document.getElementById('chatCompactFileInput');
    const mainFileInput = document.getElementById('fileInput');
    if (compactAttach && compactFileInput) {
      compactAttach.addEventListener('click', () => {
        // Use main file input if available (has the change handler wired in chat.js)
        if (mainFileInput) {
          mainFileInput.click();
        } else {
          compactFileInput.click();
        }
      });
    }

    // Compact thinking dropdown — self-contained, clones items from main menu
    const compactThinkingDropdown = document.getElementById('compactThinkingDropdown');
    const compactThinkingTrigger = document.getElementById('compactThinkingTrigger');
    const compactThinkingMenu = document.getElementById('compactThinkingMenu');
    if (compactThinkingTrigger && compactThinkingDropdown && compactThinkingMenu) {
      compactThinkingTrigger.addEventListener('click', (e) => {
        e.stopPropagation();
        const isOpen = compactThinkingDropdown.classList.toggle('open');
        if (isOpen) {
          // Clone items from main thinking menu
          const mainMenu = document.getElementById('statusThinkingMenu');
          compactThinkingMenu.innerHTML = '';
          if (mainMenu) {
            for (const item of mainMenu.children) {
              const clone = item.cloneNode(true);
              clone.addEventListener('click', (ev) => {
                ev.stopPropagation();
                // Forward click to the original main menu item (has the real handler)
                item.click();
                compactThinkingDropdown.classList.remove('open');
                setTimeout(syncCompactStatusBar, 50);
              });
              compactThinkingMenu.appendChild(clone);
            }
          }
        }
      });
      document.addEventListener('click', () => compactThinkingDropdown.classList.remove('open'));
    }

    // Compact model dropdown — self-contained, clones items from main menu
    const compactModelDropdown = document.getElementById('compactModelDropdown');
    const compactModelTrigger = document.getElementById('compactModelTrigger');
    const compactModelMenu = document.getElementById('compactModelMenu');
    if (compactModelTrigger && compactModelDropdown && compactModelMenu) {
      compactModelTrigger.addEventListener('click', (e) => {
        e.stopPropagation();
        const isOpen = compactModelDropdown.classList.toggle('open');
        if (isOpen) {
          // Clone items from main model menu
          const mainMenu = document.getElementById('modelMenu');
          compactModelMenu.innerHTML = '';
          if (mainMenu) {
            for (const item of mainMenu.children) {
              const clone = item.cloneNode(true);
              clone.addEventListener('click', (ev) => {
                ev.stopPropagation();
                // Forward click to the original main menu item (has the real handler)
                item.click();
                compactModelDropdown.classList.remove('open');
                setTimeout(syncCompactStatusBar, 50);
              });
              compactModelMenu.appendChild(clone);
            }
          }
        }
      });
      document.addEventListener('click', () => compactModelDropdown.classList.remove('open'));
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

    // Delegate to the main input + send mechanism in sse.js
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
      const gap0 = compactMsgs.scrollHeight - compactMsgs.scrollTop - compactMsgs.clientHeight;
      if (gap0 < 80) compactMsgs.scrollTop = compactMsgs.scrollHeight;
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
      const gap1 = compactMsgs.scrollHeight - compactMsgs.scrollTop - compactMsgs.clientHeight;
      if (gap1 < 80) compactMsgs.scrollTop = compactMsgs.scrollHeight;
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
    const gap2 = compactMsgs.scrollHeight - compactMsgs.scrollTop - compactMsgs.clientHeight;
    if (gap2 < 80) compactMsgs.scrollTop = compactMsgs.scrollHeight;
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

  // ─── Status Bar Mirror (thinking, model, context) ───
  // Syncs compact status bar controls from the main input area

  function syncCompactStatusBar() {
    if (body.getAttribute('data-mode') !== 'ide') return;

    // Thinking mode label + visibility
    const mainThinkingLabel = document.getElementById('statusThinkingLabel');
    const compactThinkingLabel = document.getElementById('compactThinkingLabel');
    const mainThinkingDropdown = document.getElementById('statusThinkingDropdown');
    const compactThinkingDropdown = document.getElementById('compactThinkingDropdown');
    if (mainThinkingLabel && compactThinkingLabel) {
      compactThinkingLabel.textContent = mainThinkingLabel.textContent;
    }
    if (mainThinkingDropdown && compactThinkingDropdown) {
      compactThinkingDropdown.style.display = mainThinkingDropdown.style.display;
    }

    // Model label
    const mainModelLabel = document.querySelector('#modelDropdown .glass-dropdown-label');
    const compactModelLabel = document.getElementById('compactModelLabel');
    if (mainModelLabel && compactModelLabel) {
      compactModelLabel.textContent = mainModelLabel.textContent;
    }

    // Context usage (SVG ring — must copy innerHTML)
    const mainContext = document.getElementById('statusContext');
    const compactContext = document.getElementById('compactContext');
    if (mainContext && compactContext) {
      compactContext.innerHTML = mainContext.innerHTML;
      compactContext.title = mainContext.title;
    }
  }

  let statusBarObserver = null;

  function startStatusBarObserver() {
    const mainStatusBar = document.getElementById('statusBar');
    if (!mainStatusBar || statusBarObserver) return;

    statusBarObserver = new MutationObserver(() => syncCompactStatusBar());
    statusBarObserver.observe(mainStatusBar, { childList: true, subtree: true, characterData: true });

    // Also sync on model change events
    const modelSelect = document.getElementById('modelSelect');
    if (modelSelect) {
      modelSelect.addEventListener('change', () => setTimeout(syncCompactStatusBar, 50));
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
  // Delegated click handler re-enables them without touching bundled chat.js.

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
  // sse.js logic, forward each click to the matching live button in the source message.
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

  // ─── Activity Rail ───

  function setupActivityRail() {
    const rail = document.getElementById('activityRail');
    if (!rail) return;
    const buttons = rail.querySelectorAll('.rail-btn');
    buttons.forEach(btn => {
      btn.addEventListener('click', () => {
        const wasActive = btn.classList.contains('active');
        buttons.forEach(b => b.classList.remove('active'));
        if (wasActive) {
          // Toggle off — deselect, notify with null target
          window.dispatchEvent(new CustomEvent('rail-switch', { detail: { target: null } }));
        } else {
          btn.classList.add('active');
          window.dispatchEvent(new CustomEvent('rail-switch', { detail: { target: btn.dataset.rail } }));
        }
      });
    });
  }

  // ─── Rail Sidebar Toggle ───

  function setupRailSidebarToggle() {
    const btn = document.getElementById('railSidebarToggle');
    if (!btn) return;

    const syncState = () => {
      const isMobile = window.matchMedia('(max-width: 860px)').matches;
      const isOpen = isMobile
        ? body.classList.contains('sidebar-open')
        : !body.classList.contains('sidebar-collapsed');
      btn.classList.toggle('active', isOpen);
      // Swap icon between panel-left-close and panel-left-open
      const lucideIcon = btn.querySelector('.icon-lucide');
      if (lucideIcon) {
        lucideIcon.setAttribute('data-lucide', isOpen ? 'panel-left-close' : 'panel-left-open');
        if (window.lucide) lucide.createIcons({ nodes: [btn] });
      }
    };

    btn.addEventListener('click', () => {
      const isMobile = window.matchMedia('(max-width: 860px)').matches;
      if (isMobile) {
        body.classList.toggle('sidebar-open');
      } else {
        body.classList.toggle('sidebar-collapsed');
      }
      syncState();
    });

    // Observe body class changes to stay in sync (Alt+B, other toggles)
    new MutationObserver(syncState).observe(body, { attributes: true, attributeFilter: ['class'] });

    // Initial state
    syncState();
  }

  // ─── Init ───

  // ─── Mobile Menu Toggle ───

  function setupMobileMenu() {
    const btn = document.getElementById('mobileMenuBtn');
    const newChatBtn = document.getElementById('mobileNewChatBtn');
    if (!btn) return;

    const overlay = document.querySelector('.sidebar-overlay');

    const toggle = () => {
      const isOpen = body.classList.toggle('mobile-menu-open');
      if (!isOpen) {
        body.classList.remove('sidebar-open');
      }
    };

    btn.addEventListener('click', toggle);

    // Close menu when overlay is tapped
    if (overlay) {
      overlay.addEventListener('click', () => {
        body.classList.remove('mobile-menu-open');
        body.classList.remove('sidebar-open');
      });
    }

    // Close menu when a rail button is clicked (user selected something)
    window.addEventListener('rail-switch', () => {
      if (body.classList.contains('mobile-menu-open')) {
        body.classList.remove('mobile-menu-open');
        body.classList.remove('sidebar-open');
      }
    });

    // Mobile new chat button — delegates to sidebar new chat button
    if (newChatBtn) {
      newChatBtn.addEventListener('click', () => {
        const sidebarBtn = document.getElementById('sidebarNewChatBtn');
        if (sidebarBtn) sidebarBtn.click();
      });
    }
  }


  function init() {
    const saved = getLayoutMode();
    setLayoutMode(saved);
    setupSwitcherButtons();
    setupActivityRail();
    setupRailSidebarToggle();
    setupMobileMenu();
    patchSidebarToggle();
    setupCompactInput();
    setupCompactCopyDelegation();
    setupCompactToolbarDelegation();
    startChatObserver();
    startSendBtnObserver();
    startStatusBarObserver();
    // Initial mirror if starting in IDE mode
    if (saved === 'ide') {
      setTimeout(mirrorMessages, 500);
      setTimeout(syncCompactStatusBar, 600);
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
