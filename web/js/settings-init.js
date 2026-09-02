

    // --- Service control buttons ---
    const stopServiceBtn = document.getElementById('stopServiceBtn');
    const restartServiceBtn = document.getElementById('restartServiceBtn');
    if (stopServiceBtn) {
      stopServiceBtn.addEventListener('click', async () => {
        if (!await sableConfirm('Stop the Sable service? The UI will go offline.')) return;
        stopServiceBtn.textContent = 'Stopping…';
        try { await fetch('/api/settings/service/stop', { method: 'POST' }); } catch {}
        showToast('Service stopping — UI will go offline', 'info');
      });
    }
    if (restartServiceBtn) {
      restartServiceBtn.addEventListener('click', async () => {
        if (!await sableConfirm('Restart the Sable service? Brief downtime (~20s).')) return;
        restartServiceBtn.textContent = 'Restarting…';
        try { await fetch('/api/settings/service/restart', { method: 'POST' }); } catch {}
        showToast('Restarting — back in ~20s', 'info');
        setTimeout(() => { restartServiceBtn.textContent = '↻ Restart Service'; }, 25000);
      });
    }




    // --- Consolidation queue: messages sent while consolidation is pending get queued ---
    let _consolidationPromise = null;
    let _messageQueue = [];

    function consolidateMemory(chatId, model, useTimeout = false) {
      const cid = chatId || activeChatId;
      if (!cid) return Promise.resolve();
      const mode = scraperMode ? 'scraper' : 'api';
      showToast("🧠 Consolidating memory...", "info");

      const controller = new AbortController();
      const timeout = useTimeout ? setTimeout(() => controller.abort(), 30000) : null;

      _consolidationPromise = fetch("/api/memory/consolidate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ chat_id: cid, model: model || selectedModel, mode: mode }),
        signal: useTimeout ? controller.signal : undefined
      })
        .then(async (res) => {
          clearTimeout(timeout);
          if (!res.ok) {
            const text = await res.text().catch(() => "");
            showToast(`🧠 Consolidation failed (${res.status}): ${text.slice(0, 200)}`, "error");
            return;
          }
          const data = await res.json();
          if (data.status === "ok") {
            if (data.added > 0 || data.deleted > 0 || data.dedup_skipped > 0 || data.dedup_updated > 0) {
              const parts = [];
              if (data.added) parts.push(`${data.added} added`);
              if (data.deleted) parts.push(`${data.deleted} deleted`);
              if (data.dedup_skipped) parts.push(`${data.dedup_skipped} merged`);
              if (data.dedup_updated) parts.push(`${data.dedup_updated} updated`);
              showToast(`🧠 ${parts.join(", ")}`, "success");
            } else {
              showToast("🧠 Nothing new worth remembering", "info");
            }
          } else if (data.status === "skipped") {
            showToast("🧠 Skipped — too few messages", "info");
          } else {
            showToast(`🧠 Consolidation failed: ${data.detail || "unknown error"}`, "error");
          }
        })
        .catch((e) => {
          clearTimeout(timeout);
          if (e.name === "AbortError") {
            showToast("🧠 Consolidation timed out (30s)", "error");
          } else {
            showToast("🧠 Consolidation error: " + e.message, "error");
          }
        })
        .finally(() => {
          _consolidationPromise = null;
          // Flush queued messages
          const queued = _messageQueue.splice(0);
          if (queued.length) {
            inputEl.value = queued[0];
            sendMessage();
          }
        });

      return _consolidationPromise;
    }

    async function createChat() {
      if (creating) return null;
      setCreating(true);
      // Show loading overlay on current pane while creating new chat
      const loadingPane = activePane;
      showPaneLoading(loadingPane);
      const oldChatId = activeChatId;
      const isStreaming = oldChatId && activeStreams.has(oldChatId);
      if (isStreaming) {
        // Skip consolidation — current chat is still responding
        showToast("⏭️ Skipped memory consolidation (chat still streaming)", "info");
      } else if (oldChatId) {
        // Unified consolidation — backend handles model selection, fallback chain,
        // and uses ephemeral chat_id internally (no pollution of source chat)
        consolidateMemory(oldChatId, selectedModel);
      }
      try {
        const res  = await fetch("/api/chat/new", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ model: selectedModel, project_id: activeProjectId })
        });
        const text = await res.text();
        let data = {};
        try { data = JSON.parse(text); } catch (_) { data = { error: `Server ${res.status}: ${text.slice(0, 300)}` }; }
        if (!data.chat_id) {
          showToast(data.error || "Could not create chat", "error");
          return null;
        }
        // Open as a new tab
        switchToTab(data.chat_id);
        parentId = null;
        lockModelDropdown(null); // unlock dropdown for fresh chat
        if (typeof onChatOpened === "function") onChatOpened(activeChatId);
        saveActiveChat();
        await loadChats();
        // Reset context ring for fresh chat
        contextCharsCache.set(activeChatId, 0);
        window._statusContextChars = 0;
        updateStatusBarContext();
        // Pane already has empty state from createTabPane
        inputEl.focus();
        return activeChatId;
      } catch (err) {
        showToast("Network error: " + err.message, "error");
        return null;
      } finally {
        hidePaneLoading(loadingPane);
        setCreating(false);
      }
    }

    /* ============================= attachments ============================= */

    function addAttachmentChip(file) {
      const chip = document.createElement("div");
      chip.className = "attach-chip uploading";
      const img = document.createElement("img");
      img.src = URL.createObjectURL(file);
      chip.appendChild(img);
      attachPreview.appendChild(chip);
      return chip;
    }

    function removePendingByChip(chip) {
      const idx = pendingFiles.findIndex(p => p.chip === chip);
      if (idx !== -1) {
        URL.revokeObjectURL(pendingFiles[idx].chip.querySelector("img").src);
        pendingFiles[idx].chip.remove();
        pendingFiles.splice(idx, 1);
      }
    }

    function clearPending() {
      while (pendingFiles.length) removePendingByChip(pendingFiles[0].chip);
    }

    async function uploadFile(file) {
      const chip = addAttachmentChip(file);
      const idx = pendingFiles.length;
      pendingFiles.push({ file, path: null, chip });

      const form = new FormData();
      form.append("file", file);
      try {
        const res = await fetch("/api/upload", { method: "POST", body: form });
        const data = await res.json();
        if (!res.ok || !data.uploaded) {
          showToast(data.detail || "Upload failed", "error");
          removePendingByChip(chip);
          return;
        }
        pendingFiles[idx].path = data.path;
        pendingFiles[idx].meta = data.meta || null;
        chip.classList.remove("uploading");
        const rm = document.createElement("button");
        rm.className = "remove";
        rm.textContent = "\u00d7";
        rm.onclick = () => removePendingByChip(chip);
        chip.appendChild(rm);
      } catch (err) {
        showToast("Upload error: " + err.message, "error");
        removePendingByChip(chip);
      }
    }

    function handleFiles(files) {
      const caps = getActiveCapabilities();
      for (const f of files) {
        const kind = f.type.startsWith("image/") ? "image"
          : f.type.startsWith("video/") ? "video"
          : f.type.startsWith("audio/") ? "audio"
          : "document";
        if (caps[kind]) uploadFile(f);
        else showToast(`${kind} files not supported by this model`, "error");
      }
    }

    attachBtn.addEventListener("click", () => fileInput.click());
    // Make the whole glass pill clickable (forwards to inner button)
    document.querySelector(".attach-cell").addEventListener("click", (e) => {
      if (e.target === e.currentTarget) attachBtn.click();
    });
    document.querySelector(".send-cell").addEventListener("click", (e) => {
      if (e.target === e.currentTarget) sendBtn.click();
    });

    // Header pill click forwarding
    document.querySelectorAll(".header-icon-cell").forEach((cell) => {
      cell.style.cursor = "pointer";
      cell.addEventListener("click", (e) => {
        if (e.target === e.currentTarget) cell.querySelector("button")?.click();
      });
    });


    fileInput.addEventListener("change", () => {
      if (fileInput.files.length) handleFiles(fileInput.files);
      fileInput.value = "";
    });

    inputEl.addEventListener("paste", (e) => {
      const items = e.clipboardData?.items;
      if (!items) return;
      const caps = getActiveCapabilities();
      for (const item of items) {
        const kind = item.type.startsWith("image/") ? "image"
          : item.type.startsWith("video/") ? "video"
          : item.type.startsWith("audio/") ? "audio"
          : null;
        if (kind && caps[kind]) {
          e.preventDefault();
          handleFiles([item.getAsFile()]);
        }
      }
    });

    let dragCounter = 0;
    inputArea.addEventListener("dragenter", (e) => {
      e.preventDefault();
      dragCounter++;
      inputArea.classList.add("drag-over");
    });
    inputArea.addEventListener("dragleave", (e) => {
      e.preventDefault();
      dragCounter--;
      if (dragCounter <= 0) { dragCounter = 0; inputArea.classList.remove("drag-over"); }
    });
    inputArea.addEventListener("dragover", (e) => e.preventDefault());
    inputArea.addEventListener("drop", (e) => {
      e.preventDefault();
      dragCounter = 0;
      inputArea.classList.remove("drag-over");
      if (e.dataTransfer.files.length) handleFiles(e.dataTransfer.files);
    });

    /* =========================== end attachments =========================== */

    async function sendMessage() {
      if (isStreaming()) {
        const ctrl = activeStreams.get(activeChatId);
        if (ctrl) ctrl.abort();
        // Fallback: if abort doesn't end the stream within 3s, force-clean
        const _stuckId = activeChatId;
        setTimeout(() => {
          if (activeStreams.has(_stuckId)) {
            activeStreams.delete(_stuckId);
            updateSendBtn();
            _toggleStreamIndicator(_stuckId, false);
          }
        }, 3000);
        // Best-effort: tell backend to stop upstream Qwen generation (fire-and-forget)
        fetch("/api/chat/stop", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ chat_id: activeChatId }),
        }).catch(() => {});
        return;
      }

      let message = inputEl.value.trim();
      if (!message) return;

      // Inject speech interruption note if user spoke over TTS
      if (typeof window.takeSpeechInterrupted === 'function' && window.takeSpeechInterrupted()) {
        message = '[Note: the user interrupted your previous spoken reply]\n\n' + message;
      }

      // @ mention → spawn agent instead of sending chat message
      if (typeof parseAgentMention === "function") {
        const mention = parseAgentMention(message);
        if (mention) {
          if (!activeChatId) {
            const created = await createChat();
            if (!created) return;
          }
          inputEl.value = "";
          autoResize();
          hideMentionPopup();
          showToast(`${mention.role === "researcher" ? "🔍" : mention.role === "coder" ? "💻" : mention.role === "reviewer" ? "📋" : mention.role === "writer" ? "✍️" : "⚙️"} Spawning ${mention.role}…`, "info");
          try {
            const result = await spawnAgentFromMention(mention.role, mention.task, activeChatId);
            if (result.error) {
              showToast(`Agent spawn failed: ${result.error}`, "error");
            } else {
              showToast(`✅ ${mention.role} spawned (${result.model})`, "success");
            }
          } catch (e) {
            showToast(`Agent spawn error: ${e.message}`, "error");
          }
          return;
        }
      }

      // Queue message if consolidation is still running in SCRAPER mode only
      // API mode consolidation runs independently without blocking user input
      if (_consolidationPromise && scraperMode) {
        _messageQueue.push(message);
        inputEl.value = "";
        autoResize();
        showToast("🧠 Message queued — waiting for memory consolidation...", "info");
        return;
      }

      if (!activeChatId) {
        const created = await createChat();
        if (!created) return;
      }

      // Mode cross-guard: block sending from mismatched provider chats
      const activeMeta = chatList.find(c => c.id === activeChatId);
      if (activeMeta?.provider) {
        if (scraperMode && activeMeta.provider !== "scraping") {
          showToast("This chat is locked to " + activeMeta.provider + " — switch off scraper mode or start a new chat.", "error");
          return;
        }
        if (!scraperMode && activeMeta.provider === "scraping") {
          showToast("This is a scraping chat — enable scraper mode or start a new chat.", "error");
          return;
        }
      }

      const streamChatId = activeChatId;
      const controller = startStream(streamChatId);
      inputEl.value = "";
      autoResize();

      // Collect image URLs for chat display BEFORE clearing pending chips
      const imageUrls = pendingFiles
        .filter(p => p.path)
        .map(p => "/system/uploads/" + p.path.split("/").pop());

      // Remove previous turn's file-edit summary card
      if (activePane) activePane.querySelectorAll(".file-edit-summary-card").forEach(el => el.remove());

      const userMsgDiv = addMessage("user", message, imageUrls);
      const lastSentMessage = message;
      const ui = addBotStreaming();

      const filesPayload = pendingFiles
        .filter(p => p.path)
        .map(p => p.meta || { path: p.path });
      clearPending();

      try {
        const res = await fetch("/api/chat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            message,
            chat_id: streamChatId,
            parent_id: parentId != null ? String(parentId) : undefined,
            files: filesPayload.length ? filesPayload : undefined,
            model: selectedModel,
            thinking_mode: selectedThinkingMode,
            stream: true
          }),
          signal: controller.signal
        });

        if (!res.ok) {
          let detail = "";
          try { detail = await res.text(); } catch (_) {}
          const message = `Server error ${res.status}${detail ? ": " + detail.slice(0, 500) : ""}`;
          showToast(message, "error");
          ui.appendAnswer(`\n[error] ${message}`);
          ui.finalize();
          return;
        }

        const contentType = res.headers.get("content-type") || "";
        if (contentType.includes("application/json")) {
          const data = await res.json();
          const msg = data.error || "Unexpected JSON response";
          showToast(msg, "error");
          ui.appendAnswer(`\n[error] ${msg}`);
          ui.finalize();
          return;
        }

        const { gotAnswer, gotDone, gotError, gotTitle } = await consumeChatStream(res, ui, userMsgDiv, streamChatId);

        // Detect empty response: stream ended with no answer tokens,
        // or stream "done" but answer content is whitespace-only.
        // Don't error if we got a chat_title — the auto-loop will continue.
        const emptyResponse = (!gotAnswer && !gotError && !gotDone && !gotTitle)
          || (gotDone && !gotAnswer && !gotError && !gotTitle);
        if (emptyResponse) {
          const msg = gotDone ? "Response finished with no content" : "Stream ended without a response";
          showToast(msg, "error");
          ui.appendAnswer(`\n[error] ${msg}`);
        }
        // Show resend button when response was empty or errored
        if ((emptyResponse || gotError) && userMsgDiv && lastSentMessage) {
          attachResendBar(userMsgDiv, lastSentMessage);
        }

        // Auto-TTS for live voice chat mode
        if (gotAnswer && !gotError && typeof LiveVoiceChat !== 'undefined' && LiveVoiceChat._active) {
          const botMsg = userMsgDiv?.parentElement?.querySelector('.msg.bot .md-content');
          const responseText = botMsg ? botMsg.innerText : '';
          if (responseText.trim() && typeof window.playAutoTTS === 'function') {
            window.playAutoTTS(responseText);
          }
        }
      } catch (err) {
        if (err.name === "AbortError") {
          showToast("Generation stopped", "info");
          ui.appendAnswer("\n[stopped]");
        } else {
          showToast("Connection lost: " + err.message, "error");
          ui.appendAnswer(`\n[client error] ${err.message}`);
          if (userMsgDiv && lastSentMessage) {
            attachResendBar(userMsgDiv, lastSentMessage);
          }
        }
      } finally {
        ui.finalize();
        endStream(streamChatId);

        // After first message, provider is now locked in DB — lock the dropdown
        const meta = chatList.find(c => c.id === streamChatId);
        if (meta && !meta.provider) {
          const prov = scraperMode ? "scraping"
            : (modelList.find(m => m.id === selectedModel)?.api_backend || "qwen");
          meta.provider = prov; // update local cache
          lockModelDropdown(prov);
        }
      }
    }

    sendBtn.addEventListener("click", sendMessage);
    window.sendMessage = sendMessage; // expose for LiveVoiceChat


    // Programmatic message send for auto-turn (agent completion notifications).
    // Goes through the exact same /api/chat pipeline as a user-typed message,
    // so skill cards, stop button, markdown, and history replay all work normally.
    async function sendAutoTurnMessage(message, opts = {}) {
      if (!message || !activeChatId) return;
      if (isStreaming()) {
        // Queue: retry after current stream finishes
        setTimeout(() => sendAutoTurnMessage(message, opts), 1500);
        return;
      }

      const streamChatId = activeChatId;
      const controller = startStream(streamChatId);

      // Remove previous turn's file-edit summary card
      if (activePane) activePane.querySelectorAll(".file-edit-summary-card").forEach(el => el.remove());

      const userMsgDiv = opts.skipUserBubble ? null : addMessage("user", message);
      const ui = addBotStreaming();

      try {
        const res = await fetch("/api/chat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            message,
            chat_id: streamChatId,
            parent_id: parentId != null ? String(parentId) : undefined,
            model: selectedModel,
            thinking_mode: selectedThinkingMode,
            stream: true,
            ...(opts.skipUserSave ? { skip_user_save: true } : {})
          }),
          signal: controller.signal
        });

        if (!res.ok) {
          let detail = "";
          try { detail = await res.text(); } catch (_) {}
          const msg = `Auto-turn error ${res.status}${detail ? ": " + detail.slice(0, 300) : ""}`;
          showToast(msg, "error");
          ui.appendAnswer(`\n[error] ${msg}`);
          ui.finalize();
          return;
        }

        const contentType = res.headers.get("content-type") || "";
        if (contentType.includes("application/json")) {
          const data = await res.json();
          const msg = data.error || "Unexpected JSON response";
          showToast(msg, "error");
          ui.appendAnswer(`\n[error] ${msg}`);
          ui.finalize();
          return;
        }

        await consumeChatStream(res, ui, userMsgDiv, streamChatId);
      } catch (err) {
        if (err.name === "AbortError") {
          showToast("Auto-turn stopped", "info");
          ui.appendAnswer("\n[stopped]");
        } else {
          showToast("Auto-turn connection lost: " + err.message, "error");
          ui.appendAnswer(`\n[client error] ${err.message}`);
        }
      } finally {
        ui.finalize();
        endStream(streamChatId);
      }
    }



    inputEl.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        // If the @mention popup is open, let agents.js handle Enter (selection)
        const mp = document.querySelector(".agent-mention-popup");
        if (mp && mp.style.display !== "none") return;
        if (isStreaming()) return; // let Enter insert newline while model responds
        // On touch devices Enter inserts a newline; the send button sends.
        if (window.matchMedia("(pointer: coarse)").matches) return;
        e.preventDefault();
        sendMessage();
      }
    });

    if (newChatBtn) newChatBtn.addEventListener("click", createChat);
    const newChatSidebarBtn = document.getElementById("newChatSidebar");
    if (newChatSidebarBtn) {
        newChatSidebarBtn.addEventListener("click", createChat);
    }
    // Sidebar top "New Chat" button
    const sidebarNewChatBtn = document.getElementById('sidebarNewChatBtn');
    if (sidebarNewChatBtn) {
      sidebarNewChatBtn.addEventListener('click', createChat);
    }

    // Chat search toggle + filter
    const projectFolderBtn = document.getElementById('projectFolderBtn');
    if (projectFolderBtn) {
      projectFolderBtn.addEventListener('click', () => { loadProjects().then(() => showProjectFolderDropdown()); });
    }

    // Project ... menu button
    (() => {
      const pmb = document.getElementById('projectMenuBtn');
      if (!pmb) return;
      pmb.addEventListener('click', (e) => {
        e.stopPropagation();
        document.querySelectorAll('.project-menu-dropdown').forEach(el => el.remove());
        if (!activeProjectId) return;
        const proj = projectList.find(p => p.id === activeProjectId);
        if (!proj) return;

        const dd = document.createElement('div');
        dd.className = 'project-folder-dropdown project-menu-dropdown';
        const rect = pmb.getBoundingClientRect();
        dd.style.position = 'fixed';
        dd.style.top = (rect.bottom + 4) + 'px';
        dd.style.left = (rect.right - 180) + 'px';

        const items = [
          { label: 'Rename', icon: 'pencil', action: async () => {
              const newName = await sablePrompt('Rename project:', proj.name);
              if (!newName || !newName.trim() || newName.trim() === proj.name) return;
              try {
                await fetch('/api/projects/' + activeProjectId, {
                  method: 'PUT',
                  headers: { 'Content-Type': 'application/json' },
                  body: JSON.stringify({ name: newName.trim() })
                });
                const nameEl = document.querySelector('.project-name');
                if (nameEl) nameEl.textContent = newName.trim();
                await loadProjects();
              } catch (err) { console.error('Rename failed:', err); }
            } },
          { label: 'Settings', icon: 'settings', action: () => showProjectSettingsPopup(proj, pmb) },
          { label: 'Exit Project', icon: 'arrow-left', action: () => {
              fetch('/api/projects/deactivate', { method: 'POST' }).then(async (r) => {
                const data = await r.json().catch(() => ({}));
                activeProjectId = null;
                if (data.new_cwd && typeof window.pickFsRoot === 'function') window.pickFsRoot(data.new_cwd);
                await createChat();
                fetch('/api/sync-context', { method: 'POST' });
              });
            } },
        ];

        items.forEach(item => {
          const row = document.createElement('div');
          row.className = 'project-folder-item';
          row.innerHTML = '<i data-lucide="' + item.icon + '" class="icon-lucide" style="width:14px;height:14px;margin-right:8px;vertical-align:-2px;"></i>' + item.label;
          row.onclick = () => { dd.remove(); item.action(); };
          dd.appendChild(row);
        });

        document.body.appendChild(dd);
        if (typeof lucide !== 'undefined') lucide.createIcons({ nodes: [dd] });

        const close = (ev) => { if (!dd.contains(ev.target) && ev.target !== pmb) { dd.remove(); document.removeEventListener('click', close, true); } };
        setTimeout(() => document.addEventListener('click', close, true), 0);
      });
    })();


    const chatSearchBtn = document.getElementById('chatSearchBtn');
    const chatSearchInput = document.getElementById('chatSearch');
    if (chatSearchBtn && chatSearchInput) {
      chatSearchBtn.addEventListener('click', () => {
        const isVisible = chatSearchInput.classList.toggle('visible');
        chatsEl.style.marginTop = isVisible ? '36px' : '';
        if (isVisible) {
          chatSearchInput.focus();
        } else {
          chatSearchInput.value = '';
          chatSearchQuery = '';
          chatSearchResults = null;
          renderChats();
        }
      });
      let _searchDebounce = null;
      chatSearchInput.addEventListener('input', () => {
        chatSearchQuery = chatSearchInput.value;
        clearTimeout(_searchDebounce);
        if (!chatSearchQuery.trim()) {
          chatSearchResults = null;
          renderChats();
          return;
        }
        _searchDebounce = setTimeout(async () => {
          try {
            const data = await fetch(`/api/chats/search?q=${encodeURIComponent(chatSearchQuery.trim())}`).then(r => r.json());
            chatSearchResults = data.results || [];
          } catch { chatSearchResults = []; }
          renderChats();
        }, 300);
      });
      chatSearchInput.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
          chatSearchInput.value = '';
          chatSearchQuery = '';
          chatSearchResults = null;
          chatSearchInput.classList.remove('visible');
          chatsEl.style.marginTop = '';
          renderChats();
        }
      });
    }

    const sidebarToggleBtn = document.getElementById('sidebarToggle');
    if (sidebarToggleBtn) {
      sidebarToggleBtn.addEventListener('click', () => {
        const isMobile = window.matchMedia('(max-width: 860px)').matches;
        if (isMobile) {
          document.body.classList.toggle('sidebar-open');
        } else {
          document.body.classList.toggle('sidebar-collapsed');
        }
      });
    }
    const sidebarOverlay = document.querySelector('.sidebar-overlay');

    const brandRow = document.querySelector('.brand-row');
    if (brandRow) {
      brandRow.addEventListener('click', (e) => {
        // Don't toggle if they clicked the new-chat button itself
        if (e.target.closest('#newChat')) return;
        document.querySelector('.sidebar-top-content')?.classList.toggle('collapsed');
      });
    }

    if (sidebarOverlay) {
      sidebarOverlay.addEventListener('click', () => {
        document.body.classList.remove('sidebar-open');
      });
    }



    (async () => {
    await ensureAuth();
    await loadModels();
    // Fetch typewriter animation speed from server config
    fetch("/api/config/ui").then(r => r.json()).then(cfg => {
      if (cfg.typewriter_chars_per_tick) TW_CHARS = cfg.typewriter_chars_per_tick;
      if (cfg.typewriter_tick_ms) TW_MS = cfg.typewriter_tick_ms;
    }).catch(() => {});

    // Load chats filtered by current mode so sidebar matches active mode
    const initialMode = scraperMode ? 'scraper' : 'api';
    loadChats(initialMode).then(async () => {
      let savedChatId = null;
      let savedParentId = null;
      try {
        savedChatId = localStorage.getItem(ACTIVE_CHAT_KEY);
        savedParentId = localStorage.getItem(PARENT_KEY);
      } catch (err) {
        console.warn("Could not read persisted chat:", err);
      }

      // Always start fresh — don't restore last chat on reload
      if (false && savedChatId && chatList.some(c => c.id === savedChatId)) {
        await selectChat(savedChatId);
      } else if (false && chatList.length > 0) {
        await selectChat(chatList[0].id);
      } else {
        chatEl.innerHTML = `<div class="empty"><h2>Start a chat</h2><p>Create a new chat and talk to Sable.</p></div>`;
      }
    });
    })();

