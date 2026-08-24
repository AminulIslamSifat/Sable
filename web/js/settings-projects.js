    // ── Projects Settings Tab ────────────────────────────────────────────────
    async function renderProjectsTab() {
      await loadProjects();
      const list = document.getElementById('projectsList');
      if (!list) return;
      list.innerHTML = '';

      if (projectList.length === 0) {
        list.innerHTML = '<p class="muted" style="font-size:12px;text-align:center;padding:20px;">No projects yet. Create one above.</p>';
        return;
      }

      for (const proj of projectList) {
        const card = document.createElement('div');
        card.className = 'project-card';

        const info = document.createElement('div');
        info.className = 'project-card-info';
        const nameEl = document.createElement('div');
        nameEl.className = 'project-card-name';
        nameEl.textContent = proj.name;
        const metaEl = document.createElement('div');
        metaEl.className = 'project-card-meta';
        metaEl.textContent = (proj.path || 'No path') + ' \u00b7 Universal memory: ' + (proj.use_universal_memory ? 'ON' : 'OFF');
        info.appendChild(nameEl);
        info.appendChild(metaEl);

        const actions = document.createElement('div');
        actions.className = 'project-card-actions';

        const toggleBtn = document.createElement('button');
        toggleBtn.textContent = proj.use_universal_memory ? 'Memory ON' : 'Memory OFF';
        toggleBtn.onclick = () => {
          fetch('/api/projects/' + proj.id, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ use_universal_memory: !proj.use_universal_memory })
          }).then(() => renderProjectsTab());
        };

        const delBtn = document.createElement('button');
        delBtn.className = 'delete';
        delBtn.textContent = 'Delete';
        delBtn.onclick = async () => {
          if (await sableConfirm('Delete project "' + proj.name + '"? Chats will be moved to global.', { danger: true })) {
            fetch('/api/projects/' + proj.id, { method: 'DELETE' }).then(() => {
              if (activeProjectId === proj.id) { activeProjectId = null; loadChats(); }
              renderProjectsTab();
            });
          }
        };

        actions.appendChild(toggleBtn);
        actions.appendChild(delBtn);
        card.appendChild(info);
        card.appendChild(actions);
        list.appendChild(card);
      }
    }

    // ── Project Settings Popup ───────────────────────────────────────────────
    function showProjectSettingsPopup(proj, anchorEl) {
      const existingOverlay = document.querySelector('.project-settings-overlay');
      if (existingOverlay) existingOverlay.remove();

      const overlay = document.createElement('div');
      overlay.className = 'project-settings-overlay';

      const modal = document.createElement('div');
      modal.className = 'project-settings-modal';

      const title = document.createElement('h4');
      title.textContent = proj.name + ' Settings';
      modal.appendChild(title);

      // Helper: create toggle row
      const makeToggle = (id, labelText, checked) => {
        const row = document.createElement('div');
        row.className = 'psm-toggle-row';
        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.id = id;
        cb.checked = !!checked;
        const lbl = document.createElement('label');
        lbl.textContent = labelText;
        lbl.htmlFor = id;
        row.appendChild(cb);
        row.appendChild(lbl);
        row.addEventListener('click', (e) => {
          if (e.target !== cb) cb.checked = !cb.checked;
        });
        return { row, cb };
      };

      // Helper: create labeled text input
      const makeTextInput = (labelText, value, placeholder) => {
        const lbl = document.createElement('label');
        lbl.textContent = labelText;
        const inp = document.createElement('input');
        inp.type = 'text';
        inp.value = value || '';
        if (placeholder) inp.placeholder = placeholder;
        return { lbl, inp };
      };

      // Helper: create collapsible section
      const makeSection = (headerText, collapsed) => {
        const section = document.createElement('div');
        section.className = 'psm-section';
        const header = document.createElement('div');
        header.className = 'psm-section-header';
        const chevron = document.createElement('span');
        chevron.className = 'psm-chevron';
        chevron.textContent = collapsed ? '▸' : '▾';
        const hText = document.createElement('span');
        hText.textContent = headerText;
        header.appendChild(chevron);
        header.appendChild(hText);
        const body = document.createElement('div');
        body.className = 'psm-section-body';
        if (collapsed) body.style.display = 'none';
        header.addEventListener('click', () => {
          const hidden = body.style.display === 'none';
          body.style.display = hidden ? '' : 'none';
          chevron.textContent = hidden ? '▾' : '▸';
        });
        section.appendChild(header);
        section.appendChild(body);
        return { section, body };
      };

      // ── A. General ──
      const generalSec = makeSection('General', false);
      const nameField = makeTextInput('Name', proj.name, 'Project name');
      generalSec.body.appendChild(nameField.lbl);
      generalSec.body.appendChild(nameField.inp);
      const pathField = makeTextInput('Project Path', proj.path, '/path/to/project');
      generalSec.body.appendChild(pathField.lbl);
      generalSec.body.appendChild(pathField.inp);
      modal.appendChild(generalSec.section);

      // ── B. Instructions ──
      const instrSec = makeSection('Instructions', false);
      const personaTog = makeToggle('psm-persona', 'Use custom persona instructions', proj.persona_enabled);
      instrSec.body.appendChild(personaTog.row);

      const instrTextarea = document.createElement('textarea');
      instrTextarea.className = 'psm-textarea';
      instrTextarea.placeholder = 'Enter custom instructions...';
      instrTextarea.value = proj.instruction_text || '';
      const instrHelper = document.createElement('div');
      instrHelper.className = 'psm-helper';
      instrHelper.textContent = 'This replaces Maria.md when active';
      instrSec.body.appendChild(instrTextarea);
      instrSec.body.appendChild(instrHelper);

      const updateInstrVisibility = () => {
        instrTextarea.style.display = personaTog.cb.checked ? '' : 'none';
        instrHelper.style.display = personaTog.cb.checked ? '' : 'none';
      };
      personaTog.cb.addEventListener('change', updateInstrVisibility);
      updateInstrVisibility();
      modal.appendChild(instrSec.section);

      // ── C. Output & Memory ──
      const memSec = makeSection('Output & Memory', false);
      const outFmtTog = makeToggle('psm-outfmt', 'Output format rules', proj.output_format_enabled !== false);
      const univMemTog = makeToggle('psm-univmem', 'Universal memory', proj.use_universal_memory);
      const projMemTog = makeToggle('psm-projmem', 'Project memory', proj.project_memory_enabled);
      memSec.body.appendChild(outFmtTog.row);
      memSec.body.appendChild(univMemTog.row);
      memSec.body.appendChild(projMemTog.row);

      const factsLbl = document.createElement('label');
      factsLbl.textContent = 'Key Facts';
      const factsArea = document.createElement('textarea');
      factsArea.className = 'psm-textarea';
      factsArea.placeholder = 'Key facts to remember for this project...';
      factsArea.value = proj.facts || '';
      memSec.body.appendChild(factsLbl);
      memSec.body.appendChild(factsArea);
      modal.appendChild(memSec.section);

      // ── D. Git Repository ──
      const gitSec = makeSection('Git Repository', !(proj.git_repo || proj.git_username || proj.git_branch));
      const gitRepoField = makeTextInput('Repo URL', proj.git_repo, 'https://github.com/user/repo');
      const gitUserField = makeTextInput('Username', proj.git_username, 'git username');
      const gitBranchField = makeTextInput('Branch', proj.git_branch, 'main');
      gitSec.body.appendChild(gitRepoField.lbl);
      gitSec.body.appendChild(gitRepoField.inp);
      gitSec.body.appendChild(gitUserField.lbl);
      gitSec.body.appendChild(gitUserField.inp);
      gitSec.body.appendChild(gitBranchField.lbl);
      gitSec.body.appendChild(gitBranchField.inp);
      modal.appendChild(gitSec.section);

      // ── E. Skills ──
      const skillsSec = makeSection('Skills', true);
      const skillsContainer = document.createElement('div');
      skillsContainer.className = 'psm-skills-container';
      const skillsLoading = document.createElement('div');
      skillsLoading.className = 'psm-helper';
      skillsLoading.textContent = 'Loading skills...';
      skillsContainer.appendChild(skillsLoading);
      skillsSec.body.appendChild(skillsContainer);
      modal.appendChild(skillsSec.section);

      const skillCheckboxes = [];
      fetch('/api/skills').then(r => r.json()).then(data => {
        skillsLoading.remove();
        const skills = data.skills || [];
        if (skills.length === 0) {
          const none = document.createElement('div');
          none.className = 'psm-helper';
          none.textContent = 'No skills available';
          skillsContainer.appendChild(none);
          return;
        }
        const config = proj.skills_config || {};
        skills.forEach(sk => {
          const row = document.createElement('div');
          row.className = 'psm-skill-row';
          const cb = document.createElement('input');
          cb.type = 'checkbox';
          cb.checked = config[sk.key] !== undefined ? !!config[sk.key] : true;
          const nameSpan = document.createElement('span');
          nameSpan.className = 'psm-skill-name';
          nameSpan.textContent = sk.name || sk.key;
          const badge = document.createElement('span');
          badge.className = 'psm-skill-badge';
          badge.textContent = sk.category || 'general';
          row.appendChild(cb);
          row.appendChild(nameSpan);
          row.appendChild(badge);
          row.addEventListener('click', (e) => {
            if (e.target !== cb) cb.checked = !cb.checked;
          });
          skillsContainer.appendChild(row);
          skillCheckboxes.push({ key: sk.key, cb });
        });
      }).catch(() => {
        skillsLoading.textContent = 'Failed to load skills';
      });

      // ── Buttons ──
      const btnRow = document.createElement('div');
      btnRow.className = 'psm-btn-row';

      const delBtn = document.createElement('button');
      delBtn.textContent = 'Delete';
      delBtn.className = 'btn-danger';
      delBtn.onclick = async () => {
        if (await sableConfirm('Delete "' + proj.name + '"?', { danger: true })) {
          const wasActive = activeProjectId === proj.id;
          fetch('/api/projects/' + proj.id, { method: 'DELETE' }).then(() => {
            overlay.remove();
            if (wasActive) {
              activeProjectId = null;
              // Revert CWD back to Sable root since project is gone
              fetch('/api/projects/deactivate', { method: 'POST' }).then(() => {
                loadProjects().then(() => loadChats());
              });
            } else {
              loadProjects().then(() => loadChats());
            }
          });
        }
      };

      const saveBtn = document.createElement('button');
      saveBtn.textContent = 'Save';
      saveBtn.className = 'btn-primary';
      saveBtn.onclick = () => {
        const skillsConfig = {};
        skillCheckboxes.forEach(({ key, cb }) => { skillsConfig[key] = cb.checked; });

        const payload = {
          name: nameField.inp.value.trim(),
          path: pathField.inp.value.trim() || null,
          persona_enabled: personaTog.cb.checked,
          instruction_text: personaTog.cb.checked ? instrTextarea.value : null,
          output_format_enabled: outFmtTog.cb.checked,
          use_universal_memory: univMemTog.cb.checked,
          project_memory_enabled: projMemTog.cb.checked,
          facts: factsArea.value.trim() || null,
          git_repo: gitRepoField.inp.value.trim() || null,
          git_username: gitUserField.inp.value.trim() || null,
          git_branch: gitBranchField.inp.value.trim() || null,
          skills_config: skillsConfig
        };

        const doSave = () => {
          fetch('/api/projects/' + proj.id, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
          }).then(r => r.json()).then(() => {
            overlay.remove();
            showToast('Project updated', 'success');
            loadProjects().then(() => loadChats());
          });
        };

        if (personaTog.cb.checked && instrTextarea.value !== (proj.instruction_text || '')) {
          fetch('/api/projects/' + proj.id + '/instruction', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text: instrTextarea.value })
          }).then(() => doSave()).catch(() => doSave());
        } else {
          doSave();
        }
      };

      btnRow.appendChild(delBtn);
      btnRow.appendChild(saveBtn);
      modal.appendChild(btnRow);

      overlay.appendChild(modal);
      document.body.appendChild(overlay);

      overlay.addEventListener('click', (e) => {
        if (e.target === overlay) overlay.remove();
      });

      const escHandler = (e) => {
        if (e.key === 'Escape') { overlay.remove(); document.removeEventListener('keydown', escHandler); }
      };
      document.addEventListener('keydown', escHandler);

      nameField.inp.focus();
      nameField.inp.select();
    }

    // ── Project Folder Dropdown (sidebar button) ────────────────────────────
    // ── Project Folder Dropdown (sidebar button) ────────────────────────────
    function showProjectFolderDropdown() {
      const btn = document.getElementById('projectFolderBtn');
      if (!btn) return;
      const existing = document.querySelector('.project-folder-dropdown');
      if (existing) { existing.remove(); btn.classList.remove('active'); return; }

      const dd = document.createElement('div');
      dd.className = 'project-folder-dropdown';

      // "All Chats" — deactivate project
      const allItem = document.createElement('div');
      allItem.className = 'project-folder-item' + (!activeProjectId ? ' active' : '');
      allItem.innerHTML = '<span class="icon-emoji">💬</span><i data-lucide="message-square" class="icon-lucide"></i> All Chats';
      allItem.onclick = () => {
        dd.remove(); btn.classList.remove('active');
        if (activeProjectId) {
          fetch('/api/projects/deactivate', { method: 'POST' }).then(async (r) => {
            const data = await r.json().catch(() => ({}));
            activeProjectId = null;
            if (data.new_cwd && typeof window.pickFsRoot === 'function') window.pickFsRoot(data.new_cwd);
            await createChat();
            fetch('/api/sync-context', { method: 'POST' });
          });
        }
      };
      dd.appendChild(allItem);

      const newItem = document.createElement('div');
      newItem.className = 'project-folder-item';
      newItem.innerHTML = '<span class="icon-emoji">+</span><i data-lucide="square-pen" class="icon-lucide"></i> New Chat';
      newItem.onclick = () => { dd.remove(); btn.classList.remove('active'); createChat(); };
      dd.appendChild(newItem);

      const sep = document.createElement('div');
      sep.className = 'project-folder-sep';
      dd.appendChild(sep);

      for (const proj of projectList) {
        const item = document.createElement('div');
        item.className = 'project-folder-item' + (activeProjectId === proj.id ? ' active' : '');
        item.innerHTML = '<span class="icon-emoji">\ud83d\udcc1</span><i data-lucide="folder" class="icon-lucide"></i> ' + proj.name;
        item.onclick = () => {
          dd.remove(); btn.classList.remove('active');
          if (activeProjectId === proj.id) return; // already active
          fetch('/api/projects/' + proj.id + '/activate', { method: 'POST' })
            .then(r => r.json())
            .then(data => {
              activeProjectId = proj.id;
              if (data.new_cwd && typeof window.pickFsRoot === 'function') window.pickFsRoot(data.new_cwd);
              createChat();
            })
            .catch(() => {
              activeProjectId = proj.id;
              createChat();
            });
        };
        dd.appendChild(item);
      }

      const addProj = document.createElement('div');
      addProj.className = 'project-folder-item add-new';
      addProj.innerHTML = '<span class="icon-emoji">+</span><i data-lucide="plus" class="icon-lucide"></i> New Project';
      addProj.onclick = async () => {
        dd.remove(); btn.classList.remove('active');
        const name = await sablePrompt('Project name:');
        if (name && name.trim()) {
          fetch('/api/projects', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: name.trim() })
          }).then(r => r.json()).then(data => {
            if (data.id) {
              fetch('/api/projects/' + data.id + '/activate', { method: 'POST' })
                .then(() => { activeProjectId = data.id; loadProjects().then(() => loadChats()); })
                .catch(() => { activeProjectId = data.id; loadProjects().then(() => loadChats()); });
            }
            else showToast(data.error || 'Failed', 'error');
          });
        }
      };
      dd.appendChild(addProj);

      const rect = btn.getBoundingClientRect();
      dd.style.position = 'fixed';
      dd.style.top = (rect.bottom + 4) + 'px';
      dd.style.left = rect.left + 'px';
      dd.style.minWidth = Math.max(rect.width, 180) + 'px';
      document.body.appendChild(dd);
      if (typeof lucide !== 'undefined') lucide.createIcons();

      const closeHandler = (e) => {
        if (!dd.contains(e.target) && e.target !== btn) {
          dd.remove(); btn.classList.remove('active');
          document.removeEventListener('click', closeHandler, true);
        }
      };
      setTimeout(() => document.addEventListener('click', closeHandler, true), 0);
    }

    async function loadChats(mode) {
      try {
        // skeleton loading placeholders
        chatsEl.innerHTML = '';
        for (let i = 0; i < 3; i++) {
          const skel = document.createElement('div');
          skel.className = 'skeleton-chat';
          chatsEl.appendChild(skel);
        }
        const params = new URLSearchParams();
        if (mode) params.set('mode', mode);
        if (activeProjectId) params.set('project_id', activeProjectId);
        const qs = params.toString();
        const url = qs ? `/api/chats?${qs}` : "/api/chats";
        const data = await fetch(url).then(r => r.json());
        chatList = data.chats || [];
        renderChats();
      } catch (err) {
        console.error("Failed to load chats:", err);
      }
    }

    // Cancellation token for stale loadMessages renders.
    // Incremented on every selectChat; loadMessages checks before each batch.
    let _loadGeneration = 0;

    async function loadMessages(chatId, generation) {
      try {
        // Single fetch — all messages at once, no pagination
        const data = await fetch(`/api/chats/${chatId}/messages?include_skill_events=true`).then(r => r.json());
        // Abort if another selectChat started while we were fetching
        if (generation !== _loadGeneration) return [];

        const pane = ensurePane(chatId);
        pane.innerHTML = "";
        const chars = data.context_chars || 0;
        contextCharsCache.set(chatId, chars);
        window._statusContextChars = chars;
        updateStatusBarContext();
        const messages = data.messages || [];
        if (messages.length === 0) {
          pane.innerHTML = `<div class="empty"><h2>New conversation</h2><p>Send the first message.</p></div>`;
          return [];
        }

        // Render messages sequentially in small batches, yielding to the
        // browser between batches so GC can reclaim intermediate objects.
        // This prevents RAM spikes from holding all DOM construction in one
        // synchronous burst while still loading everything upfront.
        const BATCH_SIZE = 10;
        const prevPane = activePane;
        activePane = pane;
        for (let i = 0; i < messages.length; i += BATCH_SIZE) {
          // Abort stale render — user switched to another chat
          if (generation !== _loadGeneration) {
            activePane = prevPane;
            return [];
          }
          const batch = messages.slice(i, i + BATCH_SIZE);
          for (const msg of batch) addHistoryMessage(msg);
          // Yield to browser every batch so it can GC and keep spinner animated
          await new Promise(r => requestAnimationFrame(r));
        }
        activePane = prevPane;
        renderMathJax(pane);
        if (chatId === activeChatId) scrollBottom(true);

        return messages;
      } catch (err) {
        console.error("Failed to load messages:", err);
        return [];
      }
    }

    /**
     * Rebuild the model dropdown filtered to the provider's model group.
     * - null (new/unlocked chat): show all models, enabled
     * - "qwen": show only qwen models, enabled (free switching within group)
     * - "deepseek": show only deepseek models, enabled (free switching within group)
     * - "scraping": show deepseek models, DISABLED (locked tight)
     */
    function lockModelDropdown(provider) {
      // Pure API chats (non-qwen, non-scraping): show ALL api models — free switching
      const isApiChat = provider && provider !== "qwen" && provider !== "scraping";
      const allowed = provider
        ? isApiChat
          ? modelList.filter(m => m.api_backend && m.api_backend !== "qwen")
          : modelList.filter(m => {
              if (provider === "deepseek" || provider === "scraping") return m.api_backend === "deepseek";
              if (provider === "local") return m.api_backend === "local";
              return m.api_backend === "qwen" || !m.api_backend; // qwen fallback
            })
        : modelList;

      modelSelectEl.innerHTML = "";
      for (const m of allowed) {
        const opt = document.createElement("option");
        opt.value = m.id;
        opt.textContent = m.label || m.id;
        modelSelectEl.appendChild(opt);
      }

      // If current selection isn't in the allowed set, switch to first allowed
      if (!allowed.some(m => m.id === selectedModel)) {
        selectedModel = allowed[0]?.id || modelList[0].id;
        modelSelectEl.value = selectedModel;
        try { localStorage.setItem(MODEL_KEY, selectedModel); } catch(e) {}
      } else {
        modelSelectEl.value = selectedModel;
      }
      // Always sync thinking modes for the current model
      let _savedTm = null;
      try { _savedTm = localStorage.getItem(THINKING_MODE_KEY); } catch(e) {}
      populateThinkingModes(_savedTm);

      // Only scraping gets hard-disabled; qwen/deepseek allow within-group switching
      modelSelectEl.disabled = provider === "scraping";
      const _mt = document.getElementById("modelTrigger");
      if (_mt) { _mt.style.pointerEvents = provider === "scraping" ? "none" : ""; _mt.style.opacity = provider === "scraping" ? "0.45" : ""; }
      syncModelDropdown();
    }

    // Guard against concurrent loadMessages for the same chat
    const _loadingChats = new Set();

    async function selectChat(chatId) {
      const meta = chatList.find(c => c.id === chatId);
      const alreadyOpen = openTabs.has(chatId);

      // Switch the visible tab (creates pane if needed)
      switchToTab(chatId);

      // Cancel any stale scroll rAF from the previous chat
      _scrollPending = false;
      _scrollForChat = null;

      // Lock model dropdown to the chat's provider (or unlock for new chats)
      lockModelDropdown(meta?.provider || null);

      // Update send button: show stop-mode only if THIS chat is streaming
      updateSendBtn();

      saveActiveChat();
      renderChats();

      // Cancel any in-flight loadMessages render from a previous selectChat
      _loadGeneration++;
      const myGeneration = _loadGeneration;

      // Only load messages from API if this tab hasn't been loaded yet
      // Use _loadingChats guard to prevent duplicate loads from race conditions
      if (!alreadyOpen && !_loadingChats.has(chatId)) {
        _loadingChats.add(chatId);
        const targetPane = ensurePane(chatId);
        showPaneLoading(targetPane);
        try {
          const msgs = await loadMessages(chatId, myGeneration);
          // If stale (user switched away), skip state updates
          if (myGeneration !== _loadGeneration) return;
          hidePaneLoading(targetPane);
          // Derive parentId from the actual message chain
          if (Array.isArray(msgs) && msgs.length) {
            const last = msgs[msgs.length - 1];
            parentId = last?.parent_id ? String(last.parent_id) : last?.id ? String(last.id) : null;
          } else {
            parentId = meta?.parent_id ? String(meta.parent_id) : null;
          }
        } finally {
          _loadingChats.delete(chatId);
          hidePaneLoading(targetPane);
        }
      } else {
        // Already loaded — just derive parentId from cached meta
        parentId = meta?.parent_id ? String(meta.parent_id) : null;
        // Restore context ring from cache for this chat
        window._statusContextChars = contextCharsCache.get(chatId) || 0;
        updateStatusBarContext();
      }

      // Connect agent SSE for this chat
      if (typeof onChatOpened === "function") onChatOpened(chatId);

        inputEl.focus();
    }

    // Expose for cross-module navigation (e.g. fork button)
    window._sableSelectChat = selectChat;
    window._sableLoadChats = loadChats;

    async function deleteChat(chatId) {
      if (!await sableConfirm("Delete this chat?", { danger: true })) return;
      try {
        const res = await fetch(`/api/chats/${chatId}`, { method: "DELETE" });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || !data.deleted) {
          showToast(data.detail || "Could not delete chat", "error");
          return;
        }
        chatList = chatList.filter(c => c.id !== chatId);
        closeTab(chatId); // handles activeChatId reassignment + empty state
        renderChats();
        showToast("Chat deleted", "success");
      } catch (err) {
        showToast("Delete failed: " + err.message, "error");
      }
    }

    document.getElementById('deleteAllChatsBtn').addEventListener('click', async () => {
      if (!await sableConfirm('Delete ALL chats? This cannot be undone.', { danger: true })) return;
      try {
        const res = await fetch('/api/chats', { method: 'DELETE' });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || !data.deleted) {
          showToast(data.detail || 'Could not delete chats', 'error');
          return;
        }
        chatList = [];
        // Close all tabs
        for (const [id] of openTabs) {
          const tab = openTabs.get(id);
          if (tab) tab.pane.remove();
        }
        openTabs.clear();
        activePane = null;
        activeChatId = null;
        parentId = null;
        saveActiveChat();
        renderChats();
        renderTabBar();
        chatEl.innerHTML = `<div class="empty"><h2>Start a chat</h2><p>Create a new chat and talk to Sable.</p></div>`;
        showToast(`Deleted ${data.chats_removed} chat(s)`, 'success');
      } catch (err) {
        showToast('Delete failed: ' + err.message, 'error');
      }
    });

    // ── Strip Browser Profiles ──────────────────────────────────
    document.getElementById('stripProfilesBtn').addEventListener('click', async () => {
      if (!await sableConfirm('Strip all browser profiles down to bare session data? Caches will be removed.')) return;
      const btn = document.getElementById('stripProfilesBtn');
      const status = document.getElementById('stripProfilesStatus');
      btn.disabled = true;
      btn.textContent = '⏳ Stripping…';
      status.textContent = 'Stripping profiles…';
      status.style.color = 'var(--text-dim)';
      try {
        const res = await fetch('/api/settings/browser/strip-profiles', { method: 'POST' });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          status.textContent = data.detail || 'Strip failed';
          status.style.color = 'var(--danger)';
          showToast(data.detail || 'Strip failed', 'error');
          return;
        }
        const lastLine = (data.output || '').trim().split('\n').pop() || '';
        status.textContent = '✅ ' + lastLine;
        status.style.color = 'var(--success, #4caf50)';
        showToast('Browser profiles stripped', 'success');
      } catch (err) {
        status.textContent = 'Error: ' + err.message;
        status.style.color = 'var(--danger)';
        showToast('Strip failed: ' + err.message, 'error');
      } finally {
        btn.disabled = false;
        btn.textContent = '🧹 Strip Browser Profiles';
      }
    });
    // ── /Strip Browser Profiles ─────────────────────────────────


    // ── Data Export / Import ─────────────────────────────────────
    async function _streamDataOp(url, btnId, statusEl, confirmMsg, busyLabel, doneFn) {
      if (!await sableConfirm(confirmMsg)) return;
      const btn = document.getElementById(btnId);
      btn.disabled = true;
      btn.textContent = '⏳ ' + busyLabel + '…';
      statusEl.textContent = busyLabel + '…';
      statusEl.style.color = 'var(--text-dim)';
      try {
        const res = await fetch(url, { method: 'POST' });
        if (!res.ok || !res.body) {
          const err = await res.json().catch(() => ({}));
          statusEl.textContent = err.detail || 'Failed';
          statusEl.style.color = 'var(--danger)';
          showToast(err.detail || 'Operation failed', 'error');
          return;
        }
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buf = '';
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          const lines = buf.split('\n');
          buf = lines.pop();
          for (const line of lines) {
            if (!line.trim()) continue;
            try {
              const ev = JSON.parse(line);
              if (ev.type === 'progress') {
                statusEl.textContent = `⏳ [${ev.step}/${ev.total}] ${ev.dir} — ${ev.status}`;
              } else if (ev.type === 'done') {
                doneFn(ev);
              } else if (ev.type === 'error') {
                statusEl.textContent = '❌ ' + (ev.detail || 'Unknown error');
                statusEl.style.color = 'var(--danger)';
                showToast(ev.detail || 'Operation failed', 'error');
              }
            } catch {}
          }
        }
      } catch (err) {
        statusEl.textContent = 'Error: ' + err.message;
        statusEl.style.color = 'var(--danger)';
        showToast('Failed: ' + err.message, 'error');
      } finally {
        btn.disabled = false;
      }
    }

    document.getElementById('exportDataBtn').addEventListener('click', () => {
      const status = document.getElementById('dataExportStatus');
      const btn = document.getElementById('exportDataBtn');
      _streamDataOp(
        '/api/settings/data/export', 'exportDataBtn', status,
        'Export all data to ~/.sable/backup/? This overwrites any existing backup.',
        'Exporting',
        (ev) => {
          const dirs = Object.keys(ev.exported || {});
          status.textContent = `✅ Exported ${dirs.length} dirs → ~/.sable/backup/`;
          status.style.color = 'var(--success, #4caf50)';
          showToast('Data exported successfully', 'success');
          btn.textContent = '⬆ Export Data';
        }
      );
    });

    document.getElementById('importDataBtn').addEventListener('click', () => {
      const status = document.getElementById('dataExportStatus');
      const btn = document.getElementById('importDataBtn');
      _streamDataOp(
        '/api/settings/data/import', 'importDataBtn', status,
        'Import data from ~/.sable/backup/? This will overwrite current files.',
        'Importing',
        (ev) => {
          const dirs = ev.imported || [];
          status.textContent = `✅ Imported ${dirs.length} dirs from backup`;
          status.style.color = 'var(--success, #4caf50)';
          showToast('Data imported successfully', 'success');
          btn.textContent = '⬇ Import Data';
        }
      );
    });
    // ── /Data Export / Import ────────────────────────────────────
