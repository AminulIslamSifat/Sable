    // ---------- Account Profile Switcher ----------
    const accountProfileCards = document.getElementById("accountProfileCards");
    const refreshAccountsBtn = document.getElementById("refreshAccountsBtn");

    async function loadAccountProfiles() {
      if (!accountProfileCards) return;
      accountProfileCards.innerHTML = '<p class="muted" style="font-size:12px;margin:0;">Loading accounts…</p>';
      try {
        const res = await fetch("/api/settings/accounts");
        if (!res.ok) throw new Error("HTTP " + res.status);
        const data = await res.json();
        const accounts = data.accounts || [];
        const active = data.active;

        if (!accounts.length) {
          accountProfileCards.innerHTML = '<p class="muted" style="font-size:12px;margin:0;">No account profiles found. Create dirs like <code>system/browser-data-acc1</code>, <code>system/browser-data-acc2</code>…</p>';
          return;
        }

        accountProfileCards.innerHTML = accounts.map((acc) => {
          const isActive = acc.name === active;
          const email = acc.email || "unknown account";
          const size = acc.size_mb ? acc.size_mb + " MB" : "";
          return `<div style="display:flex;align-items:center;justify-content:space-between;background:var(--panel);border:1px solid ${isActive ? 'var(--accent)' : 'var(--border)'};border-radius:10px;padding:10px 14px;">
            <div style="min-width:0;">
              <div style="font-size:12px;font-weight:600;color:var(--text);">${email}</div>
              <div style="font-size:11px;color:var(--text-dim);margin-top:2px;">${acc.name}${size ? ' · ' + size : ''}${isActive ? ' · <span style="color:var(--accent);">active</span>' : ''}
                ${acc.has_waf ? '<span style="display:inline-block;font-size:10px;font-weight:600;color:#22c55e;border:1px solid #22c55e;border-radius:4px;padding:1px 5px;margin-left:6px;">qwen</span>' : ''}
                ${acc.has_ds ? '<span style="display:inline-block;font-size:10px;font-weight:600;color:#22c55e;border:1px solid #22c55e;border-radius:4px;padding:1px 5px;margin-left:4px;">ds</span>' : ''}
                ${acc.exhausted ? '<span style="display:inline-block;font-size:10px;font-weight:600;color:#ef4444;border:1px solid #ef4444;background:rgba(239,68,68,0.1);border-radius:4px;padding:1px 5px;margin-left:4px;">Exhausted</span>' : ''}
              </div>
            </div>
            <div style="display:flex;gap:6px;align-items:center;flex-shrink:0;">
              <button class="icon-btn account-open-btn" data-profile="${acc.name}" style="width:auto;padding:5px 12px;font-size:11px;white-space:nowrap;">Open</button>
              ${isActive ? '' : `<button class="icon-btn account-switch-btn" data-profile="${acc.name}" style="width:auto;padding:5px 12px;font-size:11px;white-space:nowrap;">Switch</button>`}
              ${isActive ? '' : `<button class="icon-btn account-delete-btn" data-profile="${acc.name}" style="width:auto;padding:5px 10px;font-size:11px;white-space:nowrap;color:var(--danger);border-color:var(--danger);">Delete</button>`}
            </div>
          </div>`;
        }).join("");

        accountProfileCards.querySelectorAll(".account-switch-btn").forEach((btn) => {
          btn.addEventListener("click", async () => {
            const profile = btn.dataset.profile;
            btn.disabled = true;
            btn.textContent = "Switching…";
            showToast("🔄 Switching account profile…", "info");
            try {
              const res = await fetch("/api/settings/accounts/switch", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ profile }),
              });
              const data = await res.json().catch(() => ({}));
              if (res.ok) {
                showToast(`✅ Switched to ${data.email || profile}`, "success");
                await loadAccountProfiles();
                await loadModels();
              } else {
                showToast("Switch failed: " + (data.detail || "unknown"), "error");
                btn.disabled = false;
                btn.textContent = "Switch";
              }
            } catch (e) {
              showToast("Switch error: " + e.message, "error");
              btn.disabled = false;
              btn.textContent = "Switch";
            }
          });
        });
        accountProfileCards.querySelectorAll(".account-delete-btn").forEach((btn) => {
          btn.addEventListener("click", async () => {
            const profile = btn.dataset.profile;
            if (!await sableConfirm(`Delete ${profile}?\n\nThis permanently removes the browser data directory.`, { danger: true })) return;
            btn.disabled = true;
            btn.textContent = "Deleting…";
            try {
              const res = await fetch("/api/settings/accounts/delete", {
                method: "DELETE",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ profile }),
              });
              const data = await res.json().catch(() => ({}));
              if (res.ok) {
                showToast(`🗑️ Deleted ${profile}`, "success");
                await loadAccountProfiles();
              } else {
                showToast("Delete failed: " + (data.detail || "unknown"), "error");
                btn.disabled = false;
                btn.textContent = "Delete";
              }
            } catch (e) {
              showToast("Delete error: " + e.message, "error");
              btn.disabled = false;
              btn.textContent = "Delete";
            }
          });
        });
        accountProfileCards.querySelectorAll(".account-open-btn").forEach((btn) => {
          btn.addEventListener("click", async () => {
            const profile = btn.dataset.profile;
            btn.disabled = true;
            btn.textContent = "Opening…";
            try {
              const res = await fetch("/api/settings/accounts/open", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ profile }),
              });
              const data = await res.json().catch(() => ({}));
              if (res.ok) {
                showToast(`🌐 Opened browser for ${profile}`, "success");
              } else {
                showToast("Open failed: " + (data.detail || "unknown"), "error");
              }
            } catch (e) {
              showToast("Open error: " + e.message, "error");
            }
            btn.disabled = false;
            btn.textContent = "Open";
          });
        });
      } catch (e) {
        accountProfileCards.innerHTML = `<p class="muted" style="font-size:12px;margin:0;color:var(--danger);">Failed to load: ${e.message}</p>`;
      }
    }

    if (refreshAccountsBtn) {
      refreshAccountsBtn.addEventListener("click", loadAccountProfiles);
    }


    const addAccountBtn = document.getElementById("addAccountBtn");
    if (addAccountBtn) {
      addAccountBtn.addEventListener("click", async () => {
        addAccountBtn.disabled = true;
        addAccountBtn.textContent = "Opening…";
        try {
          const res = await fetch("/api/settings/accounts/create", { method: "POST" });
          const data = await res.json();
          if (!res.ok) throw new Error(data.detail || "Failed");
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
        if (tabName === 'general') { loadBrowserSettings(); initTelegramToggle(); }
        else if (tabName === 'account') loadAccountProfiles();
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
      // --- Themed tree: resolved data-URI for favicon + all logo imgs ---
      if (!window.__sableTree) {
        window.__sableTree = fetch("/static/assets/sable_tree.svg?v=4")
          .then(r => (r.ok ? r.text() : null)).catch(() => null);
      }
      window.__sableTree.then(txt => {
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
          window.__sableIconImgs = Array.from(document.querySelectorAll('img[src*="sable_icon"], img[src*="sable_tree"]'));
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

  // ── Browser Profile Restore ─────────────────────────────────
  async function loadBrowserProfiles() {
    const container = document.getElementById('browserProfileCards');
    if (!container) return;
    try {
      const res = await fetch('/api/settings/accounts/backups');
      const d = await res.json();
      const accounts = d.accounts || [];
      if (!accounts.length) {
        container.innerHTML = '<p class="muted" style="font-size:12px;margin:0;">No account profiles found.</p>';
        return;
      }
      let html = '';
      for (const acc of accounts) {
        const bakDot = acc.has_backup ? '\u{1F7E2}' : '\u{1F534}';
        html +=
          '<div style="background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:12px 14px;">' +
            '<div style="display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:6px;">' +
              '<span style="font-size:13px;font-weight:600;color:var(--text);min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">' + escHtml(acc.name) + (acc.email ? ' <span style="font-weight:400;font-size:11px;color:var(--text-dim);">' + escHtml(acc.email) + '</span>' : '') + '</span>' +
              '<span style="display:flex;gap:6px;flex-shrink:0;">' +
                '<button data-profile="' + escAttr(acc.name) + '" class="accBackupBtn" style="background:var(--panel);color:var(--accent);border:1px solid var(--accent);border-radius:6px;padding:5px 12px;font-size:11px;cursor:pointer;font-weight:600;">Backup</button>' +
                '<button data-profile="' + escAttr(acc.name) + '" class="accRestoreBtn" style="background:var(--accent);color:#fff;border:none;border-radius:6px;padding:5px 14px;font-size:11px;cursor:pointer;font-weight:600;' + (acc.has_backup ? '' : 'opacity:0.4;pointer-events:none;') + '">Restore</button>' +
              '</span>' +
            '</div>' +
            '<div style="display:grid;grid-template-columns:auto 1fr;gap:2px 12px;font-size:12px;color:var(--text-dim);">' +
              '<span>Size</span><span style="color:var(--text);">' + acc.size_mb + ' MB</span>' +
              '<span>Backup</span><span style="color:var(--text);">' + bakDot + ' ' + (acc.has_backup ? acc.backup_size_mb + ' MB' : 'None') + '</span>' +
            '</div>' +
          '</div>';
      }
      container.innerHTML = html;
      container.querySelectorAll('.accRestoreBtn').forEach(btn => {
        btn.addEventListener('click', () => restoreAccountProfile(btn.dataset.profile, btn));
      });
      container.querySelectorAll('.accBackupBtn').forEach(btn => {
        btn.addEventListener('click', () => backupAccountProfile(btn.dataset.profile, btn));
      });
    } catch {
      container.innerHTML = '<p class="muted" style="font-size:12px;margin:0;color:var(--danger);">Failed to load profiles.</p>';
    }
  }

  async function restoreAccountProfile(profile, btn) {
    if (!await sableConfirm('Restore ' + profile + ' from backup?\n\nThis DELETES the current profile and replaces it with the .bak snapshot.', { danger: true })) return;
    btn.disabled = true;
    btn.textContent = 'Restoring…';
    try {
      const res = await fetch('/api/settings/accounts/restore', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ profile })
      });
      const d = await res.json();
      if (res.ok) showToast(profile + ' restored', 'success');
      else showToast('Restore failed: ' + (d.detail || 'Unknown'), 'error');
    } catch { showToast('Restore failed — network error', 'error'); }
    btn.disabled = false;
    btn.textContent = 'Restore';
    await loadBrowserProfiles();
  }

  async function backupAccountProfile(profile, btn) {
    btn.disabled = true;
    btn.textContent = 'Backing up…';
    try {
      const res = await fetch('/api/settings/accounts/backup', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ profile })
      });
      const d = await res.json();
      if (res.ok) showToast(profile + ' backed up (' + d.size_mb + ' MB)', 'success');
      else showToast('Backup failed: ' + (d.detail || 'Unknown'), 'error');
    } catch { showToast('Backup failed — network error', 'error'); }
    btn.disabled = false;
    btn.textContent = 'Backup';
    await loadBrowserProfiles();
  }

  document.getElementById('refreshProfilesBtn')?.addEventListener('click', loadBrowserProfiles);

  document.getElementById('backupAllBtn')?.addEventListener('click', async () => {
    const btn = document.getElementById('backupAllBtn');
    btn.disabled = true; btn.textContent = '⬆ Backing up…';
    try {
      const res = await fetch('/api/settings/accounts/backups');
      const d = await res.json();
      const accounts = d.accounts || [];
      let ok = 0, fail = 0;
      for (const acc of accounts) {
        try {
          const r = await fetch('/api/settings/accounts/backup', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ profile: acc.name })
          });
          if (r.ok) ok++; else fail++;
        } catch { fail++; }
      }
      showToast('Backup All: ' + ok + ' done' + (fail ? ', ' + fail + ' failed' : ''), fail ? 'error' : 'success');
    } catch { showToast('Backup All failed — network error', 'error'); }
    btn.disabled = false; btn.textContent = '⬆ Backup All';
    await loadBrowserProfiles();
  });

  document.getElementById('restoreAllBtn')?.addEventListener('click', async () => {
    if (!await sableConfirm('Restore ALL profiles from .bak snapshots?\nThis DELETES current data and replaces with backups.', { danger: true })) return;
    const btn = document.getElementById('restoreAllBtn');
    btn.disabled = true; btn.textContent = '⬇ Restoring…';
    try {
      const res = await fetch('/api/settings/accounts/backups');
      const d = await res.json();
      const accounts = (d.accounts || []).filter(a => a.has_backup);
      if (!accounts.length) { showToast('No backups found to restore', 'error'); btn.disabled = false; btn.textContent = '⬇ Restore All'; return; }
      let ok = 0, fail = 0;
      for (const acc of accounts) {
        try {
          const r = await fetch('/api/settings/accounts/restore', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ profile: acc.name })
          });
          if (r.ok) ok++; else fail++;
        } catch { fail++; }
      }
      showToast('Restore All: ' + ok + ' done' + (fail ? ', ' + fail + ' failed' : ''), fail ? 'error' : 'success');
    } catch { showToast('Restore All failed — network error', 'error'); }
    btn.disabled = false; btn.textContent = '⬇ Restore All';
    await loadBrowserProfiles();
  });

  loadBrowserProfiles();
  // ── /Browser Profile Backup ────────────────────────────────


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
    navigator.clipboard.writeText(codeEl.textContent).then(() => {
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

    if (action === 'new-chat') {
      document.getElementById('newChat')?.click();
    } else if (action === 'settings') {
      document.getElementById('settingsBtn')?.click();
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
        const res = await fetch('/api/sync-context', { method: 'POST' });
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



