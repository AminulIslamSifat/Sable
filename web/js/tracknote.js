    // ---------- file edit sidebar ----------
    const diffSidebarEl = document.getElementById("diffSidebar");
    const diffCardsEl = document.getElementById("diffCards");
    const diffCloseBtn = document.getElementById("diffClose");
    const diffClearBtn = document.getElementById("diffClear");
    const diffToggleBtn = document.getElementById("diffToggleBtn");
    const MAX_DIFF_CARDS = 12;

    if (diffCloseBtn) diffCloseBtn.addEventListener("click", () => {
      if (_libReaderDocked && _libReaderTempHidden) {
        // File viewer was temporarily shown — close it, restore markdown reader
        _restoreLibReaderContent();
        document.body.classList.remove("diff-open");
      } else if (_libReaderDocked) {
        undockLibraryReader();
      } else {
        document.body.classList.remove("diff-open");
      }
    });
    if (diffClearBtn) diffClearBtn.addEventListener("click", () => { if (diffCardsEl) diffCardsEl.innerHTML = ""; });
    if (diffToggleBtn) diffToggleBtn.addEventListener("click", () => {
      if (_libReaderDocked && !_libReaderTempHidden) {
        // Temporarily show file viewer, hide markdown reader
        _tempShowFileViewer();
      } else if (_libReaderDocked && _libReaderTempHidden) {
        // Restore markdown reader
        _restoreLibReaderContent();
      } else {
        const opening = !document.body.classList.contains("diff-open");
        document.body.classList.toggle("diff-open");
        if (opening) {
          // Close notes/todo/tasks panels if open in sidebar
          if (window.sidebarHost) {
            const cur = window.sidebarHost.getCurrent();
            if (cur === 'notes' || cur === 'todo' || cur === 'tasks') window.sidebarHost.closePanel(cur);
          }
          if (typeof AgentPanel !== "undefined") AgentPanel.close();
        }
      }
    });

    // ---------- Notes, Todo & Tasks: register as left-sidebar hostable panels ----------
    const trackNoteBtn = document.getElementById("trackNoteBtn");

    function closeOtherPanels() {
      document.body.classList.remove("diff-open");
      document.body.classList.remove("calendar-open");
      const calView = document.getElementById("calendarView");
      if (calView) calView.classList.add("hidden");
      if (typeof AgentPanel !== "undefined") AgentPanel.close();
    }

    // Register notes panel with sidebar host — always left sidebar
    if (window.sidebarHost) {
      window.sidebarHost.savePosition('notes', 'left');
      window.sidebarHost.register('notes', {
        panelId: 'notesPanel',
        onOpen: (el) => {
          closeOtherPanels();
          loadNotesOnly();
        },
        onClose: () => {},
      });

      window.sidebarHost.savePosition('todo', 'left');
      window.sidebarHost.register('todo', {
        panelId: 'todoPanel',
        onOpen: (el) => {
          closeOtherPanels();
          loadTodos();
        },
        onClose: () => {},
      });

      window.sidebarHost.savePosition('tasks', 'left');
      window.sidebarHost.register('tasks', {
        panelId: 'tasksPanel',
        onOpen: (el) => {
          closeOtherPanels();
          loadAgentOps();
        },
        onClose: () => {},
      });
    }

    // Legacy footer button — opens todo in left sidebar
    if (trackNoteBtn) trackNoteBtn.addEventListener("click", () => {
      if (window.sidebarHost) {
        const current = window.sidebarHost.getCurrent();
        if (current === 'todo') {
          window.sidebarHost.closePanel('todo');
        } else {
          window.sidebarHost.openPanel('todo');
        }
      }
    });


    // ---------- TrackNote: API helpers ----------
    function esc(s) { const d = document.createElement("div"); d.textContent = s || ""; return d.innerHTML; }
    const TN_API = "/api";
    async function tnFetch(path) {
      const r = await fetch(TN_API + path);
      if (!r.ok) throw new Error(r.statusText);
      return r.json();
    }
    async function tnPost(path, body) {
      const r = await fetch(TN_API + path, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
      if (!r.ok) throw new Error(r.statusText);
      return r.json();
    }
    async function tnPut(path, body) {
      const r = await fetch(TN_API + path, { method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
      if (!r.ok) throw new Error(r.statusText);
      return r.json();
    }
    async function tnDelete(path) {
      const r = await fetch(TN_API + path, { method: "DELETE" });
      if (!r.ok) throw new Error(r.statusText);
      return r.json();
    }

    // ---------- Notes panel (filesystem: sable_output/notes/) ----------
    const NOTES_DIR = "/home/sifat/sable_output/notes";
    const notesListEl = document.getElementById("notesList");
    const notesEmptyEl = document.getElementById("notesEmpty");

    function noteNameFromPath(p) {
      const name = p.split("/").pop() || "Untitled";
      return name.replace(/\.md$/, "");
    }

    function renderNoteFileCard(item) {
      const div = document.createElement("div");
      div.className = "tn-item tn-note-card";
      const title = item.title || noteNameFromPath(item.filename || "Untitled");
      const preview = item.preview || "";
      const dateStr = item.date || "";
      let html = `<div class="tn-item-title">${esc(title)}</div>`;
      if (preview) html += `<div class="tn-item-preview">${esc(preview)}</div>`;
      html += `<div class="tn-item-meta">${dateStr}</div>`;
      html += `<div class="tn-item-actions">`;
      const filePath = NOTES_DIR + "/" + (item.filename || "");
      html += `<button class="tn-item-action" data-fedit="${esc(filePath)}" title="Edit"><i data-lucide="pencil" class="icon-lucide"></i></button>`;
      html += `<button class="tn-item-action danger" data-fdel="${esc(filePath)}" title="Delete"><i data-lucide="x" class="icon-lucide"></i></button>`;
      html += `</div>`;
      div.innerHTML = html;
      // Click card to open library reader popup (same as library notes)
      div.addEventListener("click", (e) => {
        if (e.target.closest(".tn-item-action")) return;
        if (typeof window.openLibraryReader === "function") {
          window.openLibraryReader("notes", item.filename, title);
        }
      });
      div.querySelector("[data-fdel]").addEventListener("click", async (e) => {
        e.stopPropagation();
        if (!confirm(`Delete "${title}"?`)) return;
        await fetch("/api/filesystem/delete", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ path: filePath }) });
        loadNotesOnly();
      });
      div.querySelector("[data-fedit]").addEventListener("click", (e) => {
        e.stopPropagation();
        openFileNoteEditModal(filePath, title);
      });
      return div;
    }

    async function loadNotesOnly() {
      try {
        const r = await fetch("/api/library/notes");
        const items = await r.json();
        if (!notesListEl) return;
        notesListEl.innerHTML = "";
        const notes = Array.isArray(items) ? items : [];
        notes.forEach(item => notesListEl.appendChild(renderNoteFileCard(item)));
        if (notesEmptyEl) notesEmptyEl.style.display = notes.length ? "none" : "block";
        if (window.lucide) lucide.createIcons({ nodes: notesListEl.querySelectorAll("[data-lucide]") });
      } catch(e) { console.warn("loadNotesOnly failed", e); }
    }

    async function openFileNoteEditModal(filePath, title) {
      const overlay = ensureTnEditModal();
      const body = overlay.querySelector(".tn-edit-body");
      const titleEl = overlay.querySelector(".tn-edit-title");
      const saveBtn = overlay.querySelector(".tn-edit-save");
      body.innerHTML = "";
      overlay.style.display = "flex";
      titleEl.textContent = "Edit Note: " + title;
      let content = "";
      try {
        const r = await fetch(`/api/filesystem/read?path=${encodeURIComponent(filePath)}`);
        const d = await r.json();
        content = d.content || "";
      } catch(e) { content = ""; }
      body.innerHTML = `<label>Filename<input type="text" id="tnEditFileName" value="${esc(filePath.split('/').pop())}" /></label><label>Content<textarea id="tnEditFileContent" rows="12" style="font-family:monospace;font-size:13px;">${esc(content)}</textarea></label>`;
      saveBtn.onclick = async () => {
        const newFileName = document.getElementById("tnEditFileName").value.trim();
        const newContent = document.getElementById("tnEditFileContent").value;
        if (!newFileName) return;
        const dir = filePath.substring(0, filePath.lastIndexOf("/"));
        const newPath = dir + "/" + newFileName;
        await fetch("/api/filesystem/write", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ path: newPath, content: newContent }) });
        if (newPath !== filePath) {
          await fetch("/api/filesystem/delete", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ path: filePath }) });
        }
        closeTnEditModal();
        loadNotesOnly();
      };
    }

    // ---------- Todo panel (note_type=checklist only) ----------
    const tnTodoList = document.getElementById("tnTodoList");
    const tnTodoEmpty = document.getElementById("tnTodoEmpty");

    function renderTodoItem(n) {
      const div = document.createElement("div");
      div.className = "tn-item";
      let html = `<div class="tn-item-title">${esc(n.title || "Untitled")}</div>`;
      if (n.content) html += `<div class="tn-item-meta">${esc(n.content).slice(0, 120)}</div>`;
      if (n.due_date) html += `<div class="tn-item-meta">Due: ${esc(n.due_date)}</div>`;
      // Checklist items
      if (n.items && n.items.length) {
        html += `<div class="tn-checklist">`;
        n.items.forEach((item, i) => {
          const doneClass = item.done ? " done" : "";
          html += `<div class="tn-checklist-item${doneClass}"><input type="checkbox" ${item.done ? "checked" : ""} data-note="${n.id}" data-idx="${i}" /><span>${esc(item.text || "")}</span></div>`;
        });
        html += `</div>`;
      }
      html += `<div class="tn-item-actions"><button class="tn-item-action" data-edit="${n.id}" title="Edit"><i data-lucide="pencil" class="icon-lucide"></i></button><button class="tn-item-action danger" data-del="${n.id}" title="Delete"><i data-lucide="x" class="icon-lucide"></i></button></div>`;
      div.innerHTML = html;
      div.querySelector("[data-del]").addEventListener("click", async () => {
        await tnDelete("/notes/" + n.id);
        loadTodos();
      });
      div.querySelector("[data-edit]").addEventListener("click", () => {
        openTnEditModal("note", n);
      });
      div.querySelectorAll("input[type=checkbox][data-note]").forEach(cb => {
        cb.addEventListener("change", async () => {
          await tnPost("/notes/" + cb.dataset.note + "/toggle-item?index=" + cb.dataset.idx);
          loadTodos();
        });
      });
      return div;
    }

    async function loadTodos() {
      try {
        const data = await tnFetch("/notes?note_type=checklist");
        if (!tnTodoList) return;
        tnTodoList.innerHTML = "";
        const items = data.notes || [];
        items.forEach(n => tnTodoList.appendChild(renderTodoItem(n)));
        if (tnTodoEmpty) tnTodoEmpty.style.display = items.length ? "none" : "block";
        if (window.lucide) lucide.createIcons({ nodes: tnTodoList.querySelectorAll("[data-lucide]") });
      } catch(e) { console.warn("loadTodos failed", e); }
    }

    // ---------- Add form toggle (+ button) ----------
    function setupAddToggle(toggleId, formId) {
      const toggle = document.getElementById(toggleId);
      const form = document.getElementById(formId);
      if (!toggle || !form) return;
      toggle.addEventListener("click", () => {
        const isHidden = form.classList.contains("hidden");
        form.classList.toggle("hidden", !isHidden);
        toggle.classList.toggle("active", isHidden);
        if (isHidden) {
          const firstInput = form.querySelector("input, textarea");
          if (firstInput) requestAnimationFrame(() => firstInput.focus());
        }
      });
    }
    setupAddToggle("notesAddToggle", "notesAddForm");
    setupAddToggle("tnAddToggle", "tnAddForm");
    setupAddToggle("tnAgentAddToggle", "tnAgentForm");

    // Helper: collapse add form after successful add
    function collapseAddForm(toggleId, formId) {
      const toggle = document.getElementById(toggleId);
      const form = document.getElementById(formId);
      if (form) form.classList.add("hidden");
      if (toggle) toggle.classList.remove("active");
    }

    // Notes panel add handler (filesystem)
    document.getElementById("notesAddBtn")?.addEventListener("click", async () => {
      const title = document.getElementById("notesTitle").value.trim();
      const content = document.getElementById("notesContent").value.trim();
      if (!title && !content) return;
      const fileName = (title || "untitled").replace(/[^a-zA-Z0-9_\-. ]/g, "_") + ".md";
      const filePath = NOTES_DIR + "/" + fileName;
      await fetch("/api/filesystem/write", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ path: filePath, content: content || "" })
      });
      document.getElementById("notesTitle").value = "";
      document.getElementById("notesContent").value = "";
      collapseAddForm("notesAddToggle", "notesAddForm");
      loadNotesOnly();
    });

    // Todo panel add handler
    document.getElementById("tnTodoAdd")?.addEventListener("click", async () => {
      const title = document.getElementById("tnNoteTitle").value.trim();
      const content = document.getElementById("tnNoteContent").value.trim();
      if (!title && !content) return;
      const firstItem = content || "New item";
      await tnPost("/notes", { title: title || "Untitled", note_type: "checklist", items: [{ text: firstItem, done: false }] });
      document.getElementById("tnNoteTitle").value = "";
      document.getElementById("tnNoteContent").value = "";
      collapseAddForm("tnAddToggle", "tnAddForm");
      loadTodos();
    });

    // ---------- TrackNote: Agent Ops panel ----------
    const tnAgentList = document.getElementById("tnAgentList");
    const tnAgentEmpty = document.getElementById("tnAgentEmpty");
    const tnAgentModelSel = document.getElementById("tnAgentModel");

    // Populate model dropdown from global MODELS if available
    function populateAgentModels() {
      if (!tnAgentModelSel) return;
      const models = (typeof window.SABLE_MODELS !== "undefined" ? window.SABLE_MODELS : null) || [
        { id: "qwen3.7-max", label: "Qwen3.7 Max" },
        { id: "qwen3.8-max", label: "Qwen3.8 Max" },
        { id: "deepseek-expert", label: "DeepSeek Expert" },
      ];
      tnAgentModelSel.innerHTML = "";
      models.forEach(m => {
        const opt = document.createElement("option");
        opt.value = m.id; opt.textContent = m.label || m.id;
        tnAgentModelSel.appendChild(opt);
      });
    }
    populateAgentModels();

    function renderAgentOp(op) {
      const div = document.createElement("div");
      div.className = "tn-item";
      const statusClass = op.enabled ? "on" : "off";
      const tStr = op.schedule_time || "";
      const schedInfo = op.schedule_type === "cron" ? `Cron: ${op.cron_expression || "?"}` : `${op.schedule_type}${tStr ? " " + tStr : ""}`;
      const lastRun = op.last_run ? new Date(op.last_run).toLocaleString() : "never";
      const toggleIcon = op.enabled ? "pause" : "play";
      div.innerHTML = `<div class="tn-item-title"><span class="tn-agent-status ${statusClass}"></span>${esc(op.name)}</div><div class="tn-item-meta">${schedInfo} · Model: ${esc(op.model)} · Last: ${lastRun}</div><div class="tn-item-meta" style="margin-top:4px;opacity:.7">${esc(op.prompt).slice(0, 150)}${op.prompt.length > 150 ? "…" : ""}</div><div class="tn-item-actions"><button class="tn-item-action" data-toggle="${op.id}" title="Toggle"><i data-lucide="${toggleIcon}" class="icon-lucide"></i></button><button class="tn-item-action" data-edit="${op.id}" title="Edit"><i data-lucide="pencil" class="icon-lucide"></i></button><button class="tn-item-action danger" data-del="${op.id}" title="Delete"><i data-lucide="x" class="icon-lucide"></i></button></div>`;
      div.querySelector("[data-toggle]").addEventListener("click", async () => {
        await tnPut("/agent-ops/" + op.id, { enabled: op.enabled ? 0 : 1 });
        loadAgentOps();
      });
      div.querySelector("[data-del]").addEventListener("click", async () => {
        await tnDelete("/agent-ops/" + op.id);
        loadAgentOps();
      });
      div.querySelector("[data-edit]").addEventListener("click", () => {
        openTnEditModal("agent-op", op);
      });
      return div;
    }

    async function loadAgentOps() {
      try {
        const data = await tnFetch("/agent-ops");
        console.log("[TrackNote] loadAgentOps response:", data);
        if (!tnAgentList) { console.error("[TrackNote] tnAgentList is null!"); return; }
        tnAgentList.innerHTML = "";
        const items = data.ops || [];
        console.log(`[TrackNote] Rendering ${items.length} agent ops`);
        items.forEach(op => tnAgentList.appendChild(renderAgentOp(op)));
        tnAgentEmpty.style.display = items.length ? "none" : "block";
        if (window.lucide) lucide.createIcons({ nodes: tnAgentList.querySelectorAll("[data-lucide]") });
      } catch(e) { console.error("[TrackNote] loadAgentOps FAILED:", e); }
    }

    document.getElementById("tnAgentAdd")?.addEventListener("click", async () => {
      const name = document.getElementById("tnAgentName").value.trim();
      const prompt = document.getElementById("tnAgentPrompt").value.trim();
      console.log("[TrackNote] Add clicked:", { name, prompt });
      if (!name || !prompt) { console.warn("[TrackNote] Missing name or prompt"); return; }
      const model = tnAgentModelSel?.value || "qwen3.7-max";
      const stype = document.getElementById("tnAgentSchedType").value;
      const time = document.getElementById("tnAgentTime").value || null;
      try {
        const result = await tnPost("/agent-ops", { name, prompt, model, schedule_type: stype, schedule_time: time });
        console.log("[TrackNote] Add result:", result);
        document.getElementById("tnAgentName").value = "";
        document.getElementById("tnAgentPrompt").value = "";
        collapseAddForm("tnAgentAddToggle", "tnAgentForm");
        loadAgentOps();
      } catch(e) { console.error("[TrackNote] Add FAILED:", e); }
    });

    // ---------- TrackNote: Edit Modal ----------
    let tnEditOverlay = null;

    function ensureTnEditModal() {
      if (tnEditOverlay) return tnEditOverlay;
      tnEditOverlay = document.createElement("div");
      tnEditOverlay.className = "tn-edit-overlay";
      tnEditOverlay.innerHTML = `<div class="tn-edit-modal"><div class="tn-edit-header"><span class="tn-edit-title">Edit</span><button class="tn-edit-close" title="Close">✕</button></div><div class="tn-edit-body"></div><div class="tn-edit-footer"><button class="tn-edit-cancel">Cancel</button><button class="tn-edit-save">Save</button></div></div>`;
      document.body.appendChild(tnEditOverlay);
      tnEditOverlay.querySelector(".tn-edit-close").addEventListener("click", closeTnEditModal);
      tnEditOverlay.querySelector(".tn-edit-cancel").addEventListener("click", closeTnEditModal);
      tnEditOverlay.addEventListener("click", (e) => { if (e.target === tnEditOverlay) closeTnEditModal(); });
      return tnEditOverlay;
    }

    function closeTnEditModal() {
      if (tnEditOverlay) tnEditOverlay.style.display = "none";
    }

    function openTnEditModal(type, item) {
      const overlay = ensureTnEditModal();
      const body = overlay.querySelector(".tn-edit-body");
      const titleEl = overlay.querySelector(".tn-edit-title");
      const saveBtn = overlay.querySelector(".tn-edit-save");
      body.innerHTML = "";
      overlay.style.display = "flex";

      if (type === "note") {
        titleEl.textContent = "Edit Note / Todo";
        const isChecklist = item.items && item.items.length > 0;
        let itemsHtml = "";
        if (isChecklist) {
          itemsHtml = `<label>Checklist Items (one per line)<textarea id="tnEditItems" rows="5">${(item.items||[]).map(i=>`${i.done?"[x] ":""}${i.text}`).join("\n")}</textarea></label>`;
        }
        body.innerHTML = `<label>Title<input type="text" id="tnEditTitle" value="${esc(item.title||"")}" /></label><label>Content<textarea id="tnEditContent" rows="3">${esc(item.content||"")}</textarea></label>${itemsHtml}<label>Due Date<input type="date" id="tnEditDue" value="${(item.due_date||"").slice(0,10)}" /></label>`;
        saveBtn.onclick = async () => {
          const updates = {
            title: document.getElementById("tnEditTitle").value.trim(),
            content: document.getElementById("tnEditContent").value.trim(),
            due_date: document.getElementById("tnEditDue").value || null,
          };
          if (isChecklist) {
            const lines = document.getElementById("tnEditItems").value.split("\n").filter(l => l.trim());
            updates.items = lines.map(l => {
              const done = /^\[x\]\s*/i.test(l);
              return { text: l.replace(/^\[x\]\s*/i, "").trim(), done };
            });
          }
          await tnPut("/notes/" + item.id, updates);
          closeTnEditModal();
          if (isChecklist) { loadTodos(); } else { loadNotesOnly(); }
        };
      } else if (type === "agent-op") {
        titleEl.textContent = "Edit Agent Op";
        const modelOpts = (typeof window.SABLE_MODELS !== "undefined" ? window.SABLE_MODELS : [
          { id: "qwen3.7-max", label: "Qwen3.7 Max" },
          { id: "qwen3.8-max", label: "Qwen3.8 Max" },
          { id: "deepseek-expert", label: "DeepSeek Expert" },
        ]).map(m => `<option value="${m.id}"${m.id===item.model?" selected":""}>${m.label||m.id}</option>`).join("");
        const time12 = item.schedule_time ? (() => { const [h,m] = item.schedule_time.split(":"); const hr = parseInt(h,10); const ampm = hr >= 12 ? "PM" : "AM"; const h12 = hr % 12 || 12; return `${h12}:${m} ${ampm}`; })() : "";
        body.innerHTML = `<label>Name<input type="text" id="tnEditName" value="${esc(item.name)}" /></label><label>Prompt<textarea id="tnEditPrompt" rows="4">${esc(item.prompt)}</textarea></label><label>Model<select id="tnEditModel">${modelOpts}</select></label><label>Schedule Type<select id="tnEditSchedType"><option value="daily"${item.schedule_type==="daily"?" selected":""}>Daily</option><option value="weekly"${item.schedule_type==="weekly"?" selected":""}>Weekly</option><option value="cron"${item.schedule_type==="cron"?" selected":""}>Cron</option></select></label><label>Time<input type="time" id="tnEditTime" value="${item.schedule_time||""}" /><span class="tn-time-preview">${time12}</span></label><label>Cron Expression<input type="text" id="tnEditCron" value="${esc(item.cron_expression||"")}" placeholder="e.g. 0 */6 * * *" /></label>`;
        saveBtn.onclick = async () => {
          await tnPut("/agent-ops/" + item.id, {
            name: document.getElementById("tnEditName").value.trim(),
            prompt: document.getElementById("tnEditPrompt").value.trim(),
            model: document.getElementById("tnEditModel").value,
            schedule_type: document.getElementById("tnEditSchedType").value,
            schedule_time: document.getElementById("tnEditTime").value || null,
            cron_expression: document.getElementById("tnEditCron").value.trim() || null,
          });
          closeTnEditModal(); loadAgentOps();
        };
      }
    }

    // Load all panels when sidebar opens
    function loadAllPanels() {
      loadNotesOnly();
      loadTodos();
      loadAgentOps();
    }



    function diffLineEl(cls, text) {
      const d = document.createElement("div");
      d.className = `diff-line ${cls}`;
      d.textContent = text ?? "";
      return d;
    }

    function handleFileEdit(evt, autoOpen) {
      if (!diffCardsEl || !evt) return;
      const item = document.createElement("div");
      item.className = "diff-item";

      const name = document.createElement("div");
      name.className = "diff-item-name";
      name.textContent = evt.name || evt.path || "file";
      name.title = evt.path || "";
      name.addEventListener("click", () => {
        if (evt.backup_path && typeof window.openDiffEditor === "function") {
          window.openDiffEditor(evt.path, evt.backup_path, evt.name || evt.path);
        }
      });

      const stats = document.createElement("div");
      stats.className = "diff-item-stats";
      const added = evt.added || 0;
      const removed = evt.removed || 0;
      stats.innerHTML = `<span class="diff-add-stat">+${added}</span><span class="diff-sep"> / </span><span class="diff-del-stat">-${removed}</span>`;

      const revertBtn = document.createElement("button");
      revertBtn.className = "diff-revert-btn";
      revertBtn.textContent = "\u21a9 Revert";
      if (!evt.backup_path) revertBtn.disabled = true;
      revertBtn.addEventListener("click", async (e) => {
        e.stopPropagation();
        if (!evt.backup_path || revertBtn.disabled) return;
        revertBtn.disabled = true;
        revertBtn.textContent = "Reverting\u2026";
        try {
          const res = await fetch("/api/file/revert", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ path: evt.path, backup_path: evt.backup_path }),
          });
          const data = await res.json().catch(() => ({}));
          if (res.ok && data.status === "ok") {
            revertBtn.textContent = "\u2713 Reverted";
            showToast("File reverted successfully", "success");
          } else {
            revertBtn.textContent = "\u2717 Failed";
            showToast(data.detail || "Revert failed", "error");
          }
        } catch (err) {
          revertBtn.textContent = "\u2717 Error";
          showToast("Revert error: " + err.message, "error");
        }
        setTimeout(() => { revertBtn.textContent = "\u21a9 Revert"; revertBtn.disabled = false; }, 2000);
      });

      item.append(name, stats, revertBtn);
      diffCardsEl.prepend(item);
      while (diffCardsEl.children.length > MAX_DIFF_CARDS) {
        diffCardsEl.lastElementChild.remove();
      }
      if (autoOpen) {
        document.body.classList.add("diff-open");
        if (typeof AgentPanel !== "undefined") AgentPanel.close();
      }
    }
