/**
 * study-ai.js — the Study AI chat surface.
 *
 * One conversation store per document, shared by two views:
 *   - the compact floating popup (bottom-right launcher)
 *   - the full chat pane that replaces the PDF when the header toggle flips
 *
 * The host (study-reader.js) injects a `ctx` describing the open document, so
 * this module never reaches into reader internals. Context shape:
 *   { host, getDocId, getTitle, isRemote, getCurrentPage,
 *     getPageImage(page), getPdfBase64(), getPageText(page), getPdfText(),
 *     api(path,opts), toast(msg) }
 *
 * Exposes window.studyAI = { mountFloating, createPane, resetThread, getThread }.
 */
(function () {
  'use strict';

  const LS_PREFIX = 'sable_study_ai_thread_';

  // -- Model capability cache (which model can see images / read docs) --------
  let MODELS = null;
  let AI_CONFIG = null;

  async function loadModels(ctx) {
    if (MODELS) return MODELS;
    try {
      const [m, c] = await Promise.all([
        ctx.api('/api/study/ai/models'),
        ctx.api('/api/study/ai/config'),
      ]);
      MODELS = (m && m.models) || [];
      AI_CONFIG = c || {};
    } catch {
      MODELS = [];
      AI_CONFIG = {};
    }
    return MODELS;
  }

  function currentCaps() {
    if (!MODELS || !MODELS.length) return {};
    const id = (AI_CONFIG && AI_CONFIG.model) || (MODELS[0] && MODELS[0].id);
    const m = MODELS.find((x) => x.id === id) || MODELS[0];
    return (m && m.capabilities) || {};
  }

  // -- Conversation store (per document, persisted) ---------------------------
  const threads = {};
  const listeners = {};

  function lsKey(docId) { return LS_PREFIX + docId; }

  function getThread(docId) {
    if (threads[docId]) return threads[docId];
    let arr = [];
    try {
      const raw = localStorage.getItem(lsKey(docId));
      if (raw) arr = JSON.parse(raw) || [];
    } catch { arr = []; }
    threads[docId] = Array.isArray(arr) ? arr : [];
    return threads[docId];
  }

  function saveThread(docId) {
    try {
      const arr = getThread(docId);
      const trimmed = arr.length > 60 ? arr.slice(arr.length - 60) : arr;
      localStorage.setItem(lsKey(docId), JSON.stringify(trimmed));
    } catch { /* quota / private mode - best effort */ }
  }

  function emit(docId) {
    (listeners[docId] || new Set()).forEach((fn) => { try { fn(); } catch {} });
  }

  function subscribe(docId, fn) {
    (listeners[docId] || (listeners[docId] = new Set())).add(fn);
    return () => { (listeners[docId] || new Set()).delete(fn); };
  }

  function pushMessage(docId, msg) {
    getThread(docId).push(msg);
    saveThread(docId);
    emit(docId);
  }

  function patchLast(docId, patch) {
    const arr = getThread(docId);
    if (!arr.length) return;
    Object.assign(arr[arr.length - 1], patch);
    saveThread(docId);
    emit(docId);
  }

  function resetThread(docId) {
    threads[docId] = [];
    try { localStorage.removeItem(lsKey(docId)); } catch {}
    emit(docId);
  }

  // -- Rendering helpers ------------------------------------------------------
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  function md(text) {
    if (typeof window.renderMarkdown === 'function') {
      try { return window.renderMarkdown(text); } catch { /* fall through */ }
    }
    return `<div class="sa-plain">${esc(text)}</div>`;
  }

  function typeset(host) {
    if (window.MathJax && MathJax.typesetPromise) {
      MathJax.typesetPromise([host]).catch(() => {});
    } else if (typeof window._lazyLoadMathJax === 'function') {
      window._lazyLoadMathJax()
        .then(() => { if (window.MathJax && MathJax.typesetPromise) MathJax.typesetPromise([host]).catch(() => {}); })
        .catch(() => {});
    }
    if (window.mermaid && window.renderMermaidDiagrams) window.renderMermaidDiagrams(host);
    if (window.lucide) window.lucide.createIcons({ nodes: host.querySelectorAll('[data-lucide]') });
  }

  // -- Streaming send ---------------------------------------------------------
  async function send(ctx, view) {
    const docId = ctx.getDocId();
    const text = view.input.value.trim();
    if (!text && !view.pending.length) return;
    if (view.busy) return;

    const staged = view.pending.slice();
    const attMeta = staged.map((a) => ({ name: a.name, kind: a.kind }));

    view.input.value = '';
    view.pending = [];
    view.renderAttachments();

    pushMessage(docId, { role: 'user', content: text, attachments: attMeta });
    pushMessage(docId, { role: 'assistant', content: '', streaming: true });
    view.busy = true;
    syncComposer(view);

    const thread = getThread(docId).filter((m) => !m.streaming);
    const messages = thread.map((m) => ({ role: m.role, content: m.content }));
    const attachments = staged.map((a) => ({ kind: a.kind, name: a.name, data: a.data }));

    const controller = new AbortController();
    view.controller = controller;

    let raw = '';
    // Tool + image activity for this turn, rendered as a live activity strip
    // under the streaming bubble. Reset per send so each turn shows its own.
    const toolsAcc = [];
    const imagesAcc = [];
    patchTools(docId, toolsAcc, imagesAcc);
    try {
      const res = await fetch('/api/study/doc/' + encodeURIComponent(docId) + '/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          messages: messages,
          attachments: attachments,
          scope: 'doc',
          page: ctx.getCurrentPage ? ctx.getCurrentPage() : 1,
        }),
        signal: controller.signal,
      });
      if (!res.ok || !res.body) throw new Error('HTTP ' + res.status);

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = '';

      for (;;) {
        const r = await reader.read();
        if (r.done) break;
        buf += decoder.decode(r.value, { stream: true });
        let nl;
        while ((nl = buf.indexOf('\n\n')) !== -1) {
          const chunk = buf.slice(0, nl);
          buf = buf.slice(nl + 2);
          const line = chunk.split('\n').find((l) => l.startsWith('data:'));
          if (!line) continue;
          let ev;
          try { ev = JSON.parse(line.slice(5).trim()); } catch { continue; }

          if (ev.type === 'answer') {
            raw += ev.text || '';
            // Live-update only the streaming bubble — a full re-render per
            // chunk flickers and fights the user's scroll.
            streamText(docId, raw);
          } else if (ev.type === 'skill_start') {
            toolsAcc.push({ id: ev.id, name: ev.name, status: 'running', output: '' });
            patchTools(docId, toolsAcc);
          } else if (ev.type === 'skill_output') {
            const t = toolsAcc.find((x) => x.id === ev.id) || toolsAcc[toolsAcc.length - 1];
            if (t) { t.output += ev.text || ''; patchTools(docId, toolsAcc); }
          } else if (ev.type === 'skill_end') {
            const t = toolsAcc.find((x) => x.id === ev.id) || toolsAcc[toolsAcc.length - 1];
            if (t) {
              t.status = ev.ok ? 'ok' : 'error';
              t.error = ev.error || '';
              t.duration = ev.duration_ms || 0;
              patchTools(docId, toolsAcc);
            }
          } else if (ev.type === 'doc_changed') {
            // A tool mutated study state — let the reader refresh its panes.
            window.dispatchEvent(new CustomEvent('study-doc-changed', { detail: ev }));
          } else if (ev.type === 'study_attachment') {
            // Page image the model pulled in — show it in the thread.
            if (ev.data) {
              imagesAcc.push({ name: ev.name, page: ev.page, data: ev.data });
              patchTools(docId, toolsAcc, imagesAcc);
            }
          } else if (ev.type === 'done') {
            if (ev.text) { raw = ev.text; streamText(docId, raw); }
          } else if (ev.type === 'error') {
            finishStream(docId, raw, ev.message || 'AI error');
            view.busy = false;
            syncComposer(view);
            return;
          }
        }
      }
      finishStream(docId, raw, null);
    } catch (err) {
      if (err.name === 'AbortError') {
        finishStream(docId, raw, 'stopped');
      } else {
        finishStream(docId, raw, err.message);
      }
    } finally {
      view.controller = null;
      view.busy = false;
      syncComposer(view);
    }
  }

  /** Abort an in-flight stream (Stop button). */
  function stopStream(view) {
    if (view.controller) {
      try { view.controller.abort(); } catch {}
    }
  }

  /** Show the Stop button while a response streams, Send otherwise. */
  function syncComposer(view) {
    if (!view || !view.sendBtn || !view.stopBtn) return;
    view.sendBtn.style.display = view.busy ? 'none' : '';
    view.stopBtn.style.display = view.busy ? '' : 'none';
  }

  /**
   * Live streaming update. Mutates the streaming message in the store but does
   * NOT emit a full re-render — each subscribed view patches only its last
   * bubble, so the thread never flickers or jumps during a stream.
   */
  function streamText(docId, text) {
    const arr = getThread(docId);
    const last = arr[arr.length - 1];
    if (last && last.streaming) last.content = text;
    (listeners[docId] || new Set()).forEach((fn) => {
      if (fn.updateStream) { try { fn.updateStream(text); } catch {} }
    });
  }

  /** Finalize the streaming message and do one clean full render. */
  function finishStream(docId, text, error) {
    const arr = getThread(docId);
    const last = arr[arr.length - 1];
    if (last && last.streaming) {
      last.content = error === 'stopped' ? (text || '(stopped)') : (text || '(empty response)');
      last.streaming = false;
      if (error && error !== 'stopped') last.error = error;
    }
    saveThread(docId);
    emit(docId);
  }

  // -- Attachment staging -----------------------------------------------------
  async function addCurrentPage(ctx, view) {
    if (view.busy) return;
    const caps = currentCaps();
    const page = ctx.getCurrentPage ? ctx.getCurrentPage() : 1;
    const label = 'Page ' + page;
    try {
      if (caps.image) {
        const dataUrl = await ctx.getPageImage(page);
        if (!dataUrl) { ctx.toast && ctx.toast('Could not render this page'); return; }
        view.pending.push({ kind: 'image', name: label + '.png', data: dataUrl, label: label });
      } else {
        const txt = await ctx.getPageText(page);
        if (!txt) { ctx.toast && ctx.toast('No text on this page'); return; }
        view.pending.push({ kind: 'text', name: label, data: txt, label: label });
      }
      view.renderAttachments();
    } catch (err) {
      ctx.toast && ctx.toast('Attach failed: ' + err.message);
    }
  }

  async function addPdf(ctx, view) {
    if (view.busy) return;
    const caps = currentCaps();
    const title = (ctx.getTitle && ctx.getTitle()) || 'Document';
    try {
      if (caps.document) {
        const file = await ctx.getPdfBase64();
        if (!file) { ctx.toast && ctx.toast('Could not read the PDF'); return; }
        view.pending.push({ kind: 'document', name: file.name || (title + '.pdf'), data: file.data, label: 'PDF' });
      } else {
        const txt = await ctx.getPdfText();
        if (!txt) { ctx.toast && ctx.toast('No extractable text in this PDF'); return; }
        view.pending.push({ kind: 'text', name: title, data: txt, label: 'PDF text' });
      }
      view.renderAttachments();
    } catch (err) {
      ctx.toast && ctx.toast('Attach failed: ' + err.message);
    }
  }

  // -- Live tool activity -----------------------------------------------------
  // Each send accumulates the tools it runs (highlight, note edit, PDF read…)
  // and any page images it pulled in. They render as a strip under the thread
  // so the user watches the model work instead of staring at a frozen bubble.
  const liveByDoc = {};

  // Human labels + icons per tool, so the card reads as an action, not a symbol.
  const TOOL_META = {
    study_document: { label: 'Document', icon: 'file-text' },
    study_note: { label: 'Note', icon: 'notebook-pen' },
    study_pdf: { label: 'PDF', icon: 'file-search' },
    study_diagnostics: { label: 'Diagnostics', icon: 'activity' },
    web_search: { label: 'Web search', icon: 'globe' },
    web_fetch: { label: 'Web fetch', icon: 'globe' },
    generate_image: { label: 'Image', icon: 'image-plus' },
    run_simulacra: { label: 'Simulation', icon: 'play' },
    grep: { label: 'Search files', icon: 'search' },
    glob: { label: 'Find files', icon: 'folder-search' },
    list_dir: { label: 'List folder', icon: 'folder' },
    view_file: { label: 'Read file', icon: 'file' },
    edit_file: { label: 'Edit file', icon: 'file-pen' },
    create_file: { label: 'Create file', icon: 'file-plus' },
    insert_file: { label: 'Insert lines', icon: 'file-plus' },
    get_file: { label: 'Attach file', icon: 'paperclip' },
  };

  function toolMeta(name) {
    return TOOL_META[name] || { label: (name || 'tool').replace(/_/g, ' '), icon: 'wrench' };
  }

  function renderLiveHtml(tools, images) {
    if ((!tools || !tools.length) && (!images || !images.length)) return '';
    let h = '<div class="sa-live">';
    (tools || []).forEach((t) => {
      const meta = toolMeta(t.name);
      const running = t.status !== 'ok' && t.status !== 'error';
      const cls = t.status === 'error' ? 'err' : (t.status === 'ok' ? 'ok' : 'run');
      const state = t.status === 'error' ? 'Failed' : (t.status === 'ok' ? 'Done' : 'Running');
      // Collapsible body: open only on failure (so errors are visible); the
      // successful output stays tucked away until the user asks for it.
      const open = t.status === 'error' ? ' open' : '';
      h += '<details class="sa-live-tool ' + cls + '"' + open + '>'
        + '<summary class="sa-live-head">'
        +   '<span class="sa-live-dot"></span>'
        +   '<i data-lucide="' + meta.icon + '" class="icon-lucide sa-live-icon"></i>'
        +   '<span class="sa-live-name">' + esc(meta.label) + '</span>'
        +   '<span class="sa-live-state">' + state
        +     (running ? '<span class="sa-live-dots"><i></i><i></i><i></i></span>' : '')
        +     (!running && t.duration ? ' · ' + t.duration + 'ms' : '') + '</span>'
        +   '<i data-lucide="chevron-down" class="icon-lucide sa-live-chev"></i>'
        + '</summary>'
        + '<div class="sa-live-body">'
        +   (t.error ? '<div class="sa-live-err">' + esc(t.error) + '</div>' : '')
        +   (t.output
              ? '<pre class="sa-live-pre">' + esc(String(t.output).slice(0, 6000)) + '</pre>'
              : '<div class="sa-live-empty">' + (running ? 'Working…' : 'No output') + '</div>')
        + '</div>'
        + '</details>';
    });
    if (images && images.length) {
      h += '<div class="sa-live-imgs">';
      images.forEach((img) => {
        h += '<button class="sa-live-img" data-sa-img="' + esc(img.data).slice(0, 60) + '">'
          + '<img src="' + img.data + '" alt="' + esc(img.name || '') + '">'
          + '<span>' + esc(img.name || 'page') + '</span></button>';
      });
      h += '</div>';
    }
    return h + '</div>';
  }

  function patchTools(docId, tools, images) {
    liveByDoc[docId] = { tools: tools || [], images: images || [] };
    const html = renderLiveHtml(liveByDoc[docId].tools, liveByDoc[docId].images);
    document.querySelectorAll('.sa-thread').forEach((threadEl) => {
      let host = threadEl.querySelector('.sa-live-host');
      if (!host) {
        if (!html) return;
        host = document.createElement('div');
        host.className = 'sa-live-host';
        threadEl.appendChild(host);
      }
      host.innerHTML = html;
      threadEl.scrollTop = threadEl.scrollHeight;
    });
  }

  function clearLive(docId) {
    liveByDoc[docId] = { tools: [], images: [] };
  }

  // -- View -------------------------------------------------------------------
  function createView(ctx, opts) {
    const compact = !!(opts && opts.compact);
    const view = { pending: [], busy: false, input: null, root: null, render: null, renderAttachments: null };

    const root = document.createElement('div');
    root.className = 'sa-view' + (compact ? ' sa-compact' : ' sa-full');
    root.innerHTML =
      '<div class="sa-head">' +
        '<span class="sa-head-icon"><i data-lucide="sparkles" class="icon-lucide"></i></span>' +
        '<span class="sa-head-title">Study AI</span>' +
        '<div class="sa-head-spacer"></div>' +
        '<button class="sa-icon-btn" data-sa="new" title="New chat"><i data-lucide="plus" class="icon-lucide"></i></button>' +
        (compact ? '<button class="sa-icon-btn" data-sa="close" title="Close"><i data-lucide="x" class="icon-lucide"></i></button>' : '') +
      '</div>' +
      '<div class="sa-thread" data-sa-thread></div>' +
      '<div class="sa-attachments empty" data-sa-atts></div>' +
      '<div class="sa-composer">' +
        '<button class="sa-attach-btn" data-sa="page" title="Add current page"><i data-lucide="file-image" class="icon-lucide"></i></button>' +
        '<button class="sa-attach-btn" data-sa="pdf" title="Add PDF"><i data-lucide="file-text" class="icon-lucide"></i></button>' +
        '<input class="sa-input" data-sa-input type="text" placeholder="Ask about this document...">' +
        '<button class="sa-send" data-sa="send" title="Send"><i data-lucide="send" class="icon-lucide"></i></button>' +
        '<button class="sa-stop" data-sa="stop" title="Stop" style="display:none;"><i data-lucide="square" class="icon-lucide"></i></button>' +
      '</div>';
    view.root = root;
    view.input = root.querySelector('[data-sa-input]');
    view.sendBtn = root.querySelector('[data-sa="send"]');
    view.stopBtn = root.querySelector('[data-sa="stop"]');

    const threadEl = root.querySelector('[data-sa-thread]');
    const attsEl = root.querySelector('[data-sa-atts]');

    view.renderAttachments = function () {
      if (!view.pending.length) { attsEl.innerHTML = ''; attsEl.classList.add('empty'); return; }
      attsEl.classList.remove('empty');
      attsEl.innerHTML = view.pending.map((a, i) =>
        '<span class="sa-chip">' +
          '<i data-lucide="' + (a.kind === 'image' ? 'image' : 'file-text') + '" class="icon-lucide"></i>' +
          esc(a.label || a.name) +
          '<button class="sa-chip-x" data-sa-chip="' + i + '"><i data-lucide="x" class="icon-lucide"></i></button>' +
        '</span>').join('');
      if (window.lucide) window.lucide.createIcons({ nodes: attsEl.querySelectorAll('[data-lucide]') });
    };

    view.render = function () {
      const docId = ctx.getDocId();
      const arr = getThread(docId);
      if (!arr.length) {
        threadEl.innerHTML = '<div class="sa-empty">Ask anything about this document.<br><span>Use the buttons below to attach the current page or the whole PDF.</span></div>';
      } else {
        threadEl.innerHTML = arr.map((m) => {
          if (m.role === 'user') {
            const atts = (m.attachments || []).map((a) => '<span class="sa-msg-chip">' + esc(a.name) + '</span>').join('');
            return '<div class="sa-msg sa-user"><div class="sa-bubble">' + esc(m.content) + (atts ? '<div class="sa-msg-atts">' + atts + '</div>' : '') + '</div></div>';
          }
          if (m.streaming && !m.content) {
            return '<div class="sa-msg sa-ai"><div class="sa-bubble"><span class="sa-typing"><i></i><i></i><i></i></span></div></div>';
          }
          const err = m.error ? '<div class="sa-err">' + esc(m.error) + '</div>' : '';
          return '<div class="sa-msg sa-ai"><div class="sa-bubble">' + err + (m.content ? md(m.content) : '') + '</div></div>';
        }).join('');
      }
      typeset(threadEl);
      threadEl.scrollTop = threadEl.scrollHeight;
      view.renderAttachments();
    };

    // Flicker-free streaming: patch ONLY the last AI bubble in place. The full
    // thread HTML (and its scroll position) is never rebuilt mid-stream.
    view.updateStream = function (text) {
      const bubbles = threadEl.querySelectorAll('.sa-msg.sa-ai .sa-bubble');
      const last = bubbles[bubbles.length - 1];
      if (!last) return;
      const wasBottom = threadEl.scrollHeight - threadEl.scrollTop - threadEl.clientHeight < 48;
      last.innerHTML = md(text);
      if (window.lucide) window.lucide.createIcons({ nodes: last.querySelectorAll('[data-lucide]') });
      if (wasBottom) threadEl.scrollTop = threadEl.scrollHeight;
    };
    // The store calls subscribers as `fn()` (full render) but streamText looks
    // for `fn.updateStream` (fast path) — attach it to the same function object.
    view.__sub = view.render;
    view.__sub.updateStream = view.updateStream;

    // Swap Send ⇄ Stop based on busy state.
    view.syncComposer = function () { syncComposer(view); };

    root.addEventListener('click', async (e) => {
      const chip = e.target.closest('[data-sa-chip]');
      if (chip) { view.pending.splice(parseInt(chip.dataset.saChip, 10), 1); view.renderAttachments(); return; }
      const btn = e.target.closest('[data-sa]');
      if (!btn) return;
      const act = btn.dataset.sa;
      if (act === 'send') send(ctx, view);
      else if (act === 'stop') stopStream(view);
      else if (act === 'page') addCurrentPage(ctx, view);
      else if (act === 'pdf') addPdf(ctx, view);
      else if (act === 'new') { resetThread(ctx.getDocId()); view.render(); }
      else if (act === 'close' && opts.onClose) opts.onClose();
    });
    view.input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(ctx, view); }
    });

    // Keep this view in sync with the shared store; expose an unsubscribe so
    // the host can drop it when the reader closes (avoids a detached-DOM leak).
    view.dispose = subscribe(ctx.getDocId(), view.__sub);
    view.render();
    view.syncComposer();
    return view;
  }

  // -- Floating launcher + popup ---------------------------------------------
  function mountFloating(ctx) {
    if (!ctx.host) return null;
    const wrap = document.createElement('div');
    wrap.className = 'sa-float';
    wrap.innerHTML =
      '<button class="sa-float-btn" data-sa-launch title="Study AI">' +
        '<i data-lucide="sparkles" class="icon-lucide"></i>' +
      '</button>' +
      '<div class="sa-popup hidden" data-sa-popup></div>';
    ctx.host.appendChild(wrap);
    if (window.lucide) window.lucide.createIcons({ nodes: wrap.querySelectorAll('[data-lucide]') });

    const popup = wrap.querySelector('[data-sa-popup]');
    const btn = wrap.querySelector('[data-sa-launch]');
    let view = null;

    function open() {
      loadModels(ctx);
      if (!view) {
        view = createView(ctx, { compact: true, onClose: close });
        popup.appendChild(view.root);
      }
      popup.classList.remove('hidden');
      btn.classList.add('active');
      view.render();
      if (window.lucide) window.lucide.createIcons({ nodes: popup.querySelectorAll('[data-lucide]') });
      view.input.focus();
      // Dismiss on outside-click / Escape. Bound only while open so no
      // listener outlives the popup when the reader closes.
      document.addEventListener('mousedown', onDocDown, true);
      document.addEventListener('keydown', onDocKey, true);
    }
    function close() {
      popup.classList.add('hidden');
      btn.classList.remove('active');
      document.removeEventListener('mousedown', onDocDown, true);
      document.removeEventListener('keydown', onDocKey, true);
    }
    function onDocDown(e) {
      // Click inside the popup or on the launcher → keep it open.
      if (wrap.contains(e.target)) return;
      close();
    }
    function onDocKey(e) {
      if (e.key === 'Escape') { e.stopPropagation(); close(); }
    }
    btn.addEventListener('click', () => (popup.classList.contains('hidden') ? open() : close()));

    return { open: open, close: close, getView: () => view };
  }

  // -- Full pane (used by the Doc/Chat header toggle) -------------------------
  function createPane(ctx) {
    loadModels(ctx);
    const host = document.createElement('div');
    host.className = 'sa-pane';
    const inner = document.createElement('div');
    inner.className = 'sa-pane-inner';
    host.appendChild(inner);
    const view = createView(ctx, { compact: false });
    inner.appendChild(view.root);
    return { el: host, view: view };
  }

  window.studyAI = {
    mountFloating: mountFloating,
    createPane: createPane,
    resetThread: resetThread,
    getThread: getThread,
    loadModels: loadModels,
    // Exposed so the reader's context menu can decide image vs text staging.
    currentCaps: currentCaps,
  };
})();
