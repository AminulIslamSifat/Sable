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

  /* ── Sidebar: Prompt History (results live in the main window) ──
     Clicking an entry restores the saved generated prompt into the output
     area, or refills the task box if no output was captured. */
  function loadSidebarPrompts() {
    const content = document.getElementById('pgSidebarContent');
    if (!content) return;
    if (!window.genHistory) {
      content.innerHTML = '<div class="tracknote-empty">History module not loaded.</div>';
      return;
    }
    window.genHistory.renderInto('prompt', content, (item) => {
      const meta = item.meta || {};
      const outputEl = document.getElementById('pgOutput');
      const wrap = document.getElementById('pgOutputWrap');
      if (meta.output && outputEl && wrap) {
        outputEl.textContent = meta.output;
        wrap.style.display = 'block';
        const task = document.getElementById('pgQuery');
        if (task && meta.task) task.value = meta.task;
      } else {
        const task = document.getElementById('pgQuery');
        if (task) {
          task.value = item.text;
          task.focus();
          task.dispatchEvent(new Event('input', { bubbles: true }));
        }
      }
    });
    sidebarLoaded = true;
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
    widget.className = 'tracknote-standalone hidden';
    widget.innerHTML = `
      <div class="tracknote-standalone-header">
        <span class="tracknote-standalone-title"><i data-lucide="history" class="icon-lucide"></i> Prompt History</span>
        <button class="tn-add-toggle" id="pgSidebarClear" title="Clear history"><i data-lucide="trash-2" class="icon-lucide"></i></button>
      </div>
      <div class="tracknote-body" id="pgSidebarContent"></div>
    `;
    document.body.appendChild(widget);

    widget.querySelector('#pgSidebarClear')?.addEventListener('click', () => {
      if (!window.genHistory) return;
      if (!confirm('Clear all prompt history?')) return;
      window.genHistory.clear('prompt');
    });

    window.addEventListener('gen-history-change', (e) => {
      if (e.detail?.kind === 'prompt' && isOpen) loadSidebarPrompts();
    });

    if (window.lucide) lucide.createIcons({ nodes: widget.querySelectorAll('[data-lucide]') });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
