
/* =========================================================================
   Sable File Manager — VS Code-style tree explorer with editing
   ========================================================================= */
(function () {
  "use strict";

  const overlay = document.getElementById("fsOverlay");
  const closeBtn = document.getElementById("fsClose");
  const switchRootBtn = document.getElementById("fsSwitchRoot");
  const treeEl = document.getElementById("fsTree");
  const viewerEl = document.getElementById("fsViewer");
  const pathBar = document.getElementById("fsPathBar");
  const openBtn = document.getElementById("filesBtn");

  if (!overlay) return;

  let rootPath = "";
  let expandedDirs = new Set();
  let activeFileEl = null;
  let currentFilePath = "";
  let isDirty = false;
  let monacoEditor = null; // current Monaco editor instance
  let monacoReady = null;  // promise that resolves when Monaco is loaded
  let diffReviewEditor = null; // inline diff review editor

  function getEditorFontSize() {
    return parseInt(localStorage.getItem("sable_editor_font_size") || "13", 10);
  }

  /* ---------- Lucide helpers ---------- */
  function icon(name, size) {
    return `<i data-lucide="${name}" style="width:${size || 14}px;height:${size || 14}px;"></i>`;
  }
  function refreshIcons() {
    if (window.lucide) lucide.createIcons();
  }

  /* ---------- File type → Lucide icon ---------- */
  function fileIcon(name) {
    const ext = name.split(".").pop().toLowerCase();
    const map = {
      py: "file-code-2", js: "file-code", ts: "file-code", tsx: "file-code", jsx: "file-code",
      html: "file-code", css: "palette", json: "braces", yaml: "file-text", yml: "file-text",
      md: "file-text", txt: "file-text", toml: "settings-2", cfg: "settings-2", ini: "settings-2",
      sh: "terminal", bash: "terminal", zsh: "terminal",
      sql: "database", csv: "table", xml: "file-code",
      svg: "image", png: "image", jpg: "image", jpeg: "image", gif: "image", webp: "image",
      lock: "lock", env: "key-round",
    };
    return map[ext] || "file";
  }

  /* ---------- Open / Close ---------- */
  function openFS() {
    overlay.classList.remove("hidden");
    if (!rootPath) showRootPicker();
  }
  function closeFS() {
    hideFsCtx();
    if (isDiffOpen) {
      // Closing diff popup — dispose diff editor only, preserve main editor
      if (diffEditor) { diffEditor.dispose(); diffEditor = null; }
      isDiffOpen = false;
    } else {
      // Closing file manager — safe to dispose main editor
      if (monacoEditor) { monacoEditor.dispose(); monacoEditor = null; }
      if (diffReviewEditor) { diffReviewEditor.dispose(); diffReviewEditor = null; }
    }
    overlay.classList.add("hidden");
  }

  if (openBtn) openBtn.addEventListener("click", openFS);
  closeBtn.addEventListener("click", closeFS);
  switchRootBtn.addEventListener("click", () => {
    rootPath = "";
    expandedDirs.clear();
    activeFileEl = null;
    if (monacoEditor) { monacoEditor.dispose(); monacoEditor = null; }
    if (diffReviewEditor) { diffReviewEditor.dispose(); diffReviewEditor = null; }
    viewerEl.innerHTML = '<div class="fs-viewer-empty">Select a file to view</div>';
    showRootPicker();
  });
  overlay.addEventListener("click", (e) => {
    hideFsCtx();
    if (e.target === overlay) closeFS();
  });

  /* ---------- Root picker ---------- */
  const fsHistory = JSON.parse(localStorage.getItem("fs_root_history") || "[]");

  function saveHistory(path) {
    const idx = fsHistory.indexOf(path);
    if (idx > -1) fsHistory.splice(idx, 1);
    fsHistory.unshift(path);
    if (fsHistory.length > 8) fsHistory.pop();
    localStorage.setItem("fs_root_history", JSON.stringify(fsHistory));
  }

  function showRootPicker() {
    pathBar.textContent = "";
    treeEl.innerHTML = "";

    // Open Folder button
    const openRow = document.createElement("div");
    openRow.className = "fs-input-row";
    openRow.innerHTML = `<button class="fs-open-btn" id="fsOpenFolderBtn">${icon("folder-open", 15)} <span>Open Folder</span></button>`;
    treeEl.appendChild(openRow);

    const openFolderBtn = openRow.querySelector("#fsOpenFolderBtn");
    openFolderBtn.addEventListener("click", async () => {
      openFolderBtn.disabled = true;
      openFolderBtn.querySelector("span").textContent = "Waiting…";
      try {
        const res = await fetch("/api/filesystem/pick-folder");
        const data = await res.json();
        if (data.path) { saveHistory(data.path); openRoot(data.path); return; }
        openFolderBtn.querySelector("span").textContent = data.error || "Open Folder";
      } catch { openFolderBtn.querySelector("span").textContent = "Open Folder"; }
      openFolderBtn.disabled = false;
    });

    // History
    if (fsHistory.length > 0) {
      const histLabel = document.createElement("div");
      histLabel.className = "fs-pick-label";
      histLabel.innerHTML = `${icon("history", 12)} Recent`;
      treeEl.appendChild(histLabel);
      fsHistory.forEach((p) => {
        const item = document.createElement("div");
        item.className = "fs-item";
        item.innerHTML = `<span class="fs-icon">${icon("folder", 14)}</span><span class="fs-name">${esc(p)}</span>`;
        item.addEventListener("click", () => { saveHistory(p); openRoot(p); });
        treeEl.appendChild(item);
      });
    }

    // Quick access
    fetch("/api/filesystem/roots")
      .then((r) => r.json())
      .then((roots) => {
        const label = document.createElement("div");
        label.className = "fs-pick-label";
        label.innerHTML = `${icon("hard-drive", 12)} Quick access`;
        treeEl.appendChild(label);
        roots.forEach((r) => {
          const item = document.createElement("div");
          item.className = "fs-item fs-root";
          item.innerHTML = `<span class="fs-icon">${icon("hard-drive", 14)}</span><span class="fs-name">${esc(r.label)}</span>`;
          item.addEventListener("click", () => { saveHistory(r.path); openRoot(r.path); });
          treeEl.appendChild(item);
        });
        refreshIcons();
      })
      .catch(() => { treeEl.innerHTML += '<div class="fs-error">Failed to load</div>'; });
  }

  function openRoot(path) {
    rootPath = path;
    expandedDirs.clear();
    expandedDirs.add(path);
    pathBar.textContent = path;
    loadTree();
  }

  /* ---------- Tree rendering ---------- */
  async function loadTree() {
    treeEl.innerHTML = '<div class="fs-loading">Loading…</div>';
    const items = await fetchDir(rootPath);
    if (items === null) return;
    treeEl.innerHTML = "";
    renderToolbar();
    renderItems(items, 0, treeEl);
    refreshIcons();
  }

  function renderToolbar() {
    const bar = document.createElement("div");
    bar.className = "fs-toolbar";
    bar.innerHTML = `
      <button class="fs-tool-btn" id="fsNewFile" title="New File">${icon("file-plus-2", 14)}</button>
      <button class="fs-tool-btn" id="fsNewFolder" title="New Folder">${icon("folder-plus", 14)}</button>
    `;
    treeEl.appendChild(bar);

    bar.querySelector("#fsNewFile").addEventListener("click", () => promptCreate("file"));
    bar.querySelector("#fsNewFolder").addEventListener("click", () => promptCreate("dir"));
  }

  async function promptCreate(type) {
    const label = type === "file" ? "New file name:" : "New folder name:";
    const name = prompt(label);
    if (!name || !name.trim()) return;

    const parentDir = rootPath;
    const fullPath = parentDir + "/" + name.trim();

    try {
      const endpoint = type === "file" ? "/api/filesystem/write" : "/api/filesystem/mkdir";
      const body = type === "file" ? { path: fullPath, content: "" } : { path: fullPath };
      const res = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (data.error) { alert(data.error); return; }
      loadTree(); // refresh
    } catch { alert("Failed to create"); }
  }

  async function fetchDir(dirPath) {
    try {
      const res = await fetch(`/api/filesystem/list?path=${encodeURIComponent(dirPath)}`);
      const data = await res.json();
      if (data.error) return null;
      return data.items;
    } catch { return null; }
  }

  function renderItems(items, depth, container) {
    items.forEach((item) => {
      const el = document.createElement("div");
      el.className = "fs-item" + (item.is_dir ? " fs-dir" : "");
      el.style.paddingLeft = `${10 + depth * 16}px`;
      el.dataset.path = item.path;

      if (item.is_dir) {
        const isOpen = expandedDirs.has(item.path);
        el.innerHTML = `
          <span class="fs-chevron">${icon(isOpen ? "chevron-down" : "chevron-right", 12)}</span>
          <span class="fs-icon">${icon(isOpen ? "folder-open" : "folder", 14)}</span>
          <span class="fs-name">${esc(item.name)}</span>
        `;
        el.addEventListener("click", () => toggleDir(el, item.path, depth));
      } else {
        el.innerHTML = `
          <span class="fs-chevron-placeholder"></span>
          <span class="fs-icon">${icon(fileIcon(item.name), 14)}</span>
          <span class="fs-name">${esc(item.name)}</span>
          <span class="fs-size">${formatSize(item.size || 0)}</span>
        `;
        el.addEventListener("click", () => openFile(el, item.path, item.binary));
      }
      container.appendChild(el);

      if (item.is_dir && expandedDirs.has(item.path)) {
        const childContainer = document.createElement("div");
        childContainer.className = "fs-children";
        childContainer.dataset.parent = item.path;
        container.appendChild(childContainer);
        fetchDir(item.path).then((children) => {
          if (children) { renderItems(children, depth + 1, childContainer); refreshIcons(); }
        });
      }
    });
  }

  /* ---------- Folder expand/collapse ---------- */
  async function toggleDir(el, dirPath, depth) {
    if (expandedDirs.has(dirPath)) {
      expandedDirs.delete(dirPath);
      el.querySelector(".fs-chevron").innerHTML = icon("chevron-right", 12);
      el.querySelector(".fs-icon").innerHTML = icon("folder", 14);
      const existing = treeEl.querySelector(`.fs-children[data-parent="${CSS.escape(dirPath)}"]`);
      if (existing) existing.remove();
      refreshIcons();
    } else {
      expandedDirs.add(dirPath);
      el.querySelector(".fs-chevron").innerHTML = icon("chevron-down", 12);
      el.querySelector(".fs-icon").innerHTML = icon("folder-open", 14);
      const childContainer = document.createElement("div");
      childContainer.className = "fs-children";
      childContainer.dataset.parent = dirPath;
      el.after(childContainer);
      const children = await fetchDir(dirPath);
      if (children && children.length > 0) {
        renderItems(children, depth + 1, childContainer);
      } else {
        childContainer.innerHTML = `<div class="fs-empty" style="padding-left:${10 + (depth + 1) * 16}px">Empty</div>`;
      }
      refreshIcons();
    }
  }

  /* ---------- File viewer / editor ---------- */
  async function openFile(el, filePath, isBinary) {
    if (activeFileEl) activeFileEl.classList.remove("fs-active");
    el.classList.add("fs-active");
    activeFileEl = el;
    currentFilePath = filePath;
    isDirty = false;

    if (isBinary) {
      viewerEl.innerHTML = `<div class="fs-viewer-empty">${icon("image", 32)} Binary file — cannot preview</div>`;
      refreshIcons();
      return;
    }

    viewerEl.innerHTML = '<div class="fs-loading">Reading…</div>';
    try {
      const res = await fetch(`/api/filesystem/read?path=${encodeURIComponent(filePath)}`);
      const data = await res.json();
      if (data.error) {
        viewerEl.innerHTML = `<div class="fs-viewer-error">${icon("alert-triangle", 16)} ${esc(data.error)}</div>`;
        refreshIcons();
        return;
      }

      // Check for pending diff (recent edit with backup)
      try {
        const diffRes = await fetch(`/api/filesystem/pending-diff?path=${encodeURIComponent(filePath)}`);
        const diffData = await diffRes.json();
        if (diffData.has_diff) {
          renderDiffReview(data, diffData);
          return;
        }
      } catch { /* diff check failed — fall through to normal editor */ }

      renderEditor(data);
    } catch {
      viewerEl.innerHTML = '<div class="fs-viewer-error">Network error</div>';
    }
  }

  /* ---------- Sable Monaco Theme ---------- */
  function defineSableMonacoTheme() {
    const cs = getComputedStyle(document.documentElement);
    const v = (name, fallback) => (cs.getPropertyValue(name).trim() || fallback);

    const bg = v("--bg", "#0d0c0a");
    const panel = v("--panel", "#191714");
    const panel2 = v("--panel-2", "#201d19");
    const border = v("--border", "#2a2720");
    const text = v("--text", "#eaeaea");
    const textDim = v("--text-dim", "#c4c4c8");
    const muted = v("--muted", "#85858c");
    const muted2 = v("--muted-2", "#616167");
    const accent = v("--accent", "#9a7d4a");
    const accentText = v("--accent-text", "#c4a66b");

    // Helper: hex to hex without #
    const h = (c) => c.replace("#", "");

    monaco.editor.defineTheme("sable-dark", {
      base: "vs-dark",
      inherit: true,
      rules: [
        { token: "comment", foreground: h(muted2), fontStyle: "italic" },
        { token: "keyword", foreground: h(accentText) },
        { token: "keyword.flow", foreground: h(accentText) },
        { token: "string", foreground: "8fa876" },
        { token: "string.escape", foreground: "a3b88e" },
        { token: "number", foreground: "c98a5e" },
        { token: "constant", foreground: "c98a5e" },
        { token: "type", foreground: "d4b896" },
        { token: "type.identifier", foreground: "d4b896" },
        { token: "identifier", foreground: h(textDim) },
        { token: "function", foreground: h(accentText) },
        { token: "variable", foreground: "c4837a" },
        { token: "variable.predefined", foreground: "c4837a" },
        { token: "operator", foreground: h(muted) },
        { token: "delimiter", foreground: h(muted) },
        { token: "tag", foreground: h(accentText) },
        { token: "attribute.name", foreground: "d4b896" },
        { token: "attribute.value", foreground: "8fa876" },
        { token: "regexp", foreground: "c98a5e" },
        { token: "annotation", foreground: h(muted) },
        { token: "namespace", foreground: "d4b896" },
      ],
      colors: {
        "editor.background": bg,
        "editor.foreground": textDim,
        "editor.lineHighlightBackground": panel,
        "editorLineNumber.foreground": muted2,
        "editorLineNumber.activeForeground": textDim,
        "editorCursor.foreground": accentText,
        "editor.selectionBackground": accent + "30",
        "editor.inactiveSelectionBackground": accent + "18",
        "editorGutter.background": bg,
        "editorWidget.background": panel,
        "editorWidget.border": border,
        "editorWidget.foreground": textDim,
        "input.background": panel2,
        "input.foreground": text,
        "input.border": border,
        "dropdown.background": panel,
        "dropdown.border": border,
        "dropdown.foreground": textDim,
        "list.hoverBackground": panel2,
        "list.activeSelectionBackground": accent + "25",
        "list.activeSelectionForeground": text,
        "scrollbarSlider.background": border + "80",
        "scrollbarSlider.hoverBackground": border,
        "scrollbarSlider.activeBackground": muted2,
        "editorBracketMatch.border": accent + "60",
        "editorBracketHighlight.foreground1": accentText,
        "editorBracketHighlight.foreground2": "d4b896",
        "editorBracketHighlight.foreground3": "8fa876",
        "editorBracketHighlight.foreground4": "c98a5e",
        "editorBracketHighlight.foreground5": "c4837a",
        "editorBracketHighlight.foreground6": h(muted),
        "editorIndentGuide.background1": border,
        "editorIndentGuide.activeBackground1": muted2,
        "minimap.background": bg,
        "editorOverviewRuler.border": bg,
        "editorGroup.border": border,
        "tab.activeBackground": panel,
        "tab.inactiveBackground": bg,
        "tab.activeForeground": text,
        "tab.inactiveForeground": muted,
        "tab.border": border,
        "focusBorder": accent + "60",
        "editorSuggestWidget.background": panel,
        "editorSuggestWidget.border": border,
        "editorSuggestWidget.foreground": textDim,
        "editorSuggestWidget.selectedBackground": accent + "25",
        "editorHoverWidget.background": panel,
        "editorHoverWidget.border": border,
        "peekView.border": accent + "40",
        "peekViewEditor.background": bg,
        "peekViewResult.background": panel,
        "diffEditor.insertedTextBackground": "#6fcf9730",
        "diffEditor.insertedLineBackground": "#6fcf9720",
        "diffEditor.removedTextBackground": "#e5646a30",
        "diffEditor.removedLineBackground": "#e5646a20",
        "diffEditorGutter.insertedLineBackground": "#6fcf9740",
        "diffEditorGutter.removedLineBackground": "#e5646a40",
        "diffEditorOverview.insertedForeground": "#6fcf9780",
        "diffEditorOverview.removedForeground": "#e5646a80",
      },
    });
  }

  // Re-apply Monaco theme when Sable theme changes
  const themeObserver = new MutationObserver(() => {
    if (typeof monaco !== "undefined" && monaco.editor) {
      defineSableMonacoTheme();
      monaco.editor.setTheme("sable-dark");
    }
  });
  themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme", "class"] });

  /* ---------- Monaco lazy init ---------- */
  function loadMonaco() {
    if (monacoReady) return monacoReady;
    monacoReady = new Promise((resolve) => {
      require(["vs/editor/editor.main"], () => {
        // Define Sable theme — reads live CSS variables so it follows theme switches
        defineSableMonacoTheme();
        monaco.editor.setTheme("sable-dark");
        resolve(monaco);
      });
    });
    return monacoReady;
  }

  /* ---------- Language detection ---------- */
  function monacoLang(ext) {
    const map = {
      py: "python", js: "javascript", ts: "typescript", tsx: "typescript",
      jsx: "javascript", json: "json", css: "css", scss: "scss",
      html: "html", htm: "html", md: "markdown", markdown: "markdown",
      sh: "shell", bash: "shell", zsh: "shell", yml: "yaml", yaml: "yaml",
      xml: "xml", svg: "xml", sql: "sql", toml: "ini", ini: "ini",
      cfg: "ini", conf: "ini", bat: "bat", cmd: "bat",
    };
    return map[ext] || "plaintext";
  }

  async function renderEditor(data, targetEl) {
    const el = targetEl || viewerEl;
    const ext = (data.ext || "").replace(".", "");
    const lines = data.content.split("\n");

    // Dispose previous editor + its model to avoid duplicate-URI errors on re-open
    if (monacoEditor) {
      const oldModel = monacoEditor.getModel();
      monacoEditor.dispose();
      if (oldModel) oldModel.dispose();
      monacoEditor = null;
    }
    // Also dispose any orphaned model matching the current file URI
    if (currentFilePath && typeof monaco !== "undefined" && monaco.editor) {
      const orphan = monaco.editor.getModel(monaco.Uri.file(currentFilePath));
      if (orphan) orphan.dispose();
    }

    el.innerHTML = `
      <div class="fs-viewer-header">
        <span class="fs-viewer-name">${icon(fileIcon(data.name), 14)} <span id="fsFileName">${esc(data.name)}</span><span id="fsDirtyDot" class="fs-dirty-dot" style="display:none;">●</span></span>
        <div class="fs-viewer-actions">
          <button class="fs-action-btn fs-save-btn" id="fsSaveBtn" title="Save (Ctrl+S)">${icon("save", 13)} Save</button>
        </div>
        <span class="fs-viewer-meta">${formatSize(data.size)} · ${lines.length} lines${ext ? ` · .${ext}` : ""}</span>
      </div>
      <div class="fs-monaco-wrap" id="fsMonacoWrap"></div>
    `;

    const wrapEl = el.querySelector("#fsMonacoWrap");
    const saveBtn = el.querySelector("#fsSaveBtn");
    const dirtyDot = el.querySelector("#fsDirtyDot");

    // Save handler
    async function doSave() {
      if (!monacoEditor) return;
      const content = monacoEditor.getValue();
      try {
        const res = await fetch("/api/filesystem/write", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: currentFilePath, content }),
        });
        const result = await res.json();
        if (result.error) { alert(result.error); return; }
        isDirty = false;
        dirtyDot.style.display = "none";
        syncSidebarDirty();
      } catch { alert("Save failed"); }
    }

    saveBtn.addEventListener("click", doSave);

    // Wait for Monaco to be ready
    await loadMonaco();

    // Create editor — reuse existing model if URI still registered (prevents duplicate-URI crash on re-open)
    const uri = currentFilePath ? monaco.Uri.file(currentFilePath) : undefined;
    let model;
    if (uri) {
      const existing = monaco.editor.getModel(uri);
      if (existing) {
        existing.setValue(data.content);
        model = existing;
      } else {
        model = monaco.editor.createModel(data.content, monacoLang(ext), uri);
      }
    } else {
      model = monaco.editor.createModel(data.content, monacoLang(ext));
    }
    monacoEditor = monaco.editor.create(wrapEl, {
      model,
      theme: "sable-dark",
      fontSize: getEditorFontSize(),
      fontFamily: "JetBrains Mono, Fira Code, monospace",
      minimap: { enabled: false },
      lineNumbers: "on",
      wordWrap: "on",
      scrollBeyondLastLine: false,
      padding: { top: 8 },
      smoothScrolling: true,
      cursorBlinking: "smooth",
      renderLineHighlight: "all",
      scrollbar: { verticalScrollbarSize: 8, horizontalScrollbarSize: 8 },
      automaticLayout: true,
    });

    // Track dirty state
    monacoEditor.onDidChangeModelContent(() => {
      if (!isDirty) {
        isDirty = true;
        dirtyDot.style.display = "inline";
        syncSidebarDirty();
      }
    });

    // Ctrl+S binding
    monacoEditor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, doSave);

    refreshIcons();
  }

  /* ---------- Inline Diff Review (accept/reject) ---------- */
  async function renderDiffReview(fileData, diffData, targetEl) {
    const el = targetEl || viewerEl;
    const ext = (fileData.ext || "").replace(".", "");
    const fileName = fileData.name;

    // Dispose previous editors
    if (monacoEditor) { monacoEditor.dispose(); monacoEditor = null; }
    if (diffReviewEditor) { diffReviewEditor.dispose(); diffReviewEditor = null; }

    el.innerHTML = `
      <div class="fs-monaco-wrap" id="fsDiffReviewWrap">
        <div class="fs-diff-float-actions">
          <button class="fs-action-btn fs-reject-btn" id="fsRejectBtn" title="Reject — restore previous version">${icon("x", 13)} Reject</button>
          <button class="fs-action-btn fs-accept-btn" id="fsAcceptBtn" title="Accept — dismiss diff view">${icon("check", 13)} Accept</button>
        </div>
      </div>
    `;

    const wrapEl = el.querySelector("#fsDiffReviewWrap");
    const acceptBtn = el.querySelector("#fsAcceptBtn");
    const rejectBtn = el.querySelector("#fsRejectBtn");

    await loadMonaco();
    defineSableMonacoTheme();
    monaco.editor.setTheme("sable-dark");

    const originalModel = monaco.editor.createModel(diffData.original_content, monacoLang(ext));
    const modifiedModel = monaco.editor.createModel(diffData.modified_content, monacoLang(ext));

    diffReviewEditor = monaco.editor.createDiffEditor(wrapEl, {
      fontSize: getEditorFontSize(),
      fontFamily: "JetBrains Mono, Fira Code, monospace",
      readOnly: true,
      renderSideBySide: true,
      minimap: { enabled: false },
      scrollBeyondLastLine: false,
      automaticLayout: true,
      padding: { top: 8 },
    });
    diffReviewEditor.setModel({ original: originalModel, modified: modifiedModel });

    // Accept handler — just dismiss diff, show normal editor, keep backup
    acceptBtn.addEventListener("click", () => {
      if (diffReviewEditor) { diffReviewEditor.dispose(); diffReviewEditor = null; }
      renderEditor(fileData, targetEl);
    });

    // Reject handler
    rejectBtn.addEventListener("click", async () => {
      try {
        const res = await fetch("/api/filesystem/reject-edit", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: currentFilePath, backup_path: diffData.backup_path }),
        });
        const result = await res.json();
        if (result.error) { showToast(result.error, "error"); return; }

        // Dispose diff editor, reload file with restored content
        if (diffReviewEditor) { diffReviewEditor.dispose(); diffReviewEditor = null; }
        const restoredData = { ...fileData, content: diffData.original_content };
        renderEditor(restoredData, targetEl);
        showToast("Edit rejected — restored previous version", "success");
      } catch { showToast("Reject failed", "error"); }
    });

    refreshIcons();
  }

  /* ---------- Context Menu ---------- */
  // Context menu — same pattern as global ctxMenu in app.js
  const fsCtxMenu = document.getElementById("fsContextMenu") || (() => {
    const el = document.createElement("div");
    el.id = "fsContextMenu";
    el.className = "fs-ctx-menu";
    document.body.appendChild(el);
    return el;
  })();

  function hideFsCtx() { fsCtxMenu.classList.remove("open"); }
  function showFsCtx() { fsCtxMenu.classList.add("open"); }

  // Close on any click outside the menu (same as global)
  document.addEventListener("click", (e) => {
    if (!e.target.closest("#fsContextMenu")) hideFsCtx();
  });

  // Suppress default browser context menu inside the file manager
  overlay.addEventListener("contextmenu", (e) => {
    e.preventDefault();
  });

  function showCtx(x, y, items) {
    fsCtxMenu.innerHTML = "";
    items.forEach((item) => {
      if (item.separator) {
        const sep = document.createElement("div");
        sep.className = "fs-ctx-sep";
        fsCtxMenu.appendChild(sep);
        return;
      }
      const btn = document.createElement("button");
      btn.className = "fs-ctx-item" + (item.danger ? " fs-ctx-danger" : "");
      btn.innerHTML = `${icon(item.icon, 13)} <span>${item.label}</span>`;
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        hideFsCtx();
        item.action();
      });
      fsCtxMenu.appendChild(btn);
    });

    fsCtxMenu.style.left = Math.min(x, window.innerWidth - 180) + "px";
    fsCtxMenu.style.top = Math.min(y, window.innerHeight - 250) + "px";
    showFsCtx();
    refreshIcons();
  }

  function getParentDir(path) {
    return path.replace(/\/[^/]+\/?$/, "") || "/";
  }

  // Right-click on tree area
  treeEl.addEventListener("contextmenu", (e) => {
    e.preventDefault();
    const itemEl = e.target.closest(".fs-item");

    if (itemEl && itemEl.dataset.path) {
      // Right-clicked on a file/folder
      const path = itemEl.dataset.path;
      const isDir = itemEl.classList.contains("fs-dir");
      const parentDir = getParentDir(path);

      showCtx(e.clientX, e.clientY, [
        { label: "New File", icon: "file-plus-2", action: () => createIn(parentDir, "file") },
        { label: "New Folder", icon: "folder-plus", action: () => createIn(parentDir, "dir") },
        { label: "Copy Path", icon: "clipboard", action: () => copyPath(path) },
        { separator: true },
        { label: "Copy", icon: "copy", action: () => doCopy(path, isDir) },
        { label: "Move", icon: "arrow-right-left", action: () => doMove(path) },
        { label: "Delete", icon: "trash-2", danger: true, action: () => doDelete(path) },
      ]);
    } else {
      // Right-clicked on empty space
      const targetDir = rootPath || "/";
      showCtx(e.clientX, e.clientY, [
        { label: "New File", icon: "file-plus-2", action: () => createIn(targetDir, "file") },
        { label: "New Folder", icon: "folder-plus", action: () => createIn(targetDir, "dir") },
        { label: "Copy Path", icon: "clipboard", action: () => copyPath(targetDir) },
      ]);
    }
  });

  /* ---------- Context menu actions ---------- */
  function copyPath(path) {
    navigator.clipboard.writeText(path).catch(() => {});
  }

  async function createIn(dir, type) {
    const label = type === "file" ? "New file name:" : "New folder name:";
    const name = prompt(label);
    if (!name || !name.trim()) return;
    const fullPath = dir + "/" + name.trim();
    const endpoint = type === "file" ? "/api/filesystem/write" : "/api/filesystem/mkdir";
    const body = type === "file" ? { path: fullPath, content: "" } : { path: fullPath };
    try {
      const res = await fetch(endpoint, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      const data = await res.json();
      if (data.error) alert(data.error);
      else loadTree();
    } catch { alert("Failed"); }
  }

  async function doCopy(path, isDir) {
    const name = prompt("Copy to (full destination path):", path + (isDir ? "_copy" : ".copy"));
    if (!name) return;
    try {
      const res = await fetch("/api/filesystem/copy", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path, dest: name }) });
      const data = await res.json();
      if (data.error) alert(data.error);
      else loadTree();
    } catch { alert("Copy failed"); }
  }

  async function doMove(path) {
    const name = prompt("Move to (full destination path):", path);
    if (!name) return;
    try {
      const res = await fetch("/api/filesystem/move", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path, dest: name }) });
      const data = await res.json();
      if (data.error) alert(data.error);
      else loadTree();
    } catch { alert("Move failed"); }
  }

  async function doDelete(path) {
    if (!confirm(`Delete "${path.split("/").pop()}"? This cannot be undone.`)) return;
    try {
      const res = await fetch("/api/filesystem/delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path }) });
      const data = await res.json();
      if (data.error) alert(data.error);
      else loadTree();
    } catch { alert("Delete failed"); }
  }

  /* ---------- Utilities ---------- */
  function shorten(p) {
    return p.replace(/^\/home\/sifat/, "~");
  }

  function esc(str) {
    const d = document.createElement("div");
    d.textContent = str;
    return d.innerHTML;
  }

  function formatSize(bytes) {
    if (bytes === 0) return "0 B";
    const units = ["B", "KB", "MB", "GB"];
    let i = 0, size = bytes;
    while (size >= 1024 && i < units.length - 1) { size /= 1024; i++; }
    return size.toFixed(i === 0 ? 0 : 1) + " " + units[i];
  }

  /* ==========================================================
     SIDEBAR FILE TREE (right panel)
     ========================================================== */
  const sidebarTree = document.getElementById("fsSidebarTree");
  let sidebarRoot = "";
  let sidebarExpanded = new Set();

  const SB_BASE = "/api/filesystem";
  function sbLoadRecent() {
    return JSON.parse(localStorage.getItem("fs_root_history") || "[]");
  }

  // Sidebar-specific fetch (doesn't depend on rootPath)
  async function sbApi(sub) {
    const res = await fetch(SB_BASE + sub);
    return await res.json();
  }

  async function showSidebarPicker() {
    const dirs = await sbApi("/roots");
    const recents = sbLoadRecent();

    sidebarTree.innerHTML = "";

    if (recents.length) {
      const label = document.createElement("div");
      label.className = "fs-pick-label";
      label.innerHTML = `${icon("history", 12)} Recent`;
      sidebarTree.appendChild(label);
      recents.forEach((p) => {
        const item = document.createElement("div");
        item.className = "fs-item";
        item.innerHTML = `<span class="fs-icon">${icon("folder", 14)}</span><span class="fs-name">${esc(shorten(p))}</span>`;
        item.addEventListener("click", () => pickSidebarRoot(p));
        sidebarTree.appendChild(item);
      });
    }

    const rootsLabel = document.createElement("div");
    rootsLabel.className = "fs-pick-label";
    rootsLabel.innerHTML = `${icon("hard-drive", 12)} Quick Access`;
    sidebarTree.appendChild(rootsLabel);

    if (dirs.length) {
      dirs.forEach((d) => {
        const item = document.createElement("div");
        item.className = "fs-item";
        item.innerHTML = `<span class="fs-icon">${icon("folder", 14)}</span><span class="fs-name">${esc(d.name || shorten(d.path))}</span>`;
        item.addEventListener("click", () => pickSidebarRoot(d.path));
        sidebarTree.appendChild(item);
      });
    } else {
      const empty = document.createElement("div");
      empty.className = "fs-item";
      empty.style.color = "var(--muted)";
      empty.textContent = "No saved roots";
      sidebarTree.appendChild(empty);
    }
    refreshIcons();
  }

  function pickSidebarRoot(path) {
    sidebarRoot = path;
    sidebarExpanded.clear();
    localStorage.setItem("fs_ide_last_folder", path);
    loadSidebarTree();
    openRoot(path);
  }

  async function loadSidebarTree() {
    if (!sidebarTree || !sidebarRoot) {
      showSidebarPicker();
      return;
    }
    const data = await sbApi("/list?path=" + encodeURIComponent(sidebarRoot));
    const items = data.items || [];
    if (!items.length) return;
    sidebarTree.innerHTML = "";

    const backItem = document.createElement("div");
    backItem.className = "fs-item fs-back";
    backItem.innerHTML = `${icon("arrow-left", 12)} <span class="fs-name">← Roots</span>`;
    backItem.addEventListener("click", () => showSidebarPicker());
    sidebarTree.appendChild(backItem);

    const rootItem = document.createElement("div");
    rootItem.className = "fs-item fs-root";
    rootItem.innerHTML = `<span class="fs-icon">${icon("folder-open", 14)}</span><span class="fs-name">${esc(sidebarRoot.split("/").pop() || "/")}</span>`;
    sidebarTree.appendChild(rootItem);

    const inner = document.createElement("div");
    sidebarTree.appendChild(inner);
    const frag = document.createDocumentFragment();
    for (const it of items) frag.appendChild(buildSidebarNode(it, 0));
    inner.appendChild(frag);
    refreshIcons();

    // Recursively expand directories already in sidebarExpanded
    await expandSidebarDirs(inner, 0);
  }

  async function expandSidebarDirs(container, depth) {
    const dirs = container.querySelectorAll(":scope > .fs-item.fs-dir");
    for (const row of dirs) {
      const path = row.dataset.path;
      if (!sidebarExpanded.has(path)) continue;
      // Already rendered children? skip
      if (container.querySelector(`:scope > [data-children="${CSS.escape(path)}"]`)) continue;
      const data = await sbApi("/list?path=" + encodeURIComponent(path));
      const children = data.items || [];
      if (!children.length) continue;
      const childWrap = document.createElement("div");
      childWrap.dataset.children = path;
      for (const it of children) childWrap.appendChild(buildSidebarNode(it, depth + 1));
      row.after(childWrap);
      // Recurse into this child container
      await expandSidebarDirs(childWrap, depth + 1);
    }
    refreshIcons();
  }

  function buildSidebarNode(item, depth) {
    const row = document.createElement("div");
    const isDir = item.is_dir || item.type === "dir";
    row.className = "fs-item" + (isDir ? " fs-dir" : "");
    row.style.paddingLeft = (10 + depth * 14) + "px";
    row.dataset.path = item.path;
    row.dataset.type = isDir ? "dir" : "file";

    const iconName = isDir
      ? (sidebarExpanded.has(item.path) ? "folder-open" : "folder")
      : fileIcon(item.name);
    const dirtyDot = isDir ? "" : `<span class="fs-dirty-dot" style="display:none;">●</span>`;
    row.innerHTML = `<span class="fs-icon">${icon(iconName, 14)}</span><span class="fs-name">${esc(item.name)}</span>${dirtyDot}`;

    if (isDir) {
      row.addEventListener("click", () => toggleSidebarDir(item.path, depth));
    } else {
      row.addEventListener("click", () => openSidebarFile(item.path, item.name));
    }
    return row;
  }

  async function toggleSidebarDir(path, depth) {
    if (sidebarExpanded.has(path)) {
      sidebarExpanded.delete(path);
      const container = sidebarTree.querySelector(`[data-children="${path}"]`);
      if (container) container.remove();
      // Update folder icon
      const row = sidebarTree.querySelector(`.fs-item[data-path="${path}"]`);
      if (row) row.innerHTML = `<span class="fs-icon">${icon("folder", 14)}</span><span class="fs-name">${esc(path.split("/").pop())}</span>`;
      refreshIcons();
      return;
    }

    sidebarExpanded.add(path);
    const data = await sbApi("/list?path=" + encodeURIComponent(path));
    const items = data.items || [];
    if (!items.length) return;

    const row = sidebarTree.querySelector(`.fs-item[data-path="${path}"]`);
    if (!row) return;
    // Update icon to open
    row.innerHTML = `<span class="fs-icon">${icon("folder-open", 14)}</span><span class="fs-name">${esc(path.split("/").pop())}</span>`;

    const container = document.createElement("div");
    container.dataset.children = path;
    for (const it of items) container.appendChild(buildSidebarNode(it, depth + 1));
    row.after(container);
    refreshIcons();
  }

  async function openSidebarFile(path, name) {
    const data = await sbApi("/read?path=" + encodeURIComponent(path));
    if (!data || data.error) return;
    currentFilePath = path;
    isDirty = false;
    syncSidebarDirty();
    localStorage.setItem("fs_ide_last_file", path);

    if (data.binary) {
      showToast("Binary file — cannot preview", "error");
      return;
    }

    // Check for pending diff
    let diffData = null;
    try {
      const diffRes = await fetch(`/api/filesystem/pending-diff?path=${encodeURIComponent(path)}`);
      const d = await diffRes.json();
      if (d.has_diff) diffData = d;
    } catch { /* ignore */ }

    // IDE mode: render into the center editor pane
    if (document.body.getAttribute("data-mode") === "ide") {
      const editorContainer = document.getElementById("editorContainer");
      const editorEmpty = document.getElementById("editorEmptyState");
      if (editorEmpty) editorEmpty.classList.add("hidden");
      if (editorContainer) {
        editorContainer.classList.remove("hidden");
        if (diffData) {
          renderDiffReview(data, diffData, editorContainer);
        } else {
          renderEditor(data, editorContainer);
        }
      }
      highlightSidebarFile(path);
      return;
    }

    // Agent mode: open the overlay, synced with sidebar folder
    const syncRoot = sidebarRoot || path.split("/").slice(0, -1).join("/") || "/";
    if (rootPath !== syncRoot) {
      rootPath = syncRoot;
      expandedDirs.clear();
      expandedDirs.add(rootPath);
      pathBar.textContent = rootPath;
    }
    // Expand parent dirs so the opened file is visible in tree
    const fileParts = path.split("/");
    for (let i = 1; i < fileParts.length - 1; i++) {
      expandedDirs.add(fileParts.slice(0, i + 1).join("/"));
    }
    openFS();
    await loadTree();
    // Highlight the opened file in the overlay tree
    const overlayRow = treeEl.querySelector(`.fs-item[data-path="${path}"]`);
    if (overlayRow) {
      if (activeFileEl) activeFileEl.classList.remove("fs-active");
      overlayRow.classList.add("fs-active");
      activeFileEl = overlayRow;
    }
    if (diffData) {
      renderDiffReview(data, diffData);
    } else {
      renderEditor(data);
    }
  }

  function highlightSidebarFile(path) {
    sidebarTree.querySelectorAll(".fs-item.fs-active").forEach(el => el.classList.remove("fs-active"));
    const row = sidebarTree.querySelector(`.fs-item[data-path="${path}"]`);
    if (row) row.classList.add("fs-active");
  }

  function syncSidebarDirty() {
    // Hide all dots first, then show only if current file is dirty
    sidebarTree.querySelectorAll(".fs-dirty-dot").forEach(d => d.style.display = "none");
    if (isDirty && currentFilePath) {
      const row = sidebarTree.querySelector(`.fs-item[data-path="${CSS.escape(currentFilePath)}"]`);
      if (row) {
        const dot = row.querySelector(".fs-dirty-dot");
        if (dot) dot.style.display = "inline";
      }
    }
  }

  /* ---------- IDE CWD + open file getters (for auto-inject into chat) ---------- */
  window.getIdeCwd = function () {
    return sidebarRoot || localStorage.getItem("fs_ide_last_folder") || "";
  };
  window.getIdeOpenFile = function () {
    return currentFilePath || "";
  };

  /* ---------- Open file at position (Problems panel jump) ---------- */
  window.openIdeFileAt = async function (filePath, line, col) {
    if (document.body.dataset.mode !== "ide") {
      const ideBtn = document.getElementById("layoutIde");
      if (ideBtn) ideBtn.click();
    }
    try {
      if (filePath && filePath !== currentFilePath) {
        await openSidebarFile(filePath, filePath.split("/").pop());
      }
      if (monacoEditor && line) {
        const pos = { lineNumber: line, column: col || 1 };
        monacoEditor.revealPositionInCenter(pos);
        monacoEditor.setPosition(pos);
        monacoEditor.focus();
      }
    } catch { /* file may be gone — ignore */ }
  };

  /* ---------- Live refresh: re-fetch open file when agent edits it ---------- */
  window.refreshIdeFile = async function (filePath) {
    if (!filePath || filePath !== currentFilePath || isDirty) return;
    try {
      const res = await fetch("/api/filesystem/read?path=" + encodeURIComponent(filePath));
      const data = await res.json();
      if (data.error || data.binary) return;

      // Check for pending diff — show inline review instead of silent update
      try {
        const diffRes = await fetch(`/api/filesystem/pending-diff?path=${encodeURIComponent(filePath)}`);
        const diffData = await diffRes.json();
        if (diffData.has_diff) {
          const isIde = document.body.getAttribute("data-mode") === "ide";
          const targetEl = isIde ? document.getElementById("editorContainer") : null;
          renderDiffReview(data, diffData, targetEl);
          return;
        }
      } catch { /* fall through to normal update */ }

      if (monacoEditor) {
        const model = monacoEditor.getModel();
        if (model && model.getValue() !== data.content) {
          model.setValue(data.content);
          isDirty = false;
          const dirtyDot = document.getElementById("fsDirtyDot");
          if (dirtyDot) dirtyDot.style.display = "none";
        }
      }
    } catch { /* silent — file may have been deleted */ }
  };

  /* ---------- IDE Session Restore ---------- */
  window.restoreIdeSession = async function () {
    const lastFolder = localStorage.getItem("fs_ide_last_folder");
    const lastFile = localStorage.getItem("fs_ide_last_file");
    if (!lastFolder) return;

    // Restore folder tree
    sidebarRoot = lastFolder;
    sidebarExpanded.clear();
    sidebarExpanded.add(lastFolder);

    // Expand all parent dirs leading to the last opened file
    if (lastFile) {
      const fileDir = lastFile.substring(0, lastFile.lastIndexOf("/"));
      let dir = fileDir;
      while (dir.length > lastFolder.length && dir.startsWith(lastFolder)) {
        sidebarExpanded.add(dir);
        dir = dir.substring(0, dir.lastIndexOf("/"));
      }
    }

    saveHistory(lastFolder);
    await loadSidebarTree();

    // Restore last opened file
    if (lastFile) {
      try {
        const data = await sbApi("/read?path=" + encodeURIComponent(lastFile));
        if (data && !data.error && !data.binary) {
          currentFilePath = lastFile;
          isDirty = false;
          const editorContainer = document.getElementById("editorContainer");
          const editorEmpty = document.getElementById("editorEmptyState");
          if (editorEmpty) editorEmpty.classList.add("hidden");
          if (editorContainer) {
            editorContainer.classList.remove("hidden");
            renderEditor(data, editorContainer);
          }
          highlightSidebarFile(lastFile);
        }
      } catch { /* file may have been deleted */ }
    }
  };

  /* ---------- Sidebar header: Open Folder button ---------- */
  const sbHeaderOpenBtn = document.getElementById("sbOpenFolderBtn");
  if (sbHeaderOpenBtn) {
    sbHeaderOpenBtn.addEventListener("click", async () => {
      sbHeaderOpenBtn.disabled = true;
      try {
        const res = await fetch(SB_BASE + "/pick-folder");
        const data = await res.json();
        if (data.path) pickSidebarRoot(data.path);
      } catch {}
      sbHeaderOpenBtn.disabled = false;
    });
  }

  /* ---------- Sidebar tab switching ---------- */
  const sidebarTabs = document.querySelectorAll(".fs-sidebar-tab");
  const filesPanel = document.getElementById("sidebarFilesPanel");
  const diffPanel = document.getElementById("sidebarDiffPanel");

  sidebarTabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      sidebarTabs.forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      const panel = tab.dataset.panel;
      if (filesPanel) filesPanel.classList.toggle("active", panel === "files");
      if (diffPanel) diffPanel.classList.toggle("active", panel === "diff");
      if (panel === "files") {
        sidebarRoot ? loadSidebarTree() : showSidebarPicker();
      }
    });
  });


  /* ---------- Diff editor (Monaco split view) ---------- */
  let diffEditor = null;
  let isDiffOpen = false;

  window.openDiffEditor = async function (path, backupPath, fileName) {
    await loadMonaco();

    const [origRes, modRes] = await Promise.all([
      fetch("/api/filesystem/read?path=" + encodeURIComponent(backupPath)),
      fetch("/api/filesystem/read?path=" + encodeURIComponent(path)),
    ]);
    const origData = await origRes.json();
    const modData = await modRes.json();

    if (origData.binary || modData.binary) {
      showToast("Cannot diff binary files", "error");
      return;
    }

    openFS();
    isDiffOpen = true;
    if (diffEditor) { diffEditor.dispose(); diffEditor = null; }

    viewerEl.innerHTML = `
      <div class="fs-viewer-header">
        <span class="fs-viewer-name">${icon("git-compare", 14)} <span>${esc(fileName || path.split("/").pop())}</span> <span style="color:var(--text-dim);font-size:11px;">(diff)</span></span>
        <span class="fs-viewer-meta">before \u2194 after</span>
      </div>
      <div class="fs-monaco-wrap" id="fsDiffWrap"></div>
    `;

    const wrapEl = viewerEl.querySelector("#fsDiffWrap");
    const ext = (fileName || "").split(".").pop() || "";

    const originalModel = monaco.editor.createModel(origData.content, monacoLang(ext));
    const modifiedModel = monaco.editor.createModel(modData.content, monacoLang(ext));

    defineSableMonacoTheme();
    monaco.editor.setTheme("sable-dark");

    diffEditor = monaco.editor.createDiffEditor(wrapEl, {
      fontSize: getEditorFontSize(),
      fontFamily: "JetBrains Mono, Fira Code, monospace",
      readOnly: true,
      renderSideBySide: true,
      minimap: { enabled: false },
      scrollBeyondLastLine: false,
      automaticLayout: true,
    });
    diffEditor.setModel({ original: originalModel, modified: modifiedModel });
    refreshIcons();
  };

  /* ---------- Init: populate right sidebar on load ---------- */
  const savedFolder = localStorage.getItem("fs_ide_last_folder");
  if (savedFolder) {
    sidebarRoot = savedFolder;
    sidebarExpanded.clear();
    sidebarExpanded.add(savedFolder);
    loadSidebarTree();
  } else {
    showSidebarPicker();
  }
})();
