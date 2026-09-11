
/**
 * resize-panels.js — Resizable left/right panels in IDE mode + agent sidebar
 */
(function () {
  'use strict';

  const LEFT_KEY = 'sable_ide_left_w';
  const SIDEBAR_KEY = 'sable_sidebar_w';
  const MIN_LEFT = 200, MAX_LEFT = 500;
  const MIN_SIDEBAR = 180, MAX_SIDEBAR = 480;

  let leftHandle, sidebarHandle;

  function isDesktop() {
    return window.innerWidth >= 769;
  }

  function isIdeMode() {
    return document.body.getAttribute('data-mode') === 'ide';
  }

  function createHandles() {
    leftHandle = document.createElement('div');
    leftHandle.className = 'ide-resize-handle ide-resize-left';
    leftHandle.title = 'Drag to resize chat panel';
    document.body.appendChild(leftHandle);
  }

  /* ── Agent-mode sidebar resize ── */
  function createSidebarHandle() {
    const sidebar = document.querySelector('.sidebar');
    if (!sidebar || sidebar.querySelector('.sidebar-resize-handle')) return;
    sidebarHandle = document.createElement('div');
    sidebarHandle.className = 'sidebar-resize-handle';
    sidebarHandle.title = 'Drag to resize sidebar';
    sidebar.appendChild(sidebarHandle);

    sidebarHandle.addEventListener('mousedown', (e) => {
      if (!isDesktop() || isIdeMode()) return;
      if (document.body.classList.contains('sidebar-collapsed')) return;
      e.preventDefault();
      e.stopPropagation();

      const startX = e.clientX;
      const startW = sidebar.getBoundingClientRect().width;
      document.body.classList.add('sidebar-resizing');

      function onMove(ev) {
        const dx = ev.clientX - startX;
        const newW = clamp(startW + dx, MIN_SIDEBAR, MAX_SIDEBAR);
        sidebar.style.setProperty('--sidebar-w', newW + 'px');
        localStorage.setItem(SIDEBAR_KEY, newW);
      }

      function onUp() {
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
        document.body.classList.remove('sidebar-resizing');
      }

      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });
  }

  function restoreSidebarWidth() {
    if (isIdeMode()) return;
    const saved = localStorage.getItem(SIDEBAR_KEY);
    if (saved) {
      const sidebar = document.querySelector('.sidebar');
      if (sidebar) sidebar.style.setProperty('--sidebar-w', saved + 'px');
    }
  }

  function positionHandles() {
    if (!isIdeMode() || !isDesktop()) {
      if (leftHandle) leftHandle.style.display = 'none';
      return;
    }

    const chatCompact = document.getElementById('chatCompact');

    // Left handle: right edge of chatCompact
    if (chatCompact && chatCompact.offsetParent !== null) {
      const rect = chatCompact.getBoundingClientRect();
      leftHandle.style.display = '';
      leftHandle.style.left = (rect.right - 3) + 'px';
    } else {
      leftHandle.style.display = 'none';
    }
  }

  function clamp(val, min, max) {
    return Math.max(min, Math.min(max, val));
  }

  function startDrag(handle) {
    return function (e) {
      if (!isIdeMode() || !isDesktop()) return;
      e.preventDefault();

      const startX = e.clientX;
      const chatCompact = document.getElementById('chatCompact');
      const startW = chatCompact.getBoundingClientRect().width;

      document.body.classList.add('is-resizing');

      function onMove(ev) {
        const dx = ev.clientX - startX;
        const newW = clamp(startW + dx, MIN_LEFT, MAX_LEFT);
        chatCompact.style.width = newW + 'px';
        chatCompact.style.minWidth = newW + 'px';
        localStorage.setItem(LEFT_KEY, newW);
        positionHandles();
      }

      function onUp() {
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
        document.body.classList.remove('is-resizing');
      }

      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    };
  }

  function restoreWidths() {
    if (!isIdeMode()) return;
    const savedLeft = localStorage.getItem(LEFT_KEY);
    if (savedLeft) {
      const chatCompact = document.getElementById('chatCompact');
      if (chatCompact) {
        chatCompact.style.width = savedLeft + 'px';
        chatCompact.style.minWidth = savedLeft + 'px';
      }
    }
  }

  function init() {
    if (!isDesktop()) return;
    createHandles();
    createSidebarHandle();
    restoreWidths();
    restoreSidebarWidth();
    positionHandles();

    leftHandle.addEventListener('mousedown', startDrag(leftHandle));

    // Reposition on resize and mode changes
    window.addEventListener('resize', positionHandles);

    const observer = new MutationObserver(() => {
      positionHandles();
      if (isIdeMode() && isDesktop()) {
        restoreWidths();
      } else {
        restoreSidebarWidth();
      }
    });
    observer.observe(document.body, {
      attributes: true,
      attributeFilter: ['data-mode', 'class']
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
