/* ── Research Panel (Main Area + Sidebar Recent Research) ── */
/* Depends on: renderResearchPanel (from research.js), sidebarHost, escHtml, openLibraryReader */
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
    ['searchView', 'dashboardView', 'knowledgeView', 'imageView', 'promptgenView', 'calendarView', 'ocrView'].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.classList.add('hidden');
    });
    // Also reset inline display styles that search panel may have set
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
  function openResearchView() {
    const view = document.getElementById('researchView');
    if (!view) return;
    hideOtherViews();
    document.body.classList.add('research-open');
    view.classList.remove('hidden');
    if (typeof renderResearchPanel === 'function') {
      renderResearchPanel(view);
    }
    // Host sidebar widget
    const widget = document.getElementById('researchSidebarWidget');
    if (widget && window.sidebarHost) {
      widget.classList.remove('hidden');
      sidebarHost.host('researchSidebarWidget');
      if (!sidebarLoaded) {
        loadSidebarResearch();
      }
    }
    isOpen = true;
  }

  function closeResearchView() {
    if (!isOpen) return;
    document.body.classList.remove('research-open');
    const view = document.getElementById('researchView');
    if (view) view.classList.add('hidden');
    const widget = document.getElementById('researchSidebarWidget');
    if (widget && window.sidebarHost && sidebarHost.getCurrent() === 'researchSidebarWidget') {
      sidebarHost.unhost();
      widget.classList.add('hidden');
    } else if (widget) {
      widget.classList.add('hidden');
    }
    showChat();
    isOpen = false;
  }

  /* ── Sidebar: Recent Research ── */
  async function loadSidebarResearch() {
    const content = document.getElementById('rpSidebarContent');
    if (!content) return;
    content.innerHTML = '<div class="rp-sidebar-loading">Loading…</div>';
    try {
      const res = await fetch('/api/library/research');
      const items = await res.json();
      if (!items || !items.length) {
        content.innerHTML = '<div class="rp-sidebar-empty">No research yet.<br>Start one from the main panel!</div>';
        return;
      }
      content.innerHTML = '';
      const list = document.createElement('div');
      list.className = 'rp-sidebar-list';
      items.slice(0, 20).forEach(item => {
        const card = document.createElement('div');
        card.className = 'rp-sidebar-card';
        card.innerHTML = `
          <div class="rp-card-icon"><i data-lucide="file-text"></i></div>
          <div class="rp-card-body">
            <div class="rp-card-title">${escHtml(item.title)}</div>
            <div class="rp-card-date">${item.date || ''}</div>
            <div class="rp-card-preview">${escHtml((item.preview || '').slice(0, 100))}</div>
          </div>
        `;
        card.addEventListener('click', () => {
          if (typeof openLibraryReader === 'function') {
            openLibraryReader('research', item.filename, item.title);
          }
        });
        list.appendChild(card);
      });
      content.appendChild(list);
      if (window.lucide) lucide.createIcons({ nodes: list.querySelectorAll('[data-lucide]') });
      sidebarLoaded = true;
    } catch {
      content.innerHTML = '<div class="rp-sidebar-empty">Failed to load research.</div>';
    }
  }

  /* ── Rail-switch handler ── */
  window.addEventListener('rail-switch', (e) => {
    const target = e.detail?.target;
    if (target === 'research') {
      if (isOpen) {
        closeResearchView();
      } else {
        openResearchView();
      }
    } else if (isOpen) {
      closeResearchView();
    }
  });

  /* ── Init ── */
  function init() {
    // Create sidebar widget and insert into DOM
    const widget = document.createElement('div');
    widget.id = 'researchSidebarWidget';
    widget.className = 'rp-sidebar-widget hidden';
    widget.innerHTML = `
      <div class="rp-sidebar-header">
        <span class="rp-sidebar-title">🔬 Recent Research</span>
        <button class="rp-sidebar-refresh" id="rpSidebarRefresh" title="Refresh"><i data-lucide="refresh-cw"></i></button>
      </div>
      <div class="rp-sidebar-content" id="rpSidebarContent"></div>
    `;
    document.body.appendChild(widget);

    // Bind refresh button
    widget.querySelector('#rpSidebarRefresh')?.addEventListener('click', () => {
      sidebarLoaded = false;
      loadSidebarResearch();
    });

    if (window.lucide) lucide.createIcons({ nodes: widget.querySelectorAll('[data-lucide]') });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
