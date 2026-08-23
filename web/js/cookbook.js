
/**
 * Sable Cookbook UI — Local model download, serve, and management.
 * Live download progress via polling.
 */
(function () {
  "use strict";

  // ─── State ──────────────────────────────────────────────────────────────────
  let pollTimer = null;
  const POLL_INTERVAL = 2000; // 2s for live progress

  // ─── Format Helpers ──────────────────────────────────────────────────────────
  function fmtBytes(b) {
    if (!b || b <= 0) return "0 B";
    if (b < 1024) return b + " B";
    if (b < 1024 * 1024) return (b / 1024).toFixed(1) + " KB";
    if (b < 1024 * 1024 * 1024) return (b / (1024 * 1024)).toFixed(1) + " MB";
    return (b / (1024 * 1024 * 1024)).toFixed(2) + " GB";
  }
  function fmtSpeed(bps) {
    if (!bps || bps <= 0) return "";
    if (bps < 1024 * 1024) return (bps / 1024).toFixed(0) + " KB/s";
    return (bps / (1024 * 1024)).toFixed(1) + " MB/s";
  }

  // ─── API Helpers ────────────────────────────────────────────────────────────
  async function cbFetch(path, opts = {}) {
    const token = localStorage.getItem("sable_auth_token") || "";
    const resp = await fetch("/api/cookbook" + path, {
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

  // ─── Render: Downloads ──────────────────────────────────────────────────────
  function renderDownloads(downloads) {
    const el = document.getElementById("cbDownloadsList");
    if (!el) return;
    const active = downloads.filter((d) => d.status === "downloading" || d.status === "pending");
    const done = downloads.filter((d) => d.status === "done");
    const failed = downloads.filter((d) => d.status === "failed");

    if (active.length === 0 && done.length === 0 && failed.length === 0) {
      el.innerHTML = '<p class="muted" style="font-size:12px;font-style:italic;">No downloads</p>';
      return;
    }

    let html = "";
    for (const d of active) {
      const pct = Math.round(d.progress || 0);
      const statusIcon = pct > 0 ? '<i data-lucide="download" style="width:12px;height:12px;display:inline;vertical-align:middle;"></i>' : '<i data-lucide="hourglass" style="width:12px;height:12px;display:inline;vertical-align:middle;"></i>';
      const speedStr = fmtSpeed(d.speed_bps || 0);
      const bytesStr = fmtBytes(d.bytes_downloaded || 0) + (d.total_bytes ? " / " + fmtBytes(d.total_bytes) : "");
      html += `
        <div class="cb-download-card">
          <div class="cb-dl-header">
            <span class="cb-dl-repo">${statusIcon} ${escHtml(d.repo_id)}</span>
            <button class="cb-stop-dl-btn" data-id="${d.id}">⏹ Stop</button>
          </div>
          <div class="cb-progress-bar">
            <div class="cb-progress-fill" style="width:${pct}%"></div>
          </div>
          <div class="cb-dl-meta">
            <span style="font-weight:600;">${pct}%</span>
            <span class="muted">${bytesStr}</span>
            ${speedStr ? `<span class="cb-dl-speed">${speedStr}</span>` : ""}
          </div>
        </div>`;
    }
    for (const d of done) {
      html += `
        <div class="cb-download-card cb-done">
          <div class="cb-dl-header">
            <span class="cb-dl-repo"><i data-lucide="circle-check" style="width:12px;height:12px;display:inline;vertical-align:middle;color:var(--success);"></i> ${escHtml(d.repo_id)}</span>
          </div>
        </div>`;
    }
    for (const d of failed) {
      html += `
        <div class="cb-download-card cb-failed">
          <div class="cb-dl-header">
            <span class="cb-dl-repo" style="color:var(--error);">✗ ${escHtml(d.repo_id)}</span>
          </div>
          <p style="font-size:11px;margin-top:4px;color:var(--error);word-break:break-all;">${escHtml(d.error || "Unknown error")}</p>
        </div>`;
    }
    el.innerHTML = html;

    // Bind stop buttons
    el.querySelectorAll(".cb-stop-dl-btn").forEach((btn) => {
      btn.onclick = async () => {
        btn.disabled = true;
        btn.textContent = "Stopping...";
        try {
          await cbFetch("/download/" + btn.dataset.id, { method: "DELETE" });
          showToast("Download stopped");
          refreshCookbook();
        } catch (e) {
          showToast("Stop failed: " + e.message, true);
        }
        btn.disabled = false;
        btn.textContent = "⏹ Stop";
      };
    });
  }

  // ─── Render: Servers ────────────────────────────────────────────────────────
  function renderServers(servers) {
    const el = document.getElementById("cbServersList");
    if (!el) return;
    const running = servers.filter((s) => s.status === "running");

    if (running.length === 0) {
      el.innerHTML = '<p class="muted" style="font-size:12px;font-style:italic;">No servers running</p>';
      return;
    }

    let html = "";
    for (const s of running) {
      html += `
        <div class="cb-server-card">
          <div class="cb-server-info">
            <span class="cb-server-model">🟢 ${escHtml(s.model)}</span>
            <span class="muted" style="font-size:11px;">${s.endpoint} · ctx:${s.ctx_size} · pid:${s.pid}</span>
          </div>
          <div class="cb-server-actions">
            <button class="cb-logs-btn" data-id="${s.id}" title="View logs">📋</button>
            <button class="cb-stop-btn" data-id="${s.id}" title="Stop server">⏹</button>
          </div>
        </div>`;
    }
    el.innerHTML = html;

    el.querySelectorAll(".cb-stop-btn").forEach((btn) => {
      btn.onclick = async () => {
        try {
          await cbFetch("/server/" + btn.dataset.id, { method: "DELETE" });
          refreshCookbook();
        } catch (e) { alert("Stop failed: " + e.message); }
      };
    });

    el.querySelectorAll(".cb-logs-btn").forEach((btn) => {
      btn.onclick = async () => {
        try {
          const data = await cbFetch("/server/" + btn.dataset.id + "/logs?lines=30");
          showLogsModal(data.logs, data.diagnosis);
        } catch (e) { alert("Logs failed: " + e.message); }
      };
    });
  }

  // ─── Render: Hardware Info ──────────────────────────────────────────────────
  function renderHardware(hw) {
    const el = document.getElementById("cbHardwareInfo");
    if (!el) return;
    const gpuStr = hw.gpu_name ? `${hw.gpu_name} (${hw.gpu_vram_gb} GB VRAM)` : "No GPU (CPU only)";
    el.innerHTML = `
      <div class="cb-hw-grid">
        <div class="cb-hw-item"><span class="cb-hw-label">RAM</span><span class="cb-hw-value">${hw.total_ram_gb} GB total · ${hw.available_ram_gb} GB free</span></div>
        <div class="cb-hw-item"><span class="cb-hw-label">GPU</span><span class="cb-hw-value">${gpuStr}</span></div>
        <div class="cb-hw-item"><span class="cb-hw-label">CPU</span><span class="cb-hw-value">${hw.cpu_cores} threads</span></div>
        <div class="cb-hw-item"><span class="cb-hw-label">Disk</span><span class="cb-hw-value">${hw.disk_free_gb} GB free</span></div>
      </div>`;
  }

  // ─── Render: Recommendations ────────────────────────────────────────────────
  function renderRecommendations(recs) {
    const el = document.getElementById("cbPresetsGrid");
    if (!el) return;
    if (!recs.length) {
      el.innerHTML = '<p class="muted" style="font-size:12px;font-style:italic;">No models in catalog</p>';
      return;
    }
    let html = "";
    for (let i = 0; i < recs.length; i++) {
      const r = recs[i];
      const incompatible = !r.fits;
      const cardClass = incompatible ? "cb-preset-card cb-incompatible" : "cb-preset-card";
      const scoreColor = incompatible ? "#f87171" : r.score >= 80 ? "#4ade80" : r.score >= 60 ? "#fbbf24" : "#f87171";
      const speedIcon = r.speed === "fast" ? '<i data-lucide="zap" style="width:12px;height:12px;display:inline;vertical-align:middle;"></i>' : r.speed === "moderate" ? '<i data-lucide="refresh-cw" style="width:12px;height:12px;display:inline;vertical-align:middle;"></i>' : '<i data-lucide="gauge" style="width:12px;height:12px;display:inline;vertical-align:middle;"></i>';
      const btnHtml = incompatible
        ? `<button class="cb-preset-btn cb-btn-disabled" disabled title="${escHtml(r.notes)}">Won't Fit</button>`
        : `<button class="cb-preset-btn" data-repo="${escHtml(r.repo_id)}" data-include="${escHtml(r.include)}" data-label="${escHtml(r.label)}">Download & Serve</button>`;

      html += `
        <div class="${cardClass}" data-idx="${i}">
          <div class="cb-preset-label">
            ${incompatible ? '<i data-lucide="circle-x" style="width:12px;height:12px;display:inline;vertical-align:middle;color:#f87171;"></i> ' : ""}${escHtml(r.label)}
            <span class="cb-score-badge" style="color:${scoreColor}">${incompatible ? "✗" : r.score}</span>
          </div>
          <div class="cb-preset-desc">${escHtml(r.description)}</div>
          <div class="cb-preset-meta">
            <span>${r.params_b}B · ${r.quant}</span>
            <span><i data-lucide="download" style="width:12px;height:12px;display:inline;vertical-align:middle;"></i> ${r.download_size_gb || '~' + r.estimated_memory_gb} GB</span>
            <span><i data-lucide="cpu" style="width:12px;height:12px;display:inline;vertical-align:middle;"></i> ~${r.estimated_memory_gb} GB</span>
            ${incompatible ? "" : `<span>${speedIcon} ${r.speed}</span>`}
          </div>
          <div class="cb-preset-notes ${incompatible ? "cb-notes-red" : ""}">${escHtml(r.notes)}</div>
          <div class="cb-preset-tags">${r.tags.map((t) => `<span class="cb-tag">${t}</span>`).join("")}</div>
          ${btnHtml}
        </div>`;
    }
    el.innerHTML = html;

    el.querySelectorAll(".cb-preset-btn:not(.cb-btn-disabled)").forEach((btn) => {
      btn.onclick = async () => {
        btn.disabled = true;
        btn.textContent = "Starting...";
        try {
          await cbFetch("/download", {
            method: "POST",
            body: JSON.stringify({
              repo_id: btn.dataset.repo,
              include: btn.dataset.include,
              serve_after: true,
              model_label: btn.dataset.label,
            }),
          });
          btn.innerHTML = '<i data-lucide="download" style="width:12px;height:12px;display:inline;vertical-align:middle;"></i> Downloading...';
          showToast("Downloading " + btn.dataset.label + " — watch progress above");
          startPolling();
          refreshCookbook();
        } catch (e) {
          btn.textContent = "Download & Serve";
          showToast("Error: " + e.message, true);
        }
        btn.disabled = false;
      };
    });
  }

  // ─── Render: Cached Models ──────────────────────────────────────────────────
  function renderCachedModels(models) {
    const el = document.getElementById("cbCachedModels");
    if (!el) return;
    if (!models.length) {
      el.innerHTML = '<p class="muted" style="font-size:12px;font-style:italic;">No models downloaded yet</p>';
      return;
    }
    let html = "";
    for (const m of models) {
      const sizeStr = m.size_mb > 1024 ? (m.size_mb / 1024).toFixed(1) + " GB" : m.size_mb.toFixed(0) + " MB";
      html += `
        <div class="cb-model-row">
          <div class="cb-model-info">
            <span class="cb-model-name">${escHtml(m.name)}</span>
            <span class="muted" style="font-size:11px;">${sizeStr}</span>
          </div>
          <div style="display:flex;gap:6px;">
            <button class="cb-serve-btn" data-path="${escHtml(m.path)}" data-name="${escHtml(m.name)}">▶ Serve</button>
            <button class="cb-delete-btn" data-path="${escHtml(m.path)}" data-name="${escHtml(m.name)}">🗑</button>
          </div>
        </div>`;
    }
    el.innerHTML = html;

    el.querySelectorAll(".cb-serve-btn").forEach((btn) => {
      btn.onclick = async () => {
        btn.disabled = true;
        btn.textContent = "Starting...";
        try {
          const result = await cbFetch("/serve", {
            method: "POST",
            body: JSON.stringify({ model_path: btn.dataset.path, model_label: btn.dataset.name }),
          });
          showToast("Serving " + btn.dataset.name + " at " + result.endpoint);
          refreshCookbook();
        } catch (e) {
          showToast("Serve failed: " + e.message, true);
        }
        btn.disabled = false;
        btn.textContent = "▶ Serve";
      };
    });

    el.querySelectorAll(".cb-delete-btn").forEach((btn) => {
      btn.onclick = async () => {
        if (!confirm("Delete " + btn.dataset.name + " from disk? This cannot be undone.")) return;
        btn.disabled = true;
        btn.textContent = "...";
        try {
          await cbFetch("/model", {
            method: "DELETE",
            body: JSON.stringify({ path: btn.dataset.path }),
          });
          showToast("Deleted " + btn.dataset.name);
          refreshCookbook();
        } catch (e) {
          showToast("Delete failed: " + e.message, true);
          btn.disabled = false;
          btn.textContent = "🗑";
        }
      };
    });
  }

  // ─── Logs Modal ─────────────────────────────────────────────────────────────
  function showLogsModal(logs, diagnosis) {
    const existing = document.getElementById("cbLogsModal");
    if (existing) existing.remove();

    const modal = document.createElement("div");
    modal.id = "cbLogsModal";
    modal.className = "cb-logs-modal";
    modal.innerHTML = `
      <div class="cb-logs-modal-content">
        <div class="cb-logs-modal-header">
          <h4>Server Logs</h4>
          <button class="cb-logs-close"><i data-lucide="x" style="width:14px;height:14px;"></i></button>
        </div>
        ${diagnosis ? `<div class="cb-diagnosis"><strong>⚠ ${escHtml(diagnosis.message)}</strong><ul>${diagnosis.suggestions.map((s) => `<li>${escHtml(s)}</li>`).join("")}</ul></div>` : ""}
        <pre class="cb-logs-text">${escHtml(logs || "No output yet")}</pre>
      </div>`;
    document.body.appendChild(modal);
    if (typeof lucide !== "undefined") lucide.createIcons();
    modal.querySelector(".cb-logs-close").onclick = () => modal.remove();
    modal.onclick = (e) => { if (e.target === modal) modal.remove(); };
  }

  // ─── Custom Download ────────────────────────────────────────────────────────
  function initCustomDownload() {
    const btn = document.getElementById("cbDownloadBtn");
    if (!btn) return;
    btn.onclick = async () => {
      const repoId = document.getElementById("cbRepoId").value.trim();
      const include = document.getElementById("cbInclude").value.trim();
      if (!repoId) { showToast("Enter a repo ID (e.g. org/model)", true); return; }
      btn.disabled = true;
      try {
        await cbFetch("/download", {
          method: "POST",
          body: JSON.stringify({ repo_id: repoId, include: include || null }),
        });
        showToast("Download started: " + repoId);
        document.getElementById("cbRepoId").value = "";
        document.getElementById("cbInclude").value = "";
        startPolling();
        refreshCookbook();
      } catch (e) {
        showToast("Download failed: " + e.message, true);
      }
      btn.disabled = false;
    };
  }

  // ─── Model Search ──────────────────────────────────────────────────────────
  let _searchDebounce = null;

  function initModelSearch() {
    const input = document.getElementById("cbSearchInput");
    const btn = document.getElementById("cbSearchBtn");
    const resultsEl = document.getElementById("cbSearchResults");
    if (!input || !btn || !resultsEl) return;

    async function doSearch() {
      const q = input.value.trim();
      if (q.length < 2) { resultsEl.innerHTML = ""; return; }
      btn.disabled = true;
      resultsEl.innerHTML = '<p class="muted" style="font-size:11px;font-style:italic;">Searching HuggingFace...</p>';
      try {
        const data = await cbFetch("/search?q=" + encodeURIComponent(q));
        if (!data.results.length) {
          resultsEl.innerHTML = '<p class="muted" style="font-size:11px;font-style:italic;">No models found for "' + escHtml(q) + '"</p>';
        } else {
          let html = '<div class="cb-search-results-grid">';
          for (const r of data.results) {
            const dlStr = r.downloads >= 1000 ? (r.downloads / 1000).toFixed(1) + "k" : r.downloads;
            const tags = (r.tags || []).map(t => '<span class="cb-tag">' + t + '</span>').join("");
            let sizeStr = "";
            if (r.total_size > 0) {
              const gb = r.total_size / (1024 * 1024 * 1024);
              sizeStr = gb >= 1 ? gb.toFixed(1) + " GB" : (r.total_size / (1024 * 1024)).toFixed(0) + " MB";
            }
            const fileInfo = r.gguf_count ? `${r.gguf_count} GGUF` + (sizeStr ? ` · ${sizeStr}` : "") : "";
            html += `
              <div class="cb-search-result-card">
                <div class="cb-sr-header">
                  <span class="cb-sr-name">${escHtml(r.repo_id)}</span>
                  <span class="muted" style="font-size:10px;">⬇ ${dlStr} · ♥ ${r.likes}${fileInfo ? " · " + fileInfo : ""}</span>
                </div>
                <div class="cb-sr-tags">${tags}</div>
                <button class="cb-sr-dl-btn" data-repo="${escHtml(r.repo_id)}">Download</button>
              </div>`;
          }
          html += '</div>';
          resultsEl.innerHTML = html;

          // Bind download buttons — fill repo ID and trigger download
          resultsEl.querySelectorAll(".cb-sr-dl-btn").forEach(b => {
            b.onclick = () => {
              document.getElementById("cbRepoId").value = b.dataset.repo;
              document.getElementById("cbInclude").value = "*q4_k_m*";
              document.getElementById("cbDownloadBtn").click();
            };
          });
          if (typeof lucide !== "undefined") lucide.createIcons();
        }
      } catch (e) {
        resultsEl.innerHTML = '<p style="font-size:11px;color:var(--error);">Search failed: ' + escHtml(e.message) + '</p>';
      } finally {
        btn.disabled = false;
      }
    }

    btn.onclick = doSearch;
    input.onkeydown = (e) => {
      if (e.key === "Enter") { e.preventDefault(); e.stopPropagation(); doSearch(); }
    };
  }

  // ─── Settings ───────────────────────────────────────────────────────────────
  async function loadCookbookSettings() {
    try {
      const s = await cbFetch("/settings");
      const tokenInput = document.getElementById("cbHfToken");
      const portInput = document.getElementById("cbDefaultPort");
      const ctxInput = document.getElementById("cbDefaultCtx");
      const autoReg = document.getElementById("cbAutoRegister");
      const threadsInput = document.getElementById("cbDefaultThreads");
      if (tokenInput && s.has_hf_token) tokenInput.placeholder = "•••••••• (set)";
      if (portInput) portInput.value = s.default_port;
      if (threadsInput) threadsInput.value = s.default_threads ?? 0;
      if (ctxInput) ctxInput.value = s.default_ctx;
      if (autoReg) autoReg.checked = s.auto_register;
    } catch (e) { /* ignore */ }
  }

  async function _saveCookbookSettings() {
    const body = {
      default_port: parseInt(document.getElementById("cbDefaultPort").value) || 8080,
      default_threads: parseInt(document.getElementById("cbDefaultThreads").value) || 0,
      default_ctx: parseInt(document.getElementById("cbDefaultCtx").value) || 4096,
      auto_register: document.getElementById("cbAutoRegister").checked,
    };
    const token = document.getElementById("cbHfToken").value.trim();
    if (token) body.hf_token = token;
    await cbFetch("/settings", { method: "POST", body: JSON.stringify(body) });
  }

  function initSaveSettings() {
    // Register with universal save system if available
    if (window._universalSave) {
      window._universalSave.register("cookbook", _saveCookbookSettings);
    }
  }

  // ─── Polling for Live Progress ──────────────────────────────────────────────
  function startPolling() {
    if (pollTimer) return;
    pollTimer = setInterval(refreshCookbook, POLL_INTERVAL);
  }

  function stopPolling() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  // ─── Main Refresh ───────────────────────────────────────────────────────────
  async function refreshCookbook() {
    try {
      const data = await cbFetch("/status");
      renderDownloads(data.downloads);
      renderServers(data.servers);
      renderCachedModels(data.cached_models);

      // Auto-stop polling if no active downloads
      const hasActive = data.downloads.some((d) => d.status === "downloading");
      if (!hasActive) stopPolling();
      else startPolling();
    } catch (e) {
      // Server might be restarting
      stopPolling();
    }
    // Re-render lucide icons in dynamically inserted HTML
    if (typeof lucide !== "undefined") lucide.createIcons();
  }

  // ─── Per-Model Instruction Settings ─────────────────────────────────────────
  let _allSkills = [];
  let _allTools = [];

  async function loadAvailableSkills() {
    try {
      const resp = await fetch("/api/skills");
      if (resp.ok) {
        const data = await resp.json();
        _allSkills = (data.skills || data || []).map(s => typeof s === "string" ? s : s.key || s.name);
      }
    } catch (e) { /* fallback: hardcoded common skills */ }
    if (!_allSkills.length) {
      _allSkills = ["code_editor","grep_search","online_search","browser_control","execute_command",
        "file_uploader","http_client","deep_research","document_skills",
        "frontend_design","graph_master","svg_creator","system_repair","testing_debugging",
        "text_humanizer","youtube_downloader","phone_control","simulacra_engine","study_suite"];
    }
  }

  async function loadAvailableTools() {
    try {
      const resp = await fetch("/api/tools");
      if (resp.ok) {
        const data = await resp.json();
        _allTools = (data.tools || data || []).map(t => typeof t === "string" ? t : t.key || t.name);
      }
    } catch (e) { /* fallback */ }
    if (!_allTools.length) {
      _allTools = ["code_editor","grep_search","background_command","ask_user",
        "file_uploader","tracknote_manager","multi_agent","mcp"];
    }
  }

  async function loadModelSettings() {
    await loadAvailableSkills();
    await loadAvailableTools();
    const container = document.getElementById("cbModelSettings");
    if (!container) return;

    try {
      // Get all registered cookbook models (deduplicated by id)
      const status = await cbFetch("/status");
      const modelMap = new Map();
      // From servers — prefer "running" status over "stopped"
      for (const s of status.servers) {
        const id = "local/" + s.model.toLowerCase().replace(/\s+/g, "-");
        if (!modelMap.has(id) || s.status === "running") {
          modelMap.set(id, { id, label: s.model, status: s.status });
        }
      }
      // From cached models (not already in servers)
      for (const c of (status.cached_models || [])) {
        const id = "local/" + (c.name || c.path.split("/").pop()).toLowerCase().replace(/\s+/g, "-").replace(/\.gguf$/, "");
        if (!modelMap.has(id)) {
          modelMap.set(id, { id, label: c.name || c.path.split("/").pop(), status: "cached" });
        }
      }
      const models = [...modelMap.values()];

      if (!models.length) {
        container.innerHTML = '<p class="muted" style="font-size:12px;font-style:italic;">No models configured yet. Serve or download a model first.</p>';
        return;
      }

      // Fetch existing settings
      let allSettings = {};
      try {
        const settingsResp = await cbFetch("/model-settings");
        allSettings = settingsResp.settings || {};
      } catch (e) { /* no settings yet */ }

      container.innerHTML = models.map(m => {
        const _defaults = { use_maria: true, use_output_format: true, use_memory: true, use_utilities: true, skills: [], tools: [], distilled: false };
        const cfg = { ..._defaults, ...(allSettings[m.id] || {}) };
        const isDistilled = cfg.distilled;
        const disabledAttr = isDistilled ? "disabled" : "";
        const grayClass = isDistilled ? "cb-ms-grayed" : "";

        const skillTags = (cfg.skills || []).map(sk =>
          `<span class="cb-skill-tag">${sk}<button class="cb-skill-rm" data-model="${m.id}" data-skill="${sk}">×</button></span>`
        ).join("");

        const skillOptions = _allSkills
          .filter(sk => !(cfg.skills || []).includes(sk))
          .map(sk => `<option value="${sk}">${sk}</option>`)
          .join("");

        const toolTags = (cfg.tools || []).map(t =>
          `<span class="cb-tool-tag">${t}<button class="cb-tool-rm" data-model="${m.id}" data-tool="${t}">×</button></span>`
        ).join("");

        const toolOptions = _allTools
          .filter(t => !(cfg.tools || []).includes(t))
          .map(t => `<option value="${t}">${t}</option>`)
          .join("");

        return `
        <div class="cb-model-settings-card" data-model-id="${m.id}">
          <div class="cb-ms-header">
            <span class="cb-ms-name">${m.label}</span>
            <span class="cb-ms-status ${m.status}">${m.status}</span>
          </div>
          <div class="cb-ms-body ${grayClass}">
            <label class="cb-ms-toggle">
              <input type="checkbox" ${cfg.use_maria ? "checked" : ""} ${disabledAttr} data-model="${m.id}" data-key="use_maria">
              <span>Maria.md (persona)</span>
            </label>
            <label class="cb-ms-toggle">
              <input type="checkbox" ${cfg.use_output_format ? "checked" : ""} ${disabledAttr} data-model="${m.id}" data-key="use_output_format">
              <span>output_format.md</span>
            </label>
            <label class="cb-ms-toggle">
              <input type="checkbox" ${cfg.use_memory ? "checked" : ""} ${disabledAttr} data-model="${m.id}" data-key="use_memory">
              <span>Memory</span>
            </label>
            <label class="cb-ms-toggle">
              <input type="checkbox" ${cfg.use_utilities ? "checked" : ""} ${disabledAttr} data-model="${m.id}" data-key="use_utilities">
              <span>Utilities <em class="muted">(schedule, chat_title)</em></span>
            </label>
            <div class="cb-ms-skills">
              <div class="cb-ms-skills-label">Skills</div>
              <div class="cb-ms-skill-tags">${skillTags || '<span class="muted" style="font-size:11px;">none</span>'}</div>
              <div class="cb-ms-skill-add">
                <select class="cb-skill-select" data-model="${m.id}" ${disabledAttr}>
                  <option value="">+ add skill</option>
                  ${skillOptions}
                </select>
              </div>
            </div>
            <div class="cb-ms-skills">
              <div class="cb-ms-skills-label">Tools</div>
              <div class="cb-ms-skill-tags">${toolTags || '<span class="muted" style="font-size:11px;">none</span>'}</div>
              <div class="cb-ms-skill-add">
                <select class="cb-tool-select" data-model="${m.id}" ${disabledAttr}>
                  <option value="">+ add tool</option>
                  ${toolOptions}
                </select>
              </div>
            </div>
          </div>
          <div class="cb-ms-footer">
            <label class="cb-ms-toggle cb-ms-distilled">
              <input type="checkbox" ${isDistilled ? "checked" : ""} data-model="${m.id}" data-key="distilled">
              <span>⚡ Distilled Instruction <em class="muted">(minimal prompt, disables above)</em></span>
            </label>
          </div>
        </div>`;
      }).join("");

      // Bind events
      container.querySelectorAll("input[type=checkbox]").forEach(cb => {
        cb.onchange = async () => {
          const modelId = cb.dataset.model;
          const key = cb.dataset.key;
          const body = { [key]: cb.checked };
          try {
            await cbFetch("/model-settings/" + modelId, { method: "PUT", body: JSON.stringify(body) });
            if (key === "distilled") loadModelSettings(); // re-render to gray/ungray
          } catch (e) { showToast("Save failed: " + e.message, true); }
        };
      });

      container.querySelectorAll(".cb-skill-select").forEach(sel => {
        sel.onchange = async () => {
          if (!sel.value) return;
          const modelId = sel.dataset.model;
          const card = container.querySelector(`[data-model-id="${modelId}"]`);
          const currentSkills = [...card.querySelectorAll(".cb-skill-tag")].map(t => t.textContent.replace("×", "").trim());
          currentSkills.push(sel.value);
          try {
            await cbFetch("/model-settings/" + modelId, { method: "PUT", body: JSON.stringify({ skills: currentSkills }) });
            loadModelSettings();
          } catch (e) { showToast("Save failed: " + e.message, true); }
        };
      });

      container.querySelectorAll(".cb-skill-rm").forEach(btn => {
        btn.onclick = async () => {
          const modelId = btn.dataset.model;
          const skill = btn.dataset.skill;
          const card = container.querySelector(`[data-model-id="${modelId}"]`);
          const currentSkills = [...card.querySelectorAll(".cb-skill-tag")].map(t => t.textContent.replace("×", "").trim()).filter(s => s !== skill);
          try {
            await cbFetch("/model-settings/" + modelId, { method: "PUT", body: JSON.stringify({ skills: currentSkills }) });
            loadModelSettings();
          } catch (e) { showToast("Save failed: " + e.message, true); }
        };
      });

      container.querySelectorAll(".cb-tool-select").forEach(sel => {
        sel.onchange = async () => {
          if (!sel.value) return;
          const modelId = sel.dataset.model;
          const card = container.querySelector(`[data-model-id="${modelId}"]`);
          const currentTools = [...card.querySelectorAll(".cb-tool-tag")].map(t => t.textContent.replace("×", "").trim());
          currentTools.push(sel.value);
          try {
            await cbFetch("/model-settings/" + modelId, { method: "PUT", body: JSON.stringify({ tools: currentTools }) });
            loadModelSettings();
          } catch (e) { showToast("Save failed: " + e.message, true); }
        };
      });

      container.querySelectorAll(".cb-tool-rm").forEach(btn => {
        btn.onclick = async () => {
          const modelId = btn.dataset.model;
          const tool = btn.dataset.tool;
          const card = container.querySelector(`[data-model-id="${modelId}"]`);
          const currentTools = [...card.querySelectorAll(".cb-tool-tag")].map(t => t.textContent.replace("×", "").trim()).filter(t => t !== tool);
          try {
            await cbFetch("/model-settings/" + modelId, { method: "PUT", body: JSON.stringify({ tools: currentTools }) });
            loadModelSettings();
          } catch (e) { showToast("Save failed: " + e.message, true); }
        };
      });

      // Prune stale settings for models that no longer exist
      const modelIds = new Set(models.map(m => m.id));
      for (const settingsKey of Object.keys(allSettings)) {
        if (!modelIds.has(settingsKey)) {
          cbFetch("/model-settings/" + settingsKey, { method: "DELETE" }).catch(() => {});
        }
      }

    } catch (e) {
      container.innerHTML = '<p class="muted" style="font-size:12px;color:var(--error);">Failed to load model settings</p>';
    }
  }

  // ─── Init ───────────────────────────────────────────────────────────────────
  // ─── Loading Skeletons ──────────────────────────────────────────────────────
  function showSkeleton(elId, lines = 3) {
    const el = document.getElementById(elId);
    if (!el) return;
    let html = '<div class="cb-skeleton-wrap">';
    for (let i = 0; i < lines; i++) {
      html += `<div class="cb-skeleton-line" style="width:${60 + Math.random() * 40}%;animation-delay:${i * 0.15}s"></div>`;
    }
    html += '</div>';
    el.innerHTML = html;
  }

  function showGridSkeleton(elId, cards = 4) {
    const el = document.getElementById(elId);
    if (!el) return;
    let html = '';
    for (let i = 0; i < cards; i++) {
      html += `
        <div class="cb-preset-card cb-skeleton-card">
          <div class="cb-skeleton-line" style="width:70%;height:16px;margin-bottom:8px;animation-delay:${i * 0.1}s"></div>
          <div class="cb-skeleton-line" style="width:90%;height:10px;margin-bottom:6px;animation-delay:${i * 0.1 + 0.05}s"></div>
          <div class="cb-skeleton-line" style="width:50%;height:10px;animation-delay:${i * 0.1 + 0.1}s"></div>
        </div>`;
    }
    el.innerHTML = html;
  }

  function initCookbook() {
    // Show loading skeletons immediately
    showSkeleton("cbHardwareInfo", 2);
    showGridSkeleton("cbPresetsGrid", 4);

    // Fast stuff first — no awaits blocking each other
    loadCookbookSettings();
    initCustomDownload();
    initModelSearch();
    initSaveSettings();

    // Status + model settings (independent of recommendations)
    refreshCookbook();
    loadModelSettings();

    // Recommendations: poll until dynamic HF models arrive (skeleton stays visible)
    let recAttempts = 0;
    const REC_MAX_ATTEMPTS = 20; // 20 × 3s = 60s max
    const REC_POLL_MS = 3000;

    function fetchRecommendations() {
      cbFetch("/recommendations")
        .then((data) => {
          renderHardware(data.hardware);
          if (data.has_dynamic) {
            // Real HF models loaded — render and stop polling
            renderRecommendations(data.recommendations);
          } else if (recAttempts < REC_MAX_ATTEMPTS) {
            // Only static catalog so far — keep skeleton, poll again
            recAttempts++;
            setTimeout(fetchRecommendations, REC_POLL_MS);
          } else {
            // Timeout — show whatever we have with a note
            renderRecommendations(data.recommendations);
            showToast("Model catalog still loading. Refresh to retry.", true);
          }
        })
        .catch(() => {
          const hwEl = document.getElementById("cbHardwareInfo");
          if (hwEl) hwEl.innerHTML = '<p class="muted" style="font-size:12px;font-style:italic;">Failed to load hardware info</p>';
          const recEl = document.getElementById("cbPresetsGrid");
          if (recEl) recEl.innerHTML = '<p class="muted" style="font-size:12px;font-style:italic;">Failed to load recommendations</p>';
        });
    }

    fetchRecommendations();
  }

  // ─── Tab Hook ───────────────────────────────────────────────────────────────
  // Expose init globally so library.js tab handler can call it
  window._cbInit = initCookbook;

  // ─── Utilities ──────────────────────────────────────────────────────────────
  function escHtml(str) {
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
})();
