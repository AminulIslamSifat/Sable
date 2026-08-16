    // === Brain / Memory Panel ===
    const CATEGORIES = ["semantic", "episodic", "ephemeral"];
    const CAT_LABELS = { semantic: "Facts & Knowledge", episodic: "Events & Experiences", ephemeral: "⏳ Temporary" };
    let _memoryCache = { semantic: [], episodic: [], procedural: [], ephemeral: [] };
    let _activeCat = "semantic";
    let _protectedCache = [];

    // Dynamic memory panel for Library > Memory tab
    function renderMemoryPanel(container) {
      container.innerHTML = "";

      // === Protected Memory ===
      const protSection = document.createElement("div");
      protSection.style.marginTop = "24px";
      protSection.style.paddingTop = "16px";
      protSection.style.borderTop = "1px solid var(--border)";
      protSection.innerHTML = `
        <h3 class="mem-title"><span class="icon-emoji">🔒</span><i data-lucide="lock" class="icon-lucide"></i> Protected Memory</h3>
        <p class="muted mem-desc">Stored in Brain/Protected.json. Cannot be deleted by consolidation. Boosted in search.</p>
        <div id="libProtectedList" class="mem-list"></div>
        <div class="mem-input-row">
          <input type="text" id="libProtKeyInput" placeholder="Key" class="mem-input mem-key">
          <input type="text" id="libProtValInput" placeholder="Core fact (never forget)" class="mem-input mem-val">
          <button id="libProtAddBtn" class="icon-btn" style="width:auto;padding:6px 12px;font-size:12px;">+ Add</button>
        </div>
        <div class="mem-input-row mem-save-row">
          <button id="libProtSaveBtn" class="icon-btn" style="width:auto;padding:6px 16px;font-size:12px;"><span class="icon-emoji">💾</span><i data-lucide="hard-drive" class="icon-lucide"></i>&nbsp; Save</button>
          <span id="libProtStatus" class="muted mem-status"></span>
        </div>`;
      container.appendChild(protSection);

      // === Memory Entries ===
      const memSection = document.createElement("div");
      memSection.style.marginTop = "20px";
      memSection.innerHTML = `
        <h3 class="mem-title">Memory Entries</h3>
        <p class="muted mem-desc">Stored in Brain/Memory.json. Injected into context on sync.</p>
        <div id="libMemoryList" class="mem-list"></div>
        <div class="mem-input-row">
          <input type="text" id="libMemKeyInput" placeholder="Key (e.g. Favorite color)" class="mem-input mem-key">
          <input type="text" id="libMemValInput" placeholder="Value" class="mem-input mem-val">
          <button id="libMemAddBtn" class="icon-btn" style="width:auto;padding:6px 12px;font-size:12px;">+ Add</button>
        </div>
        <div class="mem-input-row" id="libMemExpiryRow" style="display:none;">
          <label class="muted" style="font-size:12px;"><span class="icon-emoji">⏳</span><i data-lucide="hourglass" class="icon-lucide"></i> Expires:</label>
          <input type="datetime-local" id="libMemExpiryInput" class="mem-input" style="flex:1;">
        </div>
        <div class="mem-input-row mem-save-row">
          <button id="libMemSaveBtn" class="icon-btn" style="width:auto;padding:6px 16px;font-size:12px;"><span class="icon-emoji">💾</span><i data-lucide="hard-drive" class="icon-lucide"></i> Save</button>
          <span id="libMemStatus" class="muted mem-status"></span>
        </div>`;
      container.appendChild(memSection);

      // === Personality Assessment ===
      const persSection = document.createElement("div");
      persSection.style.marginTop = "24px";
      persSection.style.paddingTop = "16px";
      persSection.style.borderTop = "1px solid var(--border)";
      persSection.innerHTML = `
        <h3 class="mem-title" style="cursor:pointer;user-select:none;" id="libPersonalityToggle"><span class="icon-emoji">🪞</span><i data-lucide="scan-face" class="icon-lucide"></i> Personality Assessment <span id="libPersonalityArrow" style="font-size:11px;opacity:0.5;">▶</span></h3>
        <div id="libPersonalitySection" style="display:none;">
          <p class="muted mem-desc">Behavioral profile generated during memory consolidation. Stored in Brain/user_personality.json. Updated automatically.</p>
          <div id="libPersonalityContent" style="margin-top:10px;font-size:12px;line-height:1.6;"></div>
        </div>`;
      container.appendChild(persSection);

      if (typeof lucide !== "undefined") lucide.createIcons();

      // Wire up all event handlers
      _wireMemoryPanelHandlers(container);
    }

    function _wireMemoryPanelHandlers(container) {
      // --- Protected Memory ---
      const libProtectedList = container.querySelector("#libProtectedList");
      const libProtKeyInput = container.querySelector("#libProtKeyInput");
      const libProtValInput = container.querySelector("#libProtValInput");
      const libProtAddBtn = container.querySelector("#libProtAddBtn");
      const libProtSaveBtn = container.querySelector("#libProtSaveBtn");
      const libProtStatus = container.querySelector("#libProtStatus");

      function renderLibProtected() {
        libProtectedList.innerHTML = "";
        if (_protectedCache.length === 0) {
          libProtectedList.innerHTML = '<p class="muted mem-empty">No protected entries yet.</p>';
          return;
        }
        _protectedCache.forEach((entry, idx) => {
          const wrapper = document.createElement("div");
          wrapper.className = "mem-entry mem-entry-protected";
          const header = document.createElement("div");
          header.className = "mem-entry-header";
          const keySpan = document.createElement("span");
          keySpan.className = "mem-entry-key";
          keySpan.textContent = entry.key || "(no key)";
          const valPreview = document.createElement("span");
          valPreview.className = "mem-entry-val";
          valPreview.textContent = entry.value || "";
          const badge = document.createElement("span");
          badge.className = "mem-protected-badge";
          badge.textContent = "🔒";
          const delBtn = document.createElement("button");
          delBtn.textContent = "✕";
          delBtn.className = "icon-btn mem-del-btn";
          delBtn.title = "Delete permanently";
          delBtn.addEventListener("click", async (e) => {
            e.stopPropagation();
            if (!await sableConfirm(`Delete protected entry "${entry.key}"?`, { danger: true })) return;
            try {
              const res = await fetch("/api/settings/memory/protected", {
                method: "DELETE", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ key: entry.key })
              });
              if (res.ok) {
                _protectedCache.splice(idx, 1);
                renderLibProtected();
                showToast("🗑 Protected entry deleted", "success");
              } else {
                showToast("✕ Failed to delete", "error");
              }
            } catch { showToast("✕ Error deleting", "error"); }
          });
          header.append(keySpan, valPreview, badge, delBtn);
          wrapper.appendChild(header);
          const fullVal = document.createElement("div");
          fullVal.className = "mem-full-val";
          fullVal.textContent = entry.value || "";
          wrapper.appendChild(fullVal);
          let expanded = false;
          wrapper.addEventListener("click", () => {
            expanded = !expanded;
            fullVal.classList.toggle("show", expanded);
            valPreview.classList.toggle("hidden", expanded);
          });
          libProtectedList.appendChild(wrapper);
        });
      }

      // Load protected
      fetch("/api/settings/memory/protected").then(r => r.json()).then(data => {
        _protectedCache = Array.isArray(data.protected) ? data.protected : [];
        renderLibProtected();
      }).catch(() => {});

      libProtAddBtn.addEventListener("click", () => {
        const key = libProtKeyInput.value.trim();
        const value = libProtValInput.value.trim();
        if (!key && !value) return;
        _protectedCache.push({ key, value });
        libProtKeyInput.value = "";
        libProtValInput.value = "";
        renderLibProtected();
      });

      libProtSaveBtn.addEventListener("click", async () => {
        libProtStatus.textContent = "Saving...";
        try {
          const res = await fetch("/api/settings/memory/protected", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ protected: _protectedCache })
          });
          libProtStatus.innerHTML = res.ok ? "✓ Saved" : "✕ Failed";
        } catch { libProtStatus.innerHTML = "✕ Error"; }
        setTimeout(() => { libProtStatus.textContent = ""; }, 2000);
      });

      // --- Memory Entries ---
      const libMemoryList = container.querySelector("#libMemoryList");
      const libMemKeyInput = container.querySelector("#libMemKeyInput");
      const libMemValInput = container.querySelector("#libMemValInput");
      const libMemAddBtn = container.querySelector("#libMemAddBtn");
      const libMemSaveBtn = container.querySelector("#libMemSaveBtn");
      const libMemStatus = container.querySelector("#libMemStatus");
      const libMemExpiryRow = container.querySelector("#libMemExpiryRow");
      const libMemExpiryInput = container.querySelector("#libMemExpiryInput");

      function renderLibMemory() {
        libMemoryList.innerHTML = "";
        libMemExpiryRow.style.display = _activeCat === "ephemeral" ? "" : "none";
        const tabBar = document.createElement("div");
        tabBar.className = "mem-tab-bar";
        CATEGORIES.forEach(cat => {
          const btn = document.createElement("button");
          btn.textContent = CAT_LABELS[cat];
          btn.className = "icon-btn mem-tab-btn" + (cat === _activeCat ? " active" : "");
          btn.addEventListener("click", () => { _activeCat = cat; renderLibMemory(); });
          tabBar.appendChild(btn);
        });
        libMemoryList.appendChild(tabBar);
        const entries = _memoryCache[_activeCat] || [];
        if (entries.length === 0) {
          libMemoryList.innerHTML += '<p class="muted mem-empty">No entries in this category yet.</p>';
          return;
        }
        entries.forEach((entry, idx) => {
          const wrapper = document.createElement("div");
          wrapper.className = "mem-entry";
          const header = document.createElement("div");
          header.className = "mem-entry-header";
          const keySpan = document.createElement("span");
          keySpan.className = "mem-entry-key";
          keySpan.textContent = entry.key || "(no key)";
          const valPreview = document.createElement("span");
          valPreview.className = "mem-entry-val";
          valPreview.textContent = entry.value || "";
          const delBtn = document.createElement("button");
          delBtn.textContent = "✕";
          delBtn.className = "icon-btn mem-del-btn";
          delBtn.title = "Delete permanently";
          delBtn.addEventListener("click", async (e) => {
            e.stopPropagation();
            if (!await sableConfirm(`Delete memory entry "${entry.key}" from ${_activeCat}?`, { danger: true })) return;
            try {
              const res = await fetch("/api/settings/memory", {
                method: "DELETE", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ category: _activeCat, key: entry.key })
              });
              if (res.ok) {
                _memoryCache[_activeCat].splice(idx, 1);
                renderLibMemory();
                showToast("🗑 Memory entry deleted", "success");
              } else {
                showToast("✕ Failed to delete", "error");
              }
            } catch { showToast("✕ Error deleting", "error"); }
          });
          header.append(keySpan, valPreview);
          if (entry.expires_at) {
            const badge = document.createElement("span");
            badge.className = "mem-expiry-badge";
            badge.textContent = "⏳ " + String(entry.expires_at).replace("T", " ").slice(0, 16);
            header.appendChild(badge);
          }
          header.appendChild(delBtn);
          wrapper.appendChild(header);
          const fullVal = document.createElement("div");
          fullVal.className = "mem-full-val";
          fullVal.textContent = entry.value || "";
          wrapper.appendChild(fullVal);
          let expanded = false;
          wrapper.addEventListener("click", () => {
            expanded = !expanded;
            fullVal.classList.toggle("show", expanded);
            valPreview.classList.toggle("hidden", expanded);
          });
          libMemoryList.appendChild(wrapper);
        });
      }

      // Load memory
      fetch("/api/settings/memory").then(r => r.json()).then(data => {
        const raw = data.memory;
        if (raw && typeof raw === "object" && !Array.isArray(raw)) {
          _memoryCache = { semantic: raw.semantic || [], episodic: raw.episodic || [], procedural: raw.procedural || [], ephemeral: raw.ephemeral || [] };
        } else {
          _memoryCache = { semantic: Array.isArray(raw) ? raw : [], episodic: [], procedural: [], ephemeral: [] };
        }
        renderLibMemory();
      }).catch(() => {});

      libMemAddBtn.addEventListener("click", () => {
        const key = libMemKeyInput.value.trim();
        const value = libMemValInput.value.trim();
        if (!key && !value) return;
        if (!_memoryCache[_activeCat]) _memoryCache[_activeCat] = [];
        const newEntry = { key, value };
        if (_activeCat === "ephemeral" && libMemExpiryInput.value) {
          newEntry.expires_at = libMemExpiryInput.value;
        }
        _memoryCache[_activeCat].push(newEntry);
        libMemKeyInput.value = "";
        libMemValInput.value = "";
        libMemExpiryInput.value = "";
        renderLibMemory();
      });

      libMemSaveBtn.addEventListener("click", async () => {
        libMemStatus.textContent = "Saving...";
        try {
          const res = await fetch("/api/settings/memory", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ memory: _memoryCache })
          });
          libMemStatus.innerHTML = res.ok ? "✓ Saved" : "✕ Failed";
        } catch { libMemStatus.innerHTML = "✕ Error"; }
        setTimeout(() => { libMemStatus.textContent = ""; }, 2000);
      });

      // --- Personality Assessment ---
      const libPersonalityToggle = container.querySelector("#libPersonalityToggle");
      const libPersonalitySection = container.querySelector("#libPersonalitySection");
      const libPersonalityContent = container.querySelector("#libPersonalityContent");
      const libPersonalityArrow = container.querySelector("#libPersonalityArrow");
      let _libPersonalityLoaded = false;

      if (libPersonalityToggle) {
        libPersonalityToggle.addEventListener("click", async () => {
          const isOpen = libPersonalitySection.style.display !== "none";
          libPersonalitySection.style.display = isOpen ? "none" : "";
          libPersonalityArrow.textContent = isOpen ? "▶" : "▼";
          if (!isOpen && !_libPersonalityLoaded) {
            _libPersonalityLoaded = true;
            try {
              const res = await fetch("/api/settings/personality");
              const data = await res.json();
              if (data.personality) {
                libPersonalityContent.innerHTML = renderPersonality(data.personality);
              } else {
                libPersonalityContent.innerHTML = '<p class="muted" style="font-style:italic;">No assessment yet. It generates after your next memory consolidation.</p>';
              }
            } catch {
              libPersonalityContent.innerHTML = '<p class="muted">Failed to load personality data.</p>';
            }
          }
        });
      }
    }








    // ── Memory Consolidation Settings ──
    const consolidationModel = document.getElementById("consolidationModel");
    const consolidationFB1 = document.getElementById("consolidationFallback1");
    const consolidationFB2 = document.getElementById("consolidationFallback2");
    const consolidationFB3 = document.getElementById("consolidationFallback3");
    const consolidationP1 = document.getElementById("consolidationProfile1");
    const consolidationP2 = document.getElementById("consolidationProfile2");
    const consolidationP3 = document.getElementById("consolidationProfile3");


    function populateConsolidationModels() {
      const selects = [consolidationModel, consolidationFB1, consolidationFB2, consolidationFB3];
      for (const sel of selects) {
        if (!sel) continue;
        const current = sel.value;
        const isPrimary = sel === consolidationModel;
        sel.innerHTML = isPrimary ? '<option value="">Default (current chat model)</option>' : '<option value="">— None —</option>';
        for (const m of modelList) {
          const opt = document.createElement("option");
          opt.value = m.id;
          opt.textContent = m.name || m.id;
          sel.appendChild(opt);
        }
        sel.value = current;
      }
    }

    async function populateConsolidationProfiles() {
      const selects = [consolidationP1, consolidationP2, consolidationP3];
      const profiles = [];
      try {
        const res = await fetch("/api/settings/accounts");
        if (res.ok) {
          const data = await res.json();
          for (const acc of (data.accounts || [])) {
            profiles.push({ name: acc.name, label: acc.email ? `${acc.name} (${acc.email})` : acc.name });
          }
        }
      } catch {}
      for (const sel of selects) {
        if (!sel) continue;
        const current = sel.value;
        sel.innerHTML = '<option value="">— None —</option>';
        for (const p of profiles) {
          const opt = document.createElement("option");
          opt.value = p.name;
          opt.textContent = p.label;
          sel.appendChild(opt);
        }
        sel.value = current;
      }
    }

    async function loadConsolidationSettings() {
      populateConsolidationModels();
      await populateConsolidationProfiles();
      try {
        const res = await fetch("/api/settings/consolidation");
        if (res.ok) {
          const d = await res.json();
          if (consolidationModel) consolidationModel.value = d.model || "";
          const fbs = d.fallback_models || [];
          if (consolidationFB1) consolidationFB1.value = fbs[0] || "";
          if (consolidationFB2) consolidationFB2.value = fbs[1] || "";
          if (consolidationFB3) consolidationFB3.value = fbs[2] || "";
          const bps = d.browser_profiles || [];
          if (consolidationP1) consolidationP1.value = bps[0] || "";
          if (consolidationP2) consolidationP2.value = bps[1] || "";
          if (consolidationP3) consolidationP3.value = bps[2] || "";
        }
      } catch {}
    }

    // ── /Memory Consolidation Settings ──

    // === Memory Search Settings ===
    const msModelSelect = document.getElementById("msModelSelect");
    const msThresholdEditor = document.getElementById("msThresholdEditor");
    const msEnabled = document.getElementById("msEnabled");
    const msSaveBtn = document.getElementById("msSaveBtn");
    const msInfo = document.getElementById("msInfo");
    let _msLoaded = false;

    function buildThresholdEditor(models, customThresholds) {
      msThresholdEditor.innerHTML = "";
      const header = document.createElement("p");
      header.className = "muted";
      header.style.cssText = "font-size:11px;margin:0 0 2px;text-transform:uppercase;letter-spacing:0.5px;";
      header.textContent = "Per-model thresholds (blank = calibrated default)";
      msThresholdEditor.appendChild(header);
      (models || []).forEach((m) => {
        const row = document.createElement("div");
        row.style.cssText = "display:flex;align-items:center;gap:8px;";
        const label = document.createElement("span");
        label.className = "muted";
        label.style.cssText = "font-size:12px;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;";
        label.textContent = m.id.split("/").pop();
        label.title = m.id;
        const input = document.createElement("input");
        input.type = "number";
        input.step = "0.001";
        input.min = "0";
        input.max = "1";
        input.className = "mem-input";
        input.style.cssText = "width:80px;";
        input.placeholder = String(m.threshold);
        input.dataset.model = m.id;
        const custom = customThresholds?.[m.id];
        if (custom !== undefined && custom !== null) input.value = custom;
        row.appendChild(label);
        row.appendChild(input);
        msThresholdEditor.appendChild(row);
      });
    }

    async function loadMemorySearchSettings() {
      if (_msLoaded) return;
      try {
        const res = await fetch("/api/settings/memory-search");
        if (!res.ok) return;
        const data = await res.json();
        msModelSelect.innerHTML = "";
        (data.available_models || []).forEach((m) => {
          const opt = document.createElement("option");
          opt.value = m.id;
          opt.textContent = `${m.id.split("/").pop()} (θ=${m.threshold})`;
          if (m.id === data.current_model) opt.selected = true;
          msModelSelect.appendChild(opt);
        });
        document.getElementById("msTopMemory").value = data.top_memory || 5;
        document.getElementById("msTopProcedural").value = data.top_procedural || 3;
        document.getElementById("msTopTotal").value = data.top_total || 9;
        document.getElementById("msMaxChars").value = data.max_prompt_chars || 20000;
        buildThresholdEditor(data.available_models, data.model_thresholds);
        msEnabled.checked = data.enabled !== false;
        msInfo.textContent = `Active: ${data.current_model} | Threshold: ${data.current_threshold}`;
        _msLoaded = true;
      } catch (e) { console.error("loadMemorySearchSettings failed", e); }
    }

    msSaveBtn.addEventListener("click", async () => {
      try {
        const modelThresholds = {};
        msThresholdEditor.querySelectorAll("input[data-model]").forEach((inp) => {
          if (inp.value.trim() !== "") modelThresholds[inp.dataset.model] = parseFloat(inp.value);
        });
        const res = await fetch("/api/settings/memory-search", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            model: msModelSelect.value,
            top_memory: parseInt(document.getElementById("msTopMemory").value) || 5,
            top_procedural: parseInt(document.getElementById("msTopProcedural").value) || 3,
            top_total: parseInt(document.getElementById("msTopTotal").value) || 9,
            max_prompt_chars: parseInt(document.getElementById("msMaxChars").value) || 20000,
            model_thresholds: modelThresholds,
            enabled: msEnabled.checked,
          }),
        });
        if (res.ok) {
          const data = await res.json();
          msInfo.textContent = `Active: ${data.current_model} | Threshold: ${data.current_threshold}`;
          showToast("✅ Memory search settings saved", "success");
        } else {
          showToast("✕ Failed to save", "error");
        }
      } catch (e) { showToast("✕ Error saving", "error"); }
    });

    document.getElementById("msRefreshCache").addEventListener("click", async () => {
      const btn = document.getElementById("msRefreshCache");
      const status = document.getElementById("msCacheStatus");
      btn.disabled = true;
      btn.textContent = "⏳ Rebuilding…";
      status.textContent = "";
      try {
        const res = await fetch("/api/settings/memory-search/refresh-cache", { method: "POST" });
        if (res.ok) {
          const data = await res.json();
          status.textContent = data.detail || "Cache rebuilt.";
          showToast("🔄 Memory cache rebuilt", "success");
        } else {
          status.textContent = "Failed to rebuild cache.";
          showToast("✕ Cache refresh failed", "error");
        }
      } catch (e) {
        status.textContent = "Error.";
        showToast("✕ Error refreshing cache", "error");
      } finally {
        btn.disabled = false;
        btn.textContent = "🔄 Refresh Cache";
      }
    });

    // === Personal Context (instruction/personal.md) ===
    const personalArea = document.getElementById("personalContextArea");
    const personalSaveBtn = document.getElementById("personalSaveBtn");
    const personalStatus = document.getElementById("personalStatus");

    async function loadPersonal() {
      try {
        const res = await fetch("/api/settings/personal");
        const data = await res.json();
        personalArea.value = data.content || "";
      } catch (e) { console.error("loadPersonal failed", e); }
    }

    personalSaveBtn.addEventListener("click", async () => {
      personalSaveBtn.disabled = true;
      personalStatus.textContent = "Saving...";
      try {
        const res = await fetch("/api/settings/personal", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ content: personalArea.value }),
        });
        if (res.ok) {
          personalStatus.textContent = "✓ Saved";
          showToast("👤 Personal context saved", "success");
        } else {
          personalStatus.textContent = "Failed to save.";
          showToast("✕ Failed to save personal context", "error");
        }
      } catch (e) {
        personalStatus.textContent = "Error.";
        showToast("✕ Error saving personal context", "error");
      } finally {
        personalSaveBtn.disabled = false;
        setTimeout(() => { personalStatus.textContent = ""; }, 3000);
      }
    });

    // Load personal + search + consolidation settings when Brain tab is clicked
    document.querySelector('[data-tab="brain"]').addEventListener("click", () => {
      loadPersonal();
      loadMemorySearchSettings();
      loadConsolidationSettings();
    });

    // Register Brain tab with universal save
    _universalSave.register("brain", async () => {
      // Save memory search settings
      const modelThresholds = {};
      msThresholdEditor.querySelectorAll("input[data-model]").forEach((inp) => {
        if (inp.value.trim() !== "") modelThresholds[inp.dataset.model] = parseFloat(inp.value);
      });
      const msRes = await fetch("/api/settings/memory-search", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          model: msModelSelect.value,
          top_memory: parseInt(document.getElementById("msTopMemory").value) || 5,
          top_procedural: parseInt(document.getElementById("msTopProcedural").value) || 3,
          top_total: parseInt(document.getElementById("msTopTotal").value) || 9,
          max_prompt_chars: parseInt(document.getElementById("msMaxChars").value) || 20000,
          model_thresholds: modelThresholds,
          enabled: msEnabled.checked,
        }),
      });
      if (msRes.ok) {
        const data = await msRes.json();
        msInfo.textContent = `Active: ${data.current_model} | Threshold: ${data.current_threshold}`;
      }
      // Save consolidation settings
      const fallbackModels = [consolidationFB1?.value, consolidationFB2?.value, consolidationFB3?.value].filter(Boolean);
      const browserProfiles = [consolidationP1?.value, consolidationP2?.value, consolidationP3?.value].filter(Boolean);
      await fetch("/api/settings/consolidation", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          model: consolidationModel ? consolidationModel.value : "",
          fallback_models: fallbackModels,
          browser_profiles: browserProfiles,
        }),
      });
    });



    function renderPersonality(p) {
      let html = "";
      const renderList = (items, label, color) => {
        if (!items || !items.length) return "";
        let s = `<div style="margin-bottom:12px;"><strong style="color:${color};font-size:11px;text-transform:uppercase;letter-spacing:0.5px;">${label}</strong><ul style="margin:4px 0 0;padding-left:16px;list-style:disc;">`;
        items.forEach(item => {
          if (typeof item === "object" && item.trait) {
            s += `<li style="margin-bottom:4px;"><strong>${item.trait}</strong> <span class="muted" style="font-size:11px;">(${item.confidence || "—"})</span><br><span class="muted" style="font-size:11px;">${item.evidence || ""}</span></li>`;
          } else if (typeof item === "object" && item.pattern) {
            s += `<li style="margin-bottom:4px;"><strong>${item.pattern}</strong><br><span class="muted" style="font-size:11px;">${item.evidence || ""}</span></li>`;
          } else if (typeof item === "object" && item.claimed) {
            s += `<li style="margin-bottom:4px;">Says: <em>"${item.claimed}"</em> → Does: <em>"${item.actual}"</em></li>`;
          } else {
            s += `<li style="margin-bottom:4px;">${item}</li>`;
          }
        });
        return s + "</ul></div>";
      };
      html += renderList(p.strengths, "Strengths", "#4ade80");
      html += renderList(p.weaknesses, "Weaknesses", "#f87171");
      html += renderList(p.contradictions, "Contradictions", "#fbbf24");
      html += renderList(p.blind_spots, "Blind Spots", "#a78bfa");
      if (p.summary) {
        html += `<div style="margin-top:12px;padding:10px;background:var(--bg-secondary);border-radius:8px;border:1px solid var(--border);"><strong style="font-size:11px;text-transform:uppercase;letter-spacing:0.5px;">Summary</strong><p style="margin:6px 0 0;" class="muted">${p.summary}</p></div>`;
      }
      return html || '<p class="muted">Empty assessment.</p>';
    }

