// preview-pane.js — the right-hand review & preview panel.
//
// Tabs: "Files Changed" (always present) plus any number of page tabs.
// Each page tab carries its own source, html, and (optionally) a real URL.
//
// The page itself runs in a sandboxed iframe. Local files render from a
// blob: URL; http(s) URLs load directly in the frame.
(function () {
  "use strict";

  var WIDTH_KEY = "sable_preview_w";
  var MIN_W = 360;
  var DEFAULT_W = 700;
  var MAX_DIFF_LINES = 3000;

  var root = null;
  var iframe = null;
  var codeEl = null;
  var blobUrl = null;
  var zoom = 1;
  var seq = 0;

  var state = {
    open: false,
    tab: "changes",       // "changes" | page id
    face: "web",
    pages: [],            // [{ id, title, html, source, path, url }]
    changes: [],
    edits: {},            // path -> { name, path, backup_path, added, removed, at }
    editOrder: [],        // paths, newest first
    editors: [],
    selected: null,
    changesSeen: false,
  };

  var EDITORS = [
    { id: "filemanager", label: "File Manager" },
    { id: "vscode", label: "VS Code" },
    { id: "cursor", label: "Cursor" },
    { id: "zed", label: "Zed" },
    { id: "sublime", label: "Sublime Text" },
  ];

  /* ---------------------------------------------------------------- utils */

  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function icons() {
    if (window.lucide && window.lucide.createIcons) {
      try { window.lucide.createIcons({ nodes: [root] }); } catch (e) {}
    }
  }

  function toast(msg, kind) {
    if (typeof window.showToast === "function") window.showToast(msg, kind || "info");
  }

  function savedWidth() {
    var n = parseInt(localStorage.getItem(WIDTH_KEY) || "", 10);
    return clamp(isNaN(n) ? DEFAULT_W : n, MIN_W, Math.max(MIN_W, window.innerWidth - 260));
  }

  function $(sel) { return root ? root.querySelector(sel) : null; }
  function $$(sel) { return root ? root.querySelectorAll(sel) : []; }

  function activePage() {
    for (var i = 0; i < state.pages.length; i++) {
      if (state.pages[i].id === state.tab) return state.pages[i];
    }
    return null;
  }

  /* ----------------------------------------------------------------- diff */

  function unifiedDiff(original, modified) {
    var a = String(original || "").split("\n");
    var b = String(modified || "").split("\n");

    if (a.length > MAX_DIFF_LINES || b.length > MAX_DIFF_LINES) {
      return [{ type: "note", text: "File too large to diff inline — open it in the editor." }];
    }

    var n = a.length, m = b.length;
    var dp = [];
    for (var i = 0; i <= n; i++) dp.push(new Uint32Array(m + 1));
    for (i = n - 1; i >= 0; i--) {
      for (var j = m - 1; j >= 0; j--) {
        dp[i][j] = a[i] === b[j]
          ? dp[i + 1][j + 1] + 1
          : Math.max(dp[i + 1][j], dp[i][j + 1]);
      }
    }

    var out = [];
    i = 0; j = 0;
    var an = 1, bn = 1;
    while (i < n && j < m) {
      if (a[i] === b[j]) {
        out.push({ type: "same", a: an++, b: bn++, text: a[i] }); i++; j++;
      } else if (dp[i + 1][j] >= dp[i][j + 1]) {
        out.push({ type: "del", a: an++, text: a[i] }); i++;
      } else {
        out.push({ type: "add", b: bn++, text: b[j] }); j++;
      }
    }
    while (i < n) out.push({ type: "del", a: an++, text: a[i++] });
    while (j < m) out.push({ type: "add", b: bn++, text: b[j++] });
    return out;
  }

  function renderDiff(original, modified) {
    var rows = unifiedDiff(original, modified);
    var added = 0, removed = 0, html = [];

    for (var k = 0; k < rows.length; k++) {
      var r = rows[k];
      if (r.type === "add") added++;
      if (r.type === "del") removed++;
      if (r.type === "note") {
        html.push('<div class="pp-empty">' + esc(r.text) + '</div>');
        continue;
      }
      html.push(
        '<div class="pp-dl pp-dl-' + r.type + '">' +
          '<span class="pp-dln">' + (r.a || "") + '</span>' +
          '<span class="pp-dln">' + (r.b || "") + '</span>' +
          '<span class="pp-dlc">' + (r.type === "add" ? "+" : r.type === "del" ? "\u2212" : " ") + '</span>' +
          '<span class="pp-dlt">' + esc(r.text) + '</span>' +
        '</div>'
      );
    }

    return {
      html: html.join("") || '<div class="pp-empty">No differences.</div>',
      added: added,
      removed: removed,
    };
  }

  /* ------------------------------------------------------------------ DOM */

  var SHELL =
    '<div class="pp-resize" title="Drag to resize"></div>' +

    '<div class="pp-tabstrip">' +
      '<div class="pp-tabs" role="tablist">' +
        '<button class="pp-tab pp-tab-changes" role="tab" data-tab="changes">' +
          '<i data-lucide="git-compare" class="icon-lucide"></i>' +
          '<span>Files Changed</span>' +
          '<span class="pp-badge" data-badge hidden>0</span>' +
        '</button>' +
        '<div class="pp-page-tabs" data-page-tabs></div>' +
      '</div>' +
      '<button class="pp-icon-btn" data-act="new-tab" title="New tab">' +
        '<i data-lucide="plus" class="icon-lucide"></i>' +
      '</button>' +
      '<button class="pp-icon-btn" data-act="close" title="Close panel">' +
        '<i data-lucide="x" class="icon-lucide"></i>' +
      '</button>' +
    '</div>' +

    '<div class="pp-view" data-view="changes">' +
      '<div class="pp-changes-head">' +
        '<span class="pp-changes-title">Changed files</span>' +
        '<span class="pp-changes-sub" data-changes-sub></span>' +
        '<button class="pp-icon-btn" data-act="refresh-changes" title="Refresh">' +
          '<i data-lucide="refresh-cw" class="icon-lucide"></i>' +
        '</button>' +
      '</div>' +
      '<div class="pp-change-list" data-change-list>' +
        '<div class="pp-empty">Looking for edits…</div>' +
      '</div>' +
    '</div>' +

    '<div class="pp-view" data-view="page" hidden>' +
      '<div class="pp-toolbar">' +
        '<button class="pp-icon-btn" data-act="reload" title="Reload">' +
          '<i data-lucide="rotate-cw" class="icon-lucide"></i></button>' +
        '<input class="pp-address" data-address spellcheck="false" autocomplete="off" placeholder="Paste a URL or a file path" />' +
        '<div class="pp-seg" role="tablist">' +
          '<button class="pp-seg-btn active" data-face="web" title="Rendered page">' +
            '<i data-lucide="globe" class="icon-lucide"></i><span>Preview</span></button>' +
          '<button class="pp-seg-btn" data-face="code" title="Source">' +
            '<i data-lucide="code-2" class="icon-lucide"></i><span>Code</span></button>' +
        '</div>' +
        '<button class="pp-icon-btn" data-act="menu" title="More">' +
          '<i data-lucide="more-horizontal" class="icon-lucide"></i></button>' +
        '<button class="pp-openin-trigger" data-act="openin" title="Open in…">' +
          '<span>Open in</span>' +
          '<i data-lucide="chevron-down" class="icon-lucide"></i></button>' +
      '</div>' +
      '<div class="pp-face" data-face-panel="web">' +
        '<iframe class="pp-frame" sandbox="allow-scripts allow-forms allow-modals allow-popups allow-downloads allow-same-origin" referrerpolicy="no-referrer"></iframe>' +
        // Electron only: a DOM placeholder the main process parks a real
        // WebContentsView over. It sits exactly where the iframe would be,
        // so every toolbar button, tab and toggle above keeps working.
        '<div class="pp-native-host" hidden></div>' +
        '<div class="pp-blocked" hidden>' +
          '<p>This site refuses to be embedded.</p>' +
          '<p class="pp-dim">Google, GitHub and most banks send X-Frame-Options — the browser blocks them, not us.</p>' +
          '<button class="pp-view-btn" data-act="external-now">Open in browser</button>' +
        '</div>' +
      '</div>' +
      '<div class="pp-face" data-face-panel="code" hidden>' +
        '<pre class="pp-code"><code></code></pre>' +
      '</div>' +
    '</div>' +

    '<div class="pp-popover pp-menu" data-popover="menu" hidden>' +
      '<button class="pp-menu-item" data-menu="zoom-in"><span>Zoom in</span><kbd>Ctrl +</kbd></button>' +
      '<button class="pp-menu-item" data-menu="zoom-out"><span>Zoom out</span><kbd>Ctrl -</kbd></button>' +
      '<button class="pp-menu-item" data-menu="zoom-reset"><span>Reset zoom</span><kbd data-zoom-label>100%</kbd></button>' +
      '<div class="pp-menu-sep"></div>' +
      '<button class="pp-menu-item" data-menu="copy-link"><span>Copy link</span></button>' +
      '<button class="pp-menu-item" data-menu="external"><span>Open in system browser</span></button>' +
      '<div class="pp-menu-sep"></div>' +
      '<button class="pp-menu-item pp-danger" data-menu="clear-history"><span>Clear browsing history</span></button>' +
    '</div>' +

    '<div class="pp-popover pp-openin" data-popover="openin" hidden></div>';

  function build() {
    if (root) return;
    var existing = document.getElementById("previewPane");
    if (existing) {
      root = existing;
      root.innerHTML = SHELL;
    } else {
      root = document.createElement("aside");
      root.id = "previewPane";
      root.className = "preview-pane";
      root.setAttribute("aria-hidden", "true");
      root.innerHTML = SHELL;
      document.body.appendChild(root);
    }
    wire();
    if (root) renderChangeCards();
  }

  function wire() {
    iframe = $(".pp-frame");
    codeEl = $(".pp-code code");
    nativeHost = $(".pp-native-host");
    bindNative();

    root.addEventListener("click", onClick);
    bindAddress();
    bindResize();
    bindKeys();

    document.addEventListener("mousedown", function (e) {
      if (!e.target.closest(".pp-popover") &&
          !e.target.closest('[data-act="menu"]') &&
          !e.target.closest('[data-act="openin"]')) {
        closePopovers();
      }
    });

    icons();
  }

  function onClick(e) {
    // The tab close affordance is a <span> inside a <button>; catch it first
    // or the button guard below would swallow the click.
    var closer = e.target.closest(".pp-tab-close");
    if (closer && root.contains(closer)) {
      e.preventDefault();
      e.stopPropagation();
      closeTab(closer.getAttribute("data-for"));
      return;
    }

    var el = e.target.closest("button");
    if (!el || !root.contains(el)) return;

    // ── tab buttons ──
    if (el.classList.contains("pp-tab")) {
      var t = el.getAttribute("data-tab");
      if (t) setTab(t);
      return;
    }

    var face = el.getAttribute("data-face");
    if (face) { setFace(face); return; }

    var openin = el.getAttribute("data-openin");
    if (openin) { launchIn(openin); return; }

    var menu = el.getAttribute("data-menu");
    if (menu) { menuAction(menu); return; }

    // Any click inside a card head toggles it; the View button is just a cue.
    var card = el.closest(".pp-card");
    if (card && el.closest(".pp-card-head")) {
      toggleCard(card);
      return;
    }
    if (el.classList.contains("pp-accept")) { resolveEdit(el, "accept"); return; }
    if (el.classList.contains("pp-reject")) { resolveEdit(el, "reject"); return; }

    switch (el.getAttribute("data-act")) {
      case "close": close(); break;
      case "reload": reload(); break;
      case "refresh-changes": loadChanges(); break;
      case "menu": togglePopover("menu"); break;
      case "openin": togglePopover("openin"); break;
      case "new-tab": newTab(); break;
      case "external-now": openExternal(); break;
    }
  }

  function bindKeys() {
    document.addEventListener("keydown", function (e) {
      if (!state.open) return;
      if (e.key === "Escape") {
        var any = false;
        var ps = $$(".pp-popover");
        for (var i = 0; i < ps.length; i++) if (!ps[i].hidden) any = true;
        if (any) { closePopovers(); return; }
        close();
      }
    });
  }

  /* --------------------------------------------------------------- address */

  function bindAddress() {
    var addr = $("[data-address]");
    if (!addr) return;

    addr.addEventListener("keydown", function (e) {
      if (e.key === "Enter") {
        e.preventDefault();
        navigate(addr.value.trim());
        addr.blur();
      }
    });

    // Select-all on focus, like a real browser.
    addr.addEventListener("focus", function () { addr.select(); });
  }

  function navigate(input) {
    input = (input || "").trim();
    if (!input) return;

    var page = activePage() || addPage({ title: "New tab" });

    // 1. Full URL.
    if (/^https?:\/\//i.test(input)) {
      gotoUrl(page, input);
      return;
    }

    // 2. Anything that looks like a host (example.com, sub.example.io/path).
    //    A trailing slash or a dot-suffix is the tell; "main.py" is not a host.
    var hostish = /^[\w-]+(\.[\w-]+)+(\/.*)?$/i.test(input) && !input.startsWith("/");
    if (hostish) {
      gotoUrl(page, "https://" + input);
      return;
    }

    // 3. Absolute path to something on this machine.
    if (input.startsWith("/") || input.startsWith("~")) {
      openPath(input, page);
      return;
    }

    // 4. Relative path that exists → file. Otherwise it is a search.
    var looksFile = /\.[a-z0-9]{1,6}$/i.test(input) || input.includes("/");
    if (looksFile) {
      openPath(input, page, true);
      return;
    }

    // 5. Plain words → search the web.
    gotoUrl(page, "https://www.google.com/search?q=" + encodeURIComponent(input));
  }

  function gotoUrl(page, url) {
    page.url = url;
    page.path = "";
    page.html = "";
    page.source = "";
    page.title = url.replace(/^https?:\/\//i, "").split("/")[0];

    var addr = $("[data-address]");
    if (addr) addr.value = url;

    renderTabs();
    activatePage(page);
  }

  function openPath(path, targetPage, quiet) {
    if (!path) return;

    // Relative paths resolve against the project the IDE has open.
    if (!path.startsWith("/") && !path.startsWith("~")) {
      var cwd = "";
      try { if (typeof window.getIdeCwd === "function") cwd = window.getIdeCwd() || ""; } catch (e) {}
      if (!cwd) cwd = localStorage.getItem("fs_ide_last_folder") || "";
      if (cwd) path = cwd.replace(/\/+$/, "") + "/" + path.replace(/^\.\//, "");
    }

    fetch("/api/filesystem/read?path=" + encodeURIComponent(path))
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.error || data.binary) {
          if (quiet) {
            // Not a file — treat the input as a search.
            var p = targetPage || activePage() || addPage({ title: "New tab" });
            gotoUrl(p, "https://www.google.com/search?q=" + encodeURIComponent(path));
            return;
          }
          toast(data.error || "Binary file — cannot preview", "error");
          return;
        }

        var name = path.split("/").pop() || path;
        var html = data.content;

        if (/\.html?$/i.test(path)) {
          // Resolve sibling assets through serve-dir.
          var dir = path.slice(0, path.lastIndexOf("/"));
          var base = '<base href="/api/filesystem/serve-dir/?base=' + encodeURIComponent(dir) + '&">';
          html = /<head[^>]*>/i.test(html)
            ? html.replace(/<head([^>]*)>/i, '<head$1>' + base)
            : base + html;
        }

        var existing = null;
        for (var i = 0; i < state.pages.length; i++) {
          if (state.pages[i].path === path) existing = state.pages[i];
        }

        var page = existing || targetPage || addPage({ title: name, path: path });
        page.title = name;
        page.path = path;
        page.html = html;
        page.source = data.content;
        page.url = "";
        renderTabs();
        activatePage(page);
      })
      .catch(function () { toast("Could not read " + path, "error"); });
  }

  /* ----------------------------------------------------------------- tabs */

  function addPage(opts) {
    var page = {
      id: "pg" + (++seq),
      title: opts.title || "Preview",
      html: opts.html || "",
      source: opts.source != null ? opts.source : (opts.html || ""),
      path: opts.path || "",
      url: opts.url || "",
    };
    state.pages.push(page);
    renderTabs();
    return page;
  }

  function newTab() {
    var page = addPage({ title: "New tab" });
    activatePage(page);
    var addr = $("[data-address]");
    if (addr) { addr.value = ""; addr.focus(); }
  }

  function closeTab(id) {
    var idx = -1;
    for (var i = 0; i < state.pages.length; i++) if (state.pages[i].id === id) idx = i;
    if (idx < 0) return;

    state.pages.splice(idx, 1);

    if (state.tab === id) {
      var next = state.pages[Math.max(0, idx - 1)];
      if (next) activatePage(next);
      else setTab("changes");
    }
    renderTabs();
  }

  function renderTabs() {
    var host = $("[data-page-tabs]");
    if (!host) return;

    var html = "";
    for (var i = 0; i < state.pages.length; i++) {
      var p = state.pages[i];
      html +=
        '<button class="pp-tab pp-tab-page' + (p.id === state.tab ? " active" : "") + '" ' +
          'role="tab" data-tab="' + p.id + '" title="' + esc(p.url || p.path || p.title) + '">' +
          '<i data-lucide="' + (p.url ? "globe" : "file-code-2") + '" class="icon-lucide"></i>' +
          '<span>' + esc(p.title) + '</span>' +
          '<span class="pp-tab-close" data-for="' + p.id + '"><i data-lucide="x"></i></span>' +
        '</button>';
    }
    host.innerHTML = html;

    // The Files Changed tab only looks active on that view.
    var fc = $('.pp-tab-changes');
    if (fc) fc.classList.toggle("active", state.tab === "changes");

    icons();
  }

  function setTab(id) {
    state.tab = id;
    if (id === "changes") state.changesSeen = true;

    var page = activePage();
    var isChanges = id === "changes";

    var views = $$(".pp-view");
    for (var i = 0; i < views.length; i++) {
      var v = views[i].getAttribute("data-view");
      views[i].hidden = (v === "changes") ? !isChanges : isChanges;
    }

    renderTabs();
    closePopovers();

    if (isChanges) { hideNative(); loadChanges(); return; }
    if (page) activatePage(page);
  }

  function activatePage(page) {
    state.tab = page.id;

    var views = $$(".pp-view");
    for (var i = 0; i < views.length; i++) {
      views[i].hidden = views[i].getAttribute("data-view") === "changes";
    }

    var addr = $("[data-address]");
    if (addr) addr.value = page.url || page.path || "";

    if (codeEl) codeEl.textContent = page.source || "(no source)";

    loadPage(page);
    renderTabs();
    setFace(state.face);
    applyZoom();
  }

  /* ---------------------------------------------------- native browser */
  // In the Electron shell a real WebContentsView is layered over the DOM
  // where the iframe would be. That is the only way to show sites that send
  // X-Frame-Options / frame-ancestors — and the point of this pane.
  // Everything else (toolbar, tabs, Preview/Code toggle, open-in, zoom) is
  // unchanged and stays above the view.

  var nativeHost = null;
  var nativeRaf = null;
  var nativeOpen = false;

  function nativeAvailable() {
    return !!(window.sableDesktop && window.sableDesktop.hasNativeBrowser);
  }

  function measureNative() {
    if (!nativeHost) return null;
    var r = nativeHost.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return null;
    return { x: r.left, y: r.top, width: r.width, height: r.height };
  }

  function scheduleNativeBounds() {
    if (!nativeAvailable() || !nativeHost || nativeHost.hidden) return;
    if (nativeRaf) cancelAnimationFrame(nativeRaf);
    nativeRaf = requestAnimationFrame(function () {
      nativeRaf = null;
      var r = measureNative();
      if (!window.sableDesktop) return;
      if (!r) { window.sableDesktop.closeBrowser(); return; }
      window.sableDesktop.setBrowserBounds(r);
    });
  }

  function showNative() {
    if (!nativeAvailable() || !nativeHost) return;
    nativeHost.hidden = false;
    if (iframe) iframe.style.visibility = "hidden";
    nativeOpen = true;
    scheduleNativeBounds();
  }

  function hideNative() {
    if (!nativeAvailable()) return;
    if (iframe) iframe.style.visibility = "";
    if (nativeHost) nativeHost.hidden = true;
    nativeOpen = false;
    if (nativeRaf) { cancelAnimationFrame(nativeRaf); nativeRaf = null; }
    if (window.sableDesktop && window.sableDesktop.closeBrowser) window.sableDesktop.closeBrowser();
  }

  function openNative(url) {
    if (!nativeAvailable() || !nativeHost) return;
    showNative();
    // openBrowser is an async ipcRenderer.invoke — the view does not exist in
    // main until it resolves, so bounds sent earlier are dropped. Await it,
    // then measure, with a couple of retries for the laying-out frame.
    var p = window.sableDesktop.openBrowser(url);
    var done = function () {
      requestAnimationFrame(function () {
        scheduleNativeBounds();
        setTimeout(scheduleNativeBounds, 80);
        setTimeout(scheduleNativeBounds, 240);
      });
    };
    if (p && typeof p.then === "function") p.then(done, done);
    else done();
  }

  function bindNative() {
    if (!nativeAvailable()) return;

    if (nativeHost && typeof ResizeObserver === "function") {
      try {
        new ResizeObserver(function () { scheduleNativeBounds(); }).observe(nativeHost);
      } catch (e) {}
    }

    window.addEventListener("resize", scheduleNativeBounds);
    // The pane scrolls with the chat column; a scrolled ancestor moves the
    // host without resizing it, so bounds must follow that too.
    document.addEventListener("scroll", scheduleNativeBounds, true);

    if (window.sableDesktop.onBrowserState) {
      window.sableDesktop.onBrowserState(function (patch) {
        if (!patch) return;
        var page = activePage();
        if (!page) return;
        if (typeof patch.url === "string" && patch.url) {
          page.url = patch.url;
          page.title = patch.title || patch.url.replace(/^https?:\/\//i, "").split("/")[0];
          var addr = $("[data-address]");
          if (addr && document.activeElement !== addr) addr.value = patch.url;
          renderTabs();
        }
      });
    }
  }

  var loadGuard = null;

  function showBlocked(on) {
    var b = $(".pp-blocked");
    if (b) b.hidden = !on;
    if (iframe) iframe.style.visibility = on ? "hidden" : "";
  }

  function loadPage(page) {
    if (blobUrl) { try { URL.revokeObjectURL(blobUrl); } catch (e) {} blobUrl = null; }
    if (!iframe) return;

    if (loadGuard) { clearTimeout(loadGuard); loadGuard = null; }
    showBlocked(false);

    // Real URL → real Chromium view in the Electron shell, iframe on the web.
    if (page.url) {
      if (nativeAvailable()) {
        openNative(page.url);
        return;
      }
      hideNative();
      iframe.src = page.url;

      // A frame that stays cross-origin is unreadable, so we cannot ask it why
      // it failed. Give it a moment; if it never reports a same-origin body we
      // assume the site refused to be framed.
      loadGuard = setTimeout(function () {
        var blocked = true;
        try {
          var d = iframe.contentDocument;
          if (d && d.body && d.body.childElementCount > 0) blocked = false;
        } catch (e) { blocked = true; }
        if (blocked) showBlocked(true);
      }, 2500);
      return;
    }

    // Not a URL — a local file or inline HTML stays in the iframe.
    hideNative();

    if (!page.html) {
      iframe.srcdoc = '<body style="background:#0d0c0a;color:#85858c;font:13px system-ui;display:flex;align-items:center;justify-content:center;height:100vh;margin:0">Type a URL or a file path above</body>';
      return;
    }

    var doc = String(page.html);
    if (!/charset/i.test(doc)) {
      doc = /<head[^>]*>/i.test(doc)
        ? doc.replace(/<head([^>]*)>/i, '<head$1><meta charset="utf-8">')
        : '<meta charset="utf-8">' + doc;
    }
    blobUrl = URL.createObjectURL(new Blob([doc], { type: "text/html;charset=utf-8" }));
    iframe.src = blobUrl;
  }

  function setFace(face) {
    state.face = face;
    var btns = $$(".pp-seg-btn");
    for (var i = 0; i < btns.length; i++) {
      btns[i].classList.toggle("active", btns[i].getAttribute("data-face") === face);
    }
    var panels = $$("[data-face-panel]");
    for (i = 0; i < panels.length; i++) {
      panels[i].hidden = panels[i].getAttribute("data-face-panel") !== face;
    }
    // Code face hides the page; the native view must not float over it.
    if (face !== "web") hideNative();
    else scheduleNativeBounds();
  }

  function reload() {
    var page = activePage();
    if (!page || state.face === "code") return;
    if (page.url) { iframe.src = page.url; return; }
    if (blobUrl) {
      iframe.src = "about:blank";
      setTimeout(function () { iframe.src = blobUrl; }, 30);
    }
  }

  function applyZoom() {
    if (iframe) iframe.style.zoom = String(zoom);
    var lbl = $("[data-zoom-label]");
    if (lbl) lbl.textContent = Math.round(zoom * 100) + "%";
  }

  /* --------------------------------------------------------------- resize */

  function bindResize() {
    var grip = $(".pp-resize");
    if (!grip) return;

    grip.addEventListener("mousedown", function (e) {
      e.preventDefault();
      document.body.classList.add("pp-resizing");
      var startX = e.clientX;
      var startW = root.getBoundingClientRect().width;

      function move(ev) {
        var w = clamp(startW + (startX - ev.clientX), MIN_W, window.innerWidth - 260);
        root.style.width = w + "px";
      }
      function up() {
        document.body.classList.remove("pp-resizing");
        document.removeEventListener("mousemove", move);
        document.removeEventListener("mouseup", up);
        var w = parseInt(root.style.width, 10);
        if (!isNaN(w)) localStorage.setItem(WIDTH_KEY, String(w));
      }

      document.addEventListener("mousemove", move);
      document.addEventListener("mouseup", up);
    });
  }

  /* -------------------------------------------------------------- popovers */

  function closePopovers() {
    var ps = $$(".pp-popover");
    for (var i = 0; i < ps.length; i++) ps[i].hidden = true;
  }

  function togglePopover(name) {
    var el = root.querySelector('[data-popover="' + name + '"]');
    if (!el) return;
    var wasOpen = !el.hidden;
    closePopovers();
    if (name === "openin" && !wasOpen) renderOpenIn();
    el.hidden = wasOpen;
  }

  function renderOpenIn() {
    var host = root.querySelector('[data-popover="openin"]');
    if (!host) return;

    var avail = {};
    for (var i = 0; i < state.editors.length; i++) avail[state.editors[i].id] = state.editors[i].available;

    var html = "";
    for (i = 0; i < EDITORS.length; i++) {
      var ed = EDITORS[i];
      var on = avail[ed.id] !== false;   // assume available until told otherwise
      html +=
        '<button class="pp-menu-item' + (on ? '' : ' pp-disabled') + '" data-openin="' + ed.id + '"' +
        (on ? '' : ' disabled') + '>' +
          '<span>' + ed.label + '</span>' +
          (on ? '' : '<kbd>not installed</kbd>') +
        '</button>';
    }
    html += '<div class="pp-menu-sep"></div>';
    html += '<button class="pp-menu-item" data-openin="copy-path"><span>Copy path</span></button>';
    host.innerHTML = html;
  }

  function menuAction(action) {
    closePopovers();
    switch (action) {
      case "zoom-in": zoom = clamp(zoom + 0.1, 0.5, 2); applyZoom(); break;
      case "zoom-out": zoom = clamp(zoom - 0.1, 0.5, 2); applyZoom(); break;
      case "zoom-reset": zoom = 1; applyZoom(); break;
      case "copy-link":
        var p = activePage();
        copyText((p && (p.url || p.path)) || "");
        break;
      case "external": openExternal(); break;
      case "clear-history":
        try { localStorage.removeItem("sable_preview_history"); } catch (e) {}
        toast("Browsing history cleared", "success");
        break;
    }
  }

  function copyText(text) {
    if (!text) return;
    try {
      navigator.clipboard.writeText(text).then(
        function () { toast("Copied", "success"); },
        function () { toast("Copy failed", "error"); }
      );
    } catch (e) { toast("Copy failed", "error"); }
  }

  function openExternal() {
    var p = activePage();
    if (!p) return;
    if (p.url) { window.open(p.url, "_blank", "noopener"); return; }
    if (blobUrl) window.open(blobUrl, "_blank", "noopener");
  }

  function launchIn(app) {
    closePopovers();
    var page = activePage();

    if (app === "copy-path") {
      copyText((page && (page.path || page.url)) || "");
      return;
    }
    if (!page || !page.path) {
      toast("This tab has no file on disk", "error");
      return;
    }

    fetch("/api/filesystem/open-in", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: page.path, app: app }),
    })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d.error) toast(d.error, "error");
        else toast("Opened in " + app, "success");
      })
      .catch(function () { toast("Launch failed", "error"); });
  }

  function loadEditors() {
    fetch("/api/filesystem/editors")
      .then(function (r) { return r.json(); })
      .then(function (d) { state.editors = (d && d.editors) || []; })
      .catch(function () {});
  }

  /* --------------------------------------------------------------- changes */

  // The left sidebar owns the change list. We mirror it, never rescan disk.
  function loadChanges() {
    renderChangeCards();
  }

  function renderChangeCards() {
    var edits = window.SableEdits || [];
    var total = edits.length;

    // Badge and caption first — they matter even when the list is unmounted.
    var badge = $("[data-badge]");
    if (badge) {
      badge.textContent = String(total);
      // Only show a count once the user has actually looked at this tab.
      badge.hidden = !state.changesSeen || total === 0;
    }

    var sub = $("[data-changes-sub]");
    if (sub) sub.textContent = total ? total + (total === 1 ? " file" : " files") : "all reviewed";

    var list = $("[data-change-list]");
    if (!list) return;

    if (!total) {
      list.innerHTML =
        '<div class="pp-empty">No pending edits.<br><span class="pp-dim">Agent edits appear here as they land.</span></div>';
      return;
    }

    var html = "";
    for (var i = 0; i < edits.length; i++) {
      var e = edits[i];
      var stats = "";
      if (e.added != null) {
        stats =
          '<span class="pp-card-stats">' +
            '<span class="pp-add">+' + e.added + '</span>' +
            '<span class="pp-del">−' + e.removed + '</span>' +
          '</span>';
      }

      html +=
        '<div class="pp-card" data-path="' + esc(e.path) + '" data-backup="' + esc(e.backup_path || "") + '">' +
          '<div class="pp-card-head">' +
            '<i data-lucide="file-diff" class="icon-lucide pp-ci"></i>' +
            '<div class="pp-card-names">' +
              '<span class="pp-cn">' + esc(e.name) + '</span>' +
              '<span class="pp-cd">' + esc(e.dir || "") + '</span>' +
            '</div>' +
            stats +
            '<button class="pp-view-btn" data-view-btn>View</button>' +
          '</div>' +
          '<div class="pp-card-body" hidden></div>' +
        '</div>';
    }

    list.innerHTML = html;
    icons();
  }

  // Reuse the app's existing Monaco setup rather than reinventing a diff.
  function mountMonacoDiff(host, path, original, modified) {
    if (!host) return;

    host.innerHTML = '<div class="pp-empty">Loading diff…</div>';

    // Monaco is lazy — pull it in on demand instead of demanding the IDE.
    var ready = (typeof window.monaco !== "undefined")
      ? Promise.resolve()
      : (typeof window.__sableLoadMonaco === "function"
          ? window.__sableLoadMonaco().catch(function () {})
          : Promise.resolve());

    ready.then(function () {
      if (typeof window.monaco === "undefined") {
        renderTextDiff(host, original, modified);
        return;
      }

      // Clear the placeholder — Monaco appends into this node and would
      // otherwise leave "Loading diff…" sitting above the editor forever.
      host.innerHTML = "";

      try {
      if (typeof window.__sableDefineMonacoTheme === "function") {
        window.__sableDefineMonacoTheme();
      }

      var ext = (path.split(".").pop() || "").toLowerCase();
      var lang = (typeof window.__sableMonacoLang === "function")
        ? window.__sableMonacoLang(ext)
        : "plaintext";

      var origModel = window.monaco.editor.createModel(original || "", lang);
      var modModel = window.monaco.editor.createModel(modified || "", lang);

      var ed = window.monaco.editor.createDiffEditor(host, {
        readOnly: true,
        renderSideBySide: false,
        minimap: { enabled: false },
        scrollBeyondLastLine: false,
        automaticLayout: true,
        fontSize: 11,
        lineNumbers: "on",
        renderOverviewRuler: false,
        // Sticky scroll eats vertical space and fights the card layout.
        stickyScroll: { enabled: false },
      });
        ed.setModel({ original: origModel, modified: modModel });
        host.__monaco = ed;
      } catch (err) {
        renderTextDiff(host, original, modified);
      }
    });
  }

  // Last-resort renderer so the View button never shows an empty box.
  function renderTextDiff(host, original, modified) {
    host.innerHTML = "";
    var rows = unifiedDiff(original, modified);
    var html = "";
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i];
      if (r.type === "note") { html += '<div class="pp-empty">' + esc(r.text) + '</div>'; continue; }
      html +=
        '<div class="pp-dl pp-dl-' + r.type + '">' +
          '<span class="pp-dln">' + (r.a || "") + '</span>' +
          '<span class="pp-dln">' + (r.b || "") + '</span>' +
          '<span class="pp-dlc">' + (r.type === "add" ? "+" : r.type === "del" ? "−" : " ") + '</span>' +
          '<span class="pp-dlt">' + esc(r.text) + '</span>' +
        '</div>';
    }
    host.innerHTML = html || '<div class="pp-empty">No differences.</div>';
  }

  // Expand a card: fetch the diff once, then just toggle.
  function toggleCard(card) {
    var body = card.querySelector(".pp-card-body");
    var btn = card.querySelector("[data-view-btn]");
    if (!body) return;

    var open = !card.classList.contains("expanded");
    card.classList.toggle("expanded", open);
    body.hidden = !open;
    if (btn) btn.textContent = open ? "Hide" : "View";

    if (!open || card.dataset.loaded === "1") return;
    card.dataset.loaded = "1";

    var path = card.getAttribute("data-path");
    var backup = card.getAttribute("data-backup");
    body.innerHTML = '<div class="pp-empty">Loading diff…</div>';

    fetch("/api/filesystem/change-detail?path=" + encodeURIComponent(path) +
          "&backup=" + encodeURIComponent(backup || ""))
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d.error) {
          body.innerHTML = '<div class="pp-empty">' + esc(d.error) + '</div>';
          return;
        }

        var diff = renderDiff(d.original_content, d.modified_content);

        var stats = card.querySelector(".pp-card-stats");
        if (stats) {
          stats.innerHTML =
            '<span class="pp-add">+' + diff.added + '</span>' +
            '<span class="pp-del">−' + diff.removed + '</span>';
        }

        body.innerHTML =
          '<div class="pp-diff-actions">' +
            '<button class="pp-accept" data-path="' + esc(path) + '" data-backup="' + esc(backup) + '">Accept</button>' +
            '<button class="pp-reject" data-path="' + esc(path) + '" data-backup="' + esc(backup) + '">Reject</button>' +
          '</div>' +
          '<div class="pp-diff-body"></div>';

        mountMonacoDiff(body.querySelector(".pp-diff-body"), path, d.original_content, d.modified_content);
      })
      .catch(function () {
        body.innerHTML = '<div class="pp-empty">Could not load diff.</div>';
      });
  }

  function resolveEdit(btn, kind) {
    var path = btn.getAttribute("data-path");
    var backup = btn.getAttribute("data-backup");
    if (kind === "reject" && !backup) {
      toast("No backup to revert to — this file was created.", "error");
      return;
    }

    var endpoint = kind === "accept" ? "/api/filesystem/accept-edit" : "/api/filesystem/reject-edit";

    fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: path, backup_path: backup }),
    })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d.error) { toast(d.error, "error"); return; }
        toast(kind === "accept" ? "Edit accepted" : "Edit reverted", "success");
        // The card disappears with the backup it was bound to.
        loadChanges();
      })
      .catch(function () { toast("Failed", "error"); });
  }

  /* ---------------------------------------------------------------- public */

  // Paint the badge from whatever survived the reload.
  (function restoreEdits() {
    if (window.SableEdits && window.SableEdits.length) return;
    try {
      var raw = sessionStorage.getItem("sable_edits");
      if (raw) window.SableEdits = JSON.parse(raw) || [];
    } catch (e) {}
    window.SableEdits = window.SableEdits || [];
  })();

  function reveal() {
    root.style.width = savedWidth() + "px";
    root.classList.add("open");
    root.setAttribute("aria-hidden", "false");
    document.body.classList.add("pp-open");
    state.open = true;
    icons();
    scheduleNativeBounds();
  }

  function open(opts) {
    build();
    opts = opts || {};

    if (!state.editors.length) loadEditors();

    var hasContent = opts.html != null || opts.source != null || opts.url;

    if (hasContent) {
      // Reuse a tab showing the same file, else make one.
      var page = null;
      for (var i = 0; i < state.pages.length; i++) {
        var p = state.pages[i];
        if ((opts.path && p.path === opts.path) || (opts.url && p.url === opts.url)) page = p;
      }

      if (!page) page = addPage({
        title: opts.title || "Preview",
        html: opts.html || "",
        source: opts.source != null ? opts.source : (opts.html || ""),
        path: opts.path || "",
        url: opts.url || "",
      });
      else {
        if (opts.title) page.title = opts.title;
        if (opts.html != null) page.html = opts.html;
        if (opts.source != null) page.source = opts.source;
        if (opts.url) page.url = opts.url;
        renderTabs();
      }

      activatePage(page);
    } else {
      setTab("changes");
    }

    reveal();
  }

  function openChanges() {
    build();
    if (!state.editors.length) loadEditors();
    setTab("changes");
    reveal();
  }

  function close() {
    if (!root) return;
    hideNative();
    state.open = false;
    root.classList.remove("open");
    root.setAttribute("aria-hidden", "true");
    document.body.classList.remove("pp-open");
    closePopovers();
  }

  function toggle(opts) {
    if (state.open) close();
    else open(opts);
  }

  // Live for the whole page lifetime — the panel is built lazily, but the
  // badge must be honest before it is ever opened.
  window.addEventListener("sable:edits-changed", function () {
    if (root) renderChangeCards();
  });

  window.addEventListener("sable:file-edit", function (e) {
    var evt = e.detail || {};
    if (!evt.path) return;

    state.edits[evt.path] = {
      name: evt.name || evt.path.split("/").pop(),
      path: evt.path,
      dir: evt.path.slice(0, evt.path.lastIndexOf("/")),
      backup_path: evt.backup_path || "",
      added: evt.added || 0,
      removed: evt.removed || 0,
      at: Date.now(),
    };

    var i = state.editOrder.indexOf(evt.path);
    if (i >= 0) state.editOrder.splice(i, 1);
    state.editOrder.unshift(evt.path);

    if (root) renderChangeCards();
  });

  window.SablePreview = {
    open: open,
    openChanges: openChanges,
    openPath: openPath,
    close: close,
    toggle: toggle,
    refresh: loadChanges,
    isOpen: function () { return state.open; },
  };
})();
