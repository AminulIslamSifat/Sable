    /* ---------- Status Bar (thinking mode, cwd, context) ---------- */
    const statusDropdownEl = document.getElementById("statusThinkingDropdown");
    const statusLabelEl = document.getElementById("statusThinkingLabel");
    const statusMenuEl = document.getElementById("statusThinkingMenu");
    const statusCwdEl = document.getElementById("statusCwd");
    const statusContextEl = document.getElementById("statusContext");
    const modelDropdown = document.getElementById("modelDropdown");
    const modelTrigger = document.getElementById("modelTrigger");
    const modelMenu = document.getElementById("modelMenu");
    const modelLabel = document.getElementById("modelLabel");
    const personaDropdown = document.getElementById("personaDropdown");
    const personaTrigger = document.getElementById("personaTrigger");
    const personaMenu = document.getElementById("personaMenu");
    const statusPersonaEl = document.getElementById("statusPersona");
    const inputComposite = document.querySelector(".input-composite");

    function _positionMenu(menuEl, alignRight) {
      if (!inputComposite) return;
      const rect = inputComposite.getBoundingClientRect();
      menuEl.style.bottom = (window.innerHeight - rect.top + 2) + "px";
      menuEl.style.minWidth = Math.max(100, rect.width * 0.3) + "px";
      if (alignRight) {
        menuEl.style.left = "auto";
        menuEl.style.right = (window.innerWidth - rect.right) + "px";
      } else {
        menuEl.style.left = rect.left + "px";
        menuEl.style.right = "auto";
      }
    }

    function _closeAllDropdowns(except) {
      if (except !== "thinking") statusDropdownEl?.classList.remove("open");
      if (except !== "model") modelDropdown?.classList.remove("open");
      if (except !== "persona") personaDropdown?.classList.remove("open");
    }

    function syncStatusBarThinking() {
      if (!statusDropdownEl || !statusLabelEl || !statusMenuEl) return;
      const entry = currentModelEntry();
      const modes = (entry && entry.thinking_modes && entry.thinking_modes.length > 0)
        ? entry.thinking_modes
        : [{ id: "thinking", label: "Thinking" }];
      statusMenuEl.innerHTML = "";
      const activeMode = modes.find(m => m.id === selectedThinkingMode) || modes[0];
      statusLabelEl.textContent = activeMode.label || activeMode.id;
      for (const m of modes) {
        const item = document.createElement("div");
        item.className = "status-dropdown-item" + (m.id === selectedThinkingMode ? " active" : "");
        item.textContent = m.label || m.id;
        item.dataset.modeId = m.id;
        item.addEventListener("click", (e) => {
          e.stopPropagation();
          selectedThinkingMode = m.id;
          try { localStorage.setItem(THINKING_MODE_KEY, selectedThinkingMode); } catch (err) {}
          statusDropdownEl.classList.remove("open");
          populateThinkingModes(selectedThinkingMode);
        });
        statusMenuEl.appendChild(item);
      }
      statusDropdownEl.style.display = modes.length <= 1 ? "none" : "";
    }

    const statusTriggerEl = document.getElementById("statusThinkingTrigger");
    if (statusTriggerEl && statusDropdownEl && statusMenuEl) {
      statusTriggerEl.addEventListener("click", (e) => {
        e.stopPropagation();
        _closeAllDropdowns("thinking");
        const isOpen = statusDropdownEl.classList.toggle("open");
        if (isOpen) _positionMenu(statusMenuEl);
      });
    }

    function updateStatusBarCwd() {
      if (!statusCwdEl) return;
      const cwd = window.getIdeCwd ? window.getIdeCwd() : "";
      statusCwdEl.textContent = cwd ? "cwd: " + cwd : "";
      statusCwdEl.title = cwd || "No working directory";
    }

    if (statusCwdEl) {
      statusCwdEl.style.cursor = "pointer";
      statusCwdEl.addEventListener("click", async () => {
        try {
          statusCwdEl.style.opacity = "0.5";
          const res = await fetch("/api/filesystem/pick-folder");
          const data = await res.json();
          if (data.path && window.pickFsRoot) {
            window.pickFsRoot(data.path);
          } else if (data.error) {
            console.error("[StatusBar] pick-folder error:", data.error);
            if (window.showToast) window.showToast(data.error, "error");
          }
          // cancelled is fine — user just closed the dialog
        } catch (err) {
          console.error("[StatusBar] pick-folder failed:", err);
          if (window.showToast) window.showToast("Folder picker request failed", "error");
        } finally {
          statusCwdEl.style.opacity = "";
        }
      });
    }

    function updateStatusBarContext() {
      if (!statusContextEl) return;
      const entry = currentModelEntry();
      const maxChars = entry?.max_session_chars || 100_000;
      const totalChars = window._statusContextChars || 0;
      const pct = Math.min(100, Math.round((totalChars / maxChars) * 100));
      const radius = 7;
      const circ = 2 * Math.PI * radius;
      const offset = circ - (pct / 100) * circ;
      const color = pct > 85 ? "#ef4444" : pct > 60 ? "#f59e0b" : "var(--accent)";
      statusContextEl.innerHTML = `<svg width="18" height="18" viewBox="0 0 18 18" style="vertical-align:middle">` +
        `<circle cx="9" cy="9" r="${radius}" fill="none" stroke="rgba(255,255,255,0.25)" stroke-width="2"/>` +
        `<circle cx="9" cy="9" r="${radius}" fill="none" stroke="${color}" stroke-width="2" ` +
        `stroke-dasharray="${circ}" stroke-dashoffset="${offset}" stroke-linecap="round" ` +
        `transform="rotate(-90 9 9)" style="transition:stroke-dashoffset 0.3s ease"/>` +
        `</svg>`;
      window._statusContextMax = maxChars;
      statusContextEl.title = `${pct}% context used (${(totalChars/1000).toFixed(0)}k / ${(maxChars/1000).toFixed(0)}k chars)`;
    }

    // Refresh status bar on key events + slow fallback interval
    window.addEventListener('cwd-changed', () => updateStatusBarCwd());
    setInterval(() => { updateStatusBarCwd(); updateStatusBarContext(); }, 5000);
    updateStatusBarCwd();
    updateStatusBarContext();

    /* ---------- Context breakdown popup ---------- */
    function _attachCtxPopup(el) {
      el.style.cursor = 'pointer';
      el.addEventListener('click', async (e) => {
        e.stopPropagation();
        document.getElementById('ctxBreakdownPopup')?.remove();
        if (!activeChatId) return;
        const popup = document.createElement('div');
        popup.id = 'ctxBreakdownPopup';
        popup.className = 'context-breakdown-panel';
        popup.style.cssText = 'position:fixed;z-index:100;padding:4px 0;min-width:180px;background:rgba(23,23,26,0.92);border:1px solid var(--border);border-radius:var(--radius);box-shadow:0 -4px 12px rgba(0,0,0,0.4),0 -12px 32px rgba(0,0,0,0.3),inset 0 1px 0 rgba(255,255,255,0.05);opacity:0;transform:translateY(6px) scale(0.97);pointer-events:none;transition:opacity 0.18s ease,transform 0.18s ease;';
        popup.innerHTML = '<div style="color:var(--muted);font-size:10px;padding:6px 10px;">Loading…</div>';
        const trigger = e.currentTarget;
        const bar = trigger.closest('.chat-compact-input') || trigger.closest('.input-composite') || trigger.parentElement;
        const rect = bar.getBoundingClientRect();
        popup.style.right = (window.innerWidth - rect.right) + 'px';
        popup.style.left = 'auto';
        popup.style.bottom = (window.innerHeight - rect.top + 2) + 'px';
        document.body.appendChild(popup);
        try {
          const r = await fetch('/api/chats/' + activeChatId + '/context-breakdown');
          const d = await r.json();
          const total = d.total || 0;
          const entry = currentModelEntry();
          const maxChars = entry?.max_session_chars || window._statusContextMax || 100000;
          const fmt = v => (v / 1000).toFixed(1).replace(/\.0$/, '') + 'k';
          const rows = [
            ['User', d.user || 0, 'var(--accent)'],
            ['Assistant', d.assistant || 0, '#8b5cf6'],
            ['Thinking', d.thinking || 0, '#f59e0b'],
            ['Tool', d.tool || 0, '#10b981'],
          ];
          const totalPct = maxChars > 0 ? Math.min(100, (total / maxChars) * 100).toFixed(1) : '0.0';
          let html = '<div style="padding:6px 10px 4px;font-weight:600;color:var(--text);font-size:10px;border-bottom:1px solid rgba(255,255,255,0.06);margin-bottom:2px;">Context — ' + fmt(total) + '/' + fmt(maxChars) + ' (' + totalPct + '%)</div>';
          for (const [label, val, color] of rows) {
            const pct = total > 0 ? Math.round(val / total * 100) : 0;
            html += '<div style="display:flex;align-items:center;gap:6px;padding:3px 10px;">'
              + '<span style="width:8px;height:8px;border-radius:50%;background:' + color + ';flex-shrink:0;"></span>'
              + '<span style="flex:1;font-size:10px;">' + label + '</span>'
              + '<span style="color:var(--text-dim);font-size:10px;font-variant-numeric:tabular-nums;">' + fmt(val) + ' (' + pct + '%)</span></div>'
              + '<div style="height:3px;background:var(--panel-2);border-radius:2px;margin:0 10px 2px 24px;"><div style="height:100%;width:' + pct + '%;background:' + color + ';border-radius:2px;"></div></div>';
          }
          popup.innerHTML = html;
          requestAnimationFrame(() => { popup.style.opacity = '1'; popup.style.transform = 'translateY(0) scale(1)'; popup.style.pointerEvents = 'auto'; });
        } catch { popup.innerHTML = '<div style="color:var(--danger);padding:6px 10px;">Failed to load</div>'; requestAnimationFrame(() => { popup.style.opacity = '1'; popup.style.transform = 'translateY(0) scale(1)'; popup.style.pointerEvents = 'auto'; }); }
      });
    }
    const _compactCtx = document.getElementById('compactContext');
    if (_compactCtx) {
      _attachCtxPopup(_compactCtx);
    }
    const _mainCtx = document.getElementById('statusContext');
    if (_mainCtx) {
      _attachCtxPopup(_mainCtx);
    }
    document.addEventListener('click', () => document.getElementById('ctxBreakdownPopup')?.remove());


    /* ---------- Model dropdown (status-dropdown style) ---------- */
    function syncModelDropdown() {
      modelMenu.innerHTML = "";
      for (const opt of modelSelectEl.options) {
        const item = document.createElement("div");
        item.className = "status-dropdown-item" + (opt.selected ? " active" : "");
        item.textContent = opt.textContent;
        item.dataset.value = opt.value;
        item.addEventListener("click", (e) => {
          e.stopPropagation();
          modelSelectEl.value = opt.value;
          modelSelectEl.dispatchEvent(new Event("change"));
          modelLabel.textContent = opt.textContent;
          modelDropdown.classList.remove("open");
          syncModelDropdown();
        });
        modelMenu.appendChild(item);
      }
      const sel = modelSelectEl.options[modelSelectEl.selectedIndex];
      if (sel) modelLabel.textContent = sel.textContent;
    }

    if (modelTrigger) {
      modelTrigger.addEventListener("click", (e) => {
        e.stopPropagation();
        _closeAllDropdowns("model");
        const isOpen = modelDropdown.classList.toggle("open");
        if (isOpen) _positionMenu(modelMenu);
      });
    }

    /* ---------- Persona dropdown (status-dropdown style) ---------- */
    async function syncStatusPersona() {
      if (!statusPersonaEl || !personaMenu) return;
      try {
        const data = await fetch("/api/personas").then(r => r.json());
        const personas = data.personas || [];
        const hasActive = personas.some(p => p.active);
        const active = personas.find(p => p.active);
        statusPersonaEl.textContent = active ? active.name : "Default";

        personaMenu.innerHTML = "";
        const defaultItem = document.createElement("div");
        defaultItem.className = "status-dropdown-item" + (!hasActive ? " active" : "");
        defaultItem.textContent = "Default";
        defaultItem.dataset.name = "__default__";
        defaultItem.addEventListener("click", (e) => { e.stopPropagation(); selectPersona(null); });
        personaMenu.appendChild(defaultItem);

        for (const p of personas) {
          const item = document.createElement("div");
          item.className = "status-dropdown-item" + (p.active ? " active" : "");
          item.textContent = p.name;
          item.dataset.name = p.name;
          item.addEventListener("click", (e) => { e.stopPropagation(); selectPersona(p.name); });
          personaMenu.appendChild(item);
        }

        // Output format toggle
        const divider = document.createElement("div");
        divider.className = "status-dropdown-divider";
        personaMenu.appendChild(divider);

        const fmtRow = document.createElement("div");
        fmtRow.className = "status-dropdown-toggle";
        const fmtLabel = document.createElement("span");
        fmtLabel.textContent = "Output Format";
        const fmtSwitch = document.createElement("span");
        const isFmtOn = data.config?.output_format_enabled !== false;
        fmtSwitch.className = "status-toggle-switch" + (isFmtOn ? " on" : "");
        fmtSwitch.textContent = isFmtOn ? "ON" : "OFF";
        fmtRow.appendChild(fmtLabel);
        fmtRow.appendChild(fmtSwitch);
        fmtRow.addEventListener("click", async (e) => {
          e.stopPropagation();
          const newState = !fmtSwitch.classList.contains("on");
          fmtSwitch.classList.add("loading");
          fmtSwitch.innerHTML = '<span class="status-spinner"></span>';
          try {
            await fetch("/api/personas/output-format-toggle", {
              method: "PUT",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ enabled: newState })
            });
            fmtSwitch.classList.toggle("on", newState);
            fmtSwitch.textContent = newState ? "ON" : "OFF";
          } catch (err) {
            fmtSwitch.textContent = isFmtOn ? "ON" : "OFF";
            console.warn("Failed to toggle output format:", err);
          } finally {
            fmtSwitch.classList.remove("loading");
          }
        });
        personaMenu.appendChild(fmtRow);
      } catch {
        statusPersonaEl.textContent = "Default";
      }
    }

    async function selectPersona(name) {
      const items = personaMenu.querySelectorAll(".status-dropdown-item");
      let targetItem = null;
      items.forEach(item => {
        if ((name === null && item.dataset.name === "__default__") || item.dataset.name === name) {
          targetItem = item;
        }
      });
      if (targetItem) {
        const originalText = targetItem.textContent;
        targetItem.classList.add("selecting");
        targetItem.innerHTML = originalText + ' <span class="status-spinner"></span>';
      }
      try {
        await fetch("/api/personas/active", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name })
        });
        personaDropdown.classList.remove("open");
        document.dispatchEvent(new CustomEvent("persona-changed"));
      } catch (e) {
        if (targetItem) {
          targetItem.classList.remove("selecting");
          targetItem.textContent = targetItem.dataset.name === "__default__" ? "Default" : targetItem.dataset.name;
        }
        console.warn("Failed to set persona:", e);
      }
    }

    if (personaTrigger) {
      personaTrigger.addEventListener("click", (e) => {
        e.stopPropagation();
        _closeAllDropdowns("persona");
        const isOpen = personaDropdown.classList.toggle("open");
        if (isOpen) _positionMenu(personaMenu, true);
      });
    }

    // Global click closes all status dropdowns
    document.addEventListener("click", () => {
      _closeAllDropdowns(null);
    });

    syncStatusPersona();
    document.addEventListener("persona-changed", syncStatusPersona);


    function _isScraperMode() {
      const btn = document.getElementById('modeScraper');
      return btn && btn.classList.contains('active');
    }

    async function loadModels() {
      const scraper = _isScraperMode();
      let models = FALLBACK_MODELS;

      if (scraper) {
        // Load engine-specific models from scraper backend
        try {
          const data = await fetch("/api/scraper/models").then(r => r.json());
          if (Array.isArray(data.models) && data.models.length > 0) {
            models = data.models;
            // Attach thinking_modes to first model entry so populateThinkingModes works
            if (data.thinking_modes) {
              models[0].thinking_modes = data.thinking_modes;
            }
          }
        } catch (err) {
          console.warn("Could not load /api/scraper/models:", err);
        }
      } else {
        try {
          const data = await fetch("/api/models").then(r => r.json());
          if (Array.isArray(data.models) && data.models.length > 0) {
            models = data.models;
          }
        } catch (err) {
          console.warn("Could not load /api/models, using fallback list:", err);
        }
      }

      modelList = models;

      let savedModel = null;
      let savedMode = null;
      try {
        savedModel = localStorage.getItem(MODEL_KEY);
        savedMode = localStorage.getItem(THINKING_MODE_KEY);
      } catch (err) {}

      selectedModel = (savedModel && models.some(m => m.id === savedModel)) ? savedModel : models[0].id;

      modelSelectEl.innerHTML = "";
      for (const m of models) {
        const opt = document.createElement("option");
        opt.value = m.id;
        opt.textContent = m.label || m.id;
        if (m.id === selectedModel) opt.selected = true;
        modelSelectEl.appendChild(opt);
      }
      syncModelDropdown();

      populateThinkingModes(savedMode);
      updateAttachUI();
    }

    modelSelectEl.addEventListener("change", async () => {
      const scraper = _isScraperMode();

      if (!scraper) {
        // Block cross-provider switches only for qwen/scraping chats.
        // Pure API models share the same chat freely — provider swaps on every switch.
        const activeMeta = chatList.find(c => c.id === activeChatId);
        if (activeMeta?.provider) {
          const chatProvider = activeMeta.provider;
          const isApiChat = chatProvider !== "qwen" && chatProvider !== "scraping";
          if (!isApiChat) {
            const newEntry = modelList.find(m => m.id === modelSelectEl.value);
            const newProvider = newEntry?.api_backend || "qwen";
            const effectiveChatProvider = chatProvider === "scraping" ? "deepseek" : chatProvider;
            if (newProvider !== effectiveChatProvider) {
              modelSelectEl.value = selectedModel; // revert
              showToast("This chat is locked to " + activeMeta.provider + " — start a new chat to switch providers.", "error");
              return;
            }
          }
        }
      }

      selectedModel = modelSelectEl.value;
      try { localStorage.setItem(MODEL_KEY, selectedModel); } catch (err) {}
      // Switching models resets the thinking mode to that model's default,
      // since the previous mode may not exist on the newly selected model.
      populateThinkingModes(null);
      updateAttachUI();

      // In scraper mode the model selector maps to browser model buttons
      // — switch immediately and open a fresh chat so the new model starts clean.
      if (scraper) {
        try {
          const res = await fetch("/api/scraper/model", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ model_type: selectedModel })
          });
          const data = await res.json().catch(() => ({}));
          if (data.status === "ok") {
            showToast("Switched to " + (modelSelectEl.options[modelSelectEl.selectedIndex]?.textContent || selectedModel), "success");
            await createChat();
          } else {
            showToast(data.message || "Model switch failed", "error");
          }
        } catch (e) {
          showToast("Model switch error: " + e.message, "error");
        }
      }
    });

    // thinking mode change handled inline per-button above

    async function loadProjects() {
      try {
        const data = await fetch('/api/projects').then(r => r.json());
        projectList = data.projects || [];
      } catch (err) {
        console.error("Failed to load projects:", err);
      }
    }



    // Expose loadModels globally so appearance.js can trigger reload on mode switch
    window.loadModels = loadModels;


