/**
 * projects-panel.js — Projects sidebar panel
 *
 * Self-contained: NEVER touches .sidebar-chats or calls renderChats()/loadChats().
 * - Active project collapses list to just that card + Exit button
 * - Tracks last main chat & last project chat for seamless switching
 */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);

  // Persistent memory across switches
  let _lastNonProjectChatId = null;   // chat before entering projects
  let _lastProjectChatId = null;      // last opened project chat
  let _savedProjectId = null;         // project id when leaving projects

  /* ═══════════════════════════════════════════
   *  PROJECT LIST RENDERING
   * ═══════════════════════════════════════════ */

  async function renderProjectsPanel() {
    if (typeof loadProjects === 'function') await loadProjects();

    const list = $('projList');
    const empty = $('projEmpty');
    if (!list) return;
    list.innerHTML = '';

    const projects = (typeof projectList !== 'undefined' ? projectList : []);

    if (projects.length === 0) {
      if (empty) empty.style.display = '';
      hideProjectChats();
      return;
    }
    if (empty) empty.style.display = 'none';

    // If a project is active, ONLY show that project's card (collapsed view)
    if (activeProjectId) {
      const proj = projects.find(p => p.id === activeProjectId);
      if (proj) {
        list.appendChild(buildProjectCard(proj, true));
        await renderProjectChats();
      } else {
        // activeProjectId stale — reset
        activeProjectId = null;
        hideProjectChats();
      }
    } else {
      // No active project — show full list
      for (const proj of projects) {
        list.appendChild(buildProjectCard(proj, false));
      }
      hideProjectChats();
    }

    if (typeof lucide !== 'undefined') lucide.createIcons();
  }

  /**
   * Build a single project card.
   * @param {object} proj - project data
   * @param {boolean} isActive - if true, show Exit button instead of Open
   */
  function buildProjectCard(proj, isActive) {
    const card = document.createElement('div');
    card.className = 'project-card' + (isActive ? ' active' : '');

    const info = document.createElement('div');
    info.className = 'project-card-info';

    const nameEl = document.createElement('div');
    nameEl.className = 'project-card-name';
    nameEl.textContent = proj.name;

    const metaEl = document.createElement('div');
    metaEl.className = 'project-card-meta';
    metaEl.textContent = proj.path || 'No path set';

    info.appendChild(nameEl);
    info.appendChild(metaEl);

    const actions = document.createElement('div');
    actions.className = 'project-card-actions';

    if (isActive) {
      // Exit button — deactivate project, restore main chat
      const exitBtn = document.createElement('button');
      exitBtn.textContent = 'Exit';
      exitBtn.style.color = 'var(--danger, #e74c3c)';
      exitBtn.style.borderColor = 'var(--danger, #e74c3c)';
      exitBtn.onclick = (e) => { e.stopPropagation(); deactivateProject(); };
      actions.appendChild(exitBtn);
    } else {
      // Open button — activate this project
      const openBtn = document.createElement('button');
      openBtn.textContent = 'Open';
      openBtn.onclick = (e) => { e.stopPropagation(); activateProject(proj); };
      actions.appendChild(openBtn);
    }

    // Settings button
    const settingsBtn = document.createElement('button');
    settingsBtn.innerHTML = '<i data-lucide="settings" style="width:12px;height:12px;"></i>';
    settingsBtn.title = 'Settings';
    settingsBtn.onclick = (e) => {
      e.stopPropagation();
      if (typeof showProjectSettingsPopup === 'function') {
        showProjectSettingsPopup(proj, settingsBtn);
      }
    };

    // Delete button
    const delBtn = document.createElement('button');
    delBtn.className = 'delete';
    delBtn.textContent = 'Delete';
    delBtn.onclick = async (e) => {
      e.stopPropagation();
      if (typeof sableConfirm === 'function') {
        if (await sableConfirm('Delete project "' + proj.name + '"? Chats will be moved to global.', { danger: true })) {
          fetch('/api/projects/' + proj.id, { method: 'DELETE' }).then(() => {
            if (activeProjectId === proj.id) {
              activeProjectId = null;
              _lastProjectChatId = null;
            }
            renderProjectsPanel();
          });
        }
      }
    };

    actions.appendChild(settingsBtn);
    actions.appendChild(delBtn);
    card.appendChild(info);
    card.appendChild(actions);

    // Clicking the card itself activates the project (only when not already active)
    if (!isActive) {
      card.addEventListener('click', () => activateProject(proj));
    }

    return card;
  }

  /* ═══════════════════════════════════════════
   *  PROJECT CHATS SECTION
   * ═══════════════════════════════════════════ */

  function hideProjectChats() {
    const section = $('projChatsSection');
    if (section) section.classList.add('hidden');
  }

  async function renderProjectChats() {
    const section = $('projChatsSection');
    const list = $('projChatsList');
    const empty = $('projChatsEmpty');
    if (!section || !list) return;

    section.classList.remove('hidden');
    list.innerHTML = '';

    try {
      const res = await fetch('/api/chats?project_id=' + encodeURIComponent(activeProjectId));
      const data = await res.json();
      const chats = (data.chats || []).sort((a, b) =>
        (b.updated_at || b.created_at || '').localeCompare(a.updated_at || a.created_at || '')
      );

      if (chats.length === 0) {
        if (empty) empty.style.display = '';
        return;
      }
      if (empty) empty.style.display = 'none';

      for (const chat of chats) {
        const row = document.createElement('div');
        row.className = 'chat-row';
        row.dataset.chatId = chat.id;

        const btn = document.createElement('button');
        btn.className = 'chat-item' + (chat.id === activeChatId ? ' active' : '');
        btn.textContent = chat.title || 'New chat';
        btn.onclick = () => openProjectChat(chat.id);

        const del = document.createElement('button');
        del.className = 'chat-delete';
        del.textContent = '×';
        del.title = 'Delete chat';
        del.onclick = async (e) => {
          e.stopPropagation();
          if (typeof sableConfirm !== 'function' || await sableConfirm('Delete this chat?', { danger: true })) {
            try {
              const r = await fetch('/api/chats/' + chat.id, { method: 'DELETE' });
              const d = await r.json().catch(() => ({}));
              if (r.ok && d.deleted) {
                if (activeChatId === chat.id) {
                  const remaining = chats.filter(c => c.id !== chat.id);
                  if (remaining.length > 0) {
                    openProjectChat(remaining[0].id);
                  } else {
                    activeChatId = null;
                    parentId = null;
                    _lastProjectChatId = null;
                    if (typeof saveActiveChat === 'function') saveActiveChat();
                  }
                }
                renderProjectChats();
              }
            } catch (err) {
              if (typeof showToast === 'function') showToast('Delete failed: ' + err.message, 'error');
            }
          }
        };

        row.appendChild(btn);
        row.appendChild(del);
        list.appendChild(row);
      }
    } catch (err) {
      console.error('Failed to load project chats:', err);
    }

    if (typeof lucide !== 'undefined') lucide.createIcons();
  }

  /**
   * Open a project chat WITHOUT touching the main sidebar.
   */
  async function openProjectChat(chatId) {
    _lastProjectChatId = chatId;

    if (typeof switchToTab === 'function') switchToTab(chatId);
    if (typeof saveActiveChat === 'function') saveActiveChat();

    // Load messages if not already loaded
    const alreadyOpen = (typeof openTabs !== 'undefined') && openTabs.has(chatId);
    if (!alreadyOpen) {
      try {
        const data = await fetch('/api/chats/' + chatId + '/messages?include_skill_events=true').then(r => r.json());
        const pane = (typeof ensurePane === 'function') ? ensurePane(chatId) : null;
        if (pane) {
          pane.innerHTML = '';
          const messages = data.messages || [];
          if (messages.length === 0) {
            pane.innerHTML = '<div class="empty"><h2>New conversation</h2><p>Send the first message.</p></div>';
          } else {
            for (const msg of messages) {
              if (typeof addHistoryMessage === 'function') addHistoryMessage(msg);
            }
            if (typeof renderMathJax === 'function') renderMathJax(pane);
          }
          if (messages.length > 0) {
            const last = messages[messages.length - 1];
            parentId = last?.parent_id ? String(last.parent_id) : last?.id ? String(last.id) : null;
          }
          if (typeof scrollBottom === 'function') scrollBottom(true);
        }
      } catch (err) {
        console.error('Failed to load project chat messages:', err);
      }
    }

    if (typeof onChatOpened === 'function') onChatOpened(chatId);
    highlightProjectChat(chatId);

    const inputEl = $('input');
    if (inputEl) inputEl.focus();
  }

  function highlightProjectChat(chatId) {
    const list = $('projChatsList');
    if (!list) return;
    list.querySelectorAll('.chat-item').forEach(btn => {
      btn.classList.toggle('active', btn.parentElement?.dataset?.chatId === chatId);
    });
  }

  /* ═══════════════════════════════════════════
   *  ACTIVATE / DEACTIVATE PROJECT
   * ═══════════════════════════════════════════ */

  function activateProject(proj) {
    if (activeProjectId === proj.id) return;

    // Save current main chat before entering project context
    if (activeChatId) _lastNonProjectChatId = activeChatId;

    fetch('/api/projects/' + proj.id + '/activate', { method: 'POST' })
      .then(r => r.json())
      .then(data => {
        activeProjectId = proj.id;
        if (data.new_cwd && typeof window.pickFsRoot === 'function') {
          window.pickFsRoot(data.new_cwd);
        }
        renderProjectsPanel();

        // Auto-open last project chat if we have one
        if (_lastProjectChatId) {
          openProjectChat(_lastProjectChatId);
        }
      })
      .catch(() => {
        activeProjectId = proj.id;
        renderProjectsPanel();
        if (_lastProjectChatId) openProjectChat(_lastProjectChatId);
      });
  }

  function deactivateProject() {
    // Save current project chat before leaving
    if (activeChatId) _lastProjectChatId = activeChatId;
    _savedProjectId = activeProjectId;

    activeProjectId = null;

    // Immediately hide project chats section
    hideProjectChats();

    // Restore last main chat
    if (_lastNonProjectChatId) {
      if (typeof switchToTab === 'function') switchToTab(_lastNonProjectChatId);
      if (typeof saveActiveChat === 'function') saveActiveChat();
    }

    renderProjectsPanel();
  }

  /* ═══════════════════════════════════════════
   *  NEW PROJECT FORM
   * ═══════════════════════════════════════════ */

  function setupAddForm() {
    const addBtn = $('projAddBtn');
    const form = $('projAddForm');
    const createBtn = $('projCreateBtn');
    const cancelBtn = $('projCancelBtn');
    const nameInput = $('projNewName');
    const pathInput = $('projNewPath');

    if (!addBtn || !form) return;

    addBtn.addEventListener('click', () => {
      form.classList.toggle('hidden');
      if (!form.classList.contains('hidden')) {
        nameInput.value = '';
        pathInput.value = '';
        nameInput.focus();
      }
    });

    cancelBtn?.addEventListener('click', () => form.classList.add('hidden'));

    createBtn?.addEventListener('click', () => {
      const name = nameInput.value.trim();
      if (!name) { nameInput.focus(); return; }

      const body = { name };
      const path = pathInput.value.trim();
      if (path) body.path = path;

      fetch('/api/projects', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      }).then(r => r.json()).then(data => {
        if (data.id) {
          form.classList.add('hidden');
          // Save current chat before activating new project
          if (activeChatId) _lastNonProjectChatId = activeChatId;
          fetch('/api/projects/' + data.id + '/activate', { method: 'POST' })
            .then(() => {
              activeProjectId = data.id;
              if (typeof loadProjects === 'function') loadProjects().then(() => renderProjectsPanel());
            })
            .catch(() => {
              activeProjectId = data.id;
              if (typeof loadProjects === 'function') loadProjects().then(() => renderProjectsPanel());
            });
        } else {
          if (typeof showToast === 'function') showToast(data.error || 'Failed to create project', 'error');
        }
      });
    });

    nameInput?.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') createBtn?.click();
    });
  }

  /* ═══════════════════════════════════════════
   *  NEW CHAT BUTTON
   * ═══════════════════════════════════════════ */

  function setupProjectChatBtn() {
    const btn = $('projNewChatBtn');
    if (!btn) return;
    btn.addEventListener('click', async () => {
      if (!activeProjectId) return;
      try {
        const model = (typeof selectedModel !== 'undefined' ? selectedModel : null);
        const res = await fetch('/api/chat/new', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ model, project_id: activeProjectId })
        });
        const data = await res.json();
        if (data.chat_id) {
          openProjectChat(data.chat_id);
          renderProjectChats();
        } else {
          if (typeof showToast === 'function') showToast(data.error || 'Could not create chat', 'error');
        }
      } catch (err) {
        if (typeof showToast === 'function') showToast('Network error: ' + err.message, 'error');
      }
    });
  }

  /* ═══════════════════════════════════════════
   *  RAIL SWITCHING
   * ═══════════════════════════════════════════ */

  function setupRailListener() {
    window.addEventListener('rail-switch', (e) => {
      const target = e.detail?.target;

      // Switching TO main chat → deactivate project, restore last main chat
      if (target === 'chat' && activeProjectId) {
        // Save project state
        if (activeChatId) _lastProjectChatId = activeChatId;
        _savedProjectId = activeProjectId;

        // Clear project context so main sidebar shows ALL chats
        activeProjectId = null;

        // Restore last non-project chat
        if (_lastNonProjectChatId) {
          if (typeof switchToTab === 'function') switchToTab(_lastNonProjectChatId);
          if (typeof saveActiveChat === 'function') saveActiveChat();
        }

        // Refresh projects panel if it's still visible
        renderProjectsPanel();
      }

      // Switching TO projects → restore last active project + its chat
      if (target === 'projects') {
        // Save current main chat
        if (activeChatId && !activeProjectId) {
          _lastNonProjectChatId = activeChatId;
        }

        // Restore saved project if we had one
        if (!activeProjectId && _savedProjectId) {
          activeProjectId = _savedProjectId;
          renderProjectsPanel();
          if (_lastProjectChatId) {
            openProjectChat(_lastProjectChatId);
          }
        }
      }
    });
  }

  /* ═══════════════════════════════════════════
   *  INIT
   * ═══════════════════════════════════════════ */

  function init() {
    if (!window.sidebarHost) return;

    window.sidebarHost.savePosition('projects', 'left');
    window.sidebarHost.register('projects', {
      panelId: 'projectsPanel',
      onOpen: () => {
        document.body.classList.remove('diff-open', 'calendar-open');
        const calView = $('calendarView');
        if (calView) calView.classList.add('hidden');
        if (typeof AgentPanel !== 'undefined') AgentPanel.close();

        // Save current main chat before entering projects
        if (activeChatId && !activeProjectId) {
          _lastNonProjectChatId = activeChatId;
        }

        // Restore saved project if available
        if (!activeProjectId && _savedProjectId) {
          activeProjectId = _savedProjectId;
        }

        renderProjectsPanel();

        // Auto-restore last project chat
        if (activeProjectId && _lastProjectChatId) {
          openProjectChat(_lastProjectChatId);
        }
      },
      onClose: () => {},
    });

    setupAddForm();
    setupProjectChatBtn();
    setupRailListener();

    window._sableRefreshProjectChats = renderProjectChats;
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
