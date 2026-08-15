/* Personas & Output Format — settings tab logic */
(function () {
  const API = "/api/personas";
  let _personas = [];
  let _editingName = null;

  async function pFetch(path, opts = {}) {
    const token = localStorage.getItem("sable_auth_token") || "";
    const resp = await fetch(API + path, {
      ...opts,
      headers: {
        "Content-Type": "application/json",
        Authorization: "Bearer " + token,
        ...(opts.headers || {}),
      },
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ detail: resp.statusText }));
      throw new Error(err.detail || "Request failed");
    }
    return resp.json();
  }

  // ─── Render ───────────────────────────────────────────────────────────────
  async function loadPersonas() {
    const grid = document.getElementById("personaGrid");
    if (!grid) return;
    try {
      const data = await pFetch("");
      _personas = data.personas || [];
      renderGrid(grid);
    } catch (e) {
      grid.innerHTML = '<p class="muted" style="font-size:12px;">Failed to load: ' + e.message + "</p>";
    }
  }

  function renderGrid(grid) {
    if (!_personas.length) {
      grid.innerHTML = '<p class="muted" style="font-size:12px;font-style:italic;">No entries yet</p>';
      return;
    }
    grid.innerHTML = _personas.map((p) => {
      const activeClass = p.active ? " persona-card-active" : "";
      const badge = p.active ? '<span class="persona-badge-active">active</span>' : "";
      return `
        <div class="persona-card${activeClass}" data-name="${p.name}">
          <div class="persona-card-top">
            <span class="persona-card-name">${esc(p.name)}</span>
            ${badge}
          </div>
          <p class="persona-card-preview">${esc(p.preview)}</p>
          <div class="persona-card-actions">
            <button class="persona-act" data-action="edit" data-name="${p.name}">Edit</button>
            <button class="persona-act persona-act-active" data-action="setactive" data-name="${p.name}">${p.active ? "Deactivate" : "Activate"}</button>
            <button class="persona-act persona-act-delete" data-action="delete" data-name="${p.name}">Delete</button>
          </div>
        </div>`;
    }).join("");

    // Bind events
    grid.querySelectorAll(".persona-act").forEach((b) => {
      b.onclick = (e) => {
        e.stopPropagation();
        const name = b.dataset.name;
        if (b.dataset.action === "edit") openEditor(name);
        else if (b.dataset.action === "setactive") setActive(name);
        else if (b.dataset.action === "delete") deleteEntry(name);
      };
    });
    // Click card body to edit
    grid.querySelectorAll(".persona-card").forEach((c) => {
      c.onclick = (e) => {
        if (e.target.closest(".persona-act")) return;
        openEditor(c.dataset.name);
      };
    });
  }

  // ─── Editor ───────────────────────────────────────────────────────────────
  function _showEditor() {
    document.getElementById("personaEditor").classList.remove("hidden");
    document.getElementById("personaEditorBackdrop").classList.remove("hidden");
  }

  function _hideEditor() {
    document.getElementById("personaEditor").classList.add("hidden");
    document.getElementById("personaEditorBackdrop").classList.add("hidden");
  }

  function openEditor(name) {
    const nameInput = document.getElementById("personaEditorName");
    const contentArea = document.getElementById("personaEditorContent");
    _showEditor();
    _editingName = name || null;
    nameInput.value = name || "";
    nameInput.disabled = !!name;
    contentArea.value = "Loading...";

    if (name) {
      pFetch("/" + encodeURIComponent(name)).then((d) => {
        contentArea.value = d.content || "";
      }).catch((e) => {
        contentArea.value = "";
        showToast("Failed to load: " + e.message, true);
      });
    } else {
      contentArea.value = "";
    }
  }

  function openFormatEditor() {
    const nameInput = document.getElementById("personaEditorName");
    const contentArea = document.getElementById("personaEditorContent");
    _showEditor();
    _editingName = "__output_format__";
    nameInput.value = "output_format";
    nameInput.disabled = true;
    contentArea.value = "";
    pFetch("/output-format").then((d) => { contentArea.value = d.content || ""; }).catch(() => {});
  }

  function closeEditor() {
    _hideEditor();
    _editingName = null;
  }

  async function saveEditor() {
    const nameInput = document.getElementById("personaEditorName");
    const contentArea = document.getElementById("personaEditorContent");
    const name = nameInput.value.trim();
    const content = contentArea.value;

    if (_editingName === "__output_format__") {
      await pFetch("/output-format", { method: "PUT", body: JSON.stringify({ content }) });
      closeEditor();
      showToast("Output format saved");
      return;
    }

    if (!name) { showToast("Name required", true); return; }

    try {
      if (_editingName) {
        await pFetch("/" + encodeURIComponent(_editingName), { method: "PUT", body: JSON.stringify({ content }) });
        showToast("Saved " + _editingName);
      } else {
        await pFetch("", { method: "POST", body: JSON.stringify({ name, content }) });
        showToast("Created " + name);
      }
      closeEditor();
      loadPersonas();
    } catch (e) {
      showToast(e.message, true);
    }
  }

  // ─── Actions ──────────────────────────────────────────────────────────────
  async function setActive(name) {
    try {
      const current = _personas.find((p) => p.name === name);
      const newName = current && current.active ? null : name;
      await pFetch("/active", { method: "PUT", body: JSON.stringify({ name: newName }) });
      loadPersonas();
    } catch (e) { showToast(e.message, true); }
  }

  async function deleteEntry(name) {
    if (!confirm("Delete \'" + name + "\' permanently?")) return;
    try {
      await pFetch("/" + encodeURIComponent(name), { method: "DELETE" });
      showToast("Deleted " + name);
      loadPersonas();
    } catch (e) { showToast(e.message, true); }
  }

  // ─── Init ─────────────────────────────────────────────────────────────────
  function initPersonas() {
    loadPersonas();
    loadFormatToggle();
    const addBtn = document.getElementById("personaAddBtn");
    const fmtBtn = document.getElementById("personaEditFormatBtn");
    const fmtToggle = document.getElementById("personaFormatToggle");
    const saveBtn = document.getElementById("personaEditorSave");
    const closeBtn = document.getElementById("personaEditorClose");
    if (addBtn) addBtn.onclick = () => openEditor(null);
    if (fmtBtn) fmtBtn.onclick = openFormatEditor;
    if (fmtToggle) fmtToggle.onchange = () => saveFormatToggle(fmtToggle.checked);
    if (saveBtn) saveBtn.onclick = saveEditor;
    if (closeBtn) closeBtn.onclick = closeEditor;
    const backdrop = document.getElementById("personaEditorBackdrop");
    if (backdrop) backdrop.onclick = closeEditor;
  }

  async function loadFormatToggle() {
    const toggle = document.getElementById("personaFormatToggle");
    if (!toggle) return;
    try {
      const data = await pFetch("");
      toggle.checked = data.config.output_format_enabled !== false;
    } catch (e) {}
  }

  async function saveFormatToggle(enabled) {
    try {
      await pFetch("/output-format-toggle", { method: "PUT", body: JSON.stringify({ enabled }) });
    } catch (e) { showToast("Failed to save toggle", true); }
  }

  // ─── Utilities ────────────────────────────────────────────────────────────
  function esc(str) {
    if (!str) return "";
    return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function showToast(msg, isError = false) {
    const toast = document.createElement("div");
    toast.className = "cb-toast" + (isError ? " cb-toast-error" : "");
    toast.textContent = msg;
    document.body.appendChild(toast);
    setTimeout(() => toast.classList.add("visible"), 10);
    setTimeout(() => {
      toast.classList.remove("visible");
      setTimeout(() => toast.remove(), 300);
    }, 3500);
  }

  window._personaInit = initPersonas;
})();
