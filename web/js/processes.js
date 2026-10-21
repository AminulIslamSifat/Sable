/* ==========================================================
   Sable Processes — minimal monitor panel
   Filter / sort / keyboard nav / live system bars.
   ========================================================== */
(function () {
  "use strict";

  const API = "/api/processes";
  const DEFAULT_RATE = 4000;

  let timer = null;
  let rate = DEFAULT_RATE;
  let panelOpen = false;
  let data = { processes: [], running: 0, system: null };
  let selectedPid = null;

  let filterText = "";
  let activeTab = "all";   // all | browser | command
  let scope = "all";      // all | spawned  (spawned = launched by Sable/model)

  // A process was started by Sable/the model if it is a tracked bg job or an
  // engine-registered job. Merely belonging to the project tree is not enough.
  function isSableSpawned(p) {
    return p.source === "bg-job" || p.source === "engine";
  }
  let sortKey = "cpu";
  let sortDesc = true;

  const $ = (id) => document.getElementById(id);

  function authHeaders() {
    const t = localStorage.getItem("sable_token");
    return t ? { Authorization: "Bearer " + t } : {};
  }

  function esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  /* Raw comm strings tell the user nothing. */
  function displayName(p) {
    if (p.is_server) return "sable-server";
    const c = (p.comm || "").toLowerCase();
    if (c === "chrome" || c === "chromium") return "chrome";
    if (c === "electron") return "sable-desktop";
    if (c === "playwright-driver" || c === "mainthread") return "playwright";
    if (c.indexOf("crashpad") === 0) return "crashpad";
    if (c === "github-mcp-serv") return "github-mcp";
    return p.comm || "?";
  }

  /* ---------- pill ---------- */
  function renderPill() {
    const all = data.processes || [];
    const total = all.length;
    const spawned = all.filter(isSableSpawned).length;

    // Status bar: only processes Sable/model actually launched.
    const pill = $("procPill");
    const count = $("procPillCount");
    if (pill && count) {
      count.textContent = spawned;
      pill.classList.toggle("live", spawned > 0);
      pill.title = spawned === 0
        ? "No Sable-spawned processes"
        : spawned + " process" + (spawned === 1 ? "" : "es") + " started by Sable";
    }

    // Sidebar: the full picture for this project.
    const sp = $("procSidebarPill");
    const sc = $("procSidebarCount");
    if (sp && sc) {
      sc.textContent = total;
      sp.classList.toggle("live", total > 0);
      sp.title = total + " process" + (total === 1 ? "" : "es") + " in this project";
    }
  }

  /* ---------- system bars ---------- */
  function fillCls(pct) {
    return pct > 85 ? "hot" : pct > 60 ? "warn" : "";
  }

  function renderSystem(sys) {
    if (!sys) return;

    const cpu = sys.cpu_percent || 0;
    $("procCpuFill").style.width = Math.min(100, cpu) + "%";
    $("procCpuFill").className = "proc-sys-fill " + fillCls(cpu);
    $("procCpuVal").textContent = cpu + "%";

    $("procRamFill").style.width = Math.min(100, sys.mem_percent || 0) + "%";
    $("procRamFill").className = "proc-sys-fill " + fillCls(sys.mem_percent || 0);
    $("procRamVal").textContent = sys.mem_used_mb + "/" + sys.mem_total_mb + "M";

    $("procSwapFill").style.width = Math.min(100, sys.swap_percent || 0) + "%";
    $("procSwapFill").className = "proc-sys-fill " + fillCls(sys.swap_percent || 0);
    $("procSwapVal").textContent = sys.swap_used_mb + "/" + sys.swap_total_mb + "M";

    if (sys.load) {
      $("procLoad").textContent = "Load: " + sys.load.map((n) => n.toFixed(2)).join("  ");
    }
  }

  /* ---------- filtering + sorting ---------- */
  function visibleRows() {
    const q = filterText.trim().toLowerCase();
    let rows = data.processes || [];

    if (scope === "spawned") {
      rows = rows.filter(isSableSpawned);
    }

    // Tab scope next, then the free-text filter on top of it.
    if (activeTab === "browser") {
      rows = rows.filter((p) => (p.category || "others") === "browsers");
    } else if (activeTab === "command") {
      // "Running command" = everything that is not a browser: spawned jobs,
      // servers and tool subprocesses.
      rows = rows.filter((p) => (p.category || "others") !== "browsers");
    }

    if (q) {
      rows = rows.filter((p) =>
        String(p.pid).includes(q) ||
        (p.comm || "").toLowerCase().includes(q) ||
        displayName(p).toLowerCase().includes(q) ||
        (p.command || "").toLowerCase().includes(q)
      );
    }

    const key = sortKey;
    const dir = sortDesc ? -1 : 1;
    const val = (p) => {
      if (key === "cpu") return p.cpu || 0;
      if (key === "mem") return p.mem_percent || 0;
      if (key === "rss") return p.total_ram_mb || p.ram_mb || 0;
      if (key === "pid") return p.pid || 0;
      return (displayName(p) || "").toLowerCase();
    };

    return rows.slice().sort((a, b) => {
      const va = val(a), vb = val(b);
      if (typeof va === "string") return va < vb ? -1 * dir : va > vb ? dir : 0;
      return (va - vb) * dir;
    });
  }

  /* ---------- row ---------- */
  function rowHtml(p) {
    const cpuCls = p.cpu > 60 ? "proc-cpu-hot" : p.cpu > 20 ? "proc-cpu-warm" : "";
    const port = (p.ports || []).length
      ? '<span class="proc-port">:' + p.ports.join(" :") + "</span>"
      : "";

    return '<div class="proc-row' + (p.pid === selectedPid ? " selected" : "") +
      '" data-pid="' + p.pid + '">' +
      '<span class="proc-c-pid">' + p.pid + "</span>" +
      '<span class="proc-c-cpu ' + cpuCls + '">' + (p.cpu ?? 0) + "</span>" +
      '<span class="proc-c-mem">' + (p.mem_percent ?? 0) + "</span>" +
      '<span class="proc-c-rss">' + fmtRss(p.total_ram_mb ?? p.ram_mb ?? 0) + "</span>" +
      '<span class="proc-c-state">' + esc(p.state || "S") + "</span>" +
      '<span class="proc-c-cmd" title="' + esc(p.command) + '">' + port + esc(p.command) + "</span>" +
      '<button class="proc-kill" data-kill="' + p.pid + '" title="Kill process tree">' +
        '<i data-lucide="x" class="icon-lucide"></i></button>' +
    "</div>";
  }

  function fmtRss(mb) {
    if (mb >= 1024) return (mb / 1024).toFixed(1) + "G";
    return Math.round(mb) + "M";
  }

  /* ---------- render ---------- */
  function render() {
    const list = $("procList");
    if (!list) return;

    const rows = visibleRows();

    const stamp = new Date().toLocaleTimeString([], { hour12: false });
    const upd = $("procUpdated");
    if (upd) upd.textContent = "Updated " + stamp;

    renderSystem(data.system);

    if (!rows.length) {
      list.innerHTML = '<div class="proc-empty">' +
        (filterText ? "No matches for \"" + esc(filterText) + "\"" : "No Sable processes running.") +
        "</div>";
    } else {
      list.innerHTML = rows.map(rowHtml).join("");
    }

    const total = (data.processes || []).length;
    const fc = $("procFootCount");
    if (fc) fc.textContent = rows.length + " of " + total + " processes";

    if (window.lucide && typeof window.lucide.createIcons === "function") {
      try { window.lucide.createIcons(); } catch (e) {}
    }

    list.querySelectorAll(".proc-row").forEach((row) => {
      row.addEventListener("click", () => {
        selectedPid = parseInt(row.dataset.pid, 10);
        list.querySelectorAll(".proc-row").forEach((r) => r.classList.remove("selected"));
        row.classList.add("selected");
      });
    });

    list.querySelectorAll("[data-kill]").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        killProc(parseInt(btn.dataset.kill, 10));
      });
    });
  }

  /* ---------- kill ---------- */
  async function killProc(pid) {
    if (!pid) return;
    const p = (data.processes || []).find((x) => x.pid === pid);
    const cmd = p ? p.command : "";
    if (!confirm("Kill " + pid + "?\n\n" + cmd + "\n\nThis kills the whole process tree.")) return;

    try {
      const res = await fetch(API + "/" + pid + "/kill", { method: "POST", headers: authHeaders() });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || ("HTTP " + res.status));
      }
      if (typeof showToast === "function") showToast("Killed " + pid, "success");
    } catch (e) {
      if (typeof showToast === "function") showToast("Kill failed: " + e.message, "error");
    }
    refresh();
  }

  /* ---------- data ---------- */
  async function refresh() {
    try {
      const res = await fetch(API, { headers: authHeaders() });
      if (!res.ok) return;
      data = await res.json();
      renderPill();
      if (panelOpen) render();
    } catch (e) {
      /* keep last known state on a network hiccup */
    }
  }

  /* ---------- polling ---------- */
  function restartTimer() {
    if (timer) clearInterval(timer);
    timer = setInterval(refresh, rate);
  }

  /* ---------- open / close ---------- */
  function openPanel(mode) {
    const ov = $("procOverlay");
    if (!ov) return;
    scope = mode === "spawned" ? "spawned" : "all";

    const lock = $("procLock");
    if (lock) lock.hidden = scope !== "spawned";

    panelOpen = true;
    ov.classList.remove("hidden");
    refresh();
    // Do NOT autofocus the filter: it swallows arrow keys and kills keyboard
    // navigation. Focus the panel so arrows work immediately; Ctrl+F focuses
    // the input when the user actually wants to type.
    const panel = ov.querySelector(".proc-panel");
    if (panel) {
      panel.setAttribute("tabindex", "-1");
      setTimeout(() => panel.focus({ preventScroll: true }), 60);
    }
    // System CPU% needs two samples to produce a delta. Prime it so the bars
    // are not stuck at 0% on the first paint after opening.
    setTimeout(refresh, 700);
  }

  function closePanel() {
    const ov = $("procOverlay");
    if (!ov) return;
    panelOpen = false;
    ov.classList.add("hidden");
  }

  /* ---------- keyboard ---------- */
  function moveSelection(delta) {
    const rows = visibleRows();
    if (!rows.length) return;
    let idx = rows.findIndex((p) => p.pid === selectedPid);
    idx = idx === -1 ? 0 : Math.min(rows.length - 1, Math.max(0, idx + delta));
    selectedPid = rows[idx].pid;
    render();

    const el = document.querySelector('.proc-row[data-pid="' + selectedPid + '"]');
    if (el) el.scrollIntoView({ block: "nearest" });
  }

  /* ---------- wiring ---------- */
  function init() {
    const pill = $("procPill");
    const ov = $("procOverlay");
    if (pill) pill.addEventListener("click", () => openPanel("spawned"));

    const sidePill = $("procSidebarPill");
    if (sidePill) sidePill.addEventListener("click", () => openPanel("all"));

    const clear = $("procLockClear");
    if (clear) clear.addEventListener("click", () => {
      scope = "all";
      const lock = $("procLock");
      if (lock) lock.hidden = true;
      render();
    });
    if (ov) ov.addEventListener("click", (e) => { if (e.target === ov) closePanel(); });

    const closeBtn = $("procClose");
    if (closeBtn) closeBtn.addEventListener("click", closePanel);

    const refreshBtn = $("procRefreshBtn");
    if (refreshBtn) refreshBtn.addEventListener("click", refresh);
    const refreshBtn2 = $("procRefreshBtn2");
    if (refreshBtn2) refreshBtn2.addEventListener("click", refresh);

    const filter = $("procFilter");
    if (filter) filter.addEventListener("input", () => { filterText = filter.value; render(); });

    const sort = $("procSort");
    if (sort) sort.addEventListener("change", () => { sortKey = sort.value; render(); });

    const dir = $("procSortDir");
    if (dir) dir.addEventListener("click", () => {
      sortDesc = !sortDesc;
      dir.classList.toggle("desc", !sortDesc);
      render();
    });

    document.querySelectorAll(".proc-tab").forEach((btn) => {
      btn.addEventListener("click", () => {
        activeTab = btn.dataset.tab || "all";
        document.querySelectorAll(".proc-tab").forEach((b) => {
          b.classList.toggle("active", b === btn);
        });
        render();
      });
    });

    const rateEl = $("procRate");
    const rateVal = $("procRateVal");
    if (rateEl) {
      rateEl.addEventListener("input", () => {
        rate = parseInt(rateEl.value, 10) || DEFAULT_RATE;
        if (rateVal) rateVal.textContent = rate + "ms";
        restartTimer();
      });
    }

    document.addEventListener("keydown", (e) => {
      if (!panelOpen) return;

      if (e.key === "Escape") { closePanel(); return; }

      const typing = e.target && /^(INPUT|SELECT|TEXTAREA)$/.test(e.target.tagName);

      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "f") {
        e.preventDefault();
        const f = $("procFilter");
        if (f) f.focus();
        return;
      }

      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "d") {
        e.preventDefault();
        if (selectedPid) killProc(selectedPid);
        return;
      }

      if (typing) return;

      if (e.key === "ArrowDown") { e.preventDefault(); moveSelection(1); }
      else if (e.key === "ArrowUp") { e.preventDefault(); moveSelection(-1); }
    });

    restartTimer();
    refresh();

    document.addEventListener("sable:bg-job-changed", refresh);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  window.SableProcesses = { refresh, open: openPanel, close: closePanel };
})();
