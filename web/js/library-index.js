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

  async function loadAllStatuses() {
    try {
      const data = await api('/api/study/index/status');
      const jobs = (data && data.jobs) || [];
      _status.clear();
      for (const j of jobs) _status.set(j.doc_id, j);
    } catch { /* badges are cosmetic */ }
    return _status;
  }

  function statusFor(docId) {
    return _status.get(docId) || null;
  }

  /** Small badge HTML for a document card, or '' when nothing to show. */
  function badgeFor(docId) {
    const s = statusFor(docId);
    if (!s || !s.status || s.status === 'idle') return '';
    if (s.status === 'running') {
      const pct = s.ocr_total ? Math.round((s.ocr_done / s.ocr_total) * 100)
        : s.ai_total ? Math.round((s.ai_cursor / s.ai_total) * 100) : 0;
      return `<span class="li-badge running" title="Indexing…">◐ ${pct}%</span>`;
    }
    if (s.status === 'paused') return `<span class="li-badge paused" title="Indexing paused">⏸ index</span>`;
    if (s.status === 'error') return `<span class="li-badge error" title="Indexing failed">⚠ index</span>`;
    if (s.status === 'done') return `<span class="li-badge done" title="Indexed">✓ index</span>`;
    return '';
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
          <input type="checkbox" data-li-ai>
          <span>AI pass (embed chunks for document Q&amp;A)</span>
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
    aiCheck.addEventListener('change', () => { aiFields.hidden = !aiCheck.checked; });

    const progress = $('[data-li-progress]');
    const fill = $('[data-li-fill]');
    const ptext = $('[data-li-ptext]');
    const runBtn = $('[data-li-run]');

    // Seed the progress bar from any saved job state (resume preview).
    try {
      const s = await api('/api/study/index/status?doc_id=' + encodeURIComponent(docId));
      if (s && (s.ocr_total || s.ai_total)) {
        progress.hidden = false;
        ptext.textContent = `Resuming — OCR ${s.ocr_done}/${s.ocr_total}, AI ${s.ai_cursor}/${s.ai_total}`;
      }
    } catch { /* fresh doc */ }

    runBtn.onclick = () => {
      const body = {
        doc_id: docId,
        ocr: $('[data-li-ocr]').checked,
        ai: aiCheck.checked,
        model: aiCheck.checked ? modelSel.value : '',
        thinking_mode: aiCheck.checked ? thinkSel.value : '',
      };
      if (!body.ocr && !body.ai) { toast('Pick at least one pass'); return; }
      startStream(body, { progress, fill, ptext, runBtn });
    };
  }

  function startStream(body, ui) {
    if (_es) { try { _es.close(); } catch {} }
    ui.progress.hidden = false;
    ui.runBtn.disabled = true;
    ui.runBtn.textContent = 'Indexing…';
    ui.ptext.textContent = 'Starting…';

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
      toast('Indexing complete ✓');
      loadAllStatuses().then(() => window.dispatchEvent(new CustomEvent('study-index-changed')));
      return;
    }
    if (evt.type === 'error') {
      ui.ptext.textContent = 'Error: ' + (evt.message || 'unknown');
      ui.runBtn.disabled = false;
      ui.runBtn.textContent = 'Retry';
      toast('Indexing failed: ' + (evt.message || 'unknown'));
    }
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  // Refresh badges once on load, and whenever the library reloads.
  loadAllStatuses();
  window.addEventListener('study-library-changed', () => loadAllStatuses());

  window.libraryIndex = {
    open,
    badgeFor,
    statusFor,
    reloadStatuses: loadAllStatuses,
  };
})();
