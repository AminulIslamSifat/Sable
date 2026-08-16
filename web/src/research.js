/* ---------- Deep Research Panel (extracted from telegram.js) ---------- */
/* Depends on: escHtml, showToast, getToken, modelList, openLibraryReader, renderMarkdownSimple */

    // sessionId -> { eventSource, card, query } — supports concurrent runs.
    let _researchRuns = new Map();

    const _R_PHASE_LABEL = {
      starting: "starting", planning: "planning strategy", searching: "searching the web",
      reading: "reading sources", writing: "synthesizing", done: "complete", error: "error",
    };

    function _fmtMMSS(s) {
      return String(Math.floor(s / 60)).padStart(2, "0") + ":" + String(Math.floor(s % 60)).padStart(2, "0");
    }

    // Single global ticker: drives a real-time elapsed clock and a stall
    // detector for every active run, independent of server events. This is
    // what tells the user a run is alive vs. sitting on a long model call.
    let _researchTickerStarted = false;
    function ensureResearchTicker() {
      if (_researchTickerStarted) return;
      _researchTickerStarted = true;
      setInterval(() => {
        const now = Date.now();
        _researchRuns.forEach((run) => {
          if (!run.card || run.done) return;
          const elapsedEl = run.card.querySelector(".research-elapsed");
          if (elapsedEl) elapsedEl.textContent = _fmtMMSS((now - run.startedAt) / 1000);
          const idle = Math.floor((now - (run.lastEventAt || run.startedAt)) / 1000);
          const dot = run.card.querySelector(".research-live-dot");
          const note = run.card.querySelector(".research-live-note");
          if (!dot) return;
          if (idle > 45) {
            dot.className = "research-live-dot stall";
            if (note) { note.hidden = false; note.textContent = "⚠ no update for " + idle + "s — a long model call, or possibly stuck"; }
          } else if (idle > 15) {
            dot.className = "research-live-dot wait";
            if (note) { note.hidden = false; note.textContent = "working… " + idle + "s since last update"; }
          } else {
            dot.className = "research-live-dot live";
            if (note) { note.hidden = true; note.textContent = ""; }
          }
        });
      }, 1000);
    }

    async function renderResearchPanel(container) {
      container.innerHTML = "";
      container.classList.add("research-panel");
      ensureResearchTicker();

      // ── Top: launch card ──
      const launch = document.createElement("div");
      launch.className = "research-launch";
      launch.innerHTML = `
        <div class="research-launch-head">
          <span class="research-launch-title"><span class="icon-emoji">🔬</span><i data-lucide="microscope" class="icon-lucide"></i> Deep Research</span>
          <span class="research-launch-sub">LLM-in-the-loop · multi-round web research</span>
        </div>
        <textarea id="researchQuery" class="research-query" rows="3"
          placeholder="e.g. Compare Rust and Go for a high-throughput web API in 2026…"></textarea>
        <div class="research-controls">
          <label class="research-field">Depth
            <select id="researchDepth">
              <option value="1">1</option><option value="2">2</option>
              <option value="3" selected>3</option><option value="4">4</option>
              <option value="5">5</option>
            </select>
          </label>
          <label class="research-field">Time limit
            <select id="researchTime">
              <option value="600">10 min</option><option value="1500" selected>25 min</option>
              <option value="2400">40 min</option><option value="3600">60 min</option>
            </select>
          </label>
          <label class="research-field">Pages / topic
            <select id="researchPages">
              ${Array.from({ length: 20 }, (_, i) => `<option value="${i + 1}"${i + 1 === 3 ? " selected" : ""}>${i + 1}</option>`).join("")}
            </select>
          </label>
          <span class="research-spacer"></span>
          <button id="researchStartBtn" class="research-start-btn">▶ Start Research</button>
        </div>
        <div class="research-controls research-controls-fallback">
          <div class="research-fallback-col">
            <div class="research-fallback-label"><span class="icon-emoji">🧠</span><i data-lucide="cpu" class="icon-lucide"></i> Model <span class="research-hint">top = 1st choice, then fallbacks</span></div>
            <select id="researchModel1" class="research-select"></select>
            <select id="researchModel2" class="research-select"></select>
            <select id="researchModel3" class="research-select"></select>
          </div>
          <div class="research-fallback-col">
            <div class="research-fallback-label"><span class="icon-emoji">👤</span><i data-lucide="user" class="icon-lucide"></i> Account <span class="research-hint">top = 1st choice, then fallbacks</span></div>
            <select id="researchAccount1" class="research-select"></select>
            <select id="researchAccount2" class="research-select"></select>
            <select id="researchAccount3" class="research-select"></select>
          </div>
        </div>
        <div id="researchStatus" class="research-status hidden"></div>
      `;
      container.appendChild(launch);
      if (window.lucide) lucide.createIcons({ nodes: launch.querySelectorAll("[data-lucide]") });
      populateResearchSelectors(launch);

      // ── Middle: active runs (concurrent) ──
      const activeWrap = document.createElement("div");
      activeWrap.className = "research-active";
      activeWrap.id = "researchActiveRuns";
      container.appendChild(activeWrap);

      // ── Bottom: past research library ──
      const libWrap = document.createElement("div");
      libWrap.className = "research-library";
      libWrap.innerHTML = `
        <div class="research-lib-head"><i data-lucide="file-text" style="width:14px;height:14px;display:inline;vertical-align:middle;margin-right:4px;"></i> Past Research</div>
        <div id="researchLibList" class="library-loading">Loading…</div>
      `;
      container.appendChild(libWrap);

      launch.querySelector("#researchStartBtn").addEventListener("click", startResearch);
      launch.querySelector("#researchQuery").addEventListener("keydown", (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key === "Enter") startResearch();
      });

      loadResearchLibrary();

      // Restore active runs: first from in-memory Map (tab re-render), then
      // discover any server-side runs we lost (tab close + reopen).
      const restoredSids = new Set();
      _researchRuns.forEach((run, sid) => {
        if (run.done) return;
        const prior = run.nodesList || [];
        createRunCard(sid, run.query);
        const fresh = _researchRuns.get(sid);
        fresh.nodesList = prior.slice();
        prior.forEach((n) => fresh.graph.addNode(n));
        attachResearchStream(sid);
        restoredSids.add(sid);
      });

      // Discover server-side active sessions not already in memory.
      try {
        const res = await fetch("/api/research/active");
        const data = await res.json().catch(() => ({ active: [] }));
        for (const s of (data.active || [])) {
          if (!s.session_id || restoredSids.has(s.session_id)) continue;
          // Fetch current status so the card isn't blank until next SSE event.
          let initialProgress = s.progress || {};
          let startedAt = Date.now();
          try {
            const stRes = await fetch("/api/research/status/" + s.session_id);
            const stData = await stRes.json().catch(() => null);
            if (stData) {
              initialProgress = stData.progress || initialProgress;
              if (stData.started_at) startedAt = stData.started_at * 1000;
            }
          } catch {}
          createRunCard(s.session_id, s.query || "Research");
          const run = _researchRuns.get(s.session_id);
          if (run) {
            run.startedAt = startedAt;
            renderRunProgress(run, initialProgress);
          }
          attachResearchStream(s.session_id);
          restoredSids.add(s.session_id);
        }
      } catch (e) {
        console.warn("research: failed to discover active sessions", e);
      }
    }

    // Parse an API response as JSON, but surface a readable error if the server
    // returned non-JSON (e.g. an unhandled 500 plain-text body).
    async function _researchParse(res) {
      const text = await res.text();
      try { return JSON.parse(text); }
      catch {
        const err = new Error((text || "").trim().slice(0, 200) || ("HTTP " + res.status));
        err.status = res.status;
        throw err;
      }
    }

    // Horizontal live research graph: root (left) -> topics -> pages -> report.
    class ResearchGraph {
      constructor(svg, rootLabel) {
        this.svg = svg;
        this.NS = "http://www.w3.org/2000/svg";
        this.nodes = new Map();
        this.reportAdded = false;
        // Create the root node immediately with the research topic.
        this.addNode({ id: "__root__", parent: null, depth: 0, kind: "root", label: rootLabel || "Research", status: "expanding" });
      }
      _short(s, max) { s = s || ""; max = max || 18; return s.length > max ? s.slice(0, max - 1) + "…" : s; }
      _cls(rec) { return "rg-node rg-" + rec.kind + " rg-s-" + rec.status; }
      _radius(kind) {
        if (kind === "root") return 12;
        if (kind === "report") return 10;
        if (kind === "topic") return 6;
        return 4.5; // page
      }
      addNode(n) {
        // Backend root node maps to our __root__ (skip for our own __root__ creation)
        if (n.kind === "root" && n.id !== "__root__") {
          this._backendRootId = n.id;
          this.setStatus("__root__", n.status || "expanding");
          return;
        }
        if (this.nodes.has(n.id)) { this.setStatus(n.id, n.status); return; }
        // Remap backend root ID to __root__, and force depth-0 topics to __root__
        let parentId = n.parent || null;
        if (parentId === this._backendRootId) parentId = "__root__";
        if (n.depth <= 1 && n.kind === "topic") parentId = "__root__";
        const depth = (n.depth || 0) + 1;
        // Pages always spawn gray (pending); status updates come via graph_status events.
        const status = (n.kind === "page") ? "pending" : (n.status || "pending");
        const rec = { id: n.id, parent: parentId, depth, kind: n.kind, label: n.label, status, spawnTime: Date.now() };
        const g = document.createElementNS(this.NS, "g");
        g.setAttribute("class", this._cls(rec));
        const c = document.createElementNS(this.NS, "circle");
        const radius = this._radius(n.kind);
        c.setAttribute("r", radius);
        g.appendChild(c);
        if (n.kind !== "page") {
          const t = document.createElementNS(this.NS, "text");
          t.setAttribute("y", radius + 12);
          t.textContent = this._short(n.label, n.kind === "root" ? 24 : 18);
          g.appendChild(t);
        }
        this.svg.appendChild(g);
        rec.el = g;
        rec.edgeEl = null; // edges drawn in layout()
        if (n._leafParents) rec.leafParents = n._leafParents;
        this.nodes.set(n.id, rec);
        this.layout();
      }
      setStatus(id, status) {
        // Remap backend root to __root__
        if (id === this._backendRootId) id = "__root__";
        const rec = this.nodes.get(id);
        if (!rec) return;
        // Pages: enforce minimum 600ms in "pending" state so user sees gray before transition
        if (rec.kind === "page" && rec.spawnTime && (Date.now() - rec.spawnTime) < 600) {
          const delay = 600 - (Date.now() - rec.spawnTime);
          setTimeout(() => {
            rec.status = status;
            if (rec.el) rec.el.setAttribute("class", this._cls(rec));
          }, delay);
          return;
        }
        rec.status = status;
        if (rec.el) rec.el.setAttribute("class", this._cls(rec));
      }
      // Called when research completes: adds a report node connected to all leaf pages.
      addReport() {
        if (this.reportAdded) return;
        this.reportAdded = true;
        // Find all leaf nodes (pages with no children)
        const childIds = new Set();
        this.nodes.forEach((rec) => { if (rec.parent) childIds.add(rec.parent); });
        const leaves = [];
        this.nodes.forEach((rec) => {
          if (rec.kind === "page" && !childIds.has(rec.id)) leaves.push(rec.id);
        });
        // If no pages, connect to deepest topics instead
        if (!leaves.length) {
          let maxD = 0;
          this.nodes.forEach((rec) => { if (rec.depth > maxD && rec.kind === "topic") maxD = rec.depth; });
          this.nodes.forEach((rec) => { if (rec.depth === maxD && rec.kind === "topic") leaves.push(rec.id); });
        }
        const maxDepth = Math.max(...[...this.nodes.values()].map(r => r.depth), 0);
        const reportId = "__report__";
        const rec = { id: reportId, parent: null, depth: maxDepth + 1, kind: "report", label: "Report", status: "done", parents: leaves };
        // Create edges from all leaves to report
        rec.edgeEls = [];
        leaves.forEach((leafId) => {
          const path = document.createElementNS(this.NS, "path");
          path.setAttribute("class", "rg-edge");
          this.svg.appendChild(path);
          rec.edgeEls.push({ el: path, from: leafId });
        });
        const g = document.createElementNS(this.NS, "g");
        g.setAttribute("class", this._cls(rec));
        const c = document.createElementNS(this.NS, "circle");
        c.setAttribute("r", this._radius("report"));
        g.appendChild(c);
        const t = document.createElementNS(this.NS, "text");
        t.setAttribute("y", this._radius("report") + 12);
        t.textContent = "Report";
        g.appendChild(t);
        this.svg.appendChild(g);
        rec.el = g;
        this.nodes.set(reportId, rec);
        this.layout();
      }
      layout() {
        const PAD_TOP = 28; // extra space so top labels aren't clipped
        const PAD_BOTTOM = 20;
        const byDepth = new Map();
        this.nodes.forEach((rec) => {
          const d = rec.depth || 0;
          if (!byDepth.has(d)) byDepth.set(d, []);
          byDepth.get(d).push(rec);
        });
        const depths = [...byDepth.keys()].sort((a, b) => a - b);
        const colW = 130;
        const W = depths.length * colW + 60;
        let maxInCol = 1;
        byDepth.forEach((arr) => { maxInCol = Math.max(maxInCol, arr.length); });
        const H = Math.max(160, maxInCol * 38 + PAD_TOP + PAD_BOTTOM);
        this.svg.setAttribute("viewBox", "0 0 " + W + " " + H);
        this.svg.style.aspectRatio = W + " / " + H;
        this.svg.style.width = W + "px";
        this.svg.style.maxWidth = "100%";
        const pos = new Map();
        depths.forEach((d, di) => {
          const arr = byDepth.get(d);
          const x = 40 + di * colW;
          arr.forEach((rec, i) => {
            pos.set(rec.id, { x, y: PAD_TOP + ((H - PAD_TOP - PAD_BOTTOM) / (arr.length + 1)) * (i + 1) });
          });
        });
        this.nodes.forEach((rec) => {
          const p = pos.get(rec.id);
          if (!p) return;
          if (rec.el) rec.el.setAttribute("transform", "translate(" + p.x + "," + p.y + ")");
          // Standard single-parent edge — create lazily if parent exists
          if (rec.parent && pos.has(rec.parent)) {
            if (!rec.edgeEl) {
              rec.edgeEl = document.createElementNS(this.NS, "path");
              rec.edgeEl.setAttribute("class", "rg-edge");
              this.svg.insertBefore(rec.edgeEl, this.svg.firstChild);
            }
            const q = pos.get(rec.parent);
            const mx = (q.x + p.x) / 2;
            rec.edgeEl.setAttribute("d", "M " + q.x + " " + q.y + " C " + mx + " " + q.y + ", " + mx + " " + p.y + ", " + p.x + " " + p.y);
          }
          // Multi-parent edges (report node connected to leaf pages)
          if (rec.leafParents && !rec.edgeEls) {
            rec.edgeEls = rec.leafParents.map((fromId) => {
              const el = document.createElementNS(this.NS, "path");
              el.setAttribute("class", "rg-edge");
              this.svg.insertBefore(el, this.svg.firstChild);
              return { el, from: fromId };
            });
          }
          if (rec.edgeEls) {
            rec.edgeEls.forEach(({ el, from }) => {
              if (!pos.has(from)) return;
              const q = pos.get(from);
              const mx = (q.x + p.x) / 2;
              el.setAttribute("d", "M " + q.x + " " + q.y + " C " + mx + " " + q.y + ", " + mx + " " + p.y + ", " + p.x + " " + p.y);
            });
          }
        });
      }
    }

    // Fill one fallback slot select. `options` = [{value,label}]. If `noneLabel`
    // is given, an empty "skip" option is placed first. `preselect` sets the
    // initial value (falls back to the first option when absent).
    function _fillResearchSlot(sel, options, noneLabel, preselect) {
      if (!sel) return;
      sel.innerHTML = "";
      if (noneLabel) {
        const o = document.createElement("option");
        o.value = ""; o.textContent = noneLabel;
        sel.appendChild(o);
      }
      (options || []).forEach((op) => {
        const o = document.createElement("option");
        o.value = op.value; o.textContent = op.label;
        sel.appendChild(o);
      });
      const has = (v) => [...sel.options].some((o) => o.value === v);
      sel.value = (preselect != null && has(preselect)) ? preselect : (sel.options[0] ? sel.options[0].value : "");
    }

    // Populate the model + account fallback slots, one dropdown per choice.
    // Reuses the same sources as General Settings → Context Pass: `modelList`
    // for ALL models (Qwen + API backends — the engine routes each by backend)
    // and the /api/settings/accounts endpoint for Qwen browser profiles.
    // Persist research selector choices in localStorage so they survive re-renders.
    const _R_STORAGE_KEY = "sable_research_config";
    function _loadResearchConfig() {
      try { return JSON.parse(localStorage.getItem(_R_STORAGE_KEY)) || {}; } catch { return {}; }
    }
    function _saveResearchConfig() {
      const cfg = {
        models: [
          document.getElementById("researchModel1")?.value || "",
          document.getElementById("researchModel2")?.value || "",
          document.getElementById("researchModel3")?.value || "",
        ],
        accounts: [
          document.getElementById("researchAccount1")?.value || "",
          document.getElementById("researchAccount2")?.value || "",
          document.getElementById("researchAccount3")?.value || "",
        ],
        depth: document.getElementById("researchDepth")?.value || "3",
        time: document.getElementById("researchTime")?.value || "1500",
        pages: document.getElementById("researchPages")?.value || "3",
      };
      try { localStorage.setItem(_R_STORAGE_KEY, JSON.stringify(cfg)); } catch {}
    }

    async function populateResearchSelectors(scope) {
      const saved = _loadResearchConfig();
      const allModels = ((typeof modelList !== "undefined" && Array.isArray(modelList)) ? modelList : [])
        .map((m) => ({ value: m.id, label: m.name || m.label || m.id }));
      const modelIds = new Set(allModels.map((m) => m.value));
      // Use saved values if they still exist in the model list; otherwise fall back to defaults.
      const sm = saved.models || [];
      _fillResearchSlot(scope.querySelector("#researchModel1"), allModels, null,
        (sm[0] && modelIds.has(sm[0])) ? sm[0] : (allModels[0] ? allModels[0].value : ""));
      _fillResearchSlot(scope.querySelector("#researchModel2"), allModels, "— no 2nd model —",
        (sm[1] && modelIds.has(sm[1])) ? sm[1] : "");
      _fillResearchSlot(scope.querySelector("#researchModel3"), allModels, "— no 3rd model —",
        (sm[2] && modelIds.has(sm[2])) ? sm[2] : "");

      let accounts = [], active = "";
      try {
        const data = await fetch("/api/settings/accounts").then((r) => r.json());
        accounts = ((data && data.accounts) || []).map((a) => ({
          value: a.name, label: a.email ? a.name + " (" + a.email + ")" : a.name,
        }));
        active = (data && data.active) || "";
      } catch (e) { /* leave accounts empty → default option */ }
      const accountIds = new Set(accounts.map((a) => a.value));
      const primary = accounts.some((a) => a.value === active) ? active : (accounts[0] ? accounts[0].value : "");
      const sa = saved.accounts || [];
      _fillResearchSlot(scope.querySelector("#researchAccount1"), accounts, accounts.length ? null : "Default (active account)",
        (sa[0] && accountIds.has(sa[0])) ? sa[0] : primary);
      _fillResearchSlot(scope.querySelector("#researchAccount2"), accounts, "— no 2nd account —",
        (sa[1] && accountIds.has(sa[1])) ? sa[1] : "");
      _fillResearchSlot(scope.querySelector("#researchAccount3"), accounts, "— no 3rd account —",
        (sa[2] && accountIds.has(sa[2])) ? sa[2] : "");

      // Restore depth/time selectors too.
      if (saved.depth) { const el = scope.querySelector("#researchDepth"); if (el) el.value = saved.depth; }
      if (saved.time) { const el = scope.querySelector("#researchTime"); if (el) el.value = saved.time; }
      if (saved.pages) { const el = scope.querySelector("#researchPages"); if (el) el.value = saved.pages; }

      // Auto-save on any selector change.
      scope.querySelectorAll("#researchModel1,#researchModel2,#researchModel3,#researchAccount1,#researchAccount2,#researchAccount3,#researchDepth,#researchTime,#researchPages")
        .forEach((el) => el.addEventListener("change", _saveResearchConfig));
    }

    async function startResearch() {
      const qEl = document.getElementById("researchQuery");
      const query = qEl ? qEl.value.trim() : "";
      if (!query) { flashResearchStatus("Type a research question first.", true); return; }
      const depth = parseInt(document.getElementById("researchDepth")?.value || "3", 10);
      const maxTime = parseInt(document.getElementById("researchTime")?.value || "1500", 10);
      const pagesPerTopic = parseInt(document.getElementById("researchPages")?.value || "3", 10);
      // Read the three fallback slots in priority order; drop empties + dupes.
      const _ordered = (...vals) => vals.filter(Boolean).filter((v, i, a) => a.indexOf(v) === i);
      const models = _ordered(
        document.getElementById("researchModel1")?.value,
        document.getElementById("researchModel2")?.value,
        document.getElementById("researchModel3")?.value,
      );
      const accounts = _ordered(
        document.getElementById("researchAccount1")?.value,
        document.getElementById("researchAccount2")?.value,
        document.getElementById("researchAccount3")?.value,
      );
      try {
        const res = await fetch("/api/research/start", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ query, max_depth: depth, max_time: maxTime, pages_per_topic: pagesPerTopic, models, browser_data: accounts }),
        });
        const data = await _researchParse(res);
        if (!res.ok) throw new Error((data && (data.detail || data.error)) || ("Request failed (" + res.status + ")"));
        createRunCard(data.session_id, query);
        renderRunProgress(_researchRuns.get(data.session_id), data.progress || { phase: "starting", status: "queued" });
        attachResearchStream(data.session_id);
      } catch (e) {
        flashResearchStatus("Start failed: " + e.message, true);
      }
    }

    function createRunCard(sessionId, query) {
      const wrap = document.getElementById("researchActiveRuns");
      const card = document.createElement("div");
      card.className = "research-run-card";
      card.dataset.sid = sessionId;
      card.innerHTML = `
        <div class="research-run-head">
          <span class="research-run-title" title="${escHtml(query)}">${escHtml(query)}</span>
          <button class="research-stop-btn" title="Stop this research">⏹ Stop</button>
        </div>
        <div class="research-anim"><svg class="rg-svg" preserveAspectRatio="xMidYMid meet"></svg></div>
        <div class="research-run-status"></div>
      `;
      card.querySelector(".research-stop-btn").addEventListener("click", () => cancelResearch(sessionId));
      if (wrap) wrap.prepend(card);
      const now = Date.now();
      _researchRuns.set(sessionId, {
        eventSource: null, card, query,
        graph: new ResearchGraph(card.querySelector(".rg-svg"), query),
        nodesList: [], done: false,
        startedAt: now, lastEventAt: now, lastPhase: "starting", lastDetail: "",
        lastModel: "", lastAccount: "",
      });
      return card;
    }

    function renderRunProgress(run, p) {
      if (!run || !run.card) return;
      const card = run.card;
      const statusEl = card.querySelector(".research-run-status");
      if (!statusEl) return;
      const phase = p.phase || "starting";
      const label = _R_PHASE_LABEL[phase] || p.status || phase;
      const topics = p.topics || 0;
      const pages = p.pages || 0;
      const sources = p.sources || 0;
      const isDone = phase === "done";
      const isErr = phase === "error";
      // Track liveness + the active model/account for the stall detector and label.
      run.lastEventAt = Date.now();
      run.lastPhase = label;
      if (p.status) run.lastDetail = p.status;
      if (p.model) run.lastModel = p.model;
      if (p.account) run.lastAccount = p.account;
      card.classList.toggle("done", isDone);
      card.classList.toggle("err", isErr);
      const maLine = (run.lastModel || run.lastAccount)
        ? `<span class="research-ma">${escHtml(run.lastModel || "default model")}${run.lastAccount ? " · " + escHtml(run.lastAccount) : ""}</span>`
        : "";
      // Build a prominent "currently working on" line from the live detail.
      const detail = p.status || run.lastDetail || "";
      let activityHtml = "";
      if (!isDone && !isErr && detail) {
        // Parse structured details like "searching: quantum computing" or
        // "reading: Nature article" into topic + action badges.
        const m = detail.match(/^(searching|reading|decomposing|extracting|retrying|writing)[\s:]+(.+)$/i);
        if (m) {
          const action = m[1].toLowerCase();
          const target = escHtml(m[2].trim());
          const iconName = action === "searching" ? "search" : action === "reading" ? "file-text" : action === "decomposing" ? "git-branch" : action === "extracting" ? "sparkles" : action === "writing" ? "pen-tool" : "refresh-cw";
          activityHtml = `<div class="research-activity"><span class="research-activity-icon"><i data-lucide="${iconName}" style="width:14px;height:14px;display:inline;vertical-align:middle;"></i></span><span class="research-activity-action">${escHtml(action)}</span><span class="research-activity-target">${target}</span></div>`;
        } else {
          activityHtml = `<div class="research-activity"><span class="research-activity-target">${escHtml(detail)}</span></div>`;
        }
      }
      statusEl.innerHTML = `
        <div class="research-progress-top">
          <span class="research-phase ${isErr ? "err" : ""}">
            <span class="research-live-dot ${isDone ? "done" : isErr ? "err" : "live"}"></span>
            ${isDone ? '<i data-lucide="circle-check" style="width:12px;height:12px;display:inline;vertical-align:middle;color:var(--success);"></i> ' : isErr ? '<i data-lucide="circle-x" style="width:12px;height:12px;display:inline;vertical-align:middle;color:var(--error);"></i> ' : ""}${escHtml(label)}
          </span>
          <span class="research-meta">${topics ? topics + " topics" : ""}${pages ? " · " + pages + " pages" : ""}${sources ? " · " + sources + " src" : ""} · <span class="research-elapsed">${_fmtMMSS((Date.now() - run.startedAt) / 1000)}</span></span>
        </div>
        ${activityHtml}
        <div class="research-progress-bar"><div class="research-progress-fill ${isDone ? "done" : ""} ${isErr ? "err" : ""}" style="width:${isDone ? 100 : Math.min(95, (topics * 10) + (pages * 4) + 5)}%"></div></div>
        ${maLine ? `<div class="research-status-text">${maLine}</div>` : ""}
        <div class="research-live-note" hidden></div>
      `;
      if (typeof lucide !== "undefined") lucide.createIcons();
    }

    function attachResearchStream(sessionId) {
      const run = _researchRuns.get(sessionId);
      if (!run) return;
      if (run.eventSource) { try { run.eventSource.close(); } catch {} run.eventSource = null; }
      const es = new EventSource("/api/research/events/" + sessionId + "?token=" + encodeURIComponent(getToken() || ""));
      run.eventSource = es;
      es.onmessage = (ev) => {
        try {
          const p = JSON.parse(ev.data);
          const t = p.type;
          // Any event = proof of life. Reset the stall clock even for graph
          // node/status events that don't go through renderRunProgress.
          run.lastEventAt = Date.now();
          if (t === "graph_node") {
            run.nodesList.push(p);
            // Report node: connect to all leaf pages instead of backend-assigned parent
            if (p.kind === "report") {
              const childIds = new Set();
              run.graph.nodes.forEach((rec) => { if (rec.parent) childIds.add(rec.parent); });
              const leaves = [];
              run.graph.nodes.forEach((rec) => {
                if (rec.kind === "page" && !childIds.has(rec.id)) leaves.push(rec.id);
              });
              p._leafParents = leaves.length > 0 ? leaves : [p.parent];
              p.parent = null; // prevent default single-parent edge
            }
            run.graph.addNode(p);
          } else if (t === "graph_status") {
            const n = run.nodesList.find((x) => x.id === p.id);
            if (n) n.status = p.status;
            run.graph.setStatus(p.id, p.status);
          } else if (t === "done" || t === "error" || p.phase === "done" || p.phase === "error") {
            renderRunProgress(run, p);
            try { es.close(); } catch {}
            run.eventSource = null;
            finishRunCard(sessionId, (t === "done" || p.phase === "done"));
          } else {
            renderRunProgress(run, p);
          }
        } catch {}
      };
    }

    function finishRunCard(sessionId, ok) {
      const run = _researchRuns.get(sessionId);
      if (!run) return;
      run.done = true;
      const card = run.card;
      // Mark root as done. Report node arrives via backend graph_node event.
      run.graph.setStatus("__root__", ok ? "done" : "failed");
      const cancelBtn = card.querySelector(".research-run-cancel");
      if (cancelBtn) cancelBtn.remove();
      if (ok) {
        const viewBtn = document.createElement("button");
        viewBtn.className = "research-view-btn";
        viewBtn.innerHTML = '<i data-lucide="book-open" style="width:14px;height:14px;display:inline;vertical-align:middle;"></i> View Report';
        viewBtn.addEventListener("click", () => viewResearchResult(sessionId));
        card.appendChild(viewBtn);
        loadResearchLibrary();
      }
    }

    async function cancelResearch(sessionId) {
      const run = _researchRuns.get(sessionId);
      const btn = run?.card?.querySelector(".research-stop-btn");
      if (btn) { btn.disabled = true; btn.textContent = "Stopping…"; }
      try {
        const res = await fetch("/api/research/cancel/" + sessionId, { method: "POST" });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || !data.cancelled) {
          flashResearchStatus("Could not stop research.", true);
          if (btn) { btn.disabled = false; btn.textContent = "⏹ Stop"; }
          return;
        }
        flashResearchStatus("Research stopped.", false);
        if (run) {
          run.done = true;
          renderRunProgress(run, { phase: "error", status: "cancelled by user" });
        }
      } catch (e) {
        flashResearchStatus("Stop failed: " + e.message, true);
        if (btn) { btn.disabled = false; btn.textContent = "⏹ Stop"; }
      }
    }

    async function viewResearchResult(sessionId) {
      try {
        const res = await fetch("/api/research/result/" + sessionId, { method: "POST" });
        const data = await res.json();
        if (!res.ok || !data.result) { flashResearchStatus("No result available yet.", true); return; }
        openResearchReportViewer(data.result, data.sources || []);
      } catch (e) {
        flashResearchStatus("Failed to load result: " + e.message, true);
      }
    }

    function openResearchReportViewer(markdown, sources) {
      // Mirror openLibraryReader: build a reader overlay and render the markdown.
      const existing = document.getElementById("libraryReaderOverlay");
      if (existing) existing.remove();
      const overlay = document.createElement("div");
      overlay.id = "libraryReaderOverlay";
      overlay.className = "settings-overlay";
      let srcHtml = "";
      if (sources && sources.length) {
        srcHtml = "<h3>Sources</h3><ul>" + sources.map((s) =>
          `<li><a href="${escHtml(s.url)}" target="_blank" rel="noopener">${escHtml(s.title || s.url)}</a></li>`
        ).join("") + "</ul>";
      }
      overlay.innerHTML = `
        <div class="settings-panel library-reader-panel">
          <div class="settings-header">
            <h2>Research Report</h2>
            <button class="icon-btn" id="libraryReaderClose"><span class="icon-emoji">✕</span><i data-lucide="x" class="icon-lucide"></i></button>
          </div>
          <div class="library-reader-content">${renderMarkdownSimple(markdown)}${srcHtml}</div>
        </div>
      `;
      document.body.appendChild(overlay);
      overlay.querySelector("#libraryReaderClose").addEventListener("click", () => overlay.remove());
      overlay.addEventListener("click", (e) => { if (e.target === overlay) overlay.remove(); });
      if (window.lucide) lucide.createIcons({ nodes: overlay.querySelectorAll("[data-lucide]") });
    }

    function flashResearchStatus(msg, isError) {
      const statusEl = document.getElementById("researchStatus");
      if (!statusEl) return;
      statusEl.classList.remove("hidden");
      statusEl.innerHTML = `<div class="research-status-text ${isError ? "err" : ""}">${escHtml(msg)}</div>`;
    }

    async function loadResearchLibrary() {
      const list = document.getElementById("researchLibList");
      if (!list) return;
      try {
        const res = await fetch("/api/library/research");
        const items = await res.json();
        if (!items.length) {
          list.innerHTML = '<div class="library-empty">No research yet. Start one above.</div>';
          return;
        }
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
          card.addEventListener("click", () => openLibraryReader("research", item.filename, item.title));
          grid.appendChild(card);
        });
        list.appendChild(grid);
        if (typeof lucide !== "undefined") lucide.createIcons();
      } catch {
        list.innerHTML = '<div class="library-empty">Failed to load research library.</div>';
      }
    }


