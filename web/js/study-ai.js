/**
 * study-ai.js — Study AI adapter over the REAL chat pipeline.
 *
 * Study chats are ordinary chats (`study-<docId>-<threadId>`). This module no
 * longer owns a private store or its own bubble renderer — it delegates every
 * turn to window.studyChat, which drives POST /api/chat with layout_mode="study".
 *
 * That means tool calls, pulled page images, thinking and answers all persist
 * in the `messages` / `skill_events` tables and replay through the SAME chat
 * renderers (addHistoryMessage / addBotStreaming / consumeChatStream) the main
 * chat and critique feature use. "Mirror the main chat" is the whole design.
 *
 * The public API is preserved so study-reader.js (answer card, popup, pane)
 * keeps working, but every surface now renders real chat DOM.
 */
(function () {
  'use strict';

  // -- Model capability cache (reused study config endpoint) ------------------
  async function loadModels(ctx, force) { return window.studyChat.loadModels(ctx, force); }
  function setConfig(cfg) { window.studyChat.setConfig(cfg); }
  function configPayload() { return window.studyChat.configPayload(); }
  function currentCaps() { return window.studyChat.currentCaps(); }

  // -- Per-doc view state -----------------------------------------------------
  const activeHost = {};    // docId → element currently rendering this doc's thread
  const override = {};      // docId → saved-note turns to render instead of DB
  const cache = {};         // docId → last loaded messages (for save-note)
  const surfaceCtx = {};    // docId → rich ctx (getSelection/getCurrentPage)
  const busySubs = {};      // docId → Set(fn)
  const viewSubs = {};      // docId → Set(fn)
  const renderGen = {};     // docId → monotonically increasing render token

  function emitView(docId) {
    (viewSubs[docId] || new Set()).forEach((fn) => { try { fn(); } catch {} });
  }
  function emitBusy(docId, busy) {
    if (busy) docBusy[docId] = true; else delete docBusy[docId];
    (busySubs[docId] || new Set()).forEach((fn) => { try { fn(busy); } catch {} });
  }
  function onBusy(docId, fn) {
    (busySubs[docId] || (busySubs[docId] = new Set())).add(fn);
    try { fn(isBusy(docId)); } catch {}
    return () => { (busySubs[docId] || new Set()).delete(fn); };
  }
  function subscribe(docId, fn) {
    (viewSubs[docId] || (viewSubs[docId] = new Set())).add(fn);
    return () => { (viewSubs[docId] || new Set()).delete(fn); };
  }

  const docBusy = {};        // docId → true while a turn streams (adapter-level)
  function threadIdFor(docId) { return window.studyChat.activeThread(docId); }
  function isBusy(docId) {
    // Adapter-level flag OR the bridge's own busy map — either means a turn is
    // live for this doc, so an async history paint must not wipe it.
    return !!docBusy[docId] || window.studyChat.isBusy(threadIdFor(docId));
  }

  // -- Rendering --------------------------------------------------------------
  function paint(host, messages) {
    if (!host) return;
    window.studyChat.renderMessages(host, messages || []);
  }

  /** Load the active thread from the DB (or a saved-note override) and paint it.
   *  Records `host` as the active streaming target for this document. */
  async function renderThread(host, docId, opts) {
    if (!host) return;
    const force = !!(opts && opts.force);
    activeHost[docId] = host;
    // Never wipe a live stream — the pipeline is already writing into this host.
    // `force` bypasses this (used once a turn has fully finalized, to swap the
    // live DOM for the canonical DB render).
    if (!force && isBusy(docId)) return;
    // Stamp this render. A paint may only land if it is still the newest render
    // AND no turn has started in the meantime. Without this, an async load that
    // resolves after the user already hit send would wipe the live user bubble
    // and streaming answer (the "message disappears, answer dumps at once" bug).
    const gen = (renderGen[docId] = (renderGen[docId] || 0) + 1);
    const stillCurrent = () => gen === renderGen[docId] && (force || !isBusy(docId));
    if (override[docId]) { if (stillCurrent()) paint(host, override[docId]); return; }
    const tid = threadIdFor(docId);
    if (!tid) { if (stillCurrent()) paint(host, []); return; }
    const msgs = await window.studyChat.loadMessages(tid);
    if (!stillCurrent()) return;
    cache[docId] = msgs;
    paint(host, msgs);
  }

  /** Compatibility shim — the main pipeline patches the streaming bubble. */
  function updateStream() { /* no-op: consumeChatStream owns live updates */ }

  function getThread(docId) {
    return (cache[docId] || override[docId] || []).map((m) => ({ role: m.role, content: m.content }));
  }
  function setThread(docId, turns) {
    override[docId] = (turns || []).map((t) => ({ role: t.role, content: t.content }));
    cache[docId] = override[docId].slice();
    if (activeHost[docId]) paint(activeHost[docId], override[docId]);
    emitView(docId);
  }
  function resetThread(docId) {
    override[docId] = null;
    window.studyChat.newThread(docId);
    cache[docId] = [];
    if (activeHost[docId]) paint(activeHost[docId], []);
    emitView(docId);
  }

  // -- Sending ----------------------------------------------------------------
  function buildTurn(ctx, opts) {
    const docId = ctx.getDocId();
    const sctx = surfaceCtx[docId] || {};
    const selection = (opts.selection != null ? opts.selection : (sctx.getSelection && sctx.getSelection())) || '';
    const page = opts.page || (sctx.getCurrentPage ? sctx.getCurrentPage() : 1);
    const scope = opts.scope || (selection ? 'selection' : 'doc');
    let text = (opts.question || opts.label || opts.action || '').trim();
    if (!text) text = 'Ask about this document';
    // Surface the grounding in the sent bubble so the selection is visible in
    // the saved thread instead of vanishing into the request only.
    if (scope === 'selection' && selection) {
      text += '\n\n> ' + selection.replace(/\n/g, '\n> ').slice(0, 400);
    }
    return {
      text,
      context: {
        doc_id: docId,
        action: opts.action || 'ask',
        scope,
        page,
        selection,
        ...configPayload(),
      },
    };
  }

  async function runTurn(ctx, opts) {
    const docId = ctx.getDocId();
    if (!docId || isBusy(docId)) return;
    const host = activeHost[docId];
    const { text, context } = buildTurn(ctx, opts || {});
    override[docId] = null;
    if (host) host.querySelectorAll('.empty').forEach((e) => e.remove());
    emitBusy(docId, true);
    await window.studyChat.send({
      docId,
      threadId: window.studyChat.ensureThread(docId),
      text,
      context,
      pane: host,
    });
    emitBusy(docId, false);
    // Turn is fully done — swap the live DOM for the canonical DB render
    // (which now includes the persisted skill_events / tool cards in order).
    if (host) renderThread(host, docId, { force: true });
    emitView(docId);
  }

  function askAction(ctx, opts) { return runTurn(ctx, opts || {}); }
  function askFollowup(ctx, text, grounding) {
    return runTurn(ctx, Object.assign({ question: text }, grounding || {}));
  }

  function stopStream(docId) { window.studyChat.stop(threadIdFor(docId)); }

  // -- Surfaces (popup + full pane) -------------------------------------------
  function buildSurface(ctx, opts) {
    const docId = ctx.getDocId();
    surfaceCtx[docId] = ctx;
    const compact = !!(opts && opts.compact);

    const el = document.createElement('div');
    el.className = 'sa-view' + (compact ? ' sa-compact' : ' sa-full');
    el.innerHTML =
      '<div class="sa-head">' +
        '<span class="sa-head-icon"><i data-lucide="book-open" class="icon-lucide"></i></span>' +
        '<span class="sa-head-title">Study AI</span>' +
        '<div class="sa-head-spacer"></div>' +
        '<button class="sa-icon-btn" data-sa="new" title="New chat"><i data-lucide="plus" class="icon-lucide"></i></button>' +
        (compact ? '<button class="sa-icon-btn" data-sa="close" title="Close"><i data-lucide="x" class="icon-lucide"></i></button>' : '') +
      '</div>' +
      '<div class="sa-thread" data-sa-thread></div>' +
      '<div class="sa-attachments empty" data-sa-atts></div>' +
      '<div class="sa-composer">' +
        '<button class="sa-attach-btn" data-sa="page" title="Attach current page"><i data-lucide="file-image" class="icon-lucide"></i></button>' +
        '<button class="sa-attach-btn" data-sa="pdf" title="Attach whole document"><i data-lucide="file-text" class="icon-lucide"></i></button>' +
        '<input class="sa-input" data-sa-input type="text" placeholder="Ask about this document…">' +
        '<button class="sa-send" data-sa="send" title="Send"><i data-lucide="send" class="icon-lucide"></i></button>' +
        '<button class="sa-stop" data-sa="stop" title="Stop" style="display:none;"><i data-lucide="square" class="icon-lucide"></i></button>' +
      '</div>';

    const body = el.querySelector('[data-sa-thread]');
    const attsEl = el.querySelector('[data-sa-atts]');
    const input = el.querySelector('[data-sa-input]');
    const sendBtn = el.querySelector('[data-sa="send"]');
    const stopBtn = el.querySelector('[data-sa="stop"]');

    const view = {
      // Staged attachments. Study grounding is resolved server-side from the
      // scope, so an attachment is really a scope selection: a page chip →
      // page scope, a document chip → whole-doc scope. The chips give the user
      // visible confirmation of what the next turn will be about.
      pending: [],
      input,
      // Pending turn spec set by a context action (Explain / Summarize / Ask
      // about PDF…). The next send consumes it so the action + scope survive to
      // the request instead of being flattened to a plain "ask".
      pendingTurn: null,
      setTurn(opts) { view.pendingTurn = opts || null; },
      renderAttachments() {
        if (!view.pending.length) { attsEl.innerHTML = ''; attsEl.classList.add('empty'); return; }
        attsEl.classList.remove('empty');
        attsEl.innerHTML = view.pending.map((a, i) =>
          '<span class="sa-chip sa-chip-' + (a.kind === 'pdf' ? 'document' : (a.kind === 'image' ? 'image' : 'page')) + '">' +
            (a.dataUrl
              ? '<img class="sa-chip-thumb" src="' + escAttr(a.dataUrl) + '" alt="">'
              : '<i data-lucide="' + (a.kind === 'pdf' ? 'file-text' : 'file-image') + '" class="icon-lucide"></i>') +
            escAttr(a.label) +
            (a.uploading ? '<i data-lucide="loader-circle" class="icon-lucide sa-chip-spin"></i>' : '') +
            '<button class="sa-chip-x" data-sa-chip="' + i + '"><i data-lucide="x" class="icon-lucide"></i></button>' +
          '</span>').join('');
        if (window.lucide) window.lucide.createIcons({ nodes: attsEl.querySelectorAll('[data-lucide]') });
      },
      // Public staging helpers (function declarations below are hoisted).
      stagePage: stagePage,
      stageDoc: stageDoc,
      stageImageBlob: stageImageBlob,
      render() { renderThread(body, docId); },
      dispose() { /* nothing persistent to release */ },
    };

    // Escape for text placed inside an attribute/child position.
    function escAttr(s) {
      return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
      }[c]));
    }

    /** Read a Blob as a data URL (for the inline preview thumbnail). */
    function blobToDataUrl(blob) {
      return new Promise((resolve) => {
        try {
          const fr = new FileReader();
          fr.onload = () => resolve(fr.result);
          fr.onerror = () => resolve(null);
          fr.readAsDataURL(blob);
        } catch { resolve(null); }
      });
    }

    /**
     * Stage the current page. A vision-capable model gets the REAL rendered
     * page image (uploaded through /api/upload so /api/chat can resolve it for
     * the provider); anything else falls back to page-scope text grounding,
     * which the server builds from the document. Either way the turn is scoped
     * to the page.
     */
    async function stagePage(pageArg) {
      const sctx = surfaceCtx[docId] || {};
      const page = pageArg || (sctx.getCurrentPage ? sctx.getCurrentPage() : 1);
      const caps = currentCaps();
      const item = { kind: 'page', label: 'Page ' + page, page };
      view.pending = view.pending.filter((a) => a.kind !== 'page');
      view.pending.push(item);
      if (caps.image && sctx.getPageImage) {
        item.uploading = true;
        view.renderAttachments();
        try {
          const dataUrl = await sctx.getPageImage(page);
          if (dataUrl) {
            item.dataUrl = dataUrl;
            const blob = window.studyChat.dataUrlToBlob(dataUrl);
            if (blob) {
              const up = await window.studyChat.uploadBlob(blob, 'page-' + page + '.png');
              if (up) item.file = { path: up.path };
            }
          }
        } catch { /* fall back to text scope */ }
        item.uploading = false;
      }
      view.renderAttachments();
    }

    /**
     * Stage the whole document. Per the study workflow this attaches the
     * extracted TEXT (server builds it from doc scope), not the raw PDF — the
     * model reasons over the words, and non-document models still work.
     */
    function stageDoc() {
      view.pending = view.pending.filter((a) => a.kind !== 'pdf');
      view.pending.push({ kind: 'pdf', label: 'Whole document' });
      view.renderAttachments();
    }

    /** Stage an image from a paste/drop: preview immediately, upload in place. */
    async function stageImageBlob(blob, name) {
      if (!blob) return;
      const item = { kind: 'image', label: name || 'Image', uploading: true };
      view.pending.push(item);
      view.renderAttachments();
      const dataUrl = await blobToDataUrl(blob);
      if (dataUrl) item.dataUrl = dataUrl;
      const up = await window.studyChat.uploadBlob(blob, name || 'pasted-image.png');
      if (up) item.file = { path: up.path };
      item.uploading = false;
      view.renderAttachments();
    }

    function syncBusy(busy) {
      sendBtn.style.display = busy ? 'none' : '';
      stopBtn.style.display = busy ? '' : 'none';
    }

    async function sendCurrent() {
      const text = input.value.trim();
      if (!text) return;
      input.value = '';
      const sctx = surfaceCtx[docId] || {};
      const pending = view.pendingTurn;
      view.pendingTurn = null;
      // A staged attachment wins as the scope: a whole-doc chip → doc scope, a
      // page chip → page scope, otherwise fall back to the live selection.
      const staged = view.pending.slice();
      const stagedPdf = staged.find((a) => a.kind === 'pdf');
      const stagedPage = staged.find((a) => a.kind === 'page');
      // Real uploaded attachments (page image / pasted image). The server
      // resolves these paths per-model via the /api/chat file pipeline.
      const files = staged.filter((a) => a.file && a.file.path).map((a) => ({ path: a.file.path }));
      const previews = staged.filter((a) => a.dataUrl).map((a) => a.dataUrl);
      const selection = (pending && pending.selection) != null
        ? pending.selection
        : ((sctx.getSelection && sctx.getSelection()) || '');
      const page = (pending && pending.page) || (stagedPage && stagedPage.page)
        || (sctx.getCurrentPage ? sctx.getCurrentPage() : 1);
      const scope = (pending && pending.scope) || (stagedPdf ? 'doc' : (stagedPage ? 'page' : (selection ? 'selection' : 'doc')));
      view.pending = [];
      view.renderAttachments();
      // Prefix the visible message with what it is grounded on, so the sent
      // bubble records the attachment even after the chip row is cleared.
      let visible = text;
      if (stagedPdf) visible = '[Whole document]\n' + text;
      else if (stagedPage) visible = `[Page ${page}]\n` + text;
      else if (scope === 'selection' && selection) visible = '> ' + selection.replace(/\n/g, '\n> ').slice(0, 400) + '\n\n' + text;
      // Toggle this surface's Send ⇄ Stop for the duration of the turn (the
      // send path here bypasses runTurn, which is what emits for the card).
      emitBusy(docId, true);
      try {
        await window.studyChat.send({
          docId,
          threadId: window.studyChat.ensureThread(docId),
          text: visible,
          context: {
            doc_id: docId,
            action: (pending && pending.action) || 'ask',
            scope, page, selection,
            ...configPayload(),
          },
          files: files.length ? files : undefined,
          images: previews.length ? previews : undefined,
          pane: body,
        });
      } finally {
        emitBusy(docId, false);
      }
      // Turn fully done — swap live DOM for the canonical DB render so tool
      // cards appear in their persisted, ordered form.
      renderThread(body, docId, { force: true });
      if (typeof window._sableLoadChats === 'function') window._sableLoadChats();
    }

    el.addEventListener('click', (e) => {
      const chip = e.target.closest('[data-sa-chip]');
      if (chip) { view.pending.splice(parseInt(chip.dataset.saChip, 10), 1); view.renderAttachments(); return; }
      const btn = e.target.closest('[data-sa]');
      if (!btn) return;
      const act = btn.dataset.sa;
      if (act === 'send') sendCurrent();
      else if (act === 'stop') stopStream(docId);
      else if (act === 'page') view.stagePage();
      else if (act === 'pdf') view.stageDoc();
      else if (act === 'new') { resetThread(docId); }
      else if (act === 'close' && opts && opts.onClose) opts.onClose();
    });
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendCurrent(); }
    });

    // Paste an image straight into the study composer (same affordance as the
    // main chat input). Without this the surface had no clipboard path at all.
    input.addEventListener('paste', (e) => {
      const items = e.clipboardData && e.clipboardData.items;
      if (!items) return;
      for (const item of items) {
        if (item.type && item.type.startsWith('image/')) {
          const blob = item.getAsFile();
          if (blob) { e.preventDefault(); view.stageImageBlob(blob, 'pasted-image.png'); }
          return;
        }
      }
    });

    // Drag-and-drop images onto the surface.
    el.addEventListener('dragover', (e) => e.preventDefault());
    el.addEventListener('drop', (e) => {
      const files = e.dataTransfer && e.dataTransfer.files;
      if (!files || !files.length) return;
      e.preventDefault();
      for (const f of files) {
        if (f.type && f.type.startsWith('image/')) view.stageImageBlob(f, f.name || 'dropped-image.png');
      }
    });

    onBusy(docId, syncBusy);
    renderThread(body, docId);
    if (window.lucide) window.lucide.createIcons({ nodes: el.querySelectorAll('[data-lucide]') });
    return { el, view };
  }

  function createPane(ctx) {
    const { el, view } = buildSurface(ctx, { compact: false });
    return { el, view };
  }

  function mountFloating(ctx) {
    if (!ctx.host) return null;
    const wrap = document.createElement('div');
    wrap.className = 'sa-float';
    wrap.innerHTML =
      '<button class="sa-float-btn" data-sa-launch title="Study AI">' +
        '<i data-lucide="book-open" class="icon-lucide"></i>' +
      '</button>' +
      '<div class="sa-popup hidden" data-sa-popup></div>';
    ctx.host.appendChild(wrap);
    if (window.lucide) window.lucide.createIcons({ nodes: wrap.querySelectorAll('[data-lucide]') });

    const popup = wrap.querySelector('[data-sa-popup]');
    const btn = wrap.querySelector('[data-sa-launch]');
    let built = null;

    function open() {
      loadModels(ctx);
      if (!built) {
        built = buildSurface(ctx, { compact: true, onClose: close });
        popup.appendChild(built.el);
      }
      popup.classList.remove('hidden');
      btn.classList.add('active');
      renderThread(popup.querySelector('[data-sa-thread]'), ctx.getDocId());
      if (window.lucide) window.lucide.createIcons({ nodes: popup.querySelectorAll('[data-lucide]') });
      built.view.input.focus();
      document.addEventListener('mousedown', onDocDown, true);
      document.addEventListener('keydown', onDocKey, true);
    }
    function close() {
      popup.classList.add('hidden');
      btn.classList.remove('active');
      document.removeEventListener('mousedown', onDocDown, true);
      document.removeEventListener('keydown', onDocKey, true);
    }
    function onDocDown(e) { if (!wrap.contains(e.target)) close(); }
    function onDocKey(e) { if (e.key === 'Escape') { e.stopPropagation(); close(); } }
    btn.addEventListener('click', () => (popup.classList.contains('hidden') ? open() : close()));

    return { open, close, getView: () => (built && built.view) || null };
  }

  window.studyAI = {
    mountFloating, createPane, resetThread, setThread, getThread, subscribe,
    renderThread, updateStream, loadModels, setConfig, configPayload,
    askAction, askFollowup, stopStream, isBusy, onBusy, currentCaps,
  };
})();
