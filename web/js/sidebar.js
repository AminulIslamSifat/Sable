    let scraperChatsCollapsed = false;

    async function renderChats() {
      chatsEl.innerHTML = '';
      chatsEl.classList.remove('has-project-banner');

      // Split chats: scraper (browser-*) vs API
      const q = chatSearchQuery.toLowerCase().trim();
      // If we have server-side search results, show those instead of title filtering
      if (q && chatSearchResults !== null) {
        chatsEl.innerHTML = '';
        // Group results by chat_id
        const byChat = new Map();
        for (const r of chatSearchResults) {
          if (!byChat.has(r.chat_id)) byChat.set(r.chat_id, { title: r.title, messages: [] });
          byChat.get(r.chat_id).messages.push(r);
        }
        for (const [chatId, group] of byChat) {
          const lbl = document.createElement('div');
          lbl.className = 'chat-group-label';
          lbl.textContent = group.title || 'Untitled';
          lbl.style.cursor = 'pointer';
          lbl.onclick = () => selectChat(chatId);
          chatsEl.appendChild(lbl);
          for (const msg of group.messages.slice(0, 5)) {
            const row = document.createElement('div');
            row.className = 'chat-row';
            const btn = document.createElement('button');
            btn.className = 'chat-item';
            const snippet = (msg.content || '').replace(/\n/g, ' ').slice(0, 120);
            btn.textContent = `${msg.role === 'user' ? '👤' : '🤖'} ${snippet}`;
            btn.title = msg.created_at || '';
            btn.onclick = () => selectChat(chatId);
            row.appendChild(btn);
            chatsEl.appendChild(row);
          }
        }
        if (byChat.size === 0) {
          const empty = document.createElement('div');
          empty.className = 'chat-group-label';
          empty.textContent = 'No matches found';
          chatsEl.appendChild(empty);
        }
        return;
      }
      // ── Project folder / banner at top of chat list ──
      await loadProjects();
      // Show/hide project menu button based on active project
      const projectMenuBtn = document.getElementById('projectMenuBtn');
      if (projectMenuBtn) projectMenuBtn.style.display = activeProjectId ? '' : 'none';
      const projectFolderBtnEl = document.getElementById('projectFolderBtn');
      if (projectFolderBtnEl) projectFolderBtnEl.style.display = activeProjectId ? 'none' : '';

      if (activeProjectId) {
        const proj = projectList.find(p => p.id === activeProjectId);
        const banner = document.createElement('div');
        banner.className = 'project-banner';
        const nameSpan = document.createElement('span');
        nameSpan.className = 'project-name';
        nameSpan.textContent = proj ? proj.name : 'Project';
        banner.appendChild(nameSpan);
        chatsEl.classList.add('has-project-banner');
        chatsEl.appendChild(banner);
      }

      const filtered = q ? chatList.filter(c => (c.title || '').toLowerCase().includes(q)) : chatList;
      const apiChats = filtered.filter(c => !c.id.startsWith('browser-'))
        .sort((a, b) => (b.updated_at || b.created_at || '').localeCompare(a.updated_at || a.created_at || ''));
      const scraperChats = filtered.filter(c => c.id.startsWith('browser-'));

      const __groupOf = (c) => {
        const raw = c.updated_at || c.created_at || c.last_message_at || null;
        if (!raw) return 'Chats';
        const d = new Date(typeof raw === 'number' ? (raw < 1e12 ? raw * 1000 : raw) : raw);
        if (isNaN(d.getTime())) return 'Chats';
        const now = new Date();
        const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
        const yesterday = new Date(today.getTime() - 86400000);
        const ds = new Date(d.getFullYear(), d.getMonth(), d.getDate());
        if (ds >= today) return 'Today';
        if (ds >= yesterday) return 'Yesterday';
        return 'Earlier';
      };

      const renderChatRow = (chat) => {
        const row = document.createElement("div");
        row.className = "chat-row";
        row.dataset.chatId = chat.id;
        const btn = document.createElement("button");
        btn.className = "chat-item" + (chat.id === activeChatId ? " active" : "");
        btn.textContent = chat.title || "New chat";
        btn.onclick = () => selectChat(chat.id);
        if (activeStreams.has(chat.id)) row.classList.add("streaming");
        const del = document.createElement("button");
        del.className = "chat-delete";
        del.textContent = "×";
        del.title = "Delete chat";
        del.onclick = (e) => { e.stopPropagation(); deleteChat(chat.id); };
        row.appendChild(btn);
        row.appendChild(del);
        return row;
      };

      // API chats with date groups
      let __lastGroup = null;
      for (const chat of apiChats) {
        const __g = __groupOf(chat);
        if (__g !== __lastGroup) {
          __lastGroup = __g;
          const lbl = document.createElement('div');
          lbl.className = 'chat-group-label';
          lbl.textContent = __g;
          chatsEl.appendChild(lbl);
        }
        chatsEl.appendChild(renderChatRow(chat));
      }

      // Activate lucide icons for dynamically created project elements
      if (typeof lucide !== 'undefined') lucide.createIcons();

      // Scraper chats at bottom in collapsible section
      if (scraperChats.length > 0) {
        const header = document.createElement('div');
        header.className = 'scraper-chats-header' + (scraperChatsCollapsed ? ' collapsed' : '');
        header.innerHTML = '<span class="arrow">▼</span> 🌐 Scraper Chats (' + scraperChats.length + ')';
        header.onclick = () => {
          scraperChatsCollapsed = !scraperChatsCollapsed;
          renderChats();
        };
        chatsEl.appendChild(header);

        const body = document.createElement('div');
        body.className = 'scraper-chats-body' + (scraperChatsCollapsed ? ' collapsed' : '');
        for (const chat of scraperChats) {
          body.appendChild(renderChatRow(chat));
        }
        chatsEl.appendChild(body);
      }

      // Sync tab titles from chatList (backend auto-titles after first msg)
      for (const [id, tab] of openTabs) {
        const meta = chatList.find(c => c.id === id);
        if (meta && meta.title && meta.title !== tab.title) {
          tab.title = meta.title;
        }
      }
      renderTabBar();
    }

    function currentModelEntry() {
      return modelList.find(m => m.id === selectedModel) || modelList[0];
    }

    // Rebuilds the sidebar thinking-mode dropdown for whichever model is
    // currently selected — each model supports a different set of modes
    // (e.g. qwen3.8-max-preview only has "Thinking", qwen3.7-plus has
    // Fast/Auto/Thinking), so this runs on load and on every model change.
    function populateThinkingModes(preferredModeId) {
      const entry = currentModelEntry();
      const modes = (entry && entry.thinking_modes && entry.thinking_modes.length > 0)
        ? entry.thinking_modes
        : [{ id: "thinking", label: "Thinking" }];

      selectedThinkingMode = (preferredModeId && modes.some(m => m.id === preferredModeId))
        ? preferredModeId
        : modes[0].id;

      if (thinkingSwitcherEl) {
        thinkingSwitcherEl.innerHTML = "";
        thinkingSwitcherEl.style.setProperty('--n', modes.length);
        for (let idx = 0; idx < modes.length; idx++) {
          const m = modes[idx];
          const btn = document.createElement("button");
          btn.textContent = m.label || m.id;
          btn.dataset.modeId = m.id;
          if (m.id === selectedThinkingMode) {
            btn.classList.add('active');
            thinkingSwitcherEl.style.setProperty('--i', idx);
          }
          btn.addEventListener('click', () => {
            if (btn.classList.contains('active')) return;
            selectedThinkingMode = m.id;
            thinkingSwitcherEl.querySelectorAll('button').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            thinkingSwitcherEl.style.setProperty('--i', idx);
            try { localStorage.setItem(THINKING_MODE_KEY, selectedThinkingMode); } catch (err) {}
          });
          thinkingSwitcherEl.appendChild(btn);
        }

        // Only one available mode — hide the switcher entirely.
        thinkingSwitcherEl.style.display = modes.length <= 1 ? 'none' : '';
      }

      try { localStorage.setItem(THINKING_MODE_KEY, selectedThinkingMode); } catch (err) {}
      syncStatusBarThinking();
    }

