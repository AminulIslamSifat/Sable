    /* ---------- Settings & Live Logs ---------- */
    const settingsOverlay = document.getElementById("settingsOverlay");
    const settingsBtn = document.getElementById("settingsBtn");
    const settingsClose = document.getElementById("settingsClose");
    const logViewer = document.getElementById("logViewer");
    const logAutoScroll = document.getElementById("logAutoScroll");
    const logClear = document.getElementById("logClear");
    let logSource = null;

    function openSettings() {
      settingsOverlay.classList.remove("hidden");
      if (!logSource) connectLogs();
    }

    function closeSettings() {
      settingsOverlay.classList.add("hidden");
    }

    settingsBtn.addEventListener("click", openSettings);
    settingsClose.addEventListener("click", closeSettings);
    settingsOverlay.addEventListener("click", (e) => {
      if (e.target === settingsOverlay) closeSettings();
    });

    // ── Configurable Keyboard Shortcut System ────────────────────────────────
    const SHORTCUTS_STORAGE_KEY = 'SABLE_SHORTCUTS';

    function openLibraryTab(tabName) {
      libraryOverlay.classList.remove('hidden');
      const tgTab = document.getElementById('libTelegramTab');
      if (tgTab) tgTab.style.display = localStorage.getItem('sable_telegram_enabled') === 'true' ? '' : 'none';
      libraryTabs.querySelectorAll('.settings-tab').forEach(t => t.classList.remove('active'));
      libraryBody.querySelectorAll('.settings-tab-content').forEach(c => c.classList.remove('active'));
      const tab = libraryTabs.querySelector(`[data-tab="${tabName}"]`);
      if (tab) {
        tab.classList.add('active');
        const target = document.getElementById('tab-' + tabName);
        if (target) target.classList.add('active');
        loadLibraryTab(tabName);
      }
    }

    function openSettingsTab(tabName) {
      if (settingsOverlay.classList.contains('hidden')) openSettings();
      document.querySelectorAll('.settings-tab').forEach(t => t.classList.remove('active'));
      document.querySelectorAll('.settings-tab-content').forEach(c => c.classList.remove('active'));
      const tab = document.querySelector(`.settings-tab[data-tab="${tabName}"]`);
      if (tab) {
        tab.classList.add('active');
        const target = document.getElementById('tab-' + tabName);
        if (target) target.classList.add('active');
        if (tabName === 'general') { loadBrowserSettings(); initTelegramToggle(); }
        else if (tabName === 'account') loadAccountProfiles();
        else if (tabName === 'mcp') loadMcpServers();
        else if (tabName === 'cookbook') { if (window._cbInit) window._cbInit(); }
        else if (tabName === 'tools') loadTools();
        else if (tabName === 'shortcuts') renderShortcutsTab();
      }
    }

    function openTrackNotePanel(mode) {
      if (!document.body.classList.contains('tracknote-open')) {
        document.body.classList.add('tracknote-open');
        document.body.classList.remove('diff-open');
        if (typeof AgentPanel !== 'undefined') AgentPanel.close();
      }
      setTrackNoteMode(mode);
    }

    const DEFAULT_SHORTCUTS = {
      'open-deep-research': { keys: 'Alt+Shift+R', label: 'Open Deep Research', action: () => openLibraryTab('lib-research') },
      'open-gallery': { keys: 'Alt+Shift+G', label: 'Open Gallery', action: () => openLibraryTab('lib-gallery') },
      'open-library': { keys: 'Alt+L', label: 'Open Library', action: () => openLibrary() },
      'open-memory': { keys: 'Alt+Shift+M', label: 'Open Memory', action: () => openSettingsTab('brain') },
      'open-notes': { keys: 'Alt+Shift+N', label: 'Open Notes', action: () => openLibraryTab('lib-notes') },
      'open-tasks': { keys: 'Alt+Shift+T', label: 'Open Tasks', action: () => openTrackNotePanel('todo') },
      'search-conversations': { keys: 'Alt+F', label: 'Search Conversations', action: () => { const btn = document.getElementById('chatSearchBtn'); if (btn) btn.click(); } },
      'toggle-sidebar': { keys: 'Alt+B', label: 'Toggle Sidebar', action: () => { const isMobile = window.matchMedia('(max-width: 860px)').matches; isMobile ? document.body.classList.toggle('sidebar-open') : document.body.classList.toggle('sidebar-collapsed'); } },
      'focus-input': { keys: 'Alt+I', label: 'Focus Chat Input', action: () => inputEl?.focus() },
      'new-session': { keys: 'Alt+N', label: 'New Session', action: () => createChat() },
      'delete-session': { keys: 'Alt+Shift+D', label: 'Delete Session', action: () => { if (activeChatId) deleteChat(activeChatId); } },
      'toggle-tts': { keys: 'Alt+Shift+S', label: 'Play/Stop TTS', action: () => { if (_ttsActive) stopGlobalTTS(); } },
      'cheat-sheet': { keys: 'Alt+/', label: 'Shortcut Cheat Sheet', action: () => toggleCheatSheet() },
      'open-agent-ops': { keys: 'Alt+Shift+A', label: 'Open Agent Ops', action: () => openTrackNotePanel('agent-tasks') },
      'open-schedules': { keys: 'Alt+Shift+E', label: 'Open Schedules', action: () => openTrackNotePanel('schedule') },
      'toggle-settings': { keys: 'Alt+,', label: 'Toggle Settings', action: () => settingsOverlay.classList.contains('hidden') ? openSettings() : closeSettings() },
    };

    function getShortcuts() {
      try {
        const stored = JSON.parse(localStorage.getItem(SHORTCUTS_STORAGE_KEY) || '{}');
        const merged = {};
        for (const [id, def] of Object.entries(DEFAULT_SHORTCUTS)) {
          merged[id] = { ...def, keys: stored[id]?.keys || def.keys };
        }
        return merged;
      } catch { return { ...DEFAULT_SHORTCUTS }; }
    }

    function saveShortcuts(shortcuts) {
      const overrides = {};
      for (const [id, sc] of Object.entries(shortcuts)) {
        if (sc.keys !== DEFAULT_SHORTCUTS[id]?.keys) overrides[id] = { keys: sc.keys };
      }
      localStorage.setItem(SHORTCUTS_STORAGE_KEY, JSON.stringify(overrides));
    }

    function parseKeys(keysStr) {
      const parts = keysStr.toLowerCase().split('+');
      return {
        ctrl: parts.includes('ctrl'),
        shift: parts.includes('shift'),
        alt: parts.includes('alt'),
        meta: parts.includes('meta'),
        key: parts.filter(p => !['ctrl','shift','alt','meta'].includes(p))[0] || '',
      };
    }

    function matchesEvent(parsed, e) {
      const key = e.key.toLowerCase();
      const keyMatch = parsed.key === key || (parsed.key === '/' && e.key === '/') || (parsed.key === 'escape' && e.key === 'Escape');
      return keyMatch && parsed.ctrl === (e.ctrlKey || e.metaKey) && parsed.shift === e.shiftKey && parsed.alt === e.altKey;
    }

    function isInputFocused() {
      const el = document.activeElement;
      if (!el) return false;
      const tag = el.tagName.toLowerCase();
      return tag === 'input' || tag === 'textarea' || tag === 'select' || el.isContentEditable;
    }

    // Global shortcut dispatcher
    document.addEventListener('keydown', (e) => {
      // Skip when recording a new shortcut
      if (document.querySelector('.shortcut-recording')) return;

      const shortcuts = getShortcuts();
      for (const [id, sc] of Object.entries(shortcuts)) {
        if (!sc.action) continue;
        const parsed = parseKeys(sc.keys);
        if (matchesEvent(parsed, e)) {
          // Allow Escape and cheat-sheet even in inputs
          if (isInputFocused() && id !== 'escape' && id !== 'cheat-sheet') continue;
          e.preventDefault();
          e.stopPropagation();
          sc.action();
          return;
        }
      }
    });

    // Cheat sheet overlay
    const cheatSheetOverlay = document.getElementById('cheatSheetOverlay');
    const cheatSheetClose = document.getElementById('cheatSheetClose');
    const cheatSheetList = document.getElementById('cheatSheetList');

    function toggleCheatSheet() {
      if (!cheatSheetOverlay) return;
      const isHidden = cheatSheetOverlay.classList.contains('hidden');
      if (isHidden) {
        renderCheatSheet();
        cheatSheetOverlay.classList.remove('hidden');
      } else {
        cheatSheetOverlay.classList.add('hidden');
      }
    }

    function renderCheatSheet() {
      if (!cheatSheetList) return;
      const shortcuts = getShortcuts();
      cheatSheetList.innerHTML = '';
      for (const [id, sc] of Object.entries(shortcuts)) {
        const row = document.createElement('div');
        row.style.cssText = 'display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-bottom:1px solid var(--border-light, rgba(255,255,255,0.06));';
        row.innerHTML = `<span style="font-size:13px;">${sc.label}</span><kbd style="background:var(--bg-secondary, #2a2a2a);padding:2px 8px;border-radius:4px;font-size:12px;font-family:monospace;border:1px solid var(--border, #333);">${sc.keys}</kbd>`;
        cheatSheetList.appendChild(row);
      }
    }

    if (cheatSheetClose) cheatSheetClose.addEventListener('click', () => cheatSheetOverlay?.classList.add('hidden'));
    if (cheatSheetOverlay) cheatSheetOverlay.addEventListener('click', (e) => { if (e.target === cheatSheetOverlay) cheatSheetOverlay.classList.add('hidden'); });

    // Shortcuts settings tab rendering
    function renderShortcutsTab() {
      const container = document.getElementById('shortcutsList');
      if (!container) return;
      const shortcuts = getShortcuts();
      container.innerHTML = '';
      for (const [id, sc] of Object.entries(shortcuts)) {
        const row = document.createElement('div');
        row.style.cssText = 'display:flex;align-items:center;justify-content:space-between;padding:8px 10px;background:var(--bg-secondary, rgba(255,255,255,0.03));border-radius:6px;border:1px solid var(--border, rgba(255,255,255,0.08));';
        
        const label = document.createElement('span');
        label.style.cssText = 'font-size:13px;flex:1;';
        label.textContent = sc.label;

        const keyBtn = document.createElement('button');
        keyBtn.className = 'icon-btn';
        keyBtn.style.cssText = 'min-width:100px;padding:4px 12px;font-size:12px;font-family:monospace;text-align:center;background:var(--bg-tertiary, rgba(255,255,255,0.06));border:1px solid var(--border, rgba(255,255,255,0.1));border-radius:4px;cursor:pointer;color:var(--text-primary, #fff);';
        keyBtn.textContent = sc.keys;
        keyBtn.title = 'Click to reassign';

        keyBtn.addEventListener('click', () => {
          keyBtn.textContent = 'Press keys...';
          keyBtn.classList.add('shortcut-recording');
          keyBtn.style.borderColor = 'var(--accent, #7c3aed)';
          
          function onRecord(e) {
            e.preventDefault();
            e.stopPropagation();
            if (e.key === 'Escape') {
              keyBtn.textContent = sc.keys;
              keyBtn.classList.remove('shortcut-recording');
              keyBtn.style.borderColor = '';
              document.removeEventListener('keydown', onRecord, true);
              return;
            }
            const parts = [];
            if (e.ctrlKey || e.metaKey) parts.push('Ctrl');
            if (e.shiftKey) parts.push('Shift');
            if (e.altKey) parts.push('Alt');
            const key = e.key.length === 1 ? e.key.toUpperCase() : e.key;
            if (!['Control','Shift','Alt','Meta'].includes(e.key)) parts.push(key);
            if (parts.length > 0 && !['Control','Shift','Alt','Meta'].includes(e.key)) {
              const newKeys = parts.join('+');
              shortcuts[id].keys = newKeys;
              saveShortcuts(shortcuts);
              keyBtn.textContent = newKeys;
              keyBtn.classList.remove('shortcut-recording');
              keyBtn.style.borderColor = '';
              document.removeEventListener('keydown', onRecord, true);
            }
          }
          document.addEventListener('keydown', onRecord, true);
        });

        row.appendChild(label);
        row.appendChild(keyBtn);
        container.appendChild(row);
      }
    }

    // Reset shortcuts button
    const shortcutsResetBtn = document.getElementById('shortcutsResetBtn');
    if (shortcutsResetBtn) {
      shortcutsResetBtn.addEventListener('click', () => {
        localStorage.removeItem(SHORTCUTS_STORAGE_KEY);
        renderShortcutsTab();
      });
    }

    // Tab switching (lazy-load per tab)
    document.querySelectorAll(".settings-tab").forEach((tab) => {
      tab.addEventListener("click", () => {
        document.querySelectorAll(".settings-tab").forEach((t) => t.classList.remove("active"));
        document.querySelectorAll(".settings-tab-content").forEach((c) => c.classList.remove("active"));
        tab.classList.add("active");
        const target = document.getElementById("tab-" + tab.dataset.tab);
        if (target) target.classList.add("active");
        const tabName = tab.dataset.tab;
        if (tabName === 'general') { loadBrowserSettings(); initTelegramToggle(); }
        else if (tabName === 'account') loadAccountProfiles();
        else if (tabName === 'mcp') loadMcpServers();
        else if (tabName === 'cookbook') { if (window._cbInit) window._cbInit(); }
        else if (tabName === 'personas') { if (window._personaInit) window._personaInit(); }
        else if (tabName === 'shortcuts') renderShortcutsTab();
      });
    });


    // ── Universal Save System ──────────────────────────────────────────────
    // Hides individual save buttons, tracks dirty state per tab,
    // shows floating "Save All" button when anything changed.
    const _universalSave = (() => {
      const bar = document.getElementById("universalSaveBar");
      const btn = document.getElementById("universalSaveBtn");
      const _tabs = {}; // tabName -> { saveFn, snapshotFn, dirty }

      // Hide all individual save buttons
      const hideIds = [
        "personalSaveBtn", "msSaveBtn",
        "searchSaveBtn", "cbSaveSettings", "agentConfigSave",
      ];
      hideIds.forEach(id => {
        const el = document.getElementById(id);
        if (el) el.style.display = "none";
      });

      function updateBar() {
        const anyDirty = Object.values(_tabs).some(t => t.dirty);
        if (anyDirty) bar?.classList.remove("hidden");
        else bar?.classList.add("hidden");
      }

      function markDirty(tabName) {
        if (_loading) return;
        if (_tabs[tabName]) { _tabs[tabName].dirty = true; updateBar(); }
      }

      let _loading = true; // suppress dirty during initial load
      setTimeout(() => { _loading = false; }, 2000);

      function register(tabName, saveFn, snapshotFn) {
        _tabs[tabName] = { saveFn, snapshotFn, dirty: false, lastSnapshot: null };
        if (snapshotFn) {
          _tabs[tabName].lastSnapshot = snapshotFn();
        }
      }

      async function saveAll() {
        if (!btn) return;
        btn.disabled = true;
        btn.textContent = "⏳ Saving…";
        const errors = [];
        for (const [name, tab] of Object.entries(_tabs)) {
          if (!tab.dirty) continue;
          try {
            await tab.saveFn();
            tab.dirty = false;
            if (tab.snapshotFn) tab.lastSnapshot = tab.snapshotFn();
          } catch (e) {
            errors.push(`${name}: ${e.message}`);
          }
        }
        updateBar();
        if (errors.length) showToast("Some saves failed: " + errors.join(", "), true);
        else showToast("✅ All settings saved");
        btn.disabled = false;
        btn.textContent = "💾 Save All";
      }

      btn?.addEventListener("click", saveAll);

      // Auto-detect changes via input/change events on settings body
      const settingsBody = document.querySelector(".settings-body");
      if (settingsBody) {
        settingsBody.addEventListener("input", (e) => {
          if (e.target?.dataset?.noSaveTrack) return;
          const activeTab = document.querySelector(".settings-tab.active")?.dataset.tab;
          if (activeTab && _tabs[activeTab]) markDirty(activeTab);
        });
        settingsBody.addEventListener("change", (e) => {
          if (e.target?.dataset?.noSaveTrack) return;
          const activeTab = document.querySelector(".settings-tab.active")?.dataset.tab;
          if (activeTab && _tabs[activeTab]) markDirty(activeTab);
        });
      }

      return { register, markDirty, saveAll };
    })();
    window._universalSave = _universalSave;

    // Horizontal scroll on mouse wheel for settings tab bars
    document.querySelectorAll(".settings-tabs").forEach((bar) => {
      bar.addEventListener("wheel", (e) => {
        if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) {
          e.preventDefault();
          bar.scrollLeft += e.deltaY;
        }
      }, { passive: false });
    });


    logClear.addEventListener("click", () => { logViewer.textContent = ""; });

