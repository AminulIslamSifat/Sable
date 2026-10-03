    // === Skills Panel ===
    const skillDetailOverlay = document.getElementById("skillDetailOverlay");
    const skillDetailClose = document.getElementById("skillDetailClose");
    let skillsLoaded = false;

    const DISABLED_SKILLS_KEY = "sable_disabled_skills";
    let _disabledSkillsCache = null;
    function getDisabledSkills() {
      if (_disabledSkillsCache) return _disabledSkillsCache;
      try { return JSON.parse(localStorage.getItem(DISABLED_SKILLS_KEY)) || []; } catch { return []; }
    }
    // Fetch disabled skills from backend on load
    (async () => {
      try {
        const res = await fetch("/api/settings/disabled-skills");
        const data = await res.json();
        if (Array.isArray(data.disabled)) {
          _disabledSkillsCache = data.disabled;
          localStorage.setItem(DISABLED_SKILLS_KEY, JSON.stringify(data.disabled));
        }
      } catch(e) {}
    })();
    function setDisabledSkills(arr) {
      try { localStorage.setItem(DISABLED_SKILLS_KEY, JSON.stringify(arr)); } catch (e) {}
    }

    async function loadSkills() {
      if (skillsLoaded) return;
      const toggleList = document.getElementById("skillsToggleList");
      if (!toggleList) return;
      try {
        const res = await fetch("/api/skills/browse");
        const data = await res.json();
        const skills = data.skills || [];
        const disabled = getDisabledSkills();
        toggleList.innerHTML = "";
        skills.forEach((sk) => {
          const row = document.createElement("div");
          const isDisabled = disabled.includes(sk.path);
          row.style.cssText = "display:flex;align-items:center;justify-content:space-between;padding:8px 12px;background:var(--bg-secondary);border-radius:8px;border:1px solid var(--border);";
          row.innerHTML = `<div><span style="font-size:13px;font-weight:500;">${escHtml(sk.name)}</span><span class="muted" style="font-size:11px;margin-left:8px;">${escHtml(sk.category)}</span></div>`;
          const toggle = document.createElement("label");
          toggle.style.cssText = "position:relative;display:inline-block;width:36px;height:20px;cursor:pointer;";
          toggle.innerHTML = `<input type="checkbox" ${isDisabled ? "" : "checked"} style="opacity:0;width:0;height:0;"><span style="position:absolute;inset:0;background:${isDisabled ? "var(--border)" : "var(--accent)"};border-radius:20px;transition:0.2s;"></span><span style="position:absolute;left:${isDisabled ? "2px" : "18px"};top:2px;width:16px;height:16px;background:#fff;border-radius:50%;transition:0.2s;"></span>`;
          toggle.querySelector("input").addEventListener("change", async (e) => {
            let d = getDisabledSkills();
            if (!e.target.checked) {
              if (!d.includes(sk.path)) d.push(sk.path);
            } else {
              d = d.filter(k => k !== sk.path);
            }
            setDisabledSkills(d);
            // Persist to backend
            try {
              const res = await fetch("/api/settings/disabled-skills", {
                method: "POST", headers: {"Content-Type": "application/json"},
                body: JSON.stringify({ disabled: d })
              });
              if (!res.ok) console.warn("Failed to persist disabled skills");
            } catch(err) { console.warn("Disabled skills persist error:", err); }
            // Update toggle visual
            const spans = toggle.querySelectorAll("span");
            spans[0].style.background = e.target.checked ? "var(--accent)" : "var(--border)";
            spans[1].style.left = e.target.checked ? "18px" : "2px";
          });
          row.appendChild(toggle);
          toggleList.appendChild(row);
        });
        skillsLoaded = true;
      } catch (e) { console.error("loadSkills failed", e); }
    }

    function showSkillDetail(sk) {
      document.getElementById("skillDetailName").textContent = sk.name;
      document.getElementById("skillDetailCat").textContent = sk.category || "—";
      document.getElementById("skillDetailPath").textContent = "skills/" + sk.path;

      // Scripts
      const scriptsRow = document.getElementById("skillScriptsRow");
      const scriptsEl = document.getElementById("skillDetailScripts");
      scriptsEl.innerHTML = "";
      if (sk.scripts && sk.scripts.length > 0) {
        scriptsRow.style.display = "";
        sk.scripts.forEach((s) => {
          const span = document.createElement("span");
          span.textContent = s;
          scriptsEl.appendChild(span);
        });
      } else {
        scriptsRow.style.display = "none";
      }

      // Render instruction.md as markdown
      const instrEl = document.getElementById("skillInstruction");
      instrEl.innerHTML = sk.instruction_content ? renderMarkdown(sk.instruction_content) : "<em>No instruction file.</em>";


      skillDetailOverlay.classList.add("show");
    }



    skillDetailClose.addEventListener("click", () => skillDetailOverlay.classList.remove("show"));
    skillDetailOverlay.addEventListener("click", (e) => {
      if (e.target === skillDetailOverlay) skillDetailOverlay.classList.remove("show");
    });

    // --- Tools tab: load and toggle tools ---
    let toolsLoaded = false;
    const DISABLED_TOOLS_KEY = "sable_disabled_tools";
    let _disabledToolsCache = null;
    function getDisabledTools() {
      if (_disabledToolsCache) return _disabledToolsCache;
      try { return JSON.parse(localStorage.getItem(DISABLED_TOOLS_KEY) || "[]"); } catch { return []; }
    }
    // Fetch disabled tools from backend on load (sync with server truth)
    (async () => {
      try {
        const res = await fetch("/api/settings/disabled-tools");
        const data = await res.json();
        if (Array.isArray(data.disabled)) {
          _disabledToolsCache = data.disabled;
          localStorage.setItem(DISABLED_TOOLS_KEY, JSON.stringify(data.disabled));
        }
      } catch(e) {}
    })();
    function setDisabledTools(arr) {
      _disabledToolsCache = arr;
      try { localStorage.setItem(DISABLED_TOOLS_KEY, JSON.stringify(arr)); } catch (e) {}
    }

    async function loadTools() {
      if (toolsLoaded) return;
      const toggleList = document.getElementById("toolsToggleList");
      if (!toggleList) return;
      try {
        const res = await fetch("/api/tools/browse");
        const data = await res.json();
        const tools = data.tools || [];
        const disabled = getDisabledTools();
        toggleList.innerHTML = "";
        tools.forEach((tl) => {
          const row = document.createElement("div");
          const isDisabled = disabled.includes(tl.key);
          row.style.cssText = "display:flex;align-items:center;justify-content:space-between;padding:8px 12px;background:var(--bg-secondary);border-radius:8px;border:1px solid var(--border);";
          const fnCount = (tl.tools || []).length;
          row.innerHTML = `<div><span style="font-size:13px;font-weight:500;">${escHtml(tl.name)}</span><span class="muted" style="font-size:11px;margin-left:8px;">${fnCount} function${fnCount !== 1 ? 's' : ''}</span></div>`;
          const toggle = document.createElement("label");
          toggle.style.cssText = "position:relative;display:inline-block;width:36px;height:20px;cursor:pointer;";
          toggle.innerHTML = `<input type="checkbox" ${isDisabled ? "" : "checked"} style="opacity:0;width:0;height:0;"><span style="position:absolute;inset:0;background:${isDisabled ? "var(--border)" : "var(--accent)"};border-radius:20px;transition:0.2s;"></span><span style="position:absolute;left:${isDisabled ? "2px" : "18px"};top:2px;width:16px;height:16px;background:#fff;border-radius:50%;transition:0.2s;"></span>`;
          toggle.querySelector("input").addEventListener("change", async (e) => {
            let d = getDisabledTools();
            if (!e.target.checked) {
              if (!d.includes(tl.key)) d.push(tl.key);
            } else {
              d = d.filter(k => k !== tl.key);
            }
            setDisabledTools(d);
            try {
              const res = await fetch("/api/settings/disabled-tools", {
                method: "POST", headers: {"Content-Type": "application/json"},
                body: JSON.stringify({ disabled: d })
              });
              if (!res.ok) console.warn("Failed to persist disabled tools");
            } catch(err) { console.warn("Disabled tools persist error:", err); }
            const spans = toggle.querySelectorAll("span");
            spans[0].style.background = e.target.checked ? "var(--accent)" : "var(--border)";
            spans[1].style.left = e.target.checked ? "18px" : "2px";
          });
          row.appendChild(toggle);
          toggleList.appendChild(row);
        });
        toolsLoaded = true;
      } catch (e) { console.error("loadTools failed", e); }
    }

    // --- Tool Call Format dropdown ---
    const toolFormatSelect = document.getElementById("toolFormatSelect");
    const toolFormatSaveBtn = document.getElementById("toolFormatSaveBtn");
    const toolFormatStatus = document.getElementById("toolFormatStatus");

    async function loadToolFormat() {
      if (!toolFormatSelect) return;
      try {
        const res = await fetch("/api/settings/tool-format");
        const data = await res.json();
        toolFormatSelect.value = data.tool_call_format || "";
      } catch (e) { console.warn("Failed to load tool format setting", e); }
    }

    if (toolFormatSaveBtn) {
      toolFormatSaveBtn.addEventListener("click", async () => {
        toolFormatSaveBtn.disabled = true;
        if (toolFormatStatus) toolFormatStatus.textContent = "Saving...";
        try {
          const res = await fetch("/api/settings/tool-format", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ tool_call_format: toolFormatSelect.value })
          });
          if (res.ok) {
            if (toolFormatStatus) { toolFormatStatus.textContent = "✓ Saved"; toolFormatStatus.style.color = "var(--accent)"; }
          } else {
            const err = await res.json().catch(() => ({}));
            if (toolFormatStatus) { toolFormatStatus.textContent = "✗ " + (err.detail || "Failed"); toolFormatStatus.style.color = "var(--error)"; }
          }
        } catch (e) {
          if (toolFormatStatus) { toolFormatStatus.textContent = "✗ Error"; toolFormatStatus.style.color = "var(--error)"; }
        }
        toolFormatSaveBtn.disabled = false;
        setTimeout(() => { if (toolFormatStatus) toolFormatStatus.textContent = ""; }, 3000);
      });
    }

    document.querySelector('[data-tab="personalization"]').addEventListener("click", () => {
      loadTools();
      loadSkills();
      loadToolFormat();
    });
    document.querySelector('[data-tab="account"]')?.addEventListener("click", () => { if (window.loadAvailableBrowsers) window.loadAvailableBrowsers(); if (typeof loadAccountProfiles === 'function') loadAccountProfiles(); });

    // --- Providers tab: Unified API key manager ---
    const _keyProviderMeta = {
      gemini:     { apiBase: "/api/settings/gemini",     name: "Gemini",     placeholder: "Paste API key (AIza…)" },
      groq:       { apiBase: "/api/settings/groq",       name: "Groq",       placeholder: "Paste API key (gsk_…)" },
      mistral:    { apiBase: "/api/settings/mistral",    name: "Mistral",    placeholder: "Paste API key (key: …)" },
      openai:     { apiBase: "/api/settings/openai",     name: "OpenAI",     placeholder: "Paste API key (sk-…)" },
      puter:      { apiBase: "/api/settings/puter",      name: "Puter",      placeholder: "Paste Puter token (eyJ…)" },
      cloudflare: { apiBase: "/api/settings/cloudflare", name: "Cloudflare", placeholder: "Paste API token (cfat_…)", singleToken: true },
    };
    const _keyEls = {
      select: document.getElementById("keyProviderSelect"),
      input:  document.getElementById("apiKeyInput"),
      input2: document.getElementById("apiKeyInput2"),
      btn:    document.getElementById("addApiKeyBtn"),
      list:   document.getElementById("apiKeyList"),
      status: document.getElementById("apiKeyStatus"),
    };
    let _currentKeyProvider = "gemini";

    async function _loadKeysFor(provider) {
      const meta = _keyProviderMeta[provider];
      if (!meta || !_keyEls.list) return;

      // Cloudflare: special status endpoint (no multi-key rotation)
      if (meta.singleToken) {
        try {
          const res = await fetch(`${meta.apiBase}/status`);
          const data = await res.json();
          _keyEls.list.innerHTML = "";
          if (!data.available) {
            _keyEls.list.innerHTML = '<div style="font-size:12px;color:var(--text);padding:8px 0;">Not configured yet.</div>';
            _keyEls.status.textContent = "";
            return;
          }
          const budget = data.budget || {};
          const row = document.createElement("div");
          row.style.cssText = "display:flex;align-items:center;justify-content:space-between;padding:6px 10px;border-radius:8px;background:color-mix(in srgb, var(--panel) 60%, transparent);border:1px solid var(--border);";
          const label = document.createElement("span");
          label.style.cssText = "font-size:12px;font-family:monospace;color:var(--text);";
          label.textContent = `✓ Configured · ~${budget.estimated_images_per_day || "?"} images/day free`;
          const delBtn = document.createElement("button");
          delBtn.textContent = "✕";
          delBtn.style.cssText = "background:transparent;border:none;color:var(--danger);cursor:pointer;font-size:13px;padding:2px 6px;border-radius:4px;";
          delBtn.title = "Remove credentials";
          delBtn.addEventListener("click", async () => {
            if (!await sableConfirm(`Remove ${meta.name} credentials?`, { danger: true })) return;
            try {
              await fetch(`${meta.apiBase}/credentials`, { method: "DELETE" });
              _loadKeysFor(_currentKeyProvider);
            } catch (e) { showToast("Failed to remove", "error"); }
          });
          row.appendChild(label);
          row.appendChild(delBtn);
          _keyEls.list.appendChild(row);
          _keyEls.status.textContent = `${data.models?.length || 0} models available · free tier resets daily at UTC midnight`;
        } catch (e) {
          _keyEls.status.textContent = "Failed to check status";
        }
        return;
      }

      try {
        const res = await fetch(`${meta.apiBase}/keys`);
        const data = await res.json();
        const keys = data.keys || [];
        _keyEls.list.innerHTML = "";
        if (keys.length === 0) {
          _keyEls.list.innerHTML = '<div style="font-size:12px;color:var(--text);padding:8px 0;">No keys configured yet.</div>';
          _keyEls.status.textContent = "";
          return;
        }
        keys.forEach((k) => {
          const row = document.createElement("div");
          row.style.cssText = "display:flex;align-items:center;justify-content:space-between;padding:6px 10px;border-radius:8px;background:color-mix(in srgb, var(--panel) 60%, transparent);border:1px solid var(--border);";
          const label = document.createElement("span");
          label.style.cssText = "font-size:12px;font-family:monospace;color:var(--text);";
          label.textContent = k.masked + (k.active ? " ●" : "");
          const delBtn = document.createElement("button");
          delBtn.textContent = "✕";
          delBtn.style.cssText = "background:transparent;border:none;color:var(--danger);cursor:pointer;font-size:13px;padding:2px 6px;border-radius:4px;";
          delBtn.title = "Remove key";
          delBtn.addEventListener("click", async () => {
            if (!await sableConfirm(`Remove this ${meta.name} API key?`, { danger: true })) return;
            try {
              await fetch(`${meta.apiBase}/api-key/${k.index}`, { method: "DELETE" });
              _loadKeysFor(_currentKeyProvider);
            } catch (e) { showToast("Failed to remove key", "error"); }
          });
          row.appendChild(label);
          row.appendChild(delBtn);
          _keyEls.list.appendChild(row);
        });
        _keyEls.status.textContent = `${keys.length} key${keys.length !== 1 ? "s" : ""} configured · auto-rotation enabled`;
      } catch (e) {
        _keyEls.status.textContent = "Failed to load keys";
      }
    }

    function _switchKeyProvider(provider) {
      if (!provider) return;
      _currentKeyProvider = provider;
      const meta = _keyProviderMeta[provider];
      if (_keyEls.input && meta) _keyEls.input.placeholder = meta.placeholder;
      if (_keyEls.input) _keyEls.input.value = "";
      // Hide second input (no longer needed — account ID is auto-fetched)
      if (_keyEls.input2) {
        _keyEls.input2.style.display = "none";
        _keyEls.input2.value = "";
      }
      _loadKeysFor(provider);
    }

    if (_keyEls.select) {
      _keyEls.select.addEventListener("change", () => _switchKeyProvider(_keyEls.select.value));
    }
    if (_keyEls.btn) {
      _keyEls.btn.addEventListener("click", async () => {
        const key = _keyEls.input?.value?.trim();
        if (!key) { showToast("Paste an API key first", "error"); return; }
        const meta = _keyProviderMeta[_currentKeyProvider];
        if (!meta) return;

        // Cloudflare: single token (account ID auto-fetched server-side)
        if (meta.singleToken) {
          try {
            const res = await fetch(`${meta.apiBase}/credentials`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ api_token: key }),
            });
            if (!res.ok) {
              const err = await res.json().catch(() => ({}));
              showToast(err.detail || "Failed to save credentials", "error");
              return;
            }
            if (_keyEls.input) _keyEls.input.value = "";
            if (_keyEls.input2) _keyEls.input2.value = "";
            showToast(`${meta.name} credentials saved ✓`, "success");
            _loadKeysFor(_currentKeyProvider);
          } catch (e) { showToast("Failed to save credentials", "error"); }
          return;
        }

        try {
          const res = await fetch(`${meta.apiBase}/api-key`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ api_key: key }),
          });
          if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            const detail = typeof err.detail === "string" ? err.detail : (Array.isArray(err.detail) ? err.detail[0]?.msg : JSON.stringify(err.detail));
            showToast(detail || "Failed to add key", "error");
            return;
          }
          if (_keyEls.input) _keyEls.input.value = "";
          showToast(`${meta.name} key added ✓`, "success");
          _loadKeysFor(_currentKeyProvider);
        } catch (e) { showToast("Failed to add key", "error"); }
      });
    }
    if (_keyEls.input) {
      _keyEls.input.addEventListener("keydown", (e) => { if (e.key === "Enter") _keyEls.btn?.click(); });
    }


    // --- Provider model fetching ---
    const providerSelect = document.getElementById("customModelBackend");
    const modelSelect = document.getElementById("customModelId");
    const modelLabelInput = document.getElementById("customModelLabel");
    let _fetchedModels = []; // cache of {id, label} from last fetch

    async function fetchProviderModels(provider) {
      const urlFields = document.getElementById("customUrlFields");
      if (urlFields) urlFields.style.display = (provider === "url") ? "flex" : "none";
      if (provider === "url") {
        modelSelect.disabled = true;
        modelSelect.innerHTML = '<option value="">Enter URL and fetch models</option>';
        modelLabelInput.value = "";
        const _urlSt = document.getElementById("customUrlStatus");
        if (_urlSt) _urlSt.textContent = "";
        return;
      }
      if (!provider) {
        modelSelect.disabled = true;
        modelSelect.innerHTML = '<option value="">Select provider first…</option>';
        return;
      }
      modelSelect.disabled = true;
      modelSelect.innerHTML = '<option value="">Loading models…</option>';
      modelLabelInput.value = "";
      const _modelsUrl = provider.startsWith("endpoint:")
        ? `/api/settings/endpoints/${provider.slice("endpoint:".length)}/models`
        : `/api/settings/providers/${provider}/models`;
      try {
        const res = await fetch(_modelsUrl);
        const data = await res.json();
        _fetchedModels = data.models || [];
        if (!data.available) {
          const _unavailMsg = data.error || "No API key configured for this provider";
          modelSelect.innerHTML = '<option value="">⚠️ ' + _unavailMsg + '</option>';
          modelSelect.disabled = true;
          return;
        }
        if (_fetchedModels.length === 0) {
          modelSelect.innerHTML = '<option value="">No models found</option>';
          modelSelect.disabled = true;
          return;
        }
        modelSelect.innerHTML = '<option value="">— Choose a model —</option>';
        const provLabel = provider.charAt(0).toUpperCase() + provider.slice(1);
        _fetchedModels.forEach((m) => {
          const opt = document.createElement("option");
          opt.value = m.id;
          opt.textContent = `${provLabel}: ${m.label}`;
          modelSelect.appendChild(opt);
        });
        modelSelect.disabled = false;
      } catch (e) {
        modelSelect.innerHTML = '<option value="">Failed to fetch models</option>';
        modelSelect.disabled = true;
      }
    }

    if (providerSelect) {
      providerSelect.addEventListener("change", () => fetchProviderModels(providerSelect.value));
    }
    if (modelSelect) {
      modelSelect.addEventListener("change", () => {
        const selected = _fetchedModels.find((m) => m.id === modelSelect.value);
        modelLabelInput.value = selected ? selected.label : "";
      });
    }
    const fetchUrlModelsBtn = document.getElementById("fetchUrlModelsBtn");
    if (fetchUrlModelsBtn) {
      fetchUrlModelsBtn.addEventListener("click", async () => {
        const endpoint = document.getElementById("customModelEndpoint")?.value.trim();
        const urlKey = document.getElementById("customModelUrlKey")?.value.trim();
        const statusEl = document.getElementById("customUrlStatus");
        if (!endpoint) { showToast("Enter the base URL first", "error"); return; }
        if (statusEl) statusEl.textContent = "Fetching models…";
        modelSelect.disabled = true;
        modelSelect.innerHTML = '<option value="">Loading…</option>';
        modelLabelInput.value = "";
        try {
          const res = await fetch("/api/settings/providers/custom/models", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ base_url: endpoint, api_key: urlKey }),
          });
          const data = await res.json();
          _fetchedModels = data.models || [];
          if (!res.ok || !data.available) {
            const msg = (typeof data.error === "string" && data.error) || (typeof data.detail === "string" && data.detail) || "Failed to fetch models";
            modelSelect.innerHTML = '<option value="">' + msg + '</option>';
            modelSelect.disabled = true;
            if (statusEl) statusEl.textContent = "";
            showToast(msg, "error");
            return;
          }
          if (_fetchedModels.length === 0) {
            modelSelect.innerHTML = '<option value="">No models found at this endpoint</option>';
            modelSelect.disabled = true;
            if (statusEl) statusEl.textContent = "";
            return;
          }
          modelSelect.innerHTML = '<option value="">— Choose a model —</option>';
          _fetchedModels.forEach((m) => {
            const opt = document.createElement("option");
            opt.value = m.id;
            opt.textContent = m.label;
            modelSelect.appendChild(opt);
          });
          modelSelect.disabled = false;
          if (statusEl) statusEl.textContent = _fetchedModels.length + " models found";
        } catch (e) {
          modelSelect.innerHTML = '<option value="">Failed to reach endpoint</option>';
          modelSelect.disabled = true;
          if (statusEl) statusEl.textContent = "";
          showToast("Failed to reach endpoint", "error");
        }
      });
    }

    let _customEndpoints = [];

    function _refreshEndpointOptions() {
      const backendSel = document.getElementById("customModelBackend");
      if (!backendSel) return;
      Array.from(backendSel.querySelectorAll("option")).forEach((o) => {
        if (o.value.startsWith("endpoint:")) o.remove();
      });
      _customEndpoints.forEach((ep) => {
        const opt = document.createElement("option");
        opt.value = "endpoint:" + ep.id;
        opt.textContent = "🔗 " + ep.name;
        backendSel.appendChild(opt);
      });
    }

    async function loadCustomEndpoints() {
      const listEl = document.getElementById("endpointList");
      const statusEl = document.getElementById("endpointStatus");
      if (!listEl) return;
      try {
        const res = await fetch("/api/settings/endpoints");
        const data = await res.json();
        _customEndpoints = data.endpoints || [];
        listEl.innerHTML = "";
        if (_customEndpoints.length === 0) {
          listEl.innerHTML = '<div style="font-size:12px;color:var(--text);padding:4px 0;">No saved endpoints yet.</div>';
        } else {
          _customEndpoints.forEach((ep) => {
            const row = document.createElement("div");
            row.style.cssText = "display:flex;align-items:center;justify-content:space-between;gap:8px;padding:6px 10px;border-radius:8px;border:1px solid var(--border);background:var(--panel);";
            const info = document.createElement("span");
            info.style.cssText = "font-size:12px;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;";
            const nm = document.createElement("b");
            nm.textContent = ep.name;
            const urlSpan = document.createElement("span");
            urlSpan.style.cssText = "font-family:monospace;font-size:10px;opacity:0.7;margin-left:6px;";
            urlSpan.textContent = ep.base_url;
            info.appendChild(nm);
            info.appendChild(urlSpan);
            if (ep.has_key) {
              const kSpan = document.createElement("span");
              kSpan.style.cssText = "font-size:10px;opacity:0.6;margin-left:6px;";
              kSpan.textContent = "🔑 " + ep.api_key_masked;
              info.appendChild(kSpan);
            }
            const del = document.createElement("button");
            del.textContent = "✕";
            del.title = "Remove endpoint";
            del.style.cssText = "background:transparent;border:none;color:var(--danger);cursor:pointer;font-size:13px;padding:2px 6px;flex-shrink:0;";
            del.addEventListener("click", async () => {
              if (!await sableConfirm("Remove endpoint " + ep.name + "?", { danger: true })) return;
              try {
                await fetch("/api/settings/endpoints/" + ep.id, { method: "DELETE" });
                showToast("Endpoint removed", "success");
                loadCustomEndpoints();
              } catch (e) {
                showToast("Failed to remove endpoint", "error");
              }
            });
            row.appendChild(info);
            row.appendChild(del);
            listEl.appendChild(row);
          });
        }
        if (statusEl) statusEl.textContent = _customEndpoints.length ? (_customEndpoints.length + " endpoint" + (_customEndpoints.length !== 1 ? "s" : "") + " saved") : "";
        _refreshEndpointOptions();
      } catch (e) {
        if (statusEl) statusEl.textContent = "Failed to load endpoints";
      }
    }

    const addEndpointBtn = document.getElementById("addEndpointBtn");
    if (addEndpointBtn) {
      addEndpointBtn.addEventListener("click", async () => {
        const epName = document.getElementById("endpointName")?.value.trim();
        const epUrl = document.getElementById("endpointUrl")?.value.trim();
        const epKey = document.getElementById("endpointKey")?.value.trim();
        if (!epUrl) { showToast("Enter the base URL", "error"); return; }
        try {
          const res = await fetch("/api/settings/endpoints", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name: epName, base_url: epUrl, api_key: epKey }),
          });
          if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            showToast(err.detail || "Failed to save endpoint", "error");
            return;
          }
          document.getElementById("endpointName").value = "";
          document.getElementById("endpointUrl").value = "";
          document.getElementById("endpointKey").value = "";
          showToast("Endpoint saved", "success");
          loadCustomEndpoints();
        } catch (e) {
          showToast("Failed to save endpoint", "error");
        }
      });
    }

    loadCustomEndpoints();

    document.querySelector('[data-tab="providers"]')?.addEventListener("click", () => {
      const selected = _keyEls.select?.value;
      if (selected) {
        _switchKeyProvider(selected);
      } else {
        // No provider selected — clear list and show hint
        if (_keyEls.list) _keyEls.list.innerHTML = '<div style="font-size:12px;color:var(--text-muted, #888);padding:8px 0;">Select a provider above to manage API keys.</div>';
        if (_keyEls.status) _keyEls.status.textContent = "";
        if (_keyEls.input) _keyEls.input.placeholder = "Select a provider first…";
      }
      loadCustomModels();
      loadCustomEndpoints();
      if (typeof loadSearchSettings === "function") loadSearchSettings();
      if (typeof loadJitterSettings === "function") loadJitterSettings();
      if (typeof loadPromptInjectionSettings === "function") loadPromptInjectionSettings();
    });

    // --- Request Jitter section (inside Providers tab) ---
    const _jitterEls = {
      enabled: () => document.getElementById("jitterEnabled"),
      min: () => document.getElementById("jitterMin"),
      max: () => document.getElementById("jitterMax"),
      providers: () => document.getElementById("jitterProviders"),
      status: () => document.getElementById("jitterStatus"),
    };
    const _JITTER_LABELS = { deepseek: "DeepSeek", deepseek_scraper: "DeepSeek (scraper)", qwen: "Qwen", zai: "z.ai", gemini: "Gemini" };
    let _jitterProviders = {};

    function _renderJitterProviders() {
      const box = _jitterEls.providers();
      if (!box) return;
      box.innerHTML = "";
      Object.keys(_JITTER_LABELS).forEach((prov) => {
        const on = !!_jitterProviders[prov];
        const label = document.createElement("label");
        label.style.cssText = "font-size:11px;color:var(--text);display:flex;align-items:center;gap:4px;cursor:pointer;";
        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.checked = on;
        cb.style.cssText = "accent-color:var(--accent);width:13px;height:13px;";
        cb.addEventListener("change", () => {
          _jitterProviders[prov] = cb.checked;
          window._universalSave?.markDirty("jitter");
        });
        label.appendChild(cb);
        label.appendChild(document.createTextNode(" " + _JITTER_LABELS[prov]));
        box.appendChild(label);
      });
    }

    async function loadJitterSettings() {
      try {
        const res = await fetch("/api/settings/jitter");
        if (!res.ok) return;
        const d = await res.json();
        const en = _jitterEls.enabled(); if (en) en.checked = !!d.enabled;
        const mn = _jitterEls.min(); if (mn) mn.value = d.min ?? 3;
        const mx = _jitterEls.max(); if (mx) mx.value = d.max ?? 12;
        _jitterProviders = d.providers || {};
        _renderJitterProviders();
        const st = _jitterEls.status();
        if (st) st.textContent = "Loaded.";
      } catch (e) {
        console.error("Failed to load jitter settings:", e);
      }
    }

    _jitterEls.enabled()?.addEventListener("change", () => window._universalSave?.markDirty("jitter"));
    _jitterEls.min()?.addEventListener("input", () => window._universalSave?.markDirty("jitter"));
    _jitterEls.max()?.addEventListener("input", () => window._universalSave?.markDirty("jitter"));

    async function _saveJitterSettings() {
      const payload = {
        enabled: !!_jitterEls.enabled()?.checked,
        min: parseFloat(_jitterEls.min()?.value || "3"),
        max: parseFloat(_jitterEls.max()?.value || "12"),
        providers: { ..._jitterProviders },
      };
      const res = await fetch("/api/settings/jitter", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || data.error || "Save failed");
      const st = _jitterEls.status();
      if (st) st.textContent = "Saved.";
    }

    window._universalSave?.register("jitter", _saveJitterSettings);
    window.loadJitterSettings = loadJitterSettings;

    // --- Prompt Injection: provider picker + section switches ---
    const _piEls = {
      select: () => document.getElementById("piProviderSelect"),
      sections: () => document.getElementById("piSections"),
      empty: () => document.getElementById("piEmpty"),
      count: () => document.getElementById("piCount"),
      status: () => document.getElementById("piStatus"),
      allOn: () => document.getElementById("piAllOn"),
      allOff: () => document.getElementById("piAllOff"),
      reload: () => document.getElementById("piReload"),
    };
    let _piProviders = {};      // provider -> {key: bool}
    let _piKeys = [];
    let _piKeyLabels = {};
    let _piProviderLabels = {};
    let _piRows = [];           // [{key,label,group,models,url}]
    let _piActive = "";         // currently selected provider
    let _piSwitches = [];       // checkbox elements for _piActive

    // Keys grouped by where they act, so the panel reads as sections not a dump.
    const _PI_GROUPS = [
      { title: "System Prompt", keys: ["persona", "output_format", "skills", "tools", "tool_format", "mcp", "output_dir", "facts", "git"] },
      { title: "Message Prefix", keys: ["chat_title", "platform", "schedule", "timestamp", "memory"] },
    ];

    function _piUpdateCount() {
      const c = _piEls.count();
      if (!c) return;
      if (!_piActive) { c.textContent = ""; return; }
      const row = _piProviders[_piActive] || {};
      const on = _piKeys.filter((k) => row[k] !== false).length;
      c.textContent = on + " / " + _piKeys.length + " on";
    }

    function _piRender() {
      const box = _piEls.sections();
      const empty = _piEls.empty();
      if (!box || !empty) return;

      _piSwitches = [];
      box.innerHTML = "";

      if (!_piActive) {
        box.style.display = "none";
        empty.style.display = "block";
        _piUpdateCount();
        return;
      }
      box.style.display = "flex";
      empty.style.display = "none";

      const row = _piProviders[_piActive] || {};

      // If this provider is a custom endpoint covering several models, say so
      // — otherwise it looks like the toggle only affects one of them.
      const meta = _piRows.find((r) => r.key === _piActive);
      if (meta && meta.models && meta.models.length) {
        const info = document.createElement("div");
        info.style.cssText = "font-size:10.5px;color:var(--text-dim);padding:4px 8px 6px;line-height:1.5;";
        info.textContent = (meta.url ? meta.url + "\n" : "") + "Models: " + meta.models.join(", ");
        info.style.whiteSpace = "pre-wrap";
        box.appendChild(info);
      }

      _PI_GROUPS.forEach((grp) => {
        const hdr = document.createElement("div");
        hdr.textContent = grp.title;
        hdr.style.cssText = "font-size:10.5px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--text-dim);margin:10px 0 2px;";
        box.appendChild(hdr);

        grp.keys.forEach((key) => {
          const on = row[key] !== false;

          const line = document.createElement("label");
          line.style.cssText = "display:flex;align-items:center;gap:8px;padding:5px 8px;border-radius:6px;cursor:pointer;font-size:11.5px;color:var(--text);";
          line.addEventListener("mouseenter", () => { line.style.background = "rgba(255,255,255,.04)"; });
          line.addEventListener("mouseleave", () => { line.style.background = "transparent"; });

          const cb = document.createElement("input");
          cb.type = "checkbox";
          cb.checked = on;
          cb.style.cssText = "accent-color:var(--accent);width:14px;height:14px;cursor:pointer;flex:0 0 auto;";
          cb.addEventListener("change", () => {
            if (!_piProviders[_piActive]) _piProviders[_piActive] = {};
            _piProviders[_piActive][key] = cb.checked;
            _piUpdateCount();
            window._universalSave?.markDirty("prompt-injection");
            const st = _piEls.status();
            if (st) st.textContent = "Unsaved changes.";
          });
          _piSwitches.push(cb);

          const txt = document.createElement("span");
          txt.textContent = _piKeyLabels[key] || key;

          line.appendChild(cb);
          line.appendChild(txt);
          box.appendChild(line);
        });
      });

      _piUpdateCount();
    }

    function _piPopulateProviders() {
      const sel = _piEls.select();
      if (!sel) return;
      const keep = _piActive;
      sel.innerHTML = "";
      const ph = document.createElement("option");
      ph.value = "";
      ph.textContent = "Select provider…";
      sel.appendChild(ph);

      // Group rows into <optgroup>s so user-added endpoints are visually
      // separate from built-in backends.
      const groups = [];
      const byName = {};
      _piRows.forEach((r) => {
        const g = r.group || "Backends";
        if (!byName[g]) { byName[g] = []; groups.push(g); }
        byName[g].push(r);
      });

      groups.forEach((g) => {
        const og = document.createElement("optgroup");
        og.label = g;
        byName[g].forEach((r) => {
          const o = document.createElement("option");
          o.value = r.key;
          o.textContent = r.label || r.key;
          if (r.url) o.title = r.url;
          og.appendChild(o);
        });
        sel.appendChild(og);
      });

      sel.value = keep || "";
    }

    function _piSetAll(val) {
      if (!_piActive) return;
      if (!_piProviders[_piActive]) _piProviders[_piActive] = {};
      _piKeys.forEach((k) => { _piProviders[_piActive][k] = val; });
      _piSwitches.forEach((cb) => { cb.checked = val; });
      _piUpdateCount();
      window._universalSave?.markDirty("prompt-injection");
      const st = _piEls.status();
      if (st) st.textContent = "Unsaved changes.";
    }

    async function loadPromptInjectionSettings() {
      try {
        const res = await fetch("/api/settings/prompt-injection");
        if (!res.ok) {
          const st = _piEls.status();
          if (st) st.textContent = "Failed to load (HTTP " + res.status + "). Server may need a restart.";
          return;
        }
        const d = await res.json();
        _piProviders = d.providers || {};
        _piKeys = d.keys || [];
        _piKeyLabels = d.key_labels || {};
        _piProviderLabels = d.provider_labels || {};
        _piRows = d.rows || (d.available_providers || []).map((k) => ({ key: k, label: _piProviderLabels[k] || k, group: "Backends" }));
        _piPopulateProviders();
        _piRender();
        const st = _piEls.status();
        if (st) st.textContent = "Loaded.";
      } catch (e) {
        console.error("Failed to load prompt-injection settings:", e);
        const st = _piEls.status();
        if (st) st.textContent = "Failed to load: " + e.message;
      }
    }

    async function _savePromptInjectionSettings() {
      const res = await fetch("/api/settings/prompt-injection", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ providers: _piProviders }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || data.error || "Save failed");
      if (data.providers) _piProviders = data.providers;
      _piRender();
      const st = _piEls.status();
      if (st) st.textContent = "Saved.";
    }

    _piEls.select()?.addEventListener("change", (e) => {
      _piActive = e.target.value || "";
      _piRender();
      const st = _piEls.status();
      if (st) st.textContent = _piActive ? "" : "Select a provider to configure its injections.";
    });
    _piEls.allOn()?.addEventListener("click", () => _piSetAll(true));
    _piEls.allOff()?.addEventListener("click", () => _piSetAll(false));
    _piEls.reload()?.addEventListener("click", () => loadPromptInjectionSettings());

    window._universalSave?.register("prompt-injection", _savePromptInjectionSettings);
    window.loadPromptInjectionSettings = loadPromptInjectionSettings;

    // --- Search Engine section (inside Providers tab) ---
    const _searchEls = {
      provider: () => document.getElementById("searchProviderSelect"),
      searxngSection: () => document.getElementById("searchSearxngSection"),
      searxngUrl: () => document.getElementById("searchSearxngUrl"),
      fallback: () => document.getElementById("searchFallbackChain"),
      resultCount: () => document.getElementById("searchResultCount"),
      safesearch: () => document.getElementById("searchSafesearch"),
      saveBtn: () => document.getElementById("searchSaveBtn"),
      testBtn: () => document.getElementById("searchTestBtn"),
      clearCacheBtn: () => document.getElementById("searchClearCacheBtn"),
      status: () => document.getElementById("searchStatus"),
      hint: () => document.getElementById("searchProviderHint"),
      noKeyNeeded: () => document.getElementById("searchNoKeyNeeded"),
      // Multi-key API management
      keyProviderSelect: () => document.getElementById("searchKeyProviderSelect"),
      apiKeyInput: () => document.getElementById("searchApiKeyInput"),
      addKeyBtn: () => document.getElementById("addSearchApiKeyBtn"),
      keyList: () => document.getElementById("searchApiKeyList"),
      keyStatus: () => document.getElementById("searchApiKeyStatus"),
      // Google PSE CX
      googlePseCxRow: () => document.getElementById("searchGooglePseCxRow"),
      googlePseCxInput: () => document.getElementById("searchInputGooglePseCx"),
      saveGooglePseCxBtn: () => document.getElementById("saveGooglePseCxBtn"),
      // Test & compare controls
      testQuery: () => document.getElementById("searchTestQuery"),
      testCount: () => document.getElementById("searchTestCount"),
      testProvider: () => document.getElementById("searchTestProvider"),
      testResults: () => document.getElementById("searchTestResults"),
      compareBtn: () => document.getElementById("searchCompareBtn"),
      compareControls: () => document.getElementById("searchCompareControls"),
      compareA: () => document.getElementById("searchCompareA"),
      compareB: () => document.getElementById("searchCompareB"),
      compareRunBtn: () => document.getElementById("searchCompareRunBtn"),
      compareCloseBtn: () => document.getElementById("searchCompareCloseBtn"),
      compareResults: () => document.getElementById("searchCompareResults"),
      // Fallback dropdown
      fallbackDropdown: () => document.getElementById("searchFallbackDropdown"),
      fallbackSelected: () => document.getElementById("searchFallbackSelected"),
      fallbackOptions: () => document.getElementById("searchFallbackOptions"),
    };

    function _updateSearchProviderUI() {
      const provider = _searchEls.provider()?.value || "searxng";
      const section = _searchEls.searxngSection();
      if (section) section.style.display = provider === "searxng" ? "" : "none";

      const hints = {
        searxng: "Self-hosted meta-search. No API key needed.",
        brave: "Get your key at brave.com/search/api",
        duckduckgo: "Free, no key. Rate-limited.",
        google_pse: "Get keys at programmablesearchengine.google.com",
        tavily: "Get your key at tavily.com",
        serper: "Get your key at serper.dev",
        disabled: "All web search disabled.",
      };
      const hintEl = _searchEls.hint();
      if (hintEl) hintEl.textContent = hints[provider] || "";

      // Show/hide Google PSE CX row based on key provider selection
      const keyProv = _searchEls.keyProviderSelect()?.value;
      const cxRow = _searchEls.googlePseCxRow();
      if (cxRow) cxRow.style.display = keyProv === "google_pse" ? "" : "none";
    }

    // --- Search Provider Multi-Key Management ---
    let _currentSearchKeyProvider = "";

    async function _loadSearchKeys(provider) {
      const listEl = _searchEls.keyList();
      const statusEl = _searchEls.keyStatus();
      if (!listEl) return;
      if (!provider) {
        listEl.innerHTML = '<div class="muted" style="font-size:11px;">Select a provider to manage keys.</div>';
        if (statusEl) statusEl.textContent = "";
        return;
      }
      try {
        const res = await fetch(`/api/settings/search/${provider}/keys`);
        if (!res.ok) throw new Error("Failed to load keys");
        const data = await res.json();
        const keys = data.keys || [];
        if (keys.length === 0) {
          listEl.innerHTML = '<div class="muted" style="font-size:11px;">No keys added yet.</div>';
          if (statusEl) statusEl.textContent = "";
          return;
        }
        listEl.innerHTML = "";
        keys.forEach((k, idx) => {
          const row = document.createElement("div");
          row.style.cssText = "display:flex;align-items:center;justify-content:space-between;padding:6px 10px;background:var(--bg-secondary);border-radius:8px;border:1px solid var(--border);";
          row.innerHTML = `<span style="font-size:12px;font-family:monospace;color:var(--text);">${_escHtml(k.masked)}</span>`;
          const delBtn = document.createElement("button");
          delBtn.textContent = "✕";
          delBtn.title = "Remove key";
          delBtn.style.cssText = "background:none;border:none;color:var(--danger);cursor:pointer;font-size:14px;padding:2px 6px;border-radius:4px;";
          delBtn.addEventListener("click", async () => {
            try {
              const r = await fetch(`/api/settings/search/${provider}/api-key/${idx}`, { method: "DELETE" });
              if (!r.ok) throw new Error("Delete failed");
              await _loadSearchKeys(provider);
            } catch (e) {
              if (statusEl) statusEl.textContent = "Error: " + e.message;
            }
          });
          row.appendChild(delBtn);
          listEl.appendChild(row);
        });
        if (statusEl) statusEl.textContent = `${keys.length} key${keys.length !== 1 ? "s" : ""} configured — auto-rotates on rate limit.`;
      } catch (e) {
        listEl.innerHTML = `<div style="color:var(--danger);font-size:11px;">Error loading keys: ${_escHtml(e.message)}</div>`;
      }
    }

    // Wire up search key provider select
    _searchEls.keyProviderSelect()?.addEventListener("change", () => {
      _currentSearchKeyProvider = _searchEls.keyProviderSelect()?.value || "";
      _loadSearchKeys(_currentSearchKeyProvider);
      _updateSearchProviderUI();
    });

    // Wire up Add key button
    _searchEls.addKeyBtn()?.addEventListener("click", async () => {
      const provider = _searchEls.keyProviderSelect()?.value;
      const input = _searchEls.apiKeyInput();
      const statusEl = _searchEls.keyStatus();
      if (!provider) {
        if (statusEl) statusEl.textContent = "Select a provider first.";
        return;
      }
      const key = input?.value?.trim();
      if (!key) {
        if (statusEl) statusEl.textContent = "Paste an API key first.";
        return;
      }
      try {
        const res = await fetch(`/api/settings/search/${provider}/api-key`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ api_key: key })
        });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail || err.error || "Add failed");
        }
        if (input) input.value = "";
        if (statusEl) statusEl.textContent = "Key added!";
        await _loadSearchKeys(provider);
      } catch (e) {
        if (statusEl) statusEl.textContent = "Error: " + e.message;
      }
    });

    // --- Fallback chain: add-from-dropdown (no drag-and-drop) ---
    const _fallbackUI = (() => {
      let providers = [];
      let selected = []; // ordered list of provider names

      function syncHidden() {
        const h = _searchEls.fallback();
        if (h) h.value = selected.join(", ");
      }

      function markDirty() {
        window._universalSave?.markDirty("providers");
      }

      function renderChips() {
        const el = _searchEls.fallbackSelected();
        if (!el) return;
        el.innerHTML = "";
        if (selected.length === 0) {
          el.innerHTML = '<span class="muted" style="font-size:11px;">No fallback engines added</span>';
          syncHidden();
          return;
        }
        selected.forEach((name) => {
          const chip = document.createElement("span");
          chip.style.cssText = "display:inline-flex;align-items:center;gap:4px;font-size:11px;padding:2px 8px;border-radius:10px;background:color-mix(in srgb, var(--accent) 18%, transparent);border:1px solid var(--border);color:var(--text);";
          chip.textContent = name;
          const x = document.createElement("span");
          x.textContent = "×";
          x.title = "Remove";
          x.style.cssText = "cursor:pointer;opacity:.7;font-weight:700;margin-left:2px;";
          x.addEventListener("click", (ev) => {
            ev.stopPropagation();
            selected = selected.filter((p) => p !== name);
            renderChips();
            renderOptions();
            markDirty();
          });
          chip.appendChild(x);
          el.appendChild(chip);
        });
        syncHidden();
      }

      function renderOptions() {
        const el = _searchEls.fallbackOptions();
        if (!el) return;
        el.innerHTML = "";
        const currentPrimary = _searchEls.provider()?.value || "searxng";
        const available = providers.filter((p) => p !== "disabled" && p !== currentPrimary && !selected.includes(p));
        if (available.length === 0) {
          el.innerHTML = '<div class="muted" style="padding:8px 10px;font-size:11px;">All available engines already added</div>';
          return;
        }
        available.forEach((name) => {
          const row = document.createElement("div");
          row.style.cssText = "display:flex;align-items:center;gap:8px;padding:6px 10px;font-size:12px;cursor:pointer;color:var(--text);";
          row.innerHTML = `<span style="opacity:0.35">＋</span> ${name}`;
          row.addEventListener("mouseenter", () => (row.style.background = "color-mix(in srgb, var(--accent) 8%, transparent)"));
          row.addEventListener("mouseleave", () => (row.style.background = "transparent"));
          row.addEventListener("click", () => {
            selected.push(name);
            renderChips();
            renderOptions();
            markDirty();
          });
          el.appendChild(row);
        });
      }

      function _positionDropdown() {
        const dd = _searchEls.fallbackDropdown();
        const opt = _searchEls.fallbackOptions();
        if (!dd || !opt) return;
        const rect = dd.getBoundingClientRect();
        opt.style.position = "fixed";
        opt.style.top = rect.bottom + 4 + "px";
        opt.style.left = rect.left + "px";
        opt.style.width = rect.width + "px";
      }

      function init(allProviders) {
        providers = allProviders;
        const sel = _searchEls.fallbackSelected();
        const opt = _searchEls.fallbackOptions();
        if (sel) sel.addEventListener("click", () => {
          renderOptions();
          if (opt) {
            const isOpen = opt.style.display !== "none";
            if (isOpen) {
              opt.style.display = "none";
            } else {
              _positionDropdown();
              opt.style.display = "block";
            }
          }
        });
        document.addEventListener("click", (e) => {
          const dd = _searchEls.fallbackDropdown();
          if (dd && !dd.contains(e.target) && opt) opt.style.display = "none";
        });
        window.addEventListener("scroll", () => { if (opt && opt.style.display !== "none") _positionDropdown(); }, true);
        window.addEventListener("resize", () => { if (opt && opt.style.display !== "none") _positionDropdown(); });
        renderOptions();
        renderChips();
      }

      function set(chain) {
        const list = Array.isArray(chain) ? chain : (chain || "").split(",").map((s) => s.trim()).filter(Boolean);
        selected = list.filter((p) => providers.length === 0 || providers.includes(p));
        renderChips();
      }

      return { init, set, selected: () => [...selected], renderOptions };
    })();

    function _escHtml(s) {
      return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
    }

    function _renderSearchResultList(results) {
      if (!results || results.length === 0) return '<div class="muted" style="font-size:11px;padding:6px 0;">No results.</div>';
      return results
        .map((r, i) => `
        <div style="padding:8px 10px;border:1px solid var(--border);border-radius:8px;margin-bottom:6px;background:color-mix(in srgb, var(--panel) 60%, transparent);">
          <div style="font-size:12px;font-weight:600;color:var(--text);margin-bottom:2px;">${i + 1}. ${_escHtml(r.title || "(no title)")}</div>
          <a href="${_escHtml(r.url || "#")}" target="_blank" rel="noopener" style="font-size:11px;color:var(--accent-text);word-break:break-all;">${_escHtml(r.url || "")}</a>
          ${r.snippet ? `<div class="muted" style="font-size:11px;margin-top:4px;">${_escHtml(r.snippet)}</div>` : ""}
        </div>`)
        .join("");
    }

    async function _loadSearchProviders() {
      try {
        const res = await fetch("/api/settings/search/providers");
        if (!res.ok) return [];
        const data = await res.json();
        const list = data.providers || [];
        const tp = _searchEls.testProvider();
        if (tp) {
          tp.innerHTML = '<option value="">Default</option>';
          list.forEach((p) => {
            const o = document.createElement("option");
            o.value = p;
            o.textContent = p;
            tp.appendChild(o);
          });
        }
        [_searchEls.compareA(), _searchEls.compareB()].forEach((sel, i) => {
          if (!sel) return;
          sel.innerHTML = "";
          list.forEach((p) => {
            const o = document.createElement("option");
            o.value = p;
            o.textContent = p;
            sel.appendChild(o);
          });
          sel.value = list[Math.min(i, list.length - 1)] || "";
        });
        _fallbackUI.init(list);
        return list;
      } catch (e) {
        console.error("Failed to load search providers:", e);
        return [];
      }
    }

    async function loadSearchSettings() {
      try {
        const res = await fetch("/api/settings/search");
        if (!res.ok) return;
        const data = await res.json();
        const p = _searchEls.provider();
        if (p && data.search_provider) p.value = data.search_provider;
        const u = _searchEls.searxngUrl();
        if (u) u.value = data.search_url || "";
        _fallbackUI.set(data.search_fallback_chain || []);
        const rc = _searchEls.resultCount();
        if (rc && data.search_result_count != null) rc.value = data.search_result_count;
        const ss = _searchEls.safesearch();
        if (ss && data.search_safesearch) ss.value = data.search_safesearch;
        _updateSearchProviderUI();
        // Auto-select first search key provider and load its keys
        const keySelect = _searchEls.keyProviderSelect();
        if (keySelect && !keySelect.value) {
          keySelect.value = "tavily";
          _currentSearchKeyProvider = "tavily";
          _loadSearchKeys("tavily");
        } else if (_currentSearchKeyProvider) {
          _loadSearchKeys(_currentSearchKeyProvider);
        }
      } catch (e) {
        console.error("Failed to load search settings:", e);
      }
    }

    _searchEls.provider()?.addEventListener("change", () => {
      _updateSearchProviderUI();
      _fallbackUI.renderOptions();
      window._universalSave?.markDirty("providers");
    });

    _loadSearchProviders();

    async function _saveSearchSettings() {
      const payload = {
        search_provider: _searchEls.provider()?.value || "searxng",
        search_url: _searchEls.searxngUrl()?.value?.trim() || "",
        search_fallback_chain: _fallbackUI.selected(),
        search_result_count: parseInt(_searchEls.resultCount()?.value || "10", 10),
        search_safesearch: _searchEls.safesearch()?.value || "strict",
      };
      // Google PSE CX is still saved inline (not part of multi-key)
      const googlePseCx = _searchEls.googlePseCxInput()?.value?.trim();
      if (googlePseCx) payload.google_pse_cx = googlePseCx;
      const res = await fetch("/api/settings/search", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      const data = await res.json();
      if (data.status === "ok") {
        await loadSearchSettings();
      } else {
        throw new Error(data.detail || data.error || "Save failed");
      }
    }

    // Register under "providers" tab since search settings live inside the Providers panel
    _universalSave.register("providers", _saveSearchSettings);

    _searchEls.testBtn()?.addEventListener("click", async () => {
      const statusEl = _searchEls.status();
      const resultsEl = _searchEls.testResults();
      const compareEl = _searchEls.compareResults();
      if (compareEl) compareEl.style.display = "none";
      if (statusEl) statusEl.textContent = "Testing…";
      if (resultsEl) { resultsEl.style.display = "none"; resultsEl.innerHTML = ""; }
      try {
        const res = await fetch("/api/settings/search/test", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            query: _searchEls.testQuery()?.value?.trim() || "",
            count: parseInt(_searchEls.testCount()?.value || "5", 10),
            provider: _searchEls.testProvider()?.value || "",
          }),
        });
        let data;
        try { data = await res.json(); } catch (_) { data = null; }
        if (!res.ok || !data) {
          if (statusEl) statusEl.textContent = `❌ Server error (${res.status}). Try restarting Sable to load new search routes.`;
          return;
        }
        if (data.success) {
          if (statusEl) statusEl.textContent = `✅ ${data.provider_used}: ${data.result_count} results in ${data.elapsed_s}s`;
          if (resultsEl) {
            resultsEl.innerHTML = `<div class="muted" style="font-size:11px;margin-bottom:6px;">Results for <b>${_escHtml(data.query)}</b> via ${_escHtml(data.provider_used)}:</div>` + _renderSearchResultList(data.results);
            resultsEl.style.display = "block";
          }
        } else {
          if (statusEl) statusEl.textContent = `❌ ${data.provider_used || "unknown"}: ${data.error || "No results"}`;
        }
      } catch (e) {
        if (statusEl) statusEl.textContent = `❌ ${e.message}`;
      }
    });

    // --- Compare mode ---
    _searchEls.compareBtn()?.addEventListener("click", () => {
      const c = _searchEls.compareControls();
      if (c) c.style.display = c.style.display === "none" || !c.style.display ? "block" : "none";
    });

    _searchEls.compareCloseBtn()?.addEventListener("click", () => {
      const c = _searchEls.compareControls();
      if (c) c.style.display = "none";
    });

    function _renderCompareColumn(data) {
      const head = data.success
        ? `<span style="color:var(--success)">✅</span> ${data.result_count} results · ${data.elapsed_s}s`
        : `<span style="color:var(--danger)">❌</span> ${_escHtml(data.error || "failed")}`;
      return `
        <div style="flex:1;min-width:240px;">
          <div style="font-size:12px;font-weight:700;color:var(--text);margin-bottom:4px;">${_escHtml(data.provider)}</div>
          <div class="muted" style="font-size:11px;margin-bottom:8px;">${head}</div>
          ${data.success ? _renderSearchResultList(data.results) : ""}
        </div>`;
    }

    _searchEls.compareRunBtn()?.addEventListener("click", async () => {
      const statusEl = _searchEls.status();
      const resultsEl = _searchEls.compareResults();
      const testEl = _searchEls.testResults();
      if (testEl) testEl.style.display = "none";
      if (statusEl) statusEl.textContent = "Comparing…";
      const provA = _searchEls.compareA()?.value || "searxng";
      const provB = _searchEls.compareB()?.value || "duckduckgo";
      if (provA === provB) {
        if (statusEl) statusEl.textContent = "⚠️ Pick two different providers to compare.";
        return;
      }
      try {
        const res = await fetch("/api/settings/search/compare", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            query: _searchEls.testQuery()?.value?.trim() || "",
            count: parseInt(_searchEls.testCount()?.value || "5", 10),
            provider_a: provA,
            provider_b: provB,
          }),
        });
        const data = await res.json();
        if (!res.ok) {
          if (statusEl) statusEl.textContent = `❌ Server error (${res.status}).`;
          return;
        }
        if (statusEl) statusEl.textContent = `⚖️ Comparison done for “${data.query}”`;
        if (resultsEl) {
          resultsEl.innerHTML = `<div style="display:flex;gap:16px;flex-wrap:wrap;">${_renderCompareColumn(data.a)}${_renderCompareColumn(data.b)}</div>`;
          resultsEl.style.display = "block";
        }
      } catch (e) {
        if (statusEl) statusEl.textContent = `❌ ${e.message}`;
      }
    });

    _searchEls.clearCacheBtn()?.addEventListener("click", async () => {
      const statusEl = _searchEls.status();
      try {
        const res = await fetch("/api/settings/search/cache/clear", { method: "POST" });
        const data = await res.json();
        if (statusEl) statusEl.textContent = data.cleared ? "✅ Search cache cleared" : `❌ ${data.error || "Failed"}`;
      } catch (e) {
        if (statusEl) statusEl.textContent = `❌ ${e.message}`;
      }
    });



    // --- Model management (all models: static + custom) ---
    async function loadCustomModels() {
      const listEl = document.getElementById("customModelList");
      const statusEl = document.getElementById("customModelStatus");
      if (!listEl) return;
      try {
        const res = await fetch("/api/models");
        const data = await res.json();
        const allModels = data.models || [];
        listEl.innerHTML = "";
        if (allModels.length === 0) {
          listEl.innerHTML = '<div style="font-size:12px;color:var(--text);padding:8px 0;">No models available.</div>';
          statusEl.textContent = "";
          return;
        }
        allModels.forEach((m) => {
          const row = document.createElement("div");
          row.style.cssText = "display:flex;flex-direction:column;border-radius:8px;background:color-mix(in srgb, var(--panel) 60%, transparent);border:1px solid var(--border);overflow:hidden;";
          // Top bar (clickable)
          const topBar = document.createElement("div");
          topBar.style.cssText = "display:flex;align-items:center;justify-content:space-between;padding:6px 10px;cursor:pointer;";
          topBar.addEventListener("mouseenter", () => topBar.style.background = "color-mix(in srgb, var(--accent) 6%, transparent)");
          topBar.addEventListener("mouseleave", () => topBar.style.background = "transparent");
          const info = document.createElement("span");
          info.style.cssText = "font-size:12px;color:var(--text);";
          const badge = m.custom ? '<span style="color:var(--accent);font-size:9px;background:color-mix(in srgb, var(--accent) 15%, transparent);padding:1px 5px;border-radius:4px;margin-left:6px;">CUSTOM</span>' : '';
          info.innerHTML = `<b>${m.label}</b> <span style="color:var(--text);font-family:monospace;font-size:11px;">${m.id}</span> <span style="color:var(--text);font-size:10px;text-transform:uppercase;">${m.api_backend || 'local'}</span>${badge}`;
          topBar.appendChild(info);
          // Expandable detail panel — editable capabilities
          const detail = document.createElement("div");
          detail.style.cssText = "display:none;padding:8px 12px 12px;font-size:11px;color:var(--text);border-top:1px solid var(--border);";
          const caps = m.capabilities || {};
          const hasThinking = (m.thinking_modes || []).some(tm => tm.thinking_enabled);

          const chkStyle = "accent-color:var(--accent);margin-right:3px;cursor:pointer;";
          const lblStyle = "display:inline-flex;align-items:center;margin-right:10px;cursor:pointer;font-size:11px;";
          detail.innerHTML = `
            <div style="font-weight:500;margin-bottom:6px;">Capabilities</div>
            <div style="display:flex;flex-wrap:wrap;gap:4px 0;margin-bottom:8px;">
              <label style="${lblStyle}"><input type="checkbox" data-cap="image" ${caps.image ? 'checked' : ''} style="${chkStyle}"> 🖼️ Image</label>
              <label style="${lblStyle}"><input type="checkbox" data-cap="video" ${caps.video ? 'checked' : ''} style="${chkStyle}"> 🎬 Video</label>
              <label style="${lblStyle}"><input type="checkbox" data-cap="document" ${caps.document ? 'checked' : ''} style="${chkStyle}"> 📄 Document</label>
              <label style="${lblStyle}"><input type="checkbox" data-cap="audio" ${caps.audio ? 'checked' : ''} style="${chkStyle}"> 🎧 Audio</label>
              <label style="${lblStyle}"><input type="checkbox" data-cap="thinking" ${hasThinking ? 'checked' : ''} style="${chkStyle}"> 🧠 Thinking</label>
            </div>
            <button class="save-caps-btn" style="font-size:10px;padding:3px 10px;border-radius:4px;background:var(--accent);color:#fff;border:none;cursor:pointer;font-weight:600;">Save Changes</button>
            <span class="caps-status" style="margin-left:8px;font-size:10px;opacity:0.7;"></span>
          `;

          // Wire up save button
          const saveBtn = detail.querySelector(".save-caps-btn");
          const statusEl2 = detail.querySelector(".caps-status");
          saveBtn.addEventListener("click", async (e) => {
            e.stopPropagation();
            const payload = {
              capabilities: {
                image: detail.querySelector('[data-cap="image"]').checked,
                video: detail.querySelector('[data-cap="video"]').checked,
                document: detail.querySelector('[data-cap="document"]').checked,
                audio: detail.querySelector('[data-cap="audio"]').checked,
              },
              supports_thinking: detail.querySelector('[data-cap="thinking"]').checked,
            };
            try {
              saveBtn.disabled = true;
              saveBtn.textContent = "Saving…";
              const res = await fetch(`/api/settings/models/${encodeURIComponent(m.id)}`, {
                method: "PATCH",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
              });
              if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                throw new Error(err.detail || "Failed");
              }
              statusEl2.textContent = "✓ Saved";
              statusEl2.style.color = "var(--accent)";
              showToast(`${m.label} capabilities updated`, "success");
              setTimeout(() => { statusEl2.textContent = ""; }, 3000);
            } catch (err) {
              statusEl2.textContent = "✕ " + err.message;
              statusEl2.style.color = "var(--danger)";
              showToast("Failed to update capabilities", "error");
            } finally {
              saveBtn.disabled = false;
              saveBtn.textContent = "Save Changes";
            }
          });
          topBar.addEventListener("click", () => {
            const open = detail.style.display !== "none";
            detail.style.display = open ? "none" : "block";
          });
          row.appendChild(topBar);
          row.appendChild(detail);
          // Delete button (stop propagation so it doesn't toggle detail)
          const delBtn = document.createElement("button");
          delBtn.textContent = "✕";
          delBtn.style.cssText = "background:transparent;border:none;color:var(--danger);cursor:pointer;font-size:13px;padding:2px 6px;border-radius:4px;position:absolute;right:8px;top:50%;transform:translateY(-50%);";
          delBtn.title = "Remove model";
          delBtn.addEventListener("click", async (e) => {
            e.stopPropagation();
            if (!await sableConfirm(`Remove "${m.label}" from your model list?`, { danger: true })) return;
            try {
              await fetch(`/api/settings/models/${encodeURIComponent(m.id)}`, { method: "DELETE" });
              showToast("Model removed", "success");
              loadCustomModels();
              loadModels();
            } catch (e2) { showToast("Failed to remove model", "error"); }
          });
          topBar.style.position = "relative";
          topBar.appendChild(delBtn);
          listEl.appendChild(row);
        });
        const customCount = allModels.filter((m) => m.custom).length;
        statusEl.textContent = `${allModels.length} model${allModels.length !== 1 ? "s" : ""} active${customCount ? ` · ${customCount} custom` : ""}`;
      } catch (e) {
        statusEl.textContent = "Failed to load models";
        setTimeout(() => { if (statusEl.textContent === "Failed to load models") statusEl.textContent = ""; }, 4000);
      }
    }

    const addCustomModelBtn = document.getElementById("addCustomModelBtn");
    if (addCustomModelBtn) {
      addCustomModelBtn.addEventListener("click", async () => {
        const backend = document.getElementById("customModelBackend")?.value;
        const label = document.getElementById("customModelLabel")?.value.trim();
        const capabilities = {
          image: document.getElementById("capImage")?.checked || false,
          video: document.getElementById("capVideo")?.checked || false,
          document: document.getElementById("capDocument")?.checked || false,
          audio: document.getElementById("capAudio")?.checked || false,
        };
        const supportsThinking = document.getElementById("capThinking")?.checked || false;
        const maxChars = parseInt(document.getElementById("customModelMaxChars")?.value) || 500000;
        if (!backend) { showToast("Select a provider first", "error"); return; }
        if (!label) { showToast("Enter a display name", "error"); return; }
        let payload;
        if (backend === "url") {
          const endpoint = document.getElementById("customModelEndpoint")?.value.trim();
          const urlKey = document.getElementById("customModelUrlKey")?.value.trim();
          const mid = document.getElementById("customModelId")?.value;
          if (!endpoint) { showToast("Enter the base URL", "error"); return; }
          if (!mid) { showToast("Fetch models and pick one", "error"); return; }
          const safeId = "url-" + mid.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
          payload = { id: safeId, label, api_backend: "local", api_model_type: mid, local_endpoint: endpoint, local_api_key: urlKey, max_session_chars: maxChars, capabilities, supports_thinking: supportsThinking };
        } else if (backend.startsWith("endpoint:")) {
          const epId = backend.slice("endpoint:".length);
          const mid = document.getElementById("customModelId")?.value;
          if (!mid) { showToast("Select a model from the dropdown", "error"); return; }
          const safeId = epId + "-" + mid.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
          payload = { id: safeId, label, api_backend: "local", api_model_type: mid, endpoint_id: epId, max_session_chars: maxChars, capabilities, supports_thinking: supportsThinking };
        } else {
          const mid = document.getElementById("customModelId")?.value;
          if (!mid) { showToast("Select a model from the dropdown", "error"); return; }
          payload = { id: mid, label, api_backend: backend, max_session_chars: maxChars, capabilities, supports_thinking: supportsThinking };
        }
        try {
          const res = await fetch("/api/settings/models", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
          });
          if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            const detail = typeof err.detail === "string" ? err.detail : (Array.isArray(err.detail) ? err.detail[0]?.msg : JSON.stringify(err.detail));
            showToast(detail || "Failed to add model", "error");
            return;
          }
          // Reset form
          modelSelect.value = "";
          modelLabelInput.value = "";
          if (document.getElementById("customModelEndpoint")) document.getElementById("customModelEndpoint").value = "";
          if (document.getElementById("customModelUrlKey")) document.getElementById("customModelUrlKey").value = "";
          if (document.getElementById("customUrlStatus")) document.getElementById("customUrlStatus").textContent = "";
          document.getElementById("capImage").checked = false;
          document.getElementById("capVideo").checked = false;
          document.getElementById("capDocument").checked = false;
          document.getElementById("capAudio").checked = false;
          document.getElementById("capThinking").checked = false;
          document.getElementById("customModelMaxChars").value = "500000";
          showToast("Model added ✓", "success");
          loadCustomModels();
          loadModels();
        } catch (e) { showToast("Failed to add model", "error"); }
      });
    }

    function appendLogLine(msg) {
      const span = document.createElement("span");
      let cls = "log-info";
      if (/\[WARN(ING)?\]/i.test(msg)) cls = "log-warn";
      else if (/\[ERROR\]/i.test(msg)) cls = "log-error";
      else if (/\[DEBUG\]/i.test(msg)) cls = "log-debug";
      span.className = cls;
      span.textContent = msg + "\n";
      logViewer.appendChild(span);

      // Keep buffer manageable (max ~2000 lines)
      while (logViewer.childElementCount > 2000) {
        logViewer.removeChild(logViewer.firstChild);
      }

      if (logAutoScroll.checked) {
        logViewer.scrollTop = logViewer.scrollHeight;
      }
    }

    function connectLogs() {
      if (logSource) logSource.close();
      logSource = new EventSource("/api/logs?token=" + encodeURIComponent(getToken() || ""));
      logSource.onmessage = (e) => {
        try {
          const data = JSON.parse(e.data);
          if (data.type === "log" && data.message) {
            appendLogLine(data.message);
          }
        } catch {}
      };
      logSource.onerror = () => {
        // Auto-reconnect handled by EventSource
      };
    }

    // Keyboard shortcut: Escape closes settings / library / skill detail
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        const readerOverlay = document.getElementById("libraryReaderOverlay");
        if (readerOverlay) {
          readerOverlay.remove();
        } else if (skillDetailOverlay.classList.contains("show")) {
          skillDetailOverlay.classList.remove("show");
        } else if (!libraryOverlay.classList.contains("hidden")) {
          closeLibrary();
        } else if (!settingsOverlay.classList.contains("hidden")) {
          closeSettings();
        }
      }
    });

    // Browser headless toggle
    const headlessToggle = document.getElementById("headlessToggle");
    const refreshWafBtn = document.getElementById("refreshWafBtn");

    async function loadBrowserSettings() {
      try {
        const res = await fetch("/api/settings/browser");
        if (res.ok) {
          const data = await res.json();
          headlessToggle.checked = data.headless;
        }
      } catch {}
      // Also load context pass settings
      loadContextPassSettings();
      // Load general settings (tool output limit)
      loadGeneralSettings();
    }
    window.loadBrowserSettings = loadBrowserSettings;

    // ── General Settings (tool output cap) ──
    const maxToolOutputInput = document.getElementById("maxToolOutputInput");

    const desktopNotifToggle = document.getElementById("desktopNotifToggle");
    const notifCategoryList = document.getElementById("notifCategoryList");

    // Category labels — must match engine/notifications/policy.py CATEGORY_META
    const NOTIF_CATEGORIES = [
      ["permission", "🔐 A tool needs my approval"],
      ["image",      "🎨 Image generation finishes"],
      ["research",   "🔬 Deep research finishes"],
      ["agent",      "🤖 A subagent finishes"],
      ["turn",       "✅ A long turn finishes (3+ tools)"],
    ];

    function _renderNotifCategories(cats) {
      if (!notifCategoryList) return;
      notifCategoryList.innerHTML =
        '<p class="muted" style="font-size:11px;margin:0 0 2px 0;">Notify me when…</p>' +
        NOTIF_CATEGORIES.map(([id, label]) => `
          <label style="display:flex;align-items:center;gap:8px;font-size:12.5px;cursor:pointer;padding:2px 0;">
            <input type="checkbox" data-notif-cat="${id}" ${cats?.[id] === false ? "" : "checked"}
              style="width:15px;height:15px;cursor:pointer;accent-color:var(--accent);">
            <span>${label}</span>
          </label>`).join("");

      notifCategoryList.querySelectorAll("input[data-notif-cat]").forEach((cb) => {
        cb.addEventListener("change", async () => {
          const payload = {};
          notifCategoryList.querySelectorAll("input[data-notif-cat]").forEach((c) => {
            payload[c.dataset.notifCat] = c.checked;
          });
          try {
            await fetch("/api/settings/general", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ notification_categories: payload }),
            });
          } catch {}
        });
      });
    }

    async function loadGeneralSettings() {
      try {
        const res = await fetch("/api/settings/general");
        if (res.ok) {
          const d = await res.json();
          if (maxToolOutputInput && d.max_tool_output_chars) {
            maxToolOutputInput.value = d.max_tool_output_chars;
          }
          if (desktopNotifToggle) {
            desktopNotifToggle.checked = d.desktop_notifications !== false;
          }
          _renderNotifCategories(d.notification_categories || {});
          _syncNotifCategoryDisabled();
        }
      } catch {}
    }

    function _syncNotifCategoryDisabled() {
      if (!notifCategoryList) return;
      const on = desktopNotifToggle ? desktopNotifToggle.checked : true;
      notifCategoryList.style.opacity = on ? "1" : "0.4";
      notifCategoryList.style.pointerEvents = on ? "" : "none";
    }

    if (maxToolOutputInput) {
      maxToolOutputInput.addEventListener("change", async () => {
        const val = parseInt(maxToolOutputInput.value, 10);
        if (!val || val < 1000) { maxToolOutputInput.value = 100000; return; }
        try {
          await fetch("/api/settings/general", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ max_tool_output_chars: val }),
          });
        } catch {}
      });
    }

    if (desktopNotifToggle) {
      desktopNotifToggle.addEventListener("change", async () => {
        _syncNotifCategoryDisabled();
        try {
          await fetch("/api/settings/general", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ desktop_notifications: desktopNotifToggle.checked }),
          });
        } catch {}
      });
    }

    // ── Context Pass Settings (model fallback chain; browser pool is auto-selected) ──
    const ctxPassModel = document.getElementById("ctxPassModel");
    const ctxPassFb1 = document.getElementById("ctxPassFb1");
    const ctxPassFb2 = document.getElementById("ctxPassFb2");
    const _ctxPassModelSelects = [ctxPassModel, ctxPassFb1, ctxPassFb2];

    function populateCtxPassModels() {
      for (const sel of _ctxPassModelSelects) {
        if (!sel) continue;
        const current = sel.value;
        const isPrimary = sel === ctxPassModel;
        sel.innerHTML = `<option value="">${isPrimary ? "Default (current model)" : "— none —"}</option>`;
        for (const m of modelList) {
          const opt = document.createElement("option");
          opt.value = m.id;
          opt.textContent = m.name || m.id;
          sel.appendChild(opt);
        }
        sel.value = current;
      }
    }

    async function loadContextPassSettings() {
      populateCtxPassModels();
      try {
        const res = await fetch("/api/settings/context-pass");
        if (res.ok) {
          const d = await res.json();
          if (ctxPassModel) ctxPassModel.value = d.summarizer_model || "";
          const fbm = d.fallback_models || [];
          if (ctxPassFb1) ctxPassFb1.value = fbm[0] || "";
          if (ctxPassFb2) ctxPassFb2.value = fbm[1] || "";
        }
      } catch {}
    }

    function _buildCtxPassPayload() {
      const fb_models = [
        ctxPassFb1?.value,
        ctxPassFb2?.value,
      ].filter(v => v && v.trim());
      return {
        summarizer_model: ctxPassModel ? ctxPassModel.value : "",
        fallback_models: fb_models,
      };
    }

    async function saveContextPassSettings() {
      try {
        await fetch("/api/settings/context-pass", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(_buildCtxPassPayload()),
        });
      } catch {}
    }

    for (const sel of _ctxPassModelSelects) {
      if (sel) sel.addEventListener("change", saveContextPassSettings);
    }

    // Register General tab with universal save
    _universalSave.register("general", async () => {
      // Save tool output cap
      const val = parseInt(maxToolOutputInput?.value, 10);
      if (val && val >= 1000) {
        await fetch("/api/settings/general", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ max_tool_output_chars: val }),
        });
      }
      // Save context pass settings
      await fetch("/api/settings/context-pass", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(_buildCtxPassPayload()),
      });
    });
    // ── /Context Pass Settings ──

    refreshWafBtn.addEventListener("click", async () => {
      refreshWafBtn.disabled = true;
      refreshWafBtn.textContent = "🛡️ Refreshing…";
      try {
        const res = await fetch("/api/settings/browser/refresh-waf", { method: "POST" });
        if (res.ok) {
          showToast("WAF token refreshed!", "success");
        } else {
          const err = await res.json();
          showToast(err.detail || "Refresh failed", "error");
        }
      } catch (e) {
        showToast("Refresh error: " + e.message, "error");
      } finally {
        refreshWafBtn.disabled = false;
        refreshWafBtn.textContent = "🛡️ Refresh WAF";
      }
    });

    const refreshDeepseekTokenBtn = document.getElementById("refreshDeepseekTokenBtn");
    const deepseekTokenStatus = document.getElementById("deepseekTokenStatus");
    if (refreshDeepseekTokenBtn) {
      const setDsStatus = (msg, color) => {
        if (!deepseekTokenStatus) return;
        deepseekTokenStatus.textContent = msg;
        deepseekTokenStatus.style.color = color || "var(--text-dim)";
      };
      refreshDeepseekTokenBtn.addEventListener("click", async () => {
        refreshDeepseekTokenBtn.disabled = true;
        refreshDeepseekTokenBtn.textContent = "↻ Refreshing...";
        setDsStatus("Refreshing DeepSeek token from browser profile…", "var(--text-dim)");
        try {
          const res = await fetch("/api/settings/deepseek/refresh-token", { method: "POST" });
          const data = await res.json().catch(() => ({}));
          if (res.ok) {
            const preview = data.token_preview || "none";
            setDsStatus("✅ Token refreshed: " + preview, "var(--success, #3daa5c)");
            showToast("DeepSeek token: " + preview, "success");
            await loadModels();
            // Refresh promotes the new token to the pinned selection; re-render
            // the key list so it shows as active without a manual reload.
            if (window.loadAccountProfiles) await window.loadAccountProfiles();
          } else {
            const msg = data.detail || data.error || "DeepSeek token refresh failed";
            setDsStatus("✕ " + msg, "var(--danger, #cf3b52)");
            showToast(msg, "error");
          }
        } catch (e) {
          const msg = "DeepSeek refresh error: " + e.message;
          setDsStatus("✕ " + msg, "var(--danger, #cf3b52)");
          showToast(msg, "error");
        } finally {
          refreshDeepseekTokenBtn.disabled = false;
          refreshDeepseekTokenBtn.textContent = "↻ Refresh Token";
        }
      });
    }

