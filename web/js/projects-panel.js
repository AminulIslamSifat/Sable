/**
 * projects-panel.js — Projects sidebar panel
 * Reuses existing /api/projects endpoints and global projectList/activeProjectId.
 */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);

  /* ── Render project list into sidebar panel ── */
  async function renderProjectsPanel() {
    // Ensure we have fresh data
    if (typeof loadProjects === 'function') await loadProjects();

    const list = $('projList');
    const empty = $('projEmpty');
    if (!list) return;
    list.innerHTML = '';

    const projects = (typeof projectList !== 'undefined' ? projectList : []);

    if (projects.length === 0) {
      if (empty) empty.style.display = '';
      return;
    }
    if (empty) empty.style.display = 'none';

    for (const proj of projects) {
      const card = document.createElement('div');
      card.className = 'project-card' + (activeProjectId === proj.id ? ' active' : '');

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

      // Activate button
      const activateBtn = document.createElement('button');
      activateBtn.textContent = activeProjectId === proj.id ? 'Active' : 'Open';
      if (activeProjectId === proj.id) {
        activateBtn.style.color = 'var(--accent)';
        activateBtn.style.borderColor = 'var(--accent)';
      }
      activateBtn.onclick = () => activateProject(proj);

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
                if (typeof loadChats === 'function') loadChats();
              }
              renderProjectsPanel();
            });
          }
        }
      };

      actions.appendChild(activateBtn);
      actions.appendChild(settingsBtn);
      actions.appendChild(delBtn);
      card.appendChild(info);
      card.appendChild(actions);

      // Click card itself to activate
      card.addEventListener('click', () => activateProject(proj));

      list.appendChild(card);
    }

    if (typeof lucide !== 'undefined') lucide.createIcons();
  }

  /* ── Activate a project ── */
  function activateProject(proj) {
    if (activeProjectId === proj.id) return; // already active

    fetch('/api/projects/' + proj.id + '/activate', { method: 'POST' })
      .then(r => r.json())
      .then(data => {
        activeProjectId = proj.id;
        if (data.new_cwd && typeof window.pickFsRoot === 'function') {
          window.pickFsRoot(data.new_cwd);
        }
        if (typeof createChat === 'function') createChat();
        renderProjectsPanel();
      })
      .catch(() => {
        activeProjectId = proj.id;
        if (typeof createChat === 'function') createChat();
        renderProjectsPanel();
      });
  }

  /* ── Create new project ── */
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

    cancelBtn?.addEventListener('click', () => {
      form.classList.add('hidden');
    });

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
          fetch('/api/projects/' + data.id + '/activate', { method: 'POST' })
            .then(() => {
              activeProjectId = data.id;
              if (typeof loadProjects === 'function') loadProjects().then(() => {
                renderProjectsPanel();
                if (typeof loadChats === 'function') loadChats();
              });
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

    // Enter key in name field submits
    nameInput?.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') createBtn?.click();
    });
  }

  /* ── Register with sidebar host ── */
  function init() {
    if (!window.sidebarHost) return;

    window.sidebarHost.savePosition('projects', 'left');
    window.sidebarHost.register('projects', {
      panelId: 'projectsPanel',
      onOpen: () => {
        // Close other panels that might conflict
        document.body.classList.remove('diff-open', 'calendar-open');
        const calView = $('calendarView');
        if (calView) calView.classList.add('hidden');
        if (typeof AgentPanel !== 'undefined') AgentPanel.close();
        renderProjectsPanel();
      },
      onClose: () => {},
    });

    setupAddForm();
  }

  // Wait for DOM + sidebar-host
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
