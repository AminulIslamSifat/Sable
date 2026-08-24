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
    ['searchView', 'dashboardView', 'knowledgeView', 'researchView', 'promptgenView', 'calendarView'].forEach(id => {
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

  /* ── Sidebar: Recent Generated Images ── */
  async function loadSidebarImages() {
    const content = document.getElementById('igSidebarContent');
    if (!content) return;
    content.innerHTML = '<div class="ig-sidebar-loading">Loading…</div>';
    try {
      const res = await fetch('/api/library/gallery');
      const items = await res.json();
      if (!items || !items.length) {
        content.innerHTML = '<div class="ig-sidebar-empty">No images yet.<br>Generate one from the main panel!</div>';
        return;
      }
      content.innerHTML = '';
      const list = document.createElement('div');
      list.className = 'ig-sidebar-list';
      items.slice(0, 20).forEach(item => {
        const card = document.createElement('div');
        card.className = 'ig-sidebar-card';
        const dateStr = item.date
          ? new Date(item.date * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
          : '';
        card.innerHTML = `
          <div class="ig-card-thumb">
            <img src="${escHtml(item.url)}" alt="${escHtml(item.filename)}" loading="lazy">
          </div>
          <div class="ig-card-body">
            <div class="ig-card-name">${escHtml(item.filename)}</div>
            <div class="ig-card-date">${dateStr}</div>
            <div class="ig-card-meta">${item.type?.toUpperCase() || ''} · ${formatSize(item.size)}</div>
          </div>
        `;
        card.addEventListener('click', () => {
          // Open image in a new tab or lightbox
          window.open(item.url, '_blank');
        });
        list.appendChild(card);
      });
      content.appendChild(list);
      sidebarLoaded = true;
    } catch {
      content.innerHTML = '<div class="ig-sidebar-empty">Failed to load images.</div>';
    }
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
    widget.className = 'ig-sidebar-widget hidden';
    widget.innerHTML = `
      <div class="ig-sidebar-header">
        <span class="ig-sidebar-title"><i data-lucide="image" class="icon-lucide"></i> Recent Images</span>
        <button class="ig-sidebar-refresh" id="igSidebarRefresh" title="Refresh"><i data-lucide="refresh-cw"></i></button>
      </div>
      <div class="ig-sidebar-content" id="igSidebarContent"></div>
    `;
    document.body.appendChild(widget);

    // Bind refresh button
    widget.querySelector('#igSidebarRefresh')?.addEventListener('click', () => {
      sidebarLoaded = false;
      loadSidebarImages();
    });

    if (window.lucide) lucide.createIcons({ nodes: widget.querySelectorAll('[data-lucide]') });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
