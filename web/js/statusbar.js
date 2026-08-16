    /* ---------- Status Bar (thinking mode, cwd, context) ---------- */
    const statusDropdownEl = document.getElementById("statusThinkingDropdown");
    const statusLabelEl = document.getElementById("statusThinkingLabel");
    const statusMenuEl = document.getElementById("statusThinkingMenu");
    const statusCwdEl = document.getElementById("statusCwd");
    const statusContextEl = document.getElementById("statusContext");

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
    const inputComposite = document.querySelector(".input-composite");
    if (statusTriggerEl && statusDropdownEl && statusMenuEl) {
      statusTriggerEl.addEventListener("click", (e) => {
        e.stopPropagation();
        const isOpen = statusDropdownEl.classList.toggle("open");
        if (isOpen && inputComposite) {
          const rect = inputComposite.getBoundingClientRect();
          statusMenuEl.style.left = rect.left + "px";
          statusMenuEl.style.bottom = (window.innerHeight - rect.top + 2) + "px";
          statusMenuEl.style.minWidth = Math.max(100, rect.width * 0.3) + "px";
        }
      });
      document.addEventListener("click", () => {
        statusDropdownEl.classList.remove("open");
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
          const res = await fetch("/api/filesystem/pick-folder");
          const data = await res.json();
          if (data.path && window.pickFsRoot) {
            window.pickFsRoot(data.path);
          }
        } catch (err) {
          console.error("[StatusBar] pick-folder failed:", err);
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


    /* ---------- Glass dropdown (custom model selector) ---------- */
    const glassDropdown = document.getElementById("modelDropdown");
    const glassTrigger = document.getElementById("modelTrigger");
    const glassMenu = document.getElementById("modelMenu");
    const glassLabel = glassTrigger.querySelector(".glass-dropdown-label");

    function syncGlassDropdown() {
      glassMenu.innerHTML = "";
      for (const opt of modelSelectEl.options) {
        const item = document.createElement("div");
        item.className = "glass-dropdown-item" + (opt.selected ? " active" : "");
        item.textContent = opt.textContent;
        item.dataset.value = opt.value;
        item.addEventListener("click", () => {
          modelSelectEl.value = opt.value;
          modelSelectEl.dispatchEvent(new Event("change"));
          glassLabel.textContent = opt.textContent;
          glassDropdown.classList.remove("open");
          syncGlassDropdown();
        });
        glassMenu.appendChild(item);
      }
      const sel = modelSelectEl.options[modelSelectEl.selectedIndex];
      if (sel) glassLabel.textContent = sel.textContent;
    }

    glassTrigger.addEventListener("click", (e) => {
      e.stopPropagation();
      glassDropdown.classList.toggle("open");
    });

    document.addEventListener("click", (e) => {
      if (!glassDropdown.contains(e.target)) {
        glassDropdown.classList.remove("open");
      }
    });

    async function loadModels() {
      let models = FALLBACK_MODELS;
      try {
        const data = await fetch("/api/models").then(r => r.json());
        if (Array.isArray(data.models) && data.models.length > 0) {
          models = data.models;
        }
      } catch (err) {
        console.warn("Could not load /api/models, using fallback list:", err);
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
      syncGlassDropdown();

      populateThinkingModes(savedMode);
      updateAttachUI();
    }

    modelSelectEl.addEventListener("change", async () => {
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
      selectedModel = modelSelectEl.value;
      try { localStorage.setItem(MODEL_KEY, selectedModel); } catch (err) {}
      // Switching models resets the thinking mode to that model's default,
      // since the previous mode may not exist on the newly selected model.
      populateThinkingModes(null);
      updateAttachUI();

      // In scraper mode the model selector maps to browser model buttons
      // (e.g. DeepSeek Instant/Expert/Vision) — switch immediately and
      // open a fresh chat so the new model starts clean.
      if (scraperMode) {
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



