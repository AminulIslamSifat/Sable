/**
 * study-search.js — full-text search across the whole study library.
 *
 * Backed by /api/study/search, which indexes the extracted page text of every
 * document (SQLite FTS5, with a LIKE fallback). Results are grouped by
 * document with highlighted snippets; clicking a hit opens the reader at that
 * exact page.
 *
 * Also owns the "search inside this document" entry used from the reader.
 *
 * Exposes window.studySearch = { renderTab, renderSidebar, handleClick, focus }.
 */
(function () {
  'use strict';

  let query = '';
  let results = null;      // last response payload
  let busy = false;
  let scopeDoc = '';       // when set, search is limited to one document
  let scopeTitle = '';
  let stats = null;
  let debounce = null;

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  function token() {
    try { return localStorage.getItem('sable_token') || ''; } catch { return ''; }
  }

  async function api(path, opts) {
    const res = await fetch(path, {
      ...(opts || {}),
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token()}`,
        ...((opts && opts.headers) || {}),
      },
    });
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch { /* keep */ }
      throw new Error(detail);
    }
    return res.status === 204 ? null : res.json().catch(() => null);
  }

  function toast(msg) {
    if (window.studyMode && window.studyMode.toast) return window.studyMode.toast(msg);
    if (typeof window.showToast === 'function') return window.showToast(msg);
    console.log('[study-search]', msg);
  }

  // Snippets arrive with <mark> tags from the FTS highlighter; everything else
  // in the payload is user text and must be escaped.
  function snippet(html) {
    return String(html || '')
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/&lt;mark&gt;/g, '<mark>').replace(/&lt;\/mark&gt;/g, '</mark>');
  }

  function renderTab() {
    if (!stats) loadStats();
    return `
      <div class="ss-pane">
        <div class="ss-head">
          <div class="ss-searchbox">
            <i data-lucide="search" class="icon-lucide"></i>
            <input id="ssInput" type="search" placeholder="Search every page of every document…"
                   value="${esc(query)}" autocomplete="off" spellcheck="false">
            ${scopeDoc ? `<button class="ss-scope" data-ss-action="clear-scope" title="Search whole library">in: ${esc(scopeTitle || 'document')} <i data-lucide="x" class="icon-lucide"></i></button>` : ''}
          </div>
          ${stats ? `<div class="ss-stats">${stats.search_indexed || 0} of ${stats.docs || 0} documents indexed${stats.search_indexed < stats.docs ? ` · <button class="ss-link" data-ss-action="reindex">build index</button>` : ''}</div>` : ''}
        </div>
        <div class="ss-body" id="ssBody">${body()}</div>
      </div>`;
  }

  function body() {
    if (busy && !results) return '<div class="ss-empty">Searching…</div>';
    if (!query.trim()) {
      return `
        <div class="study-empty">
          <div class="study-empty-icon">🔎</div>
          <h3>Search your library</h3>
          <p>Find any phrase across the extracted text of every document — results link straight to the page.</p>
        </div>`;
    }
    if (!results) return '';

    const hits = results.hits || [];
    if (!hits.length) {
      return `<div class="ss-empty">No matches for <strong>${esc(query)}</strong>.</div>`;
    }

    const groups = (results.docs || []).map((doc) => {
      const docHits = hits.filter((h) => h.doc_id === doc.doc_id);
      return `
        <div class="ss-group">
          <div class="ss-group-head" data-ss-doc="${doc.doc_id}">
            <i data-lucide="file-text" class="icon-lucide"></i>
            <span class="ss-group-title">${esc(doc.title || 'Untitled')}</span>
            <span class="ss-group-count">${doc.count}</span>
          </div>
          ${docHits.map((hit) => `
            <div class="ss-hit" data-ss-doc="${hit.doc_id}" data-ss-page="${hit.page}" role="button" tabindex="0">
              <span class="ss-hit-page">p${hit.page}</span>
              <span class="ss-hit-text">${snippet(hit.snippet)}</span>
            </div>`).join('')}
        </div>`;
    }).join('');

    return `
      <div class="ss-summary">${results.total} match${results.total === 1 ? '' : 'es'} across ${(results.docs || []).length} document${(results.docs || []).length === 1 ? '' : 's'}</div>
      <div class="ss-results">${groups}</div>`;
  }

  function renderSidebar() {
    return `
      <div class="study-sidebar-section">
        <div class="study-sidebar-label">Search</div>
        <button class="study-sidebar-item active"><i data-lucide="search" class="icon-lucide"></i> All documents</button>
        ${scopeDoc ? `<button class="study-sidebar-item" data-ss-action="clear-scope"><i data-lucide="book-open" class="icon-lucide"></i> This document only</button>` : ''}
      </div>
      ${results && (results.docs || []).length ? `
      <div class="study-sidebar-section">
        <div class="study-sidebar-label">In these documents</div>
        ${(results.docs || []).slice(0, 12).map((doc) => `
          <button class="study-sidebar-item" data-ss-doc="${doc.doc_id}">
            <i data-lucide="file-text" class="icon-lucide"></i> ${esc((doc.title || 'Untitled').slice(0, 28))}
            <span class="sidebar-count">${doc.count}</span>
          </button>`).join('')}
      </div>` : ''}
      <div class="study-sidebar-section">
        <div class="study-sidebar-label">Index</div>
        <button class="study-sidebar-item" data-ss-action="reindex"><i data-lucide="refresh-cw" class="icon-lucide"></i> Rebuild search index</button>
        <button class="study-sidebar-item" data-ss-action="reindex-rag"><i data-lucide="brain" class="icon-lucide"></i> Rebuild AI index</button>
      </div>`;
  }

  async function loadStats() {
    try {
      stats = await api('/api/study/search/stats');
    } catch {
      stats = null;
    }
    refresh();
  }

  async function runSearch() {
    if (!query.trim()) { results = null; refresh(); return; }
    busy = true;
    try {
      const params = new URLSearchParams({ q: query, limit: '60' });
      if (scopeDoc) params.set('doc_id', scopeDoc);
      results = await api(`/api/study/search?${params.toString()}`);
    } catch (err) {
      toast(err.message);
      results = null;
    } finally {
      busy = false;
      refresh();
    }
  }

  function onInput(value) {
    query = value;
    if (debounce) clearTimeout(debounce);
    debounce = setTimeout(runSearch, 220);
  }

  async function reindex(rag) {
    toast(rag ? 'Rebuilding AI index…' : 'Rebuilding search index…');
    try {
      const res = await api('/api/study/search/reindex', {
        method: 'POST',
        body: JSON.stringify({ search: !rag, rag: !!rag }),
      });
      toast(rag ? `AI index rebuilt (${(res.rag || {}).indexed || 0} docs)` : `Search index rebuilt (${(res.search || {}).indexed || 0} docs)`);
      stats = null;
      loadStats();
      if (query.trim()) runSearch();
    } catch (err) {
      toast(err.message);
    }
  }

  // Repaint ONLY the results region. A full studyMode.refreshTab() here would
  // re-create #ssInput on every debounced search and steal focus mid-typing.
  function refresh() {
    if (!(window.studyMode && window.studyMode.getTab() === 'search')) return;

    const bodyEl = document.getElementById('ssBody');
    if (!bodyEl) return; // pane isn't mounted (reader/editor owns it) — skip

    bodyEl.innerHTML = body();
    if (window.lucide) window.lucide.createIcons();
    // Sidebar's "in these documents" list is derived from results; refresh it
    // on its own — it lives outside the content pane, so focus is untouched.
    if (window.studyMode.refreshSidebar) window.studyMode.refreshSidebar();

    const input = document.getElementById('ssInput');
    if (input && input.value !== query) input.value = query;
  }

  function handleClick(e) {
    const actionEl = e.target.closest('[data-ss-action]');
    if (actionEl) {
      const action = actionEl.dataset.ssAction;
      if (action === 'reindex') reindex(false);
      else if (action === 'reindex-rag') reindex(true);
      else if (action === 'clear-scope') { scopeDoc = ''; scopeTitle = ''; runSearch(); }
      return true;
    }

    const hit = e.target.closest('[data-ss-doc]');
    if (hit) {
      const docId = hit.dataset.ssDoc;
      const page = parseInt(hit.dataset.ssPage, 10);
      if (window.studyMode && window.studyMode.openDoc) {
        window.studyMode.openDoc(docId, Number.isFinite(page) ? page : 1);
      }
      return true;
    }
    return false;
  }

  // Bound once, on the content pane, so typing keeps focus across repaints.
  document.addEventListener('input', (e) => {
    if (e.target && e.target.id === 'ssInput') onInput(e.target.value);
  });
  document.addEventListener('keydown', (e) => {
    if (e.target && e.target.id === 'ssInput' && e.key === 'Enter') {
      e.preventDefault();
      if (debounce) clearTimeout(debounce);
      runSearch();
    }
  });

  window.studySearch = {
    renderTab,
    renderSidebar,
    handleClick,
    focus() { refresh(); const i = document.getElementById('ssInput'); if (i) i.focus(); },
    // Used by the reader's in-document search box.
    setScope(docId, title) { scopeDoc = docId || ''; scopeTitle = title || ''; query = ''; results = null; },
    searchIn(docId, title) {
      scopeDoc = docId || '';
      scopeTitle = title || '';
      query = '';
      results = null;
      if (window.studyMode) window.studyMode.setTab('search');
      setTimeout(() => { const i = document.getElementById('ssInput'); if (i) i.focus(); }, 40);
    },
    setQuery(q) { query = q; runSearch(); },
    run: runSearch,
  };
})();
