
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
        activateLucideIcons(wrap);
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
        activateLucideIcons(toolbar);
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
      activateLucideIcons(toolbar);
    }

    function _renderSkillEvents(events) {
      const cards = {};
      let group = null;
      let _histSkillPath = "";
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
          activateLucideIcons(wrap);
        } else if (evt.type === "round_text") {
          if (evt.text && evt.text.trim()) {
            const textDiv = document.createElement("div");
            textDiv.className = "msg bot";
            const content = document.createElement("div");
            content.className = "md-content";
            content.innerHTML = renderMarkdown(evt.text);
            renderMermaidDiagrams(content);
            renderMathJax(content);
            activateLucideIcons(content);
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
          activateLucideIcons(card);
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
          if (card) appendSkillCardOutput(card, evt.text);
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
        } else if (evt.type === "agent_result") {
          if (typeof addAgentResultCard === "function") {
            addAgentResultCard({
              type: evt.ok ? "agent_completed" : "agent_failed",
              agent_id: evt.agent_id,
              data: evt.data || {},
            });
          }
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
        // Remove transient "waiting" note
        activePane?.querySelectorAll('.approval-pending-note').forEach(el => el.remove());
        try {
          console.log('[approval] allow clicked, id:', id);
          const resp = await fetch('/api/skills/approve/' + id, { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({chat_id: activeChatId}) });
          console.log('[approval] response status:', resp.status);
          banner.classList.add('ab-resolved');
          if (resp.ok && activePane) {
            const card = createSkillCard({ name: name, data: { content: command } });
            const status = card.querySelector('.skill-status');
            status.textContent = 'approved \u2713';
            status.style.color = 'var(--ok)';
            const turn = activePane.querySelector('.turn:last-child');
            const target = turn ? (turn.querySelector('.skill-stack:last-of-type') || turn) : activePane.querySelector('.messages');
            if (target) { target.appendChild(card); activateLucideIcons(card); }
            activePane.querySelector('.messages')?.scrollTo({top: 999999, behavior:'smooth'});
          }
          if (resp.headers.get('content-type')?.includes('text/event-stream')) {
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
              // Also fill the skill card output in the chat
              if (activePane) {
                const lastCard = activePane.querySelector('.turn:last-child .skill-card:last-of-type');
                if (lastCard) {
                  lastCard.querySelector('.skill-output').textContent = output.slice(0, 2000);
                  const st = lastCard.querySelector('.skill-status');
                  if (st) { st.textContent = 'done \u2713'; st.style.color = 'var(--ok)'; }
                }
              }
            }
            // Auto-trigger model turn so it sees the result
            setTimeout(() => sendAutoTurnMessage('[System: Command was approved and executed. Continue.]', { skipUserBubble: true, skipUserSave: true }), 300);
          } else {
            const data = await resp.json();
            const st = document.createElement('span');
            st.className = 'ab-status ok';
            st.textContent = 'done';
            banner.querySelector('.ab-actions').replaceWith(st);
            if (data.feedback) {
              setTimeout(() => sendAutoTurnMessage(data.feedback, { skipUserBubble: true, skipUserSave: true }), 300);
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
          banner.classList.add('ab-resolved');
          if (resp.ok && activePane) {
            const card = createSkillCard({ name: name, data: { content: command } });
            const status = card.querySelector('.skill-status');
            status.textContent = 'approved (session) \u2713';
            status.style.color = 'var(--ok)';
            const turn = activePane.querySelector('.turn:last-child');
            const target = turn ? (turn.querySelector('.skill-stack:last-of-type') || turn) : activePane.querySelector('.messages');
            if (target) { target.appendChild(card); activateLucideIcons(card); }
            activePane.querySelector('.messages')?.scrollTo({top: 999999, behavior:'smooth'});
          }
          const data = await resp.json();
          const st = document.createElement('span');
          st.className = 'ab-status ok';
          st.textContent = 'session ✓';
          banner.querySelector('.ab-actions').replaceWith(st);
          if (data.feedback) {
            setTimeout(() => sendAutoTurnMessage(data.feedback, { skipUserBubble: true, skipUserSave: true }), 300);
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
    function addBotStreaming() {
      clearEmptyState();

      // Capture the chat this turn belongs to — typewriter ticks and scroll
      // calls will bail if the user has switched away before they fire.
      const turnChatId = activeChatId;

      const turn = document.createElement("div");
      turn.className = "turn";
      activePane.appendChild(turn);

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
            document.body.classList.add("diff-open");
            if (typeof AgentPanel !== "undefined") AgentPanel.close();
            // Switch sidebar tab to Diff mode
            if (typeof window.setFsSidebarMode === "function") {
              window.setFsSidebarMode("diff");
            }
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
          closeAnswer();
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

    async function consumeChatStream(res, ui, userMsgDiv, streamChatId) {
      const reader  = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let gotAnswer = false;
      let gotDone = false;
      let gotError = false;
      let gotTitle = false;
      let _lastSkillPath = "";
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
          } else if (evt.type === "status") {
            if (evt.message === "feeding_skill_results") ui.nextSkillRound();
          } else if (evt.type === "account_switch") {
            const _ascTurn = activePane.querySelector('.turn:last-child');
            if (_ascTurn) handleAccountSwitchEvent(evt, _ascTurn);
          } else if (evt.type === "token_rotation") {
            const _trTurn = activePane.querySelector('.turn:last-child');
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
            if (!gotAnswer) { ui.closeThinking(); gotAnswer = true; }
            ui.appendAnswer(evt.text || "");
          } else if (evt.type === "done") {
            gotDone = true;
            if (activeChatId === streamChatId) {
              parentId = evt.parent_id || parentId;
              saveActiveChat();
            }
          } else if (evt.type === "rate_limited") {
            gotError = true;
            ui.replaceWithRateLimit(evt.message, evt.hours, evt);
            break;
          } else if (evt.type === "waf_blocked") {
            gotError = true;
            ui.replaceWithCaptchaBlock(evt.message, evt);
            break;
          } else if (evt.type === "error") {
            gotError = true;
            const msg = evt.message || "Unknown error";
            showToast(msg, "error");
            ui.appendAnswer(`\n[error] ${msg}`);
          } else if (evt.type === "tool_call") {
            ui.addEvent(`⚙ tool: ${JSON.stringify(evt.data).slice(0, 300)}`);
          } else if (evt.type === "tool_result") {
            ui.addEvent(`✓ result: ${JSON.stringify(evt.data).slice(0, 300)}`);
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
            renderApprovalCard(evt, activePane);
          } else if (evt.type === "approval_pending") {
            // Transient "waiting" indicator — removed after approve/deny
            if (!gotAnswer) { ui.closeThinking(); gotAnswer = true; }
            const pending = document.createElement('div');
            pending.className = 'approval-pending-note';
            pending.textContent = evt.text || '⏳ Waiting for your approval…';
            if (activePane) {
              const turn = activePane.querySelector('.turn:last-child');
              (turn || activePane.querySelector('.messages')).appendChild(pending);
            }
          } else if (evt.type === "cwd_warning") {
            if (!gotAnswer) { ui.closeThinking(); gotAnswer = true; }
            renderCwdWarningCard(evt, activePane);
          } else if (evt.type === "cwd_warning_pending") {
            if (!gotAnswer) { ui.closeThinking(); gotAnswer = true; }
            const pending = document.createElement('div');
            pending.className = 'cwd-warning-pending-note';
            pending.textContent = evt.text || '⚠️ File operation outside project folder detected.';
            if (activePane) {
              const turn = activePane.querySelector('.turn:last-child');
              (turn || activePane.querySelector('.messages')).appendChild(pending);
            }
          } else if (evt.type === "sim_ready") {
            const fname = evt.filename || "simulation.html";
            const url = "/assets/" + encodeURIComponent(fname);
            const pane = activePane;
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



