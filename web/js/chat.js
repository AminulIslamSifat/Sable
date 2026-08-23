
    let toastTimer = null;
    function showToast(msg, type = "info") {
      toastEl.textContent = msg;
      toastEl.classList.remove("success", "info", "error");
      toastEl.classList.add(type, "show");
      clearTimeout(toastTimer);
      toastTimer = setTimeout(() => toastEl.classList.remove("show"), 3500);
    }
    window.showToast = showToast; // expose for filesystem.js


    /* ---------- Sable Dialog — themed replacement for confirm/prompt/alert ---------- */
    function sableDialog({ title, message, type = 'confirm', defaultValue = '', danger = false }) {
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
    window.sableConfirm = (msg, opts = {}) => sableDialog({ message: msg, type: 'confirm', ...opts });
    window.sablePrompt = (msg, def = '') => sableDialog({ message: msg, type: 'prompt', defaultValue: def });
    window.sableAlert = (msg, opts = {}) => sableDialog({ message: msg, type: 'alert', ...opts });


    // mobile: tap to dismiss toast immediately
    toastEl.addEventListener("click", () => {
      clearTimeout(toastTimer);
      toastEl.classList.remove("show");
    });

    // mobile browsers throttle setTimeout in background tabs —
    // dismiss any stale toast when the tab regains focus
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden && toastEl.classList.contains("show")) {
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => toastEl.classList.remove("show"), 800);
      }
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
      document.getElementById("approvalBanner")?.classList.add("hidden");
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

    // ── Smart auto-scroll: event-driven flag + rAF batching ──
    // Pattern from Smashing Magazine / shadcn: track user intent via scroll
    // event, batch writes with requestAnimationFrame, reset on new stream.
    let _userScrolled = false;
    let _scrollRafPending = false;
    let _scrollForChat = null;

    // Attach scroll listener whenever activePane changes (idempotent)
    const _scrollBoundPanes = new WeakSet();
    function _bindScrollListener(pane) {
      if (!pane || _scrollBoundPanes.has(pane)) return;
      _scrollBoundPanes.add(pane);
      pane.addEventListener("scroll", () => {
        const gap = pane.scrollHeight - pane.scrollTop - pane.clientHeight;
        _userScrolled = gap > 60;
      }, { passive: true });
    }

    // Call at stream start so previous scroll-up doesn't block new content
    function resetScrollTracking() {
      _userScrolled = false;
      _scrollRafPending = false;
    }

    function scrollBottom(force) {
      if (!activePane) return;
      if (_scrollForChat !== activeChatId) { _userScrolled = false; _scrollRafPending = false; }
      _scrollForChat = activeChatId;
      if (!force && _userScrolled) return;
      if (_scrollRafPending) return;
      _scrollRafPending = true;
      requestAnimationFrame(() => {
        _scrollRafPending = false;
        if (!activePane) return;
        // Re-check position at paint time — user may have scrolled up between
        // the scrollBottom() call and this rAF firing (race during fast streaming)
        if (!force) {
          const gap = activePane.scrollHeight - activePane.scrollTop - activePane.clientHeight;
          if (gap > 80) { _userScrolled = true; return; }
        }
        activePane.scrollTop = activePane.scrollHeight;
      });
    }

    function clearEmptyState() {
      if (!activePane) return;
      const empty = activePane.querySelector(".empty");
      if (empty) empty.remove();
    }

    function isStreaming(chatId) { return activeStreams.has(chatId ?? activeChatId); }

    function updateSendBtn() {
      // Always derive from the currently-viewed chat, never from a stale caller
      const streaming = activeStreams.has(activeChatId);
      sendBtn.classList.toggle("stop-mode", streaming);
      sendBtn.classList.remove("loading");
    }

    function _toggleStreamIndicator(chatId, streaming) {
      const row = chatsEl.querySelector(`.chat-row[data-chat-id="${CSS.escape(chatId)}"]`);
      if (row) {
        row.classList.toggle('streaming', streaming);
      }
    }

    function startStream(chatId) {
      resetScrollTracking();
      const controller = new AbortController();
      activeStreams.set(chatId, controller);
      if (chatId === activeChatId) updateSendBtn();
      _toggleStreamIndicator(chatId, true);
      return controller;
    }

    function endStream(chatId) {
      if (activeStreams.has(chatId)) {
        activeStreams.delete(chatId);
      } else if (activeChatId !== chatId && activeStreams.has(activeChatId)) {
        // Session recovery migrated the stream to a new ID mid-flight.
        // The stale chatId is already gone; clean up the migrated entry.
        activeStreams.delete(activeChatId);
      }
      // Always refresh — activeChatId may differ from streamChatId if a meta
      // event updated it during streaming.
      updateSendBtn();
      _toggleStreamIndicator(chatId, false);
      // Also clear indicator on migrated ID if different
      if (activeChatId !== chatId) _toggleStreamIndicator(activeChatId, false);
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
    function createSkillCard(evt) {
      const card = document.createElement("div");
      card.className = "skill-card";
      const name = evt.name || "skill";
      let initial = evt.data && evt.data.content ? String(evt.data.content) : "";
      // For tags without content (view_file, insert_file, etc.), show the
      // key attributes so the card isn't just a blank "⚡ view_file" box.
      // Backend nests attrs under data.attrs — check both levels.
      if (!initial && evt.data) {
        const d = evt.data.attrs || evt.data;
        const parts = [];
        if (name === "spawn_agent") {
          if (d.task) parts.push(`task: ${d.task.slice(0, 80)}`);
          if (d.model) parts.push(`model: ${d.model}`);
          if (d.collect === "true") parts.push("collect: true");
          if (d.timeout) parts.push(`timeout: ${d.timeout}s`);
        } else {
          if (d.path) parts.push(d.path);
          if (d.start != null) parts.push(`L${d.start}`);
          if (d.end != null) parts.push(`–${d.end}`);
          if (d.at_line != null) parts.push(`@L${d.at_line}`);
          if (d.after_str) parts.push(`after "${d.after_str.slice(0, 40)}"`);
          if (d.full === "true" || d.full === true) parts.push("(full)");
        }
        if (parts.length) initial = parts.join("\n");
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
              const blob = new Blob([html], { type: 'text/html' });
              window.open(URL.createObjectURL(blob), '_blank');
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

    function appendSkillCardOutput(card, text) {
      card.querySelector(".skill-output").textContent += text || "";
    }

    function finishSkillCard(card, evt) {
      const status = card.querySelector(".skill-status");
      const pre = card.querySelector(".skill-output");
      status.textContent = (evt.ok ? "done · " : "failed · ") + (evt.duration_ms ?? 0) + "ms";
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
            for (const imgData of imagesArr) {
              const img = document.createElement("img");
              img.src = imgData.url;
              img.className = "skill-image";
              img.alt = "Generated image";
              img.addEventListener("click", () => window.open(imgData.url, "_blank"));
              gallery.appendChild(img);
            }
            placeholder.replaceWith(gallery);
          } else {
            const img = document.createElement("img");
            img.src = result.url;
            img.className = "skill-image";
            img.alt = "Generated image";
            img.addEventListener("click", () => window.open(result.url, "_blank"));
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

    function addMessage(kind, text, images) {
      clearEmptyState();
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
          for (const src of images) {
            const img = document.createElement("img");
            img.src = src;
            img.addEventListener("click", () => window.open(src, "_blank"));
            imgWrap.appendChild(img);
          }
          div.appendChild(imgWrap);
        }
        // Copy toolbar for user messages
        if (text) {
          const toolbar = document.createElement("div");
          toolbar.className = "msg-toolbar";
          const copyBtn = document.createElement("button");
          copyBtn.innerHTML = '<i data-lucide="copy"></i>';
          copyBtn.title = "Copy";
          copyBtn.addEventListener("click", () => {
            // Read from DOM at click-time for reliability
            const userTextEl = div.querySelector(".user-text");
            const copyText = userTextEl ? userTextEl.textContent : text;
            const onSuccess = () => {
              copyBtn.innerHTML = '<i data-lucide="check"></i>';
              activateLucideIcons(copyBtn);
              setTimeout(() => { copyBtn.innerHTML = '<i data-lucide="copy"></i>'; activateLucideIcons(copyBtn); }, 1500);
            };
            if (navigator.clipboard && window.isSecureContext) {
              navigator.clipboard.writeText(copyText).then(onSuccess).catch(() => {
                // Fallback for non-secure contexts
                const ta = document.createElement("textarea");
                ta.value = copyText;
                ta.style.position = "fixed";
                ta.style.opacity = "0";
                document.body.appendChild(ta);
                ta.select();
                document.execCommand("copy");
                document.body.removeChild(ta);
                onSuccess();
              });
            } else {
              const ta = document.createElement("textarea");
              ta.value = copyText;
              ta.style.position = "fixed";
              ta.style.opacity = "0";
              document.body.appendChild(ta);
              ta.select();
              document.execCommand("copy");
              document.body.removeChild(ta);
              onSuccess();
            }
          });
          toolbar.appendChild(copyBtn);

          // TTS button for live user messages
          const ttsBtn = document.createElement("button");
          ttsBtn.innerHTML = '<i data-lucide="volume-2"></i>';
          ttsBtn.title = "Read aloud";
          ttsBtn.addEventListener("click", async () => {
            const now = Date.now();
            if (now - _ttsLastAction < TTS_DEBOUNCE_MS) return;
            if (_ttsActive) { stopGlobalTTS(); return; }
            _ttsLastAction = now;
            const userTextEl = div.querySelector(".user-text");
            const ttsText = userTextEl ? userTextEl.textContent : text;
            if (!ttsText) return;
            _ttsActive = true;
            const gen = ++_ttsGeneration;
            _activeTTS.gen = gen;
            const player = new TTSStreamPlayer((state) => {
              if (_ttsGeneration !== gen) return;
              if (state === "loading") {
                ttsBtn.innerHTML = '<i data-lucide="loader-circle"></i>';
                ttsBtn.title = "Loading...";
              } else if (state === "playing") {
                ttsBtn.innerHTML = '<i data-lucide="square"></i>';
                ttsBtn.title = "Stop";
                ttsBtn.classList.add("tts-playing");
              } else {
                ttsBtn.innerHTML = '<i data-lucide="volume-2"></i>';
                ttsBtn.title = "Read aloud";
                ttsBtn.classList.remove("tts-playing");
                if (!_ttsStopping) _ttsActive = false;
                _activeTTS.player = null;
                _activeTTS.btn = null;
                _activeTTS.gen = -1;
              }
              activateLucideIcons(ttsBtn);
            });
            _activeTTS.player = player;
            _activeTTS.btn = ttsBtn;
            player.play(ttsText);
          });
          toolbar.appendChild(ttsBtn);

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
          activateLucideIcons(toolbar);
        }
      } else {
        const content = document.createElement("div");
        content.className = "md-content";
        content.innerHTML = renderMarkdown(text);
        renderMermaidDiagrams(content);
        renderMathJax(content);
        activateLucideIcons(content);
        div.appendChild(content);
      }
      activePane.appendChild(div);
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
      const toolbar = userMsgDiv.querySelector(".msg-toolbar");
      if (toolbar) userMsgDiv.insertBefore(chip, toolbar);
      else userMsgDiv.appendChild(chip);
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

