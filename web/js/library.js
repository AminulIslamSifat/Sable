    /* ---------- Library Panel ---------- */
    const libraryOverlay = document.getElementById("libraryOverlay");
    const libraryBtn = document.getElementById("libraryBtn");
    const libraryClose = document.getElementById("libraryClose");
    const libraryTabs = document.getElementById("libraryTabs");
    const libraryBody = document.getElementById("libraryBody");
    let _libLoaded = { agents: false, research: false, notes: false, gallery: false, skills: false };

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

