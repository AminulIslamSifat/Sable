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
    function getDisabledTools() {
      try { return JSON.parse(localStorage.getItem("sable_disabled_tools") || "[]"); } catch { return []; }
    }
    function setDisabledTools(arr) {
      localStorage.setItem("sable_disabled_tools", JSON.stringify(arr));
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

    document.querySelector('[data-tab="tools"]').addEventListener("click", loadTools);
    document.querySelector('[data-tab="skills"]').addEventListener("click", loadSkills);
    document.querySelector('[data-tab="account"]')?.addEventListener("click", () => { if (typeof loadAccountProfiles === 'function') loadAccountProfiles(); });

    // --- Providers tab: Unified API key manager ---
    const _keyProviderMeta = {
      gemini:  { apiBase: "/api/settings/gemini",  name: "Gemini",  placeholder: "Paste API key (AIza…)" },
      groq:    { apiBase: "/api/settings/groq",    name: "Groq",    placeholder: "Paste API key (gsk_…)" },
      mistral: { apiBase: "/api/settings/mistral", name: "Mistral", placeholder: "Paste API key (key: …)" },
      openai:  { apiBase: "/api/settings/openai",  name: "OpenAI",  placeholder: "Paste API key (sk-…)" },
    };
    const _keyEls = {
      select: document.getElementById("keyProviderSelect"),
      input:  document.getElementById("apiKeyInput"),
      btn:    document.getElementById("addApiKeyBtn"),
      list:   document.getElementById("apiKeyList"),
      status: document.getElementById("apiKeyStatus"),
    };
    let _currentKeyProvider = "gemini";

    async function _loadKeysFor(provider) {
      const meta = _keyProviderMeta[provider];
      if (!meta || !_keyEls.list) return;
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
    });

    // --- Search Engine tab ---
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
      // API key inputs
      keyBrave: () => document.getElementById("searchInputBrave"),
      keyGooglePse: () => document.getElementById("searchInputGooglePse"),
      keyGooglePseCx: () => document.getElementById("searchInputGooglePseCx"),
      keyTavily: () => document.getElementById("searchInputTavily"),
      keySerper: () => document.getElementById("searchInputSerper"),
      // Key row containers
      rowBrave: () => document.getElementById("searchKeyBrave"),
      rowGooglePse: () => document.getElementById("searchKeyGooglePse"),
      rowTavily: () => document.getElementById("searchKeyTavily"),
      rowSerper: () => document.getElementById("searchKeySerper"),
      noKeyNeeded: () => document.getElementById("searchNoKeyNeeded"),
      // Key status badges
      statusBrave: () => document.getElementById("searchKeyBraveStatus"),
      statusGooglePse: () => document.getElementById("searchKeyGooglePseStatus"),
      statusTavily: () => document.getElementById("searchKeyTavilyStatus"),
      statusSerper: () => document.getElementById("searchKeySerperStatus"),
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

      // Show/hide API key rows based on provider
      const keyMap = {
        brave: _searchEls.rowBrave(),
        google_pse: _searchEls.rowGooglePse(),
        tavily: _searchEls.rowTavily(),
        serper: _searchEls.rowSerper(),
      };
      const needsKey = ["brave", "google_pse", "tavily", "serper"];
      for (const [prov, el] of Object.entries(keyMap)) {
        if (el) el.style.display = (provider === prov) ? "" : "none";
      }
      const noKeyEl = _searchEls.noKeyNeeded();
      if (noKeyEl) noKeyEl.style.display = (!needsKey.includes(provider) && provider !== "disabled") ? "" : "none";

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
    }

    // --- Fallback chain multi-select dropdown ---
    const _fallbackUI = (() => {
      let providers = [];
      let selected = []; // ordered

      function syncHidden() {
        const h = _searchEls.fallback();
        if (h) h.value = selected.join(", ");
      }

      function renderChips() {
        const el = _searchEls.fallbackSelected();
        if (!el) return;
        el.innerHTML = "";
        if (selected.length === 0) {
          el.innerHTML = '<span class="muted" style="font-size:11px;">Select providers…</span>';
          syncHidden();
          return;
        }
        selected.forEach((name) => {
          const chip = document.createElement("span");
          chip.draggable = true;
          chip.style.cssText = "display:inline-flex;align-items:center;gap:4px;font-size:11px;padding:2px 8px;border-radius:10px;background:color-mix(in srgb, var(--accent) 18%, transparent);border:1px solid var(--border);color:var(--text);cursor:grab;";
          chip.textContent = name;
          const x = document.createElement("span");
          x.textContent = "×";
          x.style.cssText = "cursor:pointer;opacity:.7;font-weight:700;";
          x.addEventListener("click", (ev) => {
            ev.stopPropagation();
            selected = selected.filter((p) => p !== name);
            renderChips();
            renderOptions();
          });
          chip.appendChild(x);
          chip.addEventListener("dragstart", (ev) => ev.dataTransfer.setData("text/plain", name));
          chip.addEventListener("dragover", (ev) => ev.preventDefault());
          chip.addEventListener("drop", (ev) => {
            ev.preventDefault();
            ev.stopPropagation();
            const src = ev.dataTransfer.getData("text/plain");
            if (!src || src === name) return;
            selected = selected.filter((p) => p !== src);
            selected.splice(selected.indexOf(name), 0, src);
            renderChips();
          });
          el.appendChild(chip);
        });
        syncHidden();
      }

      function renderOptions() {
        const el = _searchEls.fallbackOptions();
        if (!el) return;
        el.innerHTML = "";
        const currentPrimary = _searchEls.provider()?.value || "searxng";
        providers.filter((p) => p !== "disabled" && p !== currentPrimary).forEach((name) => {
          const isSel = selected.includes(name);
          const row = document.createElement("div");
          row.style.cssText = "display:flex;align-items:center;gap:8px;padding:6px 10px;font-size:12px;cursor:pointer;color:var(--text);";
          row.innerHTML = `<span style="opacity:${isSel ? 1 : 0.35}">${isSel ? "☑" : "☐"}</span> ${name}`;
          row.addEventListener("mouseenter", () => (row.style.background = "color-mix(in srgb, var(--accent) 8%, transparent)"));
          row.addEventListener("mouseleave", () => (row.style.background = "transparent"));
          row.addEventListener("click", () => {
            selected = selected.includes(name) ? selected.filter((p) => p !== name) : [...selected, name];
            renderChips();
            renderOptions();
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
        // Set API key status badges
        const _keyBadge = (el, hasKey) => {
          if (!el) return;
          el.textContent = hasKey ? "✅ saved" : "⚠️ not set";
          el.style.color = hasKey ? "var(--success)" : "var(--warning)";
        };
        _keyBadge(_searchEls.statusBrave(), data.has_brave_key);
        _keyBadge(_searchEls.statusGooglePse(), data.has_google_pse_key);
        _keyBadge(_searchEls.statusTavily(), data.has_tavily_key);
        _keyBadge(_searchEls.statusSerper(), data.has_serper_key);
        _updateSearchProviderUI();
      } catch (e) {
        console.error("Failed to load search settings:", e);
      }
    }

    _searchEls.provider()?.addEventListener("change", () => {
      _updateSearchProviderUI();
      _fallbackUI.renderOptions();
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
      const braveKey = _searchEls.keyBrave()?.value?.trim();
      if (braveKey) payload.brave_api_key = braveKey;
      const googlePseKey = _searchEls.keyGooglePse()?.value?.trim();
      if (googlePseKey) payload.google_pse_key = googlePseKey;
      const googlePseCx = _searchEls.keyGooglePseCx()?.value?.trim();
      if (googlePseCx) payload.google_pse_cx = googlePseCx;
      const tavilyKey = _searchEls.keyTavily()?.value?.trim();
      if (tavilyKey) payload.tavily_api_key = tavilyKey;
      const serperKey = _searchEls.keySerper()?.value?.trim();
      if (serperKey) payload.serper_api_key = serperKey;
      const res = await fetch("/api/settings/search", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      const data = await res.json();
      if (data.status === "ok") {
        ["keyBrave", "keyGooglePse", "keyGooglePseCx", "keyTavily", "keySerper"].forEach((k) => {
          const el = _searchEls[k]();
          if (el) el.value = "";
        });
        await loadSearchSettings();
      } else {
        throw new Error(data.detail || data.error || "Save failed");
      }
    }

    // Register Search tab with universal save
    _universalSave.register("search", _saveSearchSettings);

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

    document.querySelector('[data-tab="search"]')?.addEventListener("click", loadSearchSettings);

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
          // Expandable detail panel
          const detail = document.createElement("div");
          detail.style.cssText = "display:none;padding:6px 12px 10px;font-size:11px;color:var(--text);border-top:1px solid var(--border);";
          const caps = m.capabilities || {};
          const capIcons = [];
          if (caps.image) capIcons.push("🖼️ Image");
          if (caps.video) capIcons.push("🎬 Video");
          if (caps.document) capIcons.push("📄 Document");
          if (caps.audio) capIcons.push("🎧 Audio");
          const hasThinking = (m.thinking_modes || []).some(tm => tm.thinking_enabled);
          const thinkingBadge = hasThinking ? ' · 🧠 Thinking' : '';
          detail.innerHTML = `<span style="color:var(--text);font-weight:500;">Capabilities:</span> ${capIcons.length ? capIcons.join(" · ") : '<span style="opacity:0.6;">None</span>'}${thinkingBadge}`;
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

    // ── General Settings (tool output cap) ──
    const maxToolOutputInput = document.getElementById("maxToolOutputInput");

    async function loadGeneralSettings() {
      try {
        const res = await fetch("/api/settings/general");
        if (res.ok) {
          const d = await res.json();
          if (maxToolOutputInput && d.max_tool_output_chars) {
            maxToolOutputInput.value = d.max_tool_output_chars;
          }
        }
      } catch {}
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

    // ── Context Pass Settings ──
    const ctxPassModel = document.getElementById("ctxPassModel");
    const ctxPassBrowserAcc = document.getElementById("ctxPassBrowserAcc");

    function populateCtxPassModels() {
      if (!ctxPassModel) return;
      const current = ctxPassModel.value;
      ctxPassModel.innerHTML = '<option value="">Default (current model)</option>';
      for (const m of modelList) {
        const opt = document.createElement("option");
        opt.value = m.id;
        opt.textContent = m.name || m.id;
        ctxPassModel.appendChild(opt);
      }
      ctxPassModel.value = current;
    }

    async function populateCtxPassProfiles() {
      if (!ctxPassBrowserAcc) return;
      const current = ctxPassBrowserAcc.value;
      ctxPassBrowserAcc.innerHTML = '<option value="">Default (current)</option>';
      try {
        const res = await fetch("/api/settings/accounts");
        if (res.ok) {
          const data = await res.json();
          for (const acc of (data.accounts || [])) {
            const opt = document.createElement("option");
            opt.value = acc.name;
            opt.textContent = acc.email ? `${acc.name} (${acc.email})` : acc.name;
            ctxPassBrowserAcc.appendChild(opt);
          }
        }
      } catch {}
      ctxPassBrowserAcc.value = current;
    }

    async function loadContextPassSettings() {
      populateCtxPassModels();
      await populateCtxPassProfiles();
      try {
        const res = await fetch("/api/settings/context-pass");
        if (res.ok) {
          const d = await res.json();
          if (ctxPassModel) ctxPassModel.value = d.summarizer_model || "";
          if (ctxPassBrowserAcc) ctxPassBrowserAcc.value = d.browser_data_acc || "";
        }
      } catch {}
    }

    async function saveContextPassSettings() {
      try {
        await fetch("/api/settings/context-pass", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            summarizer_model: ctxPassModel ? ctxPassModel.value : "",
            browser_data_acc: ctxPassBrowserAcc ? ctxPassBrowserAcc.value : "",
          }),
        });
      } catch {}
    }

    if (ctxPassModel) ctxPassModel.addEventListener("change", saveContextPassSettings);
    if (ctxPassBrowserAcc) ctxPassBrowserAcc.addEventListener("change", saveContextPassSettings);

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
        body: JSON.stringify({
          summarizer_model: ctxPassModel ? ctxPassModel.value : "",
          browser_data_acc: ctxPassBrowserAcc ? ctxPassBrowserAcc.value : "",
        }),
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

