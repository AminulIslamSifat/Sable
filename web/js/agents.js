
/* ===== Sable Multi-Agent UI =====
 * Top bar cards, inline JSON result cards, history modal, EventSource.
 * Loaded after app.js — attaches to global scope.
 */

// --------------------------------------------------------------------------
// Helpers (app.js IIFE doesn't expose these globally)
// --------------------------------------------------------------------------
function escHtml(str) {
  return String(str).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function escAttr(str) {
  return escHtml(str).replace(/"/g, "&quot;");
}

// --------------------------------------------------------------------------
// JSON Syntax Highlighter
// --------------------------------------------------------------------------
function highlightJSON(obj) {
  const json = typeof obj === "string" ? obj : JSON.stringify(obj, null, 2);
  // Try to parse if string
  let parsed;
  try { parsed = typeof obj === "string" ? JSON.parse(obj) : obj; } catch { return escHtml(json); }
  const pretty = JSON.stringify(parsed, null, 2);
  return escHtml(pretty).replace(
    /("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d*)?(?:[eE][+\-]?\d+)?)/g,
    (match) => {
      let cls = "json-number";
      if (/^"/.test(match)) {
        cls = /:$/.test(match) ? "json-key" : "json-string";
      } else if (/true|false/.test(match)) {
        cls = "json-bool";
      } else if (/null/.test(match)) {
        cls = "json-null";
      }
      return `<span class="${cls}">${match}</span>`;
    }
  );
}

// --------------------------------------------------------------------------
// Agent Top Bar
// --------------------------------------------------------------------------
const AgentTopBar = {
  container: null,
  cards: new Map(),

  init() {
    if (this.container) return;
    this.container = document.createElement("div");
    this.container.id = "agent-top-bar";
    this.container.className = "agent-top-bar hidden";
    // Insert into the dedicated slot between model dropdown and diff toggle
    const slot = document.getElementById("agentTopBarSlot");
    if (slot) slot.appendChild(this.container);
    else document.body.appendChild(this.container);
    // Vertical wheel → horizontal scroll
    this.container.addEventListener("wheel", (e) => {
      if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) {
        e.preventDefault();
        this.container.scrollLeft += e.deltaY;
      }
    }, { passive: false });
  },

  addCard(agentId, role, task, model) {
    this.init();
    this.container.classList.remove("hidden");
    const card = document.createElement("div");
    card.className = "agent-card running";
    card.dataset.agentId = agentId;
    card.innerHTML =
      `<span class="agent-spinner"></span>` +
      `<span class="agent-role">${escHtml(role)}</span>` +
      `<span class="agent-task-preview" title="${escAttr(task)}">${escHtml(task.slice(0, 30))}${task.length > 30 ? "…" : ""}</span>`;
    card.onclick = () => AgentHistory.open(agentId);
    this.cards.set(agentId, card);
    this.container.appendChild(card);
  },

  updateCard(agentId, partial) {
    const card = this.cards.get(agentId);
    if (!card) return;
    const preview = card.querySelector(".agent-task-preview");
    if (preview && partial) preview.textContent = partial.slice(0, 30);
  },

  finishCard(agentId, summary) {
    const card = this.cards.get(agentId);
    if (!card) return;
    card.className = "agent-card completed";
    const role = card.querySelector(".agent-role")?.textContent || "agent";
    card.innerHTML = `<span class="agent-check">${lucideIcon("✓")}</span><span class="agent-role">${escHtml(role)}</span>`;
    activateLucideIcons(card);
    card.onclick = () => AgentHistory.open(agentId);
    setTimeout(() => this.removeCard(agentId), 60000);
  },

  failCard(agentId, error) {
    const card = this.cards.get(agentId);
    if (!card) return;
    card.className = "agent-card failed";
    const role = card.querySelector(".agent-role")?.textContent || "agent";
    card.innerHTML = `<span class="agent-x">${lucideIcon("✗")}</span><span class="agent-role">${escHtml(role)}</span>`;
    activateLucideIcons(card);
    card.title = error || "Failed";
    card.onclick = () => AgentHistory.open(agentId);
    setTimeout(() => this.removeCard(agentId), 60000);
  },

  removeCard(agentId) {
    const card = this.cards.get(agentId);
    if (!card) return;
    card.classList.add("fade-out");
    setTimeout(() => {
      card.remove();
      this.cards.delete(agentId);
      if (this.cards.size === 0) this.container.classList.add("hidden");
    }, 400);
  },

  clear() {
    this.cards.forEach((card) => card.remove());
    this.cards.clear();
    if (this.container) this.container.classList.add("hidden");
  },
};

// --------------------------------------------------------------------------
// Inline Result Card (JSON format)
// --------------------------------------------------------------------------
function addAgentResultCard(ev) {
  const chatEl = document.getElementById("chat");
  if (!chatEl) return;

  const data = ev.data || {};
  const isSuccess = ev.type === "agent_completed";
  const role = data.role || "agent";
  const duration = data.duration ? `${data.duration.toFixed(1)}s` : "?";
  const tokens = data.tokens || 0;

  const card = document.createElement("div");
  card.className = `agent-result-card ${isSuccess ? "arc-success" : "arc-failure"}`;

  // Result text (collapsed by default)
  const rawResult = data.result || data.summary || data.error || "";
  let jsonHtml;
  try {
    jsonHtml = highlightJSON(JSON.parse(rawResult));
  } catch {
    jsonHtml = escHtml(rawResult);
  }

  card.innerHTML =
    `<span class="arc-title">${escHtml(role)} ${isSuccess ? "completed" : "failed"}</span>` +
    `<span class="arc-detail">${isSuccess ? escHtml((data.summary || "").slice(0, 120)) : escHtml(data.error || "Unknown error")}</span>` +
    `<span class="arc-meta">${duration} · ${tokens} tokens</span>` +
    (rawResult ? `<div class="arc-json hidden">${jsonHtml}</div>` : "") +
    `<div class="arc-actions">` +
      (rawResult ? `<button class="arc-toggle">result</button>` : "") +
      `<button class="arc-expand">conversation</button>` +
    `</div>`;

  const toggleBtn = card.querySelector(".arc-toggle");
  if (toggleBtn) {
    toggleBtn.onclick = () => {
      const json = card.querySelector(".arc-json");
      json.classList.toggle("hidden");
      toggleBtn.textContent = json.classList.contains("hidden") ? "result" : "hide";
    };
  }
  card.querySelector(".arc-expand").onclick = () => AgentHistory.open(ev.agent_id);
  const pane = chatEl.querySelector(".tab-pane.active") || chatEl;
  pane.appendChild(card);

  if (typeof scrollBottom === "function") scrollBottom(true);
}

// --------------------------------------------------------------------------
// Batch Group Card (multiple agents spawned together)
// --------------------------------------------------------------------------
function addAgentBatchCard(agents) {
  const chatEl = document.getElementById("chat");
  if (!chatEl) return;

  const card = document.createElement("div");
  card.className = "agent-batch-card";

  const items = agents.map((a) =>
    `<div class="agent-batch-item" data-agent-id="${escAttr(a.id)}">` +
      `<span class="abi-status">${lucideIcon("🔄")}</span>` +
      `<span class="abi-role">${escHtml(a.role)}</span>` +
      `<span class="abi-task">${escHtml(a.task.slice(0, 40))}</span>` +
      `<span class="abi-time">running…</span>` +
    `</div>`
  ).join("");

  card.innerHTML =
    `<div class="agent-batch-header">${lucideIcon("🤖")} ${agents.length} agent${agents.length !== 1 ? "s" : ""} spawned</div>` +
    items;

  card.querySelectorAll(".agent-batch-item").forEach((el) => {
    el.style.cursor = "pointer";
    el.onclick = () => AgentHistory.open(el.dataset.agentId);
  });

  const pane = chatEl.querySelector(".tab-pane.active") || chatEl;
  pane.appendChild(card);
  activateLucideIcons(card);
  if (typeof scrollBottom === "function") scrollBottom(true);
  return card;
}

// --------------------------------------------------------------------------
// History Viewer (modal)
// --------------------------------------------------------------------------
const AgentHistory = {
  overlay: null,
  pollTimer: null,

  async open(agentId) {
    this.close(); // dismiss any existing

    const overlay = document.createElement("div");
    overlay.className = "agent-history-overlay";

    const panel = document.createElement("div");
    panel.className = "agent-history-panel";
    panel.innerHTML =
      `<div class="agent-history-header">` +
        `<h3>Agent ${escHtml(agentId)}</h3>` +
        `<button class="agent-history-close">${lucideIcon("✕")}</button>` +
      `</div>` +
      `<div class="agent-history-body"><p style="color:var(--muted)">Loading…</p></div>` +
      `<div class="agent-history-footer"></div>`;

    overlay.appendChild(panel);
    document.body.appendChild(overlay);
    activateLucideIcons(panel);
    this.overlay = overlay;

    overlay.querySelector(".agent-history-close").onclick = () => this.close();
    overlay.addEventListener("click", (e) => { if (e.target === overlay) this.close(); });
    document.addEventListener("keydown", this._escHandler);

    // Fetch messages
    try {
      const res = await fetch(`/api/agents/${agentId}/messages`);
      const data = await res.json();
      this.renderMessages(panel, data.messages || []);
      this.renderFooter(panel, agentId);
    } catch (err) {
      panel.querySelector(".agent-history-body").innerHTML =
        `<p style="color:var(--danger)">Failed to load: ${escHtml(err.message)}</p>`;
    }
  },

  renderMessages(panel, messages) {
    const body = panel.querySelector(".agent-history-body");
    body.innerHTML = "";

    if (!messages.length) {
      body.innerHTML = `<p style="color:var(--muted)">No messages recorded.</p>`;
      return;
    }

    for (const msg of messages) {
      const div = document.createElement("div");
      const role = msg.role || "user";
      div.className = `ah-msg ah-${role}`;

      // Render markdown for tool/system messages, plain text for others
      let contentHtml;
      if ((role === "tool" || role === "system") && typeof marked !== "undefined") {
        const raw = DOMPurify ? DOMPurify.sanitize(marked.parse(msg.content || "")) : marked.parse(msg.content || "");
        contentHtml = raw;
      } else {
        contentHtml = escHtml(msg.content || "");
      }

      div.innerHTML =
        `<div class="ah-msg-role">${escHtml(role)}</div>` +
        `<div class="ah-msg-content">${contentHtml}</div>`;
      body.appendChild(div);
    }
    body.scrollTop = body.scrollHeight;
  },

  renderFooter(panel, agentId) {
    // Fetch agent detail for footer stats
    fetch(`/api/agents/${agentId}`)
      .then((r) => r.json())
      .then((data) => {
        const footer = panel.querySelector(".agent-history-footer");
        if (!footer || data.error) return;
        const isRunning = data.status === "running" || data.status === "spawned";
        footer.innerHTML =
          `<span>Role: ${escHtml(data.role || "?")}</span>` +
          `<span>Model: ${escHtml(data.model || "?")}</span>` +
          `<span>Tokens: ${data.tokens_used || 0}</span>` +
          `<span>Duration: ${data.duration || 0}s</span>` +
          `<span>Status: ${escHtml(data.status || "?")}</span>` +
          (isRunning ? `<button class="agent-stop-btn" title="Stop this agent">■ stop</button>` : "");
        if (isRunning) {
          footer.querySelector(".agent-stop-btn").onclick = () => this.stopAgent(agentId, footer);
        }
      })
      .catch(() => {});
  },

  async stopAgent(agentId, footer) {
    const btn = footer.querySelector(".agent-stop-btn");
    if (btn) { btn.disabled = true; btn.textContent = "…"; }
    try {
      const res = await fetch(`/api/agents/${agentId}/kill`, { method: "POST" });
      const data = await res.json();
      if (data.status === "killed") {
        if (btn) { btn.textContent = "stopped"; btn.classList.add("stopped"); }
        // Update status text in footer
        const spans = footer.querySelectorAll("span");
        for (const s of spans) {
          if (s.textContent.startsWith("Status:")) s.textContent = "Status: stopped";
        }
      }
    } catch {
      if (btn) { btn.disabled = false; btn.textContent = "■ stop"; }
    }
  },

  close() {
    if (this.pollTimer) { clearInterval(this.pollTimer); this.pollTimer = null; }
    if (this.overlay) { this.overlay.remove(); this.overlay = null; }
    document.removeEventListener("keydown", this._escHandler);
  },

  _escHandler(e) {
    if (e.key === "Escape") AgentHistory.close();
  },
};

// --------------------------------------------------------------------------
// EventSource Connection
// --------------------------------------------------------------------------
let _agentEventSource = null;
let _agentEventChatId = null;

function connectAgentEvents(chatId) {
  if (_agentEventSource && _agentEventChatId === chatId) return; // already connected
  disconnectAgentEvents();

  _agentEventChatId = chatId;
  const token = localStorage.getItem("sable_token") || "";
  // EventSource doesn't support custom headers — use query param for auth
  _agentEventSource = new EventSource(`/api/chat/${chatId}/agent-events?token=${encodeURIComponent(token)}`);

  _agentEventSource.onmessage = (e) => {
    try {
      const ev = JSON.parse(e.data);
      handleAgentEvent(ev);
    } catch { /* ignore malformed */ }
  };

  _agentEventSource.onerror = () => {
    // EventSource auto-reconnects; just log
    console.debug("[agents] SSE connection error, will retry…");
  };
}

function disconnectAgentEvents() {
  if (_agentEventSource) {
    _agentEventSource.close();
    _agentEventSource = null;
    _agentEventChatId = null;
  }
}

function handleAgentEvent(ev) {
  switch (ev.type) {
    case "agent_spawned":
      AgentTopBar.addCard(ev.agent_id, ev.data?.role || "agent", ev.data?.task || "", ev.data?.model || "");
      break;
    case "agent_progress":
      AgentTopBar.updateCard(ev.agent_id, ev.data?.partial || "");
      break;
    case "auto_turn_trigger":
      // Agent completed — fire a normal chat turn via the standard /api/chat pipeline.
      // sendAutoTurnMessage (app.js) handles the user bubble, bot streaming, skill
      // cards, stop button, markdown, and history replay — identical to a typed message.
      if (typeof sendAutoTurnMessage === "function" && ev.data?.message) {
        sendAutoTurnMessage(ev.data.message);
      }
      break;
    case "agent_completed":
      AgentTopBar.finishCard(ev.agent_id, ev.data?.summary || "");
      addAgentResultCard(ev);
      break;
    case "agent_failed":
      AgentTopBar.failCard(ev.agent_id, ev.data?.error || "");
      addAgentResultCard(ev);
      break;
  }
}

// --------------------------------------------------------------------------
// Integration hooks (called from app.js)
// --------------------------------------------------------------------------

// Called when a chat is selected/opened
function onChatOpened(chatId) {
  AgentTopBar.clear();
  connectAgentEvents(chatId);
  // Load any active agents for this chat
  fetch(`/api/agents/active?chat_id=${encodeURIComponent(chatId)}`)
    .then((r) => r.json())
    .then((agents) => {
      if (!Array.isArray(agents)) return;
      for (const a of agents) {
        if (a.status === "running" || a.status === "spawned") {
          AgentTopBar.addCard(a.id, a.role, a.task, a.model);
        }
      }
    })
    .catch(() => {});
}

// Called when navigating away / closing chat
function onChatClosed() {
  disconnectAgentEvents();
  AgentTopBar.clear();
}

// --------------------------------------------------------------------------
// @ Mention — spawn agents from chat input
// --------------------------------------------------------------------------
const AGENT_ROLES = [
  { id: "researcher", icon: "🔍", label: "Researcher", desc: "Web search + summarize" },
  { id: "coder", icon: "💻", label: "Coder", desc: "Write/edit code" },
  { id: "reviewer", icon: "📋", label: "Reviewer", desc: "Review code quality" },
  { id: "writer", icon: "✍️", label: "Writer", desc: "Docs & content" },
  { id: "utility", icon: "⚙️", label: "Utility", desc: "General tasks" },
];

let _mentionPopup = null;
let _mentionIdx = 0;
let _mentionFiltered = [];

function _createMentionPopup() {
  if (_mentionPopup) return _mentionPopup;
  _mentionPopup = document.createElement("div");
  _mentionPopup.className = "agent-mention-popup";
  _mentionPopup.style.display = "none";
  document.body.appendChild(_mentionPopup);

  _mentionPopup.addEventListener("mousedown", (e) => {
    e.preventDefault(); // keep focus in textarea
    const item = e.target.closest("[data-role]");
    if (!item) return;
    _selectMention(item.dataset.role);
  });
  return _mentionPopup;
}

function _selectMention(role) {
  const el = document.getElementById("input");
  if (el) {
    el.value = `@${role} `;
    el.focus();
  }
  hideMentionPopup();
}

function showMentionPopup(anchorEl, filter = "") {
  const popup = _createMentionPopup();
  _mentionFiltered = AGENT_ROLES.filter((r) =>
    r.id.startsWith(filter) || r.label.toLowerCase().startsWith(filter)
  );
  if (_mentionFiltered.length === 0) { hideMentionPopup(); return; }
  _mentionIdx = 0;
  _renderMentionItems();
  const rect = anchorEl.getBoundingClientRect();
  popup.style.left = rect.left + "px";
  popup.style.bottom = (window.innerHeight - rect.top + 6) + "px";
  popup.style.display = "block";
}

function _renderMentionItems() {
  if (!_mentionPopup) return;
  _mentionPopup.innerHTML = _mentionFiltered.map((r, i) =>
    `<div class="mention-item${i === _mentionIdx ? " active" : ""}" data-role="${r.id}">
      <span class="mention-icon">${lucideIcon(r.icon)}</span>
      <span class="mention-label">${r.label}</span>
      <span class="mention-desc">${r.desc}</span>
    </div>`
  ).join("");
  activateLucideIcons(_mentionPopup);
}

function hideMentionPopup() {
  if (_mentionPopup) _mentionPopup.style.display = "none";
  _mentionFiltered = [];
  _mentionIdx = 0;
}

/** Check if message is an @agent spawn. Returns {role, task} or null. */
function parseAgentMention(text) {
  const m = text.match(/^@(\w+)\s+(.+)$/s);
  if (!m) return null;
  const role = m[1].toLowerCase();
  if (!AGENT_ROLES.some((r) => r.id === role)) return null;
  return { role, task: m[2].trim() };
}

/** Spawn agent via API. Returns response JSON. */
async function spawnAgentFromMention(role, task, chatId) {
  const res = await fetch("/api/agents/spawn", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ role, task, chat_id: chatId }),
  });
  return res.json();
}

// Wire up input listener
document.addEventListener("DOMContentLoaded", () => {
  const el = document.getElementById("input");
  if (!el) return;

  el.addEventListener("input", () => {
    const m = el.value.match(/^@(\w*)$/);
    if (m) {
      showMentionPopup(el, m[1].toLowerCase());
    } else {
      hideMentionPopup();
    }
  });

  el.addEventListener("keydown", (e) => {
    if (!_mentionPopup || _mentionPopup.style.display === "none") return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      _mentionIdx = (_mentionIdx + 1) % _mentionFiltered.length;
      _renderMentionItems();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      _mentionIdx = (_mentionIdx - 1 + _mentionFiltered.length) % _mentionFiltered.length;
      _renderMentionItems();
    } else if (e.key === "Enter" || e.key === "Tab") {
      if (_mentionFiltered.length > 0) {
        e.preventDefault();
        _selectMention(_mentionFiltered[_mentionIdx].id);
      }
    } else if (e.key === "Escape") {
      hideMentionPopup();
    }
  });
});

// --------------------------------------------------------------------------
// Settings Panel (Agents tab)
// --------------------------------------------------------------------------
const AgentSettings = {
  loaded: false,
  _roles: {},             // current role data from API
  _universalSkills: [],   // universal skills (applied to all agents)
  _allSkills: [],         // all available skill keys (for the add-skill picker)

  async load() {
    try {
      const res = await fetch("/api/agents/config", {
        headers: { Authorization: `Bearer ${localStorage.getItem("sable_token") || ""}` },
      });
      const cfg = await res.json();
      document.getElementById("agentEnabled").checked = cfg.enabled !== false;
      document.getElementById("agentGlobalMax").value = cfg.concurrency?.global_max ?? 5;
      document.getElementById("agentDeepseekMax").value = cfg.concurrency?.deepseek_max ?? 5;
      document.getElementById("agentQwenMax").value = cfg.concurrency?.qwen_max ?? 1;
      document.getElementById("agentCbThreshold").value = cfg.resilience?.circuit_breaker_threshold ?? 5;
      document.getElementById("agentCbReset").value = cfg.resilience?.circuit_breaker_reset_seconds ?? 60;
      document.getElementById("agentMaxIter").value = cfg.limits?.max_iterations ?? 25;
      document.getElementById("agentMaxConsec").value = cfg.limits?.max_consecutive_tool_calls ?? 15;
      document.getElementById("agentMaxTotal").value = cfg.limits?.max_total_tool_calls ?? 50;

      this._roles = cfg.roles || {};
      this._universalSkills = cfg.universal_skills || ["execute_command"];
      // Build allSkills from role skills + universal + known registry keys
      const skillSet = new Set(this._universalSkills);
      for (const r of Object.values(this._roles)) {
        (r.allowed_skills || []).forEach((s) => skillSet.add(s));
      }
      this._allSkills = [...skillSet].sort();
      this._renderRoles();
      this.loaded = true;
    } catch (err) {
      console.warn("[agents] Failed to load config:", err);
    }
  },

  _renderRoles() {
    const container = document.getElementById("agentRolesContainer");
    if (!container) return;
    container.innerHTML = "";


    const roleIcons = { researcher: "🔍", coder: "💻", reviewer: "👁️", writer: "✍️", utility: "🔧" };

    for (const [role, data] of Object.entries(this._roles)) {
      const card = document.createElement("div");
      card.className = "agent-role-card";
      card.dataset.role = role;

      const icon = roleIcons[role] || "🤖";
      card.innerHTML =
        `<div class="arc-header" data-role="${role}">` +
          `<span class="arc-icon">${lucideIcon(icon)}</span>` +
          `<span class="arc-name">${role}</span>` +
          `<span class="arc-model-badge">${escHtml(data.default_model)}</span>` +
          `<span class="arc-chevron">▸</span>` +
        `</div>` +
        `<div class="arc-body hidden">` +
          `<div class="arc-field">` +
            `<label>System Prompt</label>` +
            `<textarea class="arc-prompt mem-input" rows="4">${escHtml(data.system_prompt)}</textarea>` +
          `</div>` +
          `<div class="arc-field">` +
            `<label>Output Format <span class="arc-readonly-tag">read-only</span></label>` +
            `<code class="arc-output-fmt">${escHtml(data.output_format || "—")}</code>` +
          `</div>` +
          `<div class="arc-field">` +
            `<label>Default Skills <span class="arc-hint">(auto-loaded with instruction)</span></label>` +
            `<div class="arc-skills-list arc-default-skills"></div>` +
          `</div>` +
          `<div class="arc-field">` +
            `<label>Allowed Skills <span class="arc-hint">(accessible on demand)</span></label>` +
            `<div class="arc-skills-list arc-allowed-skills"></div>` +
          `</div>` +
          `<div class="arc-field arc-inline-fields">` +
            `<div><label>Model</label><input type="text" class="arc-model mem-input" value="${escAttr(data.default_model)}"></div>` +
            `<div><label>Timeout (s)</label><input type="number" class="arc-timeout mem-input" value="${data.default_timeout}" min="10" max="600"></div>` +
            `<div><label>Max Parallel</label><input type="number" class="arc-parallel mem-input" value="${data.max_parallel}" min="1" max="10"></div>` +
          `</div>` +
        `</div>`;

      // Render skill chips (two tiers)
      this._renderSkillChips(card.querySelector(".arc-default-skills"), data.default_skills || [], role + ":default");
      this._renderSkillChips(card.querySelector(".arc-allowed-skills"), data.allowed_skills || [], role + ":allowed");

      // Toggle collapse
      card.querySelector(".arc-header").onclick = () => {
        const body = card.querySelector(".arc-body");
        const chevron = card.querySelector(".arc-chevron");
        body.classList.toggle("hidden");
        chevron.textContent = body.classList.contains("hidden") ? "▸" : "▾";
      };

      container.appendChild(card);
      activateLucideIcons(card);
    }
  },

  _getSkillArray(key) {
    // key format: "role:type" e.g. "coder:default" or "coder:allowed"
    const [role, type] = key.includes(":") ? key.split(":") : [key, "allowed"];
    if (!this._roles[role]) return [];
    if (type === "default") return this._roles[role].default_skills || [];
    return this._roles[role].allowed_skills || [];
  },

  _renderSkillChips(container, skills, role) {
    container.innerHTML = "";

    // Chips wrapper (flex-row, wraps naturally)
    const wrap = document.createElement("div");
    wrap.className = "arc-chips-wrap";
    for (const skill of skills) {
      const chip = document.createElement("span");
      chip.className = "arc-skill-chip";
      chip.innerHTML = `${escHtml(skill)}<button class="arc-chip-x" title="Remove">×</button>`;
      chip.querySelector(".arc-chip-x").onclick = (e) => {
        e.stopPropagation();
        const arr = this._getSkillArray(role);
        const idx = arr.indexOf(skill);
        if (idx > -1) arr.splice(idx, 1);
        chip.remove();
      };
      wrap.appendChild(chip);
    }
    container.appendChild(wrap);

    // Add button (block-level, always on its own line)
    const addBtn = document.createElement("button");
    addBtn.className = "arc-skill-add";
    addBtn.textContent = "+ add";
    addBtn.onclick = () => this._showSkillPicker(container, role);
    container.appendChild(addBtn);
  },

  _showSkillPicker(container, role) {
    // Remove existing picker
    const existing = container.querySelector(".arc-skill-picker");
    if (existing) { existing.remove(); return; }

    const current = this._getSkillArray(role);
    const available = this._allSkills.filter((s) => !current.includes(s)).sort();
    if (!available.length) return;

    const picker = document.createElement("div");
    picker.className = "arc-skill-picker";
    picker.innerHTML = available.map((s) => `<button class="arc-pick-item" data-skill="${escAttr(s)}">${escHtml(s)}</button>`).join("");
    picker.querySelectorAll(".arc-pick-item").forEach((btn) => {
      btn.onclick = () => {
        const skill = btn.dataset.skill;
        this._getSkillArray(role).push(skill);
        if (!this._allSkills.includes(skill)) this._allSkills.push(skill);
        this._renderSkillChips(container, this._getSkillArray(role), role);
      };
    });
    container.appendChild(picker);  // appends after addBtn, full-width block
  },

  async save() {
    const status = document.getElementById("agentConfigStatus");

    // Collect role overrides from DOM
    const roles = {};
    document.querySelectorAll(".agent-role-card[data-role]").forEach((card) => {
      const role = card.dataset.role;
      roles[role] = {
        system_prompt: card.querySelector(".arc-prompt").value,
        allowed_skills: this._roles[role]?.allowed_skills || [],
        default_skills: this._roles[role]?.default_skills || [],
        default_model: card.querySelector(".arc-model").value.trim(),
        default_timeout: parseInt(card.querySelector(".arc-timeout").value) || 90,
        max_parallel: parseInt(card.querySelector(".arc-parallel").value) || 1,
      };
    });

    const config = {
      enabled: document.getElementById("agentEnabled").checked,
      concurrency: {
        global_max: parseInt(document.getElementById("agentGlobalMax").value) || 5,
        deepseek_max: parseInt(document.getElementById("agentDeepseekMax").value) || 5,
        qwen_max: parseInt(document.getElementById("agentQwenMax").value) || 1,
      },
      resilience: {
        circuit_breaker_threshold: parseInt(document.getElementById("agentCbThreshold").value) || 5,
        circuit_breaker_reset_seconds: parseInt(document.getElementById("agentCbReset").value) || 60,
      },
      limits: {
        max_iterations: parseInt(document.getElementById("agentMaxIter").value) || 25,
        max_consecutive_tool_calls: parseInt(document.getElementById("agentMaxConsec").value) || 15,
        max_total_tool_calls: parseInt(document.getElementById("agentMaxTotal").value) || 50,
      },
      roles,
    };

    try {
      const res = await fetch("/api/agents/config", {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${localStorage.getItem("sable_token") || ""}`,
        },
        body: JSON.stringify(config),
      });
      const data = await res.json();
      if (res.ok) {
        status.innerHTML = `${lucideIcon("✓")} Saved & applied`;
        activateLucideIcons(status);
        status.style.color = "#50c878";
        // Update model badges
        document.querySelectorAll(".agent-role-card").forEach((card) => {
          const badge = card.querySelector(".arc-model-badge");
          const model = card.querySelector(".arc-model").value.trim();
          if (badge) badge.textContent = model;
        });
      } else {
        status.innerHTML = `${lucideIcon("✗")} ` + escHtml(data.detail || "Save failed");
        activateLucideIcons(status);
        status.style.color = "#ff5050";
      }
    } catch (err) {
      status.textContent = "✗ " + err.message;
      status.style.color = "#ff5050";
    }
    setTimeout(() => { status.textContent = ""; }, 3000);
  },
};

// Wire up save button + lazy-load on first tab visit
document.addEventListener("DOMContentLoaded", () => {
  const saveBtn = document.getElementById("agentConfigSave");
  if (saveBtn) saveBtn.addEventListener("click", () => AgentSettings.save());

  // Load config when Agents tab is clicked
  document.querySelectorAll(".settings-tab").forEach((tab) => {
    if (tab.dataset.tab === "agents") {
      tab.addEventListener("click", () => {
        if (!AgentSettings.loaded) AgentSettings.load();
      });
    }
  });
});
