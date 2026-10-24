
    function addHistoryMessage(message, pane) {
      pane = pane || activePane;
      clearPaneEmptyState(pane);

      // --- 1. Render message content FIRST (fixes ordering: msg before skill_events) ---
      let displayContent = message.content || "";
      let realTs = null;
      if (message.role === "user") {
        const memMatch = displayContent.match(/^\[RELEVANT MEMORY CONTEXT\][\s\S]*?\n\n/);
        if (memMatch) displayContent = displayContent.slice(memMatch[0].length);
        const tsMatch = displayContent.match(/^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\n?/);
        if (tsMatch) {
          realTs = tsMatch[1];
          displayContent = displayContent.slice(tsMatch[0].length);
        }
      }

      // Legacy thinking (no round_thinking in events)
      const events = Array.isArray(message.skill_events) ? message.skill_events : [];
      const hasRoundThinking = events.some((e) => e.type === "round_thinking");
      const hasRoundText = events.some((e) => e.type === "round_text" && e.text && e.text.trim());
      if (message.thinking && !hasRoundThinking) {
        const wrap = document.createElement("div");
        wrap.className = "thinking-wrap";
        wrap.innerHTML = `
          <details class="thinking">
            <summary><i data-lucide="chevron-right" class="thinking-chevron"></i>Thinking</summary>
            <div class="thinking-body">${escHtml(message.thinking)}</div>
          </details>`;
        pane.appendChild(wrap);
        // ponytail: Skip per-element icon scan during bulk history render
        if (!window._historyLoading) activateLucideIcons(wrap);
      }

      // For assistant messages with round_text events, skip main content rendering —
      // _renderSkillEvents will replay text + skills in streaming order via round_text.
      // This prevents the "all text first, then all commands" mesh.
      const skipMainContent = message.role !== "user" && hasRoundText;
      const msgDiv = skipMainContent ? null : addMessage(message.role === "user" ? "user" : "bot", displayContent, undefined, pane);
      if (message.role === "user" && msgDiv) {
        // Enable fork button for history-loaded messages (addMessage creates it disabled)
        if (message.id) {
          msgDiv.dataset.msgId = String(message.id);
          const pendingFork = msgDiv.querySelector(".fork-pending");
          if (pendingFork) {
            pendingFork.disabled = false;
            pendingFork.classList.remove("fork-pending");
          }
        }
        if (realTs) {
          const tsEl = msgDiv.querySelector(".msg-timestamp");
          if (tsEl) tsEl.textContent = `[${realTs}]`;
        }
        if (Array.isArray(message.memory_used) && message.memory_used.length) {
          attachMemoryChip(msgDiv, message.memory_used);
        }
        // Copy + read-aloud are shared across every message kind — see
        // msg-actions.js. The checkpoint / fork buttons below stay here.
        let toolbar = window.MsgActions.ensureToolbar(msgDiv);
        window.MsgActions.addCopy(toolbar, msgDiv, "user");
        window.MsgActions.addTTS(toolbar, msgDiv, "user");
        // Checkpoint restore button (git-branch icon)
        if (!toolbar.querySelector('[title="Restore checkpoint"]') && message.id) {
          const cpBtn = document.createElement("button");
          cpBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="6" y1="3" x2="6" y2="15"></line><circle cx="18" cy="6" r="3"></circle><circle cx="6" cy="18" r="3"></circle><path d="M18 9a9 9 0 0 1-9 9"></path></svg>';
          cpBtn.title = "Restore checkpoint";
          cpBtn.dataset.msgId = message.id;
          cpBtn.addEventListener("click", () => showCheckpointModal(activeChatId, message.id, cpBtn));
          toolbar.appendChild(cpBtn);
        }
        // Fork button (git-branch icon)
        if (!toolbar.querySelector('[title="Fork from here"]') && message.id) {
          const forkBtn = document.createElement("button");
          forkBtn.innerHTML = '<i data-lucide="git-branch"></i>';
          forkBtn.title = "Fork from here";
          forkBtn.addEventListener("click", async () => {
            forkBtn.disabled = true;
            forkBtn.innerHTML = '<i data-lucide="loader-circle" class="spin"></i>';
            activateLucideIcons(forkBtn);
            try {
              const res = await fetch("/api/chat/fork", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ chat_id: activeChatId, message_id: String(message.id) }),
              });
              const data = await res.json();
              if (!res.ok || data.error) {
                showToast(data.error || data.detail || "Fork failed", "error");
                return;
              }
              showToast(`Forked ${data.message_count} messages`, "success");
              // Navigate to new chat
              if (typeof window._sableLoadChats === "function") {
                await window._sableLoadChats();
              }
              if (typeof window._sableSelectChat === "function") {
                await window._sableSelectChat(data.chat_id);
              }
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
        }
        // ponytail: Skip per-element icon scan during bulk history render
        if (!window._historyLoading) activateLucideIcons(toolbar);
      }
      // Attach toolbar to historical bot messages (or skip if round_text will handle it)
      if (message.role !== "user" && msgDiv) {
        _attachBotToolbar(msgDiv);
      }

      // --- 2. Render skill_events (embedded or lazy-loaded) ---
      if (events.length > 0) {
        _renderSkillEvents(events, pane);
      } else if (message.has_skill_events && message.id) {
        // Lazy-load skill_events from the API
        _lazyLoadSkillEvents(message.id, message.chat_id || activeChatId, pane);
      }
    }

    // Attach copy + TTS toolbar to a .msg.bot div (reused by history + round_text)
    function _attachBotToolbar(msgDiv) {
      if (!msgDiv || msgDiv.querySelector(".msg-toolbar")) return;
      // Shared implementation — see msg-actions.js. Icon activation is skipped
      // during bulk history render (window._historyLoading) to avoid a
      // per-message scan; the caller activates icons once at the end.
      window.MsgActions.attach(msgDiv, "bot", { skipIcons: !!window._historyLoading });
    }

    // ponytail: Cap stored skill_output text during history replay.
    // Backend caps live streaming at 20K chars, but DB may hold unlimited
    // output from before the cap existed. Prevents DOM explosion on load.
    const HISTORY_SKILL_OUTPUT_CAP = 20000;

    // ponytail: Max skill_output events rendered per card during history load.
    // Prevents 800-event zip outputs from creating 800 DOM mutations on replay.
    // Live streaming uses the backend batch+cap; this guards old DB records.
    const HISTORY_MAX_OUTPUT_EVENTS_PER_CARD = 50;

    function _renderSkillEvents(events, pane) {
      pane = pane || activePane;
      const cards = {};
      const cardOutputCounts = {};
      let group = null;
      let lastTextBubble = null;   // bubble that inline UI/text merges into
      let lastWasUi = false;       // last event was an inline UI segment
      // Live-stream ui_block nodes keyed by server id, so repeated `partial`
      // events re-render into the SAME element instead of stacking copies.
      const _uiSegs = Object.create(null);
      // History replay accumulator for <render_html> panes (keyed by block id).
      const _htmlHist = Object.create(null);
      // Last rendered payload signature per ui-block id. A streaming block is
      // re-emitted on every partial; when two consecutive payloads serialise
      // identically there is nothing new to draw, so we skip the
      // innerHTML='' + full re-render. Without this the block (and every <img>
      // inside it) is destroyed and rebuilt on each tick, which the browser
      // shows as "image appears, then flickers to another one".
      const _uiSigs = Object.create(null);
      let _histSkillPath = "";
      // During history load, skip per-event MathJax/Mermaid/Lucide —
      // loadMessages does a single pass over the whole pane after all events
      // are rendered. This avoids O(n) full-pane reflows for n events.
      const isHistoryLoad = window._historyLoading;
      // ponytail: No-op icon activation during bulk history render.
      // Single activateLucideIcons(pane) call in loadMessages handles all.
      const _actIcons = isHistoryLoad ? () => {} : activateLucideIcons;
      for (const evt of events) {
        if (evt.type === "round_thinking") {
          group = null;
          lastTextBubble = null;
          lastWasUi = false;
          const wrap = document.createElement("div");
          wrap.className = "thinking-wrap";
          wrap.innerHTML = `
            <details class="thinking">
              <summary><i data-lucide="chevron-right" class="thinking-chevron"></i>Thinking</summary>
              <div class="thinking-body">${escHtml(evt.text || "")}</div>
            </details>`;
          pane.appendChild(wrap);
          _actIcons(wrap);
        } else if (evt.type === "round_text") {
          if (evt.text && evt.text.trim()) {
            const content = document.createElement("div");
            content.className = "md-content";
            content.innerHTML = renderMarkdown(evt.text);
            // Skip heavy renders during history load — deferred to single
            // pane-wide pass in loadMessages after fragment is attached.
            if (!isHistoryLoad) {
              renderMermaidDiagrams(content);
              renderMathJax(content);
            }
            _actIcons(content);
            if (lastWasUi && lastTextBubble) {
              // Continuation of the same card after an inline UI segment.
              lastTextBubble.appendChild(content);
            } else {
              const textDiv = document.createElement("div");
              textDiv.className = "msg bot";
              textDiv.appendChild(content);
              pane.appendChild(textDiv);
              _attachBotToolbar(textDiv);
              lastTextBubble = textDiv;
            }
            lastWasUi = false;
          }
        } else if (evt.type === "ui_block") {
          // Rich UI block. During streaming the server emits the SAME block
          // repeatedly — `partial:true` while it's still being written, then a
          // final `partial:false` seal. We key the DOM node by `evt.id` so each
          // update re-renders into the same element instead of stacking a dozen
          // half-built copies. A block with no id (history replay, legacy) just
          // gets a fresh segment, same as before.
          const _uiId = evt.id || null;

          if (evt.remove) {
            // Final parse came back empty — drop the partial the client drew.
            if (_uiId && _uiSegs[_uiId]) {
              _uiSegs[_uiId].remove();
              delete _uiSegs[_uiId];
            }
            if (_uiId) delete _uiSigs[_uiId];
            lastWasUi = true;
          } else {
            let uiSeg = _uiId ? _uiSegs[_uiId] : null;
            if (!uiSeg || !uiSeg.isConnected) {
              if (!lastTextBubble) {
                lastTextBubble = document.createElement("div");
                lastTextBubble.className = "msg bot";
                pane.appendChild(lastTextBubble);
              }
              uiSeg = document.createElement("div");
              uiSeg.className = "md-content ui-block";
              lastTextBubble.appendChild(uiSeg);
              if (_uiId) _uiSegs[_uiId] = uiSeg;
            }
            if (evt.payload && window.SableComponents) {
              // Re-render from scratch — the tree is tiny and this is the only
              // way to handle nodes being reordered/closed mid-stream. Clear
              // first so a partial that shrank doesn't leave stale nodes behind.
              let _sig = null;
              try { _sig = JSON.stringify(evt.payload); } catch (e) { _sig = null; }
              // Identical payload as last tick → nothing to redraw. Also covers
              // the seal (partial:false) that repeats a partial verbatim.
              if (_sig === null || _uiId === null || _uiSigs[_uiId] !== _sig) {
                try {
                  // Atomic: stage off-DOM and swap only on success, so a bad
                  // payload can never blank a block that already rendered.
                  if (window.SableComponents.renderInto) {
                    window.SableComponents.renderInto(uiSeg, evt.payload);
                  } else {
                    uiSeg.innerHTML = "";
                    window.SableComponents.render(evt.payload, uiSeg);
                  }
                  if (_uiId !== null && _sig !== null) _uiSigs[_uiId] = _sig;
                } catch (e) {}
                _actIcons(uiSeg);
              }
            } else {
              _actIcons(uiSeg);
            }
            lastWasUi = true;
            // Seal: the block is final, stop tracking it so a later block with
            // a recycled id can't accidentally reuse this node.
            if (_uiId && evt.partial === false) {
              delete _uiSegs[_uiId];
              delete _uiSigs[_uiId];
            }
          }
        } else if (evt.type === "html_start" || evt.type === "html_chunk" || evt.type === "html_end") {
          // Sandboxed HTML pane (history replay). Accumulate chunks by id, then
          // mount + finish on html_end so the replayed pane matches the live one.
          const _hid = evt.id || "html_hist";
          if (!_htmlHist[_hid]) _htmlHist[_hid] = { title: "", src: "", height: null };
          if (evt.type === "html_start") {
            _htmlHist[_hid].title = evt.title || "Interactive";
            _htmlHist[_hid].height = evt.height || null;
          } else if (evt.type === "html_chunk") {
            _htmlHist[_hid].src += evt.text || "";
          } else if (evt.type === "html_end") {
            if (window.SableHtml) {
              if (!lastTextBubble) {
                lastTextBubble = document.createElement("div");
                lastTextBubble.className = "msg bot";
                pane.appendChild(lastTextBubble);
              }
              const holder = document.createElement("div");
              holder.className = "md-content ui-html";
              const _blk = window.SableHtml.mount(_hid, _htmlHist[_hid].title, _htmlHist[_hid].height);
              holder.appendChild(_blk.wrap);
              lastTextBubble.appendChild(holder);
              window.SableHtml.push(_hid, _htmlHist[_hid].src);
              window.SableHtml.finish(_hid, evt.error);
            }
            delete _htmlHist[_hid];
          }
          lastWasUi = true;
        } else if (evt.type === "skill_start") {
          lastTextBubble = null;
          lastWasUi = false;
          if (evt.name === "ask_user") continue;
          if (!group) {
            group = document.createElement("div");
            group.className = "skill-stack";
            group.style.display = "flex";
            pane.appendChild(group);
          }
          const card = createSkillCard(evt);
          group.appendChild(card);
          _actIcons(card);
          cards[evt.id] = card;
          // Track path for history preview card
          if (evt.name === "create_file" || evt.name === "edit_file" || evt.name === "save_svg" || evt.name === "create_svg") {
            const _d = evt.data && (evt.data.attrs || evt.data);
            _histSkillPath = (_d && (_d.path || _d.filename)) || "";
          }
        } else if (evt.type === "skill_output") {
          if (evt.name === "ask_user") {
            try {
              const card = renderAskUserCard(JSON.parse(evt.text), pane);
              card.classList.add("answered");
              card.querySelectorAll("button").forEach(b => b.disabled = true);
            } catch(e) { /* skip malformed */ }
            continue;
          }
          const card = cards[evt.id];
          if (card) {
            let outText = evt.text || "";
            // During history load, cap event count per card to prevent
            // old uncapped DB records from flooding the DOM.
            if (isHistoryLoad) {
              const count = (cardOutputCounts[evt.id] || 0) + 1;
              cardOutputCounts[evt.id] = count;
              if (count > HISTORY_MAX_OUTPUT_EVENTS_PER_CARD) return; // skip excess events
              if (count === HISTORY_MAX_OUTPUT_EVENTS_PER_CARD) {
                outText += "\n⚠️ Output truncated (too many events).";
              }
            }
            // Cap output text during history replay to prevent DOM explosion
            // from pre-cap DB records with unlimited command output.
            if (isHistoryLoad && outText.length > HISTORY_SKILL_OUTPUT_CAP) {
              outText = outText.slice(0, HISTORY_SKILL_OUTPUT_CAP) + "\n⚠️ Output truncated for display.";
            }
            appendSkillCardOutput(card, outText);
          }
        } else if (evt.type === "skill_end") {
          if (evt.name === "ask_user") continue;
          const card = cards[evt.id];
          if (card) finishSkillCard(card, evt);

          // Reset group so the next skill_start creates a fresh stack
          // (matches streaming behavior where each command gets its own group)
          group = null;
        } else if (evt.type === "permission_request") {
          // History replay: decision already made — show static note, not interactive banner
          const note = document.createElement('div');
          note.className = 'approval-pending-note';
          note.textContent = '🔒 Permission was requested: ' + (evt.data?.command || evt.name || '').slice(0, 60);
          if (pane) {
            const turn = pane.querySelector('.turn:last-child');
            (turn || pane).appendChild(note);
          }
        } else if (evt.type === "cwd_warning") {
          // History replay: decision already made — show static note
          const note = document.createElement('div');
          note.className = 'cwd-warning-pending-note';
          note.textContent = '⚠️ CWD warning: ' + (evt.data?.path || '').slice(0, 80);
          if (pane) {
            const turn = pane.querySelector('.turn:last-child');
            (turn || pane).appendChild(note);
          }
        } else if (evt.type === "critique_start") {
          // History replay: render completed critique box
          const box = document.createElement('div');
          box.className = 'critique-box';
          box.dataset.critiqueId = evt.id || '';
          box.innerHTML = `
            <div class="critique-header">
              <div class="critique-header-left">
                <span class="critique-icon"><i data-lucide="search-check"></i></span>
                <span class="critique-title">Critique Session</span>
                <span class="critique-badge" data-focus="${escHtml(evt.focus || 'general')}">${escHtml(evt.focus || 'general')}</span>
              </div>
              <div class="critique-status"><span style="color:var(--ok)">Complete</span></div>
            </div>
            <details class="critique-details">
              <summary>Context & Criteria</summary>
              <div class="critique-details-body">
                <div class="critique-section">
                  <div class="critique-label">Context</div>
                  <div class="critique-text">${escHtml(evt.context || '')}</div>
                </div>
                <div class="critique-section">
                  <div class="critique-label">Criteria</div>
                  <div class="critique-text">${escHtml(evt.criteria || '')}</div>
                </div>
              </div>
            </details>
            <div class="critique-log"></div>
          `;
          pane.appendChild(box);
          _actIcons(box);
          cards[evt.id] = box;  // reuse cards map for critique_tool/done lookup
        } else if (evt.type === "critique_tool") {
          const box = cards[evt.id];
          if (box) {
            const log = box.querySelector('.critique-log');
            if (log) {
              const entry = document.createElement('div');
              entry.className = 'critique-tool-entry';
              entry.innerHTML = `<span class="critique-tool-name">${escHtml(evt.tool || '')}</span><span class="critique-tool-output">${escHtml((evt.output || '').slice(0, 200))}</span>`;
              log.appendChild(entry);
            }
          }
        } else if (evt.type === "critique_done") {
          const box = cards[evt.id];
          if (box) {
            const statusEl = box.querySelector('.critique-status');
            if (evt.error) {
              if (statusEl) statusEl.innerHTML = '<span style="color:var(--error)">Failed</span>';
            } else {
              if (statusEl) statusEl.innerHTML = '<span style="color:var(--ok)">Complete</span>';
              const log = box.querySelector('.critique-log');
              if (log) {
                log.className = 'critique-report';
                log.innerHTML = renderMarkdown(evt.report || '');
              }
              box.classList.add('critique-done');
            }
          }
        } else if (evt.type === "agent_result") {
          if (typeof addAgentResultCard === "function") {
            addAgentResultCard({
              type: evt.ok ? "agent_completed" : "agent_failed",
              agent_id: evt.agent_id,
              data: evt.data || {},
            });
          }
          // Clear streaming flag so the sidebar running-dot drops immediately.
          if (typeof window._finishAgentStream === "function") window._finishAgentStream(evt.agent_id);
          if (typeof window._sableLoadChats === "function") window._sableLoadChats();
        } else if (evt.type === "file_edit") {
          handleFileEdit(evt, false);
          window.dispatchEvent(new CustomEvent("sable:file-edit", { detail: evt }));
        } else if (evt.type === "memory_used") {
          if (Array.isArray(evt.memories) && evt.memories.length) {
            const chip = createMemoryChip(evt.memories);
            chip.classList.add("memory-chip-tool");
            const allCards = pane.querySelectorAll(".skill-card");
            const target = allCards.length ? allCards[allCards.length - 1] : null;
            if (target) {
              const right = target.querySelector(".skill-header-right");
              if (right) right.insertBefore(chip, right.firstChild);
              else target.querySelector(".skill-header")?.appendChild(chip);
            } else {
              pane.appendChild(chip);
            }
          }
        }
      }
    }

    async function _lazyLoadSkillEvents(messageId, chatId, pane) {
      try {
        const data = await fetch(`/api/chats/${chatId}/messages/${messageId}/events`).then(r => r.json());
        const events = data.skill_events || [];
        if (events.length > 0) {
          _renderSkillEvents(events, pane);
        }
      } catch (err) {
        console.error("Failed to lazy-load skill events:", err);
      }
    }

    // ── Ask User MCQ Card ──
    // ── ask_user batching ──
    // Multiple ask_user cards can arrive in one round. The user must answer
    // ALL of them before the model gets any feedback — otherwise the model
    // replies to the first answer while the rest are still unanswered.
    // Mirrors the _approvalBatches pattern above.
    const _askBatches = new Map(); // chatId → { pending: Set, answers: Map<cardId, string> }
    let _askCardSeq = 0;

    function _askBatchKey() {
      return (typeof streamChatId !== "undefined" && streamChatId) || (typeof activeChatId !== "undefined" && activeChatId) || "default";
    }

    function _getOrCreateAskBatch() {
      const k = _askBatchKey();
      if (!_askBatches.has(k)) _askBatches.set(k, { pending: new Set(), answers: new Map() });
      return _askBatches.get(k);
    }

    function _registerAskCard(cardId) {
      const b = _getOrCreateAskBatch();
      b.pending.add(cardId);
      b.answers.delete(cardId);
      _updateAskProgress();
    }

    function _resolveAskCard(cardId, answer) {
      const b = _getOrCreateAskBatch();
      b.pending.delete(cardId);
      b.answers.set(cardId, answer);
      _updateAskProgress();

      // All answered? Send one combined user turn.
      if (b.pending.size === 0 && b.answers.size > 0) {
        const ordered = Array.from(b.answers.values());
        _askBatches.delete(_askBatchKey());
        const combined = ordered.length === 1
          ? ordered[0]
          : ordered.map((a, i) => `${i + 1}. ${a}`).join("\n");
        inputEl.value = combined;
        sendMessage();
      }
    }

    // ── Native rich UI block renderer ──
    // A `ui_block` event carries the parsed payload. Draw it inline in the
    // message flow — no tool card, no skill chip. Used by both the live
    // stream and history replay.
    function renderUiBlock(payload, container) {
      if (!payload || !container || !window.SableComponents) return null;
      try {
        const block = document.createElement("div");
        block.className = "msg bot";
        const content = document.createElement("div");
        content.className = "md-content";
        block.appendChild(content);
        container.appendChild(block);
        window.SableComponents.render(payload, content);
        _actIcons(content);
        return block;
      } catch (e) {
        return null;
      }
    }

    // ── Interactivity bridge for UI-block nodes ──
    // render-components.js lives in its own IIFE and can't see inputEl /
    // sendMessage / isStreaming. It calls through this small, defensive
    // surface so a click inside a rendered component becomes a real turn.
    window.SableInteract = {
      send(text) {
        try {
          if (typeof isStreaming === "function" && isStreaming()) return false;
          if (!inputEl) return false;
          inputEl.value = String(text == null ? "" : text);
          sendMessage();
          return true;
        } catch (e) { return false; }
      },
      fill(text) {
        try {
          if (!inputEl) return false;
          inputEl.value = String(text == null ? "" : text);
          if (typeof inputEl.focus === "function") inputEl.focus();
          return true;
        } catch (e) { return false; }
      },
      busy() {
        try { return typeof isStreaming === "function" ? !!isStreaming() : false; }
        catch (e) { return false; }
      },
    };

    function _updateAskProgress() {
      const b = _getOrCreateAskBatch();
      const total = b.pending.size + b.answers.size;
      document.querySelectorAll(".ask-user-card").forEach((card) => {
        if (card.classList.contains("answered")) return;
        let hint = card.querySelector(".ask-user-batch-hint");
        if (total > 1) {
          if (!hint) {
            hint = document.createElement("div");
            hint.className = "ask-user-batch-hint";
            card.appendChild(hint);
          }
          hint.textContent = `${b.answers.size}/${total} answered — answer all to send`;
        } else if (hint) {
          hint.remove();
        }
      });
    }

    function renderAskUserCard(payload, container) {
      const { question, options, multi, default: def } = payload;
      const card = document.createElement("div");
      card.className = "ask-user-card";
      const cardId = "ask-" + (++_askCardSeq);
      card.dataset.askId = cardId;

      const qEl = document.createElement("div");
      qEl.className = "ask-user-question";
      qEl.textContent = question;
      card.appendChild(qEl);

      const optWrap = document.createElement("div");
      optWrap.className = "ask-user-options";
      card.appendChild(optWrap);

      const manualWrap = document.createElement("div");
      manualWrap.className = "ask-user-manual";
      manualWrap.style.display = "none";
      manualWrap.innerHTML = `<input type="text" placeholder="Type your answer…" /><button class="ask-user-submit">Send</button>`;
      card.appendChild(manualWrap);

      function submitAnswer(answer) {
        card.classList.add("answered");
        card.querySelectorAll("button").forEach(b => b.disabled = true);
        const chosen = document.createElement("div");
        chosen.className = "ask-user-chosen";
        chosen.textContent = "→ " + answer;
        card.appendChild(chosen);
        const hint = card.querySelector(".ask-user-batch-hint");
        if (hint) hint.remove();
        _resolveAskCard(cardId, answer);
      }

      options.forEach((opt, i) => {
        const btn = document.createElement("button");
        btn.className = "ask-user-opt" + (i === def ? " default" : "");
        btn.textContent = opt;
        btn.addEventListener("click", () => {
          if (i === options.length - 1) {
            // Last option = manual escape hatch
            manualWrap.style.display = "flex";
            manualWrap.querySelector("input").focus();
            return;
          }
          submitAnswer(opt);
        });
        optWrap.appendChild(btn);
      });

      manualWrap.querySelector(".ask-user-submit").addEventListener("click", () => {
        const val = manualWrap.querySelector("input").value.trim();
        if (val) submitAnswer(val);
      });
      manualWrap.querySelector("input").addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          const val = e.target.value.trim();
          if (val) submitAnswer(val);
        }
      });

      container.appendChild(card);
      _registerAskCard(cardId);
      scrollBottom();
      return card;
    }

// ── Permission Approval Queue ──
    // Batch-aware approval system: multiple permission requests in one round
    // are queued as individual cards. The model only receives combined feedback
    // after ALL items in the batch are resolved.

    // Queue state per chat: chatId → { pendingIds: Set, results: Map<id, {approved, feedback}>, total: number, cid: string }
    const _approvalBatches = new Map();

    function _getOrCreateBatch(chatId) {
      if (!_approvalBatches.has(chatId)) {
        _approvalBatches.set(chatId, { pendingIds: new Set(), results: new Map(), total: 0, cid: chatId });
      }
      return _approvalBatches.get(chatId);
    }

    // Called when the backend emits a permission_batch event listing all IDs for this round
    function initApprovalBatch(permissionIds, cwdIds, chatId) {
      const batch = _getOrCreateBatch(chatId);
      for (const id of permissionIds) batch.pendingIds.add(id);
      for (const id of cwdIds) batch.pendingIds.add(id);
      batch.total = batch.pendingIds.size;
      _updateBatchProgress(chatId);
    }

    // Mark one item as resolved and check if the whole batch is done
    async function _resolveBatchItem(tagId, approved, feedback, chatId) {
      const batch = _approvalBatches.get(chatId);
      if (!batch) return;
      batch.pendingIds.delete(tagId);
      batch.results.set(tagId, { approved, feedback });
      _updateBatchProgress(chatId);

      // If all items resolved, send combined feedback to model
      if (batch.pendingIds.size === 0 && batch.results.size > 0) {
        const allResults = Array.from(batch.results.values());
        _approvalBatches.delete(chatId);

        try {
          const resp = await fetch('/api/skills/batch-complete/' + encodeURIComponent(chatId), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ results: allResults }),
          });
          const data = await resp.json();
          if (data.ok && data.combined_feedback) {
            setTimeout(() => sendAutoTurnMessage(
              data.combined_feedback,
              { skipUserBubble: true, skipUserSave: true, targetChatId: chatId },
            ), 300);
          }
        } catch (e) {
          console.error('[approval-batch] Failed to complete batch:', e);
          // Fallback: send results individually
          const fallback = allResults.map(r => r.feedback).join('\n\n');
          setTimeout(() => sendAutoTurnMessage(
            fallback || '[System: All commands resolved.]',
            { skipUserBubble: true, skipUserSave: true, targetChatId: chatId },
          ), 300);
        }
      }
    }

    function _updateBatchProgress(chatId) {
      const batch = _approvalBatches.get(chatId);
      const container = document.getElementById('approvalQueueContainer');
      if (!container || !batch) return;
      const progressEl = container.querySelector('.aq-progress');
      if (progressEl) {
        const resolved = batch.results.size;
        const total = batch.total || (resolved + batch.pendingIds.size);
        progressEl.textContent = `${resolved}/${total} resolved`;
      }
    }

    // Resolve the pane + chat a banner action belongs to.
    function _bannerTarget(chatId) {
      const cid = chatId || activeChatId;
      const pane = (window._sableEnsurePane && cid)
        ? window._sableEnsurePane(cid)
        : activePane;
      return { cid, pane };
    }

    // Append a live "running" skill card to the target pane.
    function _appendRunningCard(pane, name, content) {
      const card = createSkillCard({ name: name, data: { content: content } });
      const st = card.querySelector('.skill-status');
      if (st) { st.textContent = 'running…'; st.style.color = ''; }
      const turn = pane ? pane.querySelector('.turn:last-child') : null;
      const target = turn
        ? (turn.querySelector('.skill-stack:last-of-type') || turn)
        : (pane ? pane.querySelector('.messages') : null);
      if (target) { target.appendChild(card); activateLucideIcons(card); }
      pane?.querySelector('.messages')?.scrollTo({ top: 999999, behavior: 'smooth' });
      return card;
    }

    // Ensure the queue container exists inside the approval banner area
    function _ensureQueueContainer() {
      let container = document.getElementById('approvalQueueContainer');
      if (!container) {
        const banner = document.getElementById('approvalBanner');
        if (!banner) return null;
        // Repurpose the banner as our queue container
        container = document.createElement('div');
        container.id = 'approvalQueueContainer';
        container.className = 'approval-queue-container';
        container.innerHTML = '<div class="aq-header"><span class="aq-title">⏳ Pending Approvals</span><span class="aq-progress"></span></div><div class="aq-cards"></div>';
        banner.parentNode.insertBefore(container, banner);
        banner.classList.add('hidden'); // Hide old single banner
      }
      container.classList.remove('hidden');
      return container;
    }

    function _hideQueueIfEmpty() {
      const container = document.getElementById('approvalQueueContainer');
      if (!container) return;
      const cards = container.querySelector('.aq-cards');
      if (!cards || cards.children.length === 0) {
        container.classList.add('hidden');
      }
    }

    function renderApprovalCard(evt, container, chatId) {
      const { id, name, data } = evt;
      const { command, category, reason } = data;
      const { cid, pane } = _bannerTarget(chatId);

      const catIcons = {
        filesystem: 'trash-2', packages: 'package', services: 'settings',
        git: 'git-branch', network: 'globe', auth: 'shield',
        disk: 'hard-drive', process: 'cpu', database: 'database', system: 'terminal'
      };
      const icon = catIcons[category] || 'alert-triangle';
      const shortCmd = command.length > 80 ? command.slice(0, 80) + '…' : command;

      const qContainer = _ensureQueueContainer();
      if (!qContainer) return;
      const cardsDiv = qContainer.querySelector('.aq-cards');

      // Don't duplicate if already rendered
      if (cardsDiv.querySelector(`[data-tag-id="${id}"]`)) return;

      // Auto-register in batch if not tracked yet (single-item fallback)
      const batch = _getOrCreateBatch(cid);
      if (!batch.pendingIds.has(id) && !batch.results.has(id)) {
        batch.pendingIds.add(id);
        batch.total = batch.pendingIds.size + batch.results.size;
        _updateBatchProgress(cid);
      }

      const card = document.createElement('div');
      card.className = 'approval-banner aq-card';
      card.dataset.tagId = id;
      card.dataset.chatId = cid || '';
      card.innerHTML = `
        <div class="ab-icon"><i data-lucide="${icon}"></i></div>
        <div class="ab-body">
          <div class="ab-title">${shortCmd.replace(/</g,'&lt;').replace(/>/g,'&gt;')}</div>
          <div class="ab-sub">${category} · ${reason}</div>
        </div>
        <div class="ab-actions">
          <button class="ab-allow"><i data-lucide="check"></i> Allow</button>
          <button class="ab-allow-session"><i data-lucide="shield-check"></i> Session</button>
          <button class="ab-deny"><i data-lucide="x"></i> Deny</button>
        </div>
      `;
      cardsDiv.appendChild(card);
      activateLucideIcons(card);

      const allowBtn = card.querySelector('.ab-allow');
      const allowSessionBtn = card.querySelector('.ab-allow-session');
      const denyBtn = card.querySelector('.ab-deny');

      function _disableButtons() {
        allowBtn.disabled = true;
        allowSessionBtn.disabled = true;
        denyBtn.disabled = true;
      }

      allowBtn.addEventListener('click', async () => {
        _disableButtons();
        pane?.querySelectorAll('.approval-pending-note').forEach(el => el.remove());
        const runCard = _appendRunningCard(pane, name, command);
        const t0 = performance.now();
        try {
          const resp = await fetch('/api/skills/approve/' + id, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ chat_id: cid }),
          });
          const res = await resp.json();
          if (res.ok) {
            const feedback = res.feedback ? String(res.feedback) : '';
            const out = runCard.querySelector('.skill-output');
            if (out && feedback) out.textContent = feedback.slice(0, 4000);
            finishSkillCard(runCard, { name, ok: true, duration_ms: Math.round(performance.now() - t0), result: {} });
            card.classList.add('aq-resolved');
            card.remove();
            _hideQueueIfEmpty();
            await _resolveBatchItem(id, true, feedback || '[Command approved and executed.]', cid);
          } else {
            finishSkillCard(runCard, { name, ok: false, duration_ms: Math.round(performance.now() - t0), error: res.error || 'expired' });
            _disableButtons.call(null); // Re-enable on failure
            allowBtn.disabled = false; allowSessionBtn.disabled = false; denyBtn.disabled = false;
          }
        } catch (e) {
          finishSkillCard(runCard, { name, ok: false, duration_ms: Math.round(performance.now() - t0), error: String(e) });
          allowBtn.disabled = false; allowSessionBtn.disabled = false; denyBtn.disabled = false;
        }
      });

      allowSessionBtn.addEventListener('click', async () => {
        _disableButtons();
        pane?.querySelectorAll('.approval-pending-note').forEach(el => el.remove());
        const runCard = _appendRunningCard(pane, name, command);
        const t0 = performance.now();
        try {
          const resp = await fetch('/api/skills/approve/' + id, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ chat_id: cid, session: true }),
          });
          const res = await resp.json();
          if (res.ok) {
            const feedback = res.feedback ? String(res.feedback) : '';
            const out = runCard.querySelector('.skill-output');
            if (out && feedback) out.textContent = feedback.slice(0, 4000);
            finishSkillCard(runCard, { name, ok: true, duration_ms: Math.round(performance.now() - t0), result: {} });
            card.classList.add('aq-resolved');
            card.remove();
            _hideQueueIfEmpty();
            await _resolveBatchItem(id, true, feedback || '[Command approved for session and executed.]', cid);
          } else {
            finishSkillCard(runCard, { name, ok: false, duration_ms: Math.round(performance.now() - t0), error: res.error || 'expired' });
            allowBtn.disabled = false; allowSessionBtn.disabled = false; denyBtn.disabled = false;
          }
        } catch (e) {
          finishSkillCard(runCard, { name, ok: false, duration_ms: Math.round(performance.now() - t0), error: String(e) });
          allowBtn.disabled = false; allowSessionBtn.disabled = false; denyBtn.disabled = false;
        }
      });

      denyBtn.addEventListener('click', async () => {
        _disableButtons();
        const runCard = _appendRunningCard(pane, name, command);
        const st = runCard.querySelector('.skill-status');
        if (st) { st.textContent = 'denied ✗'; st.style.color = 'var(--danger)'; }
        const out = runCard.querySelector('.skill-output');
        if (out) out.textContent = '[denied by user]';
        try {
          const r = await fetch('/api/skills/deny/' + id, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ chat_id: cid }),
          });
          const res = await r.json();
          card.classList.add('aq-resolved');
          card.remove();
          _hideQueueIfEmpty();
          await _resolveBatchItem(id, false, res.feedback || '[Command denied by user.]', cid);
        } catch (e) {
          card.classList.add('aq-resolved');
          card.remove();
          _hideQueueIfEmpty();
          await _resolveBatchItem(id, false, '[Command denied by user.]', cid);
        }
      });
    }

    function renderCwdWarningCard(evt, container, chatId) {
      const { id, name, data } = evt;
      const { path, cwd } = data;
      const { cid, pane } = _bannerTarget(chatId);

      const shortPath = path.length > 80 ? '…' + path.slice(-77) : path;

      const qContainer = _ensureQueueContainer();
      if (!qContainer) return;
      const cardsDiv = qContainer.querySelector('.aq-cards');

      if (cardsDiv.querySelector(`[data-tag-id="${id}"]`)) return;

      // Auto-register in batch if not tracked yet (single-item fallback)
      const batch = _getOrCreateBatch(cid);
      if (!batch.pendingIds.has(id) && !batch.results.has(id)) {
        batch.pendingIds.add(id);
        batch.total = batch.pendingIds.size + batch.results.size;
        _updateBatchProgress(cid);
      }

      const card = document.createElement('div');
      card.className = 'approval-banner cwd-warning-banner aq-card';
      card.dataset.tagId = id;
      card.dataset.chatId = cid || '';
      card.innerHTML = `
        <div class="ab-icon"><i data-lucide="folder-alert"></i></div>
        <div class="ab-body">
          <div class="ab-title">File operation outside project folder</div>
          <div class="ab-sub">${shortPath.replace(/</g,'&lt;').replace(/>/g,'&gt;')}</div>
          <div class="ab-detail">Without making it the project folder, you can't recover in case of accidental damage.</div>
        </div>
        <div class="ab-actions">
          <button class="ab-cwd-session"><i data-lucide="shield-check"></i> Session</button>
          <button class="ab-cwd-continue"><i data-lucide="arrow-right"></i> Continue</button>
          <button class="ab-cwd-open"><i data-lucide="folder-open"></i> Open Folder</button>
          <button class="ab-cwd-deny"><i data-lucide="x"></i> Deny</button>
        </div>
      `;
      cardsDiv.appendChild(card);
      activateLucideIcons(card);

      const sessionBtn = card.querySelector('.ab-cwd-session');
      const continueBtn = card.querySelector('.ab-cwd-continue');
      const openBtn = card.querySelector('.ab-cwd-open');
      const denyBtn = card.querySelector('.ab-cwd-deny');

      function _disableButtons() {
        [sessionBtn, continueBtn, openBtn, denyBtn].forEach(b => { if (b) b.disabled = true; });
      }

      async function _approveWithSession(session) {
        _disableButtons();
        pane?.querySelectorAll('.cwd-warning-pending-note').forEach(el => el.remove());
        const runCard = _appendRunningCard(pane, name, path);
        const t0 = performance.now();
        try {
          const resp = await fetch('/api/skills/cwd-approve/' + id, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ chat_id: cid, session }),
          });
          const res = await resp.json();
          if (res.ok) {
            const out = runCard.querySelector('.skill-output');
            if (out && res.feedback) out.textContent = String(res.feedback).slice(0, 4000);
            finishSkillCard(runCard, { name, ok: true, duration_ms: Math.round(performance.now() - t0), result: {} });
            card.classList.add('aq-resolved');
            card.remove();
            _hideQueueIfEmpty();
            await _resolveBatchItem(id, true, res.feedback || '[File operation approved.]', cid);
          } else {
            finishSkillCard(runCard, { name, ok: false, duration_ms: Math.round(performance.now() - t0), error: res.error || 'error' });
            [sessionBtn, continueBtn, openBtn, denyBtn].forEach(b => { if (b) b.disabled = false; });
          }
        } catch (e) {
          finishSkillCard(runCard, { name, ok: false, duration_ms: Math.round(performance.now() - t0), error: String(e) });
          [sessionBtn, continueBtn, openBtn, denyBtn].forEach(b => { if (b) b.disabled = false; });
        }
      }

      sessionBtn?.addEventListener('click', () => _approveWithSession(true));
      continueBtn?.addEventListener('click', () => _approveWithSession(false));

      openBtn?.addEventListener('click', async () => {
        _disableButtons();
        try {
          const res = await fetch('/api/filesystem/pick-folder');
          const pickData = await res.json();
          if (pickData.path && window.pickFsRoot) {
            window.pickFsRoot(pickData.path);
            await _approveWithSession(false);
          } else {
            [sessionBtn, continueBtn, openBtn, denyBtn].forEach(b => { if (b) b.disabled = false; });
          }
        } catch (e) {
          [sessionBtn, continueBtn, openBtn, denyBtn].forEach(b => { if (b) b.disabled = false; });
        }
      });

      denyBtn?.addEventListener('click', async () => {
        _disableButtons();
        const runCard = _appendRunningCard(pane, name, path);
        const st = runCard.querySelector('.skill-status');
        if (st) { st.textContent = 'denied ✗'; st.style.color = 'var(--danger)'; }
        const out = runCard.querySelector('.skill-output');
        if (out) out.textContent = '[denied by user]';
        try {
          const resp = await fetch('/api/skills/cwd-deny/' + id, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ chat_id: cid }),
          });
          const result = await resp.json();
          card.classList.add('aq-resolved');
          card.remove();
          _hideQueueIfEmpty();
          await _resolveBatchItem(id, false, result.feedback || '[File operation denied by user.]', cid);
        } catch (e) {
          card.classList.add('aq-resolved');
          card.remove();
          _hideQueueIfEmpty();
          await _resolveBatchItem(id, false, '[File operation denied by user.]', cid);
        }
      });
    }

    // Re-sync the approval queue with whatever is still pending for a chat.
    // Called on tab switch so permissions asked in chat A survive a detour
    // to chat B and come back. Now renders ALL pending items, not just the first.
    async function refreshPendingBanner(chatId) {
      if (!chatId) {
        const container = document.getElementById('approvalQueueContainer');
        if (container) container.classList.add('hidden');
        return;
      }
      try {
        const resp = await fetch('/api/skills/pending/' + encodeURIComponent(chatId));
        if (!resp.ok) return;
        const data = await resp.json();
        if (activeChatId !== chatId) return; // switched away mid-fetch

        const approvals = data.approvals || [];
        const cwdWarnings = data.cwd_warnings || [];

        if (approvals.length === 0 && cwdWarnings.length === 0) {
          const container = document.getElementById('approvalQueueContainer');
          if (container) container.classList.add('hidden');
          return;
        }

        // Re-initialize batch tracker for rehydrated items.
        // Preserve already-resolved results from the existing batch (if any)
        // since those tools already executed on the backend but haven't been
        // sent to the model yet.
        const existingBatch = _approvalBatches.get(chatId);
        const preservedResults = existingBatch ? existingBatch.results : new Map();
        _approvalBatches.delete(chatId);
        initApprovalBatch(
          approvals.map(a => a.id),
          cwdWarnings.map(c => c.id),
          chatId,
        );
        // Restore previously resolved results
        const freshBatch = _approvalBatches.get(chatId);
        if (freshBatch && preservedResults.size > 0) {
          for (const [id, result] of preservedResults) {
            freshBatch.results.set(id, result);
          }
          freshBatch.total = freshBatch.pendingIds.size + freshBatch.results.size;
          _updateBatchProgress(chatId);
        }

        // Render all pending approval cards
        for (const appr of approvals) {
          renderApprovalCard(
            { id: appr.id, name: appr.name, data: { command: appr.command, category: appr.category, reason: appr.reason } },
            activePane, chatId,
          );
        }
        // Render all pending CWD warning cards
        for (const cwd of cwdWarnings) {
          renderCwdWarningCard(
            { id: cwd.id, name: cwd.name, data: { path: cwd.path, cwd: cwd.cwd } },
            activePane, chatId,
          );
        }
      } catch (e) { /* queue stays hidden on failure */ }
    }
    window._sableRefreshPendingBanner = refreshPendingBanner;

    // one "turn" holds everything for a single response: thinking, then any
    // skill/tool runs it made, then the final answer — all stacked in order,
    // scoped to just this response (not shared globally).
    // ── Response speed badge (tok/s + total time) ──────────────────────
    // Consumes the timing object stashed on the turn by addBotStreaming().
    function attachSpeedBadge(toolbar, turn) {
      if (!toolbar || !turn || toolbar.querySelector(".msg-stats")) return;
      const s = turn._sableStats;
      if (!s || !s.t0) return;
      if (!s.tEnd) s.tEnd = performance.now();   // ordering-safe fallback

      const md = turn.querySelector(".msg.bot .md-content");
      const chars = md ? md.innerText.trim().length : 0;
      if (chars < 24) return;   // skip empty / trivial replies

      const totalMs = s.tEnd - s.t0;
      const totalS = totalMs / 1000;

      // Token-weighted mean of per-segment speeds. A flat average let a
      // 30-char segment at 20 tok/s count as much as a 500-char segment at
      // 140 tok/s, dragging the result down — and tool-heavy turns produce
      // more short segments, so the bias hit exactly that case.
      const bursts = s.bursts || [];
      const measurable = bursts.length > 0;
      let activeMs = 0;
      let tokens = 0;
      let weighted = 0;
      for (let i = 0; i < bursts.length; i++) {
        activeMs += bursts[i].ms;
        tokens += bursts[i].tokens;
        weighted += bursts[i].tokens * bursts[i].tps;
      }
      const tps = tokens > 0 ? weighted / tokens : 0;
      const activeS = activeMs / 1000;
      const burstCount = bursts.length;

      // Label is honest about what was measured. When the whole reply arrived
      // in one or two chunks there is no emission signal to measure, so we
      // show the timing alone rather than inventing a speed.
      const label = measurable
        ? `${tps.toFixed(1)} tok/s \u00b7 ${totalS.toFixed(1)}s`
        : `${totalS.toFixed(1)}s`;

      const badge = document.createElement("span");
      badge.className = "msg-stats";
      badge.innerHTML = `<span class="msg-stats-icon">${lucideIcon("\u26a1")}</span>${label}`;
      badge.title =
        `Tokens: ~${tokens} (${chars} chars / 4)\n` +
        `Speed: ${tps.toFixed(1)} tok/s\n` +
        `Text time: ${activeS.toFixed(2)}s in ${burstCount} segment${burstCount === 1 ? "" : "s"}\n` +
        `Total: ${totalS.toFixed(2)}s` +
        (s.tFirst ? `\nFirst token: ${((s.tFirst - s.t0) / 1000).toFixed(2)}s` : "");
      toolbar.appendChild(badge);

      // Only feed the adaptive typewriter when we actually measured something.
      // A tps of 0 is rejected downstream anyway (twRecord guards tps<=0 and
      // the server requires tps>0), but skipping keeps the intent explicit.
      if (measurable) {
        if (typeof twRecord === "function") twRecord(selectedModel || "unknown", tps);
        try {
          fetch("/api/stats/response", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              model: selectedModel || "unknown",
              tokens,
              gen_ms: Math.round(activeMs),
              total_ms: Math.round(totalMs),
              tps,
            }),
          }).catch(() => {});
        } catch (_) { /* never let stats break the UI */ }
      }
    }

    function addBotStreaming(pane = activePane, chatId = activeChatId) {
      clearPaneEmptyState(pane);

      // A fresh turn begins. Any ask_user card left unanswered from a previous
      // turn is stale — mark it visually and drop it from the batch so it can
      // never block the questions about to arrive in THIS turn.
      (function _sweepStaleAskCards() {
        const b = _askBatches.get(_askBatchKey());
        if (b && b.pending.size) {
          document.querySelectorAll(".ask-user-card").forEach((card) => {
            if (card.classList.contains("answered") || card.classList.contains("stale")) return;
            card.classList.add("stale");
            card.querySelectorAll("button").forEach((btn) => { btn.disabled = true; });
            if (!card.querySelector(".ask-user-stale-note")) {
              const note = document.createElement("div");
              note.className = "ask-user-stale-note";
              note.textContent = "skipped — a newer message was sent";
              card.appendChild(note);
            }
            const hint = card.querySelector(".ask-user-batch-hint");
            if (hint) hint.remove();
          });
        }
        _askBatches.delete(_askBatchKey());
      })();

      // Capture the chat this turn belongs to — typewriter ticks and scroll
      // calls will bail if the user has switched away before they fire.
      const turnChatId = chatId;

      // `let`, not `const`: a queued user message delivered mid-stream seals this
      // turn and rolls a fresh one out below the user bubble (see
      // injectUserMessage). Every closure here shares the binding, so reassigning
      // it moves subsequent renders to the new turn automatically.
      let turn = document.createElement("div");
      turn.className = "turn";
      pane.appendChild(turn);
      // Turns abandoned by a mid-stream user injection. finalize() has to sweep
      // these too — they hold a real, user-visible answer that still needs a
      // toolbar, a speed badge and a settled pending-card state.
      const _sealedTurns = [];
      // True once a queued message has been injected. finalize() uses it to tell
      // "the model genuinely returned nothing" apart from "the answer lives in
      // a sealed turn now", so it doesn't fire a false Empty-response error.
      let _injected = false;
      // skillRounds is a flat log across the whole stream. On injection the retry
      // bar must only consider rounds from the CURRENT turn, otherwise it stitches
      // pre- and post-injection commands into one bogus retry payload.
      let _skillRoundFloor = 0;

      // ── Response timing ────────────────────────────────────────────────
      // A turn may be several separate text segments separated by tool calls.
      // Each segment is measured on its own: start when text begins, end when
      // a tool starts (or the stream ends). Speed per segment = tokens / its
      // own duration. Final speed = average of all segment speeds.
      // `let`: a mid-stream user injection starts a fresh stats window for the
      // new turn (see injectUserMessage) so the speed badge doesn't inherit the
      // pre-injection start time.
      let _stats = { t0: performance.now(), tFirst: 0, tEnd: 0, bursts: [] };
      turn._sableStats = _stats;
      let _burstStart = 0;
      let _burstChars = 0;

      function _closeBurst() {
        if (!_burstStart) return;
        const ms = performance.now() - _burstStart;
        _burstStart = 0;
        if (ms > 0 && _burstChars > 0) {
          const toks = Math.max(1, Math.round(_burstChars / 4));
          _stats.bursts.push({ tokens: toks, ms: ms, tps: toks / (ms / 1000) });
        }
        _burstChars = 0;
      }

      // Immediate feedback: create the backend status card right away with
      // a shimmering "processing…" label in the header.  The text stays
      // visible (even when collapsed) until real pipeline steps arrive or
      // the first content token hides it.
      const bsCard = document.createElement("div");
      bsCard.className = "skill-card backend-status-card";
      bsCard.innerHTML = `
        <div class="skill-header">
          <div class="skill-header-left">
            <span class="skill-name">${typeof _backendStatusIcon === "function" ? _backendStatusIcon("terminal", 15) : ""} Backend Status</span>
            <span class="bs-processing-text processing-text">processing…</span>
          </div>
          <div class="skill-header-right" style="display:flex;align-items:center;gap:8px;">
            <span class="skill-status bs-status">working…</span>
          </div>
        </div>
        <div class="bs-steps"></div>`;
      bsCard.querySelector(".skill-header").onclick = () => bsCard.classList.toggle("collapsed");
      turn.appendChild(bsCard);
      if (typeof activateLucideIcons === "function") activateLucideIcons(bsCard);

      let pendingShown = true;
      function hidePending() {
        if (!pendingShown) return;
        pendingShown = false;
        const procText = bsCard.querySelector(".bs-processing-text");
        if (procText) procText.style.display = "none";
        ensureAnswer();
      }

      // Per-round thinking: each agentic command gets its own thinking block
      // inserted right before its skill card, instead of one global bucket.
      let currentThinkWrap = null;
      let currentThinkBody = null;
      let currentThinkSummary = null;
      // Last thinking block of THIS round, kept so an early `answer` delta can
      // not fork the reasoning into a second card. DeepSeek interleaves THINK
      // and RESPONSE fragments, so the frontend's first `answer` event calls
      // closeThinking() while reasoning is still arriving; without this the
      // next `thinking` delta builds a duplicate card. Reset when a real new
      // round starts (a tool/skill card appears) so t1,c1,t2,c2 is preserved.
      let lastThinkWrap = null;
      let lastThinkBody = null;
      let lastThinkSummary = null;
      let _thinkRoundDone = true;  // true => a new round may open a fresh block

      // ── Typewriter animation for thinking reveal ──
      // Shares the adaptive tuning of the main answer typewriter (_twNow →
      // twTuneFor): rate scales to the model's measured tok/s, and fast models
      // flush instead of animating. rAF + fractional carry match _ansTick so
      // both reveals stay in lockstep at the same speed.
      let _thinkQueue = "";
      let _thinkTimer = null;      // rAF handle
      let _thinkLast = 0;          // timestamp of previous tick
      let _thinkFrac = 0;          // fractional char carry — see _thinkTick
      let _thinkLastPin = 0;       // last scroll-pin timestamp (throttled)

      function _thinkCancel() {
        if (_thinkTimer !== null) { cancelAnimationFrame(_thinkTimer); _thinkTimer = null; }
      }

      function _thinkStart() {
        if (_thinkTimer !== null) return;
        _thinkLast = performance.now();
        _thinkFrac = 0;
        _thinkTimer = requestAnimationFrame(_thinkTick);
      }

      function _thinkTick() {
        // Bail if user switched to a different chat while timer was pending
        if (turnChatId !== activeChatId) { _thinkTimer = null; return; }
        if (!_thinkQueue || !currentThinkBody) { _thinkTimer = null; return; }

        let tune = _twNow();
        // Thinking is a cheap textContent append (no markdown re-parse), so keep
        // the reveal animation alive even for fast models — just crank the rate
        // instead of dumping the whole block at once. The old flush here is what
        // made fast thinking pop in fully-formed ("frog jump") instead of typing.
        if (tune.flush) tune = { chars: TW_MAX_CHARS, ms: TW_MIN_MS, flush: false };

        // Rate-based consume mirroring _ansTick: chars/sec × elapsed seconds,
        // with a clamped dt and a fractional carry so we don't lose rate.
        const now = performance.now();
        const dt = Math.min(now - _thinkLast, 100);
        _thinkLast = now;
        let cps = ((tune.chars || TW_CHARS) / (tune.ms || TW_MS)) * 1000;
        // Backlog catch-up — same trick as _ansTick: drain a growing queue at a
        // steady, elevated rate rather than letting it dump in one burst.
        if (_thinkQueue.length > 0) cps = Math.max(cps, _thinkQueue.length / 0.2);
        _thinkFrac += cps * dt / 1000;
        const nChars = Math.floor(_thinkFrac);
        _thinkFrac -= nChars;
        if (nChars <= 0) {
          if (_thinkQueue) _thinkTimer = requestAnimationFrame(_thinkTick);
          else _thinkTimer = null;
          return;
        }
        const chunk = _thinkQueue.slice(0, nChars);
        _thinkQueue = _thinkQueue.slice(nChars);
        _thinkAppend(chunk);
        // Pin to the bottom at most ~10×/sec instead of every frame.
        //
        // `pinScrollIfAtBottom()` READS scrollHeight and WRITES scrollTop. The
        // read forces a synchronous layout, and doing that on every one of 60
        // frames/sec — right after mutating the text node — is a layout thrash
        // that grows with the block. The animation does not need 60Hz pinning:
        // the bottom is only visually stale for 100ms, and a final pin on the
        // last frame guarantees we end up exactly at the bottom.
        const _now = performance.now();
        if (_now - _thinkLastPin > 100) {
          _thinkLastPin = _now;
          pinScrollIfAtBottom();
          scrollBottom();
        }
        _thinkTimer = _thinkQueue ? requestAnimationFrame(_thinkTick) : null;
        if (!_thinkTimer) {
          // Final pin once the queue drains, so we never stop mid-scroll.
          pinScrollIfAtBottom();
          scrollBottom();
        }
      }

      function _enqueueThink(text) {
        _thinkQueue += text;
        _thinkStart();
      }

      /**
       * Append thinking text in O(chunk), not O(total).
       *
       * `currentThinkBody.textContent += chunk` re-serialises the ENTIRE
       * accumulated string on every write — O(n) per frame, O(n²) over a long
       * block. At 50k chars of reasoning that is ~3M character copies per
       * second plus a full text-node rebuild each frame, which is exactly the
       * "UI gets laggier and laggier" symptom (and why it looks wrong: the
       * content is plain text and should be cheap).
       *
       * Keeping one live text node and using appendData makes each write
       * O(chunk) and never touches the existing characters.
       */
      function _thinkAppend(text) {
        if (!currentThinkBody || !text) return;
        let tn = currentThinkBody._thinkTextNode;
        if (!tn || tn.parentNode !== currentThinkBody) {
          currentThinkBody.textContent = "";
          tn = document.createTextNode("");
          currentThinkBody.appendChild(tn);
          currentThinkBody._thinkTextNode = tn;
        }
        tn.appendData(text);
      }

      function _flushThinkQueue() {
        _thinkCancel();
        _thinkFrac = 0;
        if (_thinkQueue && currentThinkBody) {
          _thinkAppend(_thinkQueue);
          _thinkQueue = "";
        }
      }

      function ensureThinkingBlock() {
        // Already streaming into a block — nothing to do.
        if (currentThinkWrap) return;
        // SAME ROUND, block was only closed because an early `answer` delta
        // arrived mid-reasoning. Reuse it instead of forking a second card.
        // `_thinkRoundDone` is set true by addSkillStart()/nextSkillRound(),
        // i.e. only when a tool card opens a genuinely new round — so the
        // t1,c1,t2,c2 multi-round layout is untouched.
        if (!_thinkRoundDone && lastThinkWrap && lastThinkWrap.isConnected) {
          currentThinkWrap = lastThinkWrap;
          currentThinkBody = lastThinkBody;
          currentThinkSummary = lastThinkSummary;
          const det = currentThinkWrap.querySelector("details");
          if (det) det.open = true;
          if (currentThinkSummary) {
            currentThinkSummary.innerHTML = '<i data-lucide="chevron-right" class="thinking-chevron"></i>Thinking…';
            activateLucideIcons(currentThinkSummary);
          }
          return;
        }
        // Create a fresh thinking block for the current round.
        // It will be placed just before the next skill command group or answer.
        // A new thinking block means a new round — the commands that follow it
        // must land in a fresh stack placed right after this block, not piled
        // into a previous round's stack. Gives the t1,c1,t2,c2 layout.
        lastCommandGroup = null;
        currentThinkWrap = document.createElement("div");
        currentThinkWrap.className = "thinking-wrap";
        currentThinkWrap.innerHTML = `
          <details class="thinking" open>
            <summary><i data-lucide="chevron-right" class="thinking-chevron"></i>Thinking…</summary>
            <div class="thinking-body"></div>
          </details>`;
        currentThinkBody = currentThinkWrap.querySelector(".thinking-body");
        currentThinkSummary = currentThinkWrap.querySelector("summary");
        // Place the thinking block ABOVE the answer segment it belongs to.
        // appendThinking() calls hidePending() → ensureAnswer() BEFORE this,
        // so answerEl may already be in the DOM; appendChild would then drop
        // the thinking card BELOW the answer. Insert before it instead.
        if (answerEl && answerEl.parentNode === turn) {
          turn.insertBefore(currentThinkWrap, answerEl);
        } else {
          turn.appendChild(currentThinkWrap);
        }
        activateLucideIcons(currentThinkWrap);
        // Remember it so a mid-round answer delta can reattach instead of fork.
        lastThinkWrap = currentThinkWrap;
        lastThinkBody = currentThinkBody;
        lastThinkSummary = currentThinkSummary;
        _thinkRoundDone = false;
      }

      function closeCurrentThinking() {
        if (!currentThinkWrap) return;
        _flushThinkQueue();
        if (currentThinkSummary) { currentThinkSummary.innerHTML = '<i data-lucide="chevron-right" class="thinking-chevron"></i>Thinking'; activateLucideIcons(currentThinkSummary); }
        const det = currentThinkWrap.querySelector("details");
        if (det) det.open = false;
        // Keep the closed block referenced so reasoning that resumes in the
        // SAME round reuses it (see ensureThinkingBlock).
        lastThinkWrap = currentThinkWrap;
        lastThinkBody = currentThinkBody;
        lastThinkSummary = currentThinkSummary;
        currentThinkWrap = null;
        currentThinkBody = null;
        currentThinkSummary = null;
      }

      // A tool/skill card marks the end of the current reasoning round: the
      // NEXT thinking block must be a fresh card (t1,c1,t2,c2), never merged
      // into the previous round's.
      function markThinkRoundDone() {
        _thinkRoundDone = true;
      }

      const skillCards = {};
      let answerEl = null;
      let answerContent = null;
      let raw = "";
      let lastCommandGroup = null;
      let skillRounds = [[]];
      let sawNormalAnswer = false;
      let fileEditSummary = { count: 0, added: 0, removed: 0, card: null };
      let _tacExitTimer = null;

      function trackSkillEvent(evt) {
        skillRounds[skillRounds.length - 1].push(evt);
      }

      // Open a fresh text segment inside the current bubble. A turn is ONE
      // .msg.bot holding ordered .md-content segments (text / inline UI / text)
      // so the whole answer reads as a single coherent card.
      function _openAnswerSegment() {
        answerContent = document.createElement("div");
        answerContent.className = "md-content";
        answerEl.appendChild(answerContent);
        raw = "";
        _ansInFence = false;
      }
      function ensureAnswer() {
        if (answerEl) {
          if (!answerContent) _openAnswerSegment();
          return;
        }
        answerEl = document.createElement("div");
        answerEl.className = "msg bot streaming msg-enter";
        turn.appendChild(answerEl);
        answerContent = null;
        _openAnswerSegment();
        lastCommandGroup = null;
      }

      // ── Typewriter animation for answer reveal ──
      // Driven by requestAnimationFrame at ~60fps and rate-based: chars are
      // consumed proportional to elapsed time, not a fixed count per tick.
      // That makes the reveal framerate-independent AND smooth — a fixed
      // 5-chars-every-33ms reads as a word-by-word slideshow, while 2-3 chars
      // at 60fps reads as typing. rAF also auto-pauses in background tabs,
      // which removes the throttle-to-1000ms dump problem entirely.
      let _ansQueue = "";
      let _ansTimer = null;      // rAF handle
      let _ansLast = 0;          // timestamp of previous tick
      let _ansFrac = 0;          // fractional char carry — see _ansTick
      let _ansInFence = false;
      // Chars that can change markdown *structure* mid-line and therefore force
      // a full re-render. Deliberately excludes `-` and `#`: both are only
      // structural at line start, which is always preceded by \n (already in
      // the set). Keeping them meant every hyphenated word — "well-known",
      // "1-5", "e-mail" — triggered a full DOM rebuild, which was the main
      // source of streaming jank.
      const _ANS_STRUCTURAL_RE = /[\n`|<>*_~=\[]/;

      function _ansCancel() {
        if (_ansTimer !== null) { cancelAnimationFrame(_ansTimer); _ansTimer = null; }
      }

      function _ansStart() {
        if (_ansTimer !== null) return;
        _ansLast = performance.now();
        _ansFrac = 0;
        _ansTimer = requestAnimationFrame(_ansTick);
      }

      // Per-turn typewriter tuning. Reads the model's measured median tok/s
      // (loaded at init) and scales the reveal rate to match it.
      function _twNow() {
        return (typeof twTuneFor === "function")
          ? twTuneFor(selectedModel)
          : { chars: TW_CHARS, ms: TW_MS, flush: false };
      }

      function _ansTick() {
        // Bail if user switched to a different chat while timer was pending
        if (turnChatId !== activeChatId) { _ansTimer = null; return; }
        if (!_ansQueue || !answerContent) { _ansTimer = null; return; }

        let tune = _twNow();
        // Even at flush speeds keep the reveal paced. A full dump reads as a
        // "frog jump"; pure-prose chunks now append cheaply (see _flushAnswerQueue),
        // so animating the fast path is no longer expensive — just run at max rate.
        if (tune.flush) tune = { chars: TW_MAX_CHARS, ms: TW_MIN_MS, flush: false };

        // Rate-based consume: chars/sec × elapsed seconds. dt is clamped so a
        // backgrounded tab or long GC pause can't dump the whole queue in one
        // frame — we catch up smoothly over the next few frames instead.
        const now = performance.now();
        const dt = Math.min(now - _ansLast, 100);
        _ansLast = now;
        // Carry the fractional remainder between frames. Rounding 2.49 down to
        // 2 every frame silently loses 20% of the target rate, making the
        // animation lag the model and dump the tail. Accumulate instead.
        let cps = ((tune.chars || TW_CHARS) / (tune.ms || TW_MS)) * 1000;
        // Backlog catch-up: if the producer outruns the reveal, raise the rate so
        // we drain the queue smoothly (~8% of backlog per frame) instead of
        // letting it pile up and then dumping a whole burst. This is what keeps
        // the motion continuous — jiggle, not hop.
        if (_ansQueue.length > 0) cps = Math.max(cps, _ansQueue.length / 0.2);
        _ansFrac += cps * dt / 1000;
        const nChars = Math.floor(_ansFrac);
        _ansFrac -= nChars;
        if (nChars <= 0) {
          // Not enough accumulated for a char yet — keep the loop alive.
          if (_ansQueue) _ansTimer = requestAnimationFrame(_ansTick);
          else _ansTimer = null;
          return;
        }
        const chunk = _ansQueue.slice(0, nChars);
        _ansQueue = _ansQueue.slice(nChars);
        raw += chunk;

        // Fast path: plain text append — skip full markdown pipeline
        let fast = false;
        if (!_ansInFence && !_ANS_STRUCTURAL_RE.test(chunk)) {
          const lastP = answerContent.lastElementChild;
          if (lastP && lastP.tagName === "P" && lastP.lastChild && lastP.lastChild.nodeType === 3) {
            lastP.lastChild.textContent += chunk;
            fast = true;
          }
        }
        // Fast path: inside code fence — ALL chars are literal, no markdown processing.
        // Always append directly regardless of structural chars (fixes mermaid flicker).
        if (!fast && _ansInFence) {
          const codeEls = answerContent.querySelectorAll(".code-block pre code");
          const codeEl = codeEls[codeEls.length - 1];
          if (codeEl) {
            codeEl.textContent += chunk;
            // Detect fence closure → full re-render to finalize block
            if (!countOpenFences(raw).inFence) {
              _ansInFence = false;
              answerContent.innerHTML = renderMarkdown(raw);
              pinScrollIfAtBottom();
              answerContent.querySelectorAll(".mermaid-wrap").forEach(wrap => {
                const pre = wrap.querySelector("pre.mermaid");
                if (!pre) return;
                const code = pre.textContent;
                const div = document.createElement("div");
                div.className = "code-block";
                div.innerHTML = `<pre><code class="language-mermaid">${escHtml(code)}</code></pre>`;
                wrap.replaceWith(div);
              });
            }
            fast = true;
          }
        }
        if (!fast) {
          answerContent.innerHTML = renderMarkdown(raw);
          pinScrollIfAtBottom();
          // During streaming, neutralize mermaid-wrap to plain code (prevents flicker)
          answerContent.querySelectorAll(".mermaid-wrap").forEach(wrap => {
            const pre = wrap.querySelector("pre.mermaid");
            if (!pre) return;
            const code = pre.textContent;
            const div = document.createElement("div");
            div.className = "code-block";
            div.innerHTML = `<pre><code class="language-mermaid">${escHtml(code)}</code></pre>`;
            wrap.replaceWith(div);
          });
          _ansInFence = countOpenFences(raw).inFence;
        }

        scrollBottom();
        if (_ansQueue) { _ansTimer = requestAnimationFrame(_ansTick); }
        else { _ansTimer = null; }
        // Only activate lucide icons when chunk contains a data-lucide placeholder
        // or at end-of-stream — avoids calling createIcons() every 12ms tick
        if (chunk.includes("data-lucide") || !_ansTimer) {
          activateLucideIcons(answerContent);
        }
        // NOTE: an "final tick" block used to live here that re-ran
        // `innerHTML = renderMarkdown(raw)` whenever (!_ansTimer && !fast).
        // It was dead weight: the `if (!fast)` block above already rendered the
        // exact same `raw` in this same tick, so the second pass re-parsed
        // identical input and threw away the lucide icons created in between.
        // Removing it changes nothing on screen and saves one full markdown
        // pipeline + DOM rebuild per final chunk.
      }
      function _enqueueAnswer(text) {
        _ansQueue += text;
        _ansStart();
      }
      function _flushAnswerQueue() {
        _ansCancel();
        if (!_ansQueue || !answerContent) return;
        const chunk = _ansQueue;
        _ansQueue = "";
        raw += chunk;
        // Fast path: pure-prose chunks append straight onto the trailing text
        // node instead of rebuilding the whole answer. The old unconditional
        // `innerHTML = renderMarkdown(raw)` here is what made high-tok/s models
        // "frog jump" — every flush tore down and re-laid-out the entire block.
        let fast = false;
        if (!_ansInFence && !_ANS_STRUCTURAL_RE.test(chunk)) {
          const lastP = answerContent.lastElementChild;
          if (lastP && lastP.tagName === "P" && lastP.lastChild && lastP.lastChild.nodeType === 3) {
            lastP.lastChild.textContent += chunk;
            fast = true;
          }
        }
        if (!fast) {
          answerContent.innerHTML = renderMarkdown(raw);
          _ansInFence = countOpenFences(raw).inFence;
        }
        pinScrollIfAtBottom();
        activateLucideIcons(answerContent);
        scrollBottom();
      }

      function closeAnswer() {
        _flushAnswerQueue();
        if (!answerEl) return;
        // Skip markdown re-render for special cards (rate-limit, captcha) that
        // already have their final HTML set via innerHTML.
        const _isSpecialCard = raw === "__rate_limit_card__" || raw === "__captcha_block_card__" || raw === "__error_card__" || raw === "__no_slot_card__";
        // Final render with full mermaid + math support — only runs when this answer
        // segment is truly done (stream end or skill interleave boundary).
        if (answerContent && raw && !_isSpecialCard) {
          // Stash raw markdown for the copy button before final render
          answerContent.dataset.raw = raw;
          answerContent.innerHTML = renderMarkdown(raw);
          pinScrollIfAtBottom();
          // Icons are cheap and must be right immediately — keep them sync.
          activateLucideIcons(answerContent);
          // Mermaid + MathJax are the expensive tail: dagre layout + SVG build
          // per diagram, and MathJax lazy-loads its own bundle on first use.
          // Running them synchronously here is what made the LAST message of a
          // long agentic loop take seconds — the main thread was already
          // saturated, so this queued behind thousands of prior re-renders.
          // Defer one tick: diagrams/math typeset ~10ms later instead of ~5s.
          // Capture the node now — answerContent is nulled at the end of this fn.
          const _postEl = answerContent;
          setTimeout(() => {
            if (!_postEl.isConnected) return;
            renderMermaidDiagrams(_postEl);
            renderMathJax(_postEl);
            pinScrollIfAtBottom();
          }, 0);
        }
        answerEl.classList.remove("streaming");
        // Removing .streaming also drops the reserved cursor padding, so the
        // content shrinks by 1.5em. Re-pin in the same tick or that shrink
        // reads as a jump at the exact moment the response finishes.
        pinScrollIfAtBottom();
        // Drop a trailing empty text segment left after an inline UI block.
        if (answerContent && answerContent.parentNode === answerEl
            && !raw.trim() && !answerContent.childNodes.length) {
          answerContent.remove();
        }
        // Keep the bubble if it holds an inline UI block or earlier text;
        // only remove a genuinely empty one.
        const _hasInlineUi = !!answerEl.querySelector(".ui-block");
        if (!_isSpecialCard && !raw.trim() && !_hasInlineUi && !answerEl.textContent.trim()) {
          answerEl.remove();
        }
        answerEl = null;
        answerContent = null;
        raw = "";
        _ansInFence = false;
      }

      function ensureCommandGroup() {
        closeAnswer();
        if (!lastCommandGroup || !turn.contains(lastCommandGroup)) {
          lastCommandGroup = document.createElement("div");
          lastCommandGroup.className = "skill-stack";
          turn.appendChild(lastCommandGroup);
        }
        return lastCommandGroup;
      }

      // A message queued while the model was working has just been delivered
      // (SSE `queued_injected`). Close the response that was in flight so it
      // freezes ABOVE the user bubble the server just persisted, then step out
      // of the way — every later delta starts a fresh turn BELOW it.
      // Without this, `turn` keeps hosting the still-open bubble and the rest
      // of the reply streams into a card that sits above the user's message,
      // which reads as the answer belonging to the wrong thing.
      function injectUserMessage() {
        closeCurrentThinking();
        // Drop the closed reasoning block: the new turn belongs under the user
        // bubble, so the old round's thinking must not be rebuilt by the next
        // delta and re-open above it.
        lastThinkWrap = null;
        lastThinkBody = null;
        lastThinkSummary = null;
        closeAnswer();
        // .streaming carries live-only affordances (stop cursor, tool tray).
        // Whatever remains in this turn is settled history now.
        turn.querySelectorAll(".msg.bot.streaming").forEach((el) => el.classList.remove("streaming"));
        // Roll the turn over. ensureAnswer() appends into `turn`, so leaving it
        // pointing at the sealed one would reopen the bubble ABOVE the user
        // message — the exact bug this is here to fix.
        lastCommandGroup = null;
        _closeBurst();
        // Settle the sealed turn NOW. finalize() runs against the live turn, so
        // anything still spinning in the abandoned one (backend-status card,
        // pending skill cards) would spin forever otherwise.
        _settleTurn(turn);
        _sealedTurns.push(turn);
        _injected = true;
        _skillRoundFloor = skillRounds.length;
        // The backend-status card and its pending flag belong to the sealed
        // turn. Flip them here instead of relying on the next appendAnswer,
        // which would look for them in the NEW turn and find nothing.
        pendingShown = false;
        const _oldProc = bsCard.querySelector(".bs-processing-text");
        if (_oldProc) _oldProc.style.display = "none";
        const _oldStatus = bsCard.querySelector(".bs-status");
        if (_oldStatus) _oldStatus.textContent = "done";

        turn = document.createElement("div");
        turn.className = "turn";
        pane.appendChild(turn);
        // Fresh stats for the new turn: reusing _stats kept the pre-injection
        // t0, so the speed badge reported total seconds from the start of the
        // whole stream while bursts only covered post-injection segments.
        _stats = { t0: performance.now(), tFirst: 0, tEnd: 0, bursts: [] };
        turn._sableStats = _stats;
      }

      // Mark a turn as settled history: kill live-only affordances and freeze
      // any card that is still mid-flight. Shared by injectUserMessage() and
      // finalize() so a sealed turn is never left half-alive.
      function _settleTurn(t) {
        if (!t) return;
        // Freeze the timing window so attachSpeedBadge reports this turn's own
        // span, not everything up to finalize().
        if (t._sableStats && !t._sableStats.tEnd) t._sableStats.tEnd = performance.now();
        t.querySelectorAll(".msg.bot.streaming").forEach((el) => el.classList.remove("streaming"));
        t.querySelectorAll(".skill-card.pending").forEach((card) => {
          const status = card.querySelector(".pending-status");
          if (status) {
            status.textContent = "interrupted";
            status.style.color = "var(--danger)";
          }
          card.classList.remove("pending");
        });
      }

      scrollBottom();
      return {
        injectUserMessage,
        appendThinking(text) {
          hidePending();
          ensureThinkingBlock();
          _enqueueThink(text);
        },
        closeThinking() {
          closeCurrentThinking();
        },
        showRoundThinking(text) {
          if (!text) return;
          hidePending();
          // If the live `thinking` stream already built this round's block, the
          // same text is already there — just finalize it. Collapsing + rebuilding
          // (the old path) re-typed the whole block and made it jump up and down.
          if (currentThinkWrap && currentThinkBody) {
            _flushThinkQueue();
            return;
          }
          closeCurrentThinking();
          lastCommandGroup = null;
          // Build an open thinking block and animate its content via typewriter
          currentThinkWrap = document.createElement("div");
          currentThinkWrap.className = "thinking-wrap";
          currentThinkWrap.innerHTML = `
            <details class="thinking" open>
              <summary><i data-lucide="chevron-right" class="thinking-chevron"></i>Thinking…</summary>
              <div class="thinking-body"></div>
            </details>`;
          currentThinkBody = currentThinkWrap.querySelector(".thinking-body");
          currentThinkSummary = currentThinkWrap.querySelector("summary");
          // Same ordering fix as ensureThinkingBlock: keep the reasoning card
          // above the answer even if ensureAnswer() already ran this turn.
          if (answerEl && answerEl.parentNode === turn) {
            turn.insertBefore(currentThinkWrap, answerEl);
          } else {
            turn.appendChild(currentThinkWrap);
          }
          activateLucideIcons(currentThinkWrap);
          lastThinkWrap = currentThinkWrap;
          lastThinkBody = currentThinkBody;
          lastThinkSummary = currentThinkSummary;
          _thinkRoundDone = false;
          _enqueueThink(text);
        },
        addSkillStart(evt) {
          hidePending();
          closeCurrentThinking();
          // Tool card == new round boundary: next thinking gets its own block.
          markThinkRoundDone();
          const group = ensureCommandGroup();
          group.style.display = "flex";
          const card = createSkillCard(evt);
          const placeholder = skillCards[evt.id];
          if (placeholder && placeholder.classList.contains("pending")) {
            placeholder.replaceWith(card);
          } else {
            group.appendChild(card);
          }
          skillCards[evt.id] = card;
          activateLucideIcons(card);
          trackSkillEvent(evt);
          // Keep activity card as last child so its exit never causes a layout jump
          const tac = turn.querySelector(".tool-activity-card");
          if (tac) turn.appendChild(tac);
          scrollBottom();
        },
        appendSkillOutput(evt) {
          const card = skillCards[evt.id];
          if (!card) return;
          trackSkillEvent(evt);
          appendSkillCardOutput(card, evt.text);
          scrollBottom();
        },
        finishSkill(evt) {
          const card = skillCards[evt.id];
          if (!card) return;
          trackSkillEvent(evt);
          finishSkillCard(card, evt);
          delete skillCards[evt.id];
        },
        addAskUser(payload) {
          hidePending();
          closeCurrentThinking();
          renderAskUserCard(payload, turn);
        },
        addComponent(evt) {
          // Rich UI is message content. During streaming the SAME block arrives
          // repeatedly — `partial:true` while it's still being written, then a
          // final `partial:false` seal — so we key the segment by `evt.id` and
          // re-render into the same node instead of stacking copies. A block
          // with no id (legacy) keeps the old append-once behavior.
          const payload = evt && typeof evt === "object" && "payload" in evt ? evt.payload : evt;
          const _id = evt && typeof evt === "object" ? evt.id : null;
          // Only an EXPLICIT partial:false seals. A keyed event missing the
          // flag is still treated as a streaming update — otherwise (bug) each
          // chunk looked final, spawned a fresh node, and produced N copies.
          // An id-less event is legacy/history: append once, no tracking.
          const _isPartial = _id ? evt.partial !== false : false;

          if (!this._uiSegs) this._uiSegs = Object.create(null);
          if (!this._uiSigs) this._uiSigs = Object.create(null);

          // Final parse came back empty — drop whatever partial we drew.
          if (evt && typeof evt === "object" && evt.remove) {
            if (_id && this._uiSegs[_id]) {
              this._uiSegs[_id].remove();
              delete this._uiSegs[_id];
            }
            if (_id) delete this._uiSigs[_id];
            return;
          }

          let uiSeg = _id ? this._uiSegs[_id] : null;
          const _existing = uiSeg && uiSeg.isConnected;

          if (!_existing) {
            // First sighting of this block: do the full inline-segment setup —
            // close thinking, flush the typewriter, render any leading prose.
            hidePending();
            closeCurrentThinking();
            _flushAnswerQueue();          // drain pending typewriter text
            ensureAnswer();
            if (raw && raw.trim()) {
              answerContent.dataset.raw = raw;
              answerContent.innerHTML = renderMarkdown(raw);
              renderMermaidDiagrams(answerContent);
              renderMathJax(answerContent);
              activateLucideIcons(answerContent);
            } else if (answerContent && !answerContent.childNodes.length) {
              answerContent.remove();    // no leading prose — no blank gap
            }
            uiSeg = document.createElement("div");
            uiSeg.className = "md-content ui-block";
            answerEl.appendChild(uiSeg);
            if (_id) this._uiSegs[_id] = uiSeg;
          }

          // Re-render from scratch each update: the tree is small and this is
          // the only correct way to handle nodes being reordered/closed
          // mid-stream. Clear first so a partial that shrank leaves no orphans.
          // Skip the whole teardown when the payload matches the last tick —
          // otherwise every partial destroys and rebuilds the <img> elements,
          // which reads as the picture flickering between two images.
          if (payload && window.SableComponents) {
            let _sig = null;
            try { _sig = JSON.stringify(payload); } catch (e) { _sig = null; }
            if (_sig === null || _id === null || this._uiSigs[_id] !== _sig) {
              try {
                // Atomic swap — a throw or an empty tree keeps the previous
                // render instead of leaving the segment blank.
                if (window.SableComponents.renderInto) {
                  window.SableComponents.renderInto(uiSeg, payload);
                } else {
                  uiSeg.innerHTML = "";
                  window.SableComponents.render(payload, uiSeg);
                }
                if (_id !== null && _sig !== null) this._uiSigs[_id] = _sig;
              } catch (e) {
                if (!_existing) uiSeg.remove();
              }
              activateLucideIcons(uiSeg);
            }
          } else {
            activateLucideIcons(uiSeg);
          }

          // Only a FINAL (or unkeyed) block opens a fresh text segment below.
          // A partial update must not, or every chunk would spawn an empty
          // prose segment between the partials.
          if (!_isPartial) {
            _openAnswerSegment();        // fresh segment for continued text
            if (_id) {
              delete this._uiSegs[_id];
              delete this._uiSigs[_id];
            }
          }
          scrollBottom();
        },
        addHtml(evt) {
          // Sandboxed HTML pane. html_start mounts the iframe skeleton,
          // html_chunk streams a delta in, html_end seals it. Same id-keying
          // as addComponent so replayed/streamed events share one node.
          if (!window.SableHtml) return;
          const _id = evt && evt.id;
          if (!_id) return;
          if (!this._htmlSegs) this._htmlSegs = Object.create(null);
          if (evt.type === "html_start") {
            if (!this._htmlSegs[_id]) {
              hidePending();
              closeCurrentThinking();
              _flushAnswerQueue();
              ensureAnswer();
              if (raw && raw.trim()) {
                answerContent.dataset.raw = raw;
                answerContent.innerHTML = renderMarkdown(raw);
                renderMermaidDiagrams(answerContent);
                renderMathJax(answerContent);
                activateLucideIcons(answerContent);
              } else if (answerContent && !answerContent.childNodes.length) {
                answerContent.remove();
              }
              const holder = document.createElement("div");
              holder.className = "md-content ui-html";
              // evt.url (if present) mounts a LIVE framed page instead of the
              // inline shell — see render-html.js mount(). evt.height pins a
              // fixed pane height when the model asked for one.
              const el = window.SableHtml.mount(
                _id, evt.title || "Interactive", evt.height, evt.url || "");
              holder.appendChild(el.wrap);
              answerEl.appendChild(holder);
              this._htmlSegs[_id] = holder;
              _openAnswerSegment();
            }
          } else if (evt.type === "html_chunk") {
            window.SableHtml.push(_id, evt.text || "");
          } else if (evt.type === "html_end") {
            window.SableHtml.finish(_id, evt.error);
            if (this._htmlSegs[_id]) delete this._htmlSegs[_id];
          }
          scrollBottom();
        },
        addEvent(text) {
          hidePending();
          const group = ensureCommandGroup();
          group.style.display = "flex";
          const div = document.createElement("div");
          div.className = "event";
          div.textContent = text;
          group.appendChild(div);
          scrollBottom();
        },
        nextSkillRound() {
          markThinkRoundDone();
          if (skillRounds[skillRounds.length - 1].length) skillRounds.push([]);
        },
        closeBurst() { _closeBurst(); },

        attachToolMemory(memories) {
          // Memories injected from a tool result — pin the chip inline
          // inside the last skill card's header-right.
          hidePending();
          const cards = turn.querySelectorAll(".skill-card");
          const target = cards.length ? cards[cards.length - 1] : null;
          const chip = createMemoryChip(memories);
          chip.classList.add("memory-chip-tool");
          if (target) {
            const right = target.querySelector(".skill-header-right");
            if (right) right.insertBefore(chip, right.firstChild);
            else target.querySelector(".skill-header")?.appendChild(chip);
          } else {
            turn.appendChild(chip);
          }
          scrollBottom();
        },
        appendAnswer(text) {
          hidePending();
          ensureAnswer();
          answerEl.style.display = "";
          if (text && text.trim() && !/\[(error|stopped|client error)\]/.test(text)) sawNormalAnswer = true;
          if (text && text.trim()) {
            const _now = performance.now();
            if (!_stats.tFirst) _stats.tFirst = _now;
            if (!_burstStart) _burstStart = _now;
            _burstChars += text.length;
          }
          // Errors/stop messages render instantly; normal text gets typewriter
          if (/\[(error|stopped|client error)\]/.test(text)) {
            _flushAnswerQueue();
            raw += text;
            answerContent.innerHTML = renderMarkdown(raw);
            pinScrollIfAtBottom();
            activateLucideIcons(answerContent);
            scrollBottom();
          } else {
            _enqueueAnswer(text);
          }
        },
        replaceWithRateLimit(message, hours, debugInfo) {
          hidePending();
          // Kill typewriter queues immediately — don't flush partial content
          _thinkCancel();
          _thinkFrac = 0;
          _thinkQueue = "";
          _ansCancel();
          _ansQueue = "";
          currentThinkWrap = null;
          currentThinkBody = null;
          currentThinkSummary = null;
          // Remove only the currently-streaming partial answer — keep completed skill work
          if (answerEl) {
            answerEl.remove();
            answerEl = null;
            answerContent = null;
            raw = "";
          }
          // Build persistent rate-limit card
          ensureAnswer();
          answerEl.classList.remove('streaming');
          // Set raw so closeAnswer() doesn't remove this element on finalize
          raw = "__rate_limit_card__";
          const h = hours || '?';
          const dbg = debugInfo || {};
          const debugHtml = dbg.account ? `
              <div class="card-debug-info">
                <span class="cdi-label">Service Account</span><span class="cdi-value">${dbg.account}</span>
                <span class="cdi-label">Override</span><span class="cdi-value">${dbg.account_override || 'none'}</span>
                <span class="cdi-label">Active File</span><span class="cdi-value">${dbg.active_account_file || '—'}</span>
                <span class="cdi-label">Browser Data</span><span class="cdi-value cdi-path">${dbg.browser_data_dir || '—'}</span>
                <span class="cdi-label">Cookie Snippet</span><span class="cdi-value cdi-cookies">${dbg.cookie_snippet || 'none'}</span>
                <span class="cdi-label">bx_ua</span><span class="cdi-value">${dbg.has_bx_ua ? '✅' : '❌'}</span>
                <span class="cdi-label">bx_umidtoken</span><span class="cdi-value">${dbg.has_bx_umidtoken ? '✅' : '❌'}</span>
                ${dbg.error ? `<span class="cdi-label">Error</span><span class="cdi-value cdi-error">${dbg.error}</span>` : ''}
              </div>` : '';
          answerContent.innerHTML = `
            <div class="rate-limit-card">
              <span class="rl-icon">⏳</span>
              <span class="rl-title">Daily Usage Limit Reached</span>
              <span class="rl-detail">${message || 'You have reached the upper limit for today\'s usage.'}</span>
              <span class="rl-timer">Try again in ~${h} hour${h === 1 ? '' : 's'}. This message will stay visible so you don't miss it.</span>
              ${debugHtml}
            </div>`;
          // raw already set to "__rate_limit_card__" above — do NOT overwrite
          scrollBottom();
        },
        replaceWithCaptchaBlock(message, debugInfo) {
          hidePending();
          _thinkCancel();
          _thinkFrac = 0;
          _thinkQueue = "";
          _ansCancel();
          _ansQueue = "";
          currentThinkWrap = null;
          currentThinkBody = null;
          currentThinkSummary = null;
          if (answerEl) {
            answerEl.remove();
            answerEl = null;
            answerContent = null;
            raw = "";
          }
          ensureAnswer();
          answerEl.classList.remove('streaming');
          // Set raw so closeAnswer() doesn't remove this element on finalize
          raw = "__captcha_block_card__";
          const dbg = debugInfo || {};
          const debugHtml = dbg.account ? `
              <div class="card-debug-info">
                <span class="cdi-label">Service Account</span><span class="cdi-value">${dbg.account}</span>
                <span class="cdi-label">Override</span><span class="cdi-value">${dbg.account_override || 'none'}</span>
                <span class="cdi-label">Active File</span><span class="cdi-value">${dbg.active_account_file || '—'}</span>
                <span class="cdi-label">Browser Data</span><span class="cdi-value cdi-path">${dbg.browser_data_dir || '—'}</span>
                <span class="cdi-label">Cookie Snippet</span><span class="cdi-value cdi-cookies">${dbg.cookie_snippet || 'none'}</span>
                <span class="cdi-label">bx_ua</span><span class="cdi-value">${dbg.has_bx_ua ? '✅' : '❌'}</span>
                <span class="cdi-label">bx_umidtoken</span><span class="cdi-value">${dbg.has_bx_umidtoken ? '✅' : '❌'}</span>
                ${dbg.error ? `<span class="cdi-label">Error</span><span class="cdi-value cdi-error">${dbg.error}</span>` : ''}
              </div>` : '';
          answerContent.innerHTML = `
            <div class="captcha-block-card">
              <span class="cb-icon">🛡️</span>
              <span class="cb-title">Captcha / WAF Challenge Hit</span>
              <span class="cb-detail">${message || 'Qwen rejected this request with a captcha or WAF validation challenge.'}</span>
              <span class="cb-note">The request was stopped so you can switch/refresh the account or solve the challenge manually.</span>
              ${debugHtml}
            </div>`;
          // raw already set to "__captcha_block_card__" above — do NOT overwrite
          scrollBottom();
        },
        replaceWithNoSlot(message, slots) {
          hidePending();
          _thinkCancel();
          _thinkFrac = 0;
          _thinkQueue = "";
          _ansCancel();
          _ansQueue = "";
          currentThinkWrap = null;
          currentThinkBody = null;
          currentThinkSummary = null;
          if (answerEl) {
            answerEl.remove();
            answerEl = null;
            answerContent = null;
            raw = "";
          }
          ensureAnswer();
          answerEl.classList.remove('streaming');
          raw = "__no_slot_card__";
          const list = Array.isArray(slots) ? slots : [];
          const fmtAge = (s) => {
            if (s >= 3600) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
            if (s >= 60) return `${Math.floor(s / 60)}m ${s % 60}s`;
            return `${s}s`;
          };
          const rows = list.map(s => `
            <div class="nsc-slot" data-pid="${s.pid}">
              <div class="nsc-slot-main">
                <span class="nsc-slot-owner">${escHtml(s.owner || 'browser')}</span>
                <span class="nsc-slot-meta">pid ${s.pid} · idle ${fmtAge(s.idle_sec || 0)} · age ${fmtAge(s.age_sec || 0)}${s.profile ? ' · ' + escHtml(s.profile) : ''}</span>
              </div>
              <button class="nsc-kill" data-pid="${s.pid}">Kill</button>
            </div>`).join('');
          answerContent.innerHTML = `
            <div class="no-slot-card">
              <span class="nsc-icon">🚦</span>
              <span class="nsc-title">No Browser Slot Available</span>
              <span class="nsc-detail">Both browser slots are held by live chat sessions, so this message couldn't spawn a browser. Kill one below to free a slot, or wait for a chat to finish.</span>
              <div class="nsc-slots">${rows || '<div class="nsc-empty">No holders reported.</div>'}</div>
            </div>`;
          answerContent.querySelectorAll('.nsc-kill').forEach(btn => {
            btn.addEventListener('click', async () => {
              const pid = btn.getAttribute('data-pid');
              btn.disabled = true;
              btn.textContent = 'Killing…';
              try {
                const res = await fetch('/api/settings/browser/slots/kill', {
                  method: 'POST',
                  headers: { 'Content-Type': 'application/json' },
                  body: JSON.stringify({ pid: Number(pid) }),
                });
                if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
                btn.textContent = 'Killed ✓';
                btn.classList.add('nsc-killed');
                showToast(`Killed browser ${pid} — slot freed. Send your message again.`, 'success');
              } catch (e) {
                btn.disabled = false;
                btn.textContent = 'Kill';
                showToast(`Kill failed: ${e.message}`, 'error');
              }
            });
          });
          scrollBottom();
        },
        replaceWithError(message) {
          hidePending();
          // Kill typewriter queues — don't leave a half-rendered partial answer
          _thinkCancel();
          _thinkFrac = 0;
          _thinkQueue = "";
          _ansCancel();
          _ansQueue = "";
          currentThinkWrap = null;
          currentThinkBody = null;
          currentThinkSummary = null;
          // Remove only the streaming partial answer — keep completed skill work
          if (answerEl) {
            answerEl.remove();
            answerEl = null;
            answerContent = null;
            raw = "";
          }
          ensureAnswer();
          answerEl.classList.remove('streaming');
          // Mark special so closeAnswer() doesn't wipe this HTML on finalize()
          raw = "__error_card__";
          // Server sends multi-line token lists (e.g. per-token rotation failures),
          // so preserve whitespace + escape before injecting.
          answerContent.innerHTML = `
            <div class="error-card">
              <span class="ec-icon">⚠️</span>
              <span class="ec-title">Request Failed</span>
              <span class="ec-detail">${escHtml(message || 'Upstream request failed.')}</span>
            </div>`;
          scrollBottom();
        },
        trackFileEdit(evt) {
          fileEditSummary.count++;
          fileEditSummary.added += evt.added || 0;
          fileEditSummary.removed += evt.removed || 0;
          // Ensure answer container exists so we have somewhere to append the card
          ensureAnswer();
          if (!fileEditSummary.card) {
            const card = document.createElement("div");
            card.className = "file-edit-summary-card";
            card.addEventListener("click", () => {
            // Open files panel in left sidebar with Diff tab
            if (window.sidebarHost) {
              window.sidebarHost.openPanel('files', { mode: 'diff' });
            }
            if (typeof AgentPanel !== "undefined") AgentPanel.close();
          });
            fileEditSummary.card = card;
          }
          // Always re-parent to the current answerEl so the card follows the
          // latest agent round instead of staying stuck on the first one.
          if (fileEditSummary.card.parentNode !== answerEl) {
            answerEl.appendChild(fileEditSummary.card);
          }
          const c = fileEditSummary.card;
          const f = fileEditSummary.count;
          const a = fileEditSummary.added;
          const r = fileEditSummary.removed;
          c.innerHTML = `<span class="fes-icon">📝</span>` +
            `<span class="fes-text"><strong>${f}</strong> file${f === 1 ? "" : "s"} edited · ` +
            `<span class="fes-add">+${a}</span> / <span class="fes-del">-${r}</span></span>` +
            `<span class="fes-arrow">▶</span>`;
          scrollBottom();
        },
        _currentToolTag: null,
        _currentToolPath: null,
        showToolPending(evt) {
          hidePending();
          const tag = evt.tag || "tool";
          const attrs = evt.attrs || {};
          this._currentToolTag = tag;
          this._currentToolPath = attrs.path || attrs.filename || "";
          const meta = {
            create_file:  { icon: "📝", label: "Creating file", detail: attrs.path || "", progress: true },
            edit_file:    { icon: "✏️", label: "Editing file", detail: attrs.path || "", progress: true },
            insert_file:  { icon: "✏️", label: "Inserting into file", detail: attrs.path || "", progress: true },
            view_file:    { icon: "👁️", label: "Reading file", detail: attrs.path || (attrs.full ? "full file" : "") },
            execute_command: { icon: "⚡", label: attrs.bg === "true" ? "Running background task" : "Running command", detail: "" },
            get_file:     { icon: "📂", label: "Loading file", detail: "" },
            create_note:  { icon: "🗒️", label: "Creating note", detail: attrs.path || "" },
            save_svg:     { icon: "🎨", label: "Saving SVG", detail: attrs.path || "" },
            create_svg:   { icon: "🎨", label: "Creating SVG", detail: attrs.filename || attrs.path || "" },
            spawn_agent:  { icon: "🤖", label: `Spawning ${attrs.role || "agent"}`, detail: (attrs.task || "").slice(0, 60), progress: true },
          };
          const info = meta[tag] || { icon: "⚙️", label: tag, detail: "" };
          // Reuse existing card in-place to avoid layout shift
          if (_tacExitTimer) { clearTimeout(_tacExitTimer); _tacExitTimer = null; }
          let card = turn.querySelector(".tool-activity-card");
          if (!card) {
            card = document.createElement("div");
            turn.appendChild(card);
          }
          card.className = "tool-activity-card";
          const detailHtml = info.progress
            ? `<div class="tac-detail tac-detail-split"><span class="tac-path">${info.detail || ""}</span></div><div class="tac-preview"></div>`
            : (info.detail ? `<div class="tac-detail">${info.detail}</div>` : "");
          card.innerHTML =
            `<div class="tac-icon">${lucideIcon(info.icon)}</div>` +
            `<div class="tac-info"><div class="tac-title">${info.label}</div>` +
            detailHtml +
            `</div><div class="tac-status">${info.progress ? `<div class="tac-pulse-dot"></div>` : `<div class="tac-spinner"></div>`}</div>`;
          // Always keep it as the last element
          turn.appendChild(card);
          activateLucideIcons(card);
          scrollBottom();
        },
        showToolProgress(evt) {
          const card = turn.querySelector(".tool-activity-card");
          if (!card) return;
          const previewEl = card.querySelector(".tac-preview");
          if (!previewEl) return;
          const lines = evt.preview_lines || (evt.preview ? [evt.preview] : []);
          if (!lines.length) return;
          previewEl.innerHTML = lines.map(l => `<div class="tac-preview-line">${escHtml(l)}</div>`).join("");
          // Auto-scroll to bottom of preview
          previewEl.scrollTop = previewEl.scrollHeight;
        },
        showToolDone() {
          const card = turn.querySelector(".tool-activity-card");
          if (!card) return;
          const status = card.querySelector(".tac-status");
          if (status) { status.innerHTML = `<span class="tac-check">${lucideIcon("✓")}</span>`; activateLucideIcons(status); }
          card.classList.add("tac-done");

          // Fade out only if no next tool reuses it (cancelled in showToolPending)
          _tacExitTimer = setTimeout(() => {
            card.classList.add("tac-exit");
            setTimeout(() => card.remove(), 350);
          }, 600);
        },
        finalize() {
          _stats.tEnd = performance.now();
          _closeBurst();
          hidePending();
          closeCurrentThinking();
          // If auto-switch didn't recover, render the permanent error card now.
          // closeAnswer() will remove the transient switching-status card first.
          const _prl = this._pendingRateLimit;
          const _pcb = this._pendingCaptchaBlock;
          const _pns = this._pendingNoSlot;
          closeAnswer();
          if (_prl) {
            this.replaceWithRateLimit(_prl.message, _prl.hours, _prl.debug);
            this._pendingRateLimit = null;
          } else if (_pcb) {
            this.replaceWithCaptchaBlock(_pcb.message, _pcb.debug);
            this._pendingCaptchaBlock = null;
          } else if (_pns) {
            this.replaceWithNoSlot(_pns.message, _pns.slots);
            this._pendingNoSlot = null;
          }
          // Cancel any pending exit timer from a tool that finished just before stop
          if (_tacExitTimer) { clearTimeout(_tacExitTimer); _tacExitTimer = null; }
          // Clean up any lingering tool activity card — mark as interrupted then fade out
          const tac = turn.querySelector(".tool-activity-card");
          if (tac) {
            const status = tac.querySelector(".tac-status");
            if (status) {
              status.innerHTML = '<span style="color:var(--danger);font-size:0.85em">✗ interrupted</span>';
            }
            const spinner = tac.querySelector(".tac-spinner");
            if (spinner) spinner.remove();
            const pulseDot = tac.querySelector(".tac-pulse-dot");
            if (pulseDot) pulseDot.remove();
            // Trigger exit animation so the card doesn't stay frozen in place
            requestAnimationFrame(() => {
              tac.classList.add("tac-exit");
              setTimeout(() => tac.remove(), 350);
            });
          }
          // Any placeholder still spinning means the stream died mid-tag.
          turn.querySelectorAll(".skill-card.pending").forEach(card => {
            const status = card.querySelector(".pending-status");
            if (status) {
              status.textContent = "interrupted";
              status.style.color = "var(--danger)";
            }
            card.classList.remove("pending");
          });
          // A turn abandoned by a mid-stream user injection is settled, not
          // empty — sweeping it below keeps it alive, so don't cry wolf when the
          // live turn has no bot content but a sealed turn holds the answer.
          const _sealedHasContent = _injected && _sealedTurns.some(
            (t) => t.querySelector(".msg.bot") || t.querySelector(".skill-card")
          );
          if (!_sealedHasContent && !turn.querySelector(".msg.bot") && !turn.querySelector(".skill-card") && !turn.querySelector(".approval-pending-note")) {
            ensureAnswer();
            answerEl.classList.remove("streaming");
            answerContent.textContent = "⚠ Empty response from upstream — check server terminal for WAF/auth details.";
          }
          // Everything that needs a toolbar: the live turn plus every sealed
          // turn. Without this the pre-injection answer never gets Copy / TTS /
          // Regenerate or a speed badge — a visible regression on this path.
          const _turnsToSweep = [..._sealedTurns, turn];
          _sealedTurns.forEach((t) => _settleTurn(t));
          _turnsToSweep.forEach((sweepTurn) => sweepTurn.querySelectorAll(".msg.bot").forEach(botEl => {
            if (botEl.querySelector(".msg-toolbar")) return;
            window.MsgActions.attach(botEl, "bot", {
              speedTurn: sweepTurn,
              regen: () => {
                if (isStreaming()) return;
                const pane = botEl.closest(".tab-pane") || activePane;
                const allMsgs = Array.from(pane.querySelectorAll(".msg.user"));
                const thisTurn = botEl.closest(".turn");
                let prevUser = null;
                for (const u of allMsgs) {
                  if (u.compareDocumentPosition(thisTurn) & Node.DOCUMENT_POSITION_FOLLOWING) prevUser = u;
                }
                if (!prevUser) { showToast("No user message to regenerate from", "error"); return; }
                const userText = prevUser.querySelector(".user-text")?.textContent || "";
                if (!userText) return;
                // Remove this bot's own turn, not the live one — a sealed turn
                // and the current turn are different elements.
                sweepTurn.remove();
                inputEl.value = userText;
                sendMessage();
              }
            });
          }));

          // Commands ran but no normal answer ever arrived — pin a retry bar
          // under the last command group so the tool results can be resent.
          const hasCommands = turn.querySelectorAll(".skill-card").length > 0;
          if (hasCommands && !sawNormalAnswer && !turn.querySelector(".retry-command-bar")) {
            const lastRound = skillRounds.slice(_skillRoundFloor).filter(r => r.length).pop() || [];
            if (lastRound.length) {
              const stacks = turn.querySelectorAll(".skill-stack");
              const bar = document.createElement("div");
              bar.className = "retry-command-bar";
              const retryBtn = document.createElement("button");
              retryBtn.textContent = "↻ Resend tool results";
              retryBtn.addEventListener("click", () => retryLastCommand(lastRound, bar, retryBtn));
              bar.appendChild(retryBtn);
              if (stacks.length) stacks[stacks.length - 1].after(bar);
              else turn.appendChild(bar);
            }
          }
        }
      };
    }

    function attachResendBar(targetDiv, messageText) {
      if (targetDiv.querySelector('.resend-bar')) return;
      const resendBar = document.createElement("div");
      resendBar.className = "msg-toolbar resend-bar";
      const resendBtn = document.createElement("button");
      resendBtn.textContent = "↻ Resend";
      resendBtn.addEventListener("click", () => {
        resendBar.remove();
        inputEl.value = messageText;
        sendMessage();
      });
      resendBar.appendChild(resendBtn);
      targetDiv.appendChild(resendBar);
    }


    // Critique: fire a normal POST /api/chat turn, stream response into the critique card.
    // Uses a separate chat_id so the critique session doesn't pollute the main conversation history.
    // Renders identically to a normal chat (thinking, skills, live answer streaming) using
    // the standard addBotStreaming + consumeChatStream pipeline inside the critique log container.
    async function _runCritiqueTurn(message, box, logEl, statusEl, critiqueId, browserDataDir, parentChatId) {
      // The parent chat MUST be captured at trigger time and passed in — reading
      // activeChatId here is unsafe because the critique sub-chat can already
      // have been focused (sidebar refresh), which would make the critic write
      // its report back into its own history instead of the main chat.
      const _parent = parentChatId || activeChatId;
      if (!message || !_parent) return;
      const critiqueChatId = _parent + "-critique-" + (critiqueId || Date.now().toString(36));
      const controller = startStream(critiqueChatId);
      if (box) box._critiqueController = controller;

      // Maximum times we re-ask the critic to fix a malformed report before
      // giving up. A malformed report is NEVER forwarded to the main chat —
      // forwarding junk would poison the main model's context.
      const MAX_FORMAT_RETRIES = 2;

      // Required report sections. Models decorate headers inconsistently
      // (`**Mark:**`, `### Mark:`, `- Mark - 8/10`), so match loosely but
      // require all three to be present before accepting a report.
      const _MARK_RE = /^\s*(?:[>#*\-•]+\s*)*\**\s*Mark\s*\**\s*[:\-—–.]/im;
      const _WHY_RE  = /^\s*(?:[>#*\-•]+\s*)*\**\s*Why\s*\**\s*[:\-—–.]/im;
      const _SUG_RE  = /^\s*(?:[>#*\-•]+\s*)*\**\s*Suggestions?\s*\**\s*[:\-—–.]/im;

      function _validateReport(text) {
        const t = (text || "").trim();
        if (!t) return { ok: false, reason: "the response was empty" };
        const missing = [];
        if (!_MARK_RE.test(t)) missing.push("Mark");
        if (!_WHY_RE.test(t)) missing.push("Why");
        if (!_SUG_RE.test(t)) missing.push("Suggestions");
        if (missing.length) return { ok: false, reason: "missing section(s): " + missing.join(", ") };
        return { ok: true };
      }

      // Slice the report from the Mark header onward, dropping lead-in chatter.
      function _extractReport(text) {
        const t = (text || "").trim();
        const mark = t.match(_MARK_RE);
        if (mark && mark.index != null) return t.slice(mark.index).trim();
        return t;
      }

      // Sent back INTO the critique session when the report is malformed, so the
      // critic reformats its OWN output. Fixed template — only the reason varies,
      // and it is built from a closed set of section names (injection-safe).
      function _formatReminder(reason) {
        return (
          "[FORMAT ERROR] Your previous response was not an acceptable critique report. " +
          reason + ".\n\n" +
          "Respond with EXACTLY these three sections and nothing else — no preamble, " +
          "no tool calls, no closing remarks:\n\n" +
          "**Mark:** [X/10]\n" +
          "**Why:** (detailed explanation)\n" +
          "**Suggestions:** (numbered, actionable list)\n\n" +
          "Rewrite the complete report now, in that exact format."
        );
      }

      // Inline note in the critique log (textContent only — no HTML injection).
      function _appendLogNote(text) {
        if (!logEl) return;
        const note = document.createElement("div");
        note.className = "critique-format-note";
        note.textContent = text;
        logEl.appendChild(note);
      }

      let finalReport = null;
      let aborted = false;
      let errored = false;
      let exhausted = false;
      let currentMessage = message;
      let lastUi = null;

      try {
        // Attempt 0 = initial run. Each retry re-prompts the SAME critique session
        // with a format reminder until the report validates or we run out of tries.
        for (let attempt = 0; attempt <= MAX_FORMAT_RETRIES; attempt++) {
          if (controller.signal.aborted) { aborted = true; break; }

          // Fresh turn per attempt so retries are visually distinct and each
          // attempt accumulates only its own answer text.
          const ui = addBotStreaming(logEl, critiqueChatId);
          lastUi = ui;
          let fullAnswer = "";

          const _origAppend = ui.appendAnswer.bind(ui);
          ui.appendAnswer = function(text) {
            if (text) fullAnswer += text;
            _origAppend(text);
          };

          const _origSkillStart = ui.addSkillStart.bind(ui);
          let toolCount = 0;
          ui.addSkillStart = function(evt) {
            toolCount++;
            if (statusEl) statusEl.innerHTML = `<span class="critique-spinner"></span> Inspecting... (${toolCount} tools)`;
            _origSkillStart(evt);
          };

          const body = {
            message: currentMessage,
            chat_id: critiqueChatId,
            parent_id: undefined,
            model: selectedModel,
            thinking_mode: selectedThinkingMode,
            stream: true,
          };
          if (browserDataDir) body.browser_data_dir = browserDataDir;

          let res;
          try {
            res = await fetch("/api/chat", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify(body),
              signal: controller.signal,
            });
          } catch (fetchErr) {
            if (fetchErr.name === "AbortError") { aborted = true; ui.finalize(); break; }
            throw fetchErr;
          }

          if (!res.ok) {
            ui.appendAnswer(`\n[error] HTTP ${res.status}`);
            ui.finalize();
            errored = true;
            break;
          }

          // Live-stream through the standard pipeline — thinking, skills, answer all render
          const { gotError } = await consumeChatStream(res, ui, null, critiqueChatId, logEl);
          ui.finalize();

          if (controller.signal.aborted) { aborted = true; break; }
          if (gotError) { errored = true; break; }

          // Only a fully-formed report is ever accepted downstream.
          const check = _validateReport(fullAnswer);
          if (check.ok) {
            finalReport = _extractReport(fullAnswer);
            break;
          }

          if (attempt < MAX_FORMAT_RETRIES) {
            if (statusEl) statusEl.innerHTML = `<span class="critique-spinner"></span> Reformatting... (${attempt + 1}/${MAX_FORMAT_RETRIES})`;
            _appendLogNote(`⚠ Report format invalid (${check.reason}). Asking the critic to reformat — not forwarded to the main chat.`);
            currentMessage = _formatReminder(check.reason);
            await new Promise((r) => setTimeout(r, 300));
          } else {
            exhausted = true;
            _appendLogNote(`✗ Report still malformed after ${MAX_FORMAT_RETRIES} retries (${check.reason}). Discarded — not forwarded to the main chat.`);
          }
        }

        // ── Final status + delivery ──────────────────────────────────────
        if (aborted) {
          if (statusEl) statusEl.innerHTML = '<span style="color:var(--warn)">Stopped</span>';
          if (box) box.classList.add('critique-done');
        } else if (finalReport) {
          if (statusEl) statusEl.innerHTML = '<span style="color:var(--ok)">Complete</span>';
          if (box) {
            box.classList.add('critique-done');
            const sb = box.querySelector('.critique-stop-btn');
            if (sb) sb.style.display = 'none';
          }
          // Valid report → hand it to the main chat so the model can act on it.
          // targetChatId is pinned to the parent: without it the report lands in
          // whatever chat happens to be active — which is the critique sub-chat.
          if (typeof sendAutoTurnMessage === "function") {
            setTimeout(() => sendAutoTurnMessage(
              `[Critique Report]\n${finalReport}`,
              { targetChatId: _parent }
            ), 300);
          } else {
            console.warn("[critique] sendAutoTurnMessage unavailable — report not delivered");
          }
        } else if (errored) {
          if (statusEl) statusEl.innerHTML = '<span style="color:var(--error)">Error</span>';
          if (box) {
            box.classList.add('critique-done');
            const sb = box.querySelector('.critique-stop-btn');
            if (sb) sb.style.display = 'none';
          }
        } else if (exhausted) {
          if (statusEl) statusEl.innerHTML = '<span style="color:var(--warn)">Incomplete</span>';
          if (box) {
            box.classList.add('critique-done');
            const sb = box.querySelector('.critique-stop-btn');
            if (sb) sb.style.display = 'none';
          }
        }
      } catch (err) {
        if (err.name === "AbortError" || controller.signal.aborted) {
          if (statusEl) statusEl.innerHTML = '<span style="color:var(--warn)">Stopped</span>';
          if (box) box.classList.add('critique-done');
          const sb = box?.querySelector('.critique-stop-btn');
          if (sb) sb.style.display = 'none';
          if (lastUi) lastUi.finalize();
        } else {
          if (statusEl) statusEl.innerHTML = '<span style="color:var(--error)">Error</span>';
          if (lastUi) { lastUi.appendAnswer(`\n[client error] ${err.message}`); lastUi.finalize(); }
        }
      } finally {
        if (typeof endStream === "function") endStream(critiqueChatId);
        if (box) delete box._critiqueController;
      }
    }

    // ── Background Stream UI Adapter ───────────────────────────────────
    // ── Agent Streaming (main chat pipeline for subagents) ──────────────────
    const _activeAgentControllers = new Map(); // agentId -> AbortController

    async function _runAgentTurn(message, agentId, systemPrompt, model, browserDataDir, collect) {
      if (!message || !agentId || typeof sendAutoTurnMessage !== "function") return;
      if (_activeAgentControllers.has(agentId)) return;
      const controller = startStream(agentId);
      _activeAgentControllers.set(agentId, controller);

      // Lifecycle is driven entirely by SSE events:
      // - agent_spawned → addCard (running)
      // - agent_completed → finishCard
      // - agent_failed → failCard
      // We do NOT call finishCard here because sendAutoTurnMessage may
      // return immediately (setTimeout retry when parent is streaming),
      // which previously caused instant false "done" state.
      // endStream is handled by sendAutoTurnMessage's own finally block
      // when the actual stream completes.
      try {
        await sendAutoTurnMessage(message, {
          targetChatId: agentId,
          model,
          systemPrompt,
          browserDataDir,
          controller,
        });
      } catch (error) {
        // Hard failure before stream even started — SSE won't fire
        if (error.name !== "AbortError" && typeof AgentTopBar !== "undefined") {
          AgentTopBar.failCard(agentId, error.message || "failed");
        }
      }

      // Safety cleanup for _activeAgentControllers only.
      // endStream is managed by sendAutoTurnMessage's finally block.
      // Use a long timeout as agents can run for minutes.
      setTimeout(() => {
        _activeAgentControllers.delete(agentId);
      }, 600_000);
    }

    // Expose for agents.js EventSource handler
    window._runAgentTurn = _runAgentTurn;

    // Called when an agent completes or fails so the sidebar running-dot
    // clears immediately instead of waiting on the 600s safety timer.
    // NOTE: we do NOT abort by default — endStream() is owned by
    // sendAutoTurnMessage's finally block. Aborting here can truncate the
    // last answer/done frames if the server hasn't flushed yet. Only pass
    // { abort: true } for hard cancellation (e.g. agent_failed).
    window._finishAgentStream = function(agentId, { abort = false } = {}) {
      if (!agentId) return;
      if (abort) {
        const ctrl = _activeAgentControllers.get(agentId);
        if (ctrl && !ctrl.signal.aborted) {
          try { ctrl.abort(); } catch (_) {}
        }
      }
      _activeAgentControllers.delete(agentId);
    };

    // Helper: look up agent role from topbar data
    function _getAgentRole(agentId) {
      const card = document.querySelector(`.agent-card[data-agent-id="${agentId}"]`);
      return card ? (card.dataset.role || "agent") : "agent";
    }

    // Nuclear stop: abort all agent sub-streams + critique streams for this chat
    window._abortAllAgents = function() {
      // Abort all agent controllers
      for (const [id, ctrl] of _activeAgentControllers) {
        if (!ctrl.signal.aborted) ctrl.abort();
        // Also tell server to stop each agent's upstream generation
        fetch("/api/chat/stop", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ chat_id: id }),
        }).catch(() => {});
      }
      _activeAgentControllers.clear();

      // Abort all critique controllers (stored on .critique-box elements)
      document.querySelectorAll(".critique-box[data-turn-active]").forEach(box => {
        const ctrl = box._critiqueController;
        if (ctrl && !ctrl.signal.aborted) ctrl.abort();
        const critiqueId = box.dataset.critiqueId;
        if (critiqueId) {
          const critiqueChatId = `${activeChatId}-critique-${critiqueId}`;
          fetch("/api/chat/stop", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ chat_id: critiqueChatId }),
          }).catch(() => {});
        }
      });
    };

    async function consumeChatStream(res, ui, userMsgDiv, streamChatId, streamPane = activePane) {
      const reader  = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let gotAnswer = false;
      let gotDone = false;
      let gotError = false;
      // Pending terminal cards stored on ui so finalize() can access them.
      // Set when rate_limited/waf_blocked fires; cleared when answer arrives (recovery).
      // If still set at finalize(), the permanent error card is rendered.
      let gotTitle = false;
      let _lastSkillPath = "";

      // Mirror stream events to AgentPanel if this stream belongs to a subagent
      const _isAgentStream = typeof _activeAgentControllers !== "undefined" && _activeAgentControllers.has(streamChatId);
      function _mirrorToPanel(evt) {
        if (!_isAgentStream || typeof AgentPanel === "undefined" || AgentPanel.currentAgentId !== streamChatId) return;
        if (evt.type === "round_thinking") AgentPanel.appendThinking(streamChatId, evt.text || "");
        else if (evt.type === "answer") AgentPanel.appendAnswer(streamChatId, evt.text || "");
        else if (evt.type === "skill_start") AgentPanel.addSkillStart(streamChatId, evt);
        else if (evt.type === "skill_output") AgentPanel.addSkillOutput(streamChatId, evt);
        else if (evt.type === "skill_end") AgentPanel.addSkillEnd(streamChatId, evt);
        else if (evt.type === "done") AgentPanel.markDone(streamChatId, "completed");
        else if (evt.type === "error") AgentPanel.appendError(streamChatId, evt.message || "Unknown error");
      }
      let _lastSkillName = "";

      // ── Tool-in-flight tracking ────────────────────────────────────────
      // A tool run legitimately produces NO stream bytes for minutes. That is
      // not a stalled socket, so the read watchdog must not be armed against
      // it — the watchdog only guards the model's own response stream.
      //
      //   tool_pending ──> skill_start ──> skill_output… ──> skill_end
      //        └──────────── parse_error (tool never ran)
      let _toolDepth = 0;    // between skill_start and skill_end
      let _toolPending = 0;  // between tool_pending and its skill_start/parse_error
      const _toolsInFlight = () => _toolDepth + _toolPending > 0;

      // ── Read watchdog ──────────────────────────────────────────────────
      // reader.read() has NO timeout. If the server wedges (hung upstream, dead
      // browser, blocked generator) the body never closes, this loop never
      // exits, sendMessage's finally never runs, endStream() is never called,
      // and isStreaming() stays true forever — the send button silently turns
      // into a stop button until the user presses it manually. Backend watchdogs
      // cover most cases; this is the client-side backstop for the rest.
      //
      // 75s: the server's own pre-request (45s) + first-chunk (15s) watchdogs
      // fire well before this. A single tick means the socket is genuinely dead.
      //
      // While a tool is in flight the budget is 15 minutes instead — long enough
      // for a real tool run, short enough that a truly dead socket still dies.
      const _READ_STALL_MS = 75000;
      const _TOOL_STALL_MS = 900000;
      async function _readWithWatchdog() {
        let _timer;
        const _budget = _toolsInFlight() ? _TOOL_STALL_MS : _READ_STALL_MS;
        const _watchdog = new Promise((_, reject) => {
          _timer = setTimeout(
            () => reject(Object.assign(new Error("stream stalled"), { name: "StreamStallError" })),
            _budget
          );
        });
        try {
          return await Promise.race([reader.read(), _watchdog]);
        } finally {
          clearTimeout(_timer);
        }
      }

      while (true) {
        let _chunk;
        try {
          _chunk = await _readWithWatchdog();
        } catch (e) {
          if (e && e.name === "StreamStallError") {
            // Treat as a terminal error: surface it in the transcript and stop
            // reading. Throwing here would bypass the caller's empty-response
            // handling, so set the flag and break instead.
            gotError = true;
            try { ui.replaceWithError("Stream stalled — no data for 75s. The connection was dropped; send again to retry."); } catch (_) {}
            try { reader.cancel(); } catch (_) {}
            break;
          }
          throw e;
        }
        const { done, value } = _chunk;
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const parts = buffer.split("\n\n");
        buffer = parts.pop();

        for (const part of parts) {
          const line = part.trim();
          if (!line.startsWith("data: ")) continue;

          let evt;
          try { evt = JSON.parse(line.slice(6)); }
          catch { continue; }

          // Forward agent-relevant events to the panel (no-op if not an agent stream)
          _mirrorToPanel(evt);

          if (evt.type === "meta") {
            // Only adopt parent_id if the user is still viewing this stream's
            // chat — prevents a background stream from hijacking state.
            if (activeChatId === streamChatId) {
              parentId = evt.parent_id || parentId;
              // Handle upstream session recovery: chat_id may have changed
              if (evt.chat_id && evt.chat_id !== activeChatId) {
                const oldId = activeChatId;
                activeChatId = evt.chat_id;
                // Migrate the active stream entry so stop-button and
                // isStreaming() keep working under the new ID.
                const _ctrl = activeStreams.get(oldId);
                if (_ctrl) {
                  activeStreams.delete(oldId);
                  activeStreams.set(activeChatId, _ctrl);
                }
                // Update sidebar entry ID
                const sidebarBtn = document.querySelector(`[data-chat-id="${oldId}"]`);
                if (sidebarBtn) sidebarBtn.dataset.chatId = activeChatId;
                // Update tab pane
                const tabEntry = openTabs.get(oldId);
                if (tabEntry) {
                  tabEntry.pane.dataset.chatId = activeChatId;
                  openTabs.delete(oldId);
                  openTabs.set(activeChatId, tabEntry);
                }
                updateSendBtn();
                saveActiveChat();
                console.log("[session-recovery] chat_id renamed:", oldId, "->", activeChatId);
              }
              saveActiveChat();
            }
          } else if (evt.type === "response_id") {
            // Qwen stop API requires response_id. Backend emits this as soon as
            // the first upstream SSE chunk exposes it.
            if (evt.id && typeof window.setActiveResponseId === "function") {
              window.setActiveResponseId(streamChatId, String(evt.id));
              if (activeChatId === streamChatId) {
                window.setActiveResponseId(activeChatId, String(evt.id));
              }
            }
          } else if (evt.type === "status") {
            if (evt.message === "feeding_skill_results") {
              ui.nextSkillRound();
            } else {
              // The status card is a TRANSIENT element: it is created by
              // addBotStreaming and is NOT a skill_event, so it is absent from
              // the DB. A mid-stream re-render (host.innerHTML = '') destroys
              // the .turn that hosts it, and every later status update was then
              // dropped on the floor — the card sat at "working…" with no steps
              // while the real pipeline message went nowhere. Fall back to the
              // pane so the card survives the turn wrapper being rebuilt.
              const _bsTurn = streamPane?.querySelector('.turn:last-child') || streamPane;
              if (_bsTurn) handleBackendStatusEvent(evt, _bsTurn);
            }
          } else if (evt.type === "account_switch") {
            // Same transient-card problem as `status` above: fall back to the
            // pane so a mid-stream re-render cannot orphan the card.
            const _ascTurn = streamPane?.querySelector('.turn:last-child') || streamPane;
            if (_ascTurn) handleAccountSwitchEvent(evt, _ascTurn);
          } else if (evt.type === "token_rotation") {
            const _trTurn = streamPane?.querySelector('.turn:last-child') || streamPane;
            if (_trTurn) handleTokenRotationEvent(evt, _trTurn);
          } else if (evt.type === "queued_injected") {
            // Backend delivered a message that was queued while the model was
            // working. Finalize the optimistic bubble(s) it corresponds to.
            if (typeof window._sableFinalizeQueued === "function") {
              window._sableFinalizeQueued(evt.temp_ids || [], evt.ids || [], evt.text || "");
            }
            // Seal the in-flight answer above the user bubble. Everything the
            // model streams from here on lands in a NEW turn beneath it, so the
            // reply reads top-to-bottom instead of pinning itself to the bottom.
            try { ui.injectUserMessage(); } catch (_) {}
          } else if (evt.type === "user_message_id") {
            // Store DB message ID on the div and enable the fork button
            if (userMsgDiv && evt.id) {
              userMsgDiv.dataset.msgId = String(evt.id);
              // Enable any pending fork button now that we have the DB ID
              const pendingFork = userMsgDiv.querySelector(".fork-pending");
              if (pendingFork) {
                pendingFork.disabled = false;
                pendingFork.classList.remove("fork-pending");
              }
              let toolbar = userMsgDiv.querySelector(".msg-toolbar");
              if (toolbar && !toolbar.querySelector('[title="Restore checkpoint"]')) {
                const cpBtn = document.createElement("button");
                cpBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="6" y1="3" x2="6" y2="15"></line><circle cx="18" cy="6" r="3"></circle><circle cx="6" cy="18" r="3"></circle><path d="M18 9a9 9 0 0 1-9 9"></path></svg>';
                cpBtn.title = "Restore checkpoint";
                cpBtn.dataset.msgId = evt.id;
                cpBtn.addEventListener("click", () => showCheckpointModal(streamChatId, evt.id, cpBtn));
                toolbar.appendChild(cpBtn);
              }
            }
          } else if (evt.type === "memory_used") {
            if (Array.isArray(evt.memories) && evt.memories.length) {
              if (evt.source === "tool") ui.attachToolMemory(evt.memories);
              else if (userMsgDiv) attachMemoryChip(userMsgDiv, evt.memories);
            }
          } else if (evt.type === "round_thinking") {
            ui.showRoundThinking(evt.text || "");
          } else if (evt.type === "thinking") {
            // Legacy fallback — backend no longer sends raw thinking tokens
            ui.appendThinking(evt.text || "");
          } else if (evt.type === "answer") {
            if (!gotAnswer) {
              ui.closeThinking();
              gotAnswer = true;
              // Remove backend status cards from all previous turns; keep the current streaming one
              const _allTurns = streamPane?.querySelectorAll('.turn') || [];
              for (let i = 0; i < _allTurns.length - 1; i++) {
                _allTurns[i].querySelectorAll('.backend-status-card').forEach(c => c.remove());
              }
            }
            // Recovery: backend auto-switched successfully — clear pending error cards
            ui._pendingRateLimit = null;
            ui._pendingCaptchaBlock = null;
            ui._pendingNoSlot = null;
            ui.appendAnswer(evt.text || "");
          } else if (evt.type === "done") {
            gotDone = true;
            // Update backend status card to "Done" then collapse after a beat.
            // Same fallback as the `status` branch: the card may have been
            // reparented to the pane by a mid-stream rebuild.
            const _lastTurn = streamPane?.querySelector('.turn:last-child') || streamPane;
            if (_lastTurn) {
              const _bsCard = _lastTurn.querySelector('.backend-status-card');
              if (_bsCard) {
                const _statusEl = _bsCard.querySelector('.bs-status');
                if (_statusEl) {
                  _statusEl.textContent = 'Done';
                  _statusEl.style.color = 'var(--success, #4ade80)';
                }
                _bsCard.querySelectorAll('.bs-step-active').forEach(el => {
                  el.classList.remove('bs-step-active');
                  el.classList.add('bs-step-done');
                });
                if (!_bsCard.classList.contains('collapsed')) _bsCard.classList.add('collapsed');
              }
              // Safety net: sweep away any lingering completed account-switch card
              _lastTurn.querySelectorAll('.account-switch-card.asc-complete').forEach(c => {
                clearTimeout(c._ascDismissTimer);
                c.classList.add('asc-exit');
                setTimeout(() => c.remove(), 350);
              });
            }
            if (activeChatId === streamChatId) {
              parentId = evt.parent_id || parentId;
              saveActiveChat();
            }
          } else if (evt.type === "rate_limited") {
            // Don't break — backend auto-switches and emits account_switch events.
            // The existing handleAccountSwitchEvent renders the proper card.
            // If stream ends without recovery, finalize() renders the permanent card.
            ui._pendingRateLimit = { message: evt.message, hours: evt.hours, debug: evt };
          } else if (evt.type === "waf_blocked") {
            // Don't break — backend auto-switches and emits account_switch events.
            ui._pendingCaptchaBlock = { message: evt.message, debug: evt };
          } else if (evt.type === "no_slot") {
            // Both browser slots are held by live chat browsers. This is not an
            // error — it is a choice. Render the kill card instead of a toast.
            ui._pendingNoSlot = { message: evt.message, slots: evt.slots || [] };
          } else if (evt.type === "error") {
            gotError = true;
            const msg = evt.message || "Unknown error";
            // Persistent in-chat card — a toast vanishes in 4s and markdown collapses
            // multi-line token lists. This stays in the transcript for debugging.
            ui.replaceWithError(msg);
          } else if (evt.type === "tool_call") {
            const _tcName = evt.data?.name || "unknown";
            const _tcAttrs = evt.data?.attrs || {};
            const _tcSummary = _tcAttrs.path || _tcAttrs.pattern || _tcAttrs.command || _tcAttrs.query || _tcAttrs.title || "";
            ui.addEvent(`⚙ ${_tcName}${_tcSummary ? ": " + String(_tcSummary).slice(0, 120) : ""}`);
          } else if (evt.type === "tool_result") {
            const _trText = evt.data?.text || evt.data?.output || "";
            ui.addEvent(`✓ result: ${String(_trText).slice(0, 300)}`);
          } else if (evt.type === "tool_pending") {
            // The parser emits this the instant it sees a tool-call tag open —
            // exactly when prose stops and XML begins. Closing here keeps the
            // tool-call XML generation time out of the measurement window; the
            // XML never reaches us as text, so it would otherwise be counted
            // as time spent writing characters we never saw.
            ui.closeBurst();
            ui.showToolPending(evt);
            _toolPending++;
          } else if (evt.type === "tool_progress") {
            ui.showToolProgress(evt);
          } else if (evt.type === "parse_error") {
            // Parser couldn't parse tool_call JSON — clear pending animation
            ui.showToolDone();
            if (_toolPending > 0) _toolPending--;
          } else if (evt.type === "skill_start") {
            // Hand the pending slot over to the running-tool slot.
            if (_toolPending > 0) _toolPending--;
            _toolDepth++;
            if (evt.name === "ask_user") {
              if (!gotAnswer) { ui.closeThinking(); gotAnswer = true; }
              continue; // MCQ card rendered on skill_output
            }
            if (!gotAnswer) { ui.closeThinking(); gotAnswer = true; }
            ui.showToolDone();
            ui.closeBurst();
            ui.addSkillStart(evt);
            // Track path for preview card injection at skill_end
            if (evt.name === "create_file" || evt.name === "edit_file" || evt.name === "save_svg" || evt.name === "create_svg") {
              const _d = evt.data && (evt.data.attrs || evt.data);
              _lastSkillPath = (_d && (_d.path || _d.filename)) || "";
              _lastSkillName = evt.name;
            }
          } else if (evt.type === "skill_output") {
            if (evt.name === "ask_user") {
              try { ui.addAskUser(JSON.parse(evt.text)); } catch(e) { ui.appendSkillOutput(evt); }
              continue;
            }
            ui.appendSkillOutput(evt);
          } else if (evt.type === "skill_end") {
            if (evt.name === "ask_user") continue;
            ui.finishSkill(evt);
            if (_toolDepth > 0) _toolDepth--;

          } else if (evt.type === "doc_changed") {
            // A study tool mutated a document (highlights / notes / progress /
            // bookmark). The study route forwards this side-channel event on the
            // live stream; rebroadcast it as a window event so the reader and
            // notes editor refresh the panes that just changed underneath the
            // user — same path the reload case uses.
            window.dispatchEvent(new CustomEvent('study-doc-changed', { detail: evt }));

          } else if (evt.type === "ui_block") {
            // Rich UI block — a response format, not a tool. Append it as an
            // inline segment in the SAME bubble as the surrounding text.
            if (!gotAnswer) { ui.closeThinking(); gotAnswer = true; }
            if (typeof ui.addComponent === "function") {
              // Pass the WHOLE event — addComponent needs id/partial/remove to
              // re-render streaming partials into one node.
              ui.addComponent(evt);
            } else {
              renderUiBlock(evt.payload, streamPane);
            }
            scrollBottom();
          } else if (evt.type === "html_start" || evt.type === "html_chunk" || evt.type === "html_end") {
            if (!gotAnswer) { ui.closeThinking(); gotAnswer = true; }
            if (typeof ui.addHtml === "function") ui.addHtml(evt);
            scrollBottom();
          } else if (evt.type === "permission_request") {
            if (!gotAnswer) { ui.closeThinking(); gotAnswer = true; }
            // Bind the banner to the stream's chat, but only surface it if the
            // user is actually looking at that chat right now.
            if (activeChatId === streamChatId) renderApprovalCard(evt, streamPane, streamChatId);
          } else if (evt.type === "approval_pending") {
            // Transient "waiting" indicator — removed after approve/deny
            if (!gotAnswer) { ui.closeThinking(); gotAnswer = true; }
            const pending = document.createElement('div');
            pending.className = 'approval-pending-note';
            pending.textContent = evt.text || '⏳ Waiting for your approval…';
            if (streamPane) {
              const turn = streamPane.querySelector('.turn:last-child');
              (turn || streamPane.querySelector('.messages')).appendChild(pending);
            }
          } else if (evt.type === "cwd_warning") {
            if (!gotAnswer) { ui.closeThinking(); gotAnswer = true; }
            if (activeChatId === streamChatId) renderCwdWarningCard(evt, streamPane, streamChatId);
          } else if (evt.type === "cwd_warning_pending") {
            if (!gotAnswer) { ui.closeThinking(); gotAnswer = true; }
            const pending = document.createElement('div');
            pending.className = 'cwd-warning-pending-note';
            pending.textContent = evt.text || '⚠️ File operation outside project folder detected.';
            if (streamPane) {
              const turn = streamPane.querySelector('.turn:last-child');
              (turn || streamPane.querySelector('.messages')).appendChild(pending);
            }
          } else if (evt.type === "permission_batch") {
            // Backend signals all permission/cwd requests for this round — init the batch tracker
            if (!gotAnswer) { ui.closeThinking(); gotAnswer = true; }
            initApprovalBatch(
              evt.permission_ids || [],
              evt.cwd_ids || [],
              streamChatId,
            );
          } else if (evt.type === "critique_start") {
            // Create inline critique box in the chat
            if (!gotAnswer) { ui.closeThinking(); gotAnswer = true; }
            ui.showToolDone();
            const pane = streamPane;
            if (pane) {
              const turn = pane.querySelector('.turn:last-child') || pane.querySelector('.messages');
              if (turn) {
                const box = document.createElement('div');
                box.className = 'critique-box';
                box.dataset.critiqueId = evt.id || '';
                box.innerHTML = `
                  <div class="critique-header">
                    <div class="critique-header-left">
                      <span class="critique-icon"><i data-lucide="search-check"></i></span>
                      <span class="critique-title">Critique Session</span>
                      <span class="critique-badge" data-focus="${escHtml(evt.focus || 'general')}">${escHtml(evt.focus || 'general')}</span>
                    </div>
                    <div class="critique-header-right">
                      <button class="critique-stop-btn" title="Stop critique"><i data-lucide="square"></i></button>
                      <div class="critique-status"><span class="critique-spinner"></span> Reviewing...</div>
                    </div>
                  </div>
                  <details class="critique-details">
                    <summary>Context & Criteria</summary>
                    <div class="critique-details-body">
                      <div class="critique-section">
                        <div class="critique-label">Context</div>
                        <div class="critique-text">${escHtml(evt.context || '')}</div>
                      </div>
                      <div class="critique-section">
                        <div class="critique-label">Criteria</div>
                        <div class="critique-text">${escHtml(evt.criteria || '')}</div>
                      </div>
                    </div>
                  </details>
                  <div class="critique-log"></div>
                `;
                // Wire stop button to abort the critique stream + tell server to stop
                const stopBtn = box.querySelector('.critique-stop-btn');
                if (stopBtn) {
                  stopBtn.addEventListener('click', async () => {
                    const ctrl = box._critiqueController;
                    if (ctrl && !ctrl.signal.aborted) {
                      stopBtn.style.display = 'none';
                      const statusEl = box.querySelector('.critique-status');
                      if (statusEl) statusEl.innerHTML = '<span style="color:var(--warn)">Stopping...</span>';
                      // Tell the server to stop upstream generation first
                      try {
                        const critiqueChatId = activeChatId + '-critique-' + (box.dataset.critiqueId || '');
                        const _respId = typeof window.getActiveResponseId === "function"
                          ? window.getActiveResponseId(critiqueChatId)
                          : null;
                        await fetch('/api/chat/stop', {
                          method: 'POST',
                          headers: { 'Content-Type': 'application/json' },
                          body: JSON.stringify({ chat_id: critiqueChatId, response_id: _respId }),
                        });
                      } catch (_) {}
                      // Then abort the client-side stream
                      ctrl.abort();
                    }
                  });
                }
                turn.appendChild(box);
                activateLucideIcons(box);
                scrollBottom();
              }
            }
          } else if (evt.type === "critique_tool") {
            // Append tool activity to the critique log
            const pane = streamPane;
            if (pane) {
              const box = pane.querySelector(`.critique-box[data-critique-id="${evt.id}"]`);
              if (box) {
                const log = box.querySelector('.critique-log');
                if (log) {
                  const entry = document.createElement('div');
                  entry.className = 'critique-tool-entry';
                  entry.innerHTML = `<span class="critique-tool-name">${escHtml(evt.tool || '')}</span><span class="critique-tool-output">${escHtml((evt.output || '').slice(0, 200))}</span>`;
                  log.appendChild(entry);
                  scrollBottom();
                }
              }
            }
          } else if (evt.type === "critique_trigger") {
            // Fire a normal chat turn, stream response into the critique card
            const pane = streamPane;
            if (pane && evt.message) {
              const box = pane.querySelector(`.critique-box[data-critique-id="${evt.id}"]`);
              if (box) {
                box.dataset.turnActive = "1"; // prevent critique_done from racing
                const log = box.querySelector('.critique-log');
                const statusEl = box.querySelector('.critique-status');
                if (statusEl) statusEl.innerHTML = '<span class="critique-spinner"></span> Running...';
                _runCritiqueTurn(evt.message, box, log, statusEl, evt.id, evt.browser_data_dir, streamChatId);
              }
            }
            // Refresh sidebar so the critique sub-chat appears live
            if (typeof window._sableLoadChats === "function") {
              window._sableLoadChats();
            }
          } else if (evt.type === "critique_done") {
            // Backend skill_end fires immediately — if _runCritiqueTurn is still streaming,
            // don't overwrite the "Running..." status. Only handle errors or fallback.
            const pane = streamPane;
            if (pane) {
              const box = pane.querySelector(`.critique-box[data-critique-id="${evt.id}"]`);
              if (box && box.dataset.turnActive !== "1") {
                const statusEl = box.querySelector('.critique-status');
                if (evt.error) {
                  if (statusEl) statusEl.innerHTML = '<span style="color:var(--error)">Failed</span>';
                  const log = box.querySelector('.critique-log');
                  if (log) log.innerHTML += `<div class="critique-error">${escHtml(evt.error)}</div>`;
                  box.classList.add('critique-done');
                } else if (evt.report) {
                  // Fallback: only render if no active turn handled it
                  if (statusEl) statusEl.innerHTML = '<span style="color:var(--ok)">Complete</span>';
                  const log = box.querySelector('.critique-log');
                  if (log) {
                    log.className = 'critique-report';
                    log.innerHTML = renderMarkdown(evt.report);
                  }
                  box.classList.add('critique-done');
                }
                scrollBottom();
              }
            }
          } else if (evt.type === "agent_start") {
            // Handled exclusively by the agent-events EventSource in agents.js.
            // Ignoring here prevents double-triggering.
            if (typeof window._sableLoadChats === "function") {
              window._sableLoadChats();
            }
          } else if (evt.type === "agent_trigger") {
            // Handled exclusively by the agent-events EventSource in agents.js.
            // Ignoring here prevents double-triggering.
          } else if (evt.type === "chat_title") {
            gotTitle = true;
            const newTitle = (evt.title || "").trim();
            if (newTitle && activeChatId === streamChatId) {
              // Update sidebar — targeted DOM update instead of full rebuild
              const chatMeta = chatList.find(c => c.id === activeChatId);
              if (chatMeta) chatMeta.title = newTitle;
              const titleRow = chatsEl.querySelector(`.chat-row[data-chat-id="${CSS.escape(activeChatId)}"] .chat-item`);
              if (titleRow) titleRow.textContent = newTitle;
              // Update open tab
              const tab = openTabs.get(activeChatId);
              if (tab) { tab.title = newTitle; renderTabBar(); }
              if (typeof window.updateCompactTitle === "function") window.updateCompactTitle(newTitle);
            }
          } else if (evt.type === "file_edit") {
            handleFileEdit(evt, false);
            window.dispatchEvent(new CustomEvent("sable:file-edit", { detail: evt }));
            ui.trackFileEdit(evt);
            // Live-refresh the Monaco editor if the edited file is currently open
            if (typeof window.refreshIdeFile === "function" && evt.path) {
              window.refreshIdeFile(evt.path);
            }
          }
        }
        if (gotError) break;
      }
      return { gotAnswer, gotDone, gotError, gotTitle };
    }

    async function retryLastCommand(skillEvents, bar, btn) {
      if (isStreaming()) return;
      if (!activeChatId) { showToast("No active chat", "error"); return; }

      bar.remove();
      const streamChatId = activeChatId;
      const ui = addBotStreaming();
      startStream(streamChatId);

      try {
        const res = await fetch("/api/retry-command", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            chat_id: activeChatId,
            skill_events: skillEvents,
            model: selectedModel,
            thinking_mode: selectedThinkingMode
          })
        });

        if (!res.ok) {
          let detail = "";
          try { detail = await res.text(); } catch (_) {}
          const msg = `Retry failed ${res.status}${detail ? ": " + detail.slice(0, 300) : ""}`;
          ui.replaceWithError(msg);
          return;
        }

        const { gotAnswer, gotDone, gotError } = await consumeChatStream(res, ui, null, streamChatId);
        if (!gotAnswer && !gotError && !gotDone) {
          ui.replaceWithError("Stream ended without a response");
        }
      } catch (err) {
        ui.replaceWithError("Connection lost: " + err.message);
      } finally {
        ui.finalize();
        endStream(streamChatId);
      }
    }


// ── Backend Status Card (retries, errors, recovery) ─────────────────────────
const _BACKEND_STATUS_MAP = {
  processing:                              { label: "Processing request…",              icon: "loader" },
  qwen_transport_ghost:                    { label: "Qwen via ghost tab",               icon: "ghost" },
  qwen_transport_curl:                     { label: "Qwen via raw API (curl)",          icon: "terminal" },
  deepseek_transport_ghost:                { label: "DeepSeek via ghost tab",           icon: "ghost" },
  deepseek_transport_curl:                 { label: "DeepSeek via raw API (curl)",      icon: "terminal" },
  first_chunk_timeout_triggering_switch:   { label: "No response after 3 attempts — switching…", icon: "triangle-alert" },
  stream_stall_timeout_triggering_switch:  { label: "Stream stalled — switching…",      icon: "triangle-alert" },
  empty_response_exhausted_triggering_switch: { label: "Empty responses exhausted — switching…", icon: "triangle-alert" },
  recovering_session:                      { label: "Session expired — recovering…",    icon: "refresh-cw" },
  recovering_parent:                       { label: "Parent message lost — recovering…", icon: "refresh-cw" },
  waiting_for_agents:                      { label: "Waiting for agents…",              icon: "users" },
  high_skill_round_count:                  { label: "High skill round count",           icon: "alert-circle" },
  requesting_teacher_guidance:             { label: "Asking teacher for guidance…",     icon: "graduation-cap" },
  feeding_skill_results:                   { label: "Processing tool results…",         icon: "wrench" },
};

function _backendStatusIcon(name, size = 14) {
  return `<i data-lucide="${name}" style="width:${size}px;height:${size}px"></i>`;
}

function handleBackendStatusEvent(evt, container) {
  const msg = evt.message || "";
  if (!msg) return;

  // Parse retry patterns: retrying_timeout_2, retrying_stall_1, empty_response_retry_3, chat_in_progress_retry_5
  let parsedLabel = null;
  let parsedIcon = "refresh-cw";
  const retryMatch = msg.match(/^(retrying_timeout|retrying_stall|empty_response_retry|chat_in_progress_retry)_(\d+)$/);
  if (retryMatch) {
    const kind = retryMatch[1];
    const attempt = retryMatch[2];
    const labels = {
      retrying_timeout:       `Timeout — retry ${attempt}/3…`,
      retrying_stall:         `Stream stall — retry ${attempt}/3…`,
      empty_response_retry:   `Empty response — retry ${attempt}…`,
      chat_in_progress_retry: `Chat in progress — check ${attempt}/10…`,
    };
    parsedLabel = labels[kind] || msg;
    parsedIcon = kind.startsWith("empty") ? "ghost" : kind.startsWith("chat_in") ? "message-square" : "timer";
  }

  // Parse delay patterns: deepseek_delay_5.2s, deepseek_scraper_delay_3.1s,
  // qwen_delay_3.1s, zai_delay_7.0s, gemini_delay_4.5s
  const delayMatch = msg.match(/^(deepseek_scraper|qwen_scraper|deepseek|qwen|zai|gemini)_delay_([\d.]+)s$/);
  if (delayMatch) {
    const _dl = { "deepseek_scraper": "DeepSeek (scraper)", "qwen_scraper": "Qwen (scraper)" };
    const provider = _dl[delayMatch[1]]
      || delayMatch[1].charAt(0).toUpperCase() + delayMatch[1].slice(1);
    const seconds = delayMatch[2];
    parsedLabel = `${provider} jitter delay — waiting ${seconds}s…`;
    parsedIcon = "timer";
  }

  // Parse file-attach status: browser_file_attached:<name> or
  // browser_file_attach_failed:<reason>:<name>. The failure variant is
  // highlighted as an error card; the turn still proceeds WITHOUT the file.
  let attachError = false;
  const attachOk = msg.match(/^browser_file_attached:(.+)$/);
  const attachFail = msg.match(/^browser_file_attach_failed:([^:]+):(.+)$/);
  if (attachOk) {
    parsedLabel = `Attached: ${attachOk[1]}`;
    parsedIcon = "paperclip";
  } else if (attachFail) {
    const reason = attachFail[1];
    const fname = attachFail[2];
    const reasonText = {
      rate_limited:       "rate limited by ChatGPT — try again shortly",
      file_too_large:     "file too large for ChatGPT",
      not_authorized:     "session not authorized — re-login",
      network_error:      "network error during upload",
      timeout:            "upload timed out",
      upload_failed:      "upload failed",
    }[reason] || (reason.startsWith("server_error") ? "ChatGPT server error"
              : reason.startsWith("upload_rejected") ? "ChatGPT rejected the upload"
              : reason.replace(/_/g, " "));
    parsedLabel = `Couldn't attach ${fname} — ${reasonText}. Sending without it.`;
    parsedIcon = "triangle-alert";
    attachError = true;
  }

  const mapped = _BACKEND_STATUS_MAP[msg];
  const label = parsedLabel || (mapped ? mapped.label : msg.replace(/_/g, " "));
  const icon = parsedLabel ? parsedIcon : (mapped ? mapped.icon : "info");

  // Find or create the card (usually pre-created by addBotStreaming with
  // the shimmer processing text already visible in the header).
  let card = container.querySelector(".backend-status-card");
  if (!card) {
    card = document.createElement("div");
    card.className = "skill-card backend-status-card";
    card.innerHTML = `
      <div class="skill-header">
        <div class="skill-header-left">
          <span class="skill-name">${_backendStatusIcon("terminal", 15)} Backend Status</span>
        </div>
        <div class="skill-header-right" style="display:flex;align-items:center;gap:8px;">
          <span class="skill-status bs-status">working…</span>
        </div>
      </div>
      <div class="bs-steps"></div>`;
    card.querySelector(".skill-header").onclick = () => card.classList.toggle("collapsed");
    container.appendChild(card);
    if (typeof activateLucideIcons === "function") activateLucideIcons(card);
  }

  // Real pipeline step arrived — hide the initial shimmer processing text
  const procText = card.querySelector(".bs-processing-text");
  if (procText) procText.style.display = "none";

  const stepsEl = card.querySelector(".bs-steps");
  const statusEl = card.querySelector(".bs-status");

  // Deactivate all previous rows
  stepsEl.querySelectorAll(".bs-step-active").forEach(el => {
    el.classList.remove("bs-step-active");
    el.classList.add("bs-step-done");
  });

  // Add new row
  const row = document.createElement("div");
  row.className = "bs-step bs-step-active";
  if (attachError) {
    row.classList.add("bs-step-error");
    row.style.color = "var(--danger, #f87171)";
  }
  row.innerHTML = `<span class="bs-step-icon">${_backendStatusIcon(icon)}</span><span class="bs-step-label">${label}</span>`;
  stepsEl.appendChild(row);

  // Update header status text
  if (statusEl) statusEl.textContent = label;

  if (typeof activateLucideIcons === "function") activateLucideIcons(card);
}


// ── Account Switch Status Card ──────────────────────────────────────────────
const _ACCOUNT_SWITCH_STEPS = [
  { key: "triggered",        label: "Trigger detected",       icon: "triangle-alert" },
  { key: "searching",        label: "Searching accounts…",    icon: "search" },
  { key: "switching",        label: "Switching account…",     icon: "repeat" },
  { key: "syncing",          label: "Syncing context…",       icon: "clipboard-list" },
  { key: "summarizing",      label: "Summarizing history…",   icon: "file-text" },
  { key: "creating_session", label: "Creating new session…",  icon: "message-square" },
  { key: "warming_up",       label: "Warming up WAF…",        icon: "flame" },
  { key: "retrying",         label: "Retrying with next…",    icon: "refresh-cw" },
  { key: "complete",         label: "Switch complete",         icon: "circle-check" },
  { key: "failed",           label: "Failed",                  icon: "circle-x" },
];

function _ascIcon(name, size = 14) {
  return `<i data-lucide="${name}" style="width:${size}px;height:${size}px"></i>`;
}

function handleAccountSwitchEvent(evt, container) {
  // Find or create the card
  let card = container.querySelector(".account-switch-card");
  if (!card) {
    card = document.createElement("div");
    card.className = "skill-card account-switch-card";
    card.innerHTML = `
      <div class="skill-header">
        <div class="skill-header-left">
          <span class="skill-arrow">${_ascIcon("chevron-down")}</span>
          <span class="skill-name">${_ascIcon("shuffle", 15)} Account Switch</span>
        </div>
        <div class="skill-header-right" style="display:flex;align-items:center;gap:8px;">
          <span class="skill-status asc-status">initializing…</span>
        </div>
      </div>
      <div class="asc-steps"></div>`;
    card.querySelector(".skill-header").onclick = () => card.classList.toggle("collapsed");
    container.appendChild(card);
    if (typeof activateLucideIcons === "function") activateLucideIcons(card);
  }

  // Track completed steps across retry cycles (persisted on card element)
  if (!card._ascCompleted) card._ascCompleted = new Set();

  const stepsEl = card.querySelector(".asc-steps");
  const statusEl = card.querySelector(".asc-status");
  const step = evt.step;

  // Any new activity cancels a pending auto-dismiss left over from a prior "complete"
  if (step !== "complete" && card._ascDismissTimer) {
    clearTimeout(card._ascDismissTimer);
    card._ascDismissTimer = null;
    card.classList.remove("asc-exit");
  }

  // On retry, reset intermediate steps (searching..warming_up) back to pending
  if (step === "retrying") {
    const resetKeys = ["searching", "switching", "syncing", "summarizing", "creating_session", "warming_up"];
    resetKeys.forEach(k => {
      card._ascCompleted.delete(k);
      const r = stepsEl.querySelector(`[data-step="${k}"]`);
      if (r) {
        r.classList.remove("asc-step-done", "asc-step-active");
        const oldDetail = r.querySelector(".asc-step-detail");
        if (oldDetail) oldDetail.remove();
      }
    });
  }

  // Mark current step and all prior linear steps as done (using persistent set for retry safety)
  const stepIndex = _ACCOUNT_SWITCH_STEPS.findIndex(s => s.key === step);
  // For non-retry steps, mark all preceding steps as completed
  if (step !== "retrying" && step !== "failed") {
    for (let i = 0; i < stepIndex; i++) {
      card._ascCompleted.add(_ACCOUNT_SWITCH_STEPS[i].key);
    }
  }

  _ACCOUNT_SWITCH_STEPS.forEach((s, i) => {
    let row = stepsEl.querySelector(`[data-step="${s.key}"]`);
    if (!row && (card._ascCompleted.has(s.key) || i <= stepIndex)) {
      row = document.createElement("div");
      row.className = "asc-step";
      row.dataset.step = s.key;
      row.innerHTML = `<span class="asc-step-icon">${_ascIcon(s.icon)}</span><span class="asc-step-label">${s.label}</span>`;
      stepsEl.appendChild(row);
    }
    if (row) {
      if (step === "failed" && s.key === "failed") {
        row.classList.add("asc-step-failed");
        row.classList.remove("asc-step-active", "asc-step-done");
        row.querySelector(".asc-step-label").textContent = `Failed: ${evt.error || "unknown error"}`;
      } else if (s.key === step && step !== "failed") {
        // Current active step
        row.classList.add("asc-step-active");
        row.classList.remove("asc-step-done");
        // Update contextual detail (replace existing on retry cycles)
        let detail = row.querySelector(".asc-step-detail");
        if (evt.reason || evt.from || evt.to || evt.account || evt.error) {
          if (!detail) {
            detail = document.createElement("span");
            detail.className = "asc-step-detail";
            row.appendChild(detail);
          }
          if (step === "triggered") detail.textContent = evt.reason === "rate_limit" ? "(rate limited)" : "(WAF/captcha block)";
          else if (step === "switching") detail.textContent = `${evt.from} → ${evt.to}`;
          else if (step === "searching") detail.textContent = `current: ${evt.current}${evt.attempt > 1 ? ` (attempt ${evt.attempt})` : ""}`;
          else if (step === "retrying") detail.textContent = `${evt.account} (${evt.reason})`;
          else if (evt.account) detail.textContent = evt.account;
        }
      } else if (card._ascCompleted.has(s.key)) {
        // Previously completed step (survives retry cycles)
        row.classList.add("asc-step-done");
        row.classList.remove("asc-step-active");
      }
    }
  });

  // Re-render lucide icons for newly added rows
  if (typeof activateLucideIcons === "function") activateLucideIcons(card);

  // Update header status text
  if (step === "complete") {
    statusEl.textContent = `${evt.account || "switched"}`;
    card.classList.add("asc-complete");
    const finalRow = stepsEl.querySelector(`[data-step="complete"]`);
    if (finalRow) { finalRow.classList.add("asc-step-done"); finalRow.classList.remove("asc-step-active"); }
    // Auto-dismiss: let the user see the success, then fade the card out so it
    // doesn't linger on screen for the rest of the conversation.
    clearTimeout(card._ascDismissTimer);
    card._ascDismissTimer = setTimeout(() => {
      card.classList.add("asc-exit");
      setTimeout(() => card.remove(), 350);
    }, 1600);
  } else if (step === "failed") {
    statusEl.textContent = "failed";
    card.classList.add("asc-failed");
  } else {
    const meta = _ACCOUNT_SWITCH_STEPS.find(s => s.key === step);
    statusEl.textContent = meta ? meta.label : step;
  }

  // Auto-scroll steps into view
  const activeRow = stepsEl.querySelector(".asc-step-active, .asc-step-failed");
  if (activeRow) activeRow.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

// ---------------------------------------------------------------------------
// Token Rotation Card (DeepSeek API token switching)
// ---------------------------------------------------------------------------

const _TOKEN_ROTATION_REASONS = {
  round_robin:    { label: "Round-robin rotation", icon: "refresh-cw" },
  timeout:        { label: "Timeout failover",     icon: "clock" },
  empty_response: { label: "Empty response",       icon: "circle-alert" },
  "HTTP 401":     { label: "Auth failed (401)",    icon: "shield-alert" },
  "HTTP 403":     { label: "Forbidden (403)",      icon: "shield-x" },
  "HTTP 429":     { label: "Rate limited (429)",   icon: "gauge" },
};

function handleTokenRotationEvent(evt, container) {
  let card = container.querySelector(".token-rotation-card");
  if (!card) {
    card = document.createElement("div");
    card.className = "skill-card token-rotation-card";
    card.innerHTML = `
      <div class="skill-header">
        <div class="skill-header-left">
          <span class="skill-arrow"><i data-lucide="chevron-down" style="width:14px;height:14px"></i></span>
          <span class="skill-name"><i data-lucide="repeat" style="width:15px;height:15px"></i> Token Rotation</span>
        </div>
        <div class="skill-header-right" style="display:flex;align-items:center;gap:8px;">
          <span class="skill-status tr-status">switching…</span>
        </div>
      </div>
      <div class="tr-details"></div>`;
    card.querySelector(".skill-header").onclick = () => card.classList.toggle("collapsed");
    container.appendChild(card);
    if (typeof activateLucideIcons === "function") activateLucideIcons(card);
  }

  const detailsEl = card.querySelector(".tr-details");
  const statusEl = card.querySelector(".tr-status");
  const reasonMeta = _TOKEN_ROTATION_REASONS[evt.reason] || { label: evt.reason, icon: "arrow-right-left" };

  // Build detail row
  const row = document.createElement("div");
  row.className = "tr-detail-row";
  row.innerHTML = `
    <span class="tr-reason"><i data-lucide="${reasonMeta.icon}" style="width:12px;height:12px"></i> ${reasonMeta.label}</span>
    <span class="tr-token-from" title="Previous token">${evt.from_token || "???"}</span>
    <span class="tr-arrow">→</span>
    <span class="tr-token-to" title="New token">${evt.to_token || "???"}</span>
    <span class="tr-index">#${evt.to_index + 1}/${evt.total_tokens}</span>`;
  detailsEl.appendChild(row);

  if (typeof activateLucideIcons === "function") activateLucideIcons(card);

  // Update status
  statusEl.textContent = `token #${evt.to_index + 1}/${evt.total_tokens}`;

  // Auto-scroll
  row.scrollIntoView({ behavior: "smooth", block: "nearest" });
}



