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
          document.body.classList.remove("tracknote-open");
          if (typeof AgentPanel !== "undefined") AgentPanel.close();
        }
      }
    });

    // ---------- TrackNote sidebar ----------
    const trackNoteBtn = document.getElementById("trackNoteBtn");
    const trackNoteCloseBtn = document.getElementById("trackNoteClose");
    const trackNotePill = document.getElementById("trackNotePill");
    const tnPanels = {
      schedule: document.getElementById("tnPanelSchedule"),
      todo: document.getElementById("tnPanelTodo"),
      "agent-tasks": document.getElementById("tnPanelAgentTasks"),
    };

    function setTrackNoteMode(mode) {
      if (!trackNotePill) return;
      const btns = trackNotePill.querySelectorAll("button");
      let idx = 0;
      btns.forEach((b, i) => {
        const isActive = b.dataset.mode === mode;
        b.classList.toggle("active", isActive);
        if (isActive) idx = i;
      });
      trackNotePill.style.setProperty("--i", idx);
      Object.entries(tnPanels).forEach(([k, el]) => {
        if (el) el.classList.toggle("active", k === mode);
      });
    }

    if (trackNoteBtn) trackNoteBtn.addEventListener("click", () => {
      const opening = !document.body.classList.contains("tracknote-open");
      document.body.classList.toggle("tracknote-open");
      if (opening) {
        document.body.classList.remove("diff-open");
        if (typeof AgentPanel !== "undefined") AgentPanel.close();
      }
    });
    if (trackNoteCloseBtn) trackNoteCloseBtn.addEventListener("click", () => {
      document.body.classList.remove("tracknote-open");
    });
    if (trackNotePill) {
      trackNotePill.addEventListener("click", (e) => {
        const btn = e.target.closest("button[data-mode]");
        if (btn) setTrackNoteMode(btn.dataset.mode);
      });
    }


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

    // ---------- TrackNote: Schedule panel ----------
    const tnSchedList = document.getElementById("tnSchedList");
    const tnSchedEmpty = document.getElementById("tnSchedEmpty");

    function formatTime24(t) {
      if (!t) return "";
      // Already HH:MM (24h) from backend — just return as-is
      return t;
    }

    function renderScheduleItem(s) {
      const div = document.createElement("div");
      div.className = "tn-item";
      const typeLabel = s.schedule_type === "weekly" ? `Weekly (${["Mon","Tue","Wed","Thu","Fri","Sat","Sun"][s.day_of_week || 0]})` : s.schedule_type === "occasional" ? `Once (${(s.start_date||"").slice(0,10)})` : "Daily";
      div.innerHTML = `<div class="tn-item-title">${esc(s.title)}</div><div class="tn-item-meta">${typeLabel} ${formatTime24(s.time)}${s.description ? " — " + esc(s.description) : ""}</div><div class="tn-item-actions"><button class="tn-item-action" data-edit="${s.id}" title="Edit">✎</button><button class="tn-item-action danger" data-del="${s.id}" title="Delete">✕</button></div>`;
      div.querySelector("[data-del]").addEventListener("click", async () => {
        await tnDelete("/schedules/" + s.id);
        loadSchedules();
      });
      div.querySelector("[data-edit]").addEventListener("click", () => {
        openTnEditModal("schedule", s);
      });
      return div;
    }

    async function loadSchedules() {
      try {
        const data = await tnFetch("/schedules");
        tnSchedList.innerHTML = "";
        const items = data.schedules || [];
        items.forEach(s => tnSchedList.appendChild(renderScheduleItem(s)));
        tnSchedEmpty.style.display = items.length ? "none" : "block";
      } catch(e) { console.warn("loadSchedules failed", e); }
    }

    document.getElementById("tnSchedAdd")?.addEventListener("click", async () => {
      const title = document.getElementById("tnSchedTitle").value.trim();
      if (!title) return;
      const stype = document.getElementById("tnSchedType").value;
      const time = document.getElementById("tnSchedTime").value || null;
      await tnPost("/schedules", { title, schedule_type: stype, time });
      document.getElementById("tnSchedTitle").value = "";
      loadSchedules();
    });

    // ---------- TrackNote: Notes/Todos panel ----------
    const tnNoteList = document.getElementById("tnNoteList");
    const tnNoteEmpty = document.getElementById("tnNoteEmpty");

    function renderNoteItem(n) {
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
      html += `<div class="tn-item-actions"><button class="tn-item-action" data-edit="${n.id}" title="Edit">✎</button><button class="tn-item-action danger" data-del="${n.id}" title="Delete">✕</button></div>`;
      div.innerHTML = html;
      div.querySelector("[data-del]").addEventListener("click", async () => {
        await tnDelete("/notes/" + n.id);
        loadNotes();
      });
      div.querySelector("[data-edit]").addEventListener("click", () => {
        openTnEditModal("note", n);
      });
      div.querySelectorAll("input[type=checkbox][data-note]").forEach(cb => {
        cb.addEventListener("change", async () => {
          await tnPost("/notes/" + cb.dataset.note + "/toggle-item?index=" + cb.dataset.idx);
          loadNotes();
        });
      });
      return div;
    }

    async function loadNotes() {
      try {
        const data = await tnFetch("/notes");
        tnNoteList.innerHTML = "";
        const items = data.notes || [];
        items.forEach(n => tnNoteList.appendChild(renderNoteItem(n)));
        tnNoteEmpty.style.display = items.length ? "none" : "block";
      } catch(e) { console.warn("loadNotes failed", e); }
    }

    document.getElementById("tnNoteAdd")?.addEventListener("click", async () => {
      const title = document.getElementById("tnNoteTitle").value.trim();
      const content = document.getElementById("tnNoteContent").value.trim();
      if (!title && !content) return;
      await tnPost("/notes", { title: title || "Untitled", content, note_type: "note" });
      document.getElementById("tnNoteTitle").value = "";
      document.getElementById("tnNoteContent").value = "";
      loadNotes();
    });

    document.getElementById("tnTodoAdd")?.addEventListener("click", async () => {
      const title = document.getElementById("tnNoteTitle").value.trim();
      const content = document.getElementById("tnNoteContent").value.trim();
      if (!title && !content) return;
      const firstItem = content || "New item";
      await tnPost("/notes", { title: title || "Untitled", note_type: "checklist", items: [{ text: firstItem, done: false }] });
      document.getElementById("tnNoteTitle").value = "";
      document.getElementById("tnNoteContent").value = "";
      loadNotes();
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
      const schedInfo = op.schedule_type === "cron" ? `Cron: ${op.cron_expression || "?"}` : `${op.schedule_type} ${formatTime24(op.schedule_time)}`;
      const lastRun = op.last_run ? new Date(op.last_run).toLocaleString() : "never";
      div.innerHTML = `<div class="tn-item-title"><span class="tn-agent-status ${statusClass}"></span>${esc(op.name)}</div><div class="tn-item-meta">${schedInfo} · Model: ${esc(op.model)} · Last: ${lastRun}</div><div class="tn-item-meta" style="margin-top:4px;opacity:.7">${esc(op.prompt).slice(0, 150)}${op.prompt.length > 150 ? "…" : ""}</div><div class="tn-item-actions"><button class="tn-item-action" data-toggle="${op.id}" title="Toggle">${op.enabled ? "⏸" : "▶"}</button><button class="tn-item-action" data-edit="${op.id}" title="Edit">✎</button><button class="tn-item-action danger" data-del="${op.id}" title="Delete">✕</button></div>`;
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
        tnAgentList.innerHTML = "";
        const items = data.ops || [];
        items.forEach(op => tnAgentList.appendChild(renderAgentOp(op)));
        tnAgentEmpty.style.display = items.length ? "none" : "block";
      } catch(e) { console.warn("loadAgentOps failed", e); }
    }

    document.getElementById("tnAgentAdd")?.addEventListener("click", async () => {
      const name = document.getElementById("tnAgentName").value.trim();
      const prompt = document.getElementById("tnAgentPrompt").value.trim();
      if (!name || !prompt) return;
      const model = tnAgentModelSel?.value || "qwen3.7-max";
      const stype = document.getElementById("tnAgentSchedType").value;
      const time = document.getElementById("tnAgentTime").value || null;
      await tnPost("/agent-ops", { name, prompt, model, schedule_type: stype, schedule_time: time });
      document.getElementById("tnAgentName").value = "";
      document.getElementById("tnAgentPrompt").value = "";
      loadAgentOps();
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

      if (type === "schedule") {
        titleEl.textContent = "Edit Schedule";
        const time12 = item.time ? (() => { const [h,m] = item.time.split(":"); const hr = parseInt(h,10); const ampm = hr >= 12 ? "PM" : "AM"; const h12 = hr % 12 || 12; return `${h12}:${m} ${ampm}`; })() : "";
        body.innerHTML = `<label>Title<input type="text" id="tnEditTitle" value="${esc(item.title)}" /></label><label>Type<select id="tnEditType"><option value="daily"${item.schedule_type==="daily"?" selected":""}>Daily</option><option value="weekly"${item.schedule_type==="weekly"?" selected":""}>Weekly</option><option value="occasional"${item.schedule_type==="occasional"?" selected":""}>Occasional</option></select></label><label>Time<input type="time" id="tnEditTime" value="${item.time||""}" /><span class="tn-time-preview">${time12}</span></label><label>Description<textarea id="tnEditDesc" rows="2">${esc(item.description||"")}</textarea></label>`;
        saveBtn.onclick = async () => {
          await tnPut("/schedules/" + item.id, {
            title: document.getElementById("tnEditTitle").value.trim(),
            schedule_type: document.getElementById("tnEditType").value,
            time: document.getElementById("tnEditTime").value || null,
            description: document.getElementById("tnEditDesc").value.trim(),
          });
          closeTnEditModal(); loadSchedules();
        };
      } else if (type === "note") {
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
          closeTnEditModal(); loadNotes();
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

    // Load all panels when TrackNote opens
    const _origTnToggle = trackNoteBtn ? trackNoteBtn.onclick : null;
    if (trackNoteBtn) {
      trackNoteBtn.addEventListener("click", () => {
        if (document.body.classList.contains("tracknote-open")) {
          loadSchedules(); loadNotes(); loadAgentOps();
        }
      });
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
