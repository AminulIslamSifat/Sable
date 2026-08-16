    /* ---------- Library Panel ---------- */
    const libraryOverlay = document.getElementById("libraryOverlay");
    const libraryBtn = document.getElementById("libraryBtn");
    const libraryClose = document.getElementById("libraryClose");
    const libraryTabs = document.getElementById("libraryTabs");
    const libraryBody = document.getElementById("libraryBody");
    let _libLoaded = { agents: false, research: false, notes: false, gallery: false, skills: false, promptgen: false };

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

        const systemPrompt = `You are a prompt generator. Your ONLY job is to read the user's task description and output ONE clean, ready-to-use prompt.

Rules:
- Output NOTHING except the final prompt itself.
- No explanations, no preamble, no markdown fences, no labels.
- Do NOT repeat or rephrase the user's instructions back at them.
- Do NOT add meta-commentary like "Here is your prompt" or "As requested".
- The output must be directly usable as-is when pasted into an LLM or image generator.
- If the user asks for an image prompt, write a vivid descriptive image generation prompt.
- If the user asks for a coding prompt, write a precise technical prompt with constraints.
- Adapt tone, structure, and detail level to match the task domain.

Configuration context (for awareness only, do not mention in output):
- Models: ${models.join(", ") || "default"}
- Browser Data: ${browserAccounts.join(", ") || "default"}

Now generate the prompt for this task:
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
        navigator.clipboard.writeText(text).then(
          () => showToast("📋 Copied to clipboard", "success"),
          () => showToast("❌ Copy failed", "error")
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
    }


