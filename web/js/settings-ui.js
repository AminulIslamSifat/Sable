    /* ---------- Settings & Live Logs ---------- */
    const settingsOverlay = document.getElementById("settingsOverlay");
    const settingsBtn = document.getElementById("railSettingsBtn") || document.getElementById("settingsBtn");
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
        if (tabName === 'general') { loadBrowserSettings(); }
        else if (tabName === 'account') loadAccountProfiles();
        else if (tabName === 'mcp') loadMcpServers();
        else if (tabName === 'cookbook') { if (window._cbInit) window._cbInit(); }
        else if (tabName === 'tools') loadTools();
        else if (tabName === 'shortcuts') renderShortcutsTab();
        else if (tabName === 'updates') loadUpdatesTab();
      }
    }

    function openTodoPanel(mode = 'todo') {
      const panelName = mode === 'agent-tasks' ? 'tasks' : 'todo';
      if (window.sidebarHost) {
        window.sidebarHost.openPanel(panelName);
      }
    }

    const DEFAULT_SHORTCUTS = {
      'open-deep-research': { keys: 'Alt+Shift+R', label: 'Open Deep Research', action: () => openLibraryTab('lib-research') },
      'open-gallery': { keys: 'Alt+Shift+G', label: 'Open Gallery', action: () => openLibraryTab('lib-gallery') },
      'open-library': { keys: 'Alt+L', label: 'Open Library', action: () => openLibrary() },
      'open-memory': { keys: 'Alt+Shift+M', label: 'Open Memory', action: () => openSettingsTab('brain') },
      'open-notes': { keys: 'Alt+Shift+N', label: 'Open Notes', action: () => openLibraryTab('lib-notes') },
      'open-prompts': { keys: 'Alt+Shift+P', label: 'Open Prompts', action: () => openLibraryTab('lib-prompts') },
      'open-tasks': { keys: 'Alt+Shift+T', label: 'Open Todo', action: () => openTodoPanel('todo') },
      'search-conversations': { keys: 'Alt+F', label: 'Search Conversations', action: () => { const btn = document.getElementById('chatSearchBtn'); if (btn) btn.click(); } },
      'toggle-sidebar': { keys: 'Alt+B', label: 'Toggle Sidebar', action: () => { const isMobile = window.matchMedia('(max-width: 860px)').matches; isMobile ? document.body.classList.toggle('sidebar-open') : document.body.classList.toggle('sidebar-collapsed'); } },
      'focus-input': { keys: 'Alt+I', label: 'Focus Chat Input', action: () => inputEl?.focus() },
      'new-session': { keys: 'Alt+N', label: 'New Session', action: () => createChat() },
      'delete-session': { keys: 'Alt+Shift+D', label: 'Delete Session', action: () => { if (activeChatId) deleteChat(activeChatId); } },
      'toggle-tts': { keys: 'Alt+Shift+S', label: 'Play/Stop TTS', action: () => { if (_ttsActive) stopGlobalTTS(); } },
      'cheat-sheet': { keys: 'Alt+/', label: 'Shortcut Cheat Sheet', action: () => toggleCheatSheet() },
      'open-agent-ops': { keys: 'Alt+Shift+A', label: 'Open Tasks', action: () => openTodoPanel('agent-tasks') },
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
        if (tabName === 'general') { loadBrowserSettings(); }
        else if (tabName === 'account') loadAccountProfiles();
        else if (tabName === 'mcp') loadMcpServers();
        else if (tabName === 'cookbook') { if (window._cbInit) window._cbInit(); }
        else if (tabName === 'personas') { if (window._personaInit) window._personaInit(); }
        else if (tabName === 'shortcuts') renderShortcutsTab();
        else if (tabName === 'updates') loadUpdatesTab();
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

    // Horizontal scroll on mouse wheel for HORIZONTAL tab bars only.
    // Skip .settings-tabs which is a vertical sidebar (flex-direction: column).
    document.querySelectorAll(".settings-tabs").forEach((bar) => {
      const style = getComputedStyle(bar);
      if (style.flexDirection === 'column') return; // vertical layout — let native scroll work
      bar.addEventListener("wheel", (e) => {
        if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) {
          e.preventDefault();
          bar.scrollLeft += e.deltaY;
        }
      }, { passive: false });
    });


    logClear.addEventListener("click", () => { logViewer.textContent = ""; });
/* ---------- Software Update System ---------- */
let _updateData = null;
let _updateCheckTimer = null;

function _setUpdateDot(visible) {
  const btn = document.getElementById("railSettingsBtn") || document.getElementById("settingsBtn");
  if (btn) {
    let dot = btn.querySelector(".update-dot");
    if (visible && !dot) {
      dot = document.createElement("span");
      dot.className = "update-dot";
      btn.appendChild(dot);
    } else if (!visible && dot) {
      dot.remove();
    }
  }
  // Red glow on the Updates tab itself
  const updatesTab = document.querySelector('.settings-tab[data-tab="updates"]');
  if (updatesTab) {
    updatesTab.classList.toggle("has-update", visible);
  }
}

function _getAuthHeader() {
  const token = localStorage.getItem("sable_token");
  return token ? { Authorization: "Bearer " + token } : {};
}

async function checkForUpdates(force = false) {
  try {
    const url = force ? "/api/update/check?force=true" : "/api/update/check";
    const resp = await fetch(url, { headers: _getAuthHeader() });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    _updateData = await resp.json();
    _setUpdateDot(_updateData.update_available);
    return _updateData;
  } catch (e) {
    console.warn("Update check failed:", e);
    return null;
  }
}

function renderUpdateInfo(data) {
  const localEl = document.getElementById("updateLocalVersion");
  const remoteEl = document.getElementById("updateRemoteVersion");
  const changelog = document.getElementById("updateChangelog");
  const changelogBody = document.getElementById("updateChangelogBody");
  const applyBtn = document.getElementById("updateApplyBtn");
  const msg = document.getElementById("updateMessage");

  if (!data) {
    if (msg) msg.textContent = "Failed to check for updates.";
    return;
  }

  if (localEl) localEl.textContent = "v" + data.local_version;
  if (remoteEl) remoteEl.textContent = "v" + data.remote_version;

  if (data.update_available) {
    if (changelog && data.changelog) {
      changelog.classList.remove("hidden");
      if (changelogBody) changelogBody.textContent = data.changelog;
    }
    if (applyBtn) applyBtn.classList.remove("hidden");
    if (msg) msg.textContent = `Update v${data.remote_version} available! (${data.published_at ? new Date(data.published_at).toLocaleDateString() : ""})`;
  } else {
    if (changelog) changelog.classList.add("hidden");
    if (applyBtn) applyBtn.classList.add("hidden");
    if (msg) msg.textContent = "You're on the latest version ✨";
  }
}

function loadUpdatesTab() {
  renderUpdateInfo(_updateData);
}

async function applyUpdate() {
  const progress = document.getElementById("updateProgress");
  const progressBar = document.getElementById("updateProgressBar");
  const progressText = document.getElementById("updateProgressText");
  const applyBtn = document.getElementById("updateApplyBtn");
  const checkBtn = document.getElementById("updateCheckBtn");
  const msg = document.getElementById("updateMessage");

  if (!confirm("Update Sable to the latest version? The app will restart briefly.")) return;

  if (applyBtn) { applyBtn.disabled = true; applyBtn.textContent = "Updating…"; }
  if (checkBtn) checkBtn.disabled = true;
  if (progress) progress.classList.remove("hidden");
  if (progressBar) progressBar.style.width = "10%";
  if (progressText) progressText.textContent = "Starting update…";

  try {
    const resp = await fetch("/api/update/apply", {
      method: "POST",
      headers: _getAuthHeader(),
    });
    if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`);

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    const stepProgress = { check: 10, pull: 35, sync: 65, restart: 90 };

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      const lines = buffer.split("\n");
      buffer = lines.pop() || "";

      for (const line of lines) {
        if (!line.startsWith("data: ")) continue;
        try {
          const event = JSON.parse(line.slice(6));

          if (event.type === "progress") {
            const pct = stepProgress[event.step] || 50;
            if (progressBar) progressBar.style.width = pct + "%";
            if (progressText) progressText.textContent = event.message;
          } else if (event.type === "warning") {
            if (progressText) progressText.textContent = event.message;
          } else if (event.type === "error") {
            if (progressText) progressText.textContent = "❌ " + event.message;
            if (progressBar) progressBar.style.width = "100%";
            if (progressBar) progressBar.style.background = "#ef4444";
            if (applyBtn) { applyBtn.disabled = false; applyBtn.textContent = "⬆ Update Now"; }
            if (checkBtn) checkBtn.disabled = false;
            return;
          } else if (event.type === "done") {
            if (progressBar) progressBar.style.width = "100%";
            if (progressText) progressText.textContent = "✅ Update complete! Reloading…";
            if (msg) msg.textContent = "Restarting… page will reload in a few seconds.";
            _setUpdateDot(false);
            // Wait for service to restart then reload
            setTimeout(() => location.reload(), 5000);
            return;
          }
        } catch {}
      }
    }
  } catch (e) {
    if (progressText) progressText.textContent = "❌ Update failed: " + e.message;
    if (applyBtn) { applyBtn.disabled = false; applyBtn.textContent = "⬆ Update Now"; }
    if (checkBtn) checkBtn.disabled = false;
  }
}

// Wire up buttons
document.getElementById("updateCheckBtn")?.addEventListener("click", async () => {
  const btn = document.getElementById("updateCheckBtn");
  if (btn) { btn.disabled = true; btn.textContent = "Checking…"; }
  const data = await checkForUpdates(true);
  renderUpdateInfo(data);
  if (btn) { btn.disabled = false; btn.textContent = "Check for Updates"; }
});

document.getElementById("updateApplyBtn")?.addEventListener("click", applyUpdate);

// Auto-check on load + every 30 min
(async () => {
  await checkForUpdates(false);
  _updateCheckTimer = setInterval(() => checkForUpdates(false), 30 * 60 * 1000);
})();

