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
  let libView = 'recent';         // unified library view (see VIEWS below)
  let libraryQuery = '';
  // Full-text override: when set, it holds the last /api/study/search payload
  // for the sidebar query, rendered above the normal library content.
  let libSearchOverride = null;
  let libSearchBusy = false;
  let libSearchSeq = 0;
  let COLLECTIONS = [];        // Drive folders that have imported docs
  let DRIVE_FOLDERS = [];      // Drive links saved in the Drive tab (shown as cards)
  let ACADEMICS = { topics: [], semesters: [] };
  let collapsedGroups = {};    // group key → true when collapsed
  let allDrillTopic = null;    // topic name we've drilled into in "All documents"
  // Accepted upload formats, fetched from the backend. Falls back to a sane
  // default so the picker works even before /formats responds.
  let FORMATS = { accept: ['.pdf'], converter: true };


  // ─── Tab meta ───
  // 'search' and 'stats' moved out: search is the rail search panel
  // (study-aware), stats is a dashboard card. 'academics' was removed too —
  // topics are created and managed from the library's "All documents" view.
  const TABS = ['library', 'indexing', 'rooms', 'drive', 'notes', 'decks', 'humanizer'];

  // ─── Rendering ───

  /** Documents matching the current view's filter + the search query. */
  function docsMatchingFilter() {
    const q = libraryQuery.trim().toLowerCase();
    return LIBRARY_DOCS.filter((d) => {
      if (q && !String(d.title || '').toLowerCase().includes(q)) return false;
      if (libView === 'progress') return (d.progress && d.progress.percent) > 0;
      if (libView === 'bookmarked') return !!d.bookmarked;
      return true;
    });
  }

  // Unified library views — ONE list, one selection each. Every view fully
  // determines how the library renders (filter + grouping combined), so the
  // user never has to set two things to change what they see.
  //   recent     → composite dashboard (recent docs + jump-back cards)
  //   all        → flat grid of every document
  //   progress   → flat grid, docs with reading progress
  //   bookmarked → flat grid, bookmarked docs
  //   topic      → documents grouped by topic (the one parent axis)
  const VIEWS = [
    { id: 'recent',     label: 'Recent',        icon: 'history' },
    { id: 'indexing',   label: 'Indexing',      icon: 'loader' },
    { id: 'all',        label: 'All documents', icon: 'layers' },
    { id: 'progress',   label: 'In progress',   icon: 'clock' },
    { id: 'bookmarked', label: 'Bookmarked',    icon: 'bookmark' },
    { id: 'topic',      label: 'Topic',         icon: 'tag' },
  ];
  const VIEW_LABEL = Object.fromEntries(VIEWS.map((v) => [v.id, v.label]));
  // Views that group by a tag dimension (vs. flat grids). Topic is the only
  // grouping axis now — it is the parent every document must belong to.
  const GROUP_VIEWS = { topic: true };
  const GROUP_ICON = { topic: 'tag' };
  const OTHERS_TOPIC = 'Others';

  function renderLibrary() {
    // The sidebar search drives a full-text override. When it's active it wins
    // over every view (including Recent) and renders above the title-filtered
    // grid, with a close button to drop back to the normal library.
    const override = renderLibSearchOverride();

    if (!LIBRARY_DOCS.length && !DRIVE_FOLDERS.length) {
      return `
        <div class="study-empty">
          <div class="study-empty-icon">📚</div>
          <h3>Your library is empty</h3>
          <p>Upload PDFs, Word docs, slides, or notes. Sable extracts the text and you can study right here.</p>
          <button class="study-empty-btn" data-study-action="upload">Upload a document</button>
        </div>`;
    }

    // Recent — the composite dashboard (recent docs + jump-back cards).
    // Indexing — background job progress for every document.
    if (libView === 'indexing') {
      if (!window.libraryIndex || !window.libraryIndex.panelHtml) {
        return override + '<div class="study-empty"><h3>Indexing UI unavailable</h3></div>';
      }
      const titles = {};
      LIBRARY_DOCS.forEach((d) => { titles[d.id] = d.title; });
      return override + window.libraryIndex.panelHtml(titles);
    }

    if (libView === 'recent') return override + renderRecent();

    const docs = docsMatchingFilter();

    // Search applies across every non-recent view: if a query is active and
    // nothing matches, say so rather than falling back to empty grids.
    if (libraryQuery.trim() && !docs.length) {
      if (override) return override;
      return `<div class="study-empty"><div class="study-empty-icon">🔍</div><h3>Nothing matches</h3><p>No documents fit this search.</p></div>`;
    }

    // "All documents" — a grid of TOPIC FOLDERS first; clicking one drills
    // into that topic and shows its documents. A breadcrumb walks back out.
    if (libView === 'all') {
      if (allDrillTopic) return override + renderAllDrill(docs);
      return override + renderAllFolders(docs);
    }

    // Topic-grouped view — documents filed under their parent topic.
    if (GROUP_VIEWS[libView]) return override + renderGrouped(docs);

    // Flat grid views (progress / bookmarked).
    return override + (docs.length
      ? `<div class="study-library">${docs.map(docCard).join('')}</div>`
      : `<div class="study-empty"><div class="study-empty-icon">📄</div><h3>Nothing here yet</h3><p>No documents in “${escapeHtml(VIEW_LABEL[libView] || libView)}”.</p></div>`);
  }

  /** The full-text override panel for the sidebar search, or ''. */
  function renderLibSearchOverride() {
    const q = libraryQuery.trim();
    if (!q) { libSearchOverride = null; libSearchBusy = false; return ''; }

    if (libSearchBusy && !libSearchOverride) {
      return `<div class="study-search-override" id="studySearchOverride">
        <div class="sso-head">
          <span class="sso-title"><i data-lucide="loader-circle" class="icon-lucide spin"></i> Searching full text…</span>
          <button class="sso-close" data-lib-search="close" title="Clear search"><i data-lucide="x" class="icon-lucide"></i></button>
        </div></div>`;
    }
    if (!libSearchOverride) return '';

    const total = libSearchOverride.total || 0;
    const block = (window.searchPanel && window.searchPanel.renderDocsBlock)
      ? window.searchPanel.renderDocsBlock(libSearchOverride, q)
      : '';
    const body = total
      ? block
      : `<div class="sso-empty">No indexed matches for <strong>${escapeHtml(q)}</strong>.</div>`;

    return `<div class="study-search-override" id="studySearchOverride">
      <div class="sso-head">
        <span class="sso-title"><i data-lucide="book-open" class="icon-lucide"></i> Full-text results <span class="sso-count">${total}</span></span>
        <button class="sso-close" data-lib-search="close" title="Clear search"><i data-lucide="x" class="icon-lucide"></i></button>
      </div>
      <div class="sso-body">${body}</div>
    </div>`;
  }

  /** Distinct topic names across the given docs, "Others" always last. */
  function topicNamesFromDocs(docs) {
    const names = new Set();
    for (const d of docs) {
      const t = (((d.group || {}).topic) || '').trim() || OTHERS_TOPIC;
      names.add(t);
    }
    // Include topics the user created that currently hold no docs, so an empty
    // topic still shows up as a folder in "All documents".
    for (const t of (ACADEMICS.topics || [])) {
      if (t && t.name) names.add(t.name);
    }
    return [...names].sort((a, b) => {
      if (a === OTHERS_TOPIC) return 1;
      if (b === OTHERS_TOPIC) return -1;
      return a.localeCompare(b);
    });
  }

  /** "All documents" root — a card per topic folder. */
  function renderAllFolders(docs) {
    const names = topicNamesFromDocs(docs);
    const docsFor = (topic) => docs.filter((d) => ((((d.group || {}).topic) || '').trim() || OTHERS_TOPIC) === topic);

    // Topic creation lives here now that the Academics tab is gone. This is
    // the only surface that shows topics, so it owns the create action.
    const head = `<div class="study-recent-head" style="justify-content:space-between;">
                  <span class="study-recent-title"><i data-lucide="layers" class="icon-lucide"></i> Topics — tap to open</span>
                  <button class="study-room-action primary" data-acad="add-topic" style="padding:5px 12px;font-size:12px;">
                    <i data-lucide="plus" class="icon-lucide"></i> Add topic
                  </button>
                </div>`;

    if (!names.length) {
      return `${head}<div class="study-empty"><div class="study-empty-icon">📄</div><h3>Nothing here yet</h3><p>Upload a document to get started.</p></div>`;
    }
    const cards = names.map((topic) => {
      const items = docsFor(topic);
      const isOthers = topic === OTHERS_TOPIC;
      // "Others" is the catch-all — it can't be deleted (the server recreates
      // it anyway), so it gets no ⋯ menu.
      const menu = isOthers ? '' : `<button class="study-doc-menu" data-topic-menu="${escapeHtml(topic)}" title="More">⋯</button>`;
      return `
        <div class="study-doc-card study-folder-card" data-topic-open="${escapeHtml(topic)}" role="button" tabindex="0">
          <div class="study-doc-thumb">
            <i data-lucide="${isOthers ? 'archive' : 'tag'}" class="icon-lucide study-doc-icon"></i>
          </div>
          ${menu}
          <div class="study-doc-info">
            <div class="study-doc-title">${escapeHtml(topic)}</div>
            <div class="study-doc-meta">
              <span>${items.length} doc${items.length === 1 ? '' : 's'}</span>
            </div>
          </div>
        </div>`;
    }).join('');

    let html = head;
    html += `<div class="study-library">${cards}</div>`;

    // Drive folder cards stay a source list at the root level of "All".
    if (DRIVE_FOLDERS.length) {
      html += `<div class="study-folders">
                 <div class="study-folders-label"><i data-lucide="folder-open" class="icon-lucide"></i> Drive folders</div>
                 <div class="study-library">${DRIVE_FOLDERS.map(folderCard).join('')}</div>
               </div>`;
    }
    return html;
  }

  /** "All documents" drilled into one topic — breadcrumb + that topic's docs. */
  function renderAllDrill(docs) {
    const topic = allDrillTopic;
    const items = docs.filter((d) => ((((d.group || {}).topic) || '').trim() || OTHERS_TOPIC) === topic);
    // Same ⋯ affordance as the folder card, right-aligned on the breadcrumb.
    const menu = topic === OTHERS_TOPIC
      ? ''
      : `<button class="study-crumb-menu" data-topic-menu="${escapeHtml(topic)}" title="More">⋯</button>`;
    const crumb = `<div class="study-breadcrumb">
        <button class="study-crumb-link" data-topic-back type="button"><i data-lucide="layers" class="icon-lucide"></i> All documents</button>
        <i data-lucide="chevron-right" class="icon-lucide study-crumb-sep"></i>
        <span class="study-crumb-current">${escapeHtml(topic)}</span>
        ${menu}
      </div>`;
    const body = items.length
      ? `<div class="study-library">${items.map(docCard).join('')}</div>`
      : `<div class="study-empty"><div class="study-empty-icon">🗂️</div><h3>No documents</h3><p>Nothing is filed under “${escapeHtml(topic)}” yet.</p></div>`;
    return `${crumb}${body}`;
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

  /**
   * Recent — the composite landing dashboard.
   *
   * Row 1: the 4 most-recently-opened documents.
   * Row 2: "jump back in" cards — last topic and the most recently added
   *        Drive source.
   * This is the default view; it never dead-ends, falling back to the newest
   * uploads when nothing has been opened yet.
   */
  function renderRecent() {
    const opened = LIBRARY_DOCS
      .filter((d) => d.progress && d.progress.last_opened > 0)
      .sort((a, b) => (b.progress.last_opened || 0) - (a.progress.last_opened || 0));
    // Fall back to newest uploads when nothing has been opened yet, so a fresh
    // library still shows something useful on first load.
    const rowDocs = (opened.length ? opened : LIBRARY_DOCS.slice()).slice(0, 4);

    // Most-recent topic, derived from the docs sorted by last_opened (falling
    // back to newest-added) so "last topic" reflects what you actually used.
    const byUse = (opened.length ? opened : LIBRARY_DOCS.slice())
      .slice()
      .sort((a, b) => (b.progress && b.progress.last_opened || 0) - (a.progress && a.progress.last_opened || 0));
    const lastTag = (dim) => {
      for (const d of byUse) {
        const v = (((d.group || {})[dim]) || '').trim();
        if (v) return v;
      }
      return '';
    };

    if (!rowDocs.length && !DRIVE_FOLDERS.length) {
      return `<div class="study-empty"><div class="study-empty-icon">🕘</div>
                <h3>Nothing opened yet</h3>
                <p>Documents you read will show up here, newest first.</p></div>`;
    }

    let html = '';

    // ── Row 1: recent documents ──
    if (rowDocs.length) {
      html += `<div class="study-recent-head">
                 <span class="study-recent-title"><i data-lucide="history" class="icon-lucide"></i> Recently opened</span>
               </div>`;
      html += `<div class="study-library">${rowDocs.map(docCard).join('')}</div>`;
    }

    // ── Row 2: jump-back cards ──
    const jump = [];
    const lastTop = lastTag('topic');
    if (lastTop) jump.push(jumpCard('tag', 'Topic', lastTop, 'topic'));
    if (DRIVE_FOLDERS.length) {
      const last = DRIVE_FOLDERS[0];
      jump.push(`<button class="study-jump-card" data-drive-folder="${escapeHtml(last.id)}" type="button">
                   <span class="study-jump-icon"><i data-lucide="folder-open" class="icon-lucide"></i></span>
                   <span class="study-jump-body">
                     <span class="study-jump-label">Drive</span>
                     <span class="study-jump-value">${escapeHtml(last.name || 'Last source')}</span>
                   </span>
                 </button>`);
    }
    if (jump.length) {
      html += `<div class="study-recent-head" style="margin-top:16px;">
                 <span class="study-recent-title"><i data-lucide="corner-down-right" class="icon-lucide"></i> Jump back in</span>
               </div>`;
      html += `<div class="study-jump-row">${jump.join('')}</div>`;
    }

    return html;
  }

  /** A "jump back in" card that switches the library to a tag view. */
  function jumpCard(icon, label, value, dim) {
    return `<button class="study-jump-card" data-jump-view="${dim}" type="button">
              <span class="study-jump-icon"><i data-lucide="${icon}" class="icon-lucide"></i></span>
              <span class="study-jump-body">
                <span class="study-jump-label">${escapeHtml(label)}</span>
                <span class="study-jump-value">${escapeHtml(value)}</span>
              </span>
            </button>`;
  }

  /** Build the group → docs map for the active grouping view dimension. */
  function groupDocs(docs) {
    const dim = GROUP_VIEWS[libView] ? libView : 'topic';
    const map = new Map();
    for (const d of docs) {
      const label = (((d.group || {})[dim] || '').trim() || OTHERS_TOPIC);
      if (!map.has(label)) map.set(label, []);
      map.get(label).push(d);
    }
    // "Others" (the catch-all bucket) always sorts last; the rest alphabetically.
    return [...map.entries()].sort((a, b) => {
      if (a[0] === OTHERS_TOPIC) return 1;
      if (b[0] === OTHERS_TOPIC) return -1;
      return a[0].localeCompare(b[0]);
    });
  }

  function renderGrouped(docs) {
    const dim = GROUP_VIEWS[libView] ? libView : 'topic';
    const groups = groupDocs(docs);
    const sections = groups.map(([label, items]) => {
      const key = dim + ':' + label;
      const isCollapsed = !!collapsedGroups[key];
      const icon = GROUP_ICON[dim] || 'folder-tree';
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
    // Header with an inline "Add topic" so a new topic can be created without
    // leaving the Topic view.
    const head = `<div class="study-recent-head" style="justify-content:space-between;">
        <span class="study-recent-title"><i data-lucide="tag" class="icon-lucide"></i> Topics</span>
        <button class="study-room-action primary" data-acad="add-topic" style="padding:5px 12px;font-size:12px;">
          <i data-lucide="plus" class="icon-lucide"></i> Add topic
        </button>
      </div>`;
    if (!sections.length) {
      return `${head}<div class="study-empty"><div class="study-empty-icon">🏷️</div><h3>No topics yet</h3><p>Create a topic to start filing documents.</p></div>`;
    }
    return `${head}<div class="study-groups">${sections.join('')}</div>`;
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

  // ─── Topic ⋯ menu ───

  // One menu at a time, positioned against the button that opened it.
  let topicMenuEl = null;

  function closeTopicMenu() {
    if (topicMenuEl) { topicMenuEl.remove(); topicMenuEl = null; }
  }

  /**
   * Small popup anchored to a topic's ⋯ button. Carries the topic NAME — the
   * delete endpoint wants an id, so handleAcadAction resolves it on click.
   */
  function openTopicMenu(anchor, topicName) {
    closeTopicMenu();
    const rect = anchor.getBoundingClientRect();
    const el = document.createElement('div');
    el.className = 'study-topic-menu';
    el.innerHTML = `<button class="study-topic-menu-item danger" data-topic-del="${escapeHtml(topicName)}">
        <i data-lucide="trash-2" class="icon-lucide"></i> Delete topic
      </button>`;
    document.body.appendChild(el);

    // The popup lives on <body>, so content's delegated click handler never
    // sees it — bind directly. Without this the delete button does nothing.
    el.addEventListener('click', (ev) => {
      const del = ev.target.closest('[data-topic-del]');
      if (!del) return;
      ev.stopPropagation();
      const name = del.dataset.topicDel;
      closeTopicMenu();
      handleAcadAction('del-topic', name);
    });

    // Keep it on screen: flip up if there's no room below, clamp horizontally.
    const mh = el.offsetHeight, mw = el.offsetWidth;
    const below = rect.bottom + 6;
    el.style.top = (below + mh > window.innerHeight - 8 ? rect.top - mh - 6 : below) + 'px';
    el.style.left = Math.max(8, Math.min(rect.left, window.innerWidth - mw - 8)) + 'px';

    topicMenuEl = el;
    if (window.lucide) window.lucide.createIcons({ nodes: [el] });

    // Defer so the click that opened this doesn't immediately close it.
    setTimeout(() => {
      const onDown = (ev) => {
        if (el.contains(ev.target)) return;
        closeTopicMenu();
        document.removeEventListener('mousedown', onDown, true);
      };
      document.addEventListener('mousedown', onDown, true);
    }, 0);
  }

  function docCard(d) {
    const pct = (d.progress && d.progress.percent) || 0;
    const g = d.group || {};
    const ext = String(d.source_ext || 'pdf').toLowerCase();
    const icon = EXT_ICON[ext] || 'file-text';
    // Only the topic shows on the card — it is the document's parent now.
    const tags = [g.topic].filter(Boolean);
    const tagHtml = tags.length
      ? `<div class="study-doc-tags">${tags.map((t) => `<span class="study-doc-tag">${escapeHtml(t)}</span>`).join('')}</div>`
      : '';
    // Non-PDF uploads get a small "converted from .docx" style badge.
    const srcBadge = d.converted
      ? `<span class="study-doc-src" title="Converted from .${escapeHtml(ext)} to PDF">${escapeHtml(ext)}</span>`
      : '';
    // Indexing badge (OCR / AI job state) — supplied by library-index.js.
    const idxBadge = (window.libraryIndex && window.libraryIndex.badgeFor)
      ? window.libraryIndex.badgeFor(d.id) : '';
    // Always attempt to load the page-1 preview. If no cached thumbnail exists,
    // the /thumb endpoint generates it on-the-fly and caches it for next time.
    // A plain <img> can't send an Authorization header, so the thumb request
    // authenticates via ?token= (the endpoint accepts it, see application.py).
    const tok = localStorage.getItem('sable_token') || '';
    const thumbSrc = `/api/study/doc/${d.id}/thumb${tok ? '?token=' + encodeURIComponent(tok) : ''}`;
    const thumb = `
      <img class="study-doc-preview" src="${thumbSrc}" alt=""
           loading="lazy" decoding="async"
           onerror="this.style.display='none';var sib=this.nextElementSibling;if(sib){sib.style.display=''}">
      <i data-lucide="${icon}" class="icon-lucide study-doc-icon" style="display:none"></i>`;
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
            ${idxBadge}
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
      ACADEMICS = { topics: [], semesters: [] };
    }
    return ACADEMICS;
  }

  function openDoc(id, page) {
    if (window.studyReader && typeof window.studyReader.open === 'function') {
      // `page` is optional — search hits, SRS cards, and study-time entries all
      // deep-link to the exact page they came from.
      window.studyReader.open(id, page);
    } else {
      notify('Reader still loading…');
    }
  }

  // ─── Upload (batch + drag & drop + inline grouping) ───

  // Only the topic is stamped on upload — it is the document's parent now.
  const TAG_FIELDS = ['topic'];
  const FILE_INPUT_ACCEPT = () => (FORMATS.accept || ['.pdf']).join(',');

  // Mirrors _MAX_PDF_BYTES in server/api/routes/study.py. Enforced here too so a
  // 200 MB file fails instantly instead of after a long upload that the server
  // then rejects with a 413.
  const MAX_UPLOAD_BYTES = 100 * 1024 * 1024;

  // ─── Upload progress panel (pinned to the sidebar floor) ───
  //
  // Uploads are background work. The modal closes the instant the user hits
  // Upload, and this panel becomes the only place progress is reported. It is
  // a SEPARATE element from the contextual sidebar panel on purpose: it
  // repaints on its own poll tick without going through renderSidebar(), so a
  // repaint can never steal focus from the library search box or reset the
  // scroll position.
  const UPLOADS = new Map();   // key -> row
  let uploadsPanel = null;
  let uploadsPoll = null;

  const UPLOAD_PHASE_LABEL = {
    converting: 'Converting',
    extracting: 'Reading pages',
    persisting: 'Saving',
    indexing: 'Indexing',
  };

  // Everything between "bytes are moving" and "settled". 'staged' matters:
  // the server creates the job file before the pipeline task takes its first
  // step, so a poll landing in that window must not be read as "finished" and
  // stop the poll before a single phase is reported.
  const UPLOAD_ACTIVE = new Set(['transferring', 'staged', 'running']);

  function isActiveUpload(row) {
    return !!row && UPLOAD_ACTIVE.has(row.status);
  }

  /** Auth headers for the upload endpoints (XHR/fetch bypass core.js's wrapper). */
  function uploadHeaders(extra) {
    const token = localStorage.getItem('sable_token') || '';
    return Object.assign(token ? { Authorization: 'Bearer ' + token } : {}, extra || {});
  }

  function ensureUploadsPanel() {
    if (uploadsPanel) return uploadsPanel;
    uploadsPanel = document.createElement('div');
    uploadsPanel.className = 'study-uploads';
    uploadsPanel.id = 'studyUploadsPanel';
    uploadsPanel.hidden = true;
    // Appended last + margin-top:auto in CSS pins it to the sidebar floor,
    // below the contextual panel whatever height that ends up taking.
    sidebar.appendChild(uploadsPanel);
    // Cancel is the only interaction, so bind it once via delegation rather
    // than re-binding on every repaint.
    uploadsPanel.addEventListener('click', (e) => {
      const btn = e.target.closest('[data-upload-cancel]');
      if (btn) cancelUpload(btn.dataset.uploadCancel);
    });
    return uploadsPanel;
  }

  function uploadRowHtml(row) {
    const terminal = row.status === 'done' || row.status === 'error' || row.status === 'cancelled';
    const pct = row.status === 'done' ? 100 : Math.max(0, Math.min(100, row.percent || 0));

    let label;
    if (row.status === 'transferring') label = `Uploading ${row.transfer || 0}%`;
    else if (row.status === 'done') label = 'Indexed ✓';
    else if (row.status === 'error') label = row.error || 'Failed';
    else if (row.status === 'cancelled') label = 'Cancelled';
    else if (row.cancelling) label = 'Cancelling…';
    else label = UPLOAD_PHASE_LABEL[row.phase] || 'Queued';

    return `<div class="study-upload-row${terminal ? ' ' + row.status : ''}">
      <div class="study-upload-row-head">
        <span class="study-upload-row-name" title="${escapeHtml(row.name)}">${escapeHtml(row.name)}</span>
        <button class="study-upload-row-x" data-upload-cancel="${escapeHtml(row.key)}" title="Cancel upload">&times;</button>
      </div>
      <div class="li-bar"><div class="li-bar-fill${row.cancelling ? ' stopping' : ''}" style="width:${pct}%"></div></div>
      <div class="study-upload-row-foot">
        <span class="study-upload-row-phase">${escapeHtml(label)}</span>
        <span class="study-upload-row-pct">${pct}%</span>
      </div>
    </div>`;
  }

  let uploadsReapTimer = null;

  /**
   * Drop finished rows, repaint, and make sure the timer that does it keeps
   * running until the panel is empty.
   *
   * This deliberately does NOT rely on the status poll. The poll only runs
   * while something is active, so the LAST upload to finish always stopped the
   * poll — and the reaper lived inside it, so that final row was stranded at
   * the bottom of the sidebar forever.
   */
  function reapUploads() {
    uploadsReapTimer = null;
    const now = Date.now();
    let removed = false;
    for (const [key, row] of UPLOADS) {
      if (row.dropAt && now >= row.dropAt) { UPLOADS.delete(key); removed = true; }
    }
    if (removed) renderUploadsPanel();
    else scheduleReap();
  }

  /** Arm a single reap timer for the soonest pending drop, if any. */
  function scheduleReap() {
    if (uploadsReapTimer) return;
    const times = [...UPLOADS.values()].map((r) => r.dropAt).filter(Boolean);
    if (!times.length) return;
    const wait = Math.max(120, Math.min(...times) - Date.now());
    uploadsReapTimer = setTimeout(reapUploads, wait);
  }

  function renderUploadsPanel() {
    const panel = ensureUploadsPanel();
    const rows = [...UPLOADS.values()];
    if (!rows.length) {
      panel.hidden = true;
      panel.innerHTML = '';
      if (uploadsReapTimer) { clearTimeout(uploadsReapTimer); uploadsReapTimer = null; }
      return;
    }
    // Any row with a drop time needs a timer, whether or not polling is live.
    scheduleReap();
    const active = rows.filter(isActiveUpload).length;
    panel.hidden = false;
    panel.innerHTML = `
      <div class="study-uploads-head">
        <span>Uploads</span>
        ${active ? `<span class="sidebar-count">${active}</span>` : ''}
      </div>
      <div class="study-uploads-list">${rows.map(uploadRowHtml).join('')}</div>`;
    if (window.lucide) window.lucide.createIcons();
  }

  /** Run the poll only while something is genuinely moving. */
  function syncUploadPolling() {
    const active = [...UPLOADS.values()].some(isActiveUpload);
    if (active && !uploadsPoll) {
      uploadsPoll = setInterval(pollUploads, 1200);
    } else if (!active && uploadsPoll) {
      clearInterval(uploadsPoll);
      uploadsPoll = null;
    }
  }

  async function pollUploads() {
    // Nothing tracked: stop the interval, but still let the reaper clean up.
    if (!UPLOADS.size) { syncUploadPolling(); return; }

    let data = null;
    try {
      const res = await fetch('/api/study/upload/status', { headers: uploadHeaders() });
      if (!res.ok) return;
      data = await res.json();
    } catch {
      return;   // a dropped poll is not an error worth surfacing
    }

    const byId = new Map(((data && data.uploads) || []).map((u) => [u.upload_id, u]));
    let changed = false;
    let docsChanged = false;

    for (const row of UPLOADS.values()) {
      // Still transferring — no upload_id on the server yet, so nothing to merge.
      if (!row.uploadId) continue;
      const u = byId.get(row.uploadId);
      if (!u) continue;

      const before = `${row.status}|${row.phase}|${row.percent}|${row.cancelling}`;
      row.status = u.status;
      row.phase = u.phase || '';
      row.percent = u.percent || 0;
      row.error = u.error || '';
      row.cancelling = !!u.cancel_requested;
      if (`${row.status}|${row.phase}|${row.percent}|${row.cancelling}` !== before) changed = true;

      if ((u.status === 'done' || u.status === 'error' || u.status === 'cancelled') && !row.settled) {
        row.settled = true;
        row.dropAt = Date.now() + 4000;   // linger briefly so the outcome is visible
        if (u.status === 'done') docsChanged = true;
        changed = true;
      }
    }

    // Repaint (which also arms the reap timer) and refresh the library once a
    // document actually landed.
    if (changed) { renderUploadsPanel(); syncUploadPolling(); }
    if (docsChanged) {
      loadDocs();
      // The new doc's index state is what the Indexing tab and the card badges
      // read, so pull it rather than waiting for the next tick.
      if (window.libraryIndex && window.libraryIndex.reloadStatuses) {
        window.libraryIndex.reloadStatuses();
      }
    }
  }

  /**
   * Cancel one upload.
   *
   * Two very different cases:
   *  - still transferring → abort the XHR. The server never saw the file, so
   *    there is nothing to clean up.
   *  - already staged      → ask the server to cancel. It stops at its next
   *    phase boundary and deletes whatever the upload produced, so a cancelled
   *    upload leaves no orphan document behind.
   */
  async function cancelUpload(key) {
    const row = UPLOADS.get(key);
    if (!row) return;

    if (row.xhr) {
      row.status = 'cancelled';
      row.dropAt = Date.now() + 2500;
      try { row.xhr.abort(); } catch { /* already done */ }
      renderUploadsPanel();
      syncUploadPolling();
      return;
    }

    if (!row.uploadId) return;
    row.cancelling = true;
    renderUploadsPanel();
    try {
      await fetch('/api/study/upload/cancel', {
        method: 'POST',
        headers: uploadHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({ upload_id: row.uploadId }),
      });
    } catch (err) {
      notify('Could not cancel: ' + (err.message || 'network error'));
    }
    pollUploads();
  }

  /** Register a row and kick off its transfer. */
  function startUploadRow(file, tags) {
    const key = 'u' + Math.random().toString(36).slice(2, 10);
    const row = {
      key, uploadId: null, name: file.name, size: file.size,
      transfer: 0, percent: 0, status: 'transferring', phase: '',
      error: '', cancelling: false, settled: false, xhr: null,
    };
    UPLOADS.set(key, row);
    renderUploadsPanel();
    syncUploadPolling();

    uploadOne(file, tags, row)
      .then((res) => {
        if (!res || !res.upload_id) throw new Error('server did not return an upload id');
        row.uploadId = res.upload_id;
        row.status = 'running';
        row.percent = 0;
        renderUploadsPanel();
        syncUploadPolling();
        pollUploads();
      })
      .catch((err) => {
        row.xhr = null;
        if (err && err.cancelled) {
          row.status = 'cancelled';
        } else {
          row.status = 'error';
          row.error = (err && err.message) || 'upload failed';
          notify(`${file.name}: ${row.error}`);
        }
        row.dropAt = Date.now() + 4000;
        renderUploadsPanel();
        syncUploadPolling();
      });
  }

  /** Validate a file against the backend's accepted formats. Returns error or null. */
  function fileRejection(file) {
    const ext = '.' + (file.name.split('.').pop() || '').toLowerCase();
    if (!(FORMATS.accept || ['.pdf']).includes(ext)) return `unsupported type ${ext}`;
    if (ext !== '.pdf' && FORMATS.converter === false) return 'LibreOffice not installed — PDF only';
    if (file.size > MAX_UPLOAD_BYTES) {
      return `too large (${(file.size / 1048576).toFixed(0)} MB, max 100 MB)`;
    }
    return null;
  }

  // Upload one file, reporting progress via callback. Plain fetch() can't
  // report upload progress, so XHR is the right tool here. Resolves with the
  // doc summary; rejects with an Error carrying the server's detail message.
  // `row` is the sidebar row this transfer reports into. Progress lands there
  // instead of in a modal, because the modal is gone by the time the bytes
  // start moving. The XHR handle is stashed on the row so Cancel can abort a
  // transfer that hasn't reached the server yet.
  function uploadOne(file, tags, row) {
    return new Promise((resolve, reject) => {
      const fd = new FormData();
      fd.append('file', file);
      if (tags) {
        for (const k of TAG_FIELDS) if (tags[k]) fd.append(k, tags[k]);
        if (tags.index_mode) fd.append('index_mode', tags.index_mode);
        if (tags.ocr) fd.append('ocr', tags.ocr);
      }
      const xhr = new XMLHttpRequest();
      xhr.open('POST', '/api/study/upload');
      // XHR bypasses the global fetch() auth wrapper in core.js, so the bearer
      // token must be set by hand. Read the same localStorage key directly.
      const token = localStorage.getItem('sable_token') || '';
      if (token) xhr.setRequestHeader('Authorization', 'Bearer ' + token);
      if (row) row.xhr = xhr;

      let aborted = false;
      xhr.upload.onprogress = (e) => {
        if (!e.lengthComputable || !row) return;
        row.transfer = Math.round((e.loaded / e.total) * 100);
        renderUploadsPanel();
      };
      xhr.onload = () => {
        if (row) row.xhr = null;
        if (xhr.status >= 200 && xhr.status < 300) {
          let res = null;
          try { res = JSON.parse(xhr.responseText); } catch {}
          resolve(res);
        } else {
          let msg = `Upload failed (${xhr.status})`;
          try { msg = JSON.parse(xhr.responseText).detail || msg; } catch {}
          reject(new Error(msg));
        }
      };
      xhr.onerror = () => { if (row) row.xhr = null; reject(new Error('network error')); };
      xhr.onabort = () => {
        aborted = true;
        const err = new Error('cancelled');
        err.cancelled = true;
        reject(err);
      };
      xhr.onloadend = () => { if (row && aborted) row.xhr = null; };
      xhr.send(fd);
    });
  }

  // Open the batch upload dialog. `initialFiles` seeds the list (from the file
  // picker or a drop); files can be added/removed before hitting Upload. Tags
  // chosen here are stamped onto every file in the batch.
  /** <option> list for the topic select: existing topics + Others + New. */
  function topicOptions(selected) {
    const topics = (ACADEMICS.topics || []).map((t) => t.name);
    if (!topics.includes(OTHERS_TOPIC)) topics.push(OTHERS_TOPIC);
    const opt = (v) => `<option value="${escapeHtml(v)}"${v === selected ? ' selected' : ''}>${escapeHtml(v)}</option>`;
    return topics.map(opt).join('') + '<option value="__new__">+ New topic…</option>';
  }

  function openUploadDialog(initialFiles) {
    let files = (initialFiles || []).filter(Boolean);
    const overlay = document.createElement('div');
    overlay.className = 'study-modal-overlay';
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
            <span>Topic <em class="study-field-req">required</em></span>
            <select data-field="topic">${topicOptions('')}</select>
          </label>
          <input class="study-field-newtopic" data-newtopic-input type="text" placeholder="New topic name…" style="display:none;" maxlength="80">
          <label class="study-field">
            <span>Indexing</span>
            <select data-field="index_mode">
              <option value="rag">Full — search + AI retrieval (slowest)</option>
              <option value="search">Search only — no embeddings</option>
              <option value="none">None — read only, index later</option>
            </select>
          </label>
          <label class="study-field">
            <span>Scanned pages (OCR)</span>
            <select data-field="ocr">
              <option value="auto">Auto — only blank pages</option>
              <option value="force">Force — re-OCR every page</option>
              <option value="never">Skip OCR</option>
            </select>
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

    // Topic is the only field — and it's mandatory. "+ New topic…" reveals an
    // inline text input so a topic can be created right here during upload.
    const topicSel = $('[data-field="topic"]');
    const newTopicInput = $('[data-newtopic-input]');
    topicSel.addEventListener('change', () => {
      newTopicInput.style.display = topicSel.value === '__new__' ? '' : 'none';
      if (topicSel.value === '__new__') newTopicInput.focus();
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
      // Resolve the topic — mandatory. "+ New topic…" uses the inline input;
      // blank/"Others" both land in the catch-all bucket.
      let topic = topicSel.value;
      if (topic === '__new__') {
        topic = (newTopicInput.value || '').trim();
        if (!topic) { newTopicInput.focus(); notify('Enter a topic name'); return; }
      }
      if (!topic) topic = OTHERS_TOPIC;

      const tags = {
        topic,
        index_mode: $('[data-field="index_mode"]').value || 'rag',
        ocr: $('[data-field="ocr"]').value || 'auto',
      };
      const queue = files.filter((f) => !fileRejection(f));
      if (!queue.length) return;

      // Close FIRST. Uploads are background work now — the sidebar panel owns
      // them from here, so there is nothing left for this modal to wait on.
      close();

      // Register a newly-typed topic in the academics list so it persists even
      // if the upload later fails. Fire-and-forget: it must not delay the files.
      if (topic !== OTHERS_TOPIC && !(ACADEMICS.topics || []).some((t) => t.name === topic)) {
        apiSend('/api/study/academics/topics', 'POST', { name: topic })
          .catch(() => { /* already exists / offline — upload still proceeds */ });
      }

      // Fired concurrently, NOT in a for-await loop. Awaiting each file meant
      // file 2 didn't begin transferring until file 1 had been converted AND
      // extracted on the server — with a big batch that multiplied the wait by
      // the number of files. Each file now has its own row and its own request.
      for (const file of queue) startUploadRow(file, tags);

      notify(queue.length === 1
        ? `Uploading ${queue[0].name}…`
        : `Uploading ${queue.length} files…`);
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

  // Rooms are owned by study-rooms.js (they live on the Sable bridge). This
  // thin delegate keeps the RENDERERS table stable if the module fails to load.
  function renderRooms() {
    return window.studyRooms ? window.studyRooms.renderTab() : '';
  }

  const RENDERERS = {
    library: renderLibrary,
    rooms: renderRooms,
    drive: () => (window.driveMode ? window.driveMode.render() : ''),
    notes: () => (window.studyNotes ? window.studyNotes.renderTab() : ''),
    decks: () => (window.studySRS ? window.studySRS.renderTab() : ''),
    humanizer: () => (window.studyHumanizer ? window.studyHumanizer.renderTab() : ''),
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

    // Same lazy-load pattern for decks: fetch once, then repaint both panes.
    if (tab === 'decks' && window.studySRS && !window.studySRS.isLoaded()) {
      window.studySRS.reload().then(() => {
        if (activeTab === 'decks') refreshTab();
      }).catch(() => {});
    }

    // Humanizer pulls its model/persona/config list once, then repaints.
    if (tab === 'humanizer' && window.studyHumanizer && !window.studyHumanizer.isLoaded()) {
      window.studyHumanizer.reload().then(() => {
        if (activeTab === 'humanizer') refreshTab();
      }).catch(() => {});
    }
  }

  /**
   * Replace an element's innerHTML without stealing focus from the user.
   * A bare `innerHTML =` destroys and recreates every child node, which blurs
   * whatever input was focused — the classic "I type one letter and the box
   * disconnects" bug. Capture the focused node + caret, repaint, restore.
   */
  function paintPreservingFocus(el, html) {
    const act = document.activeElement;
    const keep = act && el.contains(act) ? act : null;
    const key = keep && keep.id ? keep.id : null;
    const s0 = keep && 'selectionStart' in keep ? keep.selectionStart : null;
    const s1 = keep && 'selectionEnd' in keep ? keep.selectionEnd : null;

    el.innerHTML = html;

    if (!key) return;
    const next = el.querySelector('#' + CSS.escape(key)) || document.getElementById(key);
    if (!next) return;
    next.focus();
    if (s0 != null && 'setSelectionRange' in next) {
      try { next.setSelectionRange(s0, s1); } catch { /* input type has no range */ }
    }
  }

  /** Repaint the active tab + its sidebar without changing tabs. */
  function refreshTab() {
    // An editor mounted in the content pane owns it right now — repainting
    // would destroy it (and the sidebar search would lose focus mid-typing).
    if (content.querySelector('#snEditorOverlay')) return;

    paintPreservingFocus(content, (RENDERERS[activeTab] || (() => ''))());
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
      // Count of distinct non-empty values for a tag dimension.
      const nTag = (dim) => new Set(
        LIBRARY_DOCS.map((d) => ((d.group || {})[dim] || '').trim()).filter(Boolean)
      ).size;
      const countFor = (id) => {
        if (id === 'all') return nAll;
        if (id === 'progress') return nProg;
        if (id === 'bookmarked') return nMark;
        if (id === 'topic') return nTag('topic');
        // Indexing — how many jobs are actually running right now.
        if (id === 'indexing') {
          if (!window.libraryIndex || !window.libraryIndex.statusFor) return null;
          return LIBRARY_DOCS.filter((d) => {
            const s = window.libraryIndex.statusFor(d.id);
            return s && s.status === 'running';
          }).length || null;
        }
        return null; // recent — no count
      };
      html = `
        <div class="study-sidebar-search-wrap">
          <i data-lucide="search" class="icon-lucide"></i>
          <input class="study-sidebar-search" placeholder="Search library + full text…" id="studyLibSearch" value="${escapeHtml(libraryQuery)}" autocomplete="off" spellcheck="false">
          ${libraryQuery.trim() ? `<button class="study-sidebar-search-clear" data-lib-search="close" title="Clear search"><i data-lucide="x" class="icon-lucide"></i></button>` : ''}
        </div>
        <div class="study-sidebar-section">
          <div class="study-sidebar-label">Library</div>
          ${VIEWS.map((v) => {
            const n = countFor(v.id);
            const badge = n ? `<span class="sidebar-count">${n}</span>` : '';
            return `<button class="study-sidebar-item${libView === v.id ? ' active' : ''}" data-lib-view="${v.id}">
                      <i data-lucide="${v.icon}" class="icon-lucide"></i> ${v.label} ${badge}
                    </button>`;
          }).join('')}
        </div>
        <div class="study-sidebar-section">
          <div class="study-sidebar-label">Actions</div>
          <button class="study-sidebar-item" data-study-action="upload"><i data-lucide="upload" class="icon-lucide"></i> Upload documents</button>
          <button class="study-sidebar-item" data-study-action="refresh-library"><i data-lucide="refresh-cw" class="icon-lucide"></i> Refresh</button>
        </div>`;
    } else if (tab === 'rooms') {
      html = window.studyRooms ? window.studyRooms.renderSidebar() : '';
    } else if (tab === 'drive') {
      html = window.driveMode ? window.driveMode.renderSidebar() : '';
    } else if (tab === 'notes') {
      html = window.studyNotes ? window.studyNotes.renderSidebar() : '';
    } else if (tab === 'decks') {
      html = window.studySRS ? window.studySRS.renderSidebar() : '';
    } else if (tab === 'humanizer') {
      html = window.studyHumanizer ? window.studyHumanizer.renderSidebar() : '';
    }

    // Focus-preserving: a library/index refresh firing mid-typing must not
    // yank focus out of #studyLibSearch.
    paintPreservingFocus(panel, html);
    if (window.lucide) window.lucide.createIcons();

    if (tab === 'notes' && window.studyNotes) {
      window.studyNotes.bindSidebarSearch(panel);
    }

    // Wire the library search box (rebuilt on every render, so re-bind each time).
    const searchInput = panel.querySelector('#studyLibSearch');
    if (searchInput) {
      searchInput.addEventListener('input', debounce(() => {
        libraryQuery = searchInput.value;
        // Title filter repaints instantly; the FTS override follows on its own
        // debounce so a fast typist doesn't fire a request per keystroke.
        repaintLibraryContent();
        scheduleLibFullTextSearch();
        repaintSidebarSearchChrome();
      }, 150));
    }
  }

  /** Repaint just the library content pane. */
  function repaintLibraryContent() {
    if (activeTab !== 'library') return;
    content.innerHTML = renderLibrary();
    if (window.lucide) window.lucide.createIcons();
    // The override cards are .study-doc-hit — hand them to the search panel's
    // click binder so they open the reader exactly like the rail search does.
    const ov = document.getElementById('studySearchOverride');
    if (ov && window.searchPanel && window.searchPanel.bindDocHits) {
      window.searchPanel.bindDocHits(ov);
    }
  }

  /** Refresh just the sidebar search box (clear button visibility) without
      yanking focus out of the input. */
  function repaintSidebarSearchChrome() {
    const wrap = sidebar && sidebar.querySelector('.study-sidebar-search-wrap');
    if (!wrap) return;
    const existing = wrap.querySelector('.study-sidebar-search-clear');
    if (libraryQuery.trim() && !existing) {
      const btn = document.createElement('button');
      btn.className = 'study-sidebar-search-clear';
      btn.dataset.libSearch = 'close';
      btn.title = 'Clear search';
      btn.innerHTML = '<i data-lucide="x" class="icon-lucide"></i>';
      wrap.appendChild(btn);
      if (window.lucide) window.lucide.createIcons({ nodes: [wrap] });
    } else if (!libraryQuery.trim() && existing) {
      existing.remove();
    }
  }

  // ─── Sidebar full-text search (mirrors the rail search) ───
  let libSearchTimer = null;

  function scheduleLibFullTextSearch() {
    clearTimeout(libSearchTimer);
    const q = libraryQuery.trim();
    if (!q) {
      libSearchOverride = null;
      libSearchBusy = false;
      libSearchSeq++;
      repaintLibraryContent();
      return;
    }
    libSearchBusy = true;
    repaintLibraryContent();
    libSearchTimer = setTimeout(runLibFullTextSearch, 220);
  }

  async function runLibFullTextSearch() {
    const q = libraryQuery.trim();
    if (!q) return;
    const seq = ++libSearchSeq;
    const payload = (window.searchPanel && window.searchPanel.runDocSearch)
      ? await window.searchPanel.runDocSearch(q)
      : null;
    // A newer keystroke landed while this was in flight — drop the stale reply.
    if (seq !== libSearchSeq) return;
    libSearchOverride = payload;
    libSearchBusy = false;
    repaintLibraryContent();
  }

  /** Clear both the title filter and the full-text override. */
  function clearLibrarySearch(focusInput) {
    libraryQuery = '';
    libSearchOverride = null;
    libSearchBusy = false;
    libSearchSeq++;
    clearTimeout(libSearchTimer);
    const input = sidebar && sidebar.querySelector('#studyLibSearch');
    if (input) input.value = '';
    repaintSidebarSearchChrome();
    repaintLibraryContent();
    if (focusInput && input) input.focus();
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
    // Show the uploads panel only if there is something in it. The rows keep
    // polling while it is hidden, so switching to chat never abandons an
    // in-flight upload — it just stops being drawn.
    if (uploadsPanel) uploadsPanel.hidden = UPLOADS.size === 0;
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
    if (uploadsPanel) uploadsPanel.hidden = true;
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
      } else if (target === 'humanizer') {
        // Rail button doubles as a shortcut into the Humanizer tab. The stage
        // stays mounted; only the active tab changes.
        openStudyStage();
        setTab('humanizer');
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

    // The tab strip scrolls horizontally when the 8 tabs don't fit, but a
    // desktop mouse wheel only emits deltaY — so map vertical wheel to
    // horizontal scroll. Only hijack the event when the strip actually
    // overflows, otherwise a normal page scroll would feel broken.
    const tabs = view.querySelector('#studyTabs');
    if (!tabs) return;
    tabs.addEventListener('wheel', (e) => {
      if (tabs.scrollWidth <= tabs.clientWidth) return;
      const delta = Math.abs(e.deltaY) >= Math.abs(e.deltaX) ? e.deltaY : e.deltaX;
      const before = tabs.scrollLeft;
      tabs.scrollLeft += delta;
      // Only preventDefault when we actually consumed the scroll — at either
      // end let the event bubble so the page can still scroll.
      if (tabs.scrollLeft !== before) e.preventDefault();
    }, { passive: false });
  }

  function handleContentClick(e) {
    // Feature tabs own their own actions. Each handler returns true when it
    // consumed the click, so delegation order matters but stays declarative.
    if (window.studyRooms && window.studyRooms.handleClick(e)) return;
    if (window.studySRS && window.studySRS.handleClick(e)) return;

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

    // Topic ⋯ menu — must come BEFORE [data-topic-open], since the button
    // lives inside the card and would otherwise be swallowed by the drill-in.
    const topicMenuBtn = e.target.closest('[data-topic-menu]');
    if (topicMenuBtn) {
      e.stopPropagation();
      e.preventDefault();
      openTopicMenu(topicMenuBtn, topicMenuBtn.dataset.topicMenu);
      return;
    }

    // (The menu's delete item is bound inside openTopicMenu — the popup is on
    // <body>, so it never reaches this delegated handler.)

    // Drill into a topic folder in "All documents".
    const topicOpenEl = e.target.closest('[data-topic-open]');
    if (topicOpenEl) {
      allDrillTopic = topicOpenEl.dataset.topicOpen;
      content.innerHTML = renderLibrary();
      if (window.lucide) window.lucide.createIcons();
      return;
    }

    // Breadcrumb back out of a drilled-in topic folder.
    const topicBackEl = e.target.closest('[data-topic-back]');
    if (topicBackEl) {
      allDrillTopic = null;
      content.innerHTML = renderLibrary();
      if (window.lucide) window.lucide.createIcons();
      return;
    }

    // Unified library view selector (sidebar) — one list, one selection.
    const viewEl = e.target.closest('[data-lib-view]');
    if (viewEl) {
      libView = viewEl.dataset.libView;
      allDrillTopic = null;   // leaving "All" clears any drilled-in folder
      renderSidebar('library');
      content.innerHTML = renderLibrary();
      if (window.lucide) window.lucide.createIcons();
      // The panel renders from the cached status map, so pull fresh numbers
      // first — otherwise opening the tab shows whatever was last polled.
      if (libView === 'indexing' && window.libraryIndex) {
        window.libraryIndex.reloadStatuses().then(() => {
          if (libView !== 'indexing') return;
          content.innerHTML = renderLibrary();
          if (window.lucide) window.lucide.createIcons();
        });
      }
      return;
    }

    // "Jump back in" card on the Recent dashboard — switches to a tag view.
    const jumpEl = e.target.closest('[data-jump-view]');
    if (jumpEl) {
      libView = jumpEl.dataset.jumpView;
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

    // Indexing panel actions — stop, resume one doc, or sweep the library.
    const liStop = e.target.closest('[data-li-stop]');
    if (liStop && window.libraryIndex) {
      window.libraryIndex.stop(liStop.dataset.liStop);
      return;
    }
    const liRetry = e.target.closest('[data-li-retry]');
    if (liRetry && window.libraryIndex) {
      window.libraryIndex.resume(liRetry.dataset.liRetry);
      return;
    }
    const liSweep = e.target.closest('[data-li-reindex-missing]');
    if (liSweep && window.libraryIndex) {
      window.libraryIndex.indexMissing();
      return;
    }
    // Index / Re-index a single doc from the Indexing panel. Routes into the
    // same modal the doc menu opens, so there is one indexing UI, not two.
    const liIndex = e.target.closest('[data-li-index]');
    if (liIndex && window.libraryIndex) {
      const id = liIndex.dataset.liIndex;
      const d = LIBRARY_DOCS.find((x) => x.id === id);
      window.libraryIndex.indexDoc(id, (d && d.title) || id);
      return;
    }

    // Sidebar/library search: clear the query + the full-text override.
    const libSearchEl = e.target.closest('[data-lib-search]');
    if (libSearchEl) {
      if (libSearchEl.dataset.libSearch === 'close') clearLibrarySearch(true);
      return;
    }

    const actionEl = e.target.closest('[data-study-action]');
    if (actionEl) {
      const action = actionEl.dataset.studyAction;
      if (action === 'upload') {
        pickAndUpload();
      } else if (action === 'refresh-library') {
        loadDocs().then(() => notify('Library refreshed'));
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

  /**
   * Repaint the view that shows topics after a create/delete. The library's
   * "All documents" grid is now the only place topics surface, so it is the
   * only thing that needs a repaint.
   */
  async function refreshAcademicsView() {
    await loadAcademics();
    if (activeTab === 'library') {
      content.innerHTML = renderLibrary();
      renderSidebar('library');
      if (window.lucide) window.lucide.createIcons();
    }
  }

  /**
   * @param action  'add-topic' | 'del-topic'
   * @param arg     For 'del-topic' this is the topic NAME (that's what the UI
   *                carries); the topic id is resolved here, because the delete
   *                endpoint keys off ids and _find_topic only matches on id.
   */
  async function handleAcadAction(action, arg) {
    try {
      if (action === 'add-topic') {
        const name = ((await sableDialog.prompt({
          title: 'New topic', label: 'Topic name',
          placeholder: 'e.g. Trees & Graphs', okText: 'Create',
        })) || '').trim();
        if (!name) return;
        await apiSend('/api/study/academics/topics', 'POST', { name });

      } else if (action === 'del-topic') {
        const topicName = arg;
        if (!topicName) return;
        if (topicName === OTHERS_TOPIC) {
          notify('“Others” is the catch-all and can’t be deleted');
          return;
        }

        // A topic only exists in ACADEMICS once it's been created explicitly.
        // A name that appears solely on documents has no id to delete.
        const entry = (ACADEMICS.topics || []).find((t) => t.name === topicName);
        if (!entry || !entry.id) {
          notify(`“${topicName}” isn’t a managed topic — nothing to delete`);
          return;
        }

        if (!(await sableDialog.confirm({
          title: `Delete “${topicName}”?`,
          message: 'Its documents move to “Others”. The documents themselves are not deleted.',
          okText: 'Delete topic', danger: true,
        }))) return;

        await apiSend(`/api/study/academics/topics/${entry.id}`, 'DELETE');

        // If we were drilled into it, walk back out — the folder is gone.
        if (allDrillTopic === topicName) allDrillTopic = null;

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
  function openDocMenu(docId) {
    const doc = findDoc(docId);
    if (!doc) return;
    const g = doc.group || {};
    const current = g.topic || OTHERS_TOPIC;

    const overlay = document.createElement('div');
    overlay.className = 'study-modal-overlay';
    overlay.innerHTML = `
      <div class="study-modal" role="dialog" aria-modal="true">
        <div class="study-modal-head">
          <h3>Assign topic</h3>
          <button class="study-modal-x" data-modal-close>&times;</button>
        </div>
        <p class="study-modal-doc">${escapeHtml(doc.title)}</p>
        <label class="study-field">
          <span>Topic <em class="study-field-req">required</em></span>
          <select data-field="topic">${topicOptions(current)}</select>
        </label>
        <input class="study-field-newtopic" data-newtopic-input type="text" placeholder="New topic name…" style="display:none;" maxlength="80">
        <div class="study-modal-actions">
          <button class="study-room-action danger" data-modal-remove>Remove</button>
          <button class="study-room-action secondary" data-modal-index type="button">Index…</button>
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

    const topicSel = $('[data-field="topic"]');
    const newTopicInput = $('[data-newtopic-input]');
    topicSel.addEventListener('change', () => {
      newTopicInput.style.display = topicSel.value === '__new__' ? '' : 'none';
      if (topicSel.value === '__new__') newTopicInput.focus();
    });

    $('[data-modal-save]').onclick = () => saveGroup(overlay, doc, close);
    $('[data-modal-remove]').onclick = () => removeDoc(doc, close);
    const indexBtn = $('[data-modal-index]');
    if (indexBtn) {
      indexBtn.onclick = () => {
        if (window.libraryIndex && window.libraryIndex.open) {
          window.libraryIndex.open(doc.id, doc.title);
        } else {
          notify('Indexing UI unavailable');
        }
      };
    }
  }

  /**
   * Permanently delete a document (PDF + meta + page text + highlights/notes).
   * The server owns the cleanup; the client only drops the card and re-renders.
   */
  async function removeDoc(doc, close) {
    const label = doc && doc.title ? `"${doc.title}"` : 'this document';
    if (!(await sableDialog.confirm({
      title: `Remove ${label}?`,
      message: 'Its highlights, notes and progress are deleted too. This cannot be undone.',
      okText: 'Delete', danger: true,
    }))) return;
    try {
      await apiSend(`/api/study/doc/${doc.id}`, 'DELETE');
      if (typeof close === 'function') close();
      LIBRARY_DOCS = LIBRARY_DOCS.filter((d) => d.id !== doc.id);
      if (activeTab === 'library') {
        content.innerHTML = renderLibrary();
        if (window.lucide) window.lucide.createIcons();
      }
      refreshSidebarCounts();
      // The deleted doc is still in the index-status cache, so the Indexing tab
      // and the card badges would keep rendering it. Drop the cache and pull a
      // fresh overview — the server reconciles it against the library.
      if (window.libraryIndex && window.libraryIndex.reloadStatuses) {
        window.libraryIndex.reloadStatuses().then(() => {
          if (activeTab === 'library' && libView === 'indexing') {
            content.innerHTML = renderLibrary();
            if (window.lucide) window.lucide.createIcons();
          }
        });
      }
      notify('Document removed');
    } catch (err) {
      notify(err.message || 'Could not remove document');
    }
  }

  async function saveGroup(overlay, doc, close) {
    const sel = overlay.querySelector('[data-field="topic"]');
    const newInput = overlay.querySelector('[data-newtopic-input]');
    let topic = sel ? sel.value : '';
    if (topic === '__new__') topic = (newInput && newInput.value || '').trim();
    if (!topic) topic = OTHERS_TOPIC;   // topic is mandatory — fall back to catch-all

    // Register a newly-created topic so it shows up in the topic list.
    if (topic !== OTHERS_TOPIC && !(ACADEMICS.topics || []).some((t) => t.name === topic)) {
      try { await apiSend('/api/study/academics/topics', 'POST', { name: topic }); }
      catch { /* already exists / offline — save still proceeds */ }
    }

    try {
      const res = await fetch(`/api/study/doc/${doc.id}/group`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ topic }),
      });
      if (!res.ok) throw new Error(res.statusText);
      doc.group = await res.json();
      close();
      await loadAcademics();
      content.innerHTML = renderLibrary();
      if (window.lucide) window.lucide.createIcons();
      notify('Topic updated ✓');
    } catch (err) {
      notify('Could not save topic: ' + err.message);
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
    wireRailSwitch();
    content.addEventListener('click', handleContentClick);
    content.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter' && e.key !== ' ') return;
      // Topic ⋯ button → open the menu (keyboard parity with the mouse path).
      const topicMenuKb = e.target.closest('[data-topic-menu]');
      if (topicMenuKb) {
        e.preventDefault();
        openTopicMenu(topicMenuKb, topicMenuKb.dataset.topicMenu);
        return;
      }
      // Topic folder card → drill in ("All documents" root).
      const topicEl = e.target.closest('[data-topic-open]');
      if (topicEl) {
        e.preventDefault();
        allDrillTopic = topicEl.dataset.topicOpen;
        content.innerHTML = renderLibrary();
        if (window.lucide) window.lucide.createIcons();
        return;
      }
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

    // Indexing progress tick. Statuses are already cached by library-index.js
    // (it owns the poll), so this only repaints — a full loadDocs() here would
    // fire four fetches every two seconds for data that did not change.
    window.addEventListener('study-index-changed', () => {
      if (activeTab !== 'library') return;
      if (libView !== 'indexing' && libView !== 'recent') return;
      content.innerHTML = renderLibrary();
      if (window.lucide) window.lucide.createIcons();
    });

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
      // Shared entry points the SRS / search / stats modules depend on, so they
      // never have to reach into this closure's internals.
      openDoc,
      toast: notify,
      reloadDocs: loadDocs,
      // Drill straight into a topic folder from outside study.js (the study
      // knowledge graph uses this when a topic node is clicked).
      openTopic(name) {
        if (!name) return;
        libView = 'all';
        allDrillTopic = name;
        setTab('library');
      },
    };
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
