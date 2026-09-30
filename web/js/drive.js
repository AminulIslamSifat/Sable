/**
 * drive.js — Google Drive browse + import for Study mode.
 *
 * Pure Drive API v3 on the backend (see server/api/routes/drive.py): public
 * files use an API key, private files use an OAuth connection. Either way this
 * module only talks to /api/drive/* and lets the backend pick the credential.
 *
 * Imported files land in the Study library through the same pipeline as an
 * upload — so after importing, the Library tab already shows them.
 *
 * Exposes window.driveMode = { render, renderSidebar, handleAction, init },
 * which study.js delegates to for the 'drive' tab.
 */
(function () {
  'use strict';

  let STATUS = null;            // /api/drive/status payload
  let statusLoaded = false;
  let selected = new Set();     // selected file ids
  let loading = false;
  let busyMsg = '';
  let urlInput = '';
  let setupOpen = false;
  let truncatedFlag = false;
  let managing = false;   // force the credentials screen even when connected
  let browseMode = 'public';  // 'public' (keyless) | 'authed' (private)
  let SOURCES = [];           // saved Drive links (the cards)
  let sourcesLoaded = false;
  let INDEX_STATUS = {};      // { source_id: { indexed, scanning, status } }
  let indexPoller = null;     // interval id while any source is scanning
  let searchQuery = '';       // drive-wide search term
  let searchResults = null;   // null = not searched
  let searchBusy = false;
  let searchOpen = false;     // is the search pane showing?
  let searchScope = null;     // { id, name } — restrict to one source
  let addOpen = false;        // "add link" panel visibility
  let activeSource = null;    // currently opened source card
  let sourceItems = [];       // files inside the active source
  let sourcePath = [];        // [{id,name}] — breadcrumb inside a source

  const $ = (id) => document.getElementById(id);

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  function notify(msg) {
    if (window.showToast) return window.showToast(msg);
    console.log('[drive]', msg);
  }

  async function api(path, opts) {
    const res = await fetch(path, opts);
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch {}
      throw new Error(detail);
    }
    return res.status === 204 ? null : res.json().catch(() => null);
  }

  function fmtBytes(n) {
    n = Number(n) || 0;
    if (!n) return '—';
    const u = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.min(u.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
    return `${(n / 1024 ** i).toFixed(i ? 1 : 0)} ${u[i]}`;
  }

  // MIME → lucide icon + short kind label.
  function iconFor(it) {
    if (it.is_folder) return 'folder';
    const m = it.mimeType || '';
    if (m.includes('document')) return 'file-text';
    if (m.includes('spreadsheet')) return 'table';
    if (m.includes('presentation')) return 'presentation';
    if (m.includes('pdf')) return 'file-text';
    if (m.startsWith('image/')) return 'image';
    if (m.startsWith('video/')) return 'video';
    if (m.startsWith('audio/')) return 'music';
    if (m.includes('zip') || m.includes('compressed')) return 'archive';
    return 'file';
  }

  // ── Status ────────────────────────────────────────────────────────────────

  async function loadStatus(force) {
    if (statusLoaded && !force) return STATUS;
    try {
      STATUS = await api('/api/drive/status');
    } catch (e) {
      STATUS = { connected: false, has_api_key: false, has_oauth: false };
    }
    statusLoaded = true;
    // The sidebar renders credential state straight from STATUS. It draws
    // "Checking…" until this resolves, so it must be re-rendered here — the
    // content pane refreshes on its own, but the sidebar does not.
    if (window.studyMode && window.studyMode.refreshSidebar) {
      window.studyMode.refreshSidebar();
    }
    return STATUS;
  }

  // ── Rendering ─────────────────────────────────────────────────────────────

  function render() {
    if (managing) return renderConnect();
    if (!sourcesLoaded) {
      loadSources().then(() => {
        const c = $('studyContent');
        if (c && window.studyMode && window.studyMode.getTab() === 'drive') {
          c.innerHTML = render();
          if (window.lucide) window.lucide.createIcons();
        }
      });
      return `<div class="drive-loading"><div class="drive-spinner"></div><p>Loading your Drive…</p></div>`;
    }
    // Search wins over everything; then an open source; else the card grid.
    if (searchOpen) return renderSearch();
    if (activeSource) return renderSourceView();
    return renderSources();
  }

  // ── Saved sources (card grid) ─────────────────────────────────────────────

  function renderSources() {
    const cards = SOURCES.length
      ? `<div class="drive-cards">${SOURCES.map(sourceCard).join('')}</div>`
      : `<div class="study-empty">
           <div class="study-empty-icon">📁</div>
           <h3>No Drive links yet</h3>
           <p>Add a public Google Drive folder or file link — it'll appear here as a card, ready to browse and view.</p>
           <button class="study-empty-btn" data-drive-action="toggle-add">Add a Drive link</button>
         </div>`;
    return `
      <div class="drive-sources">
        <div class="drive-sources-head">
          <div>
            <h3>Drive</h3>
            <p>${SOURCES.length} saved link${SOURCES.length === 1 ? '' : 's'}</p>
          </div>
          <div class="drive-sources-actions">
            <button class="drive-btn ghost" data-drive-action="toggle-search" title="Search all drives">
              <i data-lucide="search" class="icon-lucide"></i> Search
            </button>
            <button class="drive-btn primary" data-drive-action="toggle-add">
              <i data-lucide="plus" class="icon-lucide"></i> ${addOpen ? 'Cancel' : 'Add link'}
            </button>
          </div>
        </div>

        ${addOpen ? renderAddPanel() : ''}

        ${cards}
      </div>`;
  }

  function renderAddPanel() {
    return `
      <div class="drive-add">
        <div class="drive-url-wrap">
          <i data-lucide="link" class="icon-lucide"></i>
          <input class="drive-url" id="driveUrl"
                 placeholder="Paste a public Google Drive folder or file link…"
                 value="${esc(urlInput)}" autocomplete="off" spellcheck="false">
        </div>
        <button class="drive-btn primary" data-drive-action="add-link" ${busyMsg ? 'disabled' : ''}>
          <i data-lucide="plus" class="icon-lucide"></i> Add
        </button>
        ${busyMsg ? `<div class="drive-busy"><div class="drive-spinner sm"></div>${esc(busyMsg)}</div>` : ''}
        <div class="drive-add-note">
          <i data-lucide="globe" class="icon-lucide"></i>
          Public links need no account. Shared as “Anyone with the link”.
        </div>
      </div>`;
  }

  function sourceCard(s) {
    const isFolder = s.kind === 'folder';
    const idx = INDEX_STATUS[s.id];
    const scanning = idx && (idx.scanning || idx.status === 'scanning' || idx.status === 'refreshing');
    const indexed = idx && idx.indexed;
    const entryCount = idx ? idx.entry_count : s.count;
    return `
      <div class="drive-card" data-drive-action="open-source" data-id="${s.id}" role="button" tabindex="0">
        <div class="drive-card-thumb">
          <i data-lucide="${isFolder ? 'folder-open' : 'file-text'}" class="icon-lucide drive-card-icon"></i>
        </div>
        <button class="drive-card-menu" data-drive-action="remove-source" data-id="${s.id}"
                title="Remove from Drive">×</button>
        <div class="drive-card-info">
          <div class="drive-card-name" title="${esc(s.name)}">${esc(s.name)}</div>
          <div class="drive-card-meta">
            <span class="drive-card-kind">${isFolder ? 'Folder' : 'File'}</span>
            ${scanning
              ? '<span class="drive-card-indexing"><i data-lucide="loader" class="icon-lucide"></i> Indexing…</span>'
              : isFolder && entryCount ? `<span>${entryCount} item${entryCount === 1 ? '' : 's'}</span>` : ''}
          </div>
        </div>
      </div>`;
  }

  // ── One source open (its files) ───────────────────────────────────────────

  function renderSourceView() {
    const s = activeSource;
    const list = loading
      ? `<div class="drive-loading"><div class="drive-spinner"></div><p>Loading…</p></div>`
      : sourceItems.length
        ? `<div class="drive-list">${sourceItems.map(rowHtml).join('')}</div>`
        : `<div class="study-empty"><div class="study-empty-icon">📂</div><h3>Nothing here</h3><p>This source has no readable files.</p></div>`;

    const selCount = selected.size;
    return `
      <div class="drive-source-view">
        <div class="drive-source-head">
          <button class="drive-btn ghost" data-drive-action="close-source">
            <i data-lucide="arrow-left" class="icon-lucide"></i> All links
          </button>
          <div class="drive-source-title">
            <i data-lucide="folder-open" class="icon-lucide"></i>
            ${sourcePath.map((p, i) =>
              i < sourcePath.length - 1
                ? `<button class="drive-crumb" data-drive-action="crumb" data-index="${i}">${esc(p.name)}</button><span class="drive-crumb-sep">/</span>`
                : `<span>${esc(p.name)}</span>`
            ).join('')}
          </div>
          <button class="drive-btn ghost" data-drive-action="search-in-source" title="Search this drive">
            <i data-lucide="search" class="icon-lucide"></i>
          </button>
          <button class="drive-btn ghost" data-drive-action="reload-source" title="Refresh">
            <i data-lucide="refresh-cw" class="icon-lucide"></i>
          </button>
          ${indexStatus === 'scanning' || indexStatus === 'refreshing'
            ? '<span class="drive-index-badge scanning"><i data-lucide="loader" class="icon-lucide"></i> Indexing…</span>'
            : fromCache && indexStatus === 'stale'
              ? '<span class="drive-index-badge stale" title="Index may be outdated">⚠ Stale index</span>'
              : ''}
        </div>

        ${busyMsg ? `<div class="drive-busy"><div class="drive-spinner sm"></div>${esc(busyMsg)}</div>` : ''}
        ${truncatedFlag ? `<div class="drive-truncated"><i data-lucide="alert-triangle" class="icon-lucide"></i> Showing the first 5,000 items.</div>` : ''}

        ${list}

        ${selCount ? `
          <div class="drive-import-bar">
            <span class="drive-import-count">${selCount} file${selCount > 1 ? 's' : ''} selected</span>
            <button class="drive-btn ghost" data-drive-action="clear-sel">Clear</button>
            <button class="drive-btn primary" data-drive-action="import" ${busyMsg ? 'disabled' : ''}>
              <i data-lucide="download" class="icon-lucide"></i> Import to Library
            </button>
          </div>` : ''}
      </div>`;
  }

  function renderConnect() {
    const keySet = STATUS && STATUS.has_api_key;
    const oauthSet = STATUS && STATUS.has_oauth;
    const isManaging = managing;
    const anyCreds = !!(STATUS && (STATUS.has_api_key || STATUS.has_oauth));
    const account = (STATUS && (STATUS.account || STATUS.email)) || '';

    return `
      <div class="drive-connect">
        <div class="drive-connect-head">
          <div class="drive-connect-icon"><i data-lucide="${isManaging ? 'settings' : 'hard-drive-download'}" class="icon-lucide"></i></div>
          <div class="drive-connect-copy">
            <h3>Drive credentials <span class="drive-badge">optional</span></h3>
            <p>Public folders and files work with <b>no credentials at all</b> — just paste a link in the Drive tab.
               ${anyCreds ? 'These are only used for your <b>private</b> files.' : 'Add credentials only if you need access to your <b>private</b> files.'}</p>
          </div>
          ${isManaging
            ? `<button class="drive-btn ghost drive-back" data-drive-action="close-manage">
                 <i data-lucide="arrow-left" class="icon-lucide"></i> Back
               </button>`
            : ''}
        </div>

        <div class="drive-method-grid">
          <div class="drive-method${keySet ? ' done' : ''}">
            <div class="drive-method-top">
              <i data-lucide="key-round" class="icon-lucide"></i>
              <span class="drive-method-title">Public files</span>
              ${keySet ? '<span class="drive-badge ok">API key set</span>' : '<span class="drive-badge">API key</span>'}
            </div>
            <p class="drive-method-desc">Any file or folder shared with “anyone with the link”. No sign-in needed.</p>
            <div class="drive-field">
              <input type="password" id="driveApiKey" class="drive-input"
                     placeholder="${keySet ? 'Paste a new key to replace the saved one' : 'Paste your Google API key'}"
                     autocomplete="off" spellcheck="false">
              <button class="drive-btn primary" data-drive-action="save-key">${keySet ? 'Replace' : 'Save'}</button>
            </div>
            ${keySet
              ? `<div class="drive-ok-line"><i data-lucide="check" class="icon-lucide"></i> Public Drive access is ready</div>
                 <button class="drive-btn ghost danger" data-drive-action="remove-key">
                   <i data-lucide="trash-2" class="icon-lucide"></i> Remove key
                 </button>`
              : ''}
          </div>

          <div class="drive-method${oauthSet ? ' done' : ''}">
            <div class="drive-method-top">
              <i data-lucide="user-check" class="icon-lucide"></i>
              <span class="drive-method-title">Your own drive</span>
              ${oauthSet ? `<span class="drive-badge ok">${esc(account || 'Connected')}</span>` : '<span class="drive-badge">OAuth</span>'}
            </div>
            <p class="drive-method-desc">Sign in once to browse everything your Google account can see — including private files.</p>
            <div class="drive-field">
              <input type="text" id="driveClientId" class="drive-input" placeholder="OAuth Client ID"
                     value="${esc((STATUS && STATUS.oauth_client_id) || '')}" autocomplete="off" spellcheck="false">
              <button class="drive-btn primary" data-drive-action="save-client">Save</button>
            </div>
            ${oauthSet
              ? `<div class="drive-ok-line"><i data-lucide="check" class="icon-lucide"></i> Signed in${account ? ' as ' + esc(account) : ''}</div>
                 <button class="drive-btn ghost" data-drive-action="disconnect">
                   <i data-lucide="log-out" class="icon-lucide"></i> Disconnect
                 </button>`
              : `<button class="drive-btn connect" data-drive-action="oauth-start">
                   <i data-lucide="log-in" class="icon-lucide"></i> Sign in with Google
                 </button>`
            }
          </div>
        </div>

        <button class="drive-setup-toggle" data-drive-action="toggle-setup">
          <i data-lucide="help-circle" class="icon-lucide"></i> How do I get these credentials?
        </button>
        ${setupOpen ? renderSetupHelp() : ''}
      </div>`;
  }

  function renderSetupHelp() {
    return `
      <div class="drive-setup-help">
        <ol>
          <li>Open <b>console.cloud.google.com</b> → create (or pick) a project.</li>
          <li><b>APIs &amp; Services → Library</b> → enable <b>Google Drive API</b>.</li>
          <li><b>Credentials → Create credentials → API key</b>. Paste it in the “Public files” box above.</li>
          <li>For private files: <b>Create credentials → OAuth client ID → Desktop app</b>.
              Add <code>${esc((location.origin || 'http://localhost:61770'))}/api/drive/oauth/callback</code>
              as an authorised redirect URI, then paste the Client ID above and hit <b>Sign in</b>.</li>
        </ol>
      </div>`;
  }


  function rowHtml(it) {
    const isSel = selected.has(it.id);
    const isFolder = it.is_folder;
    // A folder row navigates; a file row toggles selection. The separate
    // View button lets a file be opened without importing it.
    return `
      <div class="drive-row${isSel ? ' selected' : ''}"
           data-drive-action="${isFolder ? 'open-folder' : 'toggle-select'}"
           data-id="${it.id}" data-name="${esc(it.name)}" data-mime="${esc(it.mimeType || '')}">
        ${isFolder
          ? '<span class="drive-check folder"><i data-lucide="chevron-right" class="icon-lucide"></i></span>'
          : `<span class="drive-check${isSel ? ' on' : ''}">${isSel ? '✓' : ''}</span>`}
        <i data-lucide="${iconFor(it)}" class="icon-lucide drive-row-icon"></i>
        <div class="drive-row-main">
          <div class="drive-row-name">${esc(it.name)}</div>
          <div class="drive-row-meta">${isFolder ? 'Folder' : esc(fmtBytes(it.size))}</div>
        </div>
        ${!isFolder ? `<button class="drive-view-btn" data-drive-action="view-file"
                                data-id="${it.id}" data-name="${esc(it.name)}" title="Open in Sable viewer">
                         <i data-lucide="eye" class="icon-lucide"></i> View
                       </button>` : ''}
      </div>`;
  }

  // ── Sidebar ───────────────────────────────────────────────────────────────

  // -- Drive-wide search ---------------------------------------------------

  function searchRowHtml(r) {
    const icon = r.kind === 'folder' ? 'folder' : 'file-text';
    const meta = esc(r.source_name) + (r.kind === 'file' && r.size ? ' · ' + fmtBytes(r.size) : '');
    return [
      '<div class="drive-row" data-drive-action="open-result"',
      ' data-source="' + esc(r.source_id) + '" data-parent="' + esc(r.parent) + '"',
      ' data-id="' + esc(r.id) + '" data-name="' + esc(r.name) + '" data-kind="' + esc(r.kind) + '">',
      '<i data-lucide="' + icon + '" class="icon-lucide drive-row-icon"></i>',
      '<div class="drive-row-main">',
      '<div class="drive-row-name">' + esc(r.name) + '</div>',
      '<div class="drive-row-meta">' + meta + '</div>',
      '</div>',
      '</div>',
    ].join('');
  }

  function renderSearch() {
    const q = searchQuery;
    const scoped = searchScope;
    let body;
    if (searchBusy) {
      body = '<div class="drive-loading"><div class="drive-spinner"></div><p>Searching…</p></div>';
    } else if (searchResults && searchResults.length) {
      if (scoped) {
        // Scoped search: hits inside this drive first, then the rest.
        const inScope = searchResults.filter((r) => r.source_id === scoped.id);
        const others = searchResults.filter((r) => r.source_id !== scoped.id);
        const blocks = [];
        blocks.push(
          '<div class="drive-result-group">'
          + '<div class="drive-result-group-label">In ' + esc(scoped.name) + '</div>'
          + (inScope.length
              ? '<div class="drive-list">' + inScope.map(searchRowHtml).join('') + '</div>'
              : '<div class="drive-result-none">Not available in this drive</div>')
          + '</div>'
        );
        if (others.length) {
          blocks.push(
            '<div class="drive-result-group">'
            + '<div class="drive-result-group-label">Other drives</div>'
            + '<div class="drive-list">' + others.map(searchRowHtml).join('') + '</div>'
            + '</div>'
          );
        }
        body = blocks.join('');
      } else {
        body = '<div class="drive-list">' + searchResults.map(searchRowHtml).join('') + '</div>';
      }
    } else if (searchResults) {
      body = '<div class="study-empty"><div class="study-empty-icon"><i data-lucide="search-x" class="icon-lucide"></i></div><h3>No matches</h3><p>Nothing matching that in any indexed drive.</p></div>';
    } else {
      body = scoped
        ? '<div class="study-empty"><div class="study-empty-icon"><i data-lucide="search" class="icon-lucide"></i></div><h3>Search this drive</h3><p>Results from <b>' + esc(scoped.name) + '</b> show first, then everything else you have indexed.</p></div>'
        : '<div class="study-empty"><div class="study-empty-icon"><i data-lucide="search" class="icon-lucide"></i></div><h3>Search all drives</h3><p>Type a file or folder name. It searches every indexed Drive source at once.</p></div>';
    }
    const stats = searchResults ? (searchResults.length + ' result' + (searchResults.length === 1 ? '' : 's')) : '';
    return [
      '<div class="drive-search">',
      '<div class="drive-source-head">',
      '<button class="drive-btn ghost" data-drive-action="close-search"><i data-lucide="arrow-left" class="icon-lucide"></i> ' + (scoped ? 'Back' : 'Drive') + '</button>',
      '<div class="drive-searchbox"><i data-lucide="search" class="icon-lucide"></i>',
      '<input id="driveSearchInput" type="search" placeholder="' + (scoped ? 'Search in ' + esc(scoped.name) + '…' : 'Search all indexed drives…') + '" value="' + esc(q) + '" autocomplete="off" spellcheck="false"></div>',
      '</div>',
      '<div class="drive-search-stats">' + stats + '</div>',
      body,
      '</div>',
    ].join('');
  }

  let searchDebounce = null;
  async function runDriveSearch() {
    const v = searchQuery.trim();
    if (!v) { searchResults = null; searchBusy = false; rerender(); return; }
    searchBusy = true;
    rerender();
    try {
      const data = await api('/api/drive/search?q=' + encodeURIComponent(v) + '&limit=200');
      searchResults = data.results || [];
    } catch (e) {
      searchResults = [];
    } finally {
      searchBusy = false;
      rerender();
      focusSearchInput();
    }
  }

  function focusSearchInput() {
    const el = document.getElementById('driveSearchInput');
    if (el && document.activeElement !== el) {
      el.focus();
      el.setSelectionRange(el.value.length, el.value.length);
    }
  }

  function openSearchResult(el) {
    const sid = el.dataset.source;
    const src = SOURCES.find((s) => s.id === sid);
    if (!src) return notify('Source not found');
    if (el.dataset.kind === 'folder') {
      activeSource = src;
      sourcePath = [{ id: src.drive_id, name: src.name }];
      selected = new Set();
      searchResults = null; searchQuery = '';
      const parent = el.dataset.parent;
      if (parent && parent !== src.drive_id) {
        sourcePath.push({ id: parent, name: el.dataset.name });
      }
      loadPath();
    } else {
      viewFile({ id: el.dataset.id, name: el.dataset.name, mimeType: '' });
    }
  }

  function renderSidebar() {
    if (!STATUS) {
      return `<div class="study-sidebar-section"><div class="study-sidebar-label">Drive</div><div class="drive-side-note">Checking…</div></div>`;
    }
    const keySet = STATUS.has_api_key;
    const oauthSet = STATUS.has_oauth;
    const credState = (keySet || oauthSet)
      ? `${[keySet ? 'key' : null, oauthSet ? 'account' : null].filter(Boolean).join(' + ')} set`
      : 'not set';
    return `
      <div class="study-sidebar-section">
        <div class="study-sidebar-label">Browse</div>
        <div class="drive-side-note">Public links work with no setup — just paste one in the Drive tab.</div>
        <button class="study-sidebar-item" data-drive-action="reload"><i data-lucide="refresh-cw" class="icon-lucide"></i> Refresh folder</button>
      </div>
      <div class="study-sidebar-section">
        <div class="study-sidebar-label">Private access</div>
        <button class="study-sidebar-item" data-drive-action="manage">
          <i data-lucide="settings" class="icon-lucide"></i> Credentials <span class="sidebar-count">${esc(credState)}</span>
        </button>
        <button class="study-sidebar-item" data-drive-action="toggle-setup"><i data-lucide="help-circle" class="icon-lucide"></i> Setup guide</button>
      </div>`;
  }

  // ── Actions ───────────────────────────────────────────────────────────────

  async function loadSources(force) {
    if (sourcesLoaded && !force) return SOURCES;
    try {
      SOURCES = await api('/api/drive/sources');
    } catch (e) {
      SOURCES = [];
    }
    sourcesLoaded = true;
    // Fetch index status for folder sources (non-blocking, best-effort).
    fetchIndexStatuses();
    // Await so the sidebar leaves its "Checking…" state as soon as the first
    // load completes instead of depending on render order.
    await loadStatus();
    return SOURCES;
  }

  /** Re-check index status for every folder source, then re-render + poll. */
  async function fetchIndexStatuses() {
    const folders = SOURCES.filter((s) => s.kind === 'folder');
    if (!folders.length) return;
    await Promise.allSettled(
      folders.map(async (s) => {
        try {
          INDEX_STATUS[s.id] = await api(`/api/drive/sources/${s.id}/index`);
        } catch {}
      })
    );
    if (!activeSource && window.studyMode && window.studyMode.getTab() === 'drive') {
      rerender();
    }
    // Keep polling while anything is still scanning, so the spinner clears
    // itself the moment the background scan finishes.
    const anyScanning = folders.some((s) => {
      const i = INDEX_STATUS[s.id];
      return i && (i.scanning || i.status === 'scanning' || i.status === 'refreshing');
    });
    if (anyScanning) ensureIndexPoller();
    else stopIndexPoller();
  }

  function ensureIndexPoller() {
    if (indexPoller) return;
    indexPoller = setInterval(() => {
      if (!sourcesLoaded) return;
      fetchIndexStatuses();
    }, 3000);
  }

  function stopIndexPoller() {
    if (indexPoller) { clearInterval(indexPoller); indexPoller = null; }
  }


  /** Add the pasted link as a saved source card. */
  async function addLink() {
    const inp = $('driveUrl');
    urlInput = inp ? inp.value.trim() : urlInput;
    if (!urlInput) return notify('Paste a Drive link first');
    busyMsg = 'Adding link…';
    rerender();
    try {
      await api('/api/drive/sources', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: urlInput }),
      });
      busyMsg = ''; addOpen = false; urlInput = '';
      await loadSources(true);
      window.dispatchEvent(new CustomEvent('drive-sources-changed'));
      rerender();
      notify('Link added ✓');
    } catch (e) {
      busyMsg = '';
      rerender();
      notify(e.message || 'Could not add that link');
    }
  }

  async function removeSource(id) {
    const src = SOURCES.find((s) => s.id === id);
    if (!confirm(`Remove "${src ? src.name : 'this link'}" from Drive? Files already imported stay in your library.`)) return;
    try {
      await api(`/api/drive/sources/${id}`, { method: 'DELETE' });
      if (activeSource && activeSource.id === id) { activeSource = null; sourceItems = []; }
      await loadSources(true);
      window.dispatchEvent(new CustomEvent('drive-sources-changed'));
      rerender();
      notify('Removed');
    } catch (e) { notify(e.message || 'Could not remove'); }
  }

  /** Open a source card and list its files. */
  async function openSource(id) {
    const src = SOURCES.find((s) => s.id === id);
    if (!src) return;
    searchOpen = false; searchScope = null;
    activeSource = src;
    sourcePath = [{ id: src.drive_id, name: src.name }];
    selected = new Set();
    if (src.kind !== 'folder') {
      // Single-file source — open it directly in the viewer.
      activeSource = null;
      rerender();
      return viewFile({ id: src.drive_id, name: src.name, mimeType: src.mimeType || '' });
    }
    await loadPath();
  }

  let indexStatus = '';   // '' | 'scanning' | 'ready' | 'stale' | 'refreshing'
  let fromCache = false;

  /** Load the folder at the end of sourcePath. */
  async function loadPath() {
    const here = sourcePath[sourcePath.length - 1];
    if (!here) return;
    loading = true; busyMsg = '';
    rerender();
    try {
      if (sourcePath.length > 1) {
        // Subfolder — use source-aware endpoint that serves from cache.
        const sub = await api(
          `/api/drive/sources/${activeSource.id}/folder/${encodeURIComponent(here.id)}`
        );
        sourceItems = sub.items || [];
        truncatedFlag = !!sub.truncated;
        browseMode = sub.mode || 'public';
        fromCache = !!sub.from_cache;
      } else {
        // Source root.
        const data = await api(`/api/drive/sources/${activeSource.id}/files`);
        sourceItems = data.items || [];
        truncatedFlag = !!data.truncated;
        browseMode = data.mode || 'public';
        fromCache = !!data.from_cache;
        indexStatus = data.index_status || (data.scanning ? 'scanning' : '');
      }
    } catch (e) {
      sourceItems = [];
      notify(e.message || 'Could not load this folder');
    } finally {
      loading = false;
      rerender();
    }
  }

  function openFolder(id, name) {
    sourcePath.push({ id, name });
    selected = new Set();
    loadPath();
  }

  async function reloadSource() {
    if (!activeSource) return;
    // Trigger a background reindex, then reload the view.
    try {
      await api(`/api/drive/sources/${activeSource.id}/reindex`, { method: 'POST' });
      indexStatus = 'refreshing';
      rerender();
    } catch {}
    // Small delay to let the indexer start before we fetch.
    await new Promise((r) => setTimeout(r, 300));
    await loadPath();
  }

  function closeSource() {
    activeSource = null;
    sourceItems = [];
    sourcePath = [];
    selected = new Set();
    truncatedFlag = false;
    rerender();
  }

  /**
   * Open a Drive file in Sable's own PDF viewer — streamed straight from
   * Drive, so nothing is downloaded to disk first.
   */
  function viewFile(it) {
    // An image is not a document — pdf.js chokes on it. Route it to the
    // lightbox instead of forcing it through the PDF reader.
    if (isImage(it.mimeType, it.name)) return viewDriveImage(it);
    if (!window.studyReader) return notify('Viewer still loading…');
    if (window.studyReader.openRemote) {
      window.studyReader.openRemote(it.id, it.name);
    } else {
      window.studyReader.open(it.id, { remote: true, title: it.name });
    }
  }

  const IMAGE_EXT = /\.(png|jpe?g|gif|webp|bmp|avif|svg|ico|tiff?)$/i;

  /**
   * Is this Drive entry an image?
   *
   * Trusts the mime when Drive supplies one, and falls back to the extension
   * when it doesn't — the keyless folder listing historically reported no mime
   * at all, which is what sent every image to the PDF viewer.
   */
  function isImage(mime, name) {
    if (mime && String(mime).startsWith('image/')) return true;
    return !mime && IMAGE_EXT.test(String(name || ''));
  }

  function authHeaders() {
    const token = localStorage.getItem('sable_token') || '';
    return token ? { Authorization: 'Bearer ' + token } : {};
  }

  /**
   * Fetch a Drive image and show it in Sable's lightbox.
   *
   * Bytes come from /raw, not /pdf, so they keep their real content type and
   * are never ingested into the library as a bogus PDF.
   */
  async function viewDriveImage(it) {
    const t = encodeURIComponent(it.name || 'image');
    const url = `/api/drive/file/${encodeURIComponent(it.id)}/raw?title=${t}`;
    try {
      const res = await fetch(url, { headers: authHeaders() });
      if (!res.ok) {
        let detail = res.statusText;
        try { detail = (await res.json()).detail || detail; } catch {}
        throw new Error(detail);
      }
      const buf = await res.arrayBuffer();
      const type = res.headers.get('Content-Type') || it.mimeType || 'application/octet-stream';
      const objUrl = URL.createObjectURL(new Blob([buf], { type }));
      if (window.openLightbox) window.openLightbox(objUrl);
      else window.open(objUrl, '_blank');
    } catch (e) {
      notify(e.message || 'Could not open that image');
    }
  }

  async function doImport() {
    const ids = [...selected];
    if (!ids.length) return;
    busyMsg = `Importing ${ids.length} file${ids.length > 1 ? 's' : ''}…`;
    rerender();
    try {
      // Send names along — keyless downloads sometimes lack a filename, and
      // the browser view already knows what each file is called.
      const names = {};
      for (const it of sourceItems) if (selected.has(it.id)) names[it.id] = it.name;
      const res = await api('/api/drive/import', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          ids,
          names,
          // Tag docs with their Drive folder so the library can group them.
          collection: activeSource ? activeSource.name : '',
          collection_id: activeSource ? activeSource.drive_id : '',
        }),
      });
      const ok = res.imported || 0;
      const bad = res.failed || 0;
      selected = new Set();
      busyMsg = '';
      rerender();
      if (ok) {
        notify(`Imported ${ok} file${ok > 1 ? 's' : ''} to your library ✓`);
        window.dispatchEvent(new CustomEvent('study-library-changed'));
      }
      if (bad) {
        const first = (res.results || []).find((r) => !r.ok);
        notify(`${bad} failed${first ? ': ' + first.error : ''}`);
      }
    } catch (e) {
      busyMsg = '';
      rerender();
      notify(e.message || 'Import failed');
    }
  }

  async function saveKey() {
    const el = $('driveApiKey');
    const val = el ? el.value.trim() : '';
    if (!val) return notify('Paste an API key first');
    try {
      await api('/api/drive/config', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ api_key: val }),
      });
      notify('API key saved ✓');
      statusLoaded = false;
      await loadStatus(true);
      rerender();
    } catch (e) { notify(e.message || 'Could not save key'); }
  }

  async function removeKey() {
    if (!confirm('Remove the saved API key? Public Drive links will stop working until you add one again.')) return;
    try {
      // null clears the stored key (save_drive_settings drops None values).
      await api('/api/drive/config', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ api_key: null }),
      });
      notify('API key removed');
      statusLoaded = false;
      await loadStatus(true);
      // No credentials left — drop out of manage mode so the full connect
      // screen (with its setup guide) is what the user sees.
      if (STATUS && !STATUS.connected) managing = false;
      rerender();
    } catch (e) { notify(e.message || 'Could not remove key'); }
  }

  async function saveClient() {
    const el = $('driveClientId');
    const val = el ? el.value.trim() : '';
    if (!val) return notify('Enter the OAuth Client ID');
    try {
      await api('/api/drive/config', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ oauth_client_id: val }),
      });
      notify('Client ID saved — now sign in');
      statusLoaded = false;
      await loadStatus(true);
      rerender();
    } catch (e) { notify(e.message || 'Could not save Client ID'); }
  }

  function oauthStart() {
    // Full-page navigation — no fetch, so the Bearer header can't be attached.
    // Pass the session token as a query param, which the auth middleware
    // accepts for this endpoint only.
    const token = localStorage.getItem('sable_token') || '';
    const q = token ? `?token=${encodeURIComponent(token)}` : '';
    window.location.href = `/api/drive/oauth/start${q}`;
  }

  async function disconnect() {
    if (!confirm('Disconnect Google Drive? Imported files stay in your library.')) return;
    try {
      const res = await api('/api/drive/oauth/disconnect', { method: 'POST' });
      if (res && res.revoked === false) {
        notify(res.warning || 'Disconnected locally — Google revoke failed');
      } else {
        notify('Disconnected');
      }
      statusLoaded = false;
      await loadStatus(true);
      activeSource = null; sourceItems = []; sourcePath = []; selected = new Set();
      truncatedFlag = false;
      rerender();
    } catch (e) { notify(e.message || 'Could not disconnect'); }
  }

  async function handleAction(action, el) {
    switch (action) {
      case 'save-key': return saveKey();
      case 'remove-key': return removeKey();
      case 'save-client': return saveClient();
      case 'oauth-start': return oauthStart();
      case 'disconnect': return disconnect();
      case 'manage': managing = true; setupOpen = false; return rerender();
      case 'close-manage': managing = false; return rerender();
      case 'toggle-setup': setupOpen = !setupOpen; return rerender();
      case 'toggle-add':
        addOpen = !addOpen;
        busyMsg = '';
        return rerender();
      case 'add-link': return addLink();
      case 'toggle-search':
        searchOpen = true; searchScope = null;
        searchQuery = ''; searchResults = null;
        return (function(){ rerender(); setTimeout(focusSearchInput, 0); })();
      case 'search-in-source':
        searchOpen = true;
        searchScope = activeSource ? { id: activeSource.id, name: activeSource.name } : null;
        searchQuery = ''; searchResults = null;
        return (function(){ rerender(); setTimeout(focusSearchInput, 0); })();
      case 'close-search':
        searchOpen = false; searchScope = null;
        searchQuery = ''; searchResults = null; searchBusy = false;
        return rerender();
      case 'open-result': return openSearchResult(el);
      case 'open-source': return openSource(el.dataset.id);
      case 'remove-source': {
        if (el && el.stopPropagation) el.stopPropagation();
        return removeSource(el.dataset.id);
      }
      case 'close-source': return closeSource();
      case 'reload-source': return reloadSource();
      case 'open-folder': return openFolder(el.dataset.id, el.dataset.name);
      case 'view-file':
        if (el && el.stopPropagation) el.stopPropagation();
        return viewFile({ id: el.dataset.id, name: el.dataset.name, mimeType: el.dataset.mime });
      case 'toggle-select': {
        const id = el.dataset.id;
        if (selected.has(id)) selected.delete(id); else selected.add(id);
        return rerender();
      }
      case 'clear-sel': selected = new Set(); return rerender();
      case 'import': return doImport();
      case 'crumb': {
        const i = Number(el.dataset.index);
        if (Number.isNaN(i)) return;
        sourcePath = sourcePath.slice(0, i + 1);
        return loadPath();
      }
      case 'noop': return;
      default: return;
    }
  }

  function rerender() {
    const c = $('studyContent');
    if (!c || !window.studyMode || window.studyMode.getTab() !== 'drive') return;
    c.innerHTML = render();
    if (window.lucide) window.lucide.createIcons();
  }

  // ── Init ──────────────────────────────────────────────────────────────────

  function init() {
    // Drive-wide search input + Enter key. Delegated so it survives rerenders.
    document.addEventListener('input', (e) => {
      const el = e.target;
      if (!el || el.id !== 'driveSearchInput') return;
      searchQuery = el.value;
      clearTimeout(searchDebounce);
      searchDebounce = setTimeout(runDriveSearch, 220);
    });
    document.addEventListener('keydown', (e) => {
      const el = e.target;
      if (!el || el.id !== 'driveSearchInput') return;
      if (e.key === 'Enter') { clearTimeout(searchDebounce); runDriveSearch(); }
      if (e.key === 'Escape') { searchQuery = ''; searchResults = null; rerender(); }
    });
    // Study tab click is wired by study.js; here we just prepare state and
    // react to a completed OAuth round-trip signalled via ?drive=ok|err.
    const params = new URLSearchParams(location.search);
    if (params.get('drive')) {
      const ok = params.get('drive') === 'ok';
      notify(params.get('msg') || (ok ? 'Google Drive connected ✓' : 'Drive connection failed'));
      statusLoaded = false;
      params.delete('drive'); params.delete('msg');
      const q = params.toString();
      history.replaceState({}, '', location.pathname + (q ? '?' + q : ''));
    }
  }

  /**
   * Open a saved source from elsewhere in the app (e.g. a folder card in the
   * Library). Switches to the Drive tab, then opens that source.
   *
   * NOTE: deliberately NOT named openFolder — that name is already taken by
   * the in-folder navigation above, and a duplicate declaration silently
   * shadows it (the later one wins), breaking folder navigation.
   */
  async function openSourceFromLibrary(sourceId) {
    if (!sourcesLoaded) await loadSources(true);
    if (window.studyMode && window.studyMode.setTab) {
      window.studyMode.setTab('drive');
    }
    await openSource(sourceId);
  }

  window.driveMode = {
    render,
    renderSidebar,
    handleAction,
    init,
    openFolder: openSourceFromLibrary,
    // Open one Drive file straight in the viewer — used by the rail search so
    // a hit there behaves the same as a hit inside the Drive tab.
    openFile: (it) => {
      if (window.studyMode && window.studyMode.setTab) window.studyMode.setTab('drive');
      viewFile(it);
    },
    ensureStatus: () => loadStatus(true),
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
