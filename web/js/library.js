    /* ---------- Library Panel ---------- */
    const libraryOverlay = document.getElementById("libraryOverlay");
    const libraryBtn = document.getElementById("libraryBtn");
    const libraryClose = document.getElementById("libraryClose");
    const libraryTabs = document.getElementById("libraryTabs");
    const libraryBody = document.getElementById("libraryBody");
    let _libLoaded = { agents: false, research: false, notes: false, gallery: false, skills: false, promptgen: false, imagegen: false };

    function openLibrary() {
      libraryOverlay.classList.remove("hidden");
      // Show/hide Telegram tab based on toggle state
      const tgTab = document.getElementById('libTelegramTab');
      if (tgTab) tgTab.style.display = localStorage.getItem('sable_telegram_enabled') === 'true' ? '' : 'none';
      // Load active tab if not yet loaded
      const activeTab = libraryTabs.querySelector(".settings-tab.active");
      if (activeTab) loadLibraryTab(activeTab.dataset.tab);
    }

    function closeLibrary() {
      stopTgPoll();
      libraryOverlay.classList.add("hidden");
    }

    libraryBtn.addEventListener("click", openLibrary);
    libraryClose.addEventListener("click", closeLibrary);
    libraryOverlay.addEventListener("click", (e) => {
      if (e.target === libraryOverlay) closeLibrary();
    });

    // Library tab switching
    libraryTabs.querySelectorAll(".settings-tab").forEach((tab) => {
      tab.addEventListener("click", () => {
        libraryTabs.querySelectorAll(".settings-tab").forEach((t) => t.classList.remove("active"));
        libraryBody.querySelectorAll(".settings-tab-content").forEach((c) => c.classList.remove("active"));
        tab.classList.add("active");
        const target = document.getElementById("tab-" + tab.dataset.tab);
        if (target) target.classList.add("active");
        loadLibraryTab(tab.dataset.tab);
      });
    });

    async function loadLibraryTab(tabId) {
      const section = tabId.replace("lib-", "");
      const container = document.getElementById("tab-" + tabId);
      if (!container) return;
      // Email: skip reload if already cached
      if (section === "email" && _emailState.loaded) return;
      // Telegram: skip reload if already cached
      if (section === "telegram" && _tgState.loaded) return;
      container.innerHTML = '<div class="library-loading">Loading…</div>';
      try {
        if (section === "gallery") {
          const res = await fetch("/api/library/gallery");
          const items = await res.json();
          renderGallery(container, items);
        } else if (section === "skills") {
          await renderSkillsPanel(container);
        } else if (section === "email") {
          renderEmailPanel(container);
        } else if (section === "telegram") {
          renderTelegramPanel(container);
        } else if (section === "memory") {
          await renderMemoryPanel(container);
        } else if (section === "research") {
          renderResearchPanel(container);
        } else if (section === "imagegen") {
          renderImageGenPanel(container);
        } else if (section === "promptgen") {
          renderPromptGenPanel(container);
        } else {
          const res = await fetch(`/api/library/${section}`);
          const items = await res.json();
          renderMdCards(container, items, section);
        }
      } catch (e) {
        container.innerHTML = '<div class="library-empty">Failed to load.</div>';
      }
    }

    function renderMdCards(container, items, section) {
      if (!items.length) {
        container.innerHTML = '<div class="library-empty">Nothing here yet.</div>';
        return;
      }
      container.innerHTML = "";
      const grid = document.createElement("div");
      grid.className = "library-card-grid";
      items.forEach((item) => {
        const card = document.createElement("div");
        card.className = "library-card";
        card.innerHTML = `
          <div class="library-card-title">${escHtml(item.title)}</div>
          <div class="library-card-date">${item.date || ""}</div>
          <div class="library-card-preview">${escHtml(item.preview || "")}</div>
        `;
        card.addEventListener("click", () => openLibraryReader(section, item.filename, item.title));
        grid.appendChild(card);
      });
      container.appendChild(grid);
    }

    function renderGallery(container, items) {
      if (!items.length) {
        container.innerHTML = '<div class="library-empty">No images uploaded yet.</div>';
        return;
      }
      container.innerHTML = "";
      const grid = document.createElement("div");
      grid.className = "library-gallery-grid";
      items.forEach((item) => {
        const cell = document.createElement("div");
        cell.className = "library-gallery-item";
        cell.innerHTML = `<img src="${item.url}" alt="${escHtml(item.filename)}" loading="lazy">`;
        cell.title = item.filename;
        cell.addEventListener("click", () => window.open(item.url, "_blank"));
        grid.appendChild(cell);
      });
      container.appendChild(grid);
    }

    async function renderSkillsPanel(container) {
      container.innerHTML = '<div class="library-loading">Loading…</div>';
      try {
        const res = await fetch("/api/skills/browse");
        const data = await res.json();
        const skills = data.skills || [];
        if (!skills.length) {
          container.innerHTML = '<div class="library-empty">No skills registered yet.</div>';
          return;
        }
        container.innerHTML = "";
        const grid = document.createElement("div");
        grid.className = "skills-grid";
        const disabled = getDisabledSkills();
        skills.forEach((sk) => {
          const isDisabled = disabled.includes(sk.path);
          const chip = document.createElement("div");
          chip.className = "skill-chip" + (isDisabled ? " skill-chip-disabled" : "");
          chip.innerHTML = `<div class="skill-chip-name">${escHtml(sk.name)}${isDisabled ? ' <span class="skill-disabled-badge">off</span>' : ""}</div><div class="skill-chip-cat">${escHtml(sk.category)}</div>`;
          chip.addEventListener("click", () => showSkillDetail(sk));
          grid.appendChild(chip);
        });
        container.appendChild(grid);

        // === Self Learned Skills (Procedural Memory) ===
        try {
          const memRes = await fetch("/api/settings/memory");
          const memData = await memRes.json();
          const raw = memData.memory;
          let procEntries = [];
          if (raw && typeof raw === "object" && !Array.isArray(raw)) {
            procEntries = raw.procedural || [];
          }
          if (procEntries.length > 0) {
            const selfSection = document.createElement("div");
            selfSection.style.cssText = "margin-top:24px;padding-top:16px;border-top:1px solid var(--border);";
            selfSection.innerHTML = '<h3 style="font-size:14px;font-weight:600;margin-bottom:4px;">Self Learned Skills</h3><p class="muted" style="font-size:11px;margin-bottom:12px;">Procedural memories extracted from conversations.</p>';
            procEntries.forEach((entry) => {
              const row = document.createElement("div");
              row.style.cssText = "padding:10px 12px;background:var(--bg-secondary);border:1px solid var(--border);border-radius:8px;margin-bottom:8px;";
              const keyEl = document.createElement("div");
              keyEl.style.cssText = "font-size:13px;font-weight:600;margin-bottom:4px;color:var(--text-primary);";
              keyEl.textContent = entry.key || "(unnamed)";
              const valEl = document.createElement("div");
              valEl.style.cssText = "font-size:12px;line-height:1.5;color:var(--text-secondary);white-space:pre-wrap;word-break:break-word;";
              valEl.textContent = entry.value || "";
              const delBtn = document.createElement("button");
              delBtn.textContent = "\u2715";
              delBtn.style.cssText = "position:absolute;top:8px;right:8px;background:none;border:none;color:var(--text-dim);cursor:pointer;font-size:14px;padding:2px 6px;border-radius:4px;opacity:0.5;transition:opacity 0.2s;";
              delBtn.title = "Delete this skill";
              delBtn.addEventListener("mouseenter", () => { delBtn.style.opacity = "1"; delBtn.style.color = "var(--danger,#ff5050)"; });
              delBtn.addEventListener("mouseleave", () => { delBtn.style.opacity = "0.5"; delBtn.style.color = "var(--text-dim)"; });
              delBtn.addEventListener("click", async (e) => {
                e.stopPropagation();
                if (!await sableConfirm("Delete self-learned skill \"" + (entry.key || "") + "\"?", { danger: true })) return;
                try {
                  const res = await fetch("/api/settings/memory", {
                    method: "DELETE", headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({ category: "procedural", key: entry.key })
                  });
                  if (res.ok) {
                    row.remove();
                    showToast("\ud83d\uddd1 Skill deleted", "success");
                  } else {
                    showToast("\u2715 Failed to delete", "error");
                  }
                } catch(err) { showToast("\u2715 Error deleting", "error"); }
              });
              row.style.position = "relative";
              row.append(keyEl, valEl, delBtn);
              selfSection.appendChild(row);
            });
            container.appendChild(selfSection);
          }
        } catch(e) { /* procedural fetch failed, skip */ }

      } catch {
        container.innerHTML = '<div class="library-empty">Failed to load skills.</div>';
      }
    }


    /* ---------- Prompt Generator Panel ---------- */
    function _fillPgSlot(selectEl, options, emptyLabel, preselect) {
      if (!selectEl) return;
      selectEl.innerHTML = "";
      if (emptyLabel) {
        const opt = document.createElement("option");
        opt.value = "";
        opt.textContent = emptyLabel;
        selectEl.appendChild(opt);
      }
      options.forEach((o) => {
        const opt = document.createElement("option");
        opt.value = o.value;
        opt.textContent = o.label;
        if (o.value === preselect) opt.selected = true;
        selectEl.appendChild(opt);
      });
    }


    /* ── Image Generator Panel ── */
    const _IG_STYLES = {
      "no_style": "(No style — raw prompt)",
      "anime": "Anime", "painted_anime": "Painted Anime", "ghibli": "Studio Ghibli",
      "your_name": "Your Name", "wlop": "WLOP", "atey_ghailan": "Atey Ghailan",
      "kantoku": "Kantoku", "redjuice": "Redjuice",
      "oil_painting": "Oil Painting", "watercolor": "Watercolor", "acrylic": "Acrylic",
      "impressionist": "Impressionist", "ukiyo_e": "Ukiyo-e", "art_nouveau": "Art Nouveau",
      "renaissance": "Renaissance",
      "digital_art": "Digital Art", "concept_art": "Concept Art", "pixel_art": "Pixel Art",
      "comic_book": "Comic Book", "manga": "Manga", "cartoon": "Cartoon",
      "photorealistic": "Photorealistic", "cinematic": "Cinematic", "portrait": "Portrait",
      "cyberpunk": "Cyberpunk", "steampunk": "Steampunk", "vaporwave": "Vaporwave",
      "fantasy": "Fantasy", "sci_fi": "Sci-Fi", "horror": "Horror", "surreal": "Surreal",
      "pop_art": "Pop Art", "low_poly": "Low Poly", "isometric": "Isometric",
      "mtg_card": "MTG Card", "50s_enamel": "50s Enamel",
    };

    const _IG_MODELS = {
      "flux": "Flux",
      "flux-realism": "Flux Realism",
      "flux-anime": "Flux Anime",
      "turbo": "SDXL Turbo",
      "sana": "Sana",
    };

    // Puter image models — populated from /api/settings/puter/models on first use
    let _IG_PUTER_MODELS = null;
    async function _loadPuterModels() {
      if (_IG_PUTER_MODELS) return _IG_PUTER_MODELS;
      try {
        const res = await fetch("/api/settings/puter/models");
        const data = await res.json();
        _IG_PUTER_MODELS = (data.models || []).reduce((acc, m) => { acc[m.id] = m.label; return acc; }, {});
      } catch { _IG_PUTER_MODELS = { "openai/gpt-image-1-mini": "GPT Image 1 Mini" }; }
      return _IG_PUTER_MODELS;
    }

    function renderImageGenPanel(container) {
      container.innerHTML = "";
      container.classList.add("promptgen-panel");

      const wrap = document.createElement("div");
      wrap.className = "promptgen-launch";
      wrap.innerHTML = `
        <div class="promptgen-launch-head">
          <div class="promptgen-launch-title">🎨 AI Image Generator</div>
          <div class="promptgen-launch-sub">Free · No login · Multiple providers</div>
        </div>
        <textarea id="igPrompt" class="promptgen-query" rows="3" placeholder="Describe what you want to generate…"></textarea>
        <div class="promptgen-controls">
          <select id="igProvider" class="promptgen-select" style="width:auto;min-width:130px;">
            <option value="cloudflare">Cloudflare AI (free ~260/day)</option>
            <option value="pollinations">Pollinations</option>
            <option value="perchance">Perchance</option>
            <option value="puter">Puter (free)</option>
          </select>
          <select id="igModel" class="promptgen-select" style="width:auto;min-width:140px;">
            ${Object.entries(_IG_MODELS).map(([k,v]) => `<option value="${k}">${v}</option>`).join("")}
          </select>
          <select id="igStyle" class="promptgen-select" style="width:auto;min-width:140px;display:none;">
            ${Object.entries(_IG_STYLES).map(([k,v]) => `<option value="${k}">${v}</option>`).join("")}
          </select>
          <select id="igShape" class="promptgen-select" style="width:auto;min-width:120px;">
            <option value="square">Square (1:1)</option>
            <option value="portrait">Portrait (2:3)</option>
            <option value="landscape">Landscape (3:2)</option>
          </select>
          <select id="igCount" class="promptgen-select" style="width:auto;min-width:90px;">
            <option value="1">1 image</option>
            <option value="2">2 images</option>
            <option value="3">3 images</option>
            <option value="4">4 images</option>
          </select>
          <div class="promptgen-spacer"></div>
          <button id="igGenBtn" class="promptgen-start-btn">🖼️ Generate</button>
        </div>
        <div id="igPuterUsage" style="display:none;margin-top:6px;font-size:11px;color:var(--text);opacity:0.7;"></div>
        <input id="igNegPrompt" class="promptgen-query" style="margin-top:8px;min-height:auto;padding:8px 12px;font-size:12px;" placeholder="Negative prompt (optional): things to avoid…">
        <div id="igOutputWrap" class="promptgen-output-wrap" style="display:none;">
          <div id="igGallery" class="library-gallery-grid" style="margin-bottom:10px;"></div>
          <pre id="igMeta" class="promptgen-output" style="max-height:120px;font-size:11px;"></pre>
        </div>
      `;
      container.appendChild(wrap);

      const genBtn = wrap.querySelector("#igGenBtn");
      const promptEl = wrap.querySelector("#igPrompt");
      const outputWrap = wrap.querySelector("#igOutputWrap");
      const gallery = wrap.querySelector("#igGallery");
      const metaEl = wrap.querySelector("#igMeta");
      const providerSel = wrap.querySelector("#igProvider");
      const modelSel = wrap.querySelector("#igModel");
      const styleSel = wrap.querySelector("#igStyle");
      const usageEl = wrap.querySelector("#igPuterUsage");

      // Cloudflare models catalog
      const _CF_MODELS = {
        "@cf/black-forest-labs/flux-1-schnell": "FLUX.1 Schnell ⚡",
        "@cf/black-forest-labs/flux-2-dev": "FLUX.2 Dev",
        "@cf/black-forest-labs/flux-2-klein-4b": "FLUX.2 Klein 4B",
        "@cf/black-forest-labs/flux-2-klein-9b": "FLUX.2 Klein 9B",
        "@cf/lykon/dreamshaper-8-lcm": "DreamShaper 8 LCM",
        "@cf/stabilityai/stable-diffusion-xl-base-1.0": "SDXL Base 1.0",
      };

      // Toggle model/style visibility based on provider
      async function updateProviderUI() {
        const prov = providerSel.value;
        if (prov === "cloudflare") {
          modelSel.style.display = "";
          styleSel.style.display = "none";
          usageEl.style.display = "block";
          usageEl.textContent = "Loading budget…";
          modelSel.innerHTML = Object.entries(_CF_MODELS).map(([k,v]) => `<option value="${k}">${v}</option>`).join("");
          try {
            const sRes = await fetch("/api/settings/cloudflare/status");
            const sData = await sRes.json();
            if (sData.available) {
              const b = sData.budget || {};
              usageEl.textContent = `☁️ ~${b.estimated_images_per_day || "?"} images/day free · resets UTC midnight`;
            } else {
              usageEl.textContent = "⚠️ Add Cloudflare credentials in Settings → Providers first";
            }
          } catch { usageEl.textContent = "⚠️ Could not fetch Cloudflare status"; }
        } else if (prov === "pollinations") {
          modelSel.style.display = "";
          styleSel.style.display = "none";
          usageEl.style.display = "none";
          modelSel.innerHTML = Object.entries(_IG_MODELS).map(([k,v]) => `<option value="${k}">${v}</option>`).join("");
        } else if (prov === "puter") {
          modelSel.style.display = "";
          styleSel.style.display = "none";
          usageEl.style.display = "block";
          usageEl.textContent = "Loading credits…";
          const models = await _loadPuterModels();
          modelSel.innerHTML = Object.entries(models).map(([k,v]) => `<option value="${k}">${v}</option>`).join("");
          try {
            const uRes = await fetch("/api/settings/puter/usage");
            const uData = await uRes.json();
            if (uData.ok) {
              usageEl.textContent = `💳 ${uData.remaining ?? '?'} / ${uData.allowance ?? '?'} credits remaining`;
            } else {
              usageEl.textContent = "⚠️ Add a Puter token in Settings → Providers first";
            }
          } catch { usageEl.textContent = "⚠️ Could not fetch credit info"; }
        } else if (prov === "perchance") {
          // Perchance blocks iframes — show launch card to open in new tab
          modelSel.style.display = "none";
          styleSel.style.display = "none";
          usageEl.style.display = "none";
          wrap.querySelector("#igShape").style.display = "none";
          wrap.querySelector("#igCount").style.display = "none";
          wrap.querySelector("#igNegPrompt").style.display = "none";
          genBtn.style.display = "none";
          promptEl.style.display = "none";
          outputWrap.style.display = "block";
          gallery.style.gridTemplateColumns = "1fr";
          const promptText = promptEl.value.trim();
          const launchUrl = promptText
            ? "https://perchance.org/ai-text-to-image-generator#" + encodeURIComponent(promptText)
            : "https://perchance.org/ai-text-to-image-generator";
          gallery.innerHTML = `
            <div style="display:flex;flex-direction:column;align-items:center;justify-content:center;padding:40px 20px;text-align:center;background:var(--surface);border-radius:12px;border:1px solid var(--border);">
              <div style="font-size:48px;margin-bottom:16px;">🎨</div>
              <div style="font-size:18px;font-weight:600;margin-bottom:8px;color:var(--text);">Perchance AI Image Generator</div>
              <div style="font-size:13px;color:var(--text);opacity:0.7;margin-bottom:20px;">Perchance blocks embedded iframes for security.<br>Click below to open it in a new tab.</div>
              <a href="${launchUrl}" target="_blank" rel="noopener" class="promptgen-start-btn" style="text-decoration:none;display:inline-flex;align-items:center;gap:8px;">🚀 Open Perchance</a>
              ${promptText ? '<div style="margin-top:12px;font-size:11px;color:var(--text);opacity:0.5;">Your prompt will be pre-filled via URL</div>' : ''}
            </div>`;
          metaEl.textContent = "";
          return;
        }
        // Restore controls when switching away from perchance
        genBtn.style.display = "";
        promptEl.style.display = "";
        wrap.querySelector("#igShape").style.display = "";
        wrap.querySelector("#igCount").style.display = "";
        wrap.querySelector("#igNegPrompt").style.display = "";
      }
      providerSel.addEventListener("change", updateProviderUI);
      updateProviderUI();

      promptEl.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); genBtn.click(); }
      });

      genBtn.addEventListener("click", async () => {
        const prompt = promptEl.value.trim();
        if (!prompt) { showToast("⚠️ Enter a prompt first", "error"); return; }

        const provider = providerSel.value;
        const shape = wrap.querySelector("#igShape").value;
        const count = wrap.querySelector("#igCount").value;
        const neg = wrap.querySelector("#igNegPrompt").value.trim();

        genBtn.disabled = true;
        genBtn.textContent = "⏳ Generating…";
        outputWrap.style.display = "none";
        gallery.innerHTML = "";
        metaEl.textContent = "";



        try {
          const attrs = { prompt, shape, count, provider };
          if (provider === "cloudflare" || provider === "pollinations" || provider === "puter") {
            attrs.model = modelSel.value;
          } else {
            attrs.style = styleSel.value;
          }
          if (neg) attrs.negative_prompt = neg;

          const res = await fetch("/api/tool/generate_image", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(attrs),
          });

          if (!res.ok) throw new Error("HTTP " + res.status);
          const result = await res.json();

          if (result.ok && result.images && result.images.length > 0) {
            // Responsive grid: 1=full, 2=half, 3-4=two per row
            const n = result.images.length;
            const cols = n === 1 ? 1 : n === 2 ? 2 : 2;
            gallery.style.gridTemplateColumns = `repeat(${cols}, 1fr)`;
            gallery.innerHTML = ""; // clear previous

            for (const img of result.images) {
              const imgUrl = "/assets/" + img.filename;
              const cell = document.createElement("div");
              cell.className = "library-gallery-item";
              cell.innerHTML = `<img src="${imgUrl}" alt="Generated image" loading="lazy">`;
              cell.title = `Seed: ${img.seed} | ${img.width}x${img.height}`;
              cell.addEventListener("click", () => window.open(imgUrl, "_blank"));
              gallery.appendChild(cell);
            }

            const first = result.images[0];
            const totalSize = result.images.reduce((s, i) => s + (i.size_bytes || 0), 0);
            const detailLabel = (provider === "cloudflare" || provider === "pollinations" || provider === "puter") ? `Model: ${modelSel.options[modelSel.selectedIndex]?.text || modelSel.value}` : `Style: ${styleSel.value}`;
            metaEl.textContent = `✅ ${result.count} image(s) | ${detailLabel} | Shape: ${shape} | Seed: ${first.seed} | ${first.width}x${first.height} | ${(totalSize / 1024).toFixed(0)}KB total`;
            if (result.errors) metaEl.textContent += `\n⚠️ Partial: ${result.errors.join("; ")}`;
            outputWrap.style.display = "block";
            showToast(`✅ ${result.count} image(s) generated!`, "success");
          } else {
            metaEl.textContent = "❌ " + (result.error || "Unknown error");
            outputWrap.style.display = "block";
            showToast("❌ Generation failed", "error");
          }
        } catch (e) {
          metaEl.textContent = "❌ " + e.message;
          outputWrap.style.display = "block";
          showToast("❌ " + e.message, "error");
        } finally {
          genBtn.disabled = false;
          genBtn.textContent = "🖼️ Generate";
        }
      });
    }

    async function renderPromptGenPanel(container) {
      container.innerHTML = "";
      container.classList.add("promptgen-panel");

      // ── Launch card ──
      const launch = document.createElement("div");
      launch.className = "promptgen-launch";
      launch.innerHTML = `
        <div class="promptgen-launch-head">
          <span class="promptgen-launch-title"><i data-lucide="sparkles" class="icon-lucide"></i> Prompt Generator</span>
          <span class="promptgen-launch-sub">Craft optimized prompts for any LLM task</span>
        </div>
        <textarea id="pgQuery" class="promptgen-query" rows="3"
          placeholder="e.g. Write a Python script that scrapes product prices from Amazon and saves to CSV…"></textarea>
        <div class="promptgen-controls">
          <span class="promptgen-spacer"></span>
          <button id="pgStartBtn" class="promptgen-start-btn">✨ Generate Prompt</button>
        </div>
        <div class="promptgen-controls promptgen-controls-fallback">
          <div class="promptgen-fallback-col">
            <div class="promptgen-fallback-label"><i data-lucide="cpu" class="icon-lucide"></i> Model <span class="promptgen-hint">top = 1st choice, then fallbacks</span></div>
            <select id="pgModel1" class="promptgen-select"></select>
            <select id="pgModel2" class="promptgen-select"></select>
            <select id="pgModel3" class="promptgen-select"></select>
          </div>
          <div class="promptgen-fallback-col">
            <div class="promptgen-fallback-label"><i data-lucide="globe" class="icon-lucide"></i> Browser Data <span class="promptgen-hint">top = 1st choice, then fallbacks</span></div>
            <select id="pgAccount1" class="promptgen-select"></select>
            <select id="pgAccount2" class="promptgen-select"></select>
            <select id="pgAccount3" class="promptgen-select"></select>
          </div>
        </div>
        <div id="pgOutputWrap" class="promptgen-output-wrap" style="display:none;">
          <div class="promptgen-output-head">
            <span class="promptgen-output-label">Generated Prompt</span>
          </div>
          <div id="pgOutput" class="promptgen-output"></div>
          <div class="promptgen-output-actions">
            <button id="pgCopyBtn" class="promptgen-action-btn"><i data-lucide="copy" style="width:13px;height:13px;display:inline;vertical-align:-2px;margin-right:4px;"></i> Copy</button>
            <button id="pgUseBtn" class="promptgen-action-btn"><i data-lucide="message-square" style="width:13px;height:13px;display:inline;vertical-align:-2px;margin-right:4px;"></i> Use in Chat</button>
          </div>
        </div>
      `;
      container.appendChild(launch);
      if (window.lucide) lucide.createIcons({ nodes: launch.querySelectorAll("[data-lucide]") });

      // ── Populate selectors ──
      const allModels = ((typeof modelList !== "undefined" && Array.isArray(modelList)) ? modelList : [])
        .map((m) => ({ value: m.id, label: m.name || m.label || m.id }));

      _fillPgSlot(launch.querySelector("#pgModel1"), allModels, null, allModels[0]?.value || "");
      _fillPgSlot(launch.querySelector("#pgModel2"), allModels, "— no 2nd model —", allModels[1]?.value || "");
      _fillPgSlot(launch.querySelector("#pgModel3"), allModels, "— no 3rd model —", allModels[2]?.value || "");

      let accounts = [], active = "";
      try {
        const data = await fetch("/api/settings/accounts").then((r) => r.json());
        accounts = ((data && data.accounts) || []).map((a) => ({
          value: a.name, label: a.email ? a.name + " (" + a.email + ")" : a.name,
        }));
        active = (data && data.active) || "";
      } catch {}
      const primary = accounts.some((a) => a.value === active) ? active : (accounts[0]?.value || "");
      _fillPgSlot(launch.querySelector("#pgAccount1"), accounts, accounts.length ? null : "Default (active account)", primary);
      _fillPgSlot(launch.querySelector("#pgAccount2"), accounts, "— no 2nd account —", "");
      _fillPgSlot(launch.querySelector("#pgAccount3"), accounts, "— no 3rd account —", "");

      // ── Wire up ──
      const startBtn = launch.querySelector("#pgStartBtn");
      const queryEl = launch.querySelector("#pgQuery");
      const outputWrap = launch.querySelector("#pgOutputWrap");
      const outputEl = launch.querySelector("#pgOutput");
      const copyBtn = launch.querySelector("#pgCopyBtn");
      const useBtn = launch.querySelector("#pgUseBtn");

      queryEl.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); startBtn.click(); }
      });

      startBtn.addEventListener("click", async () => {
        const task = queryEl.value.trim();
        if (!task) { showToast("⚠️ Describe a task first", "error"); return; }

        const _ordered = (...vals) => vals.filter(Boolean).filter((v, i, a) => a.indexOf(v) === i);
        const models = _ordered(
          launch.querySelector("#pgModel1")?.value,
          launch.querySelector("#pgModel2")?.value,
          launch.querySelector("#pgModel3")?.value,
        );
        const browserAccounts = _ordered(
          launch.querySelector("#pgAccount1")?.value,
          launch.querySelector("#pgAccount2")?.value,
          launch.querySelector("#pgAccount3")?.value,
        );
        const primaryModel = models[0] || (allModels[0]?.value || "qwen3.8-max-preview");

        startBtn.disabled = true;
        startBtn.textContent = "Generating…";
        outputWrap.style.display = "none";

        // Load prompt generator instruction from file (live, not hardcoded)
        let pgInstruction = "";
        try {
          const instrRes = await fetch("/api/instruction/prompt_generator.md");
          if (instrRes.ok) {
            const instrData = await instrRes.json();
            pgInstruction = instrData.content || "";
          }
        } catch {}
        if (!pgInstruction) {
          pgInstruction = "You are an expert prompt engineer. Generate a detailed, optimized prompt for the user's task. Output only the final prompt.";
        }

        const systemPrompt = `${pgInstruction}

Configuration context (for awareness only, never mention in output):
- Models: ${models.join(", ") || "default"}
- Browser Data: ${browserAccounts.join(", ") || "default"}

Generate the specification prompt for this task:
${task}`;

        try {
          const res = await fetch("/api/chat", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ message: systemPrompt, model: primaryModel, chat_id: null }),
          });
          if (!res.ok) throw new Error("HTTP " + res.status);

          const reader = res.body.getReader();
          const decoder = new TextDecoder();
          let fullText = "", buffer = "";
          while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split("\n");
            buffer = lines.pop() || "";
            for (const line of lines) {
              if (line.startsWith("data: ")) {
                try {
                  const evt = JSON.parse(line.slice(6));
                  if (evt.type === "answer" && evt.text) fullText += evt.text;
                } catch {}
              }
            }
          }

          let cleaned = fullText.trim();
          if (cleaned.startsWith("```")) {
            cleaned = cleaned.replace(/^```[a-z]*\n?/, "").replace(/\n?```$/, "").trim();
          }

          outputEl.textContent = cleaned;
          outputWrap.style.display = "block";
          showToast("✅ Prompt generated", "success");

          // Auto-save to ~/sable_output/prompts/
          try {
            const ts = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
            const slug = task.slice(0, 40).replace(/[^a-zA-Z0-9]+/g, "_").replace(/^_|_$/g, "").toLowerCase();
            const filename = `${ts}_${slug || "prompt"}.md`;
            const savePath = `/home/sifat/sable_output/prompts/${filename}`;
            const meta = `---\ngenerated: ${new Date().toISOString()}\nmodel: ${primaryModel}\ntask: ${task.slice(0, 200)}\n---\n\n`;
            await fetch("/api/filesystem/write", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ path: savePath, content: meta + cleaned }),
            });
          } catch {}
        } catch (e) {
          showToast("❌ Generation failed: " + e.message, "error");
        } finally {
          startBtn.disabled = false;
          startBtn.textContent = "✨ Generate Prompt";
        }
      });

      copyBtn.addEventListener("click", () => {
        const text = outputEl.textContent;
        if (!text) return;
        const originalHTML = copyBtn.innerHTML;
        navigator.clipboard.writeText(text).then(
          () => {
            copyBtn.innerHTML = '<i data-lucide="check" style="width:13px;height:13px;display:inline;vertical-align:-2px;margin-right:4px;"></i> Copied';
            copyBtn.classList.add("promptgen-copy-success");
            if (window.lucide) lucide.createIcons({ nodes: [copyBtn] });
            setTimeout(() => {
              copyBtn.innerHTML = originalHTML;
              copyBtn.classList.remove("promptgen-copy-success");
              if (window.lucide) lucide.createIcons({ nodes: [copyBtn] });
            }, 2000);
          },
          () => {
            copyBtn.innerHTML = '<i data-lucide="x" style="width:13px;height:13px;display:inline;vertical-align:-2px;margin-right:4px;"></i> Failed';
            if (window.lucide) lucide.createIcons({ nodes: [copyBtn] });
            setTimeout(() => {
              copyBtn.innerHTML = originalHTML;
              if (window.lucide) lucide.createIcons({ nodes: [copyBtn] });
            }, 2000);
          }
        );
      });

      useBtn.addEventListener("click", () => {
        const text = outputEl.textContent;
        if (!text) return;
        closeLibrary();
        const chatInput = document.getElementById("chatInput");
        if (chatInput) {
          chatInput.value = text;
          chatInput.focus();
          chatInput.dispatchEvent(new Event("input", { bubbles: true }));
        }
      });

      // ── Saved Prompts (inline, like Research tab) ──
      const savedWrap = document.createElement("div");
      savedWrap.className = "research-library";
      savedWrap.innerHTML = `
        <div class="research-lib-head"><i data-lucide="file-text" style="width:14px;height:14px;display:inline;vertical-align:middle;margin-right:4px;"></i> Saved Prompts</div>
        <div id="pgSavedList" class="library-loading">Loading…</div>
      `;
      container.appendChild(savedWrap);
      if (window.lucide) lucide.createIcons({ nodes: savedWrap.querySelectorAll("[data-lucide]") });

      // Load saved prompts
      try {
        const res = await fetch("/api/library/prompts");
        const items = await res.json();
        const list = savedWrap.querySelector("#pgSavedList");
        if (!items.length) {
          list.innerHTML = '<div class="library-empty">No saved prompts yet. Generate one above.</div>';
        } else {
          list.innerHTML = "";
          const grid = document.createElement("div");
          grid.className = "library-card-grid";
          items.forEach((item) => {
            const card = document.createElement("div");
            card.className = "library-card";
            card.innerHTML = `
              <div class="library-card-title">${escHtml(item.title)}</div>
              <div class="library-card-date">${item.date || ""}</div>
              <div class="library-card-preview">${escHtml(item.preview || "")}</div>
            `;
            card.addEventListener("click", () => openLibraryReader("prompts", item.filename, item.title));
            grid.appendChild(card);
          });
          list.appendChild(grid);
          if (window.lucide) lucide.createIcons({ nodes: grid.querySelectorAll("[data-lucide]") });
        }
      } catch {
        const list = savedWrap.querySelector("#pgSavedList");
        if (list) list.innerHTML = '<div class="library-empty">Failed to load saved prompts.</div>';
      }
    }


