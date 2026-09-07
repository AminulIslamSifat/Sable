/* ── Prompt Generator Panel (Main Area + Sidebar Recent Prompts) ── */
/* Depends on: renderPromptGenPanel (from library.js), sidebarHost, escHtml, openLibraryReader */
(function () {
  'use strict';

  let isOpen = false;
  let sidebarLoaded = false;

  function escHtml(s) {
    const d = document.createElement('div');
    d.textContent = s ?? '';
    return d.innerHTML;
  }

  /* ── Helpers ── */
  function hideOtherViews() {
    const chat = document.getElementById('chat');
    if (chat) chat.classList.add('hidden');
    const inputArea = document.getElementById('inputArea');
    if (inputArea) inputArea.classList.add('hidden');
    ['searchView', 'dashboardView', 'knowledgeView', 'researchView', 'imageView', 'calendarView', 'ocrView'].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.classList.add('hidden');
    });
    const searchEl = document.getElementById('searchView');
    if (searchEl) searchEl.style.display = '';
  }

  function showChat() {
    const chat = document.getElementById('chat');
    if (chat) chat.classList.remove('hidden');
    const inputArea = document.getElementById('inputArea');
    if (inputArea) inputArea.classList.remove('hidden');
  }

  /* ── Main Area View ── */
  function openPromptgenView() {
    const view = document.getElementById('promptgenView');
    if (!view) return;
    hideOtherViews();
    document.body.classList.add('promptgen-open');
    view.classList.remove('hidden');
    if (typeof renderPromptGenPanel === 'function') {
      renderPromptGenPanel(view);
    }
    // Host sidebar widget
    const widget = document.getElementById('pgSidebarWidget');
    if (widget && window.sidebarHost) {
      widget.classList.remove('hidden');
      sidebarHost.host('pgSidebarWidget');
      if (!sidebarLoaded) {
        loadSidebarPrompts();
      }
    }
    isOpen = true;
  }

  function closePromptgenView() {
    if (!isOpen) return;
    document.body.classList.remove('promptgen-open');
    const view = document.getElementById('promptgenView');
    if (view) view.classList.add('hidden');
    const widget = document.getElementById('pgSidebarWidget');
    if (widget && window.sidebarHost && sidebarHost.getCurrent() === 'pgSidebarWidget') {
      sidebarHost.unhost();
      widget.classList.add('hidden');
    } else if (widget) {
      widget.classList.add('hidden');
    }
    showChat();
    isOpen = false;
  }

  /* ── Sidebar: Recent Prompts ── */
  async function loadSidebarPrompts() {
    const content = document.getElementById('pgSidebarContent');
    if (!content) return;
    content.innerHTML = '<div class="pg-sidebar-loading">Loading…</div>';
    try {
      const res = await fetch('/api/library/prompts');
      const items = await res.json();
      if (!items || !items.length) {
        content.innerHTML = '<div class="pg-sidebar-empty">No prompts yet.<br>Generate one from the main panel!</div>';
        return;
      }
      content.innerHTML = '';
      const list = document.createElement('div');
      list.className = 'pg-sidebar-list';
      items.slice(0, 20).forEach(item => {
        const card = document.createElement('div');
        card.className = 'pg-sidebar-card';
        card.innerHTML = `
          <div class="pg-card-icon"><i data-lucide="sparkles"></i></div>
          <div class="pg-card-body">
            <div class="pg-card-title">${escHtml(item.title)}</div>
            <div class="pg-card-date">${item.date || ''}</div>
            <div class="pg-card-preview">${escHtml((item.preview || '').slice(0, 100))}</div>
          </div>
        `;
        card.addEventListener('click', () => {
          if (typeof openLibraryReader === 'function') {
            openLibraryReader('prompts', item.filename, item.title);
          }
        });
        list.appendChild(card);
      });
      content.appendChild(list);
      if (window.lucide) lucide.createIcons({ nodes: list.querySelectorAll('[data-lucide]') });
      sidebarLoaded = true;
    } catch {
      content.innerHTML = '<div class="pg-sidebar-empty">Failed to load prompts.</div>';
    }
  }

  /* ── Rail-switch handler ── */
  window.addEventListener('rail-switch', (e) => {
    const target = e.detail?.target;
    if (target === 'prompt') {
      if (isOpen) {
        closePromptgenView();
      } else {
        openPromptgenView();
      }
    } else if (isOpen) {
      closePromptgenView();
    }
  });

  /* ── Init ── */
  function init() {
    // Create sidebar widget and insert into DOM
    const widget = document.createElement('div');
    widget.id = 'pgSidebarWidget';
    widget.className = 'pg-sidebar-widget hidden';
    widget.innerHTML = `
      <div class="pg-sidebar-header">
        <span class="pg-sidebar-title">✨ Recent Prompts</span>
        <button class="pg-sidebar-refresh" id="pgSidebarRefresh" title="Refresh"><i data-lucide="refresh-cw"></i></button>
      </div>
      <div class="pg-sidebar-content" id="pgSidebarContent"></div>
    `;
    document.body.appendChild(widget);

    // Bind refresh button
    widget.querySelector('#pgSidebarRefresh')?.addEventListener('click', () => {
      sidebarLoaded = false;
      loadSidebarPrompts();
    });

    if (window.lucide) lucide.createIcons({ nodes: widget.querySelectorAll('[data-lucide]') });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
