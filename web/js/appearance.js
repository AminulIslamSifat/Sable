    // ---------- Safe Clipboard Copy (works on non-HTTPS / Windows HTTP) ----------
window.safeCopy = async function(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {}
  // Fallback for non-secure contexts (e.g. http://192.168.x.x on Windows)
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.cssText = 'position:fixed;left:-9999px;top:-9999px;opacity:0';
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand('copy'); } catch {}
  document.body.removeChild(ta);
  return ok;
};

// ---------- Account Profile Switcher ----------
    const accountProfileCards = document.getElementById("accountProfileCards");
    const refreshAccountsBtn = document.getElementById("refreshAccountsBtn");
    const addAccountBrowserSelect = document.getElementById("addAccountBrowserSelect");

    async function loadAvailableBrowsers() {
      if (!addAccountBrowserSelect) return;
      try {
        const res = await fetch("/api/settings/accounts/available-browsers");
        const data = await res.json();
        const browsers = data.browsers || [];
        addAccountBrowserSelect.innerHTML = browsers.map(b =>
          `<option value="${b.path.replace(/"/g, '&quot;')}">${b.name} (${b.type})</option>`
        ).join("");
        // Restore last selected browser from localStorage
        const lastBrowser = localStorage.getItem("sable_last_browser") || "default";
        addAccountBrowserSelect.value = lastBrowser;
        // If saved value doesn't exist in options, fall back to first
        if (addAccountBrowserSelect.selectedIndex === -1 && browsers.length) {
          addAccountBrowserSelect.selectedIndex = 0;
        }
      } catch (e) {
        addAccountBrowserSelect.innerHTML = '<option value="default">Default (Auto-detect)</option>';
      }
    }

    // Expose to window so other JS files (settings-ui, skills-panel) can call these on tab switch
    window.loadAvailableBrowsers = loadAvailableBrowsers;
    window.loadAccountProfiles = loadAccountProfiles;

    // Track which provider is currently selected in the filter bar
    let _activeProviderFilter = localStorage.getItem("sable_provider_filter") || "all";

    // Helper: render a toggle switch HTML
    function _renderToggle(id, enabled, labelText, descText) {
      return `<div style="display:flex;align-items:center;justify-content:space-between;background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:10px 14px;margin-bottom:10px;">
        <div style="min-width:0;">
          <div style="font-size:12px;font-weight:600;color:var(--text);">${labelText}</div>
          ${descText ? `<div style="font-size:11px;color:var(--text-dim);margin-top:2px;">${descText}</div>` : ''}
        </div>
        <label style="position:relative;display:inline-block;width:36px;height:20px;flex-shrink:0;cursor:pointer;">
          <input type="checkbox" id="${id}" ${enabled ? 'checked' : ''} style="opacity:0;width:0;height:0;">
          <span style="position:absolute;inset:0;background:${enabled ? 'var(--accent)' : 'var(--border)'};border-radius:20px;transition:0.2s;"></span>
          <span style="position:absolute;left:${enabled ? '18px' : '2px'};top:2px;width:16px;height:16px;background:#fff;border-radius:50%;transition:0.2s;"></span>
        </label>
      </div>`;
    }

    // Helper: bind toggle visual + API call
    function _bindProviderToggle(toggleId, provider) {
      const el = document.getElementById(toggleId);
      if (!el) return;
      el.addEventListener("change", async () => {
        const enabled = el.checked;
        const label = el.closest("label");
        if (label) {
          const bg = label.children[1], knob = label.children[2];
          if (bg) bg.style.background = enabled ? "var(--accent)" : "var(--border)";
          if (knob) knob.style.left = enabled ? "18px" : "2px";
        }
        try {
          await fetch(`/api/settings/accounts/auto-switch-toggle/${provider}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ enabled }),
          });
          showToast(enabled ? `✅ ${provider} auto-switch on` : `⏸️ ${provider} auto-switch off`, enabled ? "success" : "info");
        } catch (e) {
          showToast("Failed: " + e.message, "error");
          el.checked = !enabled;
          if (label) {
            const bg = label.children[1], knob = label.children[2];
            if (bg) bg.style.background = enabled ? "var(--border)" : "var(--accent)";
            if (knob) knob.style.left = enabled ? "2px" : "18px";
          }
        }
      });
    }

    // Helper: badge
    const _badge = (txt, warn) => `<span style="font-size:10px;color:${warn ? 'var(--danger)' : 'var(--text-dim)'};border:1px solid ${warn ? 'var(--danger)' : 'var(--border)'};border-radius:4px;padding:1px 5px;">${txt}</span>`;

    // Render browser account card (for Qwen/DeepSeek)
    function _renderBrowserCard(acc, active) {
      const isActive = acc.name === active;
      const email = acc.label || acc.email || "unknown account";
      const size = acc.size_mb ? acc.size_mb + " MB" : "";
      const browserMissing = acc.browser_path && acc.browser_path !== 'default' && acc.browser_available === false;
      const borderColor = browserMissing ? 'var(--danger)' : isActive ? '#4ade80' : 'var(--border)';
      const hasBackup = acc.has_backup;
      return `<div style="background:var(--panel);border:${isActive ? '2px' : '1px'} solid ${borderColor};border-radius:10px;padding:${isActive ? '13px 17px' : '14px 18px'};display:flex;flex-direction:column;gap:10px;">
        <div style="min-width:0;">
          <div style="font-size:13px;font-weight:600;color:var(--text);">${email}</div>
          <div style="font-size:11px;color:var(--text-dim);margin-top:4px;display:flex;flex-wrap:wrap;align-items:center;gap:5px;">
            <span>${acc.name}</span>
            ${size ? `<span style="opacity:0.4;">·</span><span>${size}</span>` : ''}
            ${isActive ? '<span style="color:var(--accent);">● active</span>' : ''}
            ${acc.browser_label ? _badge(acc.browser_label + (browserMissing ? ' !' : ''), browserMissing) : ''}
            ${acc.has_waf ? _badge('qwen') : ''}
            ${acc.has_ds ? _badge('ds') : ''}
            ${acc.exhausted ? _badge('exhausted', true) : ''}
            ${acc.captcha_blocked ? _badge('captcha', true) : ''}
            ${hasBackup ? _badge('backup') : ''}
          </div>
        </div>
        <div style="display:flex;gap:5px;align-items:center;flex-wrap:wrap;border-top:1px solid var(--border);padding-top:8px;">
          <button class="icon-btn account-backup-btn" data-profile="${acc.name}" style="width:auto;padding:4px 8px;font-size:11px;white-space:nowrap;">Backup</button>
          ${hasBackup ? `<button class="icon-btn account-restore-btn" data-profile="${acc.name}" style="width:auto;padding:4px 8px;font-size:11px;white-space:nowrap;">Restore</button>` : ''}
          <button class="icon-btn account-rename-btn" data-profile="${acc.name}" data-current-label="${(acc.label || acc.email || '').replace(/"/g, '&quot;')}" style="width:auto;padding:4px 8px;font-size:11px;white-space:nowrap;">Rename</button>
          <button class="icon-btn account-open-btn" data-profile="${acc.name}" style="width:auto;padding:4px 8px;font-size:11px;white-space:nowrap;">Open</button>
          <div style="flex:1;"></div>
          ${isActive ? '' : `<button class="icon-btn account-switch-btn" data-profile="${acc.name}" style="width:auto;padding:4px 8px;font-size:11px;white-space:nowrap;">Switch</button>`}
          ${isActive ? '' : `<button class="icon-btn account-delete-btn" data-profile="${acc.name}" style="width:auto;padding:4px 8px;font-size:11px;white-space:nowrap;color:var(--danger);">Delete</button>`}
        </div>
      </div>`;
    }

    // Render API key card (for Gemini, Groq, etc.)
    function _renderApiKeyCard(key, provider) {
      const isActive = key.active;
      const borderColor = isActive ? '#4ade80' : 'var(--border)';
      return `<div style="background:var(--panel);border:${isActive ? '2px' : '1px'} solid ${borderColor};border-radius:10px;padding:${isActive ? '13px 17px' : '14px 18px'};display:flex;flex-direction:column;gap:10px;">
        <div style="min-width:0;">
          <div style="font-size:13px;font-weight:600;color:var(--text);font-family:var(--font-mono);">${key.masked}</div>
          <div style="font-size:11px;color:var(--text-dim);margin-top:4px;display:flex;flex-wrap:wrap;align-items:center;gap:5px;">
            <span>Key #${key.index}</span>
            ${isActive ? '<span style="color:var(--accent);">● active</span>' : ''}
          </div>
        </div>
        <div style="display:flex;gap:5px;align-items:center;flex-wrap:wrap;border-top:1px solid var(--border);padding-top:8px;">
          <button class="icon-btn apikey-copy-btn" data-masked="${key.masked}" style="width:auto;padding:4px 8px;font-size:11px;white-space:nowrap;">Copy</button>
          <div style="flex:1;"></div>
          ${isActive ? '' : `<button class="icon-btn apikey-switch-btn" data-provider="${provider}" data-index="${key.index}" style="width:auto;padding:4px 8px;font-size:11px;white-space:nowrap;">Switch</button>`}
          <button class="icon-btn apikey-delete-btn" data-provider="${provider}" data-index="${key.index}" style="width:auto;padding:4px 8px;font-size:11px;white-space:nowrap;color:var(--danger);">Delete</button>
        </div>
      </div>`;
    }

    // Bind all browser-account handlers (preserved from original)
    function _bindBrowserHandlers() {
      accountProfileCards.querySelectorAll(".account-switch-btn").forEach((btn) => {
        btn.addEventListener("click", async () => {
          const profile = btn.dataset.profile;
          btn.disabled = true; btn.textContent = "Switching…";
          showToast("🔄 Switching account profile…", "info");
          try {
            const res = await fetch("/api/settings/accounts/switch", {
              method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ profile }),
            });
            const data = await res.json().catch(() => ({}));
            if (res.ok) { showToast(`✅ Switched to ${data.email || profile}`, "success"); await Promise.all([loadAccountProfiles(), loadModels()]); }
            else { showToast("Switch failed: " + (data.detail || "unknown"), "error"); btn.disabled = false; btn.textContent = "Switch"; }
          } catch (e) { showToast("Switch error: " + e.message, "error"); btn.disabled = false; btn.textContent = "Switch"; }
        });
      });
      accountProfileCards.querySelectorAll(".account-delete-btn").forEach((btn) => {
        btn.addEventListener("click", async () => {
          const profile = btn.dataset.profile;
          if (!await sableConfirm(`Delete ${profile}?\n\nThis permanently removes the browser data directory.`, { danger: true })) return;
          btn.disabled = true; btn.textContent = "Deleting…";
          try {
            const res = await fetch("/api/settings/accounts/delete", { method: "DELETE", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ profile }) });
            const data = await res.json().catch(() => ({}));
            if (res.ok) { showToast(`🗑️ Deleted ${profile}`, "success"); await loadAccountProfiles(); }
            else { showToast("Delete failed: " + (data.detail || "unknown"), "error"); btn.disabled = false; btn.textContent = "Delete"; }
          } catch (e) { showToast("Delete error: " + e.message, "error"); btn.disabled = false; btn.textContent = "Delete"; }
        });
      });
      accountProfileCards.querySelectorAll(".account-rename-btn").forEach((btn) => {
        btn.addEventListener("click", async () => {
          const profile = btn.dataset.profile, currentLabel = btn.dataset.currentLabel || "";
          const newLabel = await sablePrompt("Display name for " + profile + ":", currentLabel);
          if (newLabel === null) return;
          btn.disabled = true; btn.textContent = "Saving…";
          try {
            const res = await fetch("/api/settings/accounts/rename", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ profile, label: newLabel }) });
            const data = await res.json().catch(() => ({}));
            if (res.ok) { showToast(newLabel ? `✅ Renamed to "${newLabel}"` : `✅ Cleared custom name`, "success"); await loadAccountProfiles(); }
            else { showToast("Rename failed: " + (data.detail || "unknown"), "error"); btn.disabled = false; btn.textContent = "Rename"; }
          } catch (e) { showToast("Rename error: " + e.message, "error"); btn.disabled = false; btn.textContent = "Rename"; }
        });
      });
      accountProfileCards.querySelectorAll(".account-open-btn").forEach((btn) => {
        btn.addEventListener("click", async () => {
          const profile = btn.dataset.profile; btn.disabled = true; btn.textContent = "Opening…";
          try {
            const res = await fetch("/api/settings/accounts/open", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ profile }) });
            const data = await res.json().catch(() => ({}));
            if (res.ok) showToast(`🌐 Opened browser for ${profile}`, "success");
            else showToast("Open failed: " + (data.detail || "unknown"), "error");
          } catch (e) { showToast("Open error: " + e.message, "error"); }
          btn.disabled = false; btn.textContent = "Open";
        });
      });
      accountProfileCards.querySelectorAll(".account-backup-btn").forEach((btn) => {
        btn.addEventListener("click", async () => {
          const profile = btn.dataset.profile, origText = btn.innerHTML; btn.disabled = true; btn.innerHTML = "⏳…";
          try {
            const res = await fetch("/api/settings/accounts/backup", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ profile }) });
            const data = await res.json().catch(() => ({}));
            if (res.ok) { showToast(`Backed up ${profile}`, "success"); await loadAccountProfiles(); }
            else { showToast("Backup failed: " + (data.detail || "unknown"), "error"); btn.disabled = false; btn.innerHTML = origText; }
          } catch (e) { showToast("Backup error: " + e.message, "error"); btn.disabled = false; btn.innerHTML = origText; }
        });
      });
      accountProfileCards.querySelectorAll(".account-restore-btn").forEach((btn) => {
        btn.addEventListener("click", async () => {
          const profile = btn.dataset.profile;
          if (!await sableConfirm(`Restore ${profile} from backup?\n\nThis will replace the current profile data with the .bak copy.`, { danger: true })) return;
          const origText = btn.innerHTML; btn.disabled = true; btn.innerHTML = "⏳…";
          try {
            const res = await fetch("/api/settings/accounts/restore", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ profile }) });
            const data = await res.json().catch(() => ({}));
            if (res.ok) { showToast(`Restored ${profile}`, "success"); await loadAccountProfiles(); }
            else { showToast("Restore failed: " + (data.detail || "unknown"), "error"); btn.disabled = false; btn.innerHTML = origText; }
          } catch (e) { showToast("Restore error: " + e.message, "error"); btn.disabled = false; btn.innerHTML = origText; }
        });
      });
    }

    // Bind API key handlers
    function _bindApiKeyHandlers() {
      accountProfileCards.querySelectorAll(".apikey-switch-btn").forEach((btn) => {
        btn.addEventListener("click", async () => {
          const provider = btn.dataset.provider, index = parseInt(btn.dataset.index);
          btn.disabled = true; btn.textContent = "Switching…";
          try {
            const res = await fetch(`/api/settings/${provider}/switch-key`, {
              method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ index }),
            });
            const data = await res.json().catch(() => ({}));
            if (res.ok) { showToast(`✅ Switched ${provider} key`, "success"); await loadAccountProfiles(); }
            else { showToast("Switch failed: " + (data.detail || "unknown"), "error"); btn.disabled = false; btn.textContent = "Switch"; }
          } catch (e) { showToast("Switch error: " + e.message, "error"); btn.disabled = false; btn.textContent = "Switch"; }
        });
      });
      accountProfileCards.querySelectorAll(".apikey-delete-btn").forEach((btn) => {
        btn.addEventListener("click", async () => {
          const provider = btn.dataset.provider, index = parseInt(btn.dataset.index);
          if (!await sableConfirm(`Delete this ${provider} API key?`, { danger: true })) return;
          btn.disabled = true; btn.textContent = "Deleting…";
          try {
            const res = await fetch(`/api/settings/${provider}/api-key/${index}`, { method: "DELETE" });
            const data = await res.json().catch(() => ({}));
            if (res.ok) { showToast(`🗑️ Deleted ${provider} key`, "success"); await loadAccountProfiles(); }
            else { showToast("Delete failed: " + (data.detail || "unknown"), "error"); btn.disabled = false; btn.textContent = "Delete"; }
          } catch (e) { showToast("Delete error: " + e.message, "error"); btn.disabled = false; btn.textContent = "Delete"; }
        });
      });
      accountProfileCards.querySelectorAll(".apikey-copy-btn").forEach((btn) => {
        btn.addEventListener("click", () => {
          safeCopy(btn.dataset.masked);
          showToast("Copied masked key", "info");
        });
      });
    }

    async function loadAccountProfiles() {
      if (!accountProfileCards) return;
      const filterBar = document.getElementById("providerFilterBar");
      accountProfileCards.innerHTML = '<p class="muted" style="font-size:12px;margin:0;">Loading accounts…</p>';
      try {
        const res = await fetch("/api/settings/accounts/by-provider");
        if (!res.ok) throw new Error("HTTP " + res.status);
        const data = await res.json();
        const providers = data.providers || {};
        const activeAccount = data.active_account;

        // Provider order
        const provOrder = ["qwen", "deepseek", "gemini", "groq", "mistral", "openai", "puter", "cloudflare"];
        const provIds = provOrder.filter(p => providers[p]);

        // Validate saved filter
        if (_activeProviderFilter !== "all" && !provIds.includes(_activeProviderFilter)) {
          _activeProviderFilter = "all";
        }

        // Render provider filter buttons
        if (filterBar) {
          const allCount = provIds.reduce((sum, p) => {
            const pd = providers[p];
            if (pd.type === "browser") return sum + (pd.accounts || []).length;
            if (pd.type === "api_key") return sum + (pd.keys || []).length;
            return sum + (pd.available ? 1 : 0);
          }, 0);
          let btns = `<button class="provider-filter-btn" data-provider="all" style="padding:4px 12px;font-size:11px;border-radius:6px;border:1px solid ${_activeProviderFilter === 'all' ? 'var(--accent)' : 'var(--border)'};background:${_activeProviderFilter === 'all' ? 'var(--accent-dim)' : 'var(--panel)'};color:${_activeProviderFilter === 'all' ? 'var(--accent-text)' : 'var(--text-dim)'};cursor:pointer;font-weight:${_activeProviderFilter === 'all' ? '600' : '400'};">All (${allCount})</button>`;
          for (const pid of provIds) {
            const pd = providers[pid];
            const count = pd.type === "browser" ? (pd.accounts || []).length : pd.type === "api_key" ? (pd.keys || []).length : (pd.available ? 1 : 0);
            const active = _activeProviderFilter === pid;
            btns += `<button class="provider-filter-btn" data-provider="${pid}" style="padding:4px 12px;font-size:11px;border-radius:6px;border:1px solid ${active ? 'var(--accent)' : 'var(--border)'};background:${active ? 'var(--accent-dim)' : 'var(--panel)'};color:${active ? 'var(--accent-text)' : 'var(--text-dim)'};cursor:pointer;font-weight:${active ? '600' : '400'};">${pd.label} (${count})</button>`;
          }
          filterBar.innerHTML = btns;
          filterBar.querySelectorAll(".provider-filter-btn").forEach(btn => {
            btn.addEventListener("click", () => {
              _activeProviderFilter = btn.dataset.provider;
              try { localStorage.setItem("sable_provider_filter", _activeProviderFilter); } catch(e) {}
              loadAccountProfiles();
            });
          });
        }

        // Filter providers to show
        const showProvs = _activeProviderFilter === "all" ? provIds : provIds.filter(p => p === _activeProviderFilter);

        if (!showProvs.length) {
          accountProfileCards.innerHTML = '<p class="muted" style="font-size:12px;margin:0;">No providers configured.</p>';
          return;
        }

        let html = "";
        for (const pid of showProvs) {
          const pd = providers[pid];
          // Section header
          html += `<div style="font-size:12px;font-weight:600;color:var(--text);margin-top:8px;margin-bottom:4px;display:flex;align-items:center;gap:6px;"><span>${pd.label}</span><span style="font-size:10px;color:var(--text-dim);font-weight:400;">(${pd.type === 'browser' ? 'browser profiles' : pd.type === 'api_key' ? 'API keys' : 'credentials'})</span></div>`;

          if (pd.type === "browser") {
            // Auto-switch toggle for this provider
            html += _renderToggle(`autoSwitch_${pid}`, pd.auto_switch_enabled,
              `Auto-Switch on Rate Limit / Captcha`,
              `Automatically switch to another ${pd.label} account when blocked.`);
            const accounts = pd.accounts || [];
            if (!accounts.length) {
              html += '<p class="muted" style="font-size:12px;margin:0 0 8px 0;">No accounts with ' + pd.label + ' tokens found.</p>';
            } else {
              html += accounts.map(acc => _renderBrowserCard(acc, pd.active)).join("");
            }
          } else if (pd.type === "api_key") {
            html += _renderToggle(`autoSwitch_${pid}`, pd.auto_switch_enabled,
              `Auto-Rotate on Error`,
              `Automatically rotate to next ${pd.label} key when one fails.`);
            const keys = pd.keys || [];
            if (!keys.length) {
              html += '<p class="muted" style="font-size:12px;margin:0 0 8px 0;">No API keys configured. Add keys from the Providers tab.</p>';
            } else {
              html += keys.map(k => _renderApiKeyCard(k, pid)).join("");
            }
          } else if (pd.type === "credential") {
            html += _renderToggle(`autoSwitch_${pid}`, pd.auto_switch_enabled,
              `Auto-Switch`, `Toggle auto-switch for ${pd.label}.`);
            if (pd.available) {
              html += `<div style="background:var(--panel);border:1px solid #4ade80;border-radius:10px;padding:14px 18px;font-size:12px;color:var(--text);"><span style="color:#4ade80;">● Configured</span></div>`;
            } else {
              html += '<p class="muted" style="font-size:12px;margin:0 0 8px 0;">Not configured. Set up from the Providers tab.</p>';
            }
          }
        }

        accountProfileCards.innerHTML = html;

        // Bind per-provider toggles
        for (const pid of showProvs) {
          _bindProviderToggle(`autoSwitch_${pid}`, pid);
        }

        // Bind browser account handlers + API key handlers
        _bindBrowserHandlers();
        _bindApiKeyHandlers();

      } catch (e) {
        accountProfileCards.innerHTML = `<p class="muted" style="font-size:12px;margin:0;color:var(--danger);">Failed to load: ${e.message}</p>`;
      }
    }

    if (refreshAccountsBtn) {
      refreshAccountsBtn.addEventListener("click", loadAccountProfiles);
    }

    // Backup All / Restore All header buttons
    const backupAllBtn = document.getElementById("backupAllAccountsBtn");
    if (backupAllBtn) {
      backupAllBtn.addEventListener("click", async () => {
        if (!await sableConfirm("Backup ALL account profiles?\n\nThis creates/replaces .bak directories for every account.")) return;
        backupAllBtn.disabled = true;
        backupAllBtn.textContent = "⏳ Backing up…";
        try {
          const res = await fetch("/api/settings/accounts/backup-all", { method: "POST" });
          const data = await res.json().catch(() => ({}));
          if (res.ok) {
            showToast(`Backed up ${data.count} account(s)`, "success");
            await loadAccountProfiles();
          } else {
            showToast("Backup all failed: " + (data.detail || "unknown"), "error");
          }
        } catch (e) {
          showToast("Backup error: " + e.message, "error");
        }
        backupAllBtn.disabled = false;
        backupAllBtn.textContent = 'Backup All';
      });
    }

    const restoreAllBtn = document.getElementById("restoreAllAccountsBtn");
    if (restoreAllBtn) {
      restoreAllBtn.addEventListener("click", async () => {
        if (!await sableConfirm("Restore ALL account profiles from backups?\n\nThis replaces current profile data with .bak copies.\nThe active account will be skipped.", { danger: true })) return;
        restoreAllBtn.disabled = true;
        restoreAllBtn.textContent = "⏳ Restoring…";
        try {
          const res = await fetch("/api/settings/accounts/restore-all", { method: "POST" });
          const data = await res.json().catch(() => ({}));
          if (res.ok) {
            const msg = `Restored ${data.restored.length} account(s)` + (data.skipped.length ? ` (skipped active: ${data.skipped.join(', ')})` : '');
            showToast(msg, "success");
            await loadAccountProfiles();
          } else {
            showToast("Restore all failed: " + (data.detail || "unknown"), "error");
          }
        } catch (e) {
          showToast("Restore error: " + e.message, "error");
        }
        restoreAllBtn.disabled = false;
        restoreAllBtn.textContent = 'Restore All';
      });
    }


    const addAccountBtn = document.getElementById("addAccountBtn");
    if (addAccountBtn) {
      addAccountBtn.addEventListener("click", async () => {
        addAccountBtn.disabled = true;
        addAccountBtn.textContent = "Opening…";
        try {
          const browserPath = addAccountBrowserSelect ? addAccountBrowserSelect.value : "";
          // Remember last selected browser
          if (browserPath) localStorage.setItem("sable_last_browser", browserPath);
          const res = await fetch("/api/settings/accounts/create", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ browser_path: browserPath }),
          });
          const data = await res.json();
          if (!res.ok) throw new Error(data.detail || "Failed");
          const browserName = addAccountBrowserSelect ? addAccountBrowserSelect.options[addAccountBrowserSelect.selectedIndex]?.text : '';
          showToast(`🌐 Opened ${data.profile}${browserName ? ' with ' + browserName.split(' (')[0] : ''}`, "success");
          await loadAccountProfiles();
          addAccountBtn.textContent = "Opening…";
          setTimeout(() => { addAccountBtn.textContent = "Add Account"; addAccountBtn.disabled = false; }, 3000);
        } catch (e) {
          addAccountBtn.textContent = e.message?.includes("401") ? "Not logged in" : "Failed";
          setTimeout(() => { addAccountBtn.textContent = "Add Account"; addAccountBtn.disabled = false; }, 2500);
        }
      });
    }



    // Load browser settings when settings panel opens
    const origOpenSettings = openSettings;
    openSettings = function() {
      origOpenSettings();
      // Only load the active tab's data, not everything upfront
      const activeTab = document.querySelector('.settings-tab.active');
      if (activeTab) {
        const tabName = activeTab.dataset.tab;
        if (tabName === 'general') { loadBrowserSettings(); }
        else if (tabName === 'account') { loadAvailableBrowsers(); loadAccountProfiles(); }
      }
    };

    // ---------- Font Size ----------
    const FONT_SIZE_KEY = "sable_font_size";
    const fontSizeSelect = document.getElementById("fontSizeSelect");

    function applyFontSize(size) {
      document.documentElement.style.setProperty("--font-size-response", size);
    }

    fontSizeSelect.addEventListener("change", () => {
      const size = fontSizeSelect.value;
      applyFontSize(size);
      try { localStorage.setItem(FONT_SIZE_KEY, size); } catch (e) {}
    });

    (function loadFontSize() {
      let saved = null;
      try { saved = localStorage.getItem(FONT_SIZE_KEY); } catch (e) {}
      if (saved) {
        fontSizeSelect.value = saved;
        applyFontSize(saved);
      }
    })();

    // ---------- System Font Size ----------
    const SYS_FONT_KEY = "sable_system_font_size";
    const systemFontSizeSelect = document.getElementById("systemFontSizeSelect");

    function applySystemFontSize(size) {
      document.documentElement.style.setProperty("--font-size-system", size);
    }

    systemFontSizeSelect.addEventListener("change", () => {
      const size = systemFontSizeSelect.value;
      applySystemFontSize(size);
      try { localStorage.setItem(SYS_FONT_KEY, size); } catch (e) {}
    });

    (function loadSystemFontSize() {
      let saved = null;
      try { saved = localStorage.getItem(SYS_FONT_KEY); } catch (e) {}
      if (saved) { systemFontSizeSelect.value = saved; applySystemFontSize(saved); }
    })();

    // ---------- Editor Font Size ----------
    const EDITOR_FONT_KEY = "sable_editor_font_size";
    const editorFontSizeSelect = document.getElementById("editorFontSizeSelect");

    function applyEditorFontSize(size) {
      const px = parseInt(size, 10);
      document.documentElement.style.setProperty("--editor-font-size", px + "px");
      // Update all active Monaco editors (instances are local to filesystem.js IIFE)
      if (typeof monaco !== "undefined" && monaco.editor && monaco.editor.getEditors) {
        monaco.editor.getEditors().forEach(ed => ed.updateOptions({ fontSize: px }));
      }
    }

    editorFontSizeSelect.addEventListener("change", () => {
      const size = editorFontSizeSelect.value;
      applyEditorFontSize(size);
      try { localStorage.setItem(EDITOR_FONT_KEY, size); } catch (e) {}
    });

    (function loadEditorFontSize() {
      let saved = null;
      try { saved = localStorage.getItem(EDITOR_FONT_KEY); } catch (e) {}
      if (saved) { editorFontSizeSelect.value = saved; applyEditorFontSize(saved); }
    })();

    // ---------- IDE Chat Font Size ----------
    const IDE_CHAT_FONT_KEY = "sable_ide_chat_font_size";
    const ideChatFontSizeSelect = document.getElementById("ideChatFontSizeSelect");

    function applyIdeChatFontSize(size) {
      document.documentElement.style.setProperty("--ide-chat-font-size", size);
    }

    ideChatFontSizeSelect.addEventListener("change", () => {
      const size = ideChatFontSizeSelect.value;
      applyIdeChatFontSize(size);
      try { localStorage.setItem(IDE_CHAT_FONT_KEY, size); } catch (e) {}
    });

    (function loadIdeChatFontSize() {
      let saved = null;
      try { saved = localStorage.getItem(IDE_CHAT_FONT_KEY); } catch (e) {}
      if (saved) { ideChatFontSizeSelect.value = saved; applyIdeChatFontSize(saved); }
    })();

    // ---------- IDE Editor Font Family ----------
    const IDE_FONT_FAMILY_KEY = "sable_ide_font_family";
    const ideFontFamilySelect = document.getElementById("ideFontFamilySelect");

    function applyIdeFontFamily(value) {
      if (typeof monaco !== "undefined" && monaco.editor && monaco.editor.getEditors) {
        monaco.editor.getEditors().forEach(ed => ed.updateOptions({ fontFamily: value }));
      }
    }

    ideFontFamilySelect.addEventListener("change", () => {
      const val = ideFontFamilySelect.value;
      applyIdeFontFamily(val);
      try { localStorage.setItem(IDE_FONT_FAMILY_KEY, val); } catch (e) {}
    });

    (function loadIdeFontFamily() {
      let saved = null;
      try { saved = localStorage.getItem(IDE_FONT_FAMILY_KEY); } catch (e) {}
      if (saved) { ideFontFamilySelect.value = saved; applyIdeFontFamily(saved); }
    })();

    // ---------- IDE Theme ----------
    const IDE_THEME_KEY = "sable_ide_theme";
    const ideThemeSelect = document.getElementById("ideThemeSelect");

    function applyIdeTheme(themeName) {
      if (typeof monaco !== "undefined" && monaco.editor) {
        monaco.editor.setTheme(themeName);
      }
    }

    ideThemeSelect.addEventListener("change", () => {
      const val = ideThemeSelect.value;
      applyIdeTheme(val);
      try { localStorage.setItem(IDE_THEME_KEY, val); } catch (e) {}
    });

    (function loadIdeTheme() {
      let saved = null;
      try { saved = localStorage.getItem(IDE_THEME_KEY); } catch (e) {}
      if (saved) { ideThemeSelect.value = saved; applyIdeTheme(saved); }
    })();

    // ---------- IDE Auto-Save Toggle ----------
    const IDE_AUTO_SAVE_KEY = "sable_ide_auto_save";
    const ideAutoSaveToggle = document.getElementById("ideAutoSaveToggle");

    function setAutoSaveUI(on) {
      ideAutoSaveToggle.textContent = on ? "On" : "Off";
      ideAutoSaveToggle.setAttribute("aria-checked", String(on));
      ideAutoSaveToggle.style.background = on ? "var(--accent)" : "var(--panel)";
      ideAutoSaveToggle.style.color = on ? "var(--bg)" : "var(--text-dim)";
    }

    ideAutoSaveToggle.addEventListener("click", () => {
      const current = ideAutoSaveToggle.getAttribute("aria-checked") === "true";
      const next = !current;
      setAutoSaveUI(next);
      try { localStorage.setItem(IDE_AUTO_SAVE_KEY, String(next)); } catch (e) {}
      // Notify filesystem.js via custom event
      window.dispatchEvent(new CustomEvent("ide-autosave-change", { detail: { enabled: next } }));
    });

    (function loadAutoSave() {
      let saved = null;
      try { saved = localStorage.getItem(IDE_AUTO_SAVE_KEY); } catch (e) {}
      setAutoSaveUI(saved === "true");
    })();

    // ---------- IDE Sticky Scroll Toggle ----------
    const IDE_STICKY_SCROLL_KEY = "sable_ide_sticky_scroll";
    const ideStickyScrollToggle = document.getElementById("ideStickyScrollToggle");

    function setStickyScrollUI(on) {
      ideStickyScrollToggle.textContent = on ? "On" : "Off";
      ideStickyScrollToggle.setAttribute("aria-checked", String(on));
      ideStickyScrollToggle.style.background = on ? "var(--accent)" : "var(--panel)";
      ideStickyScrollToggle.style.color = on ? "var(--bg)" : "var(--text-dim)";
    }

    function applyStickyScroll(on) {
      if (typeof monaco !== "undefined" && monaco.editor && monaco.editor.getEditors) {
        monaco.editor.getEditors().forEach(ed => ed.updateOptions({ stickyScroll: { enabled: on } }));
      }
    }

    ideStickyScrollToggle.addEventListener("click", () => {
      const current = ideStickyScrollToggle.getAttribute("aria-checked") === "true";
      const next = !current;
      setStickyScrollUI(next);
      applyStickyScroll(next);
      try { localStorage.setItem(IDE_STICKY_SCROLL_KEY, String(next)); } catch (e) {}
    });

    (function loadStickyScroll() {
      let saved = null;
      try { saved = localStorage.getItem(IDE_STICKY_SCROLL_KEY); } catch (e) {}
      const on = saved === "true";
      setStickyScrollUI(on);
      applyStickyScroll(on);
    })();

    // ---------- Font Family ----------
    const FONT_FAMILY_KEY = "sable_font_family";
    const fontFamilySelect = document.getElementById("fontFamilySelect");
    const FONT_STACKS = {
      maple: "'Maple Mono', ui-monospace, monospace",
      inter: "'Inter', -apple-system, BlinkMacSystemFont, sans-serif",
      system: "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
    };

    function applyFontFamily(key) {
      const stack = FONT_STACKS[key] || FONT_STACKS.maple;
      document.documentElement.style.setProperty("--font-body", stack);
      document.documentElement.style.setProperty("--font-mono", stack);
      document.documentElement.style.setProperty("--font-serif", stack);
    }

    fontFamilySelect.addEventListener("change", () => {
      applyFontFamily(fontFamilySelect.value);
      try { localStorage.setItem(FONT_FAMILY_KEY, fontFamilySelect.value); } catch (e) {}
    });

    (function loadFontFamily() {
      let saved = null;
      try { saved = localStorage.getItem(FONT_FAMILY_KEY); } catch (e) {}
      if (saved && FONT_STACKS[saved]) {
        fontFamilySelect.value = saved;
        applyFontFamily(saved);
      }
    })();


    // ---------- MCP Server Management ----------
    async function loadMcpServers() {
      const listEl = document.getElementById("mcpServerList");
      const statusEl = document.getElementById("mcpStatus");
      if (!listEl) return;
      try {
        const res = await fetch("/api/settings/mcp");
        const data = await res.json();
        const servers = data.servers || [];
        if (servers.length === 0) {
          listEl.innerHTML = '<p style="font-size:12px;color:var(--text-dim);padding:8px 0;">No MCP servers configured yet. Add one above to get started.</p>';
          return;
        }
        listEl.innerHTML = servers.map(s => `
          <div style="border:1px solid var(--border);border-radius:10px;padding:12px;background:var(--panel);">
            <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;">
              <div style="display:flex;align-items:center;gap:8px;">
                <span style="width:8px;height:8px;border-radius:50%;background:${s.connected ? '#4ade80' : '#f87171'};display:inline-block;"></span>
                <span style="font-size:13px;font-weight:600;color:var(--text);">${s.name}</span>
                <span style="font-size:11px;color:var(--text-dim);">${s.command} ${(s.args||[]).join(' ')}</span>
              </div>
              <div style="display:flex;gap:4px;">
                ${s.connected
                  ? `<button onclick="mcpDisconnect('${s.name}')" class="icon-btn" style="width:auto;padding:4px 10px;font-size:11px;">Disconnect</button>`
                  : `<button onclick="mcpConnect('${s.name}')" class="icon-btn" style="width:auto;padding:4px 10px;font-size:11px;">Connect</button>`
                }
                <button onclick="mcpRemove('${s.name}')" class="icon-btn" style="width:auto;padding:4px 10px;font-size:11px;color:#f87171;">Remove</button>
              </div>
            </div>
            <div style="margin-top:6px;display:flex;gap:4px;align-items:center;">
              <input type="password" id="mcpEnv_${s.name}" placeholder="GITHUB_PERSONAL_ACCESS_TOKEN" value="${(s.env && Object.values(s.env)[0]) || ''}" style="flex:1;padding:4px 8px;font-size:11px;border-radius:6px;border:1px solid var(--border);background:var(--bg);color:var(--text);" />
              <button onclick="mcpUpdateEnv('${s.name}')" class="icon-btn" style="width:auto;padding:4px 10px;font-size:11px;">Save Env</button>
            </div>
            ${s.error ? `<p style="font-size:11px;color:#f87171;margin:4px 0 0 0;"><i data-lucide="triangle-alert" style="width:12px;height:12px;display:inline;vertical-align:middle;"></i> ${s.error}</p>` : ''}
            ${s.tools && s.tools.length > 0 ? `
              <div style="margin-top:8px;padding-top:8px;border-top:1px solid var(--border);">
                <p style="font-size:11px;color:var(--text-dim);margin:0 0 4px 0;">Tools (${s.tools.length}):</p>
                <div style="display:flex;flex-wrap:wrap;gap:4px;">
                  ${s.tools.map(t => `<span style="font-size:10px;padding:2px 8px;border-radius:6px;background:var(--bg);border:1px solid var(--border);color:var(--text);" title="${t.description || ''}">${t.name}</span>`).join('')}
                </div>
              </div>
            ` : ''}
          </div>
        `).join('');
        statusEl.textContent = `${servers.length} server(s) configured, ${servers.filter(s=>s.connected).length} connected`;
        if (typeof lucide !== "undefined") lucide.createIcons();
      } catch (e) {
        statusEl.textContent = "Failed to load MCP servers: " + e.message;
      }
    }

    async function mcpAddServer() {
      const name = document.getElementById("mcpName").value.trim();
      const command = document.getElementById("mcpCommand").value.trim();
      const argsRaw = document.getElementById("mcpArgs").value.trim();
      const envRaw = document.getElementById("mcpEnv").value.trim();
      const statusEl = document.getElementById("mcpStatus");

      if (!name || !command) {
        statusEl.textContent = "❌ Name and command are required.";
        return;
      }

      const args = argsRaw ? argsRaw.split(',').map(a => a.trim()).filter(Boolean) : [];
      let env = {};
      if (envRaw) {
        try { env = JSON.parse(envRaw); } catch (e) {
          statusEl.textContent = "❌ Invalid env JSON.";
          return;
        }
      }

      try {
        const res = await fetch("/api/settings/mcp", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({name, command, args, env}),
        });
        const data = await res.json();
        if (res.ok) {
          statusEl.textContent = `✅ Server '${name}' added.`;
          document.getElementById("mcpName").value = "";
          document.getElementById("mcpCommand").value = "";
          document.getElementById("mcpArgs").value = "";
          document.getElementById("mcpEnv").value = "";
          loadMcpServers();
        } else {
          statusEl.textContent = "❌ " + (data.detail || "Failed to add server");
        }
      } catch (e) {
        statusEl.textContent = "❌ " + e.message;
      }
    }

    async function mcpConnect(name) {
      const statusEl = document.getElementById("mcpStatus");
      statusEl.textContent = `Connecting to '${name}'…`;
      try {
        const res = await fetch(`/api/settings/mcp/${name}/connect`, {method: "POST"});
        const data = await res.json();
        if (data.connected) {
          statusEl.textContent = `✅ '${name}' connected — ${data.tools.length} tools discovered.`;
        } else {
          statusEl.textContent = `❌ '${name}' failed: ${data.error || 'unknown error'}`;
        }
        loadMcpServers();
      } catch (e) {
        statusEl.textContent = "❌ " + e.message;
      }
    }

    async function mcpDisconnect(name) {
      try {
        await fetch(`/api/settings/mcp/${name}/disconnect`, {method: "POST"});
        loadMcpServers();
      } catch (e) {
        document.getElementById("mcpStatus").textContent = "❌ " + e.message;
      }
    }

    async function mcpRemove(name) {
      if (!await sableConfirm(`Remove MCP server '${name}'?`, { danger: true })) return;
      try {
        const res = await fetch(`/api/settings/mcp/${name}`, {method: "DELETE"});
        if (res.ok) {
          document.getElementById("mcpStatus").textContent = `Server '${name}' removed.`;
          loadMcpServers();
        }
      } catch (e) {
        document.getElementById("mcpStatus").textContent = "❌ " + e.message;
      }
    }

    async function mcpUpdateEnv(name) {
      const input = document.getElementById(`mcpEnv_${name}`);
      const statusEl = document.getElementById("mcpStatus");
      if (!input || !statusEl) return;
      const val = input.value.trim();
      try {
        const res = await fetch(`/api/settings/mcp/${name}`, {
          method: "PUT",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({env: {GITHUB_PERSONAL_ACCESS_TOKEN: val}}),
        });
        if (res.ok) {
          statusEl.textContent = `✅ Env updated for '${name}'. Reconnect to apply.`;
        } else {
          const err = await res.json().catch(() => ({detail: res.statusText}));
          statusEl.textContent = "❌ " + (err.detail || "Failed to update env");
        }
      } catch (e) {
        statusEl.textContent = "❌ " + e.message;
      }
    }

    // Wire up the Add button
    const mcpAddBtn = document.getElementById("mcpAddBtn");
    if (mcpAddBtn) mcpAddBtn.addEventListener("click", mcpAddServer);


    // ---------- Icon Style ----------
    const ICON_STYLE_KEY = "sable_icon_style";
    const iconStyleSelect = document.getElementById("iconStyleSelect");
    function applyIconStyle(style) {
      document.documentElement.setAttribute("data-icon-style", style);
      if (style === "lucide" && window.lucide) {
        lucide.createIcons();
      }
    }

    iconStyleSelect.addEventListener("change", () => {
      applyIconStyle(iconStyleSelect.value);
      try { localStorage.setItem(ICON_STYLE_KEY, iconStyleSelect.value); } catch (e) {}
    });

    (function loadIconStyle() {
      let saved = null;
      try { saved = localStorage.getItem(ICON_STYLE_KEY); } catch (e) {}
      const style = saved || "lucide";
      iconStyleSelect.value = style;
      applyIconStyle(style);
    })();



    // ---------- Theme ----------

    const THEME_KEY = "sable_theme";
    const themePicker = document.getElementById("themePicker");

    function applyTheme(name) {
      if (name && name !== "sable") {
        document.documentElement.setAttribute("data-theme", name);
      } else {
        document.documentElement.removeAttribute("data-theme");
      }
      updateFavicon();
    }

    function updateFavicon() {
      // --- Themed icon: resolved data-URI for favicon + all logo imgs ---
      if (!window.__sableIcon) {
        window.__sableIcon = fetch("/static/assets/sable_icon.svg?v=5")
          .then(r => (r.ok ? r.text() : null)).catch(() => null);
      }
      window.__sableIcon.then(txt => {
        if (!txt) return;
        const cs = getComputedStyle(document.documentElement);
        const vars = {
          '--accent-text': cs.getPropertyValue('--accent-text').trim() || '#a78bfa',
          '--accent': cs.getPropertyValue('--accent').trim() || '#8b5cf6',
          '--panel-2': cs.getPropertyValue('--panel-2').trim() || '#211c30',
          '--panel': cs.getPropertyValue('--panel').trim() || '#1a1625',
          '--bg': cs.getPropertyValue('--bg').trim() || '#0f0d15',
          '--text': cs.getPropertyValue('--text').trim() || '#e0dce8',
        };
        let resolved = txt;
        for (const [v, val] of Object.entries(vars)) {
          resolved = resolved.replace(new RegExp('var\\(' + v + ',\\s*([^)]+)\\)', 'g'), val);
        }
        const uri = "data:image/svg+xml," + encodeURIComponent(resolved);
        let link = document.querySelector("link[rel='icon']");
        if (!link) { link = document.createElement("link"); link.rel = "icon"; document.head.appendChild(link); }
        link.type = "image/svg+xml";
        link.href = uri;
        if (!window.__sableIconImgs) {
          window.__sableIconImgs = Array.from(document.querySelectorAll('img[src*="sable_icon"]'));
        }
        window.__sableIconImgs.forEach(img => { img.src = uri; });
      });
    }

    themePicker.addEventListener("click", (e) => {
      const btn = e.target.closest(".theme-swatch");
      if (!btn) return;
      const name = btn.dataset.theme;
      applyTheme(name);
      themePicker.querySelectorAll(".theme-swatch").forEach((b) => b.classList.toggle("active", b === btn));
      try { localStorage.setItem(THEME_KEY, name); } catch (err) {}
    });

    (function loadTheme() {
      let saved = null;
      try { saved = localStorage.getItem(THEME_KEY); } catch (e) {}
      if (saved) {
        applyTheme(saved);
        const match = themePicker.querySelector('.theme-swatch[data-theme="' + saved + '"]');
        if (match) {
          themePicker.querySelectorAll(".theme-swatch").forEach((b) => b.classList.toggle("active", b === match));
        }
      } else {
        updateFavicon();
      }
    })();

    // ---------- Appearance Mode (Light / Dark / Auto) ----------
    const MODE_KEY = "sable_appearance_mode";
    const modeToggle = document.getElementById("modeToggle");

    function getEffectiveMode(saved) {
      if (saved === "light") return "light";
      if (saved === "dark") return "dark";
      // auto or unset: follow OS
      return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
    }

    function applyMode(mode) {
      const effective = getEffectiveMode(mode);
      document.documentElement.setAttribute("data-mode", effective);
      // Update Monaco editor theme if available
      if (typeof getSableMonacoThemeName === "function") {
        try { monaco.editor.setTheme(getSableMonacoThemeName()); } catch(e) {}
      }
      updateFavicon();
    }

    function updateModeButtons(saved) {
      if (!modeToggle) return;
      modeToggle.querySelectorAll(".mode-btn").forEach(btn => {
        const isActive = btn.dataset.mode === (saved || "auto");
        btn.classList.toggle("active", isActive);
        if (isActive) {
          btn.style.background = "var(--accent-dim)";
          btn.style.color = "var(--accent-text)";
        } else {
          btn.style.background = "transparent";
          btn.style.color = "var(--muted)";
        }
      });
    }

    if (modeToggle) {
      modeToggle.addEventListener("click", (e) => {
        const btn = e.target.closest(".mode-btn");
        if (!btn) return;
        const mode = btn.dataset.mode;
        try { localStorage.setItem(MODE_KEY, mode); } catch(err) {}
        applyMode(mode);
        updateModeButtons(mode);
      });
    }

    // Listen for OS preference changes when in auto mode
    window.matchMedia("(prefers-color-scheme: light)").addEventListener("change", () => {
      let saved = null;
      try { saved = localStorage.getItem(MODE_KEY); } catch(e) {}
      if (!saved || saved === "auto") {
        applyMode("auto");
      }
    });

    // Load saved mode on startup
    (function loadMode() {
      let saved = null;
      try { saved = localStorage.getItem(MODE_KEY); } catch(e) {}
      applyMode(saved || "auto");
      updateModeButtons(saved);
    })();

    // ---------- Mode Switcher (API / Scraper) ----------
    const modeApiBtn = document.getElementById('modeApi');
    const modeScraperBtn = document.getElementById('modeScraper');
    const scraperEngineWrap = document.getElementById('scraperEngineWrap');
    const scraperEngineSelect = document.getElementById('scraperEngineSelect');
    const scraperStatusEl = document.getElementById('scraperStatus');
    let scraperMode = false;
    let scraperEngines = [];

    function setScraperStatus(msg, type) {
      scraperStatusEl.textContent = msg;
      scraperStatusEl.className = 'scraper-status visible ' + (type || '');
    }

    function updateModeUI() {
      modeApiBtn.classList.toggle('active', !scraperMode);
      modeScraperBtn.classList.toggle('active', scraperMode);
      scraperEngineWrap.classList.toggle('visible', scraperMode);
      if (!scraperMode) {
        scraperStatusEl.className = 'scraper-status';
      }
    }

    async function loadScraperEngines() {
      try {
        const res = await fetch('/api/settings/scraper/engines');
        if (res.ok) {
          const data = await res.json();
          scraperEngines = data.engines || [];
          scraperEngineSelect.innerHTML = '';
          for (const eng of scraperEngines) {
            const opt = document.createElement('option');
            opt.value = eng.id;
            opt.textContent = eng.label;
            scraperEngineSelect.appendChild(opt);
          }
        }
      } catch {}
    }

    async function loadScraperSettings() {
      try {
        const res = await fetch('/api/settings/scraper');
        if (res.ok) {
          const data = await res.json();
          scraperMode = !!data.enabled;
          if (data.engine_type && scraperEngineSelect) {
            scraperEngineSelect.value = data.engine_type;
          }
          updateModeUI();
          if (scraperMode) {
            setScraperStatus('● Browser connected — ' + (data.engine_label || 'Scraper'), 'ok');
          }
        }
      } catch {}
    }

    async function setScraperMode(enabled) {
      scraperMode = enabled;
      updateModeUI();
      if (enabled) {
        setScraperStatus('Launching browser…', '');
      }
      try {
        const payload = { enabled, headless: false };
        if (enabled && scraperEngineSelect.value) {
          payload.engine_type = scraperEngineSelect.value;
        }
        const res = await fetch('/api/settings/scraper', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        if (res.ok) {
          const data = await res.json();
          scraperMode = !!data.enabled;
          updateModeUI();
          if (data.enabled) {
            const pl = data.prelaunch;
            if (pl && pl.status === 'ok') {
              setScraperStatus('● Browser launched — ' + (data.engine_label || 'Scraper'), 'ok');
            } else if (pl && pl.status === 'error') {
              setScraperStatus('✗ ' + pl.message, 'err');
            } else {
              setScraperStatus('● Scraper enabled — ' + (data.engine_label || ''), 'ok');
            }
            showToast('Scraper mode ON — headed browser', 'success');
            await loadModels();
            // Navigate to latest scraper chat
            await loadChats('scraper');
            if (chatList.length > 0) await selectChat(chatList[0].id);
          } else {
            setScraperStatus('', '');
            showToast('Scraper mode OFF — API chat', 'success');
            await loadModels();
            // Navigate to latest API chat
            await loadChats('api');
            if (chatList.length > 0) await selectChat(chatList[0].id);
          }
        } else {
          const err = await res.json().catch(() => ({}));
          showToast(err.detail || 'Could not update scraper mode', 'error');
          scraperMode = !enabled;
          updateModeUI();
        }
      } catch (e) {
        showToast('Scraper mode error: ' + e.message, 'error');
        scraperMode = !enabled;
        updateModeUI();
      }
    }

    modeApiBtn.addEventListener('click', () => {
      if (!scraperMode) return;
      setScraperMode(false);
    });

    modeScraperBtn.addEventListener('click', () => {
      if (scraperMode) return;
      setScraperMode(true);
    });

    scraperEngineSelect.addEventListener('change', async () => {
      if (!scraperMode) return;
      try {
        const res = await fetch('/api/settings/scraper', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ engine_type: scraperEngineSelect.value, enabled: true, headless: false })
        });
        if (res.ok) {
          const data = await res.json();
          const pl = data.prelaunch;
          if (pl && pl.status === 'ok') {
            setScraperStatus('● Browser relaunched — ' + (data.engine_label || ''), 'ok');
          } else if (pl && pl.status === 'error') {
            setScraperStatus('✗ ' + pl.message, 'err');
          }
          showToast('Engine switched to ' + (data.engine_label || scraperEngineSelect.value), 'success');
          await loadModels();
        }
      } catch {}
    });

    loadScraperEngines().then(async () => {
      await loadScraperSettings();
      // Refresh model list now that scraper state is known — if scraper is
      // active with DeepSeek the dropdown must show DS model types, not Qwen.
      await loadModels();
    });

  // ── Browser Session Monitor ──────────────────────────────────
  async function loadBrowserSession() {
    const card = document.getElementById('browserSessionCard');
    if (!card) return;
    try {
      const res = await fetch('/api/scraper/sessions');
      const d = await res.json();
      if (!d.active) {
        card.innerHTML = '<p class="muted" style="font-size:12px;margin:0;">No active browser session.</p>';
        return;
      }
      const alive = d.alive;
      const dot = alive ? '\u{1F7E2}' : '\u{1F534}';
      const statusTxt = alive ? 'Running' : 'Dead / Zombie';
      card.innerHTML =
        '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;">' +
          '<span style="font-size:13px;font-weight:600;color:var(--text);">' + dot + ' ' + statusTxt + '</span>' +
          '<button onclick="killBrowserSession()" style="background:var(--danger);color:#fff;border:none;border-radius:6px;padding:4px 12px;font-size:11px;cursor:pointer;font-weight:600;">\u2715 Kill</button>' +
        '</div>' +
        '<div style="display:grid;grid-template-columns:auto 1fr;gap:3px 12px;font-size:12px;color:var(--text-dim);">' +
          '<span>Engine</span><span style="color:var(--text);">' + (d.engine_type || '\u2014') + '</span>' +
          '<span>Chat ID</span><span style="color:var(--text);">' + (d.chat_id || '\u2014') + '</span>' +
          '<span>PID</span><span style="color:var(--text);">' + (d.chrome_pid || '\u2014') + '</span>' +
          '<span>CDP Port</span><span style="color:var(--text);">' + (d.cdp_port || '\u2014') + '</span>' +
          '<span>Headless</span><span style="color:var(--text);">' + (d.headless ? 'Yes' : 'No') + '</span>' +
          '<span>URL</span><span style="color:var(--text);word-break:break-all;font-size:11px;">' + (d.page_url || '\u2014') + '</span>' +
        '</div>';
    } catch {
      card.innerHTML = '<p class="muted" style="font-size:12px;margin:0;color:var(--danger);">Failed to fetch session info.</p>';
    }
  }

  async function killBrowserSession() {
    try {
      const res = await fetch('/api/scraper/sessions/kill', { method: 'POST' });
      const d = await res.json();
      showToast(d.killed_pid ? 'Killed PID ' + d.killed_pid : 'Session reset (no PID found)', 'success');
    } catch {
      showToast('Failed to kill session', 'error');
    }
    await loadBrowserSession();
  }

  document.getElementById('refreshSessionBtn')?.addEventListener('click', loadBrowserSession);
  loadBrowserSession();
  setInterval(loadBrowserSession, 15000);
  // ── /Browser Session Monitor ─────────────────────────────────

  // ── Context Menu ──────────────────────────────────────────
  const ctxMenu = document.getElementById('contextMenu');

  document.addEventListener('contextmenu', (e) => {
    // Only on main area / sidebar, not on inputs or textareas
    if (e.target.closest('textarea, input, select, .ctx-menu, #fsOverlay')) return;
    e.preventDefault();

    const x = Math.min(e.clientX, window.innerWidth - ctxMenu.offsetWidth - 12);
    const y = Math.min(e.clientY, window.innerHeight - ctxMenu.offsetHeight - 12);
    ctxMenu.style.left = x + 'px';
    ctxMenu.style.top = y + 'px';
    ctxMenu.classList.add('open');
  });

  function closeCtx() { ctxMenu.classList.remove('open'); }

  document.addEventListener('click', (e) => {
    if (!e.target.closest('.ctx-menu')) closeCtx();
  });

  // SVG popout button (delegated)
  document.addEventListener('click', (e) => {
    const popBtn = e.target.closest('.svg-popout-btn');
    if (!popBtn) return;
    const wrap = popBtn.closest('.svg-wrap');
    const svgEl = wrap?.querySelector('svg:not(.svg-popout-btn svg)');
    if (!svgEl) return;
    const svgMarkup = svgEl.outerHTML;
    const html = '<!DOCTYPE html><html><head><meta charset="utf-8"><style>*{margin:0;padding:0}body{display:flex;justify-content:center;align-items:center;min-height:100vh;background:#1a1a2e}svg{max-width:95vw;max-height:95vh}</style></head><body>' + svgMarkup + '</body></html>';
    const blob = new Blob([html], { type: 'text/html' });
    window.open(URL.createObjectURL(blob), '_blank');
  });

  // Code block run/preview button (delegated)
  document.addEventListener('click', (e) => {
    const runBtn = e.target.closest('.code-run-btn');
    if (!runBtn) return;
    const block = runBtn.closest('.code-block');
    const codeEl = block?.querySelector('pre code');
    if (!codeEl) return;
    const lang = (runBtn.dataset.lang || '').toLowerCase();
    const raw = codeEl.textContent;

    let html;
    if (/^(threejs|three\.js)$/i.test(lang)) {
      // Auto-wrap raw three.js in a working HTML shell
      html = `<!DOCTYPE html><html><head><meta charset="utf-8"><style>*{margin:0;padding:0}body{overflow:hidden;background:#000}</style></head><body><script type="importmap">{"imports":{"three":"https://cdn.jsdelivr.net/npm/three@latest/build/three.module.js","three/addons/":"https://cdn.jsdelivr.net/npm/three@latest/examples/jsm/"}}</script><script type="module">\n${raw}\n</script></body></html>`;
    } else if (/^(p5js|p5)$/i.test(lang)) {
      html = `<!DOCTYPE html><html><head><meta charset="utf-8"><script src="https://cdn.jsdelivr.net/npm/p5@latest/lib/p5.min.js"></script><style>*{margin:0;padding:0}body{display:flex;justify-content:center;align-items:center;min-height:100vh;background:#1a1a2e}</style></head><body><script>\n${raw}\n</script></body></html>`;
    } else if (/^svg$/i.test(lang)) {
      html = `<!DOCTYPE html><html><head><meta charset="utf-8"><style>*{margin:0;padding:0}body{display:flex;justify-content:center;align-items:center;min-height:100vh;background:#1a1a2e}svg{max-width:95vw;max-height:95vh}</style></head><body>${raw}</body></html>`;
    } else {
      // html/htm — use as-is
      html = raw;
    }

    const blob = new Blob([html], { type: 'text/html' });
    window.open(URL.createObjectURL(blob), '_blank');
  });

  // Code block copy button (delegated)
  document.addEventListener('click', (e) => {
    const btn = e.target.closest('.code-copy-btn');
    if (!btn) return;
    const block = btn.closest('.code-block');
    const codeEl = block?.querySelector('pre code');
    if (!codeEl) return;
    safeCopy(codeEl.textContent).then((ok) => {
      if (!ok) return;
      btn.classList.add('copied');
      btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
      setTimeout(() => {
        btn.classList.remove('copied');
        btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';
      }, 1500);
    });
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeCtx();
  });
  window.addEventListener('resize', closeCtx);
  window.addEventListener('scroll', closeCtx, true);

  ctxMenu.addEventListener('click', async (e) => {
    const item = e.target.closest('.ctx-item');
    if (!item) return;
    closeCtx();
    const action = item.dataset.action;

    if (action === 'copy') {
      const sel = window.getSelection()?.toString() || '';
      if (sel) {
        await safeCopy(sel);
        showToast('Copied', 'success');
      } else {
        showToast('Nothing selected', 'error');
      }
    } else if (action === 'paste') {
      try {
        const text = await navigator.clipboard.readText();
        const active = document.activeElement;
        if (active && (active.tagName === 'TEXTAREA' || active.tagName === 'INPUT' || active.isContentEditable)) {
          active.setRangeText?.(text, active.selectionStart, active.selectionEnd, 'end') ?? active.insertAdjacentText?.('beforeend', text);
          active.dispatchEvent(new Event('input', { bubbles: true }));
        } else if (typeof inputEl !== 'undefined' && inputEl) {
          inputEl.value += text;
          inputEl.dispatchEvent(new Event('input', { bubbles: true }));
        } else {
          showToast('No active input to paste into', 'error');
        }
      } catch { showToast('Clipboard read denied', 'error'); }
    } else if (action === 'select-all') {
      const target = e.target.closest('.message-content, .chat-area, #chatMessages, main');
      if (target) {
        const range = document.createRange();
        range.selectNodeContents(target);
        const sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
      } else {
        document.execCommand('selectAll');
      }
    } else if (action === 'new-chat') {
      document.getElementById('newChat')?.click();
    } else if (action === 'settings') {
      (document.getElementById('railSettingsBtn') || document.getElementById('settingsBtn'))?.click();
    } else if (action === 'context-pass') {
      if (!activeChatId) { showToast('No active chat to pass context from', 'error'); return; }
      showToast('Summarizing context…', 'info');
      try {
        const res = await fetch('/api/context/pass', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ chat_id: activeChatId, model: selectedModel }),
        });
        const d = await res.json();
        if (!res.ok || d.error) { showToast(d.error || 'Context pass failed', 'error'); return; }
        const summary = d.summary;
        if (!summary) { showToast('Empty summary returned', 'error'); return; }
        // Create new chat and auto-send the summary as first message
        const created = await createChat();
        if (!created) { showToast('Failed to create new chat', 'error'); return; }
        inputEl.value = summary;
        autoResize();
        await sendMessage();
        showToast('Context passed to new chat', 'success');
      } catch (e) { showToast('Context pass error: ' + e.message, 'error'); }
    } else if (action === 'sync-context') {
      showToast('Syncing context…', 'info');
      try {
        const mode = localStorage.getItem('sable_layout_mode') || 'agent';
        const res = await fetch('/api/sync-context', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ layout_mode: mode })
        });
        const d = await res.json();
        showToast(res.ok ? (d.message || 'Context synced') : (d.error || 'Sync failed'), res.ok ? 'success' : 'error');
      } catch { showToast('Sync failed', 'error'); }
    } else if (action === 'refresh-deepseek') {
      showToast('Refreshing DeepSeek token…', 'info');
      try {
        const res = await fetch('/api/settings/deepseek/refresh-token', { method: 'POST' });
        const d = await res.json();
        showToast(res.ok ? (d.message || 'Token refreshed') : (d.error || 'Refresh failed'), res.ok ? 'success' : 'error');
      } catch { showToast('Refresh failed', 'error'); }
    } else if (action === 'refresh-waf') {
      showToast('Refreshing WAF token…', 'info');
      try {
        const res = await fetch('/api/settings/browser/refresh-waf', { method: 'POST' });
        const d = await res.json();
        showToast(res.ok ? (d.message || 'WAF token refreshed') : (d.detail || 'Refresh failed'), res.ok ? 'success' : 'error');
      } catch { showToast('Refresh failed', 'error'); }
    } else if (action === 'clear-browser-cache') {
      if (!await sableConfirm('Strip all browser profile caches? This keeps session data but removes cache/junk.')) return;
      showToast('Stripping browser profiles…', 'info');
      try {
        const res = await fetch('/api/settings/browser/strip-profiles', { method: 'POST' });
        const d = await res.json();
        showToast(res.ok ? 'Profiles stripped' : (d.error || 'Strip failed'), res.ok ? 'success' : 'error');
      } catch { showToast('Strip failed', 'error'); }
    }
  });

    // ── Checkpoint Restore Modal ─────────────────────────────────────────
    async function showCheckpointModal(chatId, messageId, btn) {
      // Remove existing modal if any
      document.querySelector('.cp-modal-overlay')?.remove();

      btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="spin"><path d="M21 12a9 9 0 1 1-6.219-8.56"/></svg>';

      try {
        // 1. Get checkpoint for this message
        const cpRes = await fetch(`/api/checkpoints/${chatId}/message/${messageId}`);
        const cpData = await cpRes.json();
        if (!cpData.checkpoint) {
          showToast('No checkpoint found for this message', 'error');
          btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="6" y1="3" x2="6" y2="15"></line><circle cx="18" cy="6" r="3"></circle><circle cx="6" cy="18" r="3"></circle><path d="M18 9a9 9 0 0 1-9 9"></path></svg>';
          return;
        }
        const sha = cpData.checkpoint.commit_sha;

        // 2. Get diff preview
        const diffRes = await fetch(`/api/checkpoints/diff/${sha}`);
        const diffData = await diffRes.json();

        // 3. Build and show modal
        const overlay = document.createElement('div');
        overlay.className = 'cp-modal-overlay';
        
        let filesHtml = '';
        if (diffData.files && diffData.files.length > 0) {
          filesHtml = diffData.files.map(f => {
            const statusIcon = f.status === 'added' ? '🟢' : f.status === 'deleted' ? '🔴' : '🟡';
            return `<div class="cp-file-row">
              <span class="cp-file-status">${statusIcon}</span>
              <span class="cp-file-path">${f.path}</span>
              <span class="cp-file-stats">+${f.additions} −${f.deletions}</span>
            </div>`;
          }).join('');
        } else {
          filesHtml = '<div class="cp-no-changes">No changes since this checkpoint — project is already at this state.</div>';
        }

        overlay.innerHTML = `
          <div class="cp-modal">
            <div class="cp-modal-header">
              <h3><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="6" y1="3" x2="6" y2="15"></line><circle cx="18" cy="6" r="3"></circle><circle cx="6" cy="18" r="3"></circle><path d="M18 9a9 9 0 0 1-9 9"></path></svg> Restore Checkpoint</h3>
              <button class="cp-modal-close">&times;</button>
            </div>
            <div class="cp-modal-body">
              <p class="cp-summary">
                <strong>${diffData.total_files || 0}</strong> file(s) changed ·
                <span class="cp-adds">+${diffData.total_additions || 0}</span> ·
                <span class="cp-dels">−${diffData.total_deletions || 0}</span>
              </p>
              <div class="cp-file-list">${filesHtml}</div>
            </div>
            <div class="cp-modal-footer">
              <button class="cp-btn-cancel">Cancel</button>
              <button class="cp-btn-restore" ${!diffData.total_files ? 'disabled' : ''}>Restore</button>
            </div>
          </div>`;

        document.body.appendChild(overlay);
        activateLucideIcons(overlay);

        // Close handlers
        overlay.querySelector('.cp-modal-close').onclick = () => overlay.remove();
        overlay.querySelector('.cp-btn-cancel').onclick = () => overlay.remove();
        overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });

        // Restore handler
        overlay.querySelector('.cp-btn-restore').onclick = async () => {
          const restoreBtn = overlay.querySelector('.cp-btn-restore');
          restoreBtn.disabled = true;
          restoreBtn.textContent = 'Restoring…';
          try {
            const res = await fetch('/api/checkpoints/restore', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ commit_sha: sha }),
            });
            const result = await res.json();
            if (result.ok) {
              showToast('Checkpoint restored ✓', 'success');
              overlay.remove();
            } else {
              showToast('Restore failed: ' + (result.detail || 'Unknown error'), 'error');
              restoreBtn.disabled = false;
              restoreBtn.textContent = 'Restore';
            }
          } catch (err) {
            showToast('Restore failed: ' + err.message, 'error');
            restoreBtn.disabled = false;
            restoreBtn.textContent = 'Restore';
          }
        };
      } catch (err) {
        showToast('Failed to load checkpoint: ' + err.message, 'error');
      } finally {
        btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="6" y1="3" x2="6" y2="15"></line><circle cx="18" cy="6" r="3"></circle><circle cx="6" cy="18" r="3"></circle><path d="M18 9a9 9 0 0 1-9 9"></path></svg>';
      }
    }
    // ── /Checkpoint Restore Modal ────────────────────────────────────────

  // ── /Context Menu ─────────────────────────────────────────



