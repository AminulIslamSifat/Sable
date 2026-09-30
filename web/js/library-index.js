/**
 * library-index.js — resumable OCR + AI indexing UI for the study library.
 *
 * Backs onto /api/study/index/*. The OCR pass runs automatically on upload for
 * blank (scanned) pages; this module is the *manual* surface where the user
 * can re-run OCR, and optionally kick off the AI (embedding) pass with a
 * chosen image-capable model + thinking mode.
 *
 * Exposes: window.libraryIndex = { open(docId, title), statusFor(docId) }
 */
(function () {
  'use strict';

  const authHeaders = () => {
    const t = localStorage.getItem('sable_token') || '';
    return t ? { Authorization: 'Bearer ' + t } : {};
  };

  const toast = (msg) => {
    if (window.showToast) window.showToast(msg);
    else console.log('[library-index]', msg);
  };

  async function api(path, opts) {
    const res = await fetch(path, {
      ...(opts || {}),
      headers: { ...authHeaders(), ...((opts && opts.headers) || {}) },
    });
    if (!res.ok) throw new Error(res.statusText || ('HTTP ' + res.status));
    return res.json().catch(() => null);
  }

  /** Cache of docId → status so cards can render a badge without N fetches. */
  const _status = new Map();

  // Declared before loadAllStatuses because every status refresh also decides
  // whether the live poll should be running.
  let _timer = null;

  function syncPolling() {
    const running = [..._status.values()].some((s) => s && s.status === 'running');
    if (running && !_timer) {
      _timer = setInterval(async () => {
        await loadAllStatuses();
        window.dispatchEvent(new CustomEvent('study-index-changed'));
      }, 2000);
    } else if (!running && _timer) {
      clearInterval(_timer);
      _timer = null;
    }
  }

  async function loadAllStatuses() {
    try {
      const data = await api('/api/study/index/status');
      const jobs = (data && data.jobs) || [];
      _status.clear();
      for (const j of jobs) _status.set(j.doc_id, j);
    } catch { /* badges are cosmetic */ }
    // Poll only while something is genuinely running; an idle library must not
    // hammer the endpoint forever.
    syncPolling();
    return _status;
  }

  function statusFor(docId) {
    return _status.get(docId) || null;
  }

  const STAGE_LABEL = { ocr: 'OCR', search: 'Search', ai: 'AI' };

  /** Small badge HTML for a document card, or '' when nothing to show. */
  function badgeFor(docId) {
    const s = statusFor(docId);
    if (!s) return '';
    const state = s.state || s.status;
    // One overall figure across every stage the job planned, so the badge
    // doesn't snap back to 0% when OCR hands off to the embedding pass.
    const pct = s.percent || 0;
    if (state === 'running') {
      return `<span class="li-badge running" title="Indexing…">◐ ${pct}%</span>`;
    }
    if (state === 'paused') return `<span class="li-badge paused" title="Indexing paused">⏸ index</span>`;
    if (state === 'error') return `<span class="li-badge error" title="Indexing failed">⚠ index</span>`;
    if (state === 'indexed') return `<span class="li-badge done" title="Indexed">✓ index</span>`;
    // Searchable but no embeddings yet. Worth a badge because search and
    // document Q&A behave differently here, and the fix is one click away.
    if (state === 'partial') return `<span class="li-badge partial" title="Search only — no AI index">◑ search</span>`;
    // Deliberately nothing for 'unindexed': a card with no badge reads as
    // "nothing to report", which is truer than "0% indexed". The Indexing tab
    // is where unindexed documents are listed and can be fixed.
    return '';
  }

  /** Per-stage breakdown line: "OCR ✓ · Search 12/40 · AI 3/88".
   *
   * A finished stage shows a tick rather than its counters, because a stage
   * with nothing to do (OCR on a text PDF) legitimately reads 0/0 and would
   * otherwise look stuck.
   */
  function stageBreakdown(s) {
    const done = new Set(s.completed || []);
    const plan = (s.pipeline && s.pipeline.length)
      ? s.pipeline
      : ['ocr', 'search', 'ai'].filter((k) => (s[`${k}_total`] || 0) > 0);
    return plan.map((stage) => {
      const label = STAGE_LABEL[stage] || stage.toUpperCase();
      if (done.has(stage)) return `${label} ✓`;
      if (stage === 'ocr') return `OCR ${s.ocr_done}/${s.ocr_total}`;
      if (stage === 'search') return `Search ${s.search_done}/${s.search_total}`;
      if (stage === 'ai') return `AI ${s.ai_cursor}/${s.ai_total}`;
      return label;
    }).join(' · ');
  }

  /** Image-capable models only — the AI pass reads page images. */
  async function loadImageModels() {
    try {
      const data = await api('/api/models');
      const models = (data && data.models) || [];
      return models.filter((m) => (m.capabilities || {}).image);
    } catch {
      return [];
    }
  }

  // ── Modal ──────────────────────────────────────────────────────────────────

  let _overlay = null;
  let _es = null;   // active EventSource (SSE) — closed on modal close

  function close() {
    if (_es) { try { _es.close(); } catch {} _es = null; }
    if (_overlay) { _overlay.remove(); _overlay = null; }
  }

  async function open(docId, title) {
    close();
    const overlay = document.createElement('div');
    overlay.className = 'study-modal-overlay';
    overlay.innerHTML = `
      <div class="study-modal li-modal" role="dialog" aria-modal="true">
        <div class="study-modal-head">
          <h3>Index document</h3>
          <button class="study-modal-x" data-li-close>&times;</button>
        </div>
        <p class="study-modal-doc">${escapeHtml(title || docId)}</p>

        <label class="li-check">
          <input type="checkbox" data-li-ocr checked>
          <span>OCR blank / scanned pages <em>(BanglaOCR → SableOCR fallback, 5s/page)</em></span>
        </label>

        <label class="li-check">
          <input type="checkbox" data-li-aiocr>
          <span>AI OCR <em>(vision model — overrides the OCR chain above)</em></span>
        </label>

        <label class="li-check">
          <input type="checkbox" data-li-search checked>
          <span>Search index <em>(full-text search over the library)</em></span>
        </label>

        <label class="li-check">
          <input type="checkbox" data-li-ai>
          <span>AI pass (embed chunks for document Q&amp;A) <em>(local fastembed — no model needed)</em></span>
        </label>

        <div class="li-ai-fields" data-li-aifields hidden>
          <label class="study-field">
            <span>Vision model</span>
            <select data-li-model></select>
          </label>
          <label class="study-field">
            <span>Thinking mode</span>
            <select data-li-thinking></select>
          </label>
        </div>

        <div class="li-progress" data-li-progress hidden>
          <div class="li-bar"><div class="li-bar-fill" data-li-fill></div></div>
          <div class="li-progress-text" data-li-ptext>Ready</div>
        </div>

        <div class="study-modal-actions">
          <button class="study-room-action secondary" data-li-close>Close</button>
          <button class="study-room-action secondary li-stop-modal" data-li-stop-run hidden>Stop</button>
          <button class="study-room-action primary" data-li-run>Start indexing</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    _overlay = overlay;
    if (window.lucide) window.lucide.createIcons();

    const $ = (sel) => overlay.querySelector(sel);
    overlay.querySelectorAll('[data-li-close]').forEach((b) => (b.onclick = close));
    overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });

    const aiCheck = $('[data-li-ai]');
    const aiOcrCheck = $('[data-li-aiocr]');
    const ocrCheck = $('[data-li-ocr]');
    const aiFields = $('[data-li-aifields]');
    const modelSel = $('[data-li-model]');
    const thinkSel = $('[data-li-thinking]');

    // Populate the model picker lazily — only image-capable models.
    const models = await loadImageModels();
    if (!models.length) {
      modelSel.innerHTML = '<option value="">No vision models available</option>';
    } else {
      modelSel.innerHTML = models.map((m) => `<option value="${escapeHtml(m.id)}">${escapeHtml(m.label || m.id)}</option>`).join('');
      populateThinking(models[0]);
    }
    function populateThinking(m) {
      const modes = (m && m.thinking_modes) || [];
      thinkSel.innerHTML = modes.length
        ? modes.map((t) => `<option value="${escapeHtml(t.id)}">${escapeHtml(t.label || t.id)}</option>`).join('')
        : '<option value="">Default</option>';
    }
    modelSel.addEventListener('change', () => {
      const m = models.find((x) => x.id === modelSel.value);
      populateThinking(m);
    });
    // The vision model + thinking mode fields are ONLY for AI OCR. The
    // embedding AI pass runs on the local fastembed model and ignores the
    // model entirely, so showing these for it was a lie.
    //
    // AI OCR is a FULL OCR pass — it makes the plain "OCR blank pages" box
    // irrelevant, so that box is forced-checked and disabled while AI OCR is on.
    const syncAiFields = () => {
      const on = aiOcrCheck.checked;
      aiFields.hidden = !on;
      ocrCheck.disabled = on;
      if (on) ocrCheck.checked = true;
    };
    aiOcrCheck.addEventListener('change', syncAiFields);
    syncAiFields();

    const progress = $('[data-li-progress]');
    const fill = $('[data-li-fill]');
    const ptext = $('[data-li-ptext]');
    const runBtn = $('[data-li-run]');
    const stopBtn = $('[data-li-stop-run]');

    // The modal lives on <body>, outside the study content pane, so it cannot
    // rely on the delegated handler in study.js — wire its Stop directly.
    stopBtn.onclick = async () => {
      stopBtn.disabled = true;
      ptext.textContent = 'Stopping…';
      try {
        await api('/api/study/index/stop', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ doc_id: docId }),
        });
      } catch { /* the stream will report the outcome */ }
    };

    // Seed the progress bar from any saved job state (resume preview).
    try {
      const s = await api('/api/study/index/status?doc_id=' + encodeURIComponent(docId));
      if (s && (s.ocr_total || s.ai_total)) {
        progress.hidden = false;
        ptext.textContent = `Resuming — OCR ${s.ocr_done}/${s.ocr_total}, AI ${s.ai_cursor}/${s.ai_total}`;
      }
    } catch { /* fresh doc */ }

    runBtn.onclick = () => {
      const aiOcr = aiOcrCheck.checked;
      const body = {
        doc_id: docId,
        ocr: $('[data-li-ocr]').checked,
        search: $('[data-li-search]').checked,
        ai: aiCheck.checked,
        // AI OCR overrides the default OCR chain; it rides the OCR pass but
        // swaps the engine to the chosen vision model. The embedding AI pass
        // does NOT take a model — it runs on local fastembed.
        ai_ocr: aiOcr,
        model: aiOcr ? modelSel.value : '',
        thinking_mode: aiOcr ? thinkSel.value : '',
      };
      if (aiOcr && !modelSel.value) { toast('Pick a vision model for AI OCR'); return; }
      if (!body.ocr && !body.search && !body.ai) { toast('Pick at least one pass'); return; }
      startStream(body, { progress, fill, ptext, runBtn, stopBtn });
    };
  }

  function startStream(body, ui) {
    if (_es) { try { _es.close(); } catch {} }
    ui.progress.hidden = false;
    ui.runBtn.disabled = true;
    ui.runBtn.textContent = 'Indexing…';
    ui.ptext.textContent = 'Starting…';
    if (ui.stopBtn) { ui.stopBtn.hidden = false; ui.stopBtn.disabled = false; }

    // EventSource can't POST, so drive it through fetch + ReadableStream.
    const token = localStorage.getItem('sable_token') || '';
    fetch('/api/study/index/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: 'Bearer ' + token } : {}) },
      body: JSON.stringify(body),
    }).then(async (res) => {
      if (!res.ok || !res.body) throw new Error('stream failed');
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = '';
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const parts = buf.split('\n\n');
        buf = parts.pop();
        for (const part of parts) {
          const line = part.split('\n').find((l) => l.startsWith('data:'));
          if (!line) continue;
          let evt;
          try { evt = JSON.parse(line.slice(5).trim()); } catch { continue; }
          handleEvent(evt, ui);
        }
      }
    }).catch((err) => {
      ui.ptext.textContent = 'Failed: ' + err.message;
      ui.runBtn.disabled = false;
      ui.runBtn.textContent = 'Start indexing';
    });
  }

  function handleEvent(evt, ui) {
    if (evt.type === 'init') {
      ui.ptext.textContent = 'Indexing…';
      return;
    }
    if (evt.type === 'progress') {
      const done = evt.done || 0;
      const total = evt.total || 0;
      const pct = total ? Math.round((done / total) * 100) : 0;
      ui.fill.style.width = pct + '%';
      const stage = evt.stage === 'ai' ? 'AI' : 'OCR';
      const pageTag = evt.page ? ` · page ${evt.page}` : '';
      ui.ptext.textContent = `${stage}: ${done}/${total} (${pct}%)${pageTag}`;
      return;
    }
    if (evt.type === 'complete') {
      ui.fill.style.width = '100%';
      ui.ptext.textContent = 'Indexing complete ✓';
      ui.runBtn.disabled = false;
      ui.runBtn.textContent = 'Re-run';
      if (ui.stopBtn) ui.stopBtn.hidden = true;
      toast('Indexing complete ✓');
      loadAllStatuses().then(() => window.dispatchEvent(new CustomEvent('study-index-changed')));
      return;
    }
    if (evt.type === 'error') {
      ui.ptext.textContent = 'Error: ' + (evt.message || 'unknown');
      ui.runBtn.disabled = false;
      ui.runBtn.textContent = 'Retry';
      if (ui.stopBtn) ui.stopBtn.hidden = true;
      toast('Indexing failed: ' + (evt.message || 'unknown'));
    }
  }

  /** Index inspector: show the REAL extracted text + chunks for one doc. */
  async function inspect(docId, title) {
    if (_es) { try { _es.close(); } catch {} _es = null; }
    if (_overlay) { _overlay.remove(); _overlay = null; }

    const overlay = document.createElement('div');
    overlay.className = 'study-modal-overlay';
    overlay.innerHTML = `
      <div class="study-modal li-inspect" role="dialog" aria-modal="true">
        <div class="study-modal-head">
          <h3>Indexed output</h3>
          <button class="study-modal-x" data-li-close>&times;</button>
        </div>
        <p class="study-modal-doc">${escapeHtml(title || docId)}</p>
        <div class="li-inspect-body" data-li-inspect-body>
          <p class="li-inspect-loading">Loading…</p>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    _overlay = overlay;
    if (window.lucide) window.lucide.createIcons();
    overlay.querySelectorAll('[data-li-close]').forEach((b) => (b.onclick = () => {
      overlay.remove(); if (_overlay === overlay) _overlay = null;
    }));
    overlay.addEventListener('click', (e) => {
      if (e.target === overlay) { overlay.remove(); if (_overlay === overlay) _overlay = null; }
    });

    let data;
    try {
      data = await api('/api/study/index/inspect/' + encodeURIComponent(docId));
    } catch (err) {
      overlay.querySelector('[data-li-inspect-body]').innerHTML =
        `<p class="li-inspect-err">Failed to load: ${escapeHtml(err.message)}</p>`;
      return;
    }

    const pages = data.page_text || [];
    const chunks = data.chunks || [];
    const emptyPages = pages.filter((p) => !p.chars).length;
    const totalChars = pages.reduce((n, p) => n + (p.chars || 0), 0);

    const health = [
      `state: ${escapeHtml(data.state || '?')}`,
      `engine: ${escapeHtml(data.ocr_engine || 'default')}`,
      `${pages.length} pages`,
      `${emptyPages} empty`,
      `${totalChars.toLocaleString()} chars`,
      `${chunks.length} chunks`,
      data.stale ? '⚠ STALE EMBEDDINGS' : 'embeddings fresh',
    ].join(' · ');

    const pageHtml = pages.map((p) => `
      <details class="li-inspect-page">
        <summary>Page ${p.page} <span class="li-inspect-meta">${p.chars} chars</span></summary>
        <pre class="li-inspect-text">${escapeHtml(p.text || '(empty)')}</pre>
      </details>`).join('');

    const chunkHtml = chunks.map((c, i) => `
      <details class="li-inspect-page">
        <summary>Chunk ${i} <span class="li-inspect-meta">p${c.page ?? '?'} · ${c.chars} chars</span></summary>
        <pre class="li-inspect-text">${escapeHtml(c.text || '(empty)')}</pre>
      </details>`).join('') || '<p class="li-inspect-empty">No embedding chunks — run the AI pass.</p>';

    overlay.querySelector('[data-li-inspect-body]').innerHTML = `
      <div class="li-inspect-health">${escapeHtml(health)}</div>
      <h4 class="li-inspect-h">Page text</h4>
      <div class="li-inspect-pages">${pageHtml || '<p class="li-inspect-empty">No page text.</p>'}</div>
      <h4 class="li-inspect-h">Embedding chunks</h4>
      <div class="li-inspect-pages">${chunkHtml}</div>`;
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  // ── Indexing panel (the study "Indexing" tab) ─────────────────────────────

  // Keyed by the server's derived `state`, not the raw job status. A document
  // that was never indexed has no job at all, so the job status alone cannot
  // describe it — `state` folds in the actual search/embedding indexes.
  const STATE_LABEL = {
    running: 'Indexing',
    paused: 'Paused',
    error: 'Failed',
    indexed: 'Indexed',
    partial: 'Search only',
    unindexed: 'Not indexed',
  };

  function fmtAgo(ts) {
    if (!ts) return '';
    const secs = Math.max(0, Math.round(Date.now() / 1000 - ts));
    if (secs < 60) return `${secs}s ago`;
    if (secs < 3600) return `${Math.round(secs / 60)}m ago`;
    return `${Math.round(secs / 3600)}h ago`;
  }

  function rowHtml(s) {
    const state = s.state || s.status || 'unindexed';
    const title = escapeHtml(s.title || s.doc_id);
    const stopping = !!s.stop_requested && state === 'running';

    let label = STATE_LABEL[state] || state;
    let stage = s.stage ? (STAGE_LABEL[s.stage] || s.stage) : '';
    if (stopping) { label = 'Stopping'; stage = 'finishing current item'; }

    // Only the states that have meaningful progress get a bar. An unindexed
    // document is not "0% through" anything — it simply has no index yet.
    const showBar = state === 'running' || state === 'paused' || state === 'error';
    const pct = state === 'indexed' ? 100 : (s.percent || 0);

    let action = '';
    if (state === 'running') {
      action = stopping
        ? `<button class="li-row-btn" disabled>Stopping…</button>`
        : `<button class="li-row-btn danger" data-li-stop="${escapeHtml(s.doc_id)}">Stop</button>`;
    } else if (state === 'paused' || state === 'error') {
      action = `<button class="li-row-btn" data-li-retry="${escapeHtml(s.doc_id)}">Resume</button>`;
    } else if (state === 'unindexed' || state === 'partial') {
      // The whole point of listing unindexed documents: one click to fix it.
      const want = state === 'partial' ? 'Add AI index' : 'Index';
      action = `<button class="li-row-btn" data-li-index="${escapeHtml(s.doc_id)}">${want}</button>`;
    } else if (state === 'indexed') {
      action = `<button class="li-row-btn" data-li-index="${escapeHtml(s.doc_id)}">Re-index</button>`;
    }

    // "View" opens the index inspector — the REAL extracted text + chunks for
    // this doc, so a run's actual output is visible instead of a bare percent.
    const viewBtn = `<button class="li-row-btn" data-li-view="${escapeHtml(s.doc_id)}" title="View indexed output">View</button>`;
    action = viewBtn + action;

    // Which of the two derived indexes exist, for the at-a-glance row.
    const flags = [
      s.indexed_search ? 'search' : null,
      s.indexed_rag ? 'AI' : null,
    ].filter(Boolean).join(' + ');

    return `
      <div class="li-row" data-doc-id="${escapeHtml(s.doc_id)}">
        <div class="li-row-head">
          <span class="li-row-title">${title}</span>
          <span class="li-row-status ${escapeHtml(state)}${stopping ? ' stopping' : ''}">${label}${stage ? ' · ' + stage : ''}</span>
        </div>
        ${showBar ? `<div class="li-bar"><div class="li-bar-fill${stopping ? ' stopping' : ''}" style="width:${pct}%"></div></div>` : ''}
        <div class="li-row-foot">
          <span class="li-row-stages">${showBar ? stageBreakdown(s) : (flags || `${s.pages || 0} pages`)}</span>
          <span class="li-row-meta">${showBar ? pct + '% · ' : ''}${fmtAgo(s.updated || s.added)}</span>
          ${action}
        </div>
      </div>`;
  }

  // Ordering weight for the panel. Work that is happening or blocked comes
  // first, then documents with nothing built yet, then the finished ones — so
  // the thing you came here to act on is never buried under green ticks.
  const STATE_RANK = {
    running: 0, paused: 1, error: 2, unindexed: 3, partial: 4, indexed: 5,
  };

  /**
   * HTML for the Indexing tab.
   *
   * The server returns EVERY library document joined with its index state, so
   * this lists what is running, what is searchable, and what still needs
   * indexing — not just the docs that happen to have a job file.
   */
  function panelHtml(titles) {
    const rows = [..._status.values()]
      .filter((s) => s && s.doc_id)
      .map((s) => ({
        ...s,
        state: s.state || s.status || 'unindexed',
        title: s.title || (titles && titles[s.doc_id]) || s.doc_id,
      }))
      .sort((a, b) => {
        const ra = STATE_RANK[a.state] ?? 9;
        const rb = STATE_RANK[b.state] ?? 9;
        if (ra !== rb) return ra - rb;
        // Within a group, newest activity first.
        return (b.updated || b.added || 0) - (a.updated || a.added || 0);
      });

    if (!rows.length) {
      return `
        <div class="study-empty">
          <div class="study-empty-icon">⚙️</div>
          <h3>No documents yet</h3>
          <p>Upload something and it will be indexed in the background.</p>
        </div>`;
    }

    const count = (st) => rows.filter((s) => s.state === st).length;
    const running = count('running');
    const todo = count('unindexed') + count('partial') + count('paused') + count('error');
    const done = count('indexed');

    const bits = [];
    if (running) bits.push(`${running} running`);
    if (todo) bits.push(`${todo} need indexing`);
    if (done) bits.push(`${done} indexed`);

    return `
      <div class="li-panel">
        <div class="li-panel-head">
          <h3>Indexing</h3>
          <span class="li-panel-count">${bits.join(' · ')}</span>
          <button class="li-row-btn" data-li-reindex-missing>Index missing</button>
        </div>
        <div class="li-list">${rows.map(rowHtml).join('')}</div>
      </div>`;
  }

  // Start (or resume) a job, then refresh so the panel picks up the new state.
  async function resume(docId) {
    try {
      await api('/api/study/index/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ doc_id: docId, ocr: true, search: true, ai: true }),
      });
      toast('Indexing resumed');
    } catch (err) {
      toast('Could not resume: ' + err.message);
    }
    await loadAllStatuses();
    window.dispatchEvent(new CustomEvent('study-index-changed'));
  }

  /** Ask a running job to stop at its next checkpoint. */
  async function stop(docId) {
    try {
      const res = await api('/api/study/index/stop', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ doc_id: docId }),
      });
      if (res && res.status === 'not_running') {
        toast('That job is not running');
      } else {
        toast('Stopping…');
      }
    } catch (err) {
      toast('Could not stop: ' + err.message);
    }
    await loadAllStatuses();
    window.dispatchEvent(new CustomEvent('study-index-changed'));
  }

  async function indexMissing() {
    try {
      const res = await api('/api/study/index/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ all: true, only_missing: true, ocr: true, search: true, ai: true }),
      });
      toast(`Queued ${(res && res.docs) || 0} document(s)`);
    } catch (err) {
      toast('Could not queue: ' + err.message);
    }
    await loadAllStatuses();
    window.dispatchEvent(new CustomEvent('study-index-changed'));
  }

  // Refresh badges once on load, and whenever the library reloads.
  loadAllStatuses();
  window.addEventListener('study-library-changed', () => loadAllStatuses());

  window.libraryIndex = {
    open,
    badgeFor,
    statusFor,
    panelHtml,
    resume,
    stop,
    indexMissing,
    // Opens the same modal the doc menu uses, preloaded for this document.
    // The panel's Index / Re-index buttons route through here so there is one
    // indexing UI, not two.
    indexDoc: (docId, title) => open(docId, title),
    inspect,
    reloadStatuses: loadAllStatuses,
  };
})();
