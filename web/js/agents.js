
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
    card.onclick = () => AgentPanel.open(agentId, role, task);
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
    card.onclick = () => AgentPanel.open(agentId, role);
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
    card.onclick = () => AgentPanel.open(agentId, role);
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
  card.querySelector(".arc-expand").onclick = () => AgentPanel.open(ev.agent_id, data.role);
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
    el.onclick = () => AgentPanel.open(el.dataset.agentId);
  });

  const pane = chatEl.querySelector(".tab-pane.active") || chatEl;
  pane.appendChild(card);
  activateLucideIcons(card);
  if (typeof scrollBottom === "function") scrollBottom(true);
  return card;
}

// --------------------------------------------------------------------------
// Agent Panel (slide-in chat view)
// --------------------------------------------------------------------------
const AgentPanel = {
  el: null,
  bodyEl: null,
  currentAgentId: null,
  abortController: null,

  init() {
    if (this.el) return;
    this.el = document.createElement("div");
    this.el.className = "agent-panel hidden";
    this.el.innerHTML =
      `<div class="agent-panel-header">` +
        `<span class="agent-panel-title">Agent</span>` +
        `<span class="agent-panel-status"></span>` +
        `<button class="agent-panel-close">${lucideIcon("✕")}</button>` +
      `</div>` +
      `<div class="agent-panel-body"></div>` +
      `<div class="agent-panel-footer">` +
        `<span class="ap-timer">0:00</span>` +
        `<span class="ap-iteration">loop 0</span>` +
        `<button class="agent-panel-stop hidden">■ stop</button>` +
      `</div>`;
    document.body.appendChild(this.el);
    this.bodyEl = this.el.querySelector(".agent-panel-body");
    this.timerEl = this.el.querySelector(".ap-timer");
    this.iterEl = this.el.querySelector(".ap-iteration");
    this.el.querySelector(".agent-panel-close").onclick = () => this.close();
    this.el.querySelector(".agent-panel-stop").onclick = () => this.stopAgent();
    activateLucideIcons(this.el);
  },

  async open(agentId, role, task) {
    this.init();
    this.close(); // dismiss any existing stream
    this.currentAgentId = agentId;

    // Header
    this.el.querySelector(".agent-panel-title").textContent = `${role || "agent"}`;
    this.el.querySelector(".agent-panel-status").textContent = task ? task.slice(0, 50) : agentId;
    this.el.classList.remove("hidden");
    document.body.classList.add("agent-panel-open");
    this.bodyEl.innerHTML = "";
    if (this.iterEl) this.iterEl.textContent = "loop 0";

    // First: load existing messages + status + timing from DB
    let agentFinished = false;
    let createdAt = null;
    let completedAt = null;
    try {
      const res = await fetch(`/api/agents/${agentId}/messages`);
      const data = await res.json();
      if (data.messages && data.messages.length) {
        this._renderHistory(data.messages);
      }
      createdAt = data.created_at || null;
      completedAt = data.completed_at || null;
      if (data.status === "done" || data.status === "failed") {
        agentFinished = true;
        this._setDone(data.status === "done" ? "completed" : "failed");
      }
    } catch { /* fresh agent, no history yet */ }

    // Start timer based on actual agent start time
    this._startTimer(createdAt, completedAt);

    // Show stop button only if agent is still running
    const stopBtn = this.el.querySelector(".agent-panel-stop");
    if (agentFinished) {
      stopBtn.classList.add("hidden");
    } else {
      stopBtn.classList.remove("hidden");
    }

    // Then: subscribe to live stream
    this._connectStream(agentId);
  },

  _renderHistory(messages) {
    for (const msg of messages) {
      const role = msg.role || "user";
      if (role === "system") continue; // skip system prompt in panel view
      const div = document.createElement("div");
      div.className = `ap-msg ap-${role}`;
      let html;
      if (typeof marked !== "undefined" && (role === "assistant" || role === "tool")) {
        const raw = marked.parse(msg.content || "");
        html = typeof DOMPurify !== "undefined" ? DOMPurify.sanitize(raw) : raw;
      } else {
        html = `<p>${escHtml(msg.content || "")}</p>`;
      }
      div.innerHTML = `<div class="ap-msg-role">${escHtml(role)}</div><div class="ap-msg-content">${html}</div>`;
      this.bodyEl.appendChild(div);
    }
    this._scrollBottom();
  },

  _connectStream(agentId) {
    this.abortController = new AbortController();
    const signal = this.abortController.signal;

    fetch(`/api/agents/${agentId}/stream`, { signal })
      .then((res) => {
        if (!res.ok || !res.body) throw new Error("Stream failed");
        return this._consumeStream(res, agentId);
      })
      .catch((err) => {
        if (err.name !== "AbortError") {
          console.debug("[agents] panel stream error:", err.message);
        }
      });
  },

  async _consumeStream(res, agentId) {
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let currentAnswerEl = null;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const parts = buffer.split("\n\n");
      buffer = parts.pop();

      for (const part of parts) {
        const line = part.trim();
        if (!line.startsWith("data: ")) continue;
        let evt;
        try { evt = JSON.parse(line.slice(6)); } catch { continue; }

        if (evt.type === "chunk") {
          // Live token streaming — append to current answer element
          if (!currentAnswerEl) {
            currentAnswerEl = document.createElement("div");
            currentAnswerEl.className = "ap-msg ap-assistant ap-streaming";
            currentAnswerEl.innerHTML = `<div class="ap-msg-role">assistant</div><div class="ap-msg-content"><span class="ap-raw"></span></div>`;
            this.bodyEl.appendChild(currentAnswerEl);
          }
          const rawSpan = currentAnswerEl.querySelector(".ap-raw");
          if (rawSpan) rawSpan.textContent += evt.text;
          this._scrollBottom();
        } else if (evt.type === "answer") {
          // Final answer — replace raw streaming with rendered markdown
          if (currentAnswerEl) {
            currentAnswerEl.classList.remove("ap-streaming");
            const raw = typeof marked !== "undefined" ? marked.parse(evt.text || "") : escHtml(evt.text || "");
            const html = typeof DOMPurify !== "undefined" ? DOMPurify.sanitize(raw) : raw;
            currentAnswerEl.querySelector(".ap-msg-content").innerHTML = html;
          } else {
            // No chunks received (e.g. history replay) — create fresh
            currentAnswerEl = document.createElement("div");
            currentAnswerEl.className = "ap-msg ap-assistant";
            const raw = typeof marked !== "undefined" ? marked.parse(evt.text || "") : escHtml(evt.text || "");
            const html = typeof DOMPurify !== "undefined" ? DOMPurify.sanitize(raw) : raw;
            currentAnswerEl.innerHTML = `<div class="ap-msg-role">assistant</div><div class="ap-msg-content">${html}</div>`;
            this.bodyEl.appendChild(currentAnswerEl);
          }
          currentAnswerEl = null;
          this._scrollBottom();
        } else if (evt.type === "skill_start") {
          const card = document.createElement("div");
          card.className = "ap-skill-card";
          card.dataset.skill = evt.name || "";
          card.innerHTML = `<span class="ap-skill-icon">⚙</span><span class="ap-skill-name">${escHtml(evt.name || "tool")}</span><span class="ap-skill-status">running…</span>`;
          this.bodyEl.appendChild(card);
          this._scrollBottom();
        } else if (evt.type === "skill_output") {
          // Append output to last skill card
          const cards = this.bodyEl.querySelectorAll(".ap-skill-card");
          const last = cards[cards.length - 1];
          if (last) {
            const out = document.createElement("pre");
            out.className = "ap-skill-output";
            out.textContent = (evt.text || "").slice(0, 2000);
            last.appendChild(out);
            this._scrollBottom();
          }
        } else if (evt.type === "skill_end") {
          const cards = this.bodyEl.querySelectorAll(".ap-skill-card");
          const last = cards[cards.length - 1];
          if (last) {
            const status = last.querySelector(".ap-skill-status");
            if (status) {
              status.textContent = evt.ok ? "✓ done" : "✗ error";
              status.className = `ap-skill-status ${evt.ok ? "ok" : "fail"}`;
            }
          }
        } else if (evt.type === "iteration") {
          if (this.iterEl) this.iterEl.textContent = `loop ${evt.iteration}`;
        } else if (evt.type === "done") {
          this._setDone("completed");
          break;
        } else if (evt.type === "error") {
          const errDiv = document.createElement("div");
          errDiv.className = "ap-msg ap-error";
          errDiv.textContent = evt.message || "Agent failed";
          this.bodyEl.appendChild(errDiv);
          this._setDone("failed");
          break;
        }
      }
    }
  },

  _setDone(status) {
    const stopBtn = this.el.querySelector(".agent-panel-stop");
    if (stopBtn) stopBtn.classList.add("hidden");
    this._stopTimer();
    const statusEl = this.el.querySelector(".agent-panel-status");
    if (statusEl) statusEl.textContent = status === "completed" ? "✓ completed" : "✗ failed";
    this.el.classList.add(status === "completed" ? "panel-done" : "panel-failed");
  },

  async stopAgent() {
    if (!this.currentAgentId) return;
    const btn = this.el.querySelector(".agent-panel-stop");
    if (btn) { btn.disabled = true; btn.textContent = "…"; }
    try {
      await fetch(`/api/agents/${this.currentAgentId}/kill`, { method: "POST" });
      if (btn) { btn.textContent = "stopped"; }
    } catch {
      if (btn) { btn.disabled = false; btn.textContent = "■ stop"; }
    }
  },

  _scrollBottom() {
    if (this.bodyEl) this.bodyEl.scrollTop = this.bodyEl.scrollHeight;
  },

  close() {
    if (this.abortController) {
      this.abortController.abort();
      this.abortController = null;
    }
    this._stopTimer();
    if (this.el) {
      this.el.classList.add("hidden");
      this.el.classList.remove("panel-done", "panel-failed");
    }
    document.body.classList.remove("agent-panel-open");
    this.currentAgentId = null;
  },

  _startTimer(createdAt, completedAt) {
    // Use agent's actual start time (seconds epoch) or fall back to now
    this._timerStart = createdAt ? createdAt * 1000 : Date.now();
    const endTime = completedAt ? completedAt * 1000 : null;

    const update = () => {
      const now = endTime || Date.now();
      const s = Math.max(0, Math.floor((now - this._timerStart) / 1000));
      const m = Math.floor(s / 60);
      const sec = s % 60;
      if (this.timerEl) this.timerEl.textContent = `${m}:${String(sec).padStart(2, "0")}`;
    };

    update(); // show correct time immediately
    if (!endTime) {
      this._timerInterval = setInterval(update, 1000);
    }
  },

  _stopTimer() {
    if (this._timerInterval) { clearInterval(this._timerInterval); this._timerInterval = null; }
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
