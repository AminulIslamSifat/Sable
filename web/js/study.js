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
  const railStudyBtn = document.getElementById('railStudyBtn');

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

  // ─── Library state (backed by /api/study/*) ───
  let LIBRARY_DOCS = [];
  let libraryFilter = 'all';   // all | progress | bookmarked | highlights
  let libraryQuery = '';
  let groupBy = 'recent';      // recent | none | collection | semester | subject | topic
  let COLLECTIONS = [];        // Drive folders that have imported docs
  let DRIVE_FOLDERS = [];      // Drive links saved in the Drive tab (shown as cards)
  let userChoseGroup = false;  // don't override an explicit group-by choice
  let ACADEMICS = { semesters: [] };
  let collapsedGroups = {};    // group key → true when collapsed
  // Accepted upload formats, fetched from the backend. Falls back to a sane
  // default so the picker works even before /formats responds.
  let FORMATS = { accept: ['.pdf'], converter: true };
  const ROOMS = [];

  // ─── Tab meta ───
  const TABS = ['library', 'rooms', 'drive', 'academics', 'notes'];

  // ─── Rendering ───

  function docsMatchingFilter() {
    const q = libraryQuery.trim().toLowerCase();
    return LIBRARY_DOCS.filter((d) => {
      if (q && !String(d.title || '').toLowerCase().includes(q)) return false;
      if (libraryFilter === 'progress') return (d.progress && d.progress.percent) > 0;
      if (libraryFilter === 'bookmarked') return !!d.bookmarked;
      if (libraryFilter === 'highlights') return (d.highlights || 0) > 0;
      return true;
    });
  }

  const GROUP_LABELS = {
    recent: 'Recent', none: 'None', collection: 'Drive folder',
    semester: 'Semester', session: 'Session', subject: 'Subject', topic: 'Topic',
  };
  // View modes offered in the sidebar, in display order. `recent` is special —
  // it's a flat list sorted by last opened, not a grouping.
  const GROUP_MODES = ['recent', 'none', 'collection', 'semester', 'session', 'subject', 'topic'];
  const GROUP_ICONS = {
    recent: 'clock', none: 'layout-grid', collection: 'folder-tree',
    semester: 'graduation-cap', session: 'calendar', subject: 'book', topic: 'tag',
  };

  function renderLibrary() {
    if (!LIBRARY_DOCS.length && !DRIVE_FOLDERS.length) {
      return `
        <div class="study-empty">
          <div class="study-empty-icon">📚</div>
          <h3>Your library is empty</h3>
          <p>Upload PDFs, Word docs, slides, or notes. Sable extracts the text and you can study right here.</p>
          <button class="study-empty-btn" data-study-action="upload">Upload a document</button>
        </div>`;
    }

    // Recent — every document opened recently, newest first, regardless of
    // where it came from (local upload or Drive).
    if (groupBy === 'recent') return renderRecent();

    const docs = docsMatchingFilter();
    const folders = DRIVE_FOLDERS.map(folderCard).join('');
    if (!docs.length && !folders) {
      return `<div class="study-empty"><div class="study-empty-icon">🔍</div><h3>Nothing matches</h3><p>No documents fit this filter or search.</p></div>`;
    }

    // Flat grid when grouping is off — documents first, then Drive folders.
    if (groupBy === 'none') {
      const docGrid = `<div class="study-library">${docs.map(docCard).join('')}</div>`;
      const folderSection = folders
        ? `<div class="study-folders">
             <div class="study-folders-label">
               <i data-lucide="folder-open" class="icon-lucide"></i> Drive folders
             </div>
             <div class="study-library">${folders}</div>
           </div>`
        : '';
      return `${docGrid}${folderSection}`;
    }

    return renderGrouped(docs);
  }

  /** A Drive folder the user added, shown as a card in the library. */
  function folderCard(s) {
    const n = s.count || 0;
    return `
      <div class="study-doc-card study-folder-card" data-drive-folder="${s.id}" role="button" tabindex="0">
        <div class="study-doc-thumb">
          <i data-lucide="folder-open" class="icon-lucide study-doc-icon"></i>
        </div>
        <div class="study-doc-info">
          <div class="study-doc-title">${escapeHtml(s.name)}</div>
          <div class="study-doc-meta">
            <span>Drive folder</span>
            ${n ? `<span>${n} item${n === 1 ? '' : 's'}</span>` : ''}
          </div>
        </div>
      </div>`;
  }

  /** Documents by most-recently-opened, ignoring where they live. */
  function renderRecent() {
    const recent = LIBRARY_DOCS
      .filter((d) => d.progress && d.progress.last_opened > 0)
      .sort((a, b) => (b.progress.last_opened || 0) - (a.progress.last_opened || 0));
    if (!recent.length) {
      // Fall back to the full library when nothing has been opened yet, so the
      // default view is never a dead-end empty screen for a fresh library.
      if (LIBRARY_DOCS.length) {
        return `<div class="study-library">${LIBRARY_DOCS.map(docCard).join('')}</div>`;
      }
      return `<div class="study-empty"><div class="study-empty-icon">🕘</div>
                <h3>Nothing opened yet</h3>
                <p>Documents you read will show up here, newest first.</p></div>`;
    }
    return `<div class="study-library">${recent.map(docCard).join('')}</div>`;
  }

  /** Build the group → docs map for the active groupBy dimension. */
  function groupDocs(docs) {
    const map = new Map();
    for (const d of docs) {
      // 'collection' groups by the Drive folder a doc was imported from.
      const label = groupBy === 'collection'
        ? ((d.collection || '').trim() || 'Ungrouped')
        : (((d.group || {})[groupBy] || '').trim() || 'Ungrouped');
      if (!map.has(label)) map.set(label, []);
      map.get(label).push(d);
    }
    // Named groups first (alphabetical), "Ungrouped" always last.
    return [...map.entries()].sort((a, b) => {
      if (a[0] === 'Ungrouped') return 1;
      if (b[0] === 'Ungrouped') return -1;
      return a[0].localeCompare(b[0]);
    });
  }

  function renderGrouped(docs) {
    const groups = groupDocs(docs);
    const sections = groups.map(([label, items]) => {
      const key = groupBy + ':' + label;
      const isCollapsed = !!collapsedGroups[key];
      // A Drive collection gets a folder icon so it reads as a source, not a tag.
      const icon = groupBy === 'collection' ? 'folder-open' : 'folder-tree';
      return `
        <div class="study-group${isCollapsed ? ' collapsed' : ''}" data-group-key="${escapeHtml(key)}">
          <button class="study-group-head" data-group-toggle="${escapeHtml(key)}">
            <i data-lucide="chevron-down" class="icon-lucide study-group-caret"></i>
            <i data-lucide="${icon}" class="icon-lucide study-group-icon"></i>
            <span class="study-group-name">${escapeHtml(label)}</span>
            <span class="study-group-count">${items.length}</span>
          </button>
          <div class="study-library study-group-body">${items.map(docCard).join('')}</div>
        </div>`;
    });
    return `<div class="study-groups">${sections.join('')}</div>`;
  }

  // Source-format → display icon + accent label.
  const EXT_ICON = {
    pdf: 'file-text',
    doc: 'file-text', docx: 'file-text',
    ppt: 'presentation', pptx: 'presentation',
    odt: 'file-text', odp: 'presentation', ods: 'table',
    xls: 'table', xlsx: 'table', csv: 'table',
    rtf: 'file-text', txt: 'file-text', md: 'file-code', epub: 'book-open',
  };

  function docCard(d) {
    const pct = (d.progress && d.progress.percent) || 0;
    const g = d.group || {};
    const ext = String(d.source_ext || 'pdf').toLowerCase();
    const icon = EXT_ICON[ext] || 'file-text';
    const tags = [g.semester, g.session, g.subject, g.topic].filter(Boolean);
    const tagHtml = tags.length
      ? `<div class="study-doc-tags">${tags.map((t) => `<span class="study-doc-tag">${escapeHtml(t)}</span>`).join('')}</div>`
      : '';
    // Non-PDF uploads get a small "converted from .docx" style badge.
    const srcBadge = d.converted
      ? `<span class="study-doc-src" title="Converted from .${escapeHtml(ext)} to PDF">${escapeHtml(ext)}</span>`
      : '';
    // Page-1 preview when we have one (cached JPEG); icon as the fallback.
    // A plain <img> can't send an Authorization header, so the thumb request
    // authenticates via ?token= (the endpoint accepts it, see application.py).
    const tok = localStorage.getItem('sable_token') || '';
    const thumb = d.has_thumb
      ? `<img class="study-doc-preview" src="/api/study/doc/${d.id}/thumb${tok ? '?token=' + encodeURIComponent(tok) : ''}" alt=""
              loading="lazy" decoding="async"
              onerror="this.style.display='none';this.nextElementSibling&&(this.nextElementSibling.style.display='')">
         <i data-lucide="${icon}" class="icon-lucide study-doc-icon" style="display:none"></i>`
      : `<i data-lucide="${icon}" class="icon-lucide study-doc-icon"></i>`;
    return `
      <div class="study-doc-card" data-doc-id="${d.id}" role="button" tabindex="0">
        <div class="study-doc-thumb">
          ${thumb}
          ${srcBadge}
          <div class="study-doc-progress" style="width:${pct}%"></div>
        </div>
        <button class="study-doc-menu" data-doc-menu="${d.id}" title="More">⋯</button>
        <div class="study-doc-info">
          <div class="study-doc-title">${escapeHtml(d.title)}</div>
          ${tagHtml}
          <div class="study-doc-meta">
            <span>${d.pages || 0} pages</span>
            <span>${pct}%</span>
          </div>
        </div>
      </div>`;
  }

  // ─── API ───

  async function loadDocs() {
    try {
      const res = await fetch('/api/study/docs');
      if (!res.ok) throw new Error(res.statusText);
      LIBRARY_DOCS = await res.json();
    } catch (err) {
      LIBRARY_DOCS = [];
      notify('Could not load library: ' + err.message);
    }
    await loadCollections();
    await loadDriveFolders();
    await loadAcademics();
    if (activeTab === 'library') {
      content.innerHTML = renderLibrary();
      if (window.lucide) window.lucide.createIcons();
      refreshSidebarCounts();
    }
  }

  async function loadCollections() {
    try {
      const res = await fetch('/api/study/collections');
      if (!res.ok) throw new Error(res.statusText);
      COLLECTIONS = await res.json();
    } catch (err) {
      COLLECTIONS = [];
    }
    return COLLECTIONS;
  }

  /** Drive links the user added — surfaced in the library as folder cards. */
  async function loadDriveFolders() {
    try {
      const res = await fetch('/api/drive/sources');
      if (!res.ok) throw new Error(res.statusText);
      DRIVE_FOLDERS = await res.json();
    } catch (err) {
      DRIVE_FOLDERS = [];
    }
    return DRIVE_FOLDERS;
  }

  async function loadAcademics() {
    try {
      const res = await fetch('/api/study/academics');
      if (!res.ok) throw new Error(res.statusText);
      ACADEMICS = await res.json();
    } catch (err) {
      ACADEMICS = { semesters: [] };
    }
    return ACADEMICS;
  }

  function openDoc(id) {
    if (window.studyReader && typeof window.studyReader.open === 'function') {
      window.studyReader.open(id);
    } else {
      notify('Reader still loading…');
    }
  }

  // ─── Upload (batch + drag & drop + inline grouping) ───

  const TAG_FIELDS = ['semester', 'session', 'subject', 'topic'];
  const FILE_INPUT_ACCEPT = () => (FORMATS.accept || ['.pdf']).join(',');

  /** Validate a file against the backend's accepted formats. Returns error or null. */
  function fileRejection(file) {
    const ext = '.' + (file.name.split('.').pop() || '').toLowerCase();
    if (!(FORMATS.accept || ['.pdf']).includes(ext)) return `unsupported type ${ext}`;
    if (ext !== '.pdf' && FORMATS.converter === false) return 'LibreOffice not installed — PDF only';
    return null;
  }

  // Upload one file, reporting progress via callback. Plain fetch() can't
  // report upload progress, so XHR is the right tool here. Resolves with the
  // doc summary; rejects with an Error carrying the server's detail message.
  function uploadOne(file, tags, onProgress) {
    return new Promise((resolve, reject) => {
      const fd = new FormData();
      fd.append('file', file);
      if (tags) {
        for (const k of TAG_FIELDS) if (tags[k]) fd.append(k, tags[k]);
      }
      const xhr = new XMLHttpRequest();
      xhr.open('POST', '/api/study/upload');
      // XHR bypasses the global fetch() auth wrapper in core.js, so the bearer
      // token must be set by hand. Read the same localStorage key directly.
      const token = localStorage.getItem('sable_token') || '';
      if (token) xhr.setRequestHeader('Authorization', 'Bearer ' + token);
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable && onProgress) onProgress(Math.round((e.loaded / e.total) * 100));
      };
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          let doc = null;
          try { doc = JSON.parse(xhr.responseText); } catch {}
          resolve(doc);
        } else {
          let msg = `Upload failed (${xhr.status})`;
          try { msg = JSON.parse(xhr.responseText).detail || msg; } catch {}
          reject(new Error(msg));
        }
      };
      xhr.onerror = () => reject(new Error('network error'));
      xhr.send(fd);
    });
  }

  // Open the batch upload dialog. `initialFiles` seeds the list (from the file
  // picker or a drop); files can be added/removed before hitting Upload. Tags
  // chosen here are stamped onto every file in the batch.
  function openUploadDialog(initialFiles) {
    let files = (initialFiles || []).filter(Boolean);
    const overlay = document.createElement('div');
    overlay.className = 'study-modal-overlay';
    const p = buildGroupPicker({ group: {} });
    overlay.innerHTML = `
      <div class="study-modal study-upload-modal" role="dialog" aria-modal="true">
        <div class="study-modal-head">
          <h3>Upload documents</h3>
          <button class="study-modal-x" data-modal-close>&times;</button>
        </div>
        <div class="study-dropzone" data-dropzone>
          <i data-lucide="upload-cloud" class="icon-lucide"></i>
          <p><strong>Drop files here</strong> or <button type="button" class="study-dropzone-browse" data-browse>browse</button></p>
          <span class="study-dropzone-hint">${(FORMATS.accept || ['.pdf']).join(' · ')}</span>
        </div>
        <div class="study-upload-list" data-filelist></div>
        <div class="study-upload-fields">
          <label class="study-field">
            <span>Semester / Class</span>
            <select data-field="semester">${p.semesters}</select>
          </label>
          <label class="study-field">
            <span>Session</span>
            <select data-field="session">${p.sessions}</select>
          </label>
          <label class="study-field">
            <span>Subject</span>
            <select data-field="subject"${p.disabledSubject ? ' disabled' : ''}>${p.subjects}</select>
          </label>
          <label class="study-field">
            <span>Topic</span>
            <select data-field="topic"${p.disabledTopic ? ' disabled' : ''}>${p.topics}</select>
          </label>
        </div>
        <div class="study-modal-actions">
          <button class="study-room-action secondary" data-modal-cancel>Cancel</button>
          <button class="study-room-action primary" data-upload-go>Upload</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    if (window.lucide) window.lucide.createIcons();

    const $ = (sel) => overlay.querySelector(sel);
    const listEl = $('[data-filelist]');
    const goBtn = $('[data-upload-go]');
    const close = () => overlay.remove();

    function renderList() {
      listEl.innerHTML = files.map((f, i) => {
        const err = fileRejection(f);
        return `<div class="study-upload-item${err ? ' bad' : ''}" data-idx="${i}">
          <i data-lucide="file-text" class="icon-lucide"></i>
          <span class="study-upload-name">${escapeHtml(f.name)}</span>
          <span class="study-upload-status">${err ? escapeHtml(err) : fmtSize(f.size)}</span>
          <button class="study-upload-remove" data-remove="${i}" title="Remove">&times;</button>
        </div>`;
      }).join('');
      const n = files.filter((f) => !fileRejection(f)).length;
      goBtn.disabled = n === 0;
      goBtn.textContent = n > 1 ? `Upload ${n} files` : 'Upload';
      if (window.lucide) window.lucide.createIcons();
    }

    function addFiles(incoming) {
      for (const f of incoming) {
        if (!files.some((x) => x.name === f.name && x.size === f.size)) files.push(f);
      }
      renderList();
    }

    // Tag cascade — semester drives subjects, subject drives topics.
    const tagVal = (f) => {
      const el = overlay.querySelector(`[data-field="${f}"]`);
      return el && !el.disabled ? el.value : '';
    };
    $('[data-field="semester"]').addEventListener('change', () => {
      rebuildFor(overlay, { semester: tagVal('semester') });
    });
    $('[data-field="subject"]').addEventListener('change', () => {
      rebuildFor(overlay, { semester: tagVal('semester'), subject: tagVal('subject') });
    });

    const browse = document.createElement('input');
    browse.type = 'file';
    browse.multiple = true;
    browse.accept = FILE_INPUT_ACCEPT();
    browse.addEventListener('change', () => { addFiles([...browse.files]); browse.value = ''; });
    $('[data-browse]').onclick = () => browse.click();

    // Dropzone — the modal owns its own drag handling.
    const dz = $('[data-dropzone]');
    ['dragenter', 'dragover'].forEach((ev) => dz.addEventListener(ev, (e) => {
      e.preventDefault(); e.stopPropagation(); dz.classList.add('over');
    }));
    ['dragleave', 'drop'].forEach((ev) => dz.addEventListener(ev, (e) => {
      e.preventDefault(); e.stopPropagation(); dz.classList.remove('over');
    }));
    dz.addEventListener('drop', (e) => {
      const dropped = e.dataTransfer ? [...e.dataTransfer.files] : [];
      if (dropped.length) addFiles(dropped);
    });

    listEl.addEventListener('click', (e) => {
      const rm = e.target.closest('[data-remove]');
      if (!rm) return;
      files.splice(Number(rm.dataset.remove), 1);
      renderList();
    });

    overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
    $('[data-modal-close]').onclick = close;
    $('[data-modal-cancel]').onclick = close;

    goBtn.onclick = async () => {
      // Resolve any "+ New …" picks to real names before uploading.
      const tags = {};
      for (const f of TAG_FIELDS) {
        let v = tagVal(f);
        if (v === '__new__') {
          v = (window.prompt(`New ${f} name:`) || '').trim();
          if (!v) return; // cancelled — keep the dialog open
        }
        tags[f] = v;
      }
      if (!tags.semester) { tags.subject = ''; tags.topic = ''; }
      if (!tags.subject) tags.topic = '';

      goBtn.disabled = true;
      const queue = files.filter((f) => !fileRejection(f));
      let ok = 0;
      for (let i = 0; i < queue.length; i++) {
        const file = queue[i];
        const row = listEl.querySelector(`[data-idx="${files.indexOf(file)}"] .study-upload-status`);
        if (row) row.textContent = 'uploading…';
        try {
          await uploadOne(file, tags, (pct) => { if (row) row.textContent = pct + '%'; });
          if (row) row.textContent = 'indexed ✓';
          ok++;
        } catch (err) {
          if (row) row.textContent = err.message;
        }
      }
      await loadDocs();
      close();
      notify(ok === queue.length
        ? `${ok} file${ok === 1 ? '' : 's'} indexed ✓`
        : `${ok}/${queue.length} uploaded — check the rest`);
    };

    renderList();
  }

  function pickAndUpload() {
    const input = document.createElement('input');
    input.type = 'file';
    input.multiple = true;
    // Accept everything the backend allows — PDFs plus convertible office docs.
    input.accept = FILE_INPUT_ACCEPT();
    input.addEventListener('change', () => {
      if (input.files && input.files.length) openUploadDialog([...input.files]);
    });
    input.click();
  }

  function fmtSize(n) {
    if (!n) return '0 B';
    const u = ['B', 'KB', 'MB', 'GB'];
    const i = Math.min(u.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
    return (n / Math.pow(1024, i)).toFixed(i ? 1 : 0) + ' ' + u[i];
  }

  async function loadFormats() {
    try {
      const res = await fetch('/api/study/formats');
      if (!res.ok) return;
      FORMATS = await res.json();
    } catch { /* keep defaults */ }
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

  const RENDERERS = {
    library: renderLibrary,
    rooms: renderRooms,
    drive: () => (window.driveMode ? window.driveMode.render() : ''),
    academics: renderAcademics,
    notes: () => (window.studyNotes ? window.studyNotes.renderTab() : ''),
  };

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

    // Notes loads its own data the first time the tab is opened.
    if (tab === 'notes' && window.studyNotes && !window.studyNotes.isLoaded()) {
      window.studyNotes.reload().then(() => {
        if (activeTab === 'notes') {
          content.innerHTML = window.studyNotes.renderTab();
          renderSidebar('notes');
          if (window.lucide) window.lucide.createIcons();
        }
      });
    }
  }

  /** Repaint the active tab + its sidebar without changing tabs. */
  function refreshTab() {
    // An editor mounted in the content pane owns it right now — repainting
    // would destroy it (and the sidebar search would lose focus mid-typing).
    if (content.querySelector('#snEditorOverlay')) return;
    content.innerHTML = (RENDERERS[activeTab] || (() => ''))();
    if (window.lucide) window.lucide.createIcons();
    renderSidebar(activeTab);
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
      const nAll = LIBRARY_DOCS.length;
      const nProg = LIBRARY_DOCS.filter((d) => (d.progress && d.progress.percent) > 0).length;
      const nMark = LIBRARY_DOCS.filter((d) => d.bookmarked).length;
      const nHigh = LIBRARY_DOCS.filter((d) => (d.highlights || 0) > 0).length;
      const sel = (f) => (libraryFilter === f ? ' active' : '');
      html = `
        <div class="study-sidebar-search-wrap">
          <i data-lucide="search" class="icon-lucide"></i>
          <input class="study-sidebar-search" placeholder="Search library..." id="studyLibSearch" value="${escapeHtml(libraryQuery)}">
        </div>
        <div class="study-sidebar-section">
          <div class="study-sidebar-label">Smart filters</div>
          <button class="study-sidebar-item${sel('all')}" data-lib-filter="all"><i data-lucide="layers" class="icon-lucide"></i> All documents <span class="sidebar-count">${nAll}</span></button>
          <button class="study-sidebar-item${sel('progress')}" data-lib-filter="progress"><i data-lucide="clock" class="icon-lucide"></i> In progress <span class="sidebar-count">${nProg}</span></button>
          <button class="study-sidebar-item${sel('bookmarked')}" data-lib-filter="bookmarked"><i data-lucide="bookmark" class="icon-lucide"></i> Bookmarked <span class="sidebar-count">${nMark}</span></button>
          <button class="study-sidebar-item${sel('highlights')}" data-lib-filter="highlights"><i data-lucide="highlighter" class="icon-lucide"></i> Highlights <span class="sidebar-count">${nHigh}</span></button>
        </div>
        <div class="study-sidebar-section">
          <div class="study-sidebar-label">Group by</div>
          ${GROUP_MODES.map((mode) => {
            const n = mode === 'collection'
              ? COLLECTIONS.length
              : (mode === 'none' ? null : new Set(
                  LIBRARY_DOCS.map((d) => ((d.group || {})[mode] || '').trim()).filter(Boolean)
                ).size);
            const badge = n ? `<span class="sidebar-count">${n}</span>` : '';
            return `<button class="study-sidebar-item${groupBy === mode ? ' active' : ''}" data-group-by="${mode}">
                      <i data-lucide="${GROUP_ICONS[mode]}" class="icon-lucide"></i> ${GROUP_LABELS[mode]} ${badge}
                    </button>`;
          }).join('')}
        </div>
        <div class="study-sidebar-section">
          <div class="study-sidebar-label">Actions</div>
          <button class="study-sidebar-item" data-study-action="upload"><i data-lucide="upload" class="icon-lucide"></i> Upload documents</button>
          <button class="study-sidebar-item" data-study-action="refresh-library"><i data-lucide="refresh-cw" class="icon-lucide"></i> Refresh</button>
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
    } else if (tab === 'drive') {
      html = window.driveMode ? window.driveMode.renderSidebar() : '';
    } else if (tab === 'notes') {
      html = window.studyNotes ? window.studyNotes.renderSidebar() : '';
    } else {
      const sems = ACADEMICS.semesters || [];
      const nSubj = sems.reduce((n, s) => n + (s.subjects || []).length, 0);
      const nTopic = sems.reduce((n, s) => n + (s.subjects || []).reduce((m, x) => m + (x.topics || []).length, 0), 0);
      html = `
        <div class="study-sidebar-section">
          <div class="study-sidebar-label">Overview</div>
          <button class="study-sidebar-item active"><i data-lucide="graduation-cap" class="icon-lucide"></i> Semesters <span class="sidebar-count">${sems.length}</span></button>
          <button class="study-sidebar-item"><i data-lucide="calendar" class="icon-lucide"></i> Sessions <span class="sidebar-count">${(ACADEMICS.sessions || []).length}</span></button>
          <button class="study-sidebar-item"><i data-lucide="book" class="icon-lucide"></i> Subjects <span class="sidebar-count">${nSubj}</span></button>
          <button class="study-sidebar-item"><i data-lucide="tag" class="icon-lucide"></i> Topics <span class="sidebar-count">${nTopic}</span></button>
        </div>
        <div class="study-sidebar-section">
          <div class="study-sidebar-label">Actions</div>
          <button class="study-sidebar-item" data-acad="add-semester"><i data-lucide="plus-circle" class="icon-lucide"></i> Add semester</button>
          <button class="study-sidebar-item" data-study-action="refresh-library"><i data-lucide="refresh-cw" class="icon-lucide"></i> Refresh</button>
        </div>`;
    }

    panel.innerHTML = html;
    if (window.lucide) window.lucide.createIcons();

    if (tab === 'notes' && window.studyNotes) {
      window.studyNotes.bindSidebarSearch(panel);
    }

    // Wire the library search box (rebuilt on every render, so re-bind each time).
    const searchInput = panel.querySelector('#studyLibSearch');
    if (searchInput) {
      searchInput.addEventListener('input', debounce(() => {
        libraryQuery = searchInput.value;
        content.innerHTML = renderLibrary();
        if (window.lucide) window.lucide.createIcons();
      }, 150));
    }
  }

  function refreshSidebarCounts() {
    if (activeTab === 'library') renderSidebar('library');
  }

  function debounce(fn, ms) {
    let t;
    return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
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
      activeTab = 'library';
      setTab(activeTab);
      showStudyRail();
    } else {
      view.classList.add('hidden');
      exitStudySidebar();
      hideStudyRail();
    }
  }

  // ─── Study rail button (visible only in study mode) ───

  function showStudyRail() {
    if (railStudyBtn) railStudyBtn.style.display = '';
    const rail = document.getElementById('activityRail');
    if (!rail) return;
    rail.querySelectorAll('.rail-btn').forEach((b) => b.classList.remove('active'));
    if (railStudyBtn) railStudyBtn.classList.add('active');
  }

  function hideStudyRail() {
    if (railStudyBtn) railStudyBtn.style.display = 'none';
  }

  // ─── Rail switching inside study mode ───
  // Selecting another rail view yields the stage to it (hides the study UI);
  // the study rail button brings the study UI back.

  function openStudyStage() {
    view.classList.remove('hidden');
    enterStudySidebar();
    activeTab = 'library';
    setTab(activeTab);
    // Ensure the study rail button is the active one after any switch
    const rail = document.getElementById('activityRail');
    if (rail && railStudyBtn) {
      rail.querySelectorAll('.rail-btn').forEach((b) => b.classList.remove('active'));
      railStudyBtn.classList.add('active');
    }
  }

  function wireRailSwitch() {
    // Dedicated click handler: the study button ALWAYS brings the stage back,
    // even when mode.js reports it as a toggle-off (target: null).
    if (railStudyBtn) {
      railStudyBtn.addEventListener('click', () => {
        if (isStudy()) openStudyStage();
      });
    }

    window.addEventListener('rail-switch', (e) => {
      if (!isStudy()) return;
      const target = e.detail && e.detail.target;
      if (target == null) return; // deselect of some active button — leave stage as-is
      if (target === 'study') {
        openStudyStage();
      } else {
        view.classList.add('hidden');
        exitStudySidebar();
      }
    });
  }

  // ─── Event wiring ───

  function wireTabs() {
    view.querySelectorAll('.study-tab').forEach((btn) => {
      btn.addEventListener('click', () => setTab(btn.dataset.studyTab));
    });
  }

  function handleContentClick(e) {
    // A Drive folder card in the library opens that source in the Drive tab.
    const folderEl = e.target.closest('[data-drive-folder]');
    if (folderEl) {
      if (window.driveMode && window.driveMode.openFolder) {
        window.driveMode.openFolder(folderEl.dataset.driveFolder);
      }
      return;
    }

    // Drive tab owns its own actions — delegate before anything else.
    const driveEl = e.target.closest('[data-drive-action]');
    if (driveEl && window.driveMode) {
      e.preventDefault();
      window.driveMode.handleAction(driveEl.dataset.driveAction, driveEl);
      return;
    }

    // Doc "⋯" menu first — its stopImmediatePropagation must beat the card
    // handler, otherwise opening the menu also opens the doc.
    const menuEl = e.target.closest('[data-doc-menu]');
    if (menuEl) {
      e.stopPropagation();
      e.stopImmediatePropagation();
      openDocMenu(menuEl.dataset.docMenu);
      return;
    }

    // Library filter buttons live in the sidebar, not the content pane.
    const filterEl = e.target.closest('[data-lib-filter]');
    if (filterEl) {
      libraryFilter = filterEl.dataset.libFilter;
      renderSidebar('library');
      content.innerHTML = renderLibrary();
      if (window.lucide) window.lucide.createIcons();
      return;
    }

    // "Group by" selector (sidebar)
    const groupEl = e.target.closest('[data-group-by]');
    if (groupEl) {
      groupBy = groupEl.dataset.groupBy;
      userChoseGroup = true;
      renderSidebar('library');
      content.innerHTML = renderLibrary();
      if (window.lucide) window.lucide.createIcons();
      return;
    }

    // Collapse/expand a rendered group (content pane)
    const toggleEl = e.target.closest('[data-group-toggle]');
    if (toggleEl) {
      const key = toggleEl.dataset.groupToggle;
      collapsedGroups[key] = !collapsedGroups[key];
      const wrap = toggleEl.closest('.study-group');
      if (wrap) wrap.classList.toggle('collapsed', !!collapsedGroups[key]);
      return;
    }

    // Notes tab — sidebar filters (which share the study-sidebar-item class)
    // and content-pane actions all route through studyNotes.
    const snFilterEl = e.target.closest('[data-sn-filter]');
    if (snFilterEl && window.studyNotes) {
      window.studyNotes.sidebarAction(snFilterEl.dataset.snFilter);
      return;
    }

    // The Notes tab edits IN PLACE — same as the reader's Notes pane — rather
    // than throwing up a modal. `mount` renders the editor into the study
    // content area and swaps back to the grid when it closes.
    const snMount = { mount: content };

    const snEl = e.target.closest('[data-sn]');
    if (snEl && window.studyNotes) {
      const act = snEl.dataset.sn;
      if (act === 'new') { window.studyNotes.openEditor(null, snMount); return; }
      if (window.studyNotes.sidebarAction(act)) return;
    }

    // Delete must be tested BEFORE the card, since the button lives inside
    // the card and would otherwise match [data-sn-open] first.
    const snDelEl = e.target.closest('[data-sn-del]');
    if (snDelEl && window.studyNotes) {
      e.stopPropagation();
      window.studyNotes.deleteById(snDelEl.dataset.snDel);
      return;
    }

    const snOpenEl = e.target.closest('[data-sn-open]');
    if (snOpenEl && window.studyNotes) {
      window.studyNotes.openEditor(snOpenEl.dataset.snOpen, snMount);
      return;
    }

    // Academics manager buttons (content pane)
    const acadEl = e.target.closest('[data-acad]');
    if (acadEl) {
      handleAcadAction(acadEl.dataset.acad, acadEl.dataset.id);
      return;
    }

    const actionEl = e.target.closest('[data-study-action]');
    if (actionEl) {
      const action = actionEl.dataset.studyAction;
      if (action === 'upload') {
        pickAndUpload();
      } else if (action === 'refresh-library') {
        loadDocs().then(() => notify('Library refreshed'));
      } else if (action === 'create-room' || action === 'join-room') {
        notify('Rooms are a demo for now 🚧');
      } else if (action === 'new-folder') {
        notify('Folders coming soon');
      }
      return;
    }

    // Nothing above matched — treat it as a doc-card open (last resort, so
    // buttons inside a card don't accidentally open it).
    const card = e.target.closest('.study-doc-card');
    if (card) openDoc(card.dataset.docId);
  }

  // ─── Academics CRUD wiring ───

  async function apiSend(url, method, body) {
    const res = await fetch(url, {
      method,
      headers: body ? { 'Content-Type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch {}
      throw new Error(detail);
    }
    return res.status === 204 ? null : res.json().catch(() => null);
  }

  async function refreshAcademicsView() {
    await loadAcademics();
    if (activeTab === 'academics') {
      content.innerHTML = renderAcademics();
      if (window.lucide) window.lucide.createIcons();
    }
  }

  async function handleAcadAction(action, id) {
    try {
      if (action === 'add-semester') {
        const name = (window.prompt('Semester / class name (e.g. "5th Semester"):') || '').trim();
        if (!name) return;
        await apiSend('/api/study/academics/semesters', 'POST', { name });

      } else if (action === 'add-session') {
        const name = (window.prompt('Session name (e.g. "2019-2020"):') || '').trim();
        if (!name) return;
        await apiSend('/api/study/academics/sessions', 'POST', { name });

      } else if (action === 'del-session') {
        if (!confirm('Delete this session? Documents in it will become ungrouped.')) return;
        await apiSend(`/api/study/academics/sessions/${id}`, 'DELETE');

      } else if (action === 'del-semester') {
        if (!confirm('Delete this semester? Documents in it will become ungrouped.')) return;
        await apiSend(`/api/study/academics/semesters/${id}`, 'DELETE');

      } else if (action === 'add-subject') {
        const name = (window.prompt('Subject name (e.g. "Data Structures"):') || '').trim();
        if (!name) return;
        await apiSend(`/api/study/academics/semesters/${id}/subjects`, 'POST', { name });

      } else if (action === 'del-subject') {
        if (!confirm('Delete this subject? Its topics go too, and docs become ungrouped.')) return;
        await apiSend(`/api/study/academics/subjects/${id}`, 'DELETE');

      } else if (action === 'add-topic') {
        const name = (window.prompt('Topic name (e.g. "Trees & Graphs"):') || '').trim();
        if (!name) return;
        await apiSend(`/api/study/academics/subjects/${id}/topics`, 'POST', { name });

      } else if (action === 'del-topic') {
        if (!confirm('Delete this topic?')) return;
        await apiSend(`/api/study/academics/topics/${id}`, 'DELETE');

      } else {
        return;
      }
      await loadDocs();          // doc tags may have been retagged
      await refreshAcademicsView();
      notify('Updated ✓');
    } catch (err) {
      notify(err.message || 'Action failed');
    }
  }


  // ─── Doc "⋯" menu → academic grouping modal ───

  function findDoc(id) {
    return LIBRARY_DOCS.find((d) => d.id === id) || null;
  }

  /** Cascade options from the academics tree for the three assignment selects. */
  function buildGroupPicker(doc) {
    const g = (doc && doc.group) || {};
    const semesters = ACADEMICS.semesters || [];

    const sem = semesters.find((s) => s.name === g.semester);
    const subjects = sem ? (sem.subjects || []) : [];
    const subj = subjects.find((s) => s.name === g.subject);
    const topics = subj ? (subj.topics || []) : [];

    const opt = (v, sel) => `<option value="${escapeHtml(v)}"${sel ? ' selected' : ''}>${escapeHtml(v)}</option>`;
    const blank = '<option value="">— none —</option>';

    return {
      semesters: blank + semesters.map((s) => opt(s.name, s.name === g.semester)).join('') +
        '<option value="__new__">+ New semester…</option>',
      sessions: blank + (ACADEMICS.sessions || []).map((s) => opt(s.name, s.name === g.session)).join('') +
        '<option value="__new__">+ New session…</option>',
      subjects: blank + subjects.map((s) => opt(s.name, s.name === g.subject)).join('') +
        (g.semester ? '<option value="__new__">+ New subject…</option>' : ''),
      topics: blank + topics.map((t) => opt(t.name, t.name === g.topic)).join('') +
        (g.subject ? '<option value="__new__">+ New topic…</option>' : ''),
      disabledSubject: !g.semester,
      disabledTopic: !g.subject,
    };
  }

  function openDocMenu(docId) {
    const doc = findDoc(docId);
    if (!doc) return;
    const p = buildGroupPicker(doc);

    const overlay = document.createElement('div');
    overlay.className = 'study-modal-overlay';
    overlay.innerHTML = `
      <div class="study-modal" role="dialog" aria-modal="true">
        <div class="study-modal-head">
          <h3>Group document</h3>
          <button class="study-modal-x" data-modal-close>&times;</button>
        </div>
        <p class="study-modal-doc">${escapeHtml(doc.title)}</p>
        <label class="study-field">
          <span>Semester / Class</span>
          <select data-field="semester">${p.semesters}</select>
        </label>
        <label class="study-field">
          <span>Session</span>
          <select data-field="session">${p.sessions}</select>
        </label>
        <label class="study-field">
          <span>Subject</span>
          <select data-field="subject"${p.disabledSubject ? ' disabled' : ''}>${p.subjects}</select>
        </label>
        <label class="study-field">
          <span>Topic</span>
          <select data-field="topic"${p.disabledTopic ? ' disabled' : ''}>${p.topics}</select>
        </label>
        <div class="study-modal-actions">
          <button class="study-room-action danger" data-modal-remove>Remove</button>
          <span class="study-modal-spacer"></span>
          <button class="study-room-action secondary" data-modal-cancel>Cancel</button>
          <button class="study-room-action primary" data-modal-save>Save</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);

    const $ = (sel) => overlay.querySelector(sel);
    const close = () => overlay.remove();
    overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
    $('[data-modal-close]').onclick = close;
    $('[data-modal-cancel]').onclick = close;

    // Cascade: changing semester rebuilds subject options, etc.
    $('[data-field="semester"]').addEventListener('change', (e) => {
      if (e.target.value === '__new__') return; // handled on save
      rebuildFor(overlay, { semester: e.target.value, subject: '', topic: '' });
    });
    $('[data-field="subject"]').addEventListener('change', (e) => {
      if (e.target.value === '__new__') return;
      const sem = $('[data-field="semester"]').value;
      rebuildFor(overlay, { semester: sem, subject: e.target.value, topic: '' });
    });

    $('[data-modal-save]').onclick = () => saveGroup(overlay, doc, close);
    $('[data-modal-remove]').onclick = () => removeDoc(doc, close);
  }

  /**
   * Permanently delete a document (PDF + meta + page text + highlights/notes).
   * The server owns the cleanup; the client only drops the card and re-renders.
   */
  async function removeDoc(doc, close) {
    const label = doc && doc.title ? `"${doc.title}"` : 'this document';
    if (!confirm(`Remove ${label}? Its highlights, notes and progress are deleted too.`)) return;
    try {
      await apiSend(`/api/study/doc/${doc.id}`, 'DELETE');
      if (typeof close === 'function') close();
      LIBRARY_DOCS = LIBRARY_DOCS.filter((d) => d.id !== doc.id);
      if (activeTab === 'library') {
        content.innerHTML = renderLibrary();
        if (window.lucide) window.lucide.createIcons();
      }
      refreshSidebarCounts();
      notify('Document removed');
    } catch (err) {
      notify(err.message || 'Could not remove document');
    }
  }

  /** Re-render the modal's selects against a partial group selection. */
  function rebuildFor(overlay, partial) {
    const fake = { group: partial };
    const p = buildGroupPicker(fake);
    const sem = overlay.querySelector('[data-field="semester"]');
    const subj = overlay.querySelector('[data-field="subject"]');
    const top = overlay.querySelector('[data-field="topic"]');
    sem.innerHTML = p.semesters;
    subj.innerHTML = p.subjects;
    subj.disabled = p.disabledSubject;
    top.innerHTML = p.topics;
    top.disabled = p.disabledTopic;
  }

  async function saveGroup(overlay, doc, close) {
    const val = (f) => {
      const el = overlay.querySelector(`[data-field="${f}"]`);
      if (!el || el.disabled) return '';
      let v = el.value;
      if (v === '__new__') {
        v = (window.prompt(`New ${f} name:`) || '').trim();
        if (!v) return '';
      }
      return v;
    };
    const group = {
      semester: val('semester'), session: val('session'),
      subject: val('subject'), topic: val('topic'),
    };

    try {
      const res = await fetch(`/api/study/doc/${doc.id}/group`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(group),
      });
      if (!res.ok) throw new Error(res.statusText);
      doc.group = await res.json();
      close();
      await loadAcademics();
      content.innerHTML = renderLibrary();
      if (window.lucide) window.lucide.createIcons();
      notify('Grouping updated ✓');
    } catch (err) {
      notify('Could not save grouping: ' + err.message);
    }
  }

  // ─── Academics manager ───

  function renderAcademics() {
    const sems = ACADEMICS.semesters || [];
    const countDocs = (pred) => LIBRARY_DOCS.filter((d) => pred(d.group || {})).length;

    const sessions = ACADEMICS.sessions || [];
    const sessionDocs = (name) => LIBRARY_DOCS.filter((d) => (d.group || {}).session === name).length;
    const sessionsCard = `
      <div class="study-acad-sessions">
        <div class="study-acad-row top">
          <span class="study-acad-name"><i data-lucide="calendar" class="icon-lucide"></i> Sessions</span>
          <button class="study-acad-mini" data-acad="add-session" title="Add session"><i data-lucide="plus" class="icon-lucide"></i></button>
        </div>
        <div class="study-acad-chips">
          ${sessions.length
            ? sessions.map((s) => `
                <span class="study-acad-chip">
                  ${escapeHtml(s.name)}
                  <span class="study-acad-chip-count">${sessionDocs(s.name)}</span>
                  <button class="study-acad-chip-del" data-acad="del-session" data-id="${s.id}" title="Delete session">&times;</button>
                </span>`).join('')
            : '<div class="study-acad-empty">No sessions yet</div>'}
        </div>
      </div>`;

    const header = `
      <div class="study-acad-head">
        <div>
          <h3>Academics</h3>
          <p>Organize your library into semesters, sessions, subjects, and topics.</p>
        </div>
        <button class="study-room-action primary" data-acad="add-semester">
          <i data-lucide="plus" class="icon-lucide"></i> Add semester
        </button>
      </div>`;

    if (!sems.length) {
      return `<div class="study-academics-manager">${header}
        <div class="study-empty">
          <div class="study-empty-icon">🎓</div>
          <h3>No semesters yet</h3>
          <p>Add your first semester or class, then nest subjects and topics inside it.</p>
        </div>
      </div>`;
    }

    const tree = sems.map((sem) => {
      const semDocs = countDocs((g) => g.semester === sem.name);
      const subjects = (sem.subjects || []).map((subj) => {
        const subjDocs = countDocs((g) => g.subject === subj.name);
        const topics = (subj.topics || []).map((t) => {
          const tDocs = countDocs((g) => g.topic === t.name);
          return `
            <div class="study-acad-topic">
              <span class="study-acad-name">${escapeHtml(t.name)}</span>
              <span class="study-acad-count">${tDocs}</span>
              <button class="study-acad-del" data-acad="del-topic" data-id="${t.id}" title="Delete topic">&times;</button>
            </div>`;
        }).join('');
        return `
          <div class="study-acad-subject">
            <div class="study-acad-row">
              <span class="study-acad-name"><i data-lucide="book" class="icon-lucide"></i> ${escapeHtml(subj.name)}</span>
              <span class="study-acad-count">${subjDocs} docs</span>
              <button class="study-acad-mini" data-acad="add-topic" data-id="${subj.id}" title="Add topic"><i data-lucide="plus" class="icon-lucide"></i></button>
              <button class="study-acad-del" data-acad="del-subject" data-id="${subj.id}" title="Delete subject">&times;</button>
            </div>
            ${topics ? `<div class="study-acad-topics">${topics}</div>` : ''}
          </div>`;
      }).join('');

      return `
        <div class="study-acad-semester">
          <div class="study-acad-row top">
            <span class="study-acad-name"><i data-lucide="graduation-cap" class="icon-lucide"></i> ${escapeHtml(sem.name)}</span>
            <span class="study-acad-count">${semDocs} docs</span>
            <button class="study-acad-mini" data-acad="add-subject" data-id="${sem.id}" title="Add subject"><i data-lucide="plus" class="icon-lucide"></i></button>
            <button class="study-acad-del" data-acad="del-semester" data-id="${sem.id}" title="Delete semester">&times;</button>
          </div>
          <div class="study-acad-subjects">${subjects || '<div class="study-acad-empty">No subjects yet</div>'}</div>
        </div>`;
    }).join('');

    return `<div class="study-academics-manager">${header}${sessionsCard}<div class="study-acad-tree">${tree}</div></div>`;
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
    wireRailSwitch();
    content.addEventListener('click', handleContentClick);
    content.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter' && e.key !== ' ') return;
      const folder = e.target.closest('[data-drive-folder]');
      if (folder) {
        e.preventDefault();
        if (window.driveMode && window.driveMode.openFolder) {
          window.driveMode.openFolder(folder.dataset.driveFolder);
        }
        return;
      }
      const card = e.target.closest('.study-doc-card');
      if (!card) return;
      e.preventDefault();
      openDoc(card.dataset.docId);
    });

    // The sidebar panel is created lazily, so bind via delegation on .sidebar
    // instead of on the panel element itself.
    if (sidebar) sidebar.addEventListener('click', handleContentClick);

    // Timer / stopwatch / alarm widget in the study header.
    const timerHost = document.getElementById('studyTimerHost');
    if (timerHost && window.studyTimer) window.studyTimer.mount(timerHost);

    // Upload button in the study header
    const uploadBtn = document.getElementById('studyUploadBtn');
    if (uploadBtn) uploadBtn.addEventListener('click', pickAndUpload);

    // Drop files anywhere on the study view → batch upload dialog. A full-view
    // veil shows while a drag hovers so the affordance is obvious. Counter
    // guards against dragenter/leave firing for child elements.
    const dropVeil = document.createElement('div');
    dropVeil.className = 'study-drop-veil';
    dropVeil.innerHTML = '<div class="study-drop-veil-inner"><i data-lucide="upload-cloud" class="icon-lucide"></i><span>Drop to upload</span></div>';
    view.appendChild(dropVeil);
    let dragDepth = 0;
    const hasFiles = (e) => e.dataTransfer && [...(e.dataTransfer.types || [])].includes('Files');
    view.addEventListener('dragenter', (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      dragDepth++;
      dropVeil.classList.add('show');
    });
    view.addEventListener('dragover', (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'copy';
    });
    view.addEventListener('dragleave', (e) => {
      if (!hasFiles(e)) return;
      dragDepth = Math.max(0, dragDepth - 1);
      if (dragDepth === 0) dropVeil.classList.remove('show');
    });
    view.addEventListener('drop', (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      dragDepth = 0;
      dropVeil.classList.remove('show');
      const files = [...e.dataTransfer.files];
      if (files.length) openUploadDialog(files);
    });

    // Refresh the grid when the reader closes (progress / highlights changed)
    window.addEventListener('study-reader-closed', () => { loadDocs(); });

    // Drive imports add docs — reload the library when one lands.
    window.addEventListener('study-library-changed', () => { loadDocs(); });

    // A Drive link added/removed changes the folder cards in the library.
    window.addEventListener('drive-sources-changed', () => { loadDriveFolders().then(() => loadDocs()); });

    // Watch body[data-mode] so we react to mode.js switches
    const obs = new MutationObserver(() => syncToMode());
    obs.observe(body, { attributes: true, attributeFilter: ['data-mode'] });

    // Initial state
    syncToMode();
    loadFormats();
    loadDocs();

    // expose for other modules
    window.studyMode = {
      setTab,
      getTab: () => activeTab,
      refreshTab,
      // Lets the Drive module re-draw the sidebar once its async status
      // arrives, instead of leaving it stuck on "Checking…".
      refreshSidebar: () => renderSidebar(activeTab),
    };
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
