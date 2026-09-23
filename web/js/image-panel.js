/* ── Image Generator Panel (Main Area + Sidebar Recent Images) ── */
/* Depends on: renderImageGenPanel (from library.js), sidebarHost, escHtml */
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
    ['searchView', 'dashboardView', 'knowledgeView', 'researchView', 'promptgenView', 'calendarView', 'ocrView'].forEach(id => {
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
  function openImageView() {
    const view = document.getElementById('imageView');
    if (!view) return;
    hideOtherViews();
    document.body.classList.add('image-open');
    view.classList.remove('hidden');
    if (typeof renderImageGenPanel === 'function') {
      renderImageGenPanel(view);
    }
    // Host sidebar widget
    const widget = document.getElementById('imageSidebarWidget');
    if (widget && window.sidebarHost) {
      widget.classList.remove('hidden');
      sidebarHost.host('imageSidebarWidget');
      if (!sidebarLoaded) {
        loadSidebarImages();
      }
    }
    isOpen = true;
  }

  function closeImageView() {
    if (!isOpen) return;
    document.body.classList.remove('image-open');
    const view = document.getElementById('imageView');
    if (view) view.classList.add('hidden');
    const widget = document.getElementById('imageSidebarWidget');
    if (widget && window.sidebarHost && sidebarHost.getCurrent() === 'imageSidebarWidget') {
      sidebarHost.unhost();
      widget.classList.add('hidden');
    } else if (widget) {
      widget.classList.add('hidden');
    }
    showChat();
    isOpen = false;
  }

  /* ── Sidebar: Prompt History (results live in the main window) ──
     Each entry: click → restore the saved result into the main panel, or
     re-fill the prompt box if no result was captured for that run. */
  function loadSidebarImages() {
    const content = document.getElementById('igSidebarContent');
    if (!content) return;
    if (!window.genHistory) {
      content.innerHTML = '<div class="tracknote-empty">History module not loaded.</div>';
      return;
    }
    window.genHistory.renderInto('image', content, (item) => {
      const meta = item.meta || {};
      const restored = meta.result && typeof window._igRestoreResult === 'function'
        ? window._igRestoreResult(meta.result, { prompt: item.text, provider: meta.provider })
        : false;
      if (!restored) {
        const ta = document.getElementById('igPrompt');
        if (ta) {
          ta.value = item.text;
          ta.focus();
          ta.dispatchEvent(new Event('input', { bubbles: true }));
        }
      }
      sidebarLoaded = true;
    });
    sidebarLoaded = true;
  }

  function formatSize(bytes) {
    if (!bytes) return '';
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / 1048576).toFixed(1) + ' MB';
  }

  /* ── Rail-switch handler ── */
  window.addEventListener('rail-switch', (e) => {
    const target = e.detail?.target;
    if (target === 'image') {
      if (isOpen) {
        closeImageView();
      } else {
        openImageView();
      }
    } else if (isOpen) {
      closeImageView();
    }
  });

  /* ── Init ── */
  function init() {
    // Create sidebar widget and insert into DOM
    const widget = document.createElement('div');
    widget.id = 'imageSidebarWidget';
    widget.className = 'tracknote-standalone hidden';
    widget.innerHTML = `
      <div class="tracknote-standalone-header">
        <span class="tracknote-standalone-title"><i data-lucide="history" class="icon-lucide"></i> Prompt History</span>
        <button class="tn-add-toggle" id="igSidebarClear" title="Clear history"><i data-lucide="trash-2" class="icon-lucide"></i></button>
      </div>
      <div class="tracknote-body" id="igSidebarContent"></div>
    `;
    document.body.appendChild(widget);

    widget.querySelector('#igSidebarClear')?.addEventListener('click', () => {
      if (!window.genHistory) return;
      if (!confirm('Clear all image prompt history?')) return;
      window.genHistory.clear('image');
    });

    // Keep the list live while the panel is open.
    window.addEventListener('gen-history-change', (e) => {
      if (e.detail?.kind === 'image' && isOpen) loadSidebarImages();
    });

    if (window.lucide) lucide.createIcons({ nodes: widget.querySelectorAll('[data-lucide]') });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
