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

  /* ── Sidebar: Query History (results live in the main window) ──
     Clicking an entry refills the research question so it can be re-run.
     Saved reports are still reachable from the Past Research list in the
     main panel, so history here stays focused on *what you asked*. */
  function loadSidebarResearch() {
    const content = document.getElementById('rpSidebarContent');
    if (!content) return;
    if (!window.genHistory) {
      content.innerHTML = '<div class="tracknote-empty">History module not loaded.</div>';
      return;
    }
    window.genHistory.renderInto('research', content, (item) => {
      const ta = document.getElementById('researchQuery');
      if (ta) {
        ta.value = item.text;
        ta.focus();
        ta.dispatchEvent(new Event('input', { bubbles: true }));
        const launch = document.querySelector('.research-launch');
        if (launch) launch.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
    });
    sidebarLoaded = true;
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
    widget.className = 'tracknote-standalone hidden';
    widget.innerHTML = `
      <div class="tracknote-standalone-header">
        <span class="tracknote-standalone-title"><i data-lucide="history" class="icon-lucide"></i> Query History</span>
        <button class="tn-add-toggle" id="rpSidebarClear" title="Clear history"><i data-lucide="trash-2" class="icon-lucide"></i></button>
      </div>
      <div class="tracknote-body" id="rpSidebarContent"></div>
    `;
    document.body.appendChild(widget);

    widget.querySelector('#rpSidebarClear')?.addEventListener('click', () => {
      if (!window.genHistory) return;
      if (!confirm('Clear all research query history?')) return;
      window.genHistory.clear('research');
    });

    window.addEventListener('gen-history-change', (e) => {
      if (e.detail?.kind === 'research' && isOpen) loadSidebarResearch();
    });

    if (window.lucide) lucide.createIcons({ nodes: widget.querySelectorAll('[data-lucide]') });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
