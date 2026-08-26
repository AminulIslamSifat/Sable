
/* ===== Sable Multi-Agent UI =====
 * Top bar cards, inline JSON result cards, history modal, EventSource.
 * Loaded after core modules — attaches to global scope.
 */

// --------------------------------------------------------------------------
// Helpers (sse.js IIFE doesn't expose these globally)
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

  _slotEl: null,

  init() {
    if (this.container) return;
    this.container = document.createElement("div");
    this.container.id = "agent-top-bar";
    this.container.className = "agent-top-bar";
    // Insert into the dedicated standalone slot (outside <header>)
    this._slotEl = document.getElementById("agentTopBarSlot");
    if (this._slotEl) this._slotEl.appendChild(this.container);
    else document.body.appendChild(this.container);
    // Vertical wheel → horizontal scroll
    this.container.addEventListener("wheel", (e) => {
      if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) {
        e.preventDefault();
        this.container.scrollLeft += e.deltaY;
      }
    }, { passive: false });
  },

  _show() {
    if (!this.container) return;
    this.container.classList.remove("hidden");
    if (this._slotEl) this._slotEl.classList.remove("hidden");
  },

  _hide() {
    if (!this.container) return;
    this.container.classList.add("hidden");
    if (this._slotEl) this._slotEl.classList.add("hidden");
  },

  addCard(agentId, role, task, model) {
    this.init();
    this._show();
    const card = document.createElement("div");
    card.className = "agent-card running";
    card.dataset.agentId = agentId;
    card.innerHTML =
      `<span class="agent-spinner"></span>` +
      `<span class="agent-role">${escHtml(role)}</span>` +
      (model ? `<span class="agent-model-badge">${escHtml(model)}</span>` : "") +
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
      if (this.cards.size === 0) AgentTopBar._hide();
    }, 400);
  },

  clear() {
    this.cards.forEach((card) => card.remove());
    this.cards.clear();
    this._hide();
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

  const modelLabel = data.model ? escHtml(data.model) : "";
  const bdRaw = data.browser_data_dir || "";
  const bdLabel = bdRaw ? bdRaw.split("/").pop() : "";
  const metaParts = [duration];
  if (modelLabel) metaParts.push(modelLabel);
  if (bdLabel) metaParts.push(bdLabel);
  metaParts.push(`${tokens} words`);

  card.innerHTML =
    `<span class="arc-title">${escHtml(role)} ${isSuccess ? "completed" : "failed"}</span>` +
    `<span class="arc-detail">${isSuccess ? escHtml((data.summary || "").slice(0, 120)) : escHtml(data.error || "Unknown error")}</span>` +
    `<span class="arc-meta">${metaParts.join(" · ")}</span>` +
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
function stripToolJson(text) {
  // Backend parser already strips <tool_call> tags from the answer stream.
  // This function now only collapses excessive blank lines.
  return (text || "").replace(/\n{3,}/g, "\n\n").trim();
}

const AgentPanel = {
  el: null,
  bodyEl: null,
  currentAgentId: null,
  abortController: null,
  _userScrolled: false,
  _scrollRafPending: false,
  _isRunning: false,

  init() {
    if (this.el) return;
    this.el = document.createElement("div");
    this.el.className = "agent-panel hidden";
    this.el.innerHTML =
      `<div class="agent-panel-header">` +
        `<span class="agent-panel-title">Agent</span>` +
        `<span class="agent-panel-model"></span>` +
        `<span class="agent-panel-status"></span>` +
        `<button class="agent-panel-close">${lucideIcon("✕")}</button>` +
      `</div>` +
      `<div class="agent-panel-body"></div>` +
      `<div class="agent-panel-footer">` +
        `<span class="ap-timer">0:00</span>` +
        `<span class="ap-iteration">loop 0</span>` +
        `<div class="ap-input-wrap">` +
          `<input class="ap-input" type="text" placeholder="Guide the agent…" />` +
          `<button class="ap-send-btn">${lucideIcon("➤")}</button>` +
        `</div>` +
        `<button class="agent-panel-stop hidden">■ stop</button>` +
      `</div>`;
    document.body.appendChild(this.el);
    this.bodyEl = this.el.querySelector(".agent-panel-body");
    this.timerEl = this.el.querySelector(".ap-timer");
    this.iterEl = this.el.querySelector(".ap-iteration");
    this.inputEl = this.el.querySelector(".ap-input");
    this.sendBtnEl = this.el.querySelector(".ap-send-btn");
    this.el.querySelector(".agent-panel-close").onclick = () => this.close();
    this.el.querySelector(".agent-panel-stop").onclick = () => this.stopAgent();
    this.sendBtnEl.onclick = () => this._sendGuide();
    this.inputEl.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); this._sendGuide(); }
    });
    // Smart scroll tracking
    this.bodyEl.addEventListener("scroll", () => {
      const gap = this.bodyEl.scrollHeight - this.bodyEl.scrollTop - this.bodyEl.clientHeight;
      this._userScrolled = gap > 60;
    }, { passive: true });
    activateLucideIcons(this.el);
  },

  async _sendGuide() {
    const text = this.inputEl.value.trim();
    if (!text || !this.currentAgentId || !this._isRunning) return;
    this.inputEl.value = "";
    // Show user message immediately in panel
    this._appendUserMsg(text);
    this._scrollBottom(true);
    try {
      await fetch(`/api/agents/${this.currentAgentId}/message`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text }),
      });
    } catch { /* silent fail */ }
  },

  _appendUserMsg(text) {
    const div = document.createElement("div");
    div.className = "msg user ap-user-msg";
    const textEl = document.createElement("div");
    textEl.className = "user-text";
    textEl.textContent = text;
    div.appendChild(textEl);
    this.bodyEl.appendChild(div);
  },

  _scrollBottom(force) {
    if (!this.bodyEl) return;
    if (!force && this._userScrolled) return;
    if (this._scrollRafPending) return;
    this._scrollRafPending = true;
    requestAnimationFrame(() => {
      this._scrollRafPending = false;
      if (!this.bodyEl) return;
      if (!force) {
        const gap = this.bodyEl.scrollHeight - this.bodyEl.scrollTop - this.bodyEl.clientHeight;
        if (gap > 80) { this._userScrolled = true; return; }
      }
      this.bodyEl.scrollTop = this.bodyEl.scrollHeight;
    });
  },

  async open(agentId, role, task) {
    this.init();
    this.close(); // dismiss any existing stream
    this.currentAgentId = agentId;
    this._userScrolled = false;
    this._scrollRafPending = false;
    this._isRunning = true;

    // Header
    this.el.querySelector(".agent-panel-title").textContent = `${role || "agent"}`;
    this.el.querySelector(".agent-panel-model").textContent = "";
    this.el.querySelector(".agent-panel-status").textContent = task ? task.slice(0, 50) : agentId;
    // Close diff viewer if open (mutual exclusion)
    document.body.classList.remove("diff-open");
    this.el.classList.remove("hidden");
    document.body.classList.add("agent-panel-open");
    this.bodyEl.innerHTML = "";
    this._clearTodos();
    if (this.iterEl) this.iterEl.textContent = "loop 0";
    // Enable/disable input based on running state
    this.inputEl.disabled = false;
    this.sendBtnEl.disabled = false;

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
      if (data.status === "completed" || data.status === "degraded" || data.status === "failed") {
        agentFinished = true;
        this._setDone(data.status === "failed" ? "failed" : "completed");
      }
      // Populate model from API
      if (data.model) {
        this.el.querySelector(".agent-panel-model").textContent = data.model;
      }
      // Render initial todos from API response (must be inside try — data is block-scoped)
      if (data.todos && data.todos.length) {
        this._renderTodos(data.todos);
      }
    } catch { /* fresh agent, no history yet */ }

    // Start timer based on actual agent start time
    this._startTimer(createdAt, completedAt);

    // Show stop button only if agent is still running
    const stopBtn = this.el.querySelector(".agent-panel-stop");
    if (agentFinished) {
      stopBtn.classList.add("hidden");
      this._isRunning = false;
      this.inputEl.disabled = true;
      this.sendBtnEl.disabled = true;
    } else {
      stopBtn.classList.remove("hidden");
      stopBtn.disabled = false;
      stopBtn.textContent = "■ stop";
    }


    // Then: subscribe to live stream
    this._connectStream(agentId);
  },

  _renderHistory(messages) {
    for (const msg of messages) {
      const role = msg.role || "user";
      if (role === "system") continue; // skip system prompt in panel view
      if (role === "user") {
        this._appendUserMsg(msg.content || "");
      } else if (role === "assistant" || role === "tool") {
        const clean = stripToolJson(msg.content || "");
        if (!clean) continue; // pure tool-call message — nothing to show
        const div = document.createElement("div");
        div.className = "msg bot";
        const content = document.createElement("div");
        content.className = "md-content";
        const raw = typeof marked !== "undefined" ? marked.parse(clean) : escHtml(clean);
        content.innerHTML = typeof DOMPurify !== "undefined" ? DOMPurify.sanitize(raw, { FORBID_TAGS: ["action", "grep", "glob", "list_dir", "execute_command", "get_file", "view_file", "edit_file", "create_file", "insert_file", "spawn_agent", "ask_user", "mcp_call", "chat_title"] }) : raw;
        div.appendChild(content);
        this.bodyEl.appendChild(div);
      }
    }
    this._scrollBottom(true);
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
    let currentSkillCard = null;

    const FORBID_TAGS = ["action", "grep", "glob", "list_dir", "execute_command", "get_file", "view_file", "edit_file", "create_file", "insert_file", "spawn_agent", "ask_user", "mcp_call", "chat_title"];
    const renderMd = (text) => {
      const raw = typeof marked !== "undefined" ? marked.parse(text || "") : escHtml(text || "");
      return typeof DOMPurify !== "undefined" ? DOMPurify.sanitize(raw, { FORBID_TAGS }) : raw;
    };

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
            currentAnswerEl.className = "msg bot streaming ap-streaming";
            currentAnswerEl.innerHTML = `<div class="md-content"><span class="ap-raw"></span></div>`;
            this.bodyEl.appendChild(currentAnswerEl);
          }
          const rawSpan = currentAnswerEl.querySelector(".ap-raw");
          if (rawSpan) rawSpan.textContent += evt.text;
          this._scrollBottom();
        } else if (evt.type === "answer") {
          // Final answer — replace raw streaming with rendered markdown
          const cleanText = stripToolJson(evt.text);
          if (!cleanText && currentAnswerEl) {
            currentAnswerEl.remove(); // pure tool-call turn — drop the raw blob
          } else if (cleanText && currentAnswerEl) {
            currentAnswerEl.classList.remove("streaming", "ap-streaming");
            currentAnswerEl.querySelector(".md-content").innerHTML = renderMd(cleanText);
          } else if (cleanText) {
            // No chunks received (e.g. history replay) — create fresh
            const div = document.createElement("div");
            div.className = "msg bot";
            div.innerHTML = `<div class="md-content">${renderMd(cleanText)}</div>`;
            this.bodyEl.appendChild(div);
          }
          currentAnswerEl = null;
          this._scrollBottom();
        } else if (evt.type === "user_message") {
          // User guidance message (from the input)
          this._appendUserMsg(evt.text || "");
          this._scrollBottom(true);
        } else if (evt.type === "skill_start") {
          currentSkillCard = document.createElement("details");
          currentSkillCard.className = "ap-skill-details";
          currentSkillCard.dataset.skill = evt.name || "";
          
          // Extract command/input preview from attrs
          let argsPreview = "";
          if (evt.attrs) {
            const previewFields = ["command", "path", "pattern", "query", "url", "task"];
            for (const field of previewFields) {
              if (evt.attrs[field]) {
                argsPreview = String(evt.attrs[field]).slice(0, 80);
                if (argsPreview.length === 80) argsPreview += "…";
                break;
              }
            }
          }
          
          currentSkillCard.innerHTML =
            `<summary class="ap-skill-summary">` +
              `<span class="ap-skill-icon">⚙</span>` +
              `<span class="ap-skill-name">${escHtml(evt.name || "tool")}</span>` +
              (argsPreview ? `<span class="ap-skill-args">${escHtml(argsPreview)}</span>` : "") +
              `<span class="ap-skill-status">running…</span>` +
            `</summary>` +
            `<div class="ap-skill-body"></div>`;
          this.bodyEl.appendChild(currentSkillCard);
          this._scrollBottom();
        } else if (evt.type === "skill_output") {
          const body = currentSkillCard ? currentSkillCard.querySelector(".ap-skill-body") : null;
          if (body) {
            // Merge into existing <pre> block instead of creating new ones
            let out = body.querySelector(".ap-skill-output");
            if (!out) {
              out = document.createElement("pre");
              out.className = "ap-skill-output";
              body.appendChild(out);
            }
            const newText = (evt.text || "").slice(0, 3000);
            out.textContent += newText;
            this._scrollBottom();
          }
        } else if (evt.type === "skill_end") {
          if (currentSkillCard) {
            const status = currentSkillCard.querySelector(".ap-skill-status");
            if (status) {
              const secs = evt.duration_ms != null ? ` ${(evt.duration_ms / 1000).toFixed(1)}s` : "";
              status.textContent = (evt.ok ? "✓" : "✗") + secs;
              status.className = `ap-skill-status ${evt.ok ? "ok" : "fail"}`;
            }
            // Surface result/error inside the card body (visible on expand)
            const body = currentSkillCard.querySelector(".ap-skill-body");
            if (body) {
              if (evt.error) {
                const errPre = document.createElement("pre");
                errPre.className = "ap-skill-output";
                errPre.textContent = String(evt.error).slice(0, 3000);
                body.appendChild(errPre);
              } else if (!body.children.length && evt.result && Object.keys(evt.result).length) {
                const resPre = document.createElement("pre");
                resPre.className = "ap-skill-output";
                resPre.textContent = JSON.stringify(evt.result, null, 2).slice(0, 3000);
                body.appendChild(resPre);
              }
            }
            currentSkillCard.removeAttribute("open");
            currentSkillCard = null;
          }
        } else if (evt.type === "todo_progress") {
          if (evt.todos) this._renderTodos(evt.todos);
        } else if (evt.type === "model_fallback") {
          const modelEl = this.el.querySelector(".agent-panel-model");
          if (modelEl) modelEl.textContent = evt.to || "";
          const notice = document.createElement("div");
          notice.className = "ap-system-note";
          notice.innerHTML = `⚡ Model fallback: ${escHtml(evt.from || "?")} → ${escHtml(evt.to || "?")}`;
          this.bodyEl.appendChild(notice);
          this._scrollBottom();
        } else if (evt.type === "browser_fallback") {
          const notice = document.createElement("div");
          notice.className = "ap-system-note";
          notice.innerHTML = `🔄 Browser fallback: ${escHtml(evt.from || "default")} → ${escHtml(evt.to || "?")}`;
          this.bodyEl.appendChild(notice);
          this._scrollBottom();
        } else if (evt.type === "iteration") {
          if (this.iterEl) this.iterEl.textContent = `loop ${evt.iteration}`;
        } else if (evt.type === "done") {
          this._setDone("completed");
          break;
        } else if (evt.type === "error") {
          const errDiv = document.createElement("div");
          errDiv.className = "ap-system-note ap-error-note";
          errDiv.textContent = "✗ " + (evt.message || "Agent failed");
          this.bodyEl.appendChild(errDiv);
          this._setDone("failed");
          break;
        }
      }
    }
  },

  _setDone(status) {
    const stopBtn = this.el.querySelector(".agent-panel-stop");
    if (stopBtn) { stopBtn.classList.add("hidden"); stopBtn.disabled = false; stopBtn.textContent = "■ stop"; }
    this._stopTimer();
    this._isRunning = false;
    if (this.inputEl) this.inputEl.disabled = true;
    if (this.sendBtnEl) this.sendBtnEl.disabled = true;
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

  _renderTodos(todos) {
    // Create or update todo widget in panel header area
    let widget = this.el.querySelector(".ap-todo-widget");
    if (!widget) {
      widget = document.createElement("div");
      widget.className = "ap-todo-widget";
      // Insert after header, before body
      const header = this.el.querySelector(".agent-panel-header");
      if (header && header.nextSibling) {
        this.el.insertBefore(widget, header.nextSibling);
      } else {
        this.el.appendChild(widget);
      }
    }
    const icons = { completed: "✅", in_progress: "🔧", pending: "⏳", skipped: "⏭️" };
    let html = `<div class="ap-todo-header">📋 Plan</div>`;
    for (const t of todos) {
      const icon = icons[t.status] || "⏳";
      const cls = `ap-todo-item ap-todo-${t.status}`;
      html += `<div class="${cls}"><span class="ap-todo-icon">${icon}</span><span class="ap-todo-text">${escHtml(t.content)}</span></div>`;
      if (t.subtasks && t.subtasks.length) {
        for (const sub of t.subtasks) {
          html += `<div class="ap-todo-sub">• ${escHtml(sub)}</div>`;
        }
      }
    }
    widget.innerHTML = html;
  },

  _clearTodos() {
    const widget = this.el.querySelector(".ap-todo-widget");
    if (widget) widget.remove();
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
  const url = `/api/chat/${chatId}/agent-events?token=${encodeURIComponent(token)}`;
  console.log("[AgentDebug] connectAgentEvents called, chatId:", chatId, "url:", url);
  // EventSource doesn't support custom headers — use query param for auth
  _agentEventSource = new EventSource(url);

  _agentEventSource.onopen = () => {
    console.log("[AgentDebug] SSE connection OPENED for chat:", chatId);
  };

  _agentEventSource.onmessage = (e) => {
    console.log("[AgentDebug] SSE message received:", e.data?.slice(0, 200));
    try {
      const ev = JSON.parse(e.data);
      handleAgentEvent(ev);
    } catch (err) { console.error("[AgentDebug] SSE parse error:", err); }
  };

  _agentEventSource.onerror = (err) => {
    console.error("[AgentDebug] SSE connection error:", err, "readyState:", _agentEventSource?.readyState);
  };
}

function disconnectAgentEvents() {
  if (_agentEventSource) {
    _agentEventSource.close();
    _agentEventSource = null;
    _agentEventChatId = null;
  }
}

// Track todos from agent_spawned events for spawn-card injection
const _agentTodosRaw = new Map(); // agent_id -> raw pipe-separated string

function handleAgentEvent(ev) {
  console.log("[AgentDebug] handleAgentEvent type:", ev.type, "agent_id:", ev.agent_id);
  switch (ev.type) {
    case "agent_spawned":
      console.log("[AgentDebug] agent_spawned → calling addCard", ev.agent_id, ev.data);
      AgentTopBar.addCard(ev.agent_id, ev.data?.role || "agent", ev.data?.task || "", ev.data?.model || "");
      // Capture todos for spawn-card injection
      if (ev.data?.todos && ev.data.todos.length) {
        _agentTodosRaw.set(ev.agent_id, ev.data.todos.map(t => t.content).join(" | "));
      }
      // If panel is open for this agent, render initial todos
      if (AgentPanel.currentAgentId === ev.agent_id && ev.data?.todos) {
        AgentPanel._renderTodos(ev.data.todos);
      }
      break;
    case "agent_progress":
      AgentTopBar.updateCard(ev.agent_id, ev.data?.partial || "");
      break;
    case "auto_turn_trigger":
      // Agent completed — fire a normal chat turn via the standard /api/chat pipeline.
      // sendAutoTurnMessage (sse.js) handles the user bubble, bot streaming, skill
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
// Integration hooks (called from sse.js)
// --------------------------------------------------------------------------

// Called when a chat is selected/opened
function onChatOpened(chatId) {
  console.log("[AgentDebug] onChatOpened called, chatId:", chatId);
  AgentTopBar.clear();
  connectAgentEvents(chatId);
  // Load any active agents for this chat
  fetch(`/api/agents/active?chat_id=${encodeURIComponent(chatId)}`)
    .then((r) => r.json())
    .then((agents) => {
      console.log("[AgentDebug] /api/agents/active response:", agents);
      if (!Array.isArray(agents)) return;
      for (const a of agents) {
        if (a.status === "running" || a.status === "spawned") {
          console.log("[AgentDebug] restoring active agent card:", a.id, a.role);
          AgentTopBar.addCard(a.id, a.role, a.task, a.model);
        }
      }
    })
    .catch((err) => { console.error("[AgentDebug] /api/agents/active failed:", err); });
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
  { id: "maria", icon: "💋", label: "Maria", desc: "Full Maria — all tools & skills" },
  { id: "sysutil", icon: "🔧", label: "Utility", desc: "System repair, ADB, downloads" },
  { id: "docs", icon: "📄", label: "Docs", desc: "PDF, DOCX, XLSX, humanize" },
  { id: "visuals", icon: "🎨", label: "Visuals", desc: "Plots, diagrams, UI, simulations" },
  { id: "tester", icon: "🐛", label: "Tester", desc: "Debug, test, fix errors" },
  { id: "analyst", icon: "🔍", label: "Analyst", desc: "Research + code review" },
  { id: "coder", icon: "💻", label: "Coder", desc: "Write/edit code" },
  { id: "writer", icon: "✍️", label: "Writer", desc: "Docs & content" },
  { id: "scheduled", icon: "⏰", label: "Scheduled", desc: "Autonomous scheduled tasks" },
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

  // Use capture phase so Enter/Tab are intercepted BEFORE the main send handler
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
        e.stopPropagation();
        _selectMention(_mentionFiltered[_mentionIdx].id);
      }
    } else if (e.key === "Escape") {
      hideMentionPopup();
    }
  }, true);
});

// --------------------------------------------------------------------------
// Settings Panel (Agents tab)
// --------------------------------------------------------------------------
const AgentSettings = {
  loaded: false,
  _roles: {},             // current role data from API
  _allTools: [],          // all available tool groups [{key, name, functions}] (from API)
  _allSkills: [],         // all available skill keys (from API)
  _skillMeta: {},         // skill key → {name, trigger} metadata
  _availableModels: [],   // all models from /api/models (for dropdown)

  _markDirty() {
    if (window._universalSave?.markDirty) {
      window._universalSave.markDirty("agents");
    }
  },

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
      this._availableModels = cfg.available_models || [];

      // Teacher config
      const teacher = cfg.teacher || {};
      document.getElementById("teacherEnabled").checked = teacher.enabled !== false;
      const teacherModelSel = document.getElementById("teacherModel");
      if (teacher.model) {
        // Ensure the model option exists
        let found = false;
        for (const opt of teacherModelSel.options) {
          if (opt.value === teacher.model) { found = true; break; }
        }
        if (!found) {
          const opt = document.createElement("option");
          opt.value = teacher.model;
          opt.textContent = teacher.model;
          teacherModelSel.appendChild(opt);
        }
        teacherModelSel.value = teacher.model;
      }
      document.getElementById("teacherBrowserData").value = teacher.browser_data_dir || "";

      // Fetch available tools and skills from API
      try {
        const [toolsRes, skillsRes] = await Promise.all([
          fetch("/api/agents/available-tools"),
          fetch("/api/agents/available-skills"),
        ]);
        const toolsData = toolsRes.ok ? await toolsRes.json() : { tools: [] };
        const skillsData = skillsRes.ok ? await skillsRes.json() : { skills: [] };
        this._allTools = (toolsData.tools || []).sort((a, b) => a.name.localeCompare(b.name));
        this._allSkills = (skillsData.skills || []).map((s) => s.key).sort();
        this._skillMeta = {};
        for (const s of (skillsData.skills || [])) {
          this._skillMeta[s.key] = s;
        }
      } catch {
        this._allTools = [{ key: "code_editor", name: "Code Editor", functions: 4 }];
        this._allSkills = [];
        this._skillMeta = {};
      }

      // Populate datalists for dropdowns
      await this._loadAccountDatalist();
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

    const roleIcons = { analyst: "🔍", coder: "💻", writer: "✍️", sysutil: "🔧", docs: "📄", visuals: "🎨", tester: "🐛" };
    const models = this._availableModels || [];

    for (const [role, data] of Object.entries(this._roles)) {
      const card = document.createElement("div");
      card.className = "agent-role-card";
      card.dataset.role = role;

      const icon = roleIcons[role] || "🤖";
      const modelOptions = models.map((m) =>
        `<option value="${escAttr(m.id)}" ${m.id === data.default_model ? "selected" : ""}>${escHtml(m.label)}${m.api_backend ? ` (${m.api_backend})` : ""}</option>`
      ).join("");

      // Model chain chips
      const chain = data.model_chain || [];
      const chainHtml = chain.map((m) =>
        `<span class="arc-acct-chip">${escHtml(m)}<button class="arc-chip-x" title="Remove">×</button></span>`
      ).join("");

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
            `<label>Allowed Tools <span class="arc-hint">(handler functions available via tool_call)</span></label>` +
            `<div class="arc-skills-list arc-allowed-tools"></div>` +
          `</div>` +
          `<div class="arc-field">` +
            `<label>Allowed Skills <span class="arc-hint">(read instruction.md before use)</span></label>` +
            `<div class="arc-skills-list arc-allowed-skills"></div>` +
          `</div>` +
          `<div class="arc-field arc-inline-fields">` +
            `<div style="flex:2;"><label>Model</label><select class="arc-model mem-input">${modelOptions}</select></div>` +
            `<div><label>Timeout (s)</label><input type="number" class="arc-timeout mem-input" value="${data.default_timeout}" min="10" max="600"></div>` +
            `<div><label>Max Parallel</label><input type="number" class="arc-parallel mem-input" value="${data.max_parallel}" min="1" max="10"></div>` +
          `</div>` +
          `<div class="arc-field">` +
            `<label>Model Fallback Chain <span class="arc-hint">(up to 3, tried in order on failure)</span></label>` +
            `<div class="arc-chain-pool">${chainHtml}</div>` +
            `<div class="arc-pool-add-row">` +
              `<select class="arc-chain-select mem-input"><option value="" disabled selected>Add model…</option>${modelOptions}</select>` +
              `<button class="arc-pool-add-btn" title="Add to chain">+</button>` +
            `</div>` +
          `</div>` +
        `</div>`;

      // Render tool and skill chips
      this._renderToolChips(card.querySelector(".arc-allowed-tools"), data.allowed_tools || [], role + ":tools");
      this._renderSkillChips(card.querySelector(".arc-allowed-skills"), data.allowed_skills || [], role + ":skills");

      // Helper: create removable chip
      const makeChip = (val) => {
        const chip = document.createElement("span");
        chip.className = "arc-acct-chip";
        chip.innerHTML = `${escHtml(val)}<button class="arc-chip-x" title="Remove">×</button>`;
        chip.querySelector(".arc-chip-x").onclick = (e) => { e.stopPropagation(); chip.remove(); this._markDirty(); };
        return chip;
      };

      // Helper: wire select + add button for a pool
      const wirePoolAdd = (poolEl, selectEl, addBtn, maxChips) => {
        poolEl.querySelectorAll(".arc-chip-x").forEach((btn) => {
          btn.onclick = (e) => { e.stopPropagation(); btn.parentElement.remove(); this._markDirty(); };
        });
        const doAdd = () => {
          const val = selectEl.value;
          if (!val) return;
          if (maxChips && poolEl.querySelectorAll(".arc-acct-chip").length >= maxChips) return;
          // Prevent duplicates
          const existing = [...poolEl.querySelectorAll(".arc-acct-chip")].map(c => c.textContent.replace("×", "").trim());
          if (existing.includes(val)) return;
          poolEl.appendChild(makeChip(val));
          selectEl.selectedIndex = 0;
          this._markDirty();
        };
        addBtn.onclick = doAdd;
        selectEl.addEventListener("change", doAdd);
      };

      // Get all add buttons and selects in DOM order within this card
      const addBtns = card.querySelectorAll(".arc-pool-add-btn");
      const selects = card.querySelectorAll(".arc-pool-add-row select");

      // Model chain (max 3)
      wirePoolAdd(card.querySelector(".arc-chain-pool"), selects[0], addBtns[0], 3);

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

  async _loadAccountDatalist() {
    try {
      const res = await fetch("/api/settings/accounts");
      if (!res.ok) return;
      const data = await res.json();
      this._accounts = data.accounts || [];
    } catch {
      this._accounts = [];
    }
  },

  _getToolArray(key) {
    // key format: "role:tools" e.g. "coder:tools"
    const role = key.split(":")[0];
    if (!this._roles[role]) return [];
    if (!this._roles[role].allowed_tools) this._roles[role].allowed_tools = [];
    return this._roles[role].allowed_tools;
  },

  _getSkillArray(key) {
    // key format: "role:skills" e.g. "coder:skills"
    const role = key.split(":")[0];
    if (!this._roles[role]) return [];
    if (!this._roles[role].allowed_skills) this._roles[role].allowed_skills = [];
    return this._roles[role].allowed_skills;
  },

  _toolGroupName(key) {
    const g = (this._allTools || []).find((t) => t.key === key);
    return g ? g.name : key;
  },

  _renderToolChips(container, tools, roleKey) {
    container.innerHTML = "";
    const wrap = document.createElement("div");
    wrap.className = "arc-chips-wrap";
    for (const toolKey of tools) {
      const chip = document.createElement("span");
      chip.className = "arc-skill-chip";
      chip.dataset.toolKey = toolKey;
      chip.innerHTML = `${escHtml(this._toolGroupName(toolKey))}<button class="arc-chip-x" title="Remove">×</button>`;
      chip.querySelector(".arc-chip-x").onclick = (e) => {
        e.stopPropagation();
        const arr = this._getToolArray(roleKey);
        const idx = arr.indexOf(toolKey);
        if (idx > -1) arr.splice(idx, 1);
        chip.remove();
        this._markDirty();
      };
      wrap.appendChild(chip);
    }
    container.appendChild(wrap);

    const addBtn = document.createElement("button");
    addBtn.className = "arc-skill-add";
    addBtn.textContent = "+ add tool";
    addBtn.onclick = () => this._showItemPicker(container, roleKey, "tools");
    container.appendChild(addBtn);
  },

  _renderSkillChips(container, skills, roleKey) {
    container.innerHTML = "";
    const wrap = document.createElement("div");
    wrap.className = "arc-chips-wrap";
    for (const skill of skills) {
      const chip = document.createElement("span");
      chip.className = "arc-skill-chip";
      chip.innerHTML = `${escHtml(skill)}<button class="arc-chip-x" title="Remove">×</button>`;
      chip.querySelector(".arc-chip-x").onclick = (e) => {
        e.stopPropagation();
        const arr = this._getSkillArray(roleKey);
        const idx = arr.indexOf(skill);
        if (idx > -1) arr.splice(idx, 1);
        chip.remove();
        this._markDirty();
      };
      wrap.appendChild(chip);
    }
    container.appendChild(wrap);

    const addBtn = document.createElement("button");
    addBtn.className = "arc-skill-add";
    addBtn.textContent = "+ add skill";
    addBtn.onclick = () => this._showItemPicker(container, roleKey, "skills");
    container.appendChild(addBtn);
  },

  _showItemPicker(container, roleKey, type) {
    // Remove existing picker
    const existing = container.querySelector(".arc-skill-picker");
    if (existing) { existing.remove(); return; }

    const isTools = type === "tools";
    const current = isTools ? this._getToolArray(roleKey) : this._getSkillArray(roleKey);

    let available;
    if (isTools) {
      // _allTools is [{key, name, functions}], filter by key
      available = (this._allTools || []).filter((g) => !current.includes(g.key));
    } else {
      available = (this._allSkills || []).filter((s) => !current.includes(s)).sort();
    }
    if (!available.length) return;

    const picker = document.createElement("div");
    picker.className = "arc-skill-picker";
    if (isTools) {
      picker.innerHTML = available.map((g) => `<button class="arc-pick-item" data-item="${escAttr(g.key)}">${escHtml(g.name)} <small>(${g.functions})</small></button>`).join("");
    } else {
      picker.innerHTML = available.map((s) => `<button class="arc-pick-item" data-item="${escAttr(s)}">${escHtml(s)}</button>`).join("");
    }
    picker.querySelectorAll(".arc-pick-item").forEach((btn) => {
      btn.onclick = () => {
        const item = btn.dataset.item;
        const arr = isTools ? this._getToolArray(roleKey) : this._getSkillArray(roleKey);
        arr.push(item);
        if (isTools) {
          this._renderToolChips(container, arr, roleKey);
        } else {
          this._renderSkillChips(container, arr, roleKey);
        }
        this._markDirty();
      };
    });
    container.appendChild(picker);
  },

  async save() {
    const status = document.getElementById("agentConfigStatus");

    // Collect role overrides from DOM
    const roles = {};
    document.querySelectorAll(".agent-role-card[data-role]").forEach((card) => {
      const role = card.dataset.role;
      const modelSel = card.querySelector(".arc-model");
      roles[role] = {
        output_format: this._roles[role]?.output_format || "",
        allowed_tools: this._roles[role]?.allowed_tools || [],
        allowed_skills: this._roles[role]?.allowed_skills || [],
        default_model: modelSel.value.trim(),
        default_timeout: parseInt(card.querySelector(".arc-timeout").value) || 90,
        max_parallel: parseInt(card.querySelector(".arc-parallel").value) || 1,
      };
      // Collect model fallback chain
      const chainChips = card.querySelectorAll(".arc-chain-pool .arc-acct-chip");
      if (chainChips.length) {
        roles[role].model_chain = [...chainChips].map((c) => c.textContent.replace("×", "").trim());
      } else {
        roles[role].model_chain = [];
      }
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
      teacher: {
        enabled: document.getElementById("teacherEnabled").checked,
        model: document.getElementById("teacherModel").value,
        browser_data_dir: document.getElementById("teacherBrowserData").value.trim(),
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
  // Register with universal save system if available
  if (window._universalSave) {
    window._universalSave.register("agents", () => AgentSettings.save());
  }

  // Load config when Agents tab is clicked
  document.querySelectorAll(".settings-tab").forEach((tab) => {
    if (tab.dataset.tab === "agents") {
      tab.addEventListener("click", () => {
        if (!AgentSettings.loaded) AgentSettings.load();
      });
    }
  });
});

// --------------------------------------------------------------------------
// Inject raw todos into spawn_agent skill cards in chat
// --------------------------------------------------------------------------
(function() {
  // Uses the module-level _agentTodosRaw Map populated by handleAgentEvent.
  // sse.js appends #<8char_id> to .skill-name via textContent mutation on skill_end,
  // so we need both a MutationObserver AND a periodic sweep to handle timing races.

  function tryInjectTodos(card) {
    if (card.querySelector(".ap-spawn-todos")) return; // already injected
    const nameEl = card.querySelector(".skill-name");
    if (!nameEl) return;
    const nameText = nameEl.textContent || "";
    const idMatch = nameText.match(/#([a-f0-9]{6,8})$/);
    if (!idMatch) return;
    const shortId = idMatch[1];
    for (const [fullId, raw] of _agentTodosRaw) {
      if (fullId.startsWith(shortId)) {
        const todoLine = document.createElement("span");
        todoLine.className = "ap-spawn-todos";
        todoLine.textContent = " · 📋 " + raw;
        nameEl.appendChild(todoLine);
        break;
      }
    }
  }

  // MutationObserver catches new cards being added
  const obs = new MutationObserver(() => {
    const chatEl = document.getElementById("chat");
    if (!chatEl) return;
    chatEl.querySelectorAll(".skill-card").forEach(tryInjectTodos);
  });

  const startObs = () => {
    const el = document.getElementById("chat");
    if (el) {
      obs.observe(el, { childList: true, subtree: true, characterData: true });
    } else {
      setTimeout(startObs, 500);
    }
  };
  startObs();

  // Periodic sweep handles the race where agent_spawned arrives after card render
  // Runs for 30s then stops (agents spawn quickly)
  let sweeps = 0;
  const sweepInterval = setInterval(() => {
    const chatEl = document.getElementById("chat");
    if (chatEl) {
      chatEl.querySelectorAll(".skill-card").forEach(tryInjectTodos);
    }
    if (++sweeps > 30) clearInterval(sweepInterval);
  }, 1000);
})();



// --------------------------------------------------------------------------
// TTS Settings Tab
// --------------------------------------------------------------------------
const TTSSettings = {
  loaded: false,
  _saveTimer: null,
  _currentProvider: 'kokoro',
  _currentPrefs: null,

  async init() {
    await this.loadPrefs();
    this._updateProviderUI();
    if (this._currentProvider === 'kokoro') {
      await this.loadKokoroStatus();
    } else {
      this._showEdgeSection();
    }
    this.loaded = true;
  },

  _updateProviderUI() {
    const kokoroBtn = document.getElementById('ttsProviderKokoro');
    const edgeBtn = document.getElementById('ttsProviderEdge');
    const kokoroSection = document.getElementById('ttsKokoroSection');
    const edgeSection = document.getElementById('ttsEdgeSection');
    const prefsSection = document.getElementById('ttsPrefsSection');

    if (kokoroBtn) kokoroBtn.style.opacity = this._currentProvider === 'kokoro' ? '1' : '0.5';
    if (edgeBtn) edgeBtn.style.opacity = this._currentProvider === 'edge' ? '1' : '0.5';
    if (kokoroSection) kokoroSection.hidden = this._currentProvider !== 'kokoro';
    if (edgeSection) edgeSection.hidden = this._currentProvider !== 'edge';
    // Prefs always visible once a provider is selected
    if (prefsSection) prefsSection.hidden = false;
  },

  async setProvider(provider) {
    this._currentProvider = provider;
    this._updateProviderUI();
    // Save provider preference
    try {
      await fetch('/api/settings/tts/prefs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ provider }),
      });
    } catch (e) { /* ignore */ }
    // Reload voices for the new provider
    await this.loadVoices();
    // If switching to kokoro, refresh model status
    if (provider === 'kokoro') {
      await this.loadKokoroStatus();
    }
  },

  _showEdgeSection() {
    const statusEl = document.getElementById('ttsStatus');
    if (statusEl) statusEl.innerHTML = '';
    this.loadVoices();
  },

  async loadKokoroStatus() {
    const statusEl = document.getElementById('ttsStatus');
    const dlBtn = document.getElementById('ttsDownloadBtn');
    const delBtn = document.getElementById('ttsDeleteBtn');
    try {
      const res = await fetch('/api/settings/tts', { headers: { Authorization: `Bearer ${localStorage.getItem('sable_token') || ''}` } });
      const data = await res.json();
      let html = '<div style="display:flex;flex-direction:column;gap:8px;">';
      for (const [name, info] of Object.entries(data.files)) {
        const icon = info.installed ? '\u2705' : '\u2b1c';
        const sizeMB = (info.size / 1048576).toFixed(1);
        const expectedMB = (info.expected / 1048576).toFixed(0);
        html += '<div style="display:flex;align-items:center;gap:8px;font-size:13px;">';
        html += '<span>' + icon + '</span>';
        html += '<span style="font-weight:500;">' + info.label + '</span>';
        html += '<span class="muted" style="font-size:11px;margin-left:auto;">';
        html += info.installed ? sizeMB + ' MB' : 'Not installed (' + expectedMB + ' MB)';
        html += '</span></div>';
      }
      html += '</div>';
      statusEl.innerHTML = html;
      dlBtn.hidden = data.installed;
      dlBtn.textContent = '\u2b07 Download Models';
      dlBtn.disabled = false;
      delBtn.hidden = !data.installed;
      if (data.installed) {
        this.loadVoices();
      } else {
        const select = document.getElementById('ttsVoiceSelect');
        if (select) select.innerHTML = '<option value="">Download Kokoro models first</option>';
      }
    } catch (e) {
      statusEl.innerHTML = '<p style="color:#e74c3c;font-size:12px;">Failed to check status: ' + e.message + '</p>';
    }
  },

  async loadPrefs() {
    try {
      const res = await fetch('/api/settings/tts/prefs');
      const prefs = await res.json();
      const speedRange = document.getElementById('ttsSpeedRange');
      const speedLabel = document.getElementById('ttsSpeedLabel');
      if (speedRange && prefs.speed != null) {
        speedRange.value = prefs.speed;
        if (speedLabel) speedLabel.textContent = parseFloat(prefs.speed).toFixed(1) + '\u00d7';
      }
      this._currentProvider = prefs.provider || 'kokoro';
      this._currentPrefs = prefs;
    } catch (e) { /* ignore */ }
  },

  async loadVoices() {
    const select = document.getElementById('ttsVoiceSelect');
    if (!select) return;
    const provider = this._currentProvider;
    try {
      const res = await fetch(`/api/tts/voices?provider=${encodeURIComponent(provider)}`);
      const data = await res.json();
      const voices = data.voices || [];
      select.innerHTML = '';
      if (voices.length === 0) {
        select.innerHTML = '<option value="">No voices available</option>';
        return;
      }
      if (provider === 'edge') {
        // Edge voices are objects with id, locale, gender
        voices.forEach(v => {
          const opt = document.createElement('option');
          opt.value = v.id;
          opt.textContent = `${v.id} (${v.locale}${v.gender ? ', ' + v.gender : ''})`;
          select.appendChild(opt);
        });
        // Restore saved edge_voice
        if (this._currentPrefs && this._currentPrefs.edge_voice) {
          select.value = this._currentPrefs.edge_voice;
        }
      } else {
        // Kokoro voices are plain strings
        voices.forEach(v => {
          const opt = document.createElement('option');
          opt.value = v;
          opt.textContent = v.replace(/_/g, ' ');
          select.appendChild(opt);
        });
        if (this._currentPrefs && this._currentPrefs.voice) {
          select.value = this._currentPrefs.voice;
        }
      }
    } catch (e) {
      select.innerHTML = '<option value="">Failed to load voices</option>';
    }
  },

  savePrefs() {
    clearTimeout(this._saveTimer);
    this._saveTimer = setTimeout(async () => {
      const voiceSelect = document.getElementById('ttsVoiceSelect');
      const speedRange = document.getElementById('ttsSpeedRange');
      const body = { provider: this._currentProvider };
      if (voiceSelect) {
        if (this._currentProvider === 'edge') {
          body.edge_voice = voiceSelect.value;
        } else {
          body.voice = voiceSelect.value;
        }
      }
      if (speedRange) body.speed = parseFloat(speedRange.value);
      try {
        await fetch('/api/settings/tts/prefs', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
      } catch (e) { /* ignore */ }
    }, 300);
  },

  async download() {
    const dlBtn = document.getElementById('ttsDownloadBtn');
    const progressEl = document.getElementById('ttsProgress');
    const fillEl = document.getElementById('ttsProgressFill');
    const textEl = document.getElementById('ttsProgressText');

    dlBtn.disabled = true;
    dlBtn.textContent = '\u23f3 Downloading...';
    progressEl.hidden = false;
    fillEl.style.width = '0%';

    try {
      const res = await fetch('/api/settings/tts/download', { method: 'POST', headers: { Authorization: `Bearer ${localStorage.getItem('sable_token') || ''}` } });
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop();
        for (const line of lines) {
          if (!line.trim()) continue;
          try {
            const msg = JSON.parse(line);
            if (msg.status === 'progress') {
              const pct = Math.round((msg.downloaded / msg.total) * 100);
              fillEl.style.width = pct + '%';
              textEl.textContent = msg.file + ' \u2014 ' + pct + '% (' + (msg.downloaded / 1048576).toFixed(1) + ' / ' + (msg.total / 1048576).toFixed(0) + ' MB)';
            } else if (msg.status === 'done') {
              textEl.textContent = '\u2705 ' + msg.file + ' complete';
            } else if (msg.status === 'skip') {
              textEl.textContent = '\u23ed ' + msg.file + ' already installed';
            } else if (msg.status === 'error') {
              textEl.textContent = '\u274c ' + msg.file + ': ' + msg.error;
              fillEl.style.background = '#e74c3c';
            } else if (msg.status === 'complete') {
              textEl.textContent = '\ud83c\udf89 All models downloaded!';
              fillEl.style.width = '100%';
              fillEl.style.background = '#4caf50';
            }
          } catch (parseErr) { /* skip malformed lines */ }
        }
      }
    } catch (e) {
      textEl.textContent = '\u274c Download failed: ' + e.message;
    }

    dlBtn.disabled = false;
    dlBtn.textContent = '\u2b07 Download Models';
    this.loadKokoroStatus();
  },

  async remove() {
    if (!confirm('Remove all TTS model files? (338 MB)')) return;
    const delBtn = document.getElementById('ttsDeleteBtn');
    delBtn.disabled = true;
    try {
      await fetch('/api/settings/tts', { method: 'DELETE', headers: { Authorization: `Bearer ${localStorage.getItem('sable_token') || ''}` } });
    } catch (e) { /* ignore */ }
    delBtn.disabled = false;
    this.loadKokoroStatus();
  },
};

// Wire up Voice tab (merged TTS + STT)
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.settings-tab').forEach((tab) => {
    if (tab.dataset.tab === 'voice') {
      tab.addEventListener('click', () => {
        if (!TTSSettings.loaded) TTSSettings.init();
        if (!STTSettings.loaded) STTSettings.init();
      });
    }
  });

  // Provider buttons
  const kokoroBtn = document.getElementById('ttsProviderKokoro');
  if (kokoroBtn) kokoroBtn.addEventListener('click', () => TTSSettings.setProvider('kokoro'));
  const edgeBtn = document.getElementById('ttsProviderEdge');
  if (edgeBtn) edgeBtn.addEventListener('click', () => TTSSettings.setProvider('edge'));

  const dlBtn = document.getElementById('ttsDownloadBtn');
  if (dlBtn) dlBtn.addEventListener('click', () => TTSSettings.download());

  const delBtn = document.getElementById('ttsDeleteBtn');
  if (delBtn) delBtn.addEventListener('click', () => TTSSettings.remove());

  // Voice & speed controls — auto-save on change
  const voiceSelect = document.getElementById('ttsVoiceSelect');
  if (voiceSelect) voiceSelect.addEventListener('change', () => TTSSettings.savePrefs());

  const speedRange = document.getElementById('ttsSpeedRange');
  const speedLabel = document.getElementById('ttsSpeedLabel');
  if (speedRange) {
    speedRange.addEventListener('input', () => {
      if (speedLabel) speedLabel.textContent = parseFloat(speedRange.value).toFixed(1) + '\u00d7';
    });
    speedRange.addEventListener('change', () => TTSSettings.savePrefs());
  }
});

// ── STT Settings ─────────────────────────────────────────────────────
const STTSettings = {
  loaded: false,
  _saveTimer: null,
  _currentPrefs: null,

  async init() {
    await this.loadPrefs();
    await this.loadStatus();
    this.loaded = true;
  },

  async loadStatus() {
    const statusEl = document.getElementById('sttStatus');
    const delBtn = document.getElementById('sttDeleteBtn');
    try {
      const res = await fetch('/api/settings/stt', { headers: { Authorization: `Bearer ${localStorage.getItem('sable_token') || ''}` } });
      const data = await res.json();
      let html = '<div style="display:flex;flex-direction:column;gap:8px;">';
      for (const [name, info] of Object.entries(data.files)) {
        const icon = info.installed ? '\u2705' : '\u2b1c';
        const sizeMB = (info.size / 1048576).toFixed(1);
        const expectedMB = (info.expected / 1048576).toFixed(0);
        html += '<div style="display:flex;align-items:center;gap:8px;font-size:13px;">';
        html += '<span>' + icon + '</span>';
        html += '<span style="font-weight:500;">' + info.label + '</span>';
        html += '<span class="muted" style="font-size:11px;margin-left:auto;">';
        html += info.installed ? sizeMB + ' MB' : 'Not installed (' + expectedMB + ' MB)';
        html += '</span></div>';
      }
      html += '</div>';
      if (data.installed) {
        html += '<p class="muted" style="font-size:11px;margin-top:8px;">\u2705 Model ready \u2014 offline transcription available</p>';
      }
      statusEl.innerHTML = html;
      const dlBtn = document.getElementById('sttDownloadBtn');
      const dlProgress = document.getElementById('sttDownloadProgress');
      if (dlBtn) {
        dlBtn.style.display = data.installed ? 'none' : '';
        dlBtn.disabled = false;
        dlBtn.innerHTML = '<i data-lucide="download" class="icon-lucide"></i> Download Model (~462 MB)';
        activateLucideIcons(dlBtn);
      }
      if (dlProgress) dlProgress.style.display = 'none';
      if (delBtn) delBtn.hidden = !data.installed;
    } catch (e) {
      statusEl.innerHTML = '<p style="color:#e74c3c;font-size:12px;">Failed to check status: ' + e.message + '</p>';
    }
  },

  async loadPrefs() {
    try {
      const res = await fetch('/api/settings/stt/prefs');
      const prefs = await res.json();
      this._currentPrefs = prefs;

      const langSelect = document.getElementById('sttLangSelect');
      if (langSelect && prefs.language) langSelect.value = prefs.language;

      const beamRange = document.getElementById('sttBeamRange');
      const beamLabel = document.getElementById('sttBeamLabel');
      if (beamRange && prefs.beam_size != null) {
        beamRange.value = prefs.beam_size;
        if (beamLabel) beamLabel.textContent = prefs.beam_size;
      }

      const computeSelect = document.getElementById('sttComputeSelect');
      if (computeSelect && prefs.compute_type) computeSelect.value = prefs.compute_type;
    } catch (e) { /* ignore */ }
  },

  savePrefs() {
    clearTimeout(this._saveTimer);
    this._saveTimer = setTimeout(async () => {
      const body = {};
      const langSelect = document.getElementById('sttLangSelect');
      const beamRange = document.getElementById('sttBeamRange');
      const computeSelect = document.getElementById('sttComputeSelect');
      if (langSelect) body.language = langSelect.value;
      if (beamRange) body.beam_size = parseInt(beamRange.value);
      if (computeSelect) body.compute_type = computeSelect.value;
      try {
        await fetch('/api/settings/stt/prefs', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
      } catch (e) { /* ignore */ }
    }, 300);
  },

  async remove() {
    if (!confirm('Remove STT model files? (~464 MB)')) return;
    const delBtn = document.getElementById('sttDeleteBtn');
    if (delBtn) delBtn.disabled = true;
    try {
      await fetch('/api/settings/stt', { method: 'DELETE', headers: { Authorization: `Bearer ${localStorage.getItem('sable_token') || ''}` } });
    } catch (e) { /* ignore */ }
    if (delBtn) delBtn.disabled = false;
    this.loadStatus();
  },

  async download() {
    const dlBtn = document.getElementById('sttDownloadBtn');
    const dlProgress = document.getElementById('sttDownloadProgress');
    if (dlBtn) { dlBtn.disabled = true; dlBtn.textContent = 'Downloading...'; }
    if (dlProgress) { dlProgress.style.display = ''; dlProgress.textContent = 'Starting...'; }

    try {
      const res = await fetch('/api/settings/stt/download', {
        method: 'POST',
        headers: { Authorization: `Bearer ${localStorage.getItem('sable_token') || ''}` },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';

        for (const line of lines) {
          if (!line.trim()) continue;
          try {
            const msg = JSON.parse(line);
            if (msg.status === 'progress') {
              const pct = ((msg.downloaded / msg.total) * 100).toFixed(1);
              const mbDone = (msg.downloaded / 1048576).toFixed(1);
              const mbTotal = (msg.total / 1048576).toFixed(0);
              if (dlProgress) dlProgress.textContent = `${msg.file}: ${pct}% (${mbDone}/${mbTotal} MB)`;
            } else if (msg.status === 'start') {
              if (dlProgress) dlProgress.textContent = `Downloading ${msg.file}...`;
            } else if (msg.status === 'done') {
              if (dlProgress) dlProgress.textContent = `${msg.file} ✓`;
            } else if (msg.status === 'skip') {
              // already installed
            } else if (msg.status === 'error') {
              if (dlProgress) dlProgress.textContent = `Error: ${msg.error}`;
            } else if (msg.status === 'cancelled') {
              if (dlProgress) dlProgress.textContent = 'Cancelled';
            } else if (msg.status === 'complete') {
              if (dlProgress) dlProgress.textContent = '✅ Complete!';
            }
          } catch (_) { /* skip malformed lines */ }
        }
      }
    } catch (e) {
      if (dlProgress) dlProgress.textContent = 'Failed: ' + e.message;
    } finally {
      setTimeout(() => this.loadStatus(), 1000);
    }
  },

  async transcribe(file) {
    const btn = document.getElementById('sttTranscribeBtn');
    const resultDiv = document.getElementById('sttResult');
    const metaEl = document.getElementById('sttResultMeta');
    const textEl = document.getElementById('sttResultText');

    if (btn) { btn.disabled = true; btn.textContent = 'Transcribing...'; }
    if (resultDiv) resultDiv.hidden = true;

    const formData = new FormData();
    formData.append('file', file);

    try {
      const res = await fetch('/api/stt/transcribe', {
        method: 'POST',
        headers: { Authorization: `Bearer ${localStorage.getItem('sable_token') || ''}` },
        body: formData,
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: 'Unknown error' }));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      const data = await res.json();
      if (metaEl) {
        metaEl.textContent = `Duration: ${data.duration}s | Language: ${data.language} (${(data.language_probability * 100).toFixed(0)}%) | Segments: ${data.segments.length}`;
      }
      if (textEl) textEl.textContent = data.text;
      if (resultDiv) resultDiv.hidden = false;
    } catch (e) {
      if (textEl) textEl.textContent = 'Error: ' + e.message;
      if (metaEl) metaEl.textContent = '';
      if (resultDiv) resultDiv.hidden = false;
    } finally {
      if (btn) { btn.disabled = false; btn.innerHTML = '<i data-lucide="mic" class="icon-lucide"></i> Transcribe'; activateLucideIcons(btn); }
    }
  },
};

// ── Live Mic Recorder ───────────────────────────────────────────────
const STTMicRecorder = {
  _mediaRecorder: null,
  _chunks: [],
  _stream: null,
  _analyser: null,
  _animFrame: null,
  _recording: false,
  _startTime: 0,
  _timerInterval: null,

  async toggle() {
    if (this._recording) {
      this.stop();
    } else {
      await this.start();
    }
  },

  async start() {
    const btn = document.getElementById('sttMicBtn');
    const statusEl = document.getElementById('sttMicStatus');
    const visualizer = document.getElementById('sttVisualizer');
    const resultDiv = document.getElementById('sttLiveResult');

    try {
      this._stream = await navigator.mediaDevices.getUserMedia({ audio: { sampleRate: 16000, channelCount: 1, echoCancellation: true, noiseSuppression: true } });
    } catch (e) {
      if (statusEl) statusEl.textContent = '❌ Mic access denied: ' + e.message;
      return;
    }

    // Pick best supported MIME type
    const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
      ? 'audio/webm;codecs=opus'
      : MediaRecorder.isTypeSupported('audio/webm')
        ? 'audio/webm'
        : 'audio/mp4';

    this._chunks = [];
    this._mediaRecorder = new MediaRecorder(this._stream, { mimeType });
    this._mediaRecorder.ondataavailable = (e) => { if (e.data.size > 0) this._chunks.push(e.data); };
    this._mediaRecorder.onstop = () => this._onRecordingStopped(mimeType);
    this._mediaRecorder.start(250); // collect chunks every 250ms

    this._recording = true;
    this._startTime = Date.now();

    // UI updates
    if (btn) { btn.innerHTML = '<i data-lucide="square" class="icon-lucide"></i> Stop'; btn.classList.add('stt-recording'); activateLucideIcons(btn); }
    if (statusEl) statusEl.textContent = '🔴 Recording... 0.0s';
    if (resultDiv) resultDiv.hidden = true;
    if (visualizer) visualizer.hidden = false;

    // Timer
    this._timerInterval = setInterval(() => {
      const elapsed = ((Date.now() - this._startTime) / 1000).toFixed(1);
      if (statusEl) statusEl.textContent = `🔴 Recording... ${elapsed}s`;
    }, 100);

    // Visualizer
    this._startVisualizer();
  },

  stop() {
    if (!this._recording) return;
    this._recording = false;
    if (this._mediaRecorder && this._mediaRecorder.state !== 'inactive') {
      this._mediaRecorder.stop();
    }
    if (this._stream) {
      this._stream.getTracks().forEach(t => t.stop());
      this._stream = null;
    }
    clearInterval(this._timerInterval);
    cancelAnimationFrame(this._animFrame);

    const btn = document.getElementById('sttMicBtn');
    const statusEl = document.getElementById('sttMicStatus');
    const visualizer = document.getElementById('sttVisualizer');
    if (btn) { btn.innerHTML = '<i data-lucide="mic" class="icon-lucide"></i> Record'; btn.classList.remove('stt-recording'); activateLucideIcons(btn); }
    if (statusEl) statusEl.textContent = 'Processing...';
    if (visualizer) visualizer.hidden = true;
  },

  _startVisualizer() {
    const canvas = document.getElementById('sttVisualizer');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    this._analyser = this._stream.getAudioContext?.()?.createAnalyser?.();
    // Fallback: create AudioContext manually
    if (!this._analyser) {
      const actx = new (window.AudioContext || window.webkitAudioContext)();
      const source = actx.createMediaStreamSource(this._stream);
      this._analyser = actx.createAnalyser();
      this._analyser.fftSize = 256;
      source.connect(this._analyser);
    }
    const bufLen = this._analyser.frequencyBinCount;
    const dataArr = new Uint8Array(bufLen);

    const draw = () => {
      if (!this._recording) return;
      this._animFrame = requestAnimationFrame(draw);
      this._analyser.getByteFrequencyData(dataArr);
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      const barW = canvas.width / bufLen * 2.5;
      let x = 0;
      for (let i = 0; i < bufLen; i++) {
        const h = (dataArr[i] / 255) * canvas.height;
        ctx.fillStyle = `rgba(var(--accent-rgb,100,180,255),${0.3 + dataArr[i] / 255 * 0.7})`;
        ctx.fillRect(x, canvas.height - h, barW - 1, h);
        x += barW;
      }
    };
    draw();
  },

  async _onRecordingStopped(mimeType) {
    const statusEl = document.getElementById('sttMicStatus');
    const blob = new Blob(this._chunks, { type: mimeType });
    const ext = mimeType.includes('webm') ? '.webm' : '.mp4';
    const file = new File([blob], `recording${ext}`, { type: mimeType });

    if (statusEl) statusEl.textContent = 'Transcribing...';
    await STTSettings.transcribeLive(file);
    if (statusEl) statusEl.textContent = '';
  },
};

// Add transcribeLive to STTSettings (uses live result area)
STTSettings.transcribeLive = async function(file) {
  const resultDiv = document.getElementById('sttLiveResult');
  const metaEl = document.getElementById('sttLiveMeta');
  const textEl = document.getElementById('sttLiveText');

  if (resultDiv) resultDiv.hidden = true;

  const formData = new FormData();
  formData.append('file', file);

  try {
    const res = await fetch('/api/stt/transcribe', {
      method: 'POST',
      headers: { Authorization: `Bearer ${localStorage.getItem('sable_token') || ''}` },
      body: formData,
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: 'Unknown error' }));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    const data = await res.json();
    if (metaEl) {
      metaEl.textContent = `Duration: ${data.duration}s | Language: ${data.language} (${(data.language_probability * 100).toFixed(0)}%) | Segments: ${data.segments.length}`;
    }
    if (textEl) textEl.textContent = data.text;
    if (resultDiv) resultDiv.hidden = false;
  } catch (e) {
    if (textEl) textEl.textContent = 'Error: ' + e.message;
    if (metaEl) metaEl.textContent = '';
    if (resultDiv) resultDiv.hidden = false;
  }
};

// STT controls (init now handled by Voice tab click above)
document.addEventListener('DOMContentLoaded', () => {
  const sttDlBtn = document.getElementById('sttDownloadBtn');
  if (sttDlBtn) sttDlBtn.addEventListener('click', () => STTSettings.download());

  const sttDelBtn = document.getElementById('sttDeleteBtn');
  if (sttDelBtn) sttDelBtn.addEventListener('click', () => STTSettings.remove());

  // Live mic button
  const sttMicBtn = document.getElementById('sttMicBtn');
  if (sttMicBtn) sttMicBtn.addEventListener('click', () => STTMicRecorder.toggle());

  // File upload
  const sttFileInput = document.getElementById('sttFileInput');
  const sttTranscribeBtn = document.getElementById('sttTranscribeBtn');
  if (sttFileInput) {
    sttFileInput.addEventListener('change', () => {
      if (sttTranscribeBtn) sttTranscribeBtn.disabled = !sttFileInput.files.length;
    });
  }
  if (sttTranscribeBtn) {
    sttTranscribeBtn.addEventListener('click', () => {
      const file = sttFileInput?.files?.[0];
      if (file) STTSettings.transcribe(file);
    });
  }

  // Prefs auto-save
  const sttLangSelect = document.getElementById('sttLangSelect');
  if (sttLangSelect) sttLangSelect.addEventListener('change', () => STTSettings.savePrefs());

  const sttBeamRange = document.getElementById('sttBeamRange');
  const sttBeamLabel = document.getElementById('sttBeamLabel');
  if (sttBeamRange) {
    sttBeamRange.addEventListener('input', () => {
      if (sttBeamLabel) sttBeamLabel.textContent = sttBeamRange.value;
    });
    sttBeamRange.addEventListener('change', () => STTSettings.savePrefs());
  }

  const sttComputeSelect = document.getElementById('sttComputeSelect');
  if (sttComputeSelect) sttComputeSelect.addEventListener('change', () => STTSettings.savePrefs());
});

// ── Global Voice Input (status bar mic + Ctrl+Space) ────────────────
const GlobalVoiceInput = {
  _mediaRecorder: null,
  _chunks: [],
  _stream: null,
  _analyser: null,
  _audioCtx: null,
  _recording: false,
  _heldDown: false,
  _btn: null,
  _startTime: 0,
  _maxCapTimer: null,
  _silenceTimer: null,
  _rmsFrame: null,

  // Configurable thresholds
  MAX_RECORDING_SECONDS: 120,
  SILENCE_THRESHOLD: 0.01,   // RMS below this = silence
  SILENCE_DURATION_MS: 1500, // ms of continuous silence before auto-stop
  STOP_PHRASES: ['stop', 'cancel', 'never mind', 'nevermind', 'shut up', 'abort', 'enough'],

  init() {
    this._btn = document.getElementById('sttMicGlobalBtn');
    if (!this._btn) return;

    this._btn.addEventListener('click', (e) => {
      e.preventDefault();
      if (this._recording) this.stop();
      else this.start(false);
    });

    let spaceHeld = false;
    document.addEventListener('keydown', (e) => {
      const tag = e.target?.tagName;
      const isInput = tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT';
      if ((e.ctrlKey || e.metaKey) && e.code === 'Space') {
        e.preventDefault();
        if (!spaceHeld && !this._recording) {
          spaceHeld = true;
          this._heldDown = true;
          this.start(true);
        }
      }
    });

    document.addEventListener('keyup', (e) => {
      if ((e.ctrlKey || e.metaKey || e.code === 'Space') && spaceHeld) {
        if (e.code === 'Space' || !e.ctrlKey) {
          spaceHeld = false;
          if (this._recording && this._heldDown) {
            this.stop();
          }
        }
      }
    });

    window.addEventListener('blur', () => {
      if (this._recording) this.stop();
    });
  },

  async start(heldMode) {
    if (this._recording) return;

    try {
      this._stream = await navigator.mediaDevices.getUserMedia({
        audio: { sampleRate: 16000, channelCount: 1, echoCancellation: true, noiseSuppression: true }
      });
    } catch (e) {
      console.error('[STT] Mic access denied:', e);
      return;
    }

    const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
      ? 'audio/webm;codecs=opus'
      : MediaRecorder.isTypeSupported('audio/webm')
        ? 'audio/webm'
        : 'audio/mp4';

    this._chunks = [];
    this._heldDown = heldMode;
    this._mediaRecorder = new MediaRecorder(this._stream, { mimeType });
    this._mediaRecorder.ondataavailable = (e) => { if (e.data.size > 0) this._chunks.push(e.data); };
    this._mediaRecorder.onstop = () => this._onStopped(mimeType);
    this._mediaRecorder.start(250);
    this._recording = true;
    this._startTime = Date.now();

    // Max recording cap
    this._maxCapTimer = setTimeout(() => {
      if (this._recording) {
        console.log('[STT] Max recording cap reached, auto-stopping');
        this.stop();
      }
    }, this.MAX_RECORDING_SECONDS * 1000);

    // Silence auto-stop via RMS monitoring
    this._setupSilenceDetection();

    if (this._btn) {
      this._btn.classList.add('recording');
      this._btn.title = heldMode ? 'Release Ctrl+Space to stop' : 'Recording... click to stop';
    }
  },

  _setupSilenceDetection() {
    try {
      this._audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      const source = this._audioCtx.createMediaStreamSource(this._stream);
      this._analyser = this._audioCtx.createAnalyser();
      this._analyser.fftSize = 256;
      source.connect(this._analyser);
    } catch (e) {
      console.warn('[STT] AudioContext for silence detection failed:', e);
      return;
    }

    const bufLen = this._analyser.frequencyBinCount;
    const dataArr = new Float32Array(bufLen);
    let silentSince = null;

    const checkRMS = () => {
      if (!this._recording) return;
      this._rmsFrame = requestAnimationFrame(checkRMS);

      this._analyser.getFloatTimeDomainData(dataArr);
      let sumSq = 0;
      for (let i = 0; i < bufLen; i++) sumSq += dataArr[i] * dataArr[i];
      const rms = Math.sqrt(sumSq / bufLen);

      if (rms < this.SILENCE_THRESHOLD) {
        if (!silentSince) silentSince = Date.now();
        else if (Date.now() - silentSince >= this.SILENCE_DURATION_MS) {
          console.log('[STT] Silence detected, auto-stopping');
          this.stop();
          return;
        }
      } else {
        silentSince = null;
      }
    };
    checkRMS();
  },

  stop() {
    if (!this._recording) return;
    this._recording = false;

    if (this._maxCapTimer) { clearTimeout(this._maxCapTimer); this._maxCapTimer = null; }
    if (this._rmsFrame) { cancelAnimationFrame(this._rmsFrame); this._rmsFrame = null; }
    if (this._audioCtx) { this._audioCtx.close().catch(() => {}); this._audioCtx = null; }

    if (this._mediaRecorder && this._mediaRecorder.state !== 'inactive') {
      this._mediaRecorder.stop();
    }
    if (this._stream) {
      this._stream.getTracks().forEach(t => t.stop());
      this._stream = null;
    }

    if (this._btn) {
      this._btn.classList.remove('recording');
      this._btn.classList.add('transcribing');
      this._btn.title = 'Transcribing...';
    }
  },

  _isStopPhrase(text) {
    const lower = text.toLowerCase().trim().replace(/[.,!?]/g, '');
    return this.STOP_PHRASES.some(phrase => lower === phrase || lower.startsWith(phrase + ' '));
  },

  async _onStopped(mimeType) {
    const blob = new Blob(this._chunks, { type: mimeType });
    const ext = mimeType.includes('webm') ? '.webm' : '.mp4';
    const file = new File([blob], `voice${ext}`, { type: mimeType });

    try {
      const formData = new FormData();
      formData.append('file', file);

      const sessionId = localStorage.getItem('sable_session_id') || '';
      const headers = { Authorization: `Bearer ${localStorage.getItem('sable_token') || ''}` };
      if (sessionId) headers['X-Session-Id'] = sessionId;

      const res = await fetch('/api/stt/transcribe', {
        method: 'POST',
        headers,
        body: formData,
      });

      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: 'Unknown error' }));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }

      const data = await res.json();

      // Skip filtered transcripts (hallucination / echo)
      if (data.filtered) {
        console.log(`[STT] Transcript filtered: ${data.filtered}`);
        return;
      }

      const text = data.text?.trim();
      if (!text) return;

      // Stop phrase detection
      if (this._isStopPhrase(text)) {
        console.log('[STT] Stop phrase detected, discarding');
        return;
      }

      this._insertText(text);
    } catch (e) {
      console.error('[STT] Transcription failed:', e);
    } finally {
      this._heldDown = false;
      if (this._btn) {
        this._btn.classList.remove('transcribing');
        this._btn.title = 'Voice input (hold Ctrl+Space)';
      }
    }
  },

  _insertText(text) {
    const input = document.getElementById('input') || document.getElementById('chatCompactInput');
    if (!input) return;

    const existing = input.value.trim();
    input.value = existing ? `${existing} ${text}` : text;

    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 200) + 'px';

    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
    input.dispatchEvent(new Event('input', { bubbles: true }));
  },
};

// ── Live Voice Chat ─────────────────────────────────────────────────────
// Click live button → mic records continuously → chunks transcribed in real-time
// → text appears in input as you speak → silence triggers auto-send → bot responds
// → response auto-plays via TTS → cycle repeats until you click stop.
const LiveVoiceChat = {
  _active: false,
  _stream: null,
  _analyser: null,
  _audioCtx: null,
  _rmsFrame: null,
  _btn: null,

  // Recording state
  _mediaRecorder: null,
  _chunks: [],
  _chunkTimer: null,
  _mimeType: '',

  // Transcript accumulation
  _accumulatedText: '',
  _pendingTranscriptions: 0, // track in-flight requests

  // Silence detection
  _silentSince: null,
  _hasSpeech: false, // true once we get at least one transcript chunk

  CHUNK_MS: 2000,           // record 2s chunks
  SILENCE_RMS: 0.01,        // RMS below this = silence
  SEND_SILENCE_MS: 1800,    // send after 1.8s of continuous silence (if we have text)
  STOP_PHRASES: ['stop', 'cancel', 'never mind', 'nevermind', 'shut up', 'abort', 'enough'],

  init() {
    this._btn = document.getElementById('liveChatBtn');
    if (!this._btn) return;
    this._btn.addEventListener('click', (e) => {
      e.preventDefault();
      if (this._active) this.stop();
      else this.start();
    });
  },

  async start() {
    if (this._active) return;

    try {
      this._stream = await navigator.mediaDevices.getUserMedia({
        audio: { sampleRate: 16000, channelCount: 1, echoCancellation: true, noiseSuppression: true }
      });
    } catch (e) {
      console.error('[Live] Mic denied:', e);
      return;
    }

    this._active = true;
    this._accumulatedText = '';
    this._pendingTranscriptions = 0;
    this._silentSince = null;
    this._hasSpeech = false;

    // Determine codec
    this._mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
      ? 'audio/webm;codecs=opus'
      : MediaRecorder.isTypeSupported('audio/webm')
        ? 'audio/webm'
        : 'audio/mp4';

    this._setupRMS();
    this._startChunkLoop();

    if (this._btn) {
      this._btn.classList.add('recording');
      this._btn.title = 'Live voice chat — click to stop';
    }

    // Kill any playing TTS
    if (typeof stopGlobalTTS === 'function') stopGlobalTTS();

    console.log('[Live] Started');
  },

  _setupRMS() {
    try {
      this._audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      const src = this._audioCtx.createMediaStreamSource(this._stream);
      this._analyser = this._audioCtx.createAnalyser();
      this._analyser.fftSize = 256;
      src.connect(this._analyser);
    } catch (e) {
      console.warn('[Live] AudioContext failed:', e);
      return;
    }

    const bufLen = this._analyser.frequencyBinCount;
    const data = new Float32Array(bufLen);

    const tick = () => {
      if (!this._active) return;
      this._rmsFrame = requestAnimationFrame(tick);

      this._analyser.getFloatTimeDomainData(data);
      let sum = 0;
      for (let i = 0; i < bufLen; i++) sum += data[i] * data[i];
      const rms = Math.sqrt(sum / bufLen);

      if (rms < this.SILENCE_RMS) {
        if (!this._silentSince) this._silentSince = Date.now();
        else if (this._hasSpeech && Date.now() - this._silentSince >= this.SEND_SILENCE_MS) {
          // Enough silence after speech — flush and send
          this._flushAndSend();
        }
      } else {
        this._silentSince = null;
      }
    };
    tick();
  },

  _startChunkLoop() {
    const recordOne = () => {
      if (!this._active) return;

      this._chunks = [];
      this._mediaRecorder = new MediaRecorder(this._stream, { mimeType: this._mimeType });
      this._mediaRecorder.ondataavailable = (e) => {
        if (e.data.size > 0) this._chunks.push(e.data);
      };
      this._mediaRecorder.onstop = () => {
        this._transcribeChunk();
        // Start next chunk immediately
        if (this._active) recordOne();
      };
      this._mediaRecorder.start();

      this._chunkTimer = setTimeout(() => {
        if (this._mediaRecorder?.state === 'recording') this._mediaRecorder.stop();
      }, this.CHUNK_MS);
    };

    recordOne();
  },

  async _transcribeChunk() {
    if (this._chunks.length === 0) return;

    const blob = new Blob(this._chunks, { type: this._mimeType });
    const ext = this._mimeType.includes('webm') ? '.webm' : '.mp4';
    const file = new File([blob], `live${ext}`, { type: this._mimeType });

    this._pendingTranscriptions++;

    try {
      const form = new FormData();
      form.append('file', file);

      const sid = localStorage.getItem('sable_session_id') || '';
      const hdrs = { Authorization: `Bearer ${localStorage.getItem('sable_token') || ''}` };
      if (sid) hdrs['X-Session-Id'] = sid;

      const res = await fetch('/api/stt/transcribe', { method: 'POST', headers: hdrs, body: form });
      if (!res.ok) { console.error('[Live] STT error:', res.status); return; }

      const data = await res.json();
      if (data.filtered || !data.text?.trim()) return;

      const text = data.text.trim();

      // Stop phrase check
      const lower = text.toLowerCase().replace(/[.,!?]/g, '').trim();
      if (this.STOP_PHRASES.some(p => lower === p || lower.startsWith(p + ' '))) {
        console.log('[Live] Stop phrase, ending');
        this.stop();
        return;
      }

      // Accumulate
      this._accumulatedText += (this._accumulatedText ? ' ' : '') + text;
      this._hasSpeech = true;

      // Show in input box live
      const inp = document.getElementById('input');
      if (inp) {
        inp.value = this._accumulatedText;
        inp.style.height = 'auto';
        inp.style.height = Math.min(inp.scrollHeight, 200) + 'px';
        inp.dispatchEvent(new Event('input', { bubbles: true }));
      }

      console.log('[Live] Chunk:', text);
    } catch (e) {
      console.error('[Live] Transcribe error:', e);
    } finally {
      this._pendingTranscriptions--;
    }
  },

  _flushAndSend() {
    const text = this._accumulatedText.trim();
    if (!text) return;

    // Reset for next utterance
    this._accumulatedText = '';
    this._hasSpeech = false;
    this._silentSince = null;

    // Clear input visually
    const inp = document.getElementById('input');
    if (inp) {
      inp.value = '';
      inp.style.height = 'auto';
      inp.dispatchEvent(new Event('input', { bubbles: true }));
    }

    // Send via exposed sendMessage
    if (typeof window.sendMessage === 'function') {
      // Set input value so sendMessage reads it
      if (inp) inp.value = text;
      window.sendMessage();
      console.log('[Live] Sent:', text);
    }
  },

  stop() {
    if (!this._active) return;
    this._active = false;

    if (this._chunkTimer) { clearTimeout(this._chunkTimer); this._chunkTimer = null; }
    if (this._rmsFrame) { cancelAnimationFrame(this._rmsFrame); this._rmsFrame = null; }
    if (this._audioCtx) { this._audioCtx.close().catch(() => {}); this._audioCtx = null; }
    if (this._mediaRecorder?.state === 'recording') this._mediaRecorder.stop();
    if (this._stream) { this._stream.getTracks().forEach(t => t.stop()); this._stream = null; }

    // Flush remaining text
    if (this._accumulatedText.trim()) this._flushAndSend();

    if (this._btn) {
      this._btn.classList.remove('recording');
      this._btn.title = 'Live voice chat';
    }

    console.log('[Live] Stopped');
  },
};

// Initialize voice systems on DOM ready
document.addEventListener('DOMContentLoaded', () => {
  GlobalVoiceInput.init();
  LiveVoiceChat.init();
});
