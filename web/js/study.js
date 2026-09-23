/**
 * study.js — Study mode: dashboard, tabs (Library/Rooms/Academics),
 * and a contextual left sidebar that adapts per sub-view.
 *
 * Owns:
 *  - #studyView visibility (driven by body[data-mode="study"])
 *  - #studyContent rendering per active tab
 *  - Sidebar injection/restore so the study sub-view gets its own panel
 */
(function () {
  'use strict';

  const body = document.body;
  const view = document.getElementById('studyView');
  const content = document.getElementById('studyContent');
  const sidebar = document.querySelector('.sidebar');

  if (!view || !content) return;

  // Sidebar sections we hide while in study mode.
  // NOTE: keep .sidebar-top visible — it holds the mode switcher, so hiding it
  // would trap the user in study mode.
  // .sidebar-chats lives inside a .sidebar-content-slot wrapper created by
  // sidebar-host.js — both are flex:1, so BOTH must collapse or the study
  // panel only gets half the sidebar.
  const HIDE_IN_STUDY = ['sidebar-content-slot', 'sidebar-chats'];
  let sidebarPanel = null; // our injected contextual panel
  let activeTab = 'library';

  // ─── Dummy data (replace with API later) ───
  const LIBRARY_DOCS = [];
  const ROOMS = [];

  // ─── Tab meta ───
  const TABS = ['library', 'rooms', 'academics'];

  // ─── Rendering ───

  function renderLibrary() {
    if (!LIBRARY_DOCS.length) {
      return `
        <div class="study-empty">
          <div class="study-empty-icon">📚</div>
          <h3>Your library is empty</h3>
          <p>Upload PDFs, lecture slides, or notes. Nova will OCR them and you can study right here.</p>
          <button class="study-empty-btn" data-study-action="upload">Upload a document</button>
        </div>`;
    }
    return `<div class="study-library">${LIBRARY_DOCS.map(docCard).join('')}</div>`;
  }

  function docCard(d) {
    return `
      <div class="study-doc-card" data-doc-id="${d.id}">
        <div class="study-doc-thumb">
          ${d.thumb ? `<img src="${d.thumb}" alt="">` : '📄'}
          <div class="study-doc-progress" style="width:${d.progress || 0}%"></div>
        </div>
        <button class="study-doc-menu" title="More">⋯</button>
        <div class="study-doc-info">
          <div class="study-doc-title">${escapeHtml(d.title)}</div>
          <div class="study-doc-meta">
            <span>${d.pages || 0} pages</span>
            <span>${d.progress || 0}%</span>
          </div>
        </div>
      </div>`;
  }

  function renderRooms() {
    return `
      <div class="study-rooms">
        <div class="study-room-card">
          <div class="study-room-icon"><i data-lucide="plus" class="icon-lucide"></i></div>
          <h3>Create a room</h3>
          <p>Start a live study session. Share the code with friends and study together in real time.</p>
          <button class="study-room-action primary" data-study-action="create-room">Create room</button>
        </div>
        <div class="study-room-card">
          <div class="study-room-icon"><i data-lucide="log-in" class="icon-lucide"></i></div>
          <h3>Join a room</h3>
          <p>Got a code from a friend? Enter it below to hop into their session.</p>
          <input class="study-room-code-input" id="studyRoomCode" maxlength="8" placeholder="Enter code" autocomplete="off">
          <button class="study-room-action secondary" data-study-action="join-room">Join room</button>
        </div>
      </div>`;
  }

  function renderAcademics() {
    return `
      <div class="study-academics">
        <div class="study-academics-badge"><i data-lucide="construction" class="icon-lucide"></i> Coming soon</div>
        <h3>Academics</h3>
        <p>Courses, semesters, assignments, and grades — a full academic hub for your CSE life. Still baking. 🍞</p>
      </div>`;
  }

  const RENDERERS = { library: renderLibrary, rooms: renderRooms, academics: renderAcademics };

  function setTab(tab) {
    if (!TABS.includes(tab)) tab = 'library';
    activeTab = tab;

    // Tabs UI
    view.querySelectorAll('.study-tab').forEach((btn) => {
      btn.classList.toggle('active', btn.dataset.studyTab === tab);
    });

    // Content
    content.innerHTML = (RENDERERS[tab] || (() => ''))();
    if (window.lucide) window.lucide.createIcons();

    // Contextual sidebar
    renderSidebar(tab);
  }

  // ─── Contextual sidebar ───

  function ensureSidebarPanel() {
    if (sidebarPanel) return sidebarPanel;
    sidebarPanel = document.createElement('div');
    sidebarPanel.className = 'study-sidebar-content';
    sidebarPanel.id = 'studySidebarPanel';
    sidebarPanel.hidden = true;
    // Insert right after .sidebar-top (before .sidebar-chats) so it renders
    // from the top of the sidebar, not pushed to the bottom by the flex column.
    const top = sidebar.querySelector('.sidebar-top');
    if (top && top.nextSibling) {
      sidebar.insertBefore(sidebarPanel, top.nextSibling);
    } else if (top) {
      top.after(sidebarPanel);
    } else {
      sidebar.appendChild(sidebarPanel);
    }
    return sidebarPanel;
  }

  function renderSidebar(tab) {
    const panel = ensureSidebarPanel();
    let html = '';

    if (tab === 'library') {
      html = `
        <div class="study-sidebar-search-wrap">
          <i data-lucide="search" class="icon-lucide"></i>
          <input class="study-sidebar-search" placeholder="Search library..." id="studyLibSearch">
        </div>
        <div class="study-sidebar-section">
          <div class="study-sidebar-label">Smart filters</div>
          <button class="study-sidebar-item active"><i data-lucide="layers" class="icon-lucide"></i> All documents <span class="sidebar-count">0</span></button>
          <button class="study-sidebar-item"><i data-lucide="clock" class="icon-lucide"></i> In progress <span class="sidebar-count">0</span></button>
          <button class="study-sidebar-item"><i data-lucide="bookmark" class="icon-lucide"></i> Bookmarked <span class="sidebar-count">0</span></button>
          <button class="study-sidebar-item"><i data-lucide="highlighter" class="icon-lucide"></i> Highlights <span class="sidebar-count">0</span></button>
        </div>
        <div class="study-sidebar-section">
          <div class="study-sidebar-label">Folders</div>
          <button class="study-sidebar-item" data-study-action="new-folder"><i data-lucide="folder-plus" class="icon-lucide"></i> New folder</button>
        </div>`;
    } else if (tab === 'rooms') {
      html = `
        <div class="study-sidebar-section">
          <div class="study-sidebar-label">Your rooms</div>
          <button class="study-sidebar-item active"><i data-lucide="users" class="icon-lucide"></i> Active now <span class="sidebar-count">0</span></button>
          <button class="study-sidebar-item"><i data-lucide="history" class="icon-lucide"></i> Recent <span class="sidebar-count">0</span></button>
          <button class="study-sidebar-item"><i data-lucide="star" class="icon-lucide"></i> Favorites <span class="sidebar-count">0</span></button>
        </div>
        <div class="study-sidebar-section">
          <div class="study-sidebar-label">Quick actions</div>
          <button class="study-sidebar-item" data-study-action="create-room"><i data-lucide="plus-circle" class="icon-lucide"></i> New room</button>
          <button class="study-sidebar-item" data-study-action="join-room"><i data-lucide="log-in" class="icon-lucide"></i> Join by code</button>
        </div>`;
    } else {
      html = `
        <div class="study-sidebar-section">
          <div class="study-sidebar-label">Academics</div>
          <button class="study-sidebar-item active"><i data-lucide="graduation-cap" class="icon-lucide"></i> Courses</button>
          <button class="study-sidebar-item"><i data-lucide="clipboard-list" class="icon-lucide"></i> Assignments</button>
          <button class="study-sidebar-item"><i data-lucide="calendar" class="icon-lucide"></i> Schedule</button>
          <button class="study-sidebar-item"><i data-lucide="bar-chart" class="icon-lucide"></i> Grades</button>
        </div>`;
    }

    panel.innerHTML = html;
    if (window.lucide) window.lucide.createIcons();
  }

  // ─── Sidebar show/hide for study mode ───

  function enterStudySidebar() {
    HIDE_IN_STUDY.forEach((cls) => {
      const el = sidebar.querySelector('.' + cls);
      if (el) el.setAttribute('data-study-hidden', '1');
      if (el) el.style.display = 'none';
    });
    const panel = ensureSidebarPanel();
    panel.hidden = false;
  }

  function exitStudySidebar() {
    HIDE_IN_STUDY.forEach((cls) => {
      const el = sidebar.querySelector('.' + cls);
      if (el && el.getAttribute('data-study-hidden') === '1') {
        el.style.display = '';
        el.removeAttribute('data-study-hidden');
      }
    });
    if (sidebarPanel) sidebarPanel.hidden = true;
  }

  // ─── Mode sync ───

  function isStudy() {
    return body.getAttribute('data-mode') === 'study';
  }

  function syncToMode() {
    if (isStudy()) {
      view.classList.remove('hidden');
      enterStudySidebar();
      setTab(activeTab);
    } else {
      view.classList.add('hidden');
      exitStudySidebar();
    }
  }

  // ─── Event wiring ───

  function wireTabs() {
    view.querySelectorAll('.study-tab').forEach((btn) => {
      btn.addEventListener('click', () => setTab(btn.dataset.studyTab));
    });
  }

  function handleContentClick(e) {
    const actionEl = e.target.closest('[data-study-action]');
    if (!actionEl) return;
    const action = actionEl.dataset.studyAction;
    if (action === 'upload') {
      if (window.openLibrary) window.openLibrary();
      else notify('Upload coming soon');
    } else if (action === 'create-room' || action === 'join-room') {
      notify('Rooms are a demo for now 🚧');
    } else if (action === 'new-folder') {
      notify('Folders coming soon');
    }
  }

  function notify(msg) {
    if (window.showToast) return window.showToast(msg);
    // minimal fallback toast
    let t = document.getElementById('studyToast');
    if (!t) {
      t = document.createElement('div');
      t.id = 'studyToast';
      t.style.cssText = 'position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:var(--panel-2);color:var(--text);padding:10px 18px;border-radius:10px;font-size:13px;z-index:9999;box-shadow:0 4px 20px rgba(0,0,0,0.25);transition:opacity .2s';
      document.body.appendChild(t);
    }
    t.textContent = msg;
    t.style.opacity = '1';
    clearTimeout(t._to);
    t._to = setTimeout(() => { t.style.opacity = '0'; }, 1800);
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
  }

  // ─── Init ───

  function init() {
    wireTabs();
    content.addEventListener('click', handleContentClick);
    if (sidebarPanel) sidebarPanel.addEventListener('click', handleContentClick);

    // Watch body[data-mode] so we react to mode.js switches
    const obs = new MutationObserver(() => syncToMode());
    obs.observe(body, { attributes: true, attributeFilter: ['data-mode'] });

    // Initial state
    syncToMode();

    // expose for other modules
    window.studyMode = { setTab, getTab: () => activeTab };
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
