/* ── Unified Search Panel ──
 * Opens in chat area via search rail button.
 * Searches: messages, skills, memory, notes, todos, schedules, research, agents.
 * Each result is an expandable card. Chat/skill cards have "Open" button.
 */
(function () {
  const $ = (sel) => document.querySelector(sel);

  let isOpen = false;
  let debounceTimer = null;
  const _inlineContent = new Map(); // key: cardId -> full content string

  /* ── Source config: icon, label, color accent ── */
  const SOURCE_META = {
    message:  { icon: 'message-square', label: 'Chat Messages',  color: 'var(--accent)' },
    skill:    { icon: 'zap',            label: 'Skill Events',   color: '#f59e0b' },
    memory:   { icon: 'brain',          label: 'Memory',         color: '#8b5cf6' },
    note:     { icon: 'file-text',      label: 'Notes',          color: '#10b981' },
    todo:     { icon: 'check-square',   label: 'Todos',          color: '#ef4444' },
    schedule: { icon: 'calendar-days',  label: 'Schedules',      color: '#3b82f6' },
    research: { icon: 'flask-conical',  label: 'Research',       color: '#ec4899' },
    agent:    { icon: 'bot',            label: 'Agents',         color: '#06b6d4' },
  };

  /* ── Open / Close ── */
  function openSearch() {
    if (isOpen) return;
    isOpen = true;
    const chatEl = $('#chat');
    const inputEl = $('#inputArea');
    if (chatEl) { chatEl.classList.add('hidden'); chatEl.style.display = 'none'; }
    if (inputEl) { inputEl.classList.add('hidden'); inputEl.style.display = 'none'; }
    if (window.sidebarHost?.isTerminalOpen?.()) window.sidebarHost.closeTerminal();
    if (window.sidebarHost?.getCurrent?.()) window.sidebarHost.unhost();
    // Hide other main-area views. #studyView is position:absolute inset:0 with
    // z-index 20, so it MUST be in this list or the panel renders behind it.
    ['researchView', 'imageView', 'promptgenView', 'dashboardView', 'knowledgeView', 'calendarView', 'ocrView', 'studyView'].forEach(id => {
      const el = document.getElementById(id);
      if (el) { el.classList.add('hidden'); el.style.display = ''; }
    });
    document.body.classList.add('search-open');

    // Study mode gets the index strip + library-scoped search.
    syncStudyChrome();

    const view = $('#searchView');
    if (view) {
      view.classList.remove('hidden');
      const input = view.querySelector('#searchInput');
      if (input) setTimeout(() => input.focus(), 50);
    }
  }

  function closeSearch() {
    if (!isOpen) return;
    isOpen = false;
    document.body.classList.remove('search-open');
    docScope = null;
    const view = $('#searchView');
    if (view) { view.classList.add('hidden'); view.style.display = ''; }
    const chatEl = $('#chat');
    const inputEl = $('#inputArea');
    if (chatEl) { chatEl.classList.remove('hidden'); chatEl.style.display = ''; }
    if (inputEl) { inputEl.classList.remove('hidden'); inputEl.style.display = ''; }

    // Restore the study dashboard if that's still the active layout mode —
    // we hid it on open, and nothing else will bring it back.
    if (isStudy() && !window.sidebarHost?.getCurrent?.()) {
      const sv = document.getElementById('studyView');
      if (sv) sv.classList.remove('hidden');
    }
  }

  /* ── Study mode: index health strip + document search ─────────────────── */

  const isStudy = () => document.body.getAttribute('data-mode') === 'study';

  let indexStats = null;      // last /api/study/search/stats payload
  let docsExpanded = false;   // is the "not indexed" list open
  let docScope = null;        // { id, title } when searching one document

  function authHeaders() {
    let t = '';
    try { t = localStorage.getItem('sable_token') || ''; } catch { /* private mode */ }
    return t ? { Authorization: 'Bearer ' + t } : {};
  }

  async function studyApi(path, opts) {
    const res = await fetch(path, {
      ...(opts || {}),
      headers: { 'Content-Type': 'application/json', ...authHeaders(), ...((opts && opts.headers) || {}) },
    });
    if (!res.ok) throw new Error(res.statusText || ('HTTP ' + res.status));
    return res.json().catch(() => null);
  }

  async function loadIndexStats(force) {
    if (indexStats && !force) { renderIndexStrip(); return indexStats; }
    try {
      indexStats = await studyApi('/api/study/search/stats');
    } catch {
      indexStats = null;
    }
    renderIndexStrip();
    return indexStats;
  }

  function renderIndexStrip() {
    const strip = document.getElementById('searchIndexStrip');
    if (!strip) return;
    if (!isStudy()) { strip.classList.add('hidden'); strip.innerHTML = ''; return; }
    strip.classList.remove('hidden');

    if (!indexStats) {
      strip.innerHTML = '<span class="sis-chip static">Loading index…</span>';
      return;
    }

    const nOk = indexStats.search_indexed || 0;
    const nMiss = (indexStats.missing || []).length;
    const nAll = indexStats.docs || 0;

    let html = '';
    html += '<span class="sis-chip static ok"><i data-lucide="check-circle"></i>'
      + '<span class="sis-num">' + nOk + '</span> indexed</span>';
    html += '<button class="sis-chip warn' + (docsExpanded ? ' active' : '') + '" data-sis="toggle-missing"'
      + (nMiss ? '' : ' disabled') + '>'
      + '<i data-lucide="alert-circle"></i><span class="sis-num">' + nMiss + '</span> not indexed'
      + '<i data-lucide="' + (docsExpanded ? 'chevron-up' : 'chevron-down') + '"></i></button>';
    html += '<button class="sis-chip sis-run" data-sis="index-missing"'
      + (nMiss ? '' : ' disabled') + '><i data-lucide="zap"></i> Index all missing</button>';
    html += '<button class="sis-chip sis-run" data-sis="reindex-all"><i data-lucide="refresh-cw"></i> Rebuild all (' + nAll + ')</button>';

    if (docScope) {
      html += '<button class="sis-chip active" data-sis="clear-scope"><i data-lucide="x"></i> in: '
        + escapeHtml((docScope.title || 'document').slice(0, 30)) + '</button>';
    }

    html += '<div class="sis-doclist' + (docsExpanded ? '' : ' hidden') + '">';
    html += renderDocList();
    html += '</div>';
    strip.innerHTML = html;
    if (window.lucide) window.lucide.createIcons({ nodes: [strip] });
  }

  function renderDocList() {
    if (!indexStats) return '';
    const missing = indexStats.missing || [];
    const indexed = indexStats.indexed || [];
    // When the list is open we show the gap first, then what's already done —
    // the missing ones are the actionable half.
    const rows = [];

    for (const d of missing) {
      const badge = d.has_text
        ? '<span class="sis-doc-badge">not in index</span>'
        : '<span class="sis-doc-badge">needs OCR</span>';
      rows.push('<div class="sis-doc">'
        + '<i data-lucide="file-text"></i>'
        + '<span class="sis-doc-title">' + escapeHtml(d.title || 'Untitled') + '</span>'
        + badge
        + '<span class="sis-doc-pages">' + (d.pages || 0) + 'p</span>'
        + '<button class="sis-doc-btn" data-sis-doc="' + escapeAttr(d.id) + '" data-sis-title="' + escapeAttr(d.title || '') + '">'
        + '<i data-lucide="zap"></i> Index</button>'
        + '</div>');
    }

    for (const d of indexed) {
      rows.push('<div class="sis-doc">'
        + '<i data-lucide="file-text"></i>'
        + '<span class="sis-doc-title">' + escapeHtml(d.title || 'Untitled') + '</span>'
        + '<span class="sis-doc-badge ok">indexed</span>'
        + '<span class="sis-doc-pages">' + (d.pages || 0) + 'p</span>'
        + '<button class="sis-doc-btn" data-sis-search="' + escapeAttr(d.id) + '" data-sis-title="' + escapeAttr(d.title || '') + '">'
        + '<i data-lucide="search"></i> Search</button>'
        + '</div>');
    }

    if (!rows.length) return '<div class="sis-empty">Every document is indexed.</div>';
    return rows.join('');
  }

  function onIndexStripClick(e) {
    const actionEl = e.target.closest('[data-sis]');
    if (actionEl) {
      const a = actionEl.dataset.sis;
      if (a === 'toggle-missing') { docsExpanded = !docsExpanded; renderIndexStrip(); }
      else if (a === 'index-missing') runBulkIndex(true);
      else if (a === 'reindex-all') runBulkIndex(false);
      else if (a === 'clear-scope') {
        docScope = null;
        renderIndexStrip();
        const input = document.getElementById('searchInput');
        if (input) { input.placeholder = 'Search chats, skills, memory…'; input.focus(); }
        if (input && input.value.trim()) doSearch(input.value);
      }
      return true;
    }

    // Per-document Index → opens the existing OCR/AI modal from library-index.js
    const docBtn = e.target.closest('[data-sis-doc]');
    if (docBtn) {
      const id = docBtn.dataset.sisDoc;
      const title = docBtn.dataset.sisTitle || '';
      if (window.libraryIndex && window.libraryIndex.open) {
        window.libraryIndex.open(id, title);
      } else {
        alert('library-index.js is not loaded');
      }
      return true;
    }

    // Per-document Search → scope the query to that document
    const searchBtn = e.target.closest('[data-sis-search]');
    if (searchBtn) {
      docScope = { id: searchBtn.dataset.sisSearch, title: searchBtn.dataset.sisTitle || '' };
      const input = document.getElementById('searchInput');
      if (input) {
        input.placeholder = 'Search inside ' + (docScope.title || 'document') + '…';
        input.focus();
      }
      renderIndexStrip();
      if (input && input.value.trim()) doSearch(input.value);
      return true;
    }

    return false;
  }

  async function runBulkIndex(onlyMissing) {
    try {
      if (onlyMissing) {
        // Two halves: mirror text we already have into the index (fast), then
        // OCR/AI the docs that still need real work (slow, backgrounded).
        await studyApi('/api/study/search/reindex', {
          method: 'POST',
          body: JSON.stringify({ search: true, only_missing: true }),
        });
        await studyApi('/api/study/index/start', {
          method: 'POST',
          body: JSON.stringify({ all: true, only_missing: true, ocr: true, ai: false }),
        });
      } else {
        await studyApi('/api/study/search/reindex', {
          method: 'POST',
          body: JSON.stringify({ search: true }),
        });
      }
    } catch (err) {
      alert('Indexing failed: ' + err.message);
      return;
    }
    indexStats = null;
    await loadIndexStats(true);
  }

  function syncStudyChrome() {
    const strip = document.getElementById('searchIndexStrip');
    const input = document.getElementById('searchInput');
    if (!isStudy()) {
      if (strip) { strip.classList.add('hidden'); strip.innerHTML = ''; }
      docScope = null;
      if (input) input.placeholder = 'Search chats, skills, memory…';
      return;
    }
    if (input && !docScope) input.placeholder = 'Search chats, skills, memory & your documents…';
    loadIndexStats(false);
  }

  /* ── Search ── */
  async function doSearch(query) {
    const resultsEl = $('#searchResults');
    if (!resultsEl) return;
    if (!query.trim()) {
      resultsEl.innerHTML = '<div class="search-empty">Start typing to search across chats, skills, memory, notes, todos, schedules, research & agents</div>';
      return;
    }
    resultsEl.innerHTML = '<div class="search-loading"><i data-lucide="loader-circle" class="spin"></i> Searching…</div>';
    if (window.lucide) window.lucide.createIcons({ nodes: [resultsEl] });

    const study = isStudy();
    try {
      // In study mode fire both requests in parallel: the library full-text
      // index (documents, rendered first) and the usual unified search.
      const unifiedP = fetch('/api/search/unified?q=' + encodeURIComponent(query.trim()) + '&limit=20')
        .then(r => r.json());

      let docP = Promise.resolve(null);
      if (study) {
        const params = new URLSearchParams({ q: query.trim(), limit: '30' });
        if (docScope && docScope.id) params.set('doc_id', docScope.id);
        docP = studyApi('/api/study/search?' + params.toString()).catch(() => null);
      }

      const [data, docs] = await Promise.all([unifiedP, docP]);
      renderResults(data, query, docs);
    } catch (err) {
      resultsEl.innerHTML = '<div class="search-error">Search failed: ' + escapeHtml(err.message) + '</div>';
    }
  }

  /* ── Render grouped results ── */
  function renderResults(data, query, docs) {
    const el = $('#searchResults');
    if (!el) return;

    // Map internal source names to API response keys
    const SOURCE_KEYS = {
      message: 'messages', skill: 'skills', memory: 'memory',
      note: 'notes', todo: 'todos', schedule: 'schedules',
      research: 'research', agent: 'agents',
    };
    const sources = Object.keys(SOURCE_KEYS);
    const groups = [];
    let total = 0;
    for (const src of sources) {
      const items = data[SOURCE_KEYS[src]] || [];
      if (items.length) {
        groups.push({ source: src, items });
        total += items.length;
      }
    }

    const docHits = (docs && docs.hits) || [];
    const docTotal = (docs && docs.total) || 0;

    if (total === 0 && docTotal === 0) {
      el.innerHTML = '<div class="search-empty">No results for "' + escapeHtml(query) + '"</div>';
      return;
    }

    const parts = [];
    if (docTotal) parts.push(docTotal + ' in documents');
    if (total) parts.push(groups.map(g => g.items.length + ' ' + (SOURCE_META[g.source]?.label || g.source)).join(', '));
    let html = '<div class="search-summary">' + parts.join(' — ') + '</div>';

    // Documents come FIRST — they are the reason study mode exists.
    if (docTotal) html += renderDocsBlock(docs, query);

    for (const group of groups) {
      const meta = SOURCE_META[group.source] || { icon: 'circle', label: group.source, color: 'var(--muted)' };
      html += '<div class="search-section">';
      html += '<div class="search-section-header" style="--section-color:' + meta.color + '">';
      html += '<i data-lucide="' + meta.icon + '"></i> ' + meta.label;
      html += '<span class="search-section-count">' + group.items.length + '</span>';
      html += '</div>';

      for (const item of group.items.slice(0, 15)) {
        html += renderCard(item, group.source, query);
      }
      html += '</div>';
    }

    el.innerHTML = html;
    if (window.lucide) window.lucide.createIcons({ nodes: [el] });
    bindCardEvents(el, query);
  }

  /* Build the Documents section for a /api/study/search payload. Exported so
     the study sidebar search can render the SAME cards the rail search does. */
  function renderDocsBlock(docs, query) {
    const hits = (docs && docs.hits) || [];
    const total = (docs && docs.total) || 0;
    if (!total) return '';
    let html = '<div class="search-section study-docs">';
    html += '<div class="search-section-header">';
    html += '<i data-lucide="book-open"></i> Documents';
    html += '<span class="search-section-count">' + total + '</span>';
    html += '</div>';
    for (const hit of hits.slice(0, 25)) {
      html += '<div class="study-doc-hit" data-study-doc="' + escapeAttr(hit.doc_id || '') + '" data-study-page="' + (hit.page || 1) + '">'
        + '<span class="study-doc-hit-page">p' + (hit.page || 1) + '</span>'
        + '<div class="study-doc-hit-body">'
        + '<div class="study-doc-hit-title">' + highlightMatch(escapeHtml(hit.title || 'Untitled'), query) + '</div>'
        + '<div class="study-doc-hit-text">' + renderSnippet(hit.snippet, query) + '</div>'
        + '</div></div>';
    }
    html += '</div>';
    return html;
  }

  /* Run a full-text document search and resolve with the raw payload
     ({ hits, docs, total }) or null. No DOM side-effects — the caller
     decides where the results go. */
  async function runDocSearch(query, scopeId) {
    if (!query || !query.trim()) return null;
    const params = new URLSearchParams({ q: query.trim(), limit: '30' });
    const scope = scopeId != null ? scopeId : (docScope && docScope.id);
    if (scope) params.set('doc_id', scope);
    try { return await studyApi('/api/study/search?' + params.toString()); }
    catch { return null; }
  }

  /* FTS snippets arrive with <mark> already applied from the server, so the
     only escaping that is safe is tag-level: kill everything except <mark>. */
  function renderSnippet(raw, query) {
    if (!raw) return '';
    let s = String(raw)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/&lt;mark&gt;/g, '<mark>').replace(/&lt;\/mark&gt;/g, '</mark>');
    return s;
  }

  /* ── Render a single card ── */
  function renderCard(item, source, query) {
    const canOpen = source === 'message' || source === 'skill';
    // Memory uses key as title; others use title/skill
    const title = source === 'memory' ? (item.key || '') : (item.title || item.skill || '');
    const preview = item.preview || item.content || '';
    const time = formatTime(item.created_at || item.updated_at);
    const roleIcon = item.role === 'user' ? '👤 ' : item.role === 'assistant' ? '🤖 ' : '';
    const catBadge = item.category ? '<span class="search-badge">' + escapeHtml(item.category) + '</span>' : '';
    const dueBadge = item.due_date ? '<span class="search-badge search-due">📅 ' + escapeHtml(item.due_date) + '</span>' : '';
    const schedType = item.schedule_type ? '<span class="search-badge">' + escapeHtml(item.schedule_type) + '</span>' : '';
    const scoreTag = item.score ? '<span class="search-badge">' + Math.round(item.score * 100) + '%</span>' : '';

    let openBtn = '';
    if (canOpen && item.chat_id) {
      openBtn = '<button class="search-open-btn" data-chat-id="' + item.chat_id + '" data-msg-id="' + (item.id || item.message_id || '') + '" title="Open in chat"><i data-lucide="external-link"></i> Open</button>';
    }

    // Store full_content in JS map for memory (avoids HTML attribute escaping issues)
    const cardKey = source + ':' + (item.id || item.message_id || Math.random());
    if (item.full_content) _inlineContent.set(cardKey, item.full_content);

    return '<div class="search-card" data-source="' + source + '" data-id="' + (item.id || item.message_id || '') + '" data-chat-id="' + (item.chat_id || '') + '" data-message-id="' + (item.message_id || '') + '" data-card-key="' + cardKey + '">'
      + '<div class="search-card-head">'
      + '<span class="search-card-title">' + roleIcon + highlightMatch(escapeHtml(title), query) + '</span>'
      + '<span class="search-card-meta">' + catBadge + scoreTag + dueBadge + schedType + (time ? '<span class="search-card-time">' + time + '</span>' : '') + '</span>'
      + '</div>'
      + '<div class="search-card-preview">' + highlightMatch(escapeHtml(preview), query) + '</div>'
      + '<div class="search-card-expanded hidden"></div>'
      + '<div class="search-card-actions">'
      + openBtn
      + '</div>'
      + '</div>';
  }

  /* ── Bind click events after render ── */
  function bindCardEvents(container, query) {
    // Study document hit → open the reader at that exact page. The reader is
    // a study-mode overlay; search has to get out of the way first.
    container.querySelectorAll('.study-doc-hit').forEach(hitEl => {
      hitEl.addEventListener('click', () => {
        const docId = hitEl.dataset.studyDoc;
        const page = parseInt(hitEl.dataset.studyPage, 10) || 1;
        if (!docId || !window.studyMode) return;
        closeSearch();
        const railBtn = document.querySelector('.rail-btn[data-rail="search"]');
        if (railBtn) railBtn.classList.remove('active');
        window.studyMode.openDoc(docId, page);
      });
    });

    // Card click → expand/collapse full content
    container.querySelectorAll('.search-card').forEach(card => {
      card.addEventListener('click', (e) => {
        // Don't expand if clicking the Open button
        if (e.target.closest('.search-open-btn')) return;
        toggleExpand(card);
      });
    });

    // Open button → navigate to chat and scroll to message
    container.querySelectorAll('.search-open-btn').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        const card = btn.closest('.search-card');
        const chatId = btn.dataset.chatId || card?.dataset.chatId;
        const messageId = card?.dataset.messageId;
        if (!chatId) return;
        closeSearch();
        const railBtn = document.querySelector('.rail-btn[data-rail="search"]');
        if (railBtn) railBtn.classList.remove('active');
        window.dispatchEvent(new CustomEvent('rail-switch', { detail: { target: null } }));
        // Use the real selectChat function
        if (typeof window._sableSelectChat === 'function') {
          await window._sableSelectChat(chatId);
        }
        // Scroll to the target message and highlight it
        if (messageId) {
          requestAnimationFrame(() => {
            const msgEl = document.querySelector(`[data-message-id="${messageId}"]`);
            if (msgEl) {
              msgEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
              msgEl.classList.add('search-highlight-flash');
              setTimeout(() => msgEl.classList.remove('search-highlight-flash'), 2000);
            }
          });
        }
      });
    });
  }

  /* ── Expand card with full content ── */
  async function toggleExpand(card) {
    const expanded = card.querySelector('.search-card-expanded');
    if (!expanded) return;

    // Toggle off if already expanded
    if (!expanded.classList.contains('hidden')) {
      expanded.classList.add('hidden');
      expanded.innerHTML = '';
      card.classList.remove('expanded');
      return;
    }

    const source = card.dataset.source;

    expanded.classList.remove('hidden');
    card.classList.add('expanded');

    // Memory has inline full_content in JS map — no API call needed
    if (source === 'memory') {
      const cardKey = card.dataset.cardKey || '';
      const fullContent = _inlineContent.get(cardKey) || '';
      if (fullContent) {
        expanded.innerHTML = '<div class="search-full-content">' + formatContent(fullContent) + '</div>';
      } else {
        expanded.innerHTML = '<div class="search-expand-empty">No content available</div>';
      }
      return;
    }

    const id = card.dataset.id;
    const chatId = card.dataset.chatId;
    const messageId = card.dataset.messageId;

    expanded.innerHTML = '<div class="search-expand-loading">Loading…</div>';

    try {
      const params = new URLSearchParams({ source, id, chat_id: chatId, message_id: messageId });
      const res = await fetch('/api/search/full-content?' + params);
      const data = await res.json();
      if (data.error) {
        expanded.innerHTML = '<div class="search-expand-error">' + escapeHtml(data.error) + '</div>';
        return;
      }
      renderExpandedContent(expanded, data, source);
    } catch (err) {
      expanded.innerHTML = '<div class="search-expand-error">Failed to load</div>';
    }
  }

  /* ── Render expanded content based on source type ── */
  function renderExpandedContent(container, data, source) {
    if (source === 'message') {
      container.innerHTML = '<div class="search-full-content">' + formatContent(data.content || '') + '</div>';
    } else if (source === 'skill') {
      const events = data.events || [];
      if (!events.length) {
        container.innerHTML = '<div class="search-expand-empty">No skill event data</div>';
        return;
      }
      let html = '';
      for (const ev of events) {
        const name = ev.skill || ev.tool || ev.type || 'event';
        const body = ev.summary || ev.result || ev.output || JSON.stringify(ev, null, 2);
        const bodyStr = typeof body === 'object' ? JSON.stringify(body, null, 2) : String(body);
        html += '<div class="search-skill-event"><div class="search-skill-name">⚡ ' + escapeHtml(name) + '</div>'
          + '<pre class="search-skill-body">' + escapeHtml(bodyStr) + '</pre></div>';
      }
      container.innerHTML = html;
    } else if (source === 'note' || source === 'todo') {
      let html = '';
      if (data.content) html += '<div class="search-full-content">' + formatContent(data.content) + '</div>';
      if (data.items && data.items.length) {
        html += '<ul class="search-checklist">';
        for (const item of data.items) {
          const checked = item.done ? 'checked' : '';
          const text = typeof item === 'string' ? item : (item.text || '');
          html += '<li><input type="checkbox" disabled ' + checked + '> ' + escapeHtml(text) + '</li>';
        }
        html += '</ul>';
      }
      container.innerHTML = html || '<div class="search-expand-empty">Empty note</div>';
    } else if (source === 'schedule') {
      container.innerHTML = '<div class="search-schedule-detail">'
        + '<div><strong>Type:</strong> ' + escapeHtml(data.schedule_type || '') + '</div>'
        + '<div><strong>Time:</strong> ' + escapeHtml(data.time || '—') + '</div>'
        + '<div><strong>Description:</strong> ' + formatContent(data.description || '') + '</div>'
        + (data.day_of_week != null ? '<div><strong>Day:</strong> ' + dayName(data.day_of_week) + '</div>' : '')
        + (data.start_date ? '<div><strong>Start:</strong> ' + escapeHtml(data.start_date) + '</div>' : '')
        + (data.end_date ? '<div><strong>End:</strong> ' + escapeHtml(data.end_date) + '</div>' : '')
        + '</div>';
    } else if (source === 'research' || source === 'agent') {
      container.innerHTML = '<div class="search-full-content search-md-content">' + formatContent(data.content || '') + '</div>';
    } else if (source === 'memory') {
      container.innerHTML = '<div class="search-full-content">' + formatContent(data.content || '') + '</div>';
    } else {
      container.innerHTML = '<div class="search-expand-empty">No expandable content</div>';
    }
  }

  /* ── Helpers ── */
  function escapeHtml(str) {
    if (!str) return '';
    return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function escapeAttr(str) {
    if (!str) return '';
    return String(str).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/\n/g, '&#10;').replace(/\r/g, '');
  }

  function highlightMatch(text, query) {
    if (!query) return text;
    const escaped = query.replace(new RegExp('[.*+?^${}()|[\]\\]', 'g'), '\$&');
    const re = new RegExp('(' + escaped + ')', 'gi');
    return text.replace(re, '<mark class="search-highlight">$1</mark>');
  }

  function formatContent(text) {
    // Basic markdown-ish: preserve newlines, escape HTML
    return escapeHtml(text).replace(/\n/g, '<br>');
  }

  function formatTime(ts) {
    if (!ts) return '';
    try {
      const d = new Date(ts);
      const now = new Date();
      const diff = now - d;
      if (diff < 60000) return 'just now';
      if (diff < 3600000) return Math.floor(diff / 60000) + 'm ago';
      if (diff < 86400000) return Math.floor(diff / 3600000) + 'h ago';
      if (diff < 604800000) return Math.floor(diff / 86400000) + 'd ago';
      return d.toLocaleDateString();
    } catch { return ''; }
  }

  function dayName(n) {
    return ['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'][n] || String(n);
  }

  /* ── Init ── */
  function init() {
    window.addEventListener('rail-switch', (e) => {
      const target = e.detail?.target;
      if (target === 'search') {
        if (isOpen) closeSearch();
        else openSearch();
      } else if (isOpen) {
        closeSearch();
      }
    });

    document.addEventListener('input', (e) => {
      if (e.target.id !== 'searchInput') return;
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(() => doSearch(e.target.value), 250);
    });

    // Index strip: chips + per-document buttons.
    document.addEventListener('click', (e) => {
      if (!e.target.closest('#searchIndexStrip')) return;
      onIndexStripClick(e);
    });

    // Mode switches change whether the strip applies at all. The study grid
    // also dispatches study-index-changed when a job finishes, which should
    // refresh the counts without the user reloading.
    window.addEventListener('sable-mode-changed', () => { if (isOpen) syncStudyChrome(); });
    window.addEventListener('study-index-changed', () => {
      if (!isOpen || !isStudy()) return;
      indexStats = null;
      loadIndexStats(true);
    });

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && isOpen) {
        closeSearch();
        const railBtn = document.querySelector('.rail-btn[data-rail="search"]');
        if (railBtn) railBtn.classList.remove('active');
      }
    });
  }

  /**
   * Open the panel scoped to one document — the reader's "find" button.
   * Replaces study-search.js's old searchIn(), which needed a tab that no
   * longer exists.
   */
  function searchIn(docId, title) {
    docScope = docId ? { id: docId, title: title || '' } : null;

    const railBtn = document.querySelector('.rail-btn[data-rail="search"]');
    if (!isOpen) {
      openSearch();
      if (railBtn) railBtn.classList.add('active');
    } else {
      syncStudyChrome();
    }

    renderIndexStrip();

    const input = document.getElementById('searchInput');
    if (!input) return;
    input.value = '';
    input.placeholder = docScope
      ? ('Search inside ' + (docScope.title || 'document') + '…')
      : 'Search chats, skills, memory…';
    setTimeout(() => input.focus(), 60);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  // Exported so the study reader (and anything else) can drive the panel.
  window.searchPanel = {
    open: openSearch,
    close: closeSearch,
    searchIn,
    setScope(docId, title) {
      docScope = docId ? { id: docId, title: title || '' } : null;
      renderIndexStrip();
    },
    refreshIndex() {
      indexStats = null;
      return loadIndexStats(true);
    },
    isOpen: () => isOpen,
    // Study sidebar search reuses these so both search boxes render identical
    // document cards and hit the same FTS endpoint.
    renderDocsBlock,
    runDocSearch,
    bindDocHits(container) {
      if (container) bindCardEvents(container, '');
    },
  };
})();
