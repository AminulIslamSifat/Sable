
    function addHistoryMessage(message) {
      clearEmptyState();

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
        activePane.appendChild(wrap);
        // ponytail: Skip per-element icon scan during bulk history render
        if (!window._historyLoading) activateLucideIcons(wrap);
      }

      // For assistant messages with round_text events, skip main content rendering —
      // _renderSkillEvents will replay text + skills in streaming order via round_text.
      // This prevents the "all text first, then all commands" mesh.
      const skipMainContent = message.role !== "user" && hasRoundText;
      const msgDiv = skipMainContent ? null : addMessage(message.role === "user" ? "user" : "bot", displayContent);
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
        // Ensure user messages from DB have copy + TTS toolbar
        let toolbar = msgDiv.querySelector(".msg-toolbar");
        if (!toolbar) {
          toolbar = document.createElement("div");
          toolbar.className = "msg-toolbar";
          msgDiv.appendChild(toolbar);
        }
        if (!toolbar.querySelector('[title="Copy"]')) {
          const copyBtn = document.createElement("button");
          copyBtn.innerHTML = '<i data-lucide="copy"></i>';
          copyBtn.title = "Copy";
          copyBtn.addEventListener("click", () => {
            const userTextEl = msgDiv.querySelector(".user-text");
            const copyText = userTextEl ? userTextEl.textContent : displayContent;
            navigator.clipboard.writeText(copyText).then(() => {
              copyBtn.innerHTML = '<i data-lucide="check"></i>';
              activateLucideIcons(copyBtn);
              setTimeout(() => { copyBtn.innerHTML = '<i data-lucide="copy"></i>'; activateLucideIcons(copyBtn); }, 1500);
            });
          });
          toolbar.appendChild(copyBtn);
        }
        if (!toolbar.querySelector('[title="Read aloud"]')) {
          const ttsBtn = document.createElement("button");
          ttsBtn.innerHTML = '<i data-lucide="volume-2"></i>';
          ttsBtn.title = "Read aloud";
          ttsBtn.addEventListener("click", async () => {
            const now = Date.now();
            const delta = now - _ttsLastAction;
            const btnId = ttsBtn.dataset.msgId || ttsBtn.closest('[data-id]')?.dataset.id || 'unknown';
            const activeBtnId = _activeTTS.btn ? (_activeTTS.btn.dataset.msgId || _activeTTS.btn.closest('[data-id]')?.dataset.id || 'unknown') : 'none';
            console.log(`[TTS-DEBUG] user-msg click | delta=${delta}ms | _ttsActive=${_ttsActive} | gen=${_ttsGeneration} | btnMsg=${btnId} | activeBtnMsg=${activeBtnId} | btnTitle=${ttsBtn.title}`);
            if (delta < TTS_DEBOUNCE_MS) {
              console.log(`[TTS-DEBUG] user-msg click BLOCKED by debounce (${delta}ms < ${TTS_DEBOUNCE_MS}ms)`);
              return;
            }
            if (_ttsActive) {
              console.log(`[TTS-DEBUG] user-msg click → stopping active TTS`);
              stopGlobalTTS();
              return;
            }
            _ttsLastAction = now;
            const userTextEl = msgDiv.querySelector(".user-text");
            const text = userTextEl ? userTextEl.textContent : displayContent;
            if (!text) return;
            _ttsActive = true;
            const gen = ++_ttsGeneration;
            _activeTTS.gen = gen;
            console.log(`[TTS-DEBUG] user-msg START | gen=${gen} | textLen=${text.length}`);
            const player = new TTSStreamPlayer((state) => {
              console.log(`[TTS-DEBUG] user-msg onStateChange="${state}" | gen=${gen} | currentGen=${_ttsGeneration}`);
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
                // Natural completion: reset _ttsActive so next click can start
                // If stopGlobalTTS() initiated this stop, it handles _ttsActive via timeout
                if (!_ttsStopping) {
                  _ttsActive = false;
                  console.log(`[TTS-DEBUG] user-msg natural end | _ttsActive→false`);
                }
                _activeTTS.player = null;
                _activeTTS.btn = null;
                _activeTTS.gen = -1;
              }
              activateLucideIcons(ttsBtn);
            });
            _activeTTS.player = player;
            _activeTTS.btn = ttsBtn;
            player.play(text);
          });
          toolbar.appendChild(ttsBtn);
        }
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
        _renderSkillEvents(events);
      } else if (message.has_skill_events && message.id) {
        // Lazy-load skill_events from the API
        _lazyLoadSkillEvents(message.id, message.chat_id || activeChatId);
      }
    }

    // Attach copy + TTS toolbar to a .msg.bot div (reused by history + round_text)
    function _attachBotToolbar(msgDiv) {
      if (!msgDiv || msgDiv.querySelector(".msg-toolbar")) return;
      const toolbar = document.createElement("div");
      toolbar.className = "msg-toolbar";
      const copyBtn = document.createElement("button");
      copyBtn.innerHTML = '<i data-lucide="copy"></i>';
      copyBtn.title = "Copy";
      copyBtn.addEventListener("click", () => {
        const md = msgDiv.querySelector(".md-content");
        const text = md ? md.innerText : "";
        navigator.clipboard.writeText(text).then(() => {
          copyBtn.innerHTML = '<i data-lucide="check"></i>';
          activateLucideIcons(copyBtn);
          setTimeout(() => { copyBtn.innerHTML = '<i data-lucide="copy"></i>'; activateLucideIcons(copyBtn); }, 1500);
        });
      });
      const ttsBtn = document.createElement("button");
      ttsBtn.innerHTML = '<i data-lucide="volume-2"></i>';
      ttsBtn.title = "Read aloud";
      ttsBtn.addEventListener("click", async () => {
        const now = Date.now();
        const delta = now - _ttsLastAction;
        if (delta < TTS_DEBOUNCE_MS) return;
        if (_ttsActive) { stopGlobalTTS(); return; }
        _ttsLastAction = now;
        const md = msgDiv.querySelector(".md-content");
        const text = md ? md.innerText : "";
        if (!text) return;
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
        player.play(text);
      });
      toolbar.appendChild(copyBtn);
      toolbar.appendChild(ttsBtn);
      msgDiv.appendChild(toolbar);
      // ponytail: Skip per-element icon scan during bulk history render
      if (!window._historyLoading) activateLucideIcons(toolbar);
    }

    // ponytail: Cap stored skill_output text during history replay.
    // Backend caps live streaming at 20K chars, but DB may hold unlimited
    // output from before the cap existed. Prevents DOM explosion on load.
    const HISTORY_SKILL_OUTPUT_CAP = 20000;

    // ponytail: Max skill_output events rendered per card during history load.
    // Prevents 800-event zip outputs from creating 800 DOM mutations on replay.
    // Live streaming uses the backend batch+cap; this guards old DB records.
    const HISTORY_MAX_OUTPUT_EVENTS_PER_CARD = 50;

    function _renderSkillEvents(events) {
      const cards = {};
      const cardOutputCounts = {};
      let group = null;
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
          const wrap = document.createElement("div");
          wrap.className = "thinking-wrap";
          wrap.innerHTML = `
            <details class="thinking">
              <summary><i data-lucide="chevron-right" class="thinking-chevron"></i>Thinking</summary>
              <div class="thinking-body">${escHtml(evt.text || "")}</div>
            </details>`;
          activePane.appendChild(wrap);
          _actIcons(wrap);
        } else if (evt.type === "round_text") {
          if (evt.text && evt.text.trim()) {
            const textDiv = document.createElement("div");
            textDiv.className = "msg bot";
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
            textDiv.appendChild(content);
            activePane.appendChild(textDiv);
            _attachBotToolbar(textDiv);
          }
        } else if (evt.type === "skill_start") {
          if (evt.name === "ask_user") continue;
          if (!group) {
            group = document.createElement("div");
            group.className = "skill-stack";
            group.style.display = "flex";
            activePane.appendChild(group);
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
              const card = renderAskUserCard(JSON.parse(evt.text), activePane);
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
          if (activePane) {
            const turn = activePane.querySelector('.turn:last-child');
            (turn || activePane).appendChild(note);
          }
        } else if (evt.type === "cwd_warning") {
          // History replay: decision already made — show static note
          const note = document.createElement('div');
          note.className = 'cwd-warning-pending-note';
          note.textContent = '⚠️ CWD warning: ' + (evt.data?.path || '').slice(0, 80);
          if (activePane) {
            const turn = activePane.querySelector('.turn:last-child');
            (turn || activePane).appendChild(note);
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
          activePane.appendChild(box);
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
        } else if (evt.type === "memory_used") {
          if (Array.isArray(evt.memories) && evt.memories.length) {
            const chip = createMemoryChip(evt.memories);
            chip.classList.add("memory-chip-tool");
            const allCards = activePane.querySelectorAll(".skill-card");
            const target = allCards.length ? allCards[allCards.length - 1] : null;
            if (target) {
              const right = target.querySelector(".skill-header-right");
              if (right) right.insertBefore(chip, right.firstChild);
              else target.querySelector(".skill-header")?.appendChild(chip);
            } else {
              activePane.appendChild(chip);
            }
          }
        }
      }
    }

    async function _lazyLoadSkillEvents(messageId, chatId) {
      try {
        const data = await fetch(`/api/chats/${chatId}/messages/${messageId}/events`).then(r => r.json());
        const events = data.skill_events || [];
        if (events.length > 0) {
          _renderSkillEvents(events);
        }
      } catch (err) {
        console.error("Failed to lazy-load skill events:", err);
      }
    }

    // ── Ask User MCQ Card ──
    function renderAskUserCard(payload, container) {
      const { question, options, multi, default: def } = payload;
      const card = document.createElement("div");
      card.className = "ask-user-card";

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
        // Send as normal user message
        inputEl.value = answer;
        sendMessage();
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
      scrollBottom();
      return card;
    }

    // ── Permission Approval Banner ──
    function renderApprovalCard(evt, container) {
      const { id, name, data } = evt;
      const { command, category, reason } = data;
      const banner = document.getElementById('approvalBanner');
      if (!banner) return;

      const catIcons = {
        filesystem: 'trash-2', packages: 'package', services: 'settings',
        git: 'git-branch', network: 'globe', auth: 'shield',
        disk: 'hard-drive', process: 'cpu', database: 'database', system: 'terminal'
      };
      const icon = catIcons[category] || 'alert-triangle';
      const shortCmd = command.length > 80 ? command.slice(0, 80) + '…' : command;

      banner.className = 'approval-banner';
      banner.dataset.tagId = id;
      banner.innerHTML = `
        <div class="ab-icon"><i data-lucide="${icon}"></i></div>
        <div class="ab-body">
          <div class="ab-title">${shortCmd.replace(/</g,'&lt;').replace(/>/g,'&gt;')}</div>
          <div class="ab-sub">${category} · ${reason}</div>
        </div>
        <div class="ab-actions">
          <button class="ab-allow"><i data-lucide="check"></i> Allow</button>
          <button class="ab-allow-session"><i data-lucide="shield-check"></i> Allow for Session</button>
          <button class="ab-deny"><i data-lucide="x"></i> Deny</button>
        </div>
      `;

      const allowBtn = banner.querySelector('.ab-allow');
      const allowSessionBtn = banner.querySelector('.ab-allow-session');
      const denyBtn = banner.querySelector('.ab-deny');

      allowBtn.addEventListener('click', async () => {
        allowBtn.disabled = true;
        denyBtn.disabled = true;
        allowSessionBtn.disabled = true;
        // Remove transient "waiting" note
        activePane?.querySelectorAll('.approval-pending-note').forEach(el => el.remove());
        try {
          console.log('[approval] allow clicked, id:', id);
          const resp = await fetch('/api/skills/approve/' + id, { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({chat_id: activeChatId}) });
          console.log('[approval] response status:', resp.status);

          if (resp.headers.get('content-type')?.includes('text/event-stream')) {
            banner.classList.add('ab-resolved');
            if (activePane) {
              const card = createSkillCard({ name: name, data: { content: command } });
              const status = card.querySelector('.skill-status');
              status.textContent = 'approved \u2713';
              status.style.color = 'var(--ok)';
              const turn = activePane.querySelector('.turn:last-child');
              const target = turn ? (turn.querySelector('.skill-stack:last-of-type') || turn) : activePane.querySelector('.messages');
              if (target) { target.appendChild(card); activateLucideIcons(card); }
              activePane.querySelector('.messages')?.scrollTo({top: 999999, behavior:'smooth'});
            }
            const reader = resp.body.getReader();
            const dec = new TextDecoder();
            let buf = '';
            let output = '';
            while (true) {
              const { done, value } = await reader.read();
              if (done) break;
              buf += dec.decode(value, { stream: true });
              const lines = buf.split('\n');
              buf = lines.pop();
              for (const line of lines) {
                if (!line.startsWith('data: ')) continue;
                try {
                  const ev = JSON.parse(line.slice(6));
                  if (ev.type === 'skill_output' && ev.text) output += ev.text;
                  if (ev.type === 'skill_end') {
                    const st = document.createElement('span');
                    st.className = 'ab-status ' + (ev.ok ? 'ok' : 'no');
                    st.textContent = ev.ok ? 'done' : 'failed';
                    banner.querySelector('.ab-actions').replaceWith(st);
                  }
                } catch(e) {}
              }
            }
            if (output) {
              const out = document.createElement('div');
              out.className = 'ab-output';
              out.textContent = output.slice(0, 500);
              banner.appendChild(out);
              if (activePane) {
                const lastCard = activePane.querySelector('.turn:last-child .skill-card:last-of-type');
                if (lastCard) {
                  lastCard.querySelector('.skill-output').textContent = output.slice(0, 2000);
                  const st = lastCard.querySelector('.skill-status');
                  if (st) { st.textContent = 'done \u2713'; st.style.color = 'var(--ok)'; }
                }
              }
            }
            setTimeout(() => sendAutoTurnMessage('[System: Command was approved and executed. Continue.]', { skipUserBubble: true, skipUserSave: true }), 300);
          } else {
            const data = await resp.json();
            // Check application-level success, not just HTTP status
            if (!data.ok) {
              banner.classList.add('ab-resolved');
              const st = document.createElement('span');
              st.className = 'ab-status no';
              st.textContent = data.error || 'expired';
              banner.querySelector('.ab-actions').replaceWith(st);
              // Re-enable buttons so user sees the failure state
              allowBtn.disabled = false;
              denyBtn.disabled = false;
              allowSessionBtn.disabled = false;
              return; // Don't hide banner or send auto-turn
            }
            banner.classList.add('ab-resolved');
            if (activePane) {
              const card = createSkillCard({ name: name, data: { content: command } });
              const status = card.querySelector('.skill-status');
              status.textContent = 'approved \u2713';
              status.style.color = 'var(--ok)';
              const turn = activePane.querySelector('.turn:last-child');
              const target = turn ? (turn.querySelector('.skill-stack:last-of-type') || turn) : activePane.querySelector('.messages');
              if (target) { target.appendChild(card); activateLucideIcons(card); }
              activePane.querySelector('.messages')?.scrollTo({top: 999999, behavior:'smooth'});
            }
            const st = document.createElement('span');
            st.className = 'ab-status ok';
            st.textContent = 'done';
            banner.querySelector('.ab-actions').replaceWith(st);
            if (data.feedback) {
              if (activePane) {
                const lastCard = activePane.querySelector('.turn:last-child .skill-card:last-of-type');
                if (lastCard) {
                  lastCard.querySelector('.skill-output').textContent = String(data.feedback).slice(0, 2000);
                  const cst = lastCard.querySelector('.skill-status');
                  if (cst) { cst.textContent = 'done \u2713'; cst.style.color = 'var(--ok)'; }
                }
              }
              setTimeout(() => sendAutoTurnMessage(data.feedback, { skipUserBubble: true, skipUserSave: true }), 300);
            } else {
              // No feedback but command succeeded — still tell the model to continue
              setTimeout(() => sendAutoTurnMessage('[System: Command was approved and executed. Continue.]', { skipUserBubble: true, skipUserSave: true }), 300);
            }
          }
        } catch(e) {
          banner.classList.add('ab-resolved');
          const st = document.createElement('span');
          st.className = 'ab-status no';
          st.textContent = 'error';
          banner.querySelector('.ab-actions')?.replaceWith(st);
        }
        setTimeout(() => banner.classList.add('hidden'), 4000);
      });


      allowSessionBtn.addEventListener('click', async () => {
        allowBtn.disabled = true;
        allowSessionBtn.disabled = true;
        denyBtn.disabled = true;
        activePane?.querySelectorAll('.approval-pending-note').forEach(el => el.remove());
        try {
          const resp = await fetch('/api/skills/approve/' + id, { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({chat_id: activeChatId, session: true}) });
          const data = await resp.json();
          // Check application-level success before creating any UI
          if (!data.ok) {
            banner.classList.add('ab-resolved');
            const st = document.createElement('span');
            st.className = 'ab-status no';
            st.textContent = data.error || 'expired';
            banner.querySelector('.ab-actions').replaceWith(st);
            allowBtn.disabled = false;
            allowSessionBtn.disabled = false;
            denyBtn.disabled = false;
            return;
          }
          banner.classList.add('ab-resolved');
          if (activePane) {
            const card = createSkillCard({ name: name, data: { content: command } });
            const status = card.querySelector('.skill-status');
            status.textContent = 'approved (session) \u2713';
            status.style.color = 'var(--ok)';
            const turn = activePane.querySelector('.turn:last-child');
            const target = turn ? (turn.querySelector('.skill-stack:last-of-type') || turn) : activePane.querySelector('.messages');
            if (target) { target.appendChild(card); activateLucideIcons(card); }
            activePane.querySelector('.messages')?.scrollTo({top: 999999, behavior:'smooth'});
          }
          const st = document.createElement('span');
          st.className = 'ab-status ok';
          st.textContent = 'session ✓';
          banner.querySelector('.ab-actions').replaceWith(st);
          if (data.feedback) {
            if (activePane) {
              const lastCard = activePane.querySelector('.turn:last-child .skill-card:last-of-type');
              if (lastCard) {
                lastCard.querySelector('.skill-output').textContent = String(data.feedback).slice(0, 2000);
                const cst = lastCard.querySelector('.skill-status');
                if (cst) { cst.textContent = 'done \u2713'; cst.style.color = 'var(--ok)'; }
              }
            }
            setTimeout(() => sendAutoTurnMessage(data.feedback, { skipUserBubble: true, skipUserSave: true }), 300);
          } else {
            setTimeout(() => sendAutoTurnMessage('[System: Command was approved and executed. Continue.]', { skipUserBubble: true, skipUserSave: true }), 300);
          }
        } catch(e) {
          banner.classList.add('ab-resolved');
          const st = document.createElement('span');
          st.className = 'ab-status no';
          st.textContent = 'error';
          banner.querySelector('.ab-actions')?.replaceWith(st);
        }
        setTimeout(() => banner.classList.add('hidden'), 4000);
      });



      denyBtn.addEventListener('click', async () => {
        allowBtn.disabled = true;
        allowSessionBtn.disabled = true;
        denyBtn.disabled = true;
        // Remove transient "waiting" note
        activePane?.querySelectorAll('.approval-pending-note').forEach(el => el.remove());
        banner.classList.add('ab-resolved');
        const st = document.createElement('span');
        st.className = 'ab-status no';
        st.textContent = 'denied';
        if (activePane) {
          const card = createSkillCard({ name: name, data: { content: command } });
          const status = card.querySelector('.skill-status');
          status.textContent = 'denied ✗';
          status.style.color = 'var(--danger)';
          card.querySelector('.skill-output').textContent = '[denied by user]';
          const turn = activePane.querySelector('.turn:last-child');
          const target = turn ? (turn.querySelector('.skill-stack:last-of-type') || turn) : activePane.querySelector('.messages');
          if (target) { target.appendChild(card); activateLucideIcons(card); }
          activePane.querySelector('.messages')?.scrollTo({top: 999999, behavior:'smooth'});
        }
        banner.querySelector('.ab-actions').replaceWith(st);
        try {
          console.log('[approval] deny clicked, id:', id, 'chat:', activeChatId);
          const r = await fetch('/api/skills/deny/' + id, { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({chat_id: activeChatId}) });
          console.log('[approval] deny status:', r.status);
          const data = await r.json();
          if (data.feedback) {
            setTimeout(() => sendAutoTurnMessage(data.feedback, { skipUserBubble: true, skipUserSave: true }), 300);
          } else {
            setTimeout(() => sendAutoTurnMessage('[System: Command was denied by user.]', { skipUserBubble: true, skipUserSave: true }), 300);
          }
        } catch(e) {
          console.error('[approval] deny error:', e);
          setTimeout(() => sendAutoTurnMessage('[System: Command was denied by user.]'), 300);
        }
        setTimeout(() => banner.classList.add('hidden'), 3000);
      });

      activateLucideIcons(banner);
    }

    function renderCwdWarningCard(evt, container) {
      const { id, name, data } = evt;
      const { path, cwd } = data;
      const banner = document.getElementById('approvalBanner');
      if (!banner) return;

      const shortPath = path.length > 80 ? '…' + path.slice(-77) : path;

      banner.className = 'approval-banner cwd-warning-banner';
      banner.dataset.tagId = id;
      banner.innerHTML = `
        <div class="ab-icon"><i data-lucide="folder-alert"></i></div>
        <div class="ab-body">
          <div class="ab-title">File operation outside project folder</div>
          <div class="ab-sub">${shortPath.replace(/</g,'&lt;').replace(/>/g,'&gt;')}</div>
          <div class="ab-detail">Without making it the project folder, you can't recover in case of accidental damage.</div>
        </div>
        <div class="ab-actions">
          <button class="ab-cwd-session"><i data-lucide="shield-check"></i> Allow for Session</button>
          <button class="ab-cwd-continue"><i data-lucide="arrow-right"></i> Continue</button>
          <button class="ab-cwd-open"><i data-lucide="folder-open"></i> Open Folder</button>
          <button class="ab-cwd-deny"><i data-lucide="x"></i> Deny</button>
        </div>
      `;

      const sessionBtn = banner.querySelector('.ab-cwd-session');
      const continueBtn = banner.querySelector('.ab-cwd-continue');
      const openBtn = banner.querySelector('.ab-cwd-open');
      const denyBtn = banner.querySelector('.ab-cwd-deny');

      function disableAllCwdBtns() {
        if (sessionBtn) sessionBtn.disabled = true;
        continueBtn.disabled = true;
        openBtn.disabled = true;
        if (denyBtn) denyBtn.disabled = true;
      }

      sessionBtn?.addEventListener('click', async () => {
        disableAllCwdBtns();
        activePane?.querySelectorAll('.cwd-warning-pending-note').forEach(el => el.remove());
        try {
          const resp = await fetch('/api/skills/cwd-approve/' + id, {
            method: 'POST',
            headers: {'Content-Type':'application/json'},
            body: JSON.stringify({chat_id: activeChatId, session: true}),
          });
          banner.classList.add('ab-resolved');
          if (resp.ok && activePane) {
            const card = createSkillCard({ name: name, data: { attrs: { path: path } } });
            const status = card.querySelector('.skill-status');
            status.textContent = 'allowed for session ✓';
            status.style.color = 'var(--ok)';
            const turn = activePane.querySelector('.turn:last-child');
            const target = turn ? (turn.querySelector('.skill-stack:last-of-type') || turn) : activePane.querySelector('.messages');
            if (target) { target.appendChild(card); activateLucideIcons(card); }
            activePane.querySelector('.messages')?.scrollTo({top: 999999, behavior:'smooth'});
          }
          const result = await resp.json();
          const st = document.createElement('span');
          st.className = 'ab-status ok';
          st.textContent = 'session allowed';
          banner.querySelector('.ab-actions').replaceWith(st);
          if (result.feedback) {
            setTimeout(() => sendAutoTurnMessage(result.feedback, { skipUserBubble: true, skipUserSave: true }), 300);
          }
        } catch(e) {
          banner.classList.add('ab-resolved');
          const st = document.createElement('span');
          st.className = 'ab-status no';
          st.textContent = 'error';
          banner.querySelector('.ab-actions')?.replaceWith(st);
        }
        setTimeout(() => banner.classList.add('hidden'), 4000);
      });

      continueBtn.addEventListener('click', async () => {
        disableAllCwdBtns();
        activePane?.querySelectorAll('.cwd-warning-pending-note').forEach(el => el.remove());
        try {
          const resp = await fetch('/api/skills/cwd-approve/' + id, {
            method: 'POST',
            headers: {'Content-Type':'application/json'},
            body: JSON.stringify({chat_id: activeChatId}),
          });
          banner.classList.add('ab-resolved');
          if (resp.ok && activePane) {
            const card = createSkillCard({ name: name, data: { attrs: { path: path } } });
            const status = card.querySelector('.skill-status');
            status.textContent = 'approved ✓';
            status.style.color = 'var(--ok)';
            const turn = activePane.querySelector('.turn:last-child');
            const target = turn ? (turn.querySelector('.skill-stack:last-of-type') || turn) : activePane.querySelector('.messages');
            if (target) { target.appendChild(card); activateLucideIcons(card); }
            activePane.querySelector('.messages')?.scrollTo({top: 999999, behavior:'smooth'});
          }
          const result = await resp.json();
          const st = document.createElement('span');
          st.className = 'ab-status ok';
          st.textContent = 'done';
          banner.querySelector('.ab-actions').replaceWith(st);
          if (result.feedback) {
            setTimeout(() => sendAutoTurnMessage(result.feedback, { skipUserBubble: true, skipUserSave: true }), 300);
          }
        } catch(e) {
          banner.classList.add('ab-resolved');
          const st = document.createElement('span');
          st.className = 'ab-status no';
          st.textContent = 'error';
          banner.querySelector('.ab-actions')?.replaceWith(st);
        }
        setTimeout(() => banner.classList.add('hidden'), 4000);
      });

      openBtn.addEventListener('click', async () => {
        continueBtn.disabled = true;
        openBtn.disabled = true;
        activePane?.querySelectorAll('.cwd-warning-pending-note').forEach(el => el.remove());
        try {
          const res = await fetch('/api/filesystem/pick-folder');
          const pickData = await res.json();
          if (pickData.path && window.pickFsRoot) {
            window.pickFsRoot(pickData.path);
            // After changing CWD, approve the operation with new context
            const resp = await fetch('/api/skills/cwd-approve/' + id, {
              method: 'POST',
              headers: {'Content-Type':'application/json'},
              body: JSON.stringify({chat_id: activeChatId}),
            });
            banner.classList.add('ab-resolved');
            const result = await resp.json();
            const st = document.createElement('span');
            st.className = 'ab-status ok';
            st.textContent = 'folder changed ✓';
            banner.querySelector('.ab-actions').replaceWith(st);
            if (result.feedback) {
              setTimeout(() => sendAutoTurnMessage(result.feedback, { skipUserBubble: true, skipUserSave: true }), 300);
            }
          } else {
            // User cancelled folder picker — re-enable buttons
            continueBtn.disabled = false;
            openBtn.disabled = false;
            return;
          }
        } catch(e) {
          banner.classList.add('ab-resolved');
          const st = document.createElement('span');
          st.className = 'ab-status no';
          st.textContent = 'error';
          banner.querySelector('.ab-actions')?.replaceWith(st);
        }
        setTimeout(() => banner.classList.add('hidden'), 4000);
      });

      denyBtn?.addEventListener('click', async () => {
        disableAllCwdBtns();
        activePane?.querySelectorAll('.cwd-warning-pending-note').forEach(el => el.remove());
        banner.classList.add('ab-resolved');

        const st = document.createElement('span');
        st.className = 'ab-status no';
        st.textContent = 'denied';

        if (activePane) {
          const card = createSkillCard({ name: name, data: { attrs: { path: path } } });
          const status = card.querySelector('.skill-status');
          if (status) {
            status.textContent = 'denied ✗';
            status.style.color = 'var(--danger)';
          }
          const output = card.querySelector('.skill-output');
          if (output) output.textContent = '[denied by user]';

          const turn = activePane.querySelector('.turn:last-child');
          const target = turn ? (turn.querySelector('.skill-stack:last-of-type') || turn) : activePane.querySelector('.messages');
          if (target) { target.appendChild(card); activateLucideIcons(card); }
          activePane.querySelector('.messages')?.scrollTo({top: 999999, behavior:'smooth'});
        }

        banner.querySelector('.ab-actions')?.replaceWith(st);

        try {
          const resp = await fetch('/api/skills/cwd-deny/' + id, {
            method: 'POST',
            headers: {'Content-Type':'application/json'},
            body: JSON.stringify({chat_id: activeChatId}),
          });
          const result = await resp.json();
          if (result.feedback) {
            setTimeout(() => sendAutoTurnMessage(result.feedback, { skipUserBubble: true, skipUserSave: true }), 300);
          } else {
            setTimeout(() => sendAutoTurnMessage('[System: File operation outside project was denied by user.]', { skipUserBubble: true, skipUserSave: true }), 300);
          }
        } catch(e) {
          console.error('[cwd-warning] deny error:', e);
          setTimeout(() => sendAutoTurnMessage('[System: File operation outside project was denied by user.]', { skipUserBubble: true, skipUserSave: true }), 300);
        }

        setTimeout(() => banner.classList.add('hidden'), 3000);
      });

      activateLucideIcons(banner);
    }


    // one "turn" holds everything for a single response: thinking, then any
    // skill/tool runs it made, then the final answer — all stacked in order,
    // scoped to just this response (not shared globally).
    function addBotStreaming(pane = activePane, chatId = activeChatId) {
      clearPaneEmptyState(pane);

      // Capture the chat this turn belongs to — typewriter ticks and scroll
      // calls will bail if the user has switched away before they fire.
      const turnChatId = chatId;

      const turn = document.createElement("div");
      turn.className = "turn";
      pane.appendChild(turn);

      // Immediate feedback that the message was sent and a response is on
      // its way — removed as soon as any real content (thinking, a skill
      // event, or an answer token) actually arrives.
      const pending = document.createElement("div");
      pending.className = "pending-indicator";
      pending.innerHTML = `<span class="processing-text">processing…</span>`;
      turn.appendChild(pending);
      let pendingShown = true;
      function hidePending() {
        if (!pendingShown) return;
        pendingShown = false;
        pending.remove();
        ensureAnswer();
      }

      // Per-round thinking: each agentic command gets its own thinking block
      // inserted right before its skill card, instead of one global bucket.
      let currentThinkWrap = null;
      let currentThinkBody = null;
      let currentThinkSummary = null;

      // ── Typewriter animation for thinking reveal ──
      let _thinkQueue = "";
      let _thinkTimer = null;
      function _thinkTick() {
        // Bail if user switched to a different chat while timer was pending
        if (turnChatId !== activeChatId) { _thinkTimer = null; return; }
        if (!_thinkQueue || !currentThinkBody) { _thinkTimer = null; return; }
        const chunk = _thinkQueue.slice(0, TW_CHARS);
        _thinkQueue = _thinkQueue.slice(TW_CHARS);
        currentThinkBody.textContent += chunk;
        scrollBottom();
        _thinkTimer = _thinkQueue ? setTimeout(_thinkTick, TW_MS) : null;
      }
      function _enqueueThink(text) {
        _thinkQueue += text;
        if (!_thinkTimer) _thinkTimer = setTimeout(_thinkTick, TW_MS);
      }
      function _flushThinkQueue() {
        if (_thinkTimer) { clearTimeout(_thinkTimer); _thinkTimer = null; }
        if (_thinkQueue && currentThinkBody) {
          currentThinkBody.textContent += _thinkQueue;
          _thinkQueue = "";
        }
      }

      function ensureThinkingBlock() {
        // Create a fresh thinking block for the current round.
        // It will be placed just before the next skill command group or answer.
        if (currentThinkWrap) return;
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
        turn.appendChild(currentThinkWrap);
        activateLucideIcons(currentThinkWrap);
      }

      function closeCurrentThinking() {
        if (!currentThinkWrap) return;
        _flushThinkQueue();
        if (currentThinkSummary) { currentThinkSummary.innerHTML = '<i data-lucide="chevron-right" class="thinking-chevron"></i>Thinking'; activateLucideIcons(currentThinkSummary); }
        const det = currentThinkWrap.querySelector("details");
        if (det) det.open = false;
        currentThinkWrap = null;
        currentThinkBody = null;
        currentThinkSummary = null;
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

      function ensureAnswer() {
        if (answerEl) return;
        answerEl = document.createElement("div");
        answerEl.className = "msg bot streaming msg-enter";
        const content = document.createElement("div");
        content.className = "md-content";
        answerEl.appendChild(content);
        answerContent = content;
        raw = "";
        turn.appendChild(answerEl);
        lastCommandGroup = null;
      }

      // ── Typewriter animation for answer reveal ──
      let _ansQueue = "";
      let _ansTimer = null;
      let _ansInFence = false;
      const _ANS_STRUCTURAL_RE = /[\n`|<>#*_\[~=~-]/;

      function _ansTick() {
        // Bail if user switched to a different chat while timer was pending
        if (turnChatId !== activeChatId) { _ansTimer = null; return; }
        if (!_ansQueue || !answerContent) { _ansTimer = null; return; }
        const chunk = _ansQueue.slice(0, TW_CHARS);
        _ansQueue = _ansQueue.slice(TW_CHARS);
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
        _ansTimer = _ansQueue ? setTimeout(_ansTick, TW_MS) : null;
        // Only activate lucide icons when chunk contains a data-lucide placeholder
        // or at end-of-stream — avoids calling createIcons() every 12ms tick
        if (chunk.includes("data-lucide") || !_ansTimer) {
          activateLucideIcons(answerContent);
        }
        if (!_ansTimer) {
          // Final tick render: produce proper mermaid-wrap but defer heavy rendering
          // to closeAnswer() which runs after stream truly ends or segment closes.
          // This prevents partial math/mermaid from rendering mid-stream.
          answerContent.innerHTML = renderMarkdown(raw);
          // Neutralize mermaid during streaming to prevent flicker
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
      }
      function _enqueueAnswer(text) {
        _ansQueue += text;
        if (!_ansTimer) _ansTimer = setTimeout(_ansTick, TW_MS);
      }
      function _flushAnswerQueue() {
        if (_ansTimer) { clearTimeout(_ansTimer); _ansTimer = null; }
        if (_ansQueue && answerContent) {
          raw += _ansQueue;
          _ansQueue = "";
          // Only do lightweight markdown render here; heavy mermaid/math rendering
          // is deferred to closeAnswer() to avoid rendering partial content mid-stream.
          answerContent.innerHTML = renderMarkdown(raw);
          activateLucideIcons(answerContent);
          scrollBottom();
        }
      }

      function closeAnswer() {
        _flushAnswerQueue();
        if (!answerEl) return;
        // Skip markdown re-render for special cards (rate-limit, captcha) that
        // already have their final HTML set via innerHTML.
        const _isSpecialCard = raw === "__rate_limit_card__" || raw === "__captcha_block_card__";
        // Final render with full mermaid + math support — only runs when this answer
        // segment is truly done (stream end or skill interleave boundary).
        if (answerContent && raw && !_isSpecialCard) {
          answerContent.innerHTML = renderMarkdown(raw);
          renderMermaidDiagrams(answerContent);
          renderMathJax(answerContent);
          activateLucideIcons(answerContent);
        }
        answerEl.classList.remove("streaming");
        if (!_isSpecialCard && !raw.trim()) answerEl.remove();
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

      scrollBottom();
      return {
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
          turn.appendChild(currentThinkWrap);
          activateLucideIcons(currentThinkWrap);
          _enqueueThink(text);
        },
        addSkillStart(evt) {
          hidePending();
          closeCurrentThinking();
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
          if (skillRounds[skillRounds.length - 1].length) skillRounds.push([]);
        },
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
          // Errors/stop messages render instantly; normal text gets typewriter
          if (/\[(error|stopped|client error)\]/.test(text)) {
            _flushAnswerQueue();
            raw += text;
            answerContent.innerHTML = renderMarkdown(raw);
            activateLucideIcons(answerContent);
            scrollBottom();
          } else {
            _enqueueAnswer(text);
          }
        },
        replaceWithRateLimit(message, hours, debugInfo) {
          hidePending();
          // Kill typewriter queues immediately — don't flush partial content
          if (_thinkTimer) { clearTimeout(_thinkTimer); _thinkTimer = null; }
          _thinkQueue = "";
          if (_ansTimer) { clearTimeout(_ansTimer); _ansTimer = null; }
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
          if (_thinkTimer) { clearTimeout(_thinkTimer); _thinkTimer = null; }
          _thinkQueue = "";
          if (_ansTimer) { clearTimeout(_ansTimer); _ansTimer = null; }
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
          hidePending();
          closeCurrentThinking();
          // If auto-switch didn't recover, render the permanent error card now.
          // closeAnswer() will remove the transient switching-status card first.
          const _prl = this._pendingRateLimit;
          const _pcb = this._pendingCaptchaBlock;
          closeAnswer();
          if (_prl) {
            this.replaceWithRateLimit(_prl.message, _prl.hours, _prl.debug);
            this._pendingRateLimit = null;
          } else if (_pcb) {
            this.replaceWithCaptchaBlock(_pcb.message, _pcb.debug);
            this._pendingCaptchaBlock = null;
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
          if (!turn.querySelector(".msg.bot") && !turn.querySelector(".skill-card") && !turn.querySelector(".approval-pending-note")) {
            ensureAnswer();
            answerEl.classList.remove("streaming");
            answerContent.textContent = "⚠ Empty response from upstream — check server terminal for WAF/auth details.";
          }
          // Attach toolbar to every bot message in this turn
          turn.querySelectorAll(".msg.bot").forEach(botEl => {
            if (botEl.querySelector(".msg-toolbar")) return;
            const toolbar = document.createElement("div");
            toolbar.className = "msg-toolbar";

            const copyBtn = document.createElement("button");
            copyBtn.innerHTML = '<i data-lucide="copy"></i>';
            copyBtn.title = "Copy";
            copyBtn.addEventListener("click", () => {
              const md = botEl.querySelector(".md-content");
              const text = md ? md.innerText : "";
              navigator.clipboard.writeText(text).then(() => {
                copyBtn.innerHTML = '<i data-lucide="check"></i>';
                activateLucideIcons(copyBtn);
                setTimeout(() => { copyBtn.innerHTML = '<i data-lucide="copy"></i>'; activateLucideIcons(copyBtn); }, 1500);
              });
            });

            const regenBtn = document.createElement("button");
            regenBtn.innerHTML = '<i data-lucide="refresh-cw"></i>';
            regenBtn.title = "Regenerate";
            regenBtn.addEventListener("click", () => {
              if (isStreaming()) return;
              // Find the preceding user message in this chat
              const pane = botEl.closest(".tab-pane") || activePane;
              const allMsgs = Array.from(pane.querySelectorAll(".msg.user"));
              const thisTurn = botEl.closest(".turn");
              let prevUser = null;
              for (const u of allMsgs) {
                if (u.compareDocumentPosition(thisTurn) & Node.DOCUMENT_POSITION_FOLLOWING) {
                  prevUser = u;
                }
              }
              if (!prevUser) { showToast("No user message to regenerate from", "error"); return; }
              const userText = prevUser.querySelector(".user-text")?.textContent || "";
              if (!userText) return;
              // Remove this turn from UI
              turn.remove();
              // Re-send with current parentId (server will branch)
              inputEl.value = userText;
              sendMessage();
            });

            // TTS read-aloud button (streaming)
            const ttsBtn = document.createElement("button");
            ttsBtn.innerHTML = '<i data-lucide="volume-2"></i>';
            ttsBtn.title = "Read aloud";
            ttsBtn.addEventListener("click", async () => {
              const now = Date.now();
              const delta = now - _ttsLastAction;
              console.log(`[TTS-DEBUG] stream-bot click | delta=${delta}ms | _ttsActive=${_ttsActive} | gen=${_ttsGeneration}`);
              if (delta < TTS_DEBOUNCE_MS) {
                console.log(`[TTS-DEBUG] stream-bot click BLOCKED by debounce (${delta}ms < ${TTS_DEBOUNCE_MS}ms)`);
                return;
              }
              if (_ttsActive) {
                console.log(`[TTS-DEBUG] stream-bot click → stopping active TTS`);
                stopGlobalTTS();
                return;
              }
              _ttsLastAction = now;
              const md = botEl.querySelector(".md-content");
              const text = md ? md.innerText : "";
              if (!text) return;
              _ttsActive = true;
              const gen = ++_ttsGeneration;
              _activeTTS.gen = gen;
              console.log(`[TTS-DEBUG] stream-bot START | gen=${gen} | textLen=${text.length}`);
              const player = new TTSStreamPlayer((state) => {
                console.log(`[TTS-DEBUG] stream-bot onStateChange="${state}" | gen=${gen} | currentGen=${_ttsGeneration}`);
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
                  if (!_ttsStopping) {
                    _ttsActive = false;
                    console.log(`[TTS-DEBUG] stream-bot natural end | _ttsActive→false`);
                  }
                  _activeTTS.player = null;
                  _activeTTS.btn = null;
                  _activeTTS.gen = -1;
                }
                activateLucideIcons(ttsBtn);
              });
              _activeTTS.player = player;
              _activeTTS.btn = ttsBtn;
              player.play(text);
            });

            toolbar.appendChild(copyBtn);
            toolbar.appendChild(regenBtn);
            toolbar.appendChild(ttsBtn);
            botEl.appendChild(toolbar);
            activateLucideIcons(toolbar);
          });

          // Commands ran but no normal answer ever arrived — pin a retry bar
          // under the last command group so the tool results can be resent.
          const hasCommands = turn.querySelectorAll(".skill-card").length > 0;
          if (hasCommands && !sawNormalAnswer && !turn.querySelector(".retry-command-bar")) {
            const lastRound = skillRounds.filter(r => r.length).pop() || [];
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
    async function _runCritiqueTurn(message, box, logEl, statusEl, critiqueId, browserDataDir) {
      if (!message || !activeChatId) return;
      const critiqueChatId = activeChatId + "-critique-" + (critiqueId || Date.now().toString(36));
      const controller = startStream(critiqueChatId);
      if (box) box._critiqueController = controller;

      // Helper: extract only the structured report from the full answer text
      function _extractReport(text) {
        const idx = text.indexOf("**Mark:**");
        if (idx !== -1) return text.slice(idx);
        const idx2 = text.search(/^Mark:/m);
        if (idx2 !== -1) return text.slice(idx2);
        return text;
      }

      // Use the critique log as the rendering pane, but pass activeChatId
      // so the typewriter animation doesn't bail (user is viewing this chat).
      const ui = addBotStreaming(logEl, activeChatId);
      let fullAnswer = "";

      // Intercept appendAnswer to also accumulate raw text for report extraction
      const _origAppend = ui.appendAnswer.bind(ui);
      ui.appendAnswer = function(text) {
        if (text) fullAnswer += text;
        _origAppend(text);
      };

      // Update status when skills fire
      const _origSkillStart = ui.addSkillStart.bind(ui);
      let toolCount = 0;
      ui.addSkillStart = function(evt) {
        toolCount++;
        if (statusEl) statusEl.innerHTML = `<span class="critique-spinner"></span> Inspecting... (${toolCount} tools)`;
        _origSkillStart(evt);
      };

      try {
        const body = {
          message,
          chat_id: critiqueChatId,
          parent_id: undefined,
          model: selectedModel,
          thinking_mode: selectedThinkingMode,
          stream: true,
        };
        if (browserDataDir) body.browser_data_dir = browserDataDir;
        const res = await fetch("/api/chat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
          signal: controller.signal,
        });

        if (!res.ok) {
          if (statusEl) statusEl.innerHTML = '<span style="color:var(--error)">Failed</span>';
          ui.appendAnswer(`\n[error] HTTP ${res.status}`);
          ui.finalize();
          return;
        }

        // Live-stream through the standard pipeline — thinking, skills, answer all render
        const { gotError } = await consumeChatStream(res, ui, null, critiqueChatId, logEl);
        ui.finalize();

        if (controller.signal.aborted) {
          if (statusEl) statusEl.innerHTML = '<span style="color:var(--warn)">Stopped</span>';
          if (box) box.classList.add('critique-done');
        } else if (!gotError) {
          if (statusEl) statusEl.innerHTML = '<span style="color:var(--ok)">Complete</span>';
          if (box) {
            box.classList.add('critique-done');
            const sb = box.querySelector('.critique-stop-btn');
            if (sb) sb.style.display = 'none';
          }
          // Send the final report to the main chat as a normal user message.
          if (fullAnswer && typeof sendAutoTurnMessage === "function") {
            const hasStructuredReport = fullAnswer.includes("**Mark:**") || /^Mark:/m.test(fullAnswer);
            if (hasStructuredReport) {
              const report = _extractReport(fullAnswer);
              setTimeout(() => sendAutoTurnMessage(`[Critique Report]\n${report}`), 300);
            }
          }
        } else {
          if (statusEl) statusEl.innerHTML = '<span style="color:var(--error)">Error</span>';
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
          ui.finalize();
        } else {
          if (statusEl) statusEl.innerHTML = '<span style="color:var(--error)">Error</span>';
          ui.appendAnswer(`\n[client error] ${err.message}`);
          ui.finalize();
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
              const _bsTurn = streamPane?.querySelector('.turn:last-child');
              if (_bsTurn) handleBackendStatusEvent(evt, _bsTurn);
            }
          } else if (evt.type === "account_switch") {
            const _ascTurn = streamPane?.querySelector('.turn:last-child');
            if (_ascTurn) handleAccountSwitchEvent(evt, _ascTurn);
          } else if (evt.type === "token_rotation") {
            const _trTurn = streamPane?.querySelector('.turn:last-child');
            if (_trTurn) handleTokenRotationEvent(evt, _trTurn);
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
            ui.appendAnswer(evt.text || "");
          } else if (evt.type === "done") {
            gotDone = true;
            // Collapse backend status card when streaming finishes
            const _lastTurn = streamPane?.querySelector('.turn:last-child');
            if (_lastTurn) {
              const _bsCard = _lastTurn.querySelector('.backend-status-card');
              if (_bsCard && !_bsCard.classList.contains('collapsed')) _bsCard.classList.add('collapsed');
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
          } else if (evt.type === "error") {
            gotError = true;
            const msg = evt.message || "Unknown error";
            showToast(msg, "error");
            ui.appendAnswer(`\n[error] ${msg}`);
          } else if (evt.type === "tool_call") {
            const _tcName = evt.data?.name || "unknown";
            const _tcAttrs = evt.data?.attrs || {};
            const _tcSummary = _tcAttrs.path || _tcAttrs.pattern || _tcAttrs.command || _tcAttrs.query || _tcAttrs.title || "";
            ui.addEvent(`⚙ ${_tcName}${_tcSummary ? ": " + String(_tcSummary).slice(0, 120) : ""}`);
          } else if (evt.type === "tool_result") {
            const _trText = evt.data?.text || evt.data?.output || "";
            ui.addEvent(`✓ result: ${String(_trText).slice(0, 300)}`);
          } else if (evt.type === "tool_pending") {
            ui.showToolPending(evt);
          } else if (evt.type === "tool_progress") {
            ui.showToolProgress(evt);
          } else if (evt.type === "parse_error") {
            // Parser couldn't parse tool_call JSON — clear pending animation
            ui.showToolDone();
          } else if (evt.type === "skill_start") {
            if (evt.name === "ask_user") {
              if (!gotAnswer) { ui.closeThinking(); gotAnswer = true; }
              continue; // MCQ card rendered on skill_output
            }
            if (!gotAnswer) { ui.closeThinking(); gotAnswer = true; }
            ui.showToolDone();
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

          } else if (evt.type === "permission_request") {
            if (!gotAnswer) { ui.closeThinking(); gotAnswer = true; }
            renderApprovalCard(evt, streamPane);
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
            renderCwdWarningCard(evt, streamPane);
          } else if (evt.type === "cwd_warning_pending") {
            if (!gotAnswer) { ui.closeThinking(); gotAnswer = true; }
            const pending = document.createElement('div');
            pending.className = 'cwd-warning-pending-note';
            pending.textContent = evt.text || '⚠️ File operation outside project folder detected.';
            if (streamPane) {
              const turn = streamPane.querySelector('.turn:last-child');
              (turn || streamPane.querySelector('.messages')).appendChild(pending);
            }
          } else if (evt.type === "sim_ready") {
            const fname = evt.filename || "simulation.html";
            const url = "/assets/" + encodeURIComponent(fname);
            const pane = streamPane;
            if (pane) {
              const stack = pane.querySelector(".turn:last-child .skill-stack:last-of-type");
              const target = stack || pane.querySelector(".turn:last-child") || pane;
              const card = document.createElement("div");
              card.className = "skill-card sim-ready-card";
              card.style.cursor = "pointer";
              card.innerHTML = '<div class="skill-header"><div class="skill-header-left"><span class="skill-arrow"><i data-lucide="play-circle"></i></span><span class="skill-name">simulation · ' + fname + '</span></div><div class="skill-header-right"><span class="skill-status" style="color:var(--ok)">ready · click to open</span></div></div>';
              card.onclick = () => window.open(url, "_blank");
              target.appendChild(card);
              activateLucideIcons(card);
              scrollBottom();
            }
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
                _runCritiqueTurn(evt.message, box, log, statusEl, evt.id, evt.browser_data_dir);
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
          showToast(msg, "error");
          ui.appendAnswer(`\n[error] ${msg}`);
          return;
        }

        const { gotAnswer, gotDone, gotError } = await consumeChatStream(res, ui, null, streamChatId);
        if (!gotAnswer && !gotError && !gotDone) {
          const msg = "Stream ended without a response";
          showToast(msg, "error");
          ui.appendAnswer(`\n[error] ${msg}`);
        }
      } catch (err) {
        showToast("Connection lost: " + err.message, "error");
        ui.appendAnswer(`\n[client error] ${err.message}`);
      } finally {
        ui.finalize();
        endStream(streamChatId);
      }
    }


// ── Backend Status Card (retries, errors, recovery) ─────────────────────────
const _BACKEND_STATUS_MAP = {
  processing:                              { label: "Processing request…",              icon: "loader" },
  first_chunk_timeout_triggering_switch:   { label: "No response after 3 attempts — switching…", icon: "triangle-alert" },
  stream_stall_timeout_triggering_switch:  { label: "Stream stalled — switching…",      icon: "triangle-alert" },
  empty_response_exhausted_triggering_switch: { label: "Empty responses exhausted — switching…", icon: "triangle-alert" },
  recovering_session:                      { label: "Session expired — recovering…",    icon: "refresh-cw" },
  recovering_parent:                       { label: "Parent message lost — recovering…", icon: "refresh-cw" },
  waiting_for_agents:                      { label: "Waiting for agents…",              icon: "users" },
  high_skill_round_count:                  { label: "High skill round count",           icon: "alert-circle" },
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

  const mapped = _BACKEND_STATUS_MAP[msg];
  const label = parsedLabel || (mapped ? mapped.label : msg.replace(/_/g, " "));
  const icon = parsedLabel ? parsedIcon : (mapped ? mapped.icon : "info");

  // Find or create the card
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



