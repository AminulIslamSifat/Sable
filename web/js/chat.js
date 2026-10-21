
    const MAX_TOASTS = 4;

    function dismissToast(el) {
      if (!el || !el.isConnected) return;
      clearTimeout(el._timer);
      el.classList.remove("show");
      el.addEventListener("transitionend", () => el.remove(), { once: true });
      // fallback in case transitionend never fires (reduced-motion, display:none, etc.)
      setTimeout(() => el.remove(), 450);
    }

    // lucide icon per toast type (matches the rest of the UI)
    const TOAST_ICONS = { info: "info", success: "check", error: "triangle-alert" };

    function showToast(msg, type = "info") {
      // cap the stack — drop the oldest if we're at the limit
      const live = toastEl.querySelectorAll(".toast");
      if (live.length >= MAX_TOASTS) dismissToast(live[0]);

      const el = document.createElement("div");
      el.className = `toast ${type}`;

      // icon node built via createElement (fixed lucide name) — message stays textContent (injection-safe)
      const icon = document.createElement("i");
      icon.className = "toast-icon";
      icon.setAttribute("data-lucide", TOAST_ICONS[type] || TOAST_ICONS.info);
      el.appendChild(icon);

      const text = document.createElement("span");
      text.className = "toast-msg";
      text.textContent = msg;
      el.appendChild(text);

      el.addEventListener("click", () => dismissToast(el));

      toastEl.appendChild(el);
      if (window.lucide) window.lucide.createIcons({ nodes: [el] });

      requestAnimationFrame(() => el.classList.add("show"));

      el._timer = setTimeout(() => dismissToast(el), 3500);
    }
    window.showToast = showToast; // expose for filesystem.js


    /* ---------- Sable Dialog — themed replacement for confirm/prompt/alert ---------- */
    // NOTE: named chatDialog, NOT sableDialog — dialog.js owns window.sableDialog
    // (an object with prompt/confirm/alert/pick/form). A top-level function named
    // `sableDialog` here would overwrite that object and break sableDialog.prompt().
    function chatDialog({ title, message, type = 'confirm', defaultValue = '', danger = false }) {
      return new Promise(resolve => {
        const overlay = document.createElement('div');
        overlay.className = 'sable-dialog-overlay';

        const modal = document.createElement('div');
        modal.className = 'sable-dialog';

        if (title) {
          const t = document.createElement('div');
          t.className = 'sable-dialog-title';
          t.textContent = title;
          modal.appendChild(t);
        }

        if (message) {
          const m = document.createElement('div');
          m.className = 'sable-dialog-msg';
          m.textContent = message;
          modal.appendChild(m);
        }

        let inputEl = null;
        if (type === 'prompt') {
          inputEl = document.createElement('input');
          inputEl.type = 'text';
          inputEl.className = 'sable-dialog-input';
          inputEl.value = defaultValue || '';
          modal.appendChild(inputEl);
        }

        const actions = document.createElement('div');
        actions.className = 'sable-dialog-actions';

        if (type !== 'alert') {
          const cancelBtn = document.createElement('button');
          cancelBtn.className = 'sable-dialog-btn';
          cancelBtn.textContent = 'Cancel';
          cancelBtn.onclick = () => { overlay.remove(); resolve(type === 'prompt' ? null : false); };
          actions.appendChild(cancelBtn);
        }

        const okBtn = document.createElement('button');
        okBtn.className = 'sable-dialog-btn primary' + (danger ? ' danger' : '');
        okBtn.textContent = type === 'alert' ? 'OK' : (danger ? 'Delete' : 'Confirm');
        okBtn.onclick = () => {
          overlay.remove();
          if (type === 'prompt') resolve(inputEl.value);
          else resolve(true);
        };
        actions.appendChild(okBtn);
        modal.appendChild(actions);

        overlay.appendChild(modal);
        document.body.appendChild(overlay);

        // Focus input or OK button
        if (inputEl) {
          inputEl.focus();
          inputEl.select();
          inputEl.addEventListener('keydown', e => {
            if (e.key === 'Enter') okBtn.click();
            if (e.key === 'Escape') cancelBtn?.click();
          });
        } else {
          okBtn.focus();
        }

        // Escape key closes
        overlay.addEventListener('keydown', e => {
          if (e.key === 'Escape') {
            overlay.remove();
            resolve(type === 'prompt' ? null : false);
          }
        });

        // Click backdrop to cancel
        overlay.addEventListener('mousedown', e => {
          if (e.target === overlay) {
            overlay.remove();
            resolve(type === 'prompt' ? null : false);
          }
        });
      });
    }

    // Drop-in async replacements for native dialogs
    window.sableConfirm = (msg, opts = {}) => chatDialog({ message: msg, type: 'confirm', ...opts });
    window.sablePrompt = (msg, def = '') => chatDialog({ message: msg, type: 'prompt', defaultValue: def });
    window.sableAlert = (msg, opts = {}) => chatDialog({ message: msg, type: 'alert', ...opts });


    // mobile browsers throttle setTimeout in background tabs —
    // dismiss any stale toasts when the tab regains focus
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) return;
      toastEl.querySelectorAll(".toast").forEach(el => {
        clearTimeout(el._timer);
        el._timer = setTimeout(() => dismissToast(el), 800);
      });
    });

    function saveActiveChat() {
      try {
        if (activeChatId) localStorage.setItem(ACTIVE_CHAT_KEY, activeChatId);
        else localStorage.removeItem(ACTIVE_CHAT_KEY);

        if (parentId) localStorage.setItem(PARENT_KEY, parentId);
        else localStorage.removeItem(PARENT_KEY);
      } catch (err) {
        console.warn("Could not persist active chat:", err);
      }
    }

    /* ---------- Multi-tab infrastructure ---------- */

    function createTabPane(chatId) {
      // Remove the wrapper-level "Start a chat" placeholder once real panes exist
      const wrapperEmpty = chatEl.querySelector(":scope > .empty");
      if (wrapperEmpty) wrapperEmpty.remove();

      const pane = document.createElement("div");
      pane.className = "tab-pane";
      pane.dataset.chatId = chatId;
      pane.innerHTML = `<div class="empty"><h2>New conversation</h2><p>Send the first message.</p></div>`;
      chatEl.appendChild(pane);
      return pane;
    }

    function ensurePane(chatId) {
      if (openTabs.has(chatId)) return openTabs.get(chatId).pane;
      const pane = createTabPane(chatId);
      const meta = chatList.find(c => c.id === chatId);
      openTabs.set(chatId, { pane, title: meta?.title || "New chat" });
      return pane;
    }
    window._sableEnsurePane = ensurePane;
    window._sableAddMessage = addMessage;
    window._sableScrollBottom = scrollBottom;
    // Sandboxed <render_html> iframes are separate documents: their wheel /
    // touch events never bubble to this pane, so _atBottom stayed true and
    // every streamed chunk yanked the view back down while the user was
    // reading up. The iframe bridge forwards the raw delta here.
    window._sableNotifyWheel = function (deltaY) {
      if (!activePane) return;
      // The iframe swallowed the wheel (its own document ate it), so the pane
      // never scrolled on its own. Apply the delta here, then re-derive intent
      // from the resulting position.
      activePane.scrollTop += deltaY;
      var gap = activePane.scrollHeight - activePane.scrollTop - activePane.clientHeight;
      _atBottom = gap < 30;
    };

    /* ---------- Pane loading overlay ---------- */
    function showPaneLoading(pane) {
      if (!pane || pane.querySelector(".pane-loading")) return;
      const overlay = document.createElement("div");
      overlay.className = "pane-loading";
      overlay.innerHTML = '<div class="spinner"></div>';
      pane.appendChild(overlay);
    }
    function hidePaneLoading(pane) {
      if (!pane) return;
      const overlay = pane.querySelector(".pane-loading");
      if (overlay) overlay.remove();
    }

    function switchToTab(chatId) {
      const pane = ensurePane(chatId);
      // Hide all panes, show target
      for (const [, tab] of openTabs) {
        tab.pane.classList.remove("active");
      }
      pane.classList.add("active");
      activePane = pane;
      activeChatId = chatId;
      // Re-hydrate any permission/CWD prompt still pending for THIS chat.
      // The banner is a single global element, so switching tabs must clear
      // it and then re-ask the server what this chat is still waiting on.
      document.getElementById("approvalBanner")?.classList.add("hidden");
      if (typeof window._sableRefreshPendingBanner === "function") {
        window._sableRefreshPendingBanner(chatId);
      }
      _bindScrollListener(pane);
      updateSendBtn();
      renderTabBar();
      if (typeof window.updateCompactTitle === "function") {
        const tab = openTabs.get(chatId);
        window.updateCompactTitle(tab?.title || "New chat");
      }
    }

    function closeTab(chatId) {
      const tab = openTabs.get(chatId);
      if (!tab) return;
      tab.pane.remove();
      openTabs.delete(chatId);

      // If we closed the active tab, focus another
      if (activeChatId === chatId) {
        const remaining = [...openTabs.keys()];
        if (remaining.length > 0) {
          selectChat(remaining[remaining.length - 1]);
        } else {
          activeChatId = null;
          activePane = null;
          parentId = null;
          saveActiveChat();
          chatEl.innerHTML = `<div class="empty"><h2>Start a chat</h2><p>Create a new chat and talk to Sable.</p></div>`;
        }
      }
      renderTabBar();
    }

    function renderTabBar() { /* no-op: switching via sidebar */ }

    /* ---------- end multi-tab ---------- */

    // ── Auto-scroll ──
    // ponytail: ceiling = synchronous user-intent detection. Upgrade path: add smooth scroll animation if needed.
    let _atBottom = true;
    let _scrollForChat = null;

    const _scrollBoundPanes = new WeakSet();
    function _bindScrollListener(pane) {
      if (!pane || _scrollBoundPanes.has(pane)) return;
      _scrollBoundPanes.add(pane);

      // Synchronous intent detection — must set _atBottom BEFORE any
      // scrollBottom() call that arrives in the same event loop tick.
      // DOM mutations / programmatic scrollTop never fire these events.
      pane.addEventListener("wheel", (e) => {
        if (e.deltaY < 0) {
          _atBottom = false;
        } else {
          // Scrolling down: check if we've reached the bottom
          const gap = pane.scrollHeight - pane.scrollTop - pane.clientHeight;
          _atBottom = gap < 30;
        }
      }, { passive: true });

      let _lastTouchY = 0;
      pane.addEventListener("touchstart", (e) => {
        _lastTouchY = e.touches[0].clientY;
      }, { passive: true });
      pane.addEventListener("touchmove", (e) => {
        const dy = e.touches[0].clientY - _lastTouchY;
        _lastTouchY = e.touches[0].clientY;
        if (dy > 0) {
          _atBottom = false;   // finger moving down = content scrolls up
        } else {
          const gap = pane.scrollHeight - pane.scrollTop - pane.clientHeight;
          _atBottom = gap < 30;
        }
      }, { passive: true });

      // Scrollbar drag: track pointer on scrollbar track (right edge)
      let _draggingScrollbar = false;
      pane.addEventListener("pointerdown", (e) => {
        if (e.offsetX > pane.clientWidth) _draggingScrollbar = true;
      });
      document.addEventListener("pointerup", () => { _draggingScrollbar = false; });
      pane.addEventListener("scroll", () => {
        if (!_draggingScrollbar) return;
        const gap = pane.scrollHeight - pane.scrollTop - pane.clientHeight;
        _atBottom = gap < 30;
      }, { passive: true });
    }

    function resetScrollTracking() {
      _atBottom = true;
    }

    // ponytail: rAF throttle — coalesces dozens of per-line scroll calls during
    // fast tool output into one layout pass per frame. Upgrade to a time-based
    // throttle (e.g. 100ms interval) only if rAF still causes jank on low-end devices.
    let _scrollRafPending = false;

    function scrollBottom(force) {
      // During history load, all DOM is built off-screen in a temp container.
      // Scrolling is meaningless until the fragment is attached to the real pane.
      if (window._historyLoading) return;
      if (!activePane) return;
      if (_scrollForChat !== activeChatId) {
        _atBottom = true;
        _scrollForChat = activeChatId;
      }
      if (!force && !_atBottom) return;
      if (_scrollRafPending) return;
      _scrollRafPending = true;
      requestAnimationFrame(() => {
        _scrollRafPending = false;
        if (activePane) activePane.scrollTop = activePane.scrollHeight;
      });
    }

    // Synchronously pin to bottom when we're already there. A full markdown
    // re-render (innerHTML =) tears down every node, which destroys the scroll
    // anchor; the rAF-queued scrollBottom can then land a frame late, and that
    // one-frame gap is what reads as the screen "bouncing" during streaming.
    // Writing scrollTop in the same tick as the re-render closes the gap.
    function pinScrollIfAtBottom() {
      if (window._historyLoading) return;
      if (!activePane) return;
      if (_scrollForChat !== activeChatId) {
        _atBottom = true;
        _scrollForChat = activeChatId;
      }
      if (!_atBottom) return;
      activePane.scrollTop = activePane.scrollHeight;
    }

    function clearEmptyState() {
      if (!activePane) return;
      const empty = activePane.querySelector(".empty");
      if (empty) empty.remove();
    }

    function clearPaneEmptyState(pane) {
      if (!pane) return;
      const empty = pane.querySelector(".empty");
      if (empty) empty.remove();
    }

    function isStreaming(chatId) { return activeStreams.has(chatId ?? activeChatId); }

    function updateSendBtn() {
      // Always derive from the currently-viewed chat, never from a stale caller
      const streaming = activeStreams.has(activeChatId);
      sendBtn.classList.toggle("stop-mode", streaming);
      sendBtn.classList.remove("loading");
      // Queue-send button lives only while streaming — it is the "send anyway"
      // control that rides alongside the stop button.
      const qBtn = document.getElementById("queueSend");
      if (qBtn) qBtn.hidden = !streaming;
    }

    function _toggleStreamIndicator(chatId, streaming) {
      const row = chatsEl.querySelector(`.chat-row[data-chat-id="${CSS.escape(chatId)}"]`);
      if (row) {
        row.classList.toggle('streaming', streaming);
      }
    }

    // Tracks Qwen response_id per chat for stop API requests
    const activeResponseIds = new Map();
    window.getActiveResponseId = (chatId) => activeResponseIds.get(chatId || activeChatId) || null;
    window.setActiveResponseId = (chatId, id) => { if (id) activeResponseIds.set(chatId, id); };

    function startStream(chatId) {
      resetScrollTracking();
      const controller = new AbortController();
      activeStreams.set(chatId, controller);
      if (chatId === activeChatId) updateSendBtn();
      _toggleStreamIndicator(chatId, true);
      return controller;
    }

    function endStream(chatId) {
      activeResponseIds.delete(chatId);
      // Sub-chats (critique / agent) stream into their own id while the parent
      // chat stays active. The migration fallback below must NOT run for them,
      // or finishing a critique would tear down the MAIN chat's controller and
      // leave the sidebar pointing at a dead stream.
      const _isSubChat = chatId.includes("-critique-") || chatId.includes("-agent-");
      if (activeStreams.has(chatId)) {
        activeStreams.delete(chatId);
      } else if (!_isSubChat && activeChatId !== chatId && activeStreams.has(activeChatId)) {
        // Session recovery migrated the stream to a new ID mid-flight.
        // The stale chatId is already gone; clean up the migrated entry.
        activeStreams.delete(activeChatId);
      }
      // Always refresh — activeChatId may differ from streamChatId if a meta
      // event updated it during streaming.
      updateSendBtn();
      _toggleStreamIndicator(chatId, false);
      // Also clear indicator on migrated ID if different — but never touch the
      // parent's indicator on behalf of a sub-chat.
      if (!_isSubChat && activeChatId !== chatId) _toggleStreamIndicator(activeChatId, false);
      if (_isSubChat) return;  // no sidebar bump / context ring for ephemeral streams
      // Bump updated_at so the chat jumps to top of sidebar — only re-render if it moved
      const _bumpId = (activeChatId !== chatId) ? activeChatId : chatId;
      const _meta = chatList.find(c => c.id === _bumpId);
      if (_meta) {
        // Check if already on top BEFORE bumping
        const _wasFirst = chatList.filter(c => !c.id.startsWith('browser-'))
          .sort((a, b) => (b.updated_at || b.created_at || '').localeCompare(a.updated_at || a.created_at || ''))[0];
        const _needsMove = !_wasFirst || _wasFirst.id !== _bumpId;
        _meta.updated_at = new Date().toISOString();
        if (_needsMove) renderChats();
      }
      // Refresh context ring after message completes (use activeChatId if migrated)
      const _ringId = (chatId !== activeChatId) ? activeChatId : chatId;
      fetch(`/api/chats/${_ringId}/messages?limit=1`)
        .then(r => r.json())
        .then(d => {
          const chars = d.context_chars || 0;
          contextCharsCache.set(_ringId, chars);
          if (_ringId === activeChatId) {
            window._statusContextChars = chars;
            updateStatusBarContext();
          }
        })
        .catch(() => {});
    }

    function setCreating(val) {
      creating = val;
      if (newChatBtn) {
        newChatBtn.disabled = val;
        newChatBtn.classList.toggle("loading", val);
      }
      const floatBtn = document.getElementById("newChatFloat");
      if (floatBtn) {
        floatBtn.disabled = val;
        floatBtn.classList.toggle("loading", val);
      }
      modelSelectEl.disabled = val;
      if (thinkingSwitcherEl) thinkingSwitcherEl.style.display = val ? "none" : "";
    }

    function autoResize() {
      inputEl.style.height = "auto";
      inputEl.style.height = Math.min(inputEl.scrollHeight, 200) + "px";
    }

    inputEl.addEventListener("input", autoResize);

    // Shared skill-card DOM builders — used both for live streaming (inside
    // addBotStreaming) and for replaying persisted skill_events when a chat's
    // history is reloaded (addHistoryMessage). Keeping one implementation
    // means history looks identical to what was shown live.
    // Per-tool timeout budget (seconds) for the live card timer.
    // The model can override per call via a `timeout` attr (execute_command,
    // computer_use, spawn_agent, ...); otherwise this default table is used.
    // 0 means "no known budget" — the timer still ticks, just without a cap.
    const _TOOL_TIMEOUT_DEFAULTS = {
      execute_command: 15,
      computer_use: 30,
      spawn_agent: 300,
      generate_image: 180,
      web_search: 15,
      web_fetch: 30,
      run_simulacra: 30,
      view_file: 10,
      grep: 15,
      glob: 10,
    };
    function _toolTimeoutSec(name, attrs) {
      const a = attrs || {};
      const raw = a.timeout != null ? parseInt(a.timeout, 10) : NaN;
      if (!isNaN(raw) && raw > 0) return raw;
      return _TOOL_TIMEOUT_DEFAULTS[name] || 0;
    }

    function createSkillCard(evt) {
      const card = document.createElement("div");
      card.className = "skill-card";
      const name = evt.name || "skill";
      // Always prefer structured attribute extraction over raw content JSON.
      // Backend serializes full params as content for tools without a body/command
      // param, which causes grep/view_file cards to dump raw JSON.
      let initial = "";
      if (evt.data) {
        const d = evt.data.attrs || evt.data;
        const parts = [];
        if (name === "spawn_agent") {
          if (d.task) parts.push(`task: ${d.task.slice(0, 80)}`);
          if (d.model) parts.push(`model: ${d.model}`);
          if (d.collect === "true") parts.push("collect: true");
          if (d.timeout) parts.push(`timeout: ${d.timeout}s`);
        } else if (name === "grep") {
          if (d.pattern) parts.push(`/${d.pattern}/`);
          if (d.path) parts.push(d.path);
          if (d.glob) parts.push(`glob: ${d.glob}`);
          if (d.exclude) parts.push(`exclude: ${d.exclude}`);
          if (d.ignore_case === "true" || d.ignore_case === true) parts.push("-i");
          if (d.max_results) parts.push(`max: ${d.max_results}`);
        } else if (name === "glob") {
          if (d.pattern) parts.push(d.pattern);
          if (d.path) parts.push(d.path);
        } else if (name === "execute_command") {
          if (d.command) parts.push(d.command);
          if (d.bg === "true" || d.bg === true) parts.push("(bg)");
        } else if (name === "web_search") {
          if (d.query) parts.push(d.query);
          if (d.max_results) parts.push(`max: ${d.max_results}`);
        } else if (name === "web_fetch") {
          if (d.urls) parts.push(Array.isArray(d.urls) ? d.urls.join(", ") : String(d.urls));
        } else if (name === "ask_user") {
          if (d.question) parts.push(d.question.slice(0, 80));
        } else if (name === "chat_title") {
          if (d.title) parts.push(d.title);
        } else if (name === "edit_file" || name === "create_file") {
          // Show actual content (SEARCH/REPLACE diff or file body) not just path
          if (evt.data.content) {
            initial = String(evt.data.content);
          } else {
            if (d.path) parts.push(d.path);
          }
        } else {
          // File-based tools: view_file, insert_file, get_file
          if (d.path) parts.push(d.path);
          if (d.start != null) parts.push(`L${d.start}`);
          if (d.end != null) parts.push(`–${d.end}`);
          if (d.at_line != null) parts.push(`@L${d.at_line}`);
          if (d.after_str) parts.push(`after "${d.after_str.slice(0, 40)}"`);
          if (d.full === "true" || d.full === true) parts.push("(full)");
        }
        if (parts.length) initial = parts.join("\n");
      }
      // Fallback: use raw content only if no structured attrs were extracted
      // ponytail: ceiling = per-tool formatting above; upgrade path = registry-driven schema
      if (!initial && evt.data && evt.data.content) {
        initial = String(evt.data.content);
      }

      const header = document.createElement("div");
      header.className = "skill-header";
      header.onclick = () => card.classList.toggle("collapsed");

      const left = document.createElement("div");
      left.className = "skill-header-left";

      const arrow = document.createElement("span");
      arrow.className = "skill-arrow";
      arrow.innerHTML = '<i data-lucide="chevron-down"></i>';

      const nameEl = document.createElement("span");
      nameEl.className = "skill-name";
      // Specialized header for spawn_agent
      const _roleIcons = { researcher: "🔬", coder: "💻", reviewer: "👁️", writer: "✍️" };
      if (name === "spawn_agent") {
        const _r = (evt.data && (evt.data.attrs || evt.data).role) || "agent";
        nameEl.innerHTML = `${lucideIcon(_roleIcons[_r] || "🤖")} spawn · ${_r}`;
      } else {
        nameEl.innerHTML = lucideIcon("⚡") + " " + escHtml(name);
      }

      left.appendChild(arrow);
      left.appendChild(nameEl);

      const statusEl = document.createElement("span");
      statusEl.className = "skill-status";
      statusEl.textContent = "running…";

      // ── Live elapsed/ timeout timer ──────────────────────────────────
      // Ticks `elapsed / timeout` while the tool runs so long calls show real
      // progress instead of a dead "running…". finishSkillCard freezes the
      // final value and colours it green/red. Auto-stops after 30min as a
      // leak guard for cards whose skill_end never arrives (page switch etc.).
      const _tAttrs = (evt.data && (evt.data.attrs || evt.data)) || {};
      const _timeoutSec = _toolTimeoutSec(name, _tAttrs);
      card.dataset.timeoutSec = String(_timeoutSec);
      const _t0 = performance.now();
      const _tick = () => {
        const secs = (performance.now() - _t0) / 1000;
        if (secs > 1800) { clearInterval(card._tickId); card._tickId = null; return; }
        if (_timeoutSec > 0) {
          statusEl.textContent = `${secs.toFixed(1)}s / ${_timeoutSec}s`;
          statusEl.style.color = secs > _timeoutSec ? "var(--danger)" : "";
        } else {
          statusEl.textContent = `${secs.toFixed(1)}s`;
        }
      };
      _tick();
      card._tickId = setInterval(_tick, 100);

      const toggleBtn = document.createElement("button");
      toggleBtn.className = "skill-toggle-btn";
      toggleBtn.textContent = "Output";
      toggleBtn.onclick = (e) => {
        e.stopPropagation();
        const showing = card.classList.toggle("show-output");
        toggleBtn.textContent = showing ? "Command" : "Output";
      };

      const right = document.createElement("div");
      right.className = "skill-header-right";
      right.style.display = "flex";
      right.style.alignItems = "center";
      right.style.gap = "8px";
      right.appendChild(statusEl);
      right.appendChild(toggleBtn);

      // Preview button for previewable file types
      const _pvAttrs = evt.data && (evt.data.attrs || evt.data) || {};
      const _pvPath = _pvAttrs.path || _pvAttrs.filename || "";
      const _pvExt = _pvPath.split(".").pop().toLowerCase();
      if (/^(html|htm|svg|threejs)$/i.test(_pvExt) && /^(create_file|edit_file|save_svg|create_svg)$/.test(name)) {
        const pvBtn = document.createElement("button");
        pvBtn.className = "skill-toggle-btn skill-preview-btn";
        pvBtn.textContent = "Preview";
        pvBtn.onclick = (e) => {
          e.stopPropagation();
          fetch('/api/filesystem/read?path=' + encodeURIComponent(_pvPath))
            .then(r => r.json())
            .then(data => {
              if (data.error) { showToast(data.error, 'error'); return; }
              let html;
              if (/^svg$/i.test(_pvExt)) {
                html = '<!DOCTYPE html><html><head><meta charset="utf-8"><style>*{margin:0;padding:0}body{display:flex;justify-content:center;align-items:center;min-height:100vh;background:#1a1a2e}svg{max-width:95vw;max-height:95vh}</style></head><body>' + data.content + '</body></html>';
              } else {
                html = data.content;
              }

              // Inline assets resolve through serve-dir, same as the IDE run button.
              if (/\.html?$/i.test(_pvPath)) {
                const _dir = _pvPath.slice(0, _pvPath.lastIndexOf("/"));
                const _base = '<base href="/api/filesystem/serve-dir/?base=' + encodeURIComponent(_dir) + '&">';
                html = /<head[^>]*>/i.test(html)
                  ? html.replace(/<head([^>]*)>/i, '<head$1>' + _base)
                  : _base + html;
              }

              // Dock it beside the chat — never a new browser tab.
              if (window.SablePreview) {
                window.SablePreview.open({
                  title: _pvPath.split("/").pop() || "Preview",
                  html: html,
                  source: data.content,
                  path: _pvPath,
                });
              } else {
                const blob = new Blob([html], { type: 'text/html' });
                window.open(URL.createObjectURL(blob), '_blank');
              }
            })
            .catch(() => showToast('Failed to read file for preview', 'error'));
        };
        right.appendChild(pvBtn);
      }

      header.appendChild(left);
      header.appendChild(right);

      const cmdPre = document.createElement("pre");
      cmdPre.className = "skill-command";
      if (initial) cmdPre.textContent = initial + "\n";

      const outPre = document.createElement("pre");
      outPre.className = "skill-output";

      card.appendChild(header);
      card.appendChild(cmdPre);
      card.appendChild(outPre);

      // Image placeholder for generate_image — replaced on skill_end
      if (name === "generate_image") {
        const imgPlaceholder = document.createElement("div");
        imgPlaceholder.className = "skill-image-placeholder";
        imgPlaceholder.innerHTML = '<span class="sip-spinner"></span><span class="sip-text">Generating image…</span>';
        card.appendChild(imgPlaceholder);
      }

      return card;
    }

    // ponytail: native DOM cap — no virtualization needed until a single tool
    // call legitimately needs >50K chars visible in one card.
    const SKILL_OUTPUT_CAP = 50_000;
    const TRUNCATION_NOTE = "\n[… output truncated for display …]";

    function appendSkillCardOutput(card, text) {
      const pre = card.querySelector(".skill-output");
      if (!pre || !text) return;
      // Already marked as truncated — silently drop further appends to avoid
      // unbounded string concatenation that freezes the main thread.
      if (pre.dataset.truncated === "1") return;
      if (pre.textContent.length >= SKILL_OUTPUT_CAP) {
        pre.textContent += TRUNCATION_NOTE;
        pre.dataset.truncated = "1";
        return;
      }
      pre.textContent += text;
      if (pre.textContent.length > SKILL_OUTPUT_CAP) {
        pre.textContent = pre.textContent.slice(0, SKILL_OUTPUT_CAP) + TRUNCATION_NOTE;
        pre.dataset.truncated = "1";
      }
    }

    // Expose skill card builders for agent panel history replay
    window.createSkillCard = createSkillCard;
    window.appendSkillCardOutput = appendSkillCardOutput;
    window.finishSkillCard = finishSkillCard;

    function finishSkillCard(card, evt) {
      const status = card.querySelector(".skill-status");
      const pre = card.querySelector(".skill-output");
      // Stop the live timer and freeze the final value. Shows the timeout
      // budget when known (`done · 2.4s / 15s`), plain ms otherwise.
      if (card._tickId) { clearInterval(card._tickId); card._tickId = null; }
      const _toSec = parseFloat(card.dataset.timeoutSec || "0") || 0;
      if (_toSec > 0) {
        const _elapsedS = ((evt.duration_ms ?? 0) / 1000).toFixed(1);
        status.textContent = (evt.ok ? "done · " : "failed · ") + `${_elapsedS}s / ${_toSec}s`;
      } else {
        status.textContent = (evt.ok ? "done · " : "failed · ") + (evt.duration_ms ?? 0) + "ms";
      }
      status.style.color = evt.ok ? "var(--ok)" : "var(--danger)";
      // Show agent_id in the card name after spawn completes
      const result = evt.result || {};
      if (evt.name === "spawn_agent" && result.agent_id) {
        const nameEl = card.querySelector(".skill-name");
        if (nameEl) nameEl.textContent += `  #${result.agent_id.slice(0, 8)}`;
      }
      if (evt.error) pre.textContent += `\n[error] ${evt.error}`;
      if (!evt.ok) pre.classList.add("error");

      // Handle generate_image placeholder → real image(s) or error
      const placeholder = card.querySelector(".skill-image-placeholder");
      if (placeholder) {
        if (evt.ok && result.url && result.mime && String(result.mime).startsWith("image/")) {
          // Multi-image gallery or single image
          const imagesArr = result.images || [{ url: result.url, mime: result.mime }];
          if (imagesArr.length > 1) {
            const gallery = document.createElement("div");
            gallery.className = "skill-image-gallery";
            const _galUrls = imagesArr.map((x) => x.url);
            imagesArr.forEach((imgData, gi) => {
              const img = document.createElement("img");
              img.src = imgData.url;
              img.className = "skill-image";
              img.alt = "Generated image";
              img.addEventListener("click", () => openLightbox(_galUrls, gi));
              gallery.appendChild(img);
            });
            placeholder.replaceWith(gallery);
          } else {
            const img = document.createElement("img");
            img.src = result.url;
            img.className = "skill-image";
            img.alt = "Generated image";
            img.addEventListener("click", () => openLightbox(result.url));
            placeholder.replaceWith(img);
          }
        } else {
          placeholder.classList.add("sip-failed");
          const failText = placeholder.querySelector(".sip-text");
          if (failText) failText.textContent = "Generation failed";
          const spinner = placeholder.querySelector(".sip-spinner");
          if (spinner) spinner.style.display = "none";
        }
      } else if (result.url && result.mime && String(result.mime).startsWith("image/")) {
        // Non-generate_image skills that return images (backward compat)
        const img = document.createElement("img");
        img.src = result.url;
        img.className = "skill-image";
        card.appendChild(img);
      }
    }

    function addMessage(kind, text, images, pane = activePane) {
      clearPaneEmptyState(pane);
      const div = document.createElement("div");
      div.className = `msg ${kind}`;
      if (kind === "user") {
        const now = new Date();
        const ts = `[${now.getFullYear()}-${String(now.getMonth()+1).padStart(2,'0')}-${String(now.getDate()).padStart(2,'0')} ${String(now.getHours()).padStart(2,'0')}:${String(now.getMinutes()).padStart(2,'0')}:${String(now.getSeconds()).padStart(2,'0')}]`;
        const tsEl = document.createElement("div");
        tsEl.className = "msg-timestamp";
        tsEl.textContent = ts;
        div.appendChild(tsEl);
        if (text) {
          const textEl = document.createElement("div");
          textEl.className = "user-text";
          textEl.textContent = text;
          div.appendChild(textEl);
          if (text.length > 300) {
            div.classList.add("collapsed");
            const expandBtn = document.createElement("button");
            expandBtn.className = "user-expand-btn";
            expandBtn.textContent = "Show more";
            expandBtn.addEventListener("click", () => {
              const isCollapsed = div.classList.toggle("collapsed");
              expandBtn.textContent = isCollapsed ? "Show more" : "Show less";
            });
            div.appendChild(expandBtn);
          }
        }
        if (Array.isArray(images) && images.length) {
          const imgWrap = document.createElement("div");
          imgWrap.className = "user-images";
          images.forEach((src, ui) => {
            const img = document.createElement("img");
            img.src = src;
            img.addEventListener("click", () => openLightbox(images, ui));
            imgWrap.appendChild(img);
          });
          div.appendChild(imgWrap);
        }
        // Copy toolbar for user messages
        if (text) {
          // Copy + read-aloud are shared across every message kind — see
          // msg-actions.js. Only the fork button below is chat.js-specific.
          const toolbar = window.MsgActions.ensureToolbar(div);
          window.MsgActions.addCopy(toolbar, div, "user");
          window.MsgActions.addTTS(toolbar, div, "user");

          // Fork button — starts disabled until SSE delivers the DB message ID
          const forkBtn = document.createElement("button");
          forkBtn.innerHTML = '<i data-lucide="git-branch"></i>';
          forkBtn.title = "Fork from here";
          forkBtn.disabled = true;
          forkBtn.classList.add("fork-pending");
          forkBtn.addEventListener("click", async () => {
            forkBtn.disabled = true;
            forkBtn.innerHTML = '<i data-lucide="loader-circle" class="spin"></i>';
            activateLucideIcons(forkBtn);
            try {
              const msgDiv = forkBtn.closest(".msg");
              const dbMsgId = msgDiv?.dataset?.msgId;
              if (!dbMsgId) return; // shouldn't happen — button only enabled when ID is set
              const res = await fetch("/api/chat/fork", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ chat_id: activeChatId, message_id: dbMsgId }),
              });
              const data = await res.json();
              if (!res.ok || data.error) {
                showToast(data.error || data.detail || "Fork failed", "error");
                return;
              }
              showToast(`Forked ${data.message_count} messages`, "success");
              if (typeof window._sableLoadChats === "function") await window._sableLoadChats();
              if (typeof window._sableSelectChat === "function") await window._sableSelectChat(data.chat_id);
              // Put the fork message content into the input box
              if (data.fork_message) {
                const mainInput = document.getElementById("input");
                const compactInput = document.getElementById("chatCompactInput");
                const target = (mainInput && mainInput.offsetParent !== null) ? mainInput : compactInput;
                if (target) {
                  target.value = data.fork_message;
                  target.focus();
                  target.dispatchEvent(new Event("input", { bubbles: true }));
                }
              }
            } catch (err) {
              showToast("Fork failed: " + err.message, "error");
            } finally {
              forkBtn.disabled = false;
              forkBtn.innerHTML = '<i data-lucide="git-branch"></i>';
              activateLucideIcons(forkBtn);
            }
          });
          toolbar.appendChild(forkBtn);

          div.appendChild(toolbar);
          // ponytail: Skip per-element icon scan during bulk history render
          if (!window._historyLoading) activateLucideIcons(toolbar);
        }
      } else {
        const content = document.createElement("div");
        content.className = "md-content";
        // Stash raw markdown so the copy button can grab it instead of rendered text
        content.dataset.raw = text || "";
        content.innerHTML = renderMarkdown(text);
        // ponytail: Skip heavy renders during bulk history load.
        // loadMessages does a single pane-wide pass after fragment attach.
        if (!window._historyLoading) {
          renderMermaidDiagrams(content);
          renderMathJax(content);
          activateLucideIcons(content);
        }
        div.appendChild(content);
      }
      pane.appendChild(div);
      scrollBottom(true);
      return div;
    }

    // ---------- memory-used chip + popup ----------
    function createMemoryChip(memories) {
      const chip = document.createElement("button");
      chip.className = "memory-chip";
      chip.innerHTML = `${lucideIcon("🧠")} Memory Used (${memories.length})`;
      chip.title = "Show the memories injected into this message";
      chip.addEventListener("click", () => openMemoryPopup(memories));
      return chip;
    }

    function attachMemoryChip(userMsgDiv, memories) {
      if (!userMsgDiv || !Array.isArray(memories) || !memories.length) return;
      if (userMsgDiv.querySelector(".memory-chip")) return;  // no duplicates
      const chip = createMemoryChip(memories);
      // The toolbar is pinned last via `order:9999`, so DOM position no longer
      // controls layout — just append and give the chip an earlier order.
      chip.style.order = "1";
      userMsgDiv.appendChild(chip);
    }

    function openMemoryPopup(memories) {
      document.querySelectorAll(".memory-overlay").forEach((el) => el.remove());
      const overlay = document.createElement("div");
      overlay.className = "memory-overlay";

      const panel = document.createElement("div");
      panel.className = "memory-panel";

      const header = document.createElement("div");
      header.className = "memory-header";
      const h = document.createElement("h2");
      h.innerHTML = `${lucideIcon("🧠")} Memory Used (${memories.length})`;
      const closeBtn = document.createElement("button");
      closeBtn.className = "memory-close";
      closeBtn.textContent = "✕";
      closeBtn.addEventListener("click", () => overlay.remove());
      header.appendChild(h);
      header.appendChild(closeBtn);
      panel.appendChild(header);

      const body = document.createElement("div");
      body.className = "memory-body";
      for (const m of memories) {
        const item = document.createElement("div");
        item.className = "memory-item";

        const top = document.createElement("div");
        top.className = "memory-item-top";
        const key = document.createElement("span");
        key.className = "memory-item-key";
        key.textContent = m.key || "(untitled)";
        const cat = document.createElement("span");
        cat.className = "memory-cat memory-cat-" + (m.category || "general");
        cat.textContent = m.category || "general";
        const score = document.createElement("span");
        score.className = "memory-score";
        score.textContent = m.score != null ? `score ${m.score}` : "";
        top.appendChild(key);
        top.appendChild(cat);
        top.appendChild(score);

        const val = document.createElement("div");
        val.className = "memory-item-val";
        val.textContent = m.value || "";

        item.appendChild(top);
        item.appendChild(val);
        body.appendChild(item);
      }
      panel.appendChild(body);
      overlay.appendChild(panel);

      overlay.addEventListener("click", (e) => { if (e.target === overlay) overlay.remove(); });
      document.body.appendChild(overlay);

      const onEsc = (e) => {
        if (e.key === "Escape") { overlay.remove(); document.removeEventListener("keydown", onEsc); }
      };
      document.addEventListener("keydown", onEsc);
    }

