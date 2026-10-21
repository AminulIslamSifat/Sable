

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
      // Starting a new chat must ALWAYS snap the view back to the chat tab,
      // regardless of which rail tab is currently active.
      if (typeof window._sableFocusChatRail === "function") window._sableFocusChatRail();
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

    const _DOC_ICON_MAP = {
      "application/pdf": { label: "PDF", color: "#e74c3c" },
      "application/vnd.openxmlformats-officedocument.presentationml.presentation": { label: "PPTX", color: "#d35400" },
      "application/vnd.ms-powerpoint": { label: "PPT", color: "#d35400" },
      "application/msword": { label: "DOC", color: "#2980b9" },
      "application/vnd.openxmlformats-officedocument.wordprocessingml.document": { label: "DOCX", color: "#2980b9" },
      "text/csv": { label: "CSV", color: "#27ae60" },
      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": { label: "XLSX", color: "#16a085" },
      "application/vnd.ms-excel": { label: "XLS", color: "#16a085" },
      "text/markdown": { label: "MD", color: "#8e44ad" },
      "text/plain": { label: "TXT", color: "#7f8c8d" },
    };

    function _isImageFile(file) {
      return file.type.startsWith("image/");
    }

    function addAttachmentChip(file) {
      const chip = document.createElement("div");
      chip.className = "attach-chip uploading";

      if (_isImageFile(file)) {
        const img = document.createElement("img");
        img.src = URL.createObjectURL(file);
        chip.appendChild(img);
      } else {
        // Document / non-image file — show styled icon + extension label
        const info = _DOC_ICON_MAP[file.type] || { label: (file.name.split(".").pop() || "FILE").toUpperCase().slice(0, 5), color: "#95a5a6" };
        chip.classList.add("attach-chip-doc");
        chip.innerHTML = `<span class="doc-icon">📄</span><span class="doc-label" style="color:${info.color}">${info.label}</span>`;
        chip.title = file.name;
      }

      attachPreview.appendChild(chip);
      return chip;
    }

    function removePendingByChip(chip) {
      const idx = pendingFiles.findIndex(p => p.chip === chip);
      if (idx !== -1) {
        const img = pendingFiles[idx].chip.querySelector("img");
        if (img) URL.revokeObjectURL(img.src);
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
        pendingFiles[idx].filename = data.filename || null;
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

    // Guard: prevents sending while a stop request is in-flight
    let _stopInFlight = false;

    /**
     * Canonical stop-generation routine. Shared by the send button's stop-mode
     * state and the Ctrl+X shortcut so the two can never drift apart.
     */
    async function stopGeneration() {
      if (_stopInFlight) return;
      if (!isStreaming()) return;

      _stopInFlight = true;
      const ctrl = activeStreams.get(activeChatId);
      if (ctrl) ctrl.abort();
      // Nuclear stop: abort all active agent sub-streams for this chat
      if (typeof window._abortAllAgents === "function") window._abortAllAgents();
      // Fallback: if abort doesn't end the stream within 3s, force-clean
      const _stuckId = activeChatId;
      setTimeout(() => {
        if (activeStreams.has(_stuckId)) {
          activeStreams.delete(_stuckId);
          updateSendBtn();
          _toggleStreamIndicator(_stuckId, false);
        }
      }, 3000);
      // Await the stop API so upstream generation is actually halted before
      // the user can send a new message — eliminates CHAT_IN_PROGRESS race.
      try {
        const _respId = typeof window.getActiveResponseId === "function"
          ? window.getActiveResponseId(activeChatId)
          : null;
        await fetch("/api/chat/stop", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ chat_id: activeChatId, response_id: _respId }),
        });
      } catch (_) {}
      // Small grace period for upstream to fully release the session
      await new Promise(r => setTimeout(r, 500));
      _stopInFlight = false;
    }

    // Exposed for the shortcut dispatcher (settings-ui.js).
    window.sableStopGeneration = stopGeneration;

    async function sendMessage() {
      // Block sends while a stop is being processed — prevents CHAT_IN_PROGRESS race
      if (_stopInFlight) return;

      if (isStreaming()) {
        await stopGeneration();
        return;
      }

      let message = inputEl.value.trim();
      if (!message) return;

      // Block sending if no provider is configured
      const selModel = modelSelectEl?.value || selectedModel;
      if (selModel === "__none__") {
        showToast("No AI provider configured yet. Add one in Settings → Providers.", "error");
        return;
      }

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

          try {
            const result = await spawnAgentFromMention(mention.role, mention.task, activeChatId, message);
            if (result.error) {
              showToast(`Agent spawn failed: ${result.error}`, "error");
            }
            // Refresh sidebar so the agent's new chat entry appears with the
            // correct title (the task text, not "New conversation").
            if (typeof window._sableLoadChats === "function") {
              await window._sableLoadChats();
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
      // Forked chats are unlocked — allow any provider/model combination
      const activeMeta = chatList.find(c => c.id === activeChatId);
      if (activeMeta?.provider && !activeMeta.is_fork) {
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
      const streamPane = activePane;

      // Study chats are grounded document chats. When opened from the sidebar
      // their turns must go through the study bridge (which sends layout_mode +
      // study_context), NOT the plain agent path — otherwise the model loses the
      // document grounding and the chat/agent harness leaks in. Route the typed
      // message through studyChat and stop here.
      const _studyTarget = window.studyChat && window.studyChat.docForThread
        ? window.studyChat.docForThread(streamChatId) : '';
      if (_studyTarget) {
        inputEl.value = "";
        autoResize();
        // A `note:<id>` target is a note AI thread — ground it in the note body
        // (study_context.note_id), not a document. Otherwise it is a document
        // study chat and grounds on the current document scope.
        const _isNote = _studyTarget.startsWith('note:');
        const _ctx = _isNote
          ? { doc_id: '', note_id: _studyTarget.slice(5), action: 'ask', question: message, scope: 'doc' }
          : { doc_id: _studyTarget, action: 'ask', scope: 'doc' };
        window.studyChat.send({
          docId: _studyTarget,
          threadId: streamChatId,
          text: message,
          context: _ctx,
          pane: streamPane,
        });
        return;
      }

      const controller = startStream(streamChatId);
      inputEl.value = "";
      autoResize();

      // Collect image URLs for chat display BEFORE clearing pending chips
      const imageUrls = pendingFiles
        .filter(p => p.filename || p.path)
        .map(p => "/system/uploads/" + (p.filename || p.path.split("/").pop()));

      // Remove previous turn's file-edit summary card
      if (streamPane) streamPane.querySelectorAll(".file-edit-summary-card").forEach(el => el.remove());

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
            stream: true,
            cwd: window.getIdeCwd ? window.getIdeCwd() : undefined,
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
          // Don't fabricate a vague message — the backend now emits a real
          // `error` event for empty/rate-limited responses (gotError would be
          // true and we'd never reach here). This branch only fires when the
          // stream truly ended with nothing AND no error was sent, so be honest
          // about which case it is.
          const msg = gotDone
            ? "The model returned an empty response. Check the provider status or retry."
            : "The stream ended before any response arrived. Check your connection or retry.";
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
        // Safety net: endStream is idempotent, but a concurrent stop/recovery can
        // leave a migrated key behind. Sweep this chat's entry unconditionally so
        // isStreaming() can never stay true after a turn ends — that state locks
        // the send button into stop-mode with no way back except a manual click.
        try { if (activeStreams.has(streamChatId)) activeStreams.delete(streamChatId); } catch (_) {}
        try { if (activeChatId === streamChatId) updateSendBtn(); } catch (_) {}

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

    // ---- Queued send (message typed while the model is still responding) ----
    // Pending optimistic bubbles keyed by temp_id, so the SSE `queued_injected`
    // event can finalize them once the backend actually delivers the message.
    const _pendingQueued = new Map();
    window._sablePendingQueued = _pendingQueued;

    function _updateQueueBadge() {
      const qBtn = document.getElementById("queueSend");
      const badge = qBtn?.querySelector(".queue-badge");
      if (!badge) return;
      const n = _pendingQueued.size;
      badge.textContent = String(n);
      badge.hidden = n <= 1;  // badge only when 2+ waiting (single is obvious)
    }

    async function queueMessage() {
      const message = inputEl.value.trim();
      if (!message) return;
      const tempId = "q" + Date.now() + "-" + Math.random().toString(36).slice(2, 7);

      // Optimistic bubble — rendered immediately, marked "queued" until the
      // backend confirms delivery mid-turn.
      const bubble = addMessage("user", message, undefined, activePane);
      if (bubble) {
        bubble.classList.add("queued-msg");
        bubble.dataset.tempId = tempId;
      }
      _pendingQueued.set(tempId, { text: message, bubble });
      _updateQueueBadge();
      inputEl.value = "";
      autoResize();

      try {
        const res = await fetch("/api/chat/queue", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ chat_id: activeChatId, message, temp_id: tempId }),
        });
        const data = await res.json().catch(() => ({}));
        if (!data.queued) {
          // Stream ended between render and request — fall back to a normal send.
          _pendingQueued.delete(tempId);
          _updateQueueBadge();
          if (bubble) bubble.remove();
          inputEl.value = message;
          autoResize();
          sendMessage();
        }
      } catch (err) {
        _pendingQueued.delete(tempId);
        _updateQueueBadge();
        if (bubble) {
          bubble.classList.remove("queued-msg");
          bubble.classList.add("queue-failed");
        }
        showToast("Queue failed: " + err.message, "error");
      }
    }
    window.queueMessage = queueMessage;

    const queueSendBtn = document.getElementById("queueSend");
    if (queueSendBtn) queueSendBtn.addEventListener("click", queueMessage);

    // Finalize a queued bubble when the backend confirms it was injected.
    window._sableFinalizeQueued = function (tempIds, dbIds, text) {
      (tempIds || []).forEach((tid, i) => {
        const entry = _pendingQueued.get(tid);
        if (!entry) return;
        _pendingQueued.delete(tid);
        if (entry.bubble) {
          entry.bubble.classList.remove("queued-msg");
          entry.bubble.classList.add("queued-delivered");
          if (dbIds && dbIds[i]) entry.bubble.dataset.msgId = String(dbIds[i]);
        }
      });
      _updateQueueBadge();
    };


    // Programmatic message send for auto-turn (agent completion notifications).
    // Goes through the exact same /api/chat pipeline as a user-typed message,
    // so skill cards, stop button, markdown, and history replay all work normally.
    async function sendAutoTurnMessage(message, opts = {}) {
      // Use explicit target chat if provided (from agent completion context),
      // otherwise fall back to activeChatId.
      const targetChatId = opts.targetChatId || activeChatId;
      if (!message || !targetChatId) return;
      if (isStreaming()) {
        // Queue: retry after current stream finishes
        setTimeout(() => sendAutoTurnMessage(message, opts), 1500);
        return;
      }

      const streamChatId = targetChatId;
      const controller = opts.controller || startStream(streamChatId);
      const streamPane = typeof window._sableEnsurePane === "function"
        ? window._sableEnsurePane(streamChatId)
        : activePane;

      // Remove previous turn's file-edit summary card
      if (activePane) activePane.querySelectorAll(".file-edit-summary-card").forEach(el => el.remove());

      const userMsgDiv = opts.skipUserBubble ? null : addMessage("user", message, undefined, streamPane);
      const ui = addBotStreaming(streamPane, streamChatId);

      try {
        const res = await fetch("/api/chat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            message,
            chat_id: streamChatId,
            parent_id: opts.parentId != null ? String(opts.parentId) : undefined,
            model: opts.model || selectedModel,
            thinking_mode: selectedThinkingMode,
            stream: true,
            ...(opts.skipUserSave ? { skip_user_save: true } : {}),
            ...(opts.systemPrompt ? { system_prompt: opts.systemPrompt } : {}),
            ...(opts.browserDataDir ? { browser_data_dir: opts.browserDataDir } : {})
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

        await consumeChatStream(res, ui, userMsgDiv, streamChatId, streamPane);
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
        if (isStreaming()) {
          // Model is responding — Enter queues the message instead of being
          // swallowed. Shift+Enter still inserts a newline (handled above).
          if (window.matchMedia("(pointer: coarse)").matches) return;
          e.preventDefault();
          queueMessage();
          return;
        }
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
        // Don't toggle if they clicked an interactive control in the brand row
        // (new chat lives here now, alongside the other sidebar icon buttons).
        if (e.target.closest('#newChat, #sidebarNewChatBtn, .sidebar-icon-row')) return;
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

    // One-shot lazy warmup: the server no longer warms browsers at startup.
    // Fire this only after the UI has genuinely loaded, and guard it per
    // BROWSER SESSION (sessionStorage, not localStorage) so a tab refresh or
    // a second tab doesn't re-trigger it — the server is idempotent anyway,
    // but this keeps the request off the wire entirely on reload.
    if (!sessionStorage.getItem('sable_warmup_sent')) {
      sessionStorage.setItem('sable_warmup_sent', '1');
      fetch('/api/warmup', { method: 'POST' }).catch(() => {});
    }
    // Fetch typewriter animation speed from server config
    fetch("/api/config/ui").then(r => r.json()).then(cfg => {
      if (cfg.typewriter_chars_per_tick) TW_CHARS = cfg.typewriter_chars_per_tick;
      if (cfg.typewriter_tick_ms) TW_MS = cfg.typewriter_tick_ms;
      if (cfg.model_speeds) {
        TW_MODEL_SPEEDS = cfg.model_speeds;
        if (typeof twSeedFromServer === "function") twSeedFromServer(cfg.model_speeds);
      }
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


// ── Network Access Mode (localhost vs shared LAN) ──────────────────────────
(() => {
  const select = document.getElementById("networkModeSelect");
  const status = document.getElementById("networkModeStatus");
  if (!select) return;

  fetch("/api/settings/network").then(r => r.json()).then(data => {
    select.value = data.mode === "local" ? "local" : "lan";
  }).catch(() => {});

  select.addEventListener("change", async () => {
    if (status) status.textContent = "Saving…";
    try {
      const res = await fetch("/api/settings/network", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode: select.value }),
      });
      const data = await res.json();
      if (status) status.textContent = res.ok
        ? "Saved — restart Sable to apply."
        : "❌ " + (data.detail || "Failed to save");
    } catch {
      if (status) status.textContent = "❌ Connection error";
    }
  });
})();

