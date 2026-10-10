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
  // docId → { tid, host } for a thread whose LIVE DOM is already authoritative.
  // renderThread skips a DB repaint for this exact host+thread, so a finished
  // turn is never torn down and replayed from the DB (the "reloaded" flash).
  const liveDirty = {};

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
  function threadIdFor(docId) {
    // A surface may pin its thread (a note has exactly one, and the id can
    // change when an unsaved note is first saved) — resolve it live.
    const ctx = surfaceCtx[docId];
    if (ctx && typeof ctx.threadId === 'function') return ctx.threadId();
    return window.studyChat.activeThread(docId);
  }
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
    // The live stream already painted this exact thread into this exact host.
    // Repainting from the DB would wipe it and replay the same rows — the
    // "messages load from db instead of streaming live" symptom. Skip it.
    const _live = liveDirty[docId];
    if (!force && _live && _live.host === host && _live.tid === threadIdFor(docId)) return;
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
    // setThread() may have installed an override while we were awaiting the
    // fetch. The check at the top of this function ran BEFORE the await, so it
    // cannot see it. Honour the override instead of overwriting it with the
    // (empty) DB thread.
    if (override[docId]) {
      cache[docId] = override[docId].slice();
      paint(host, override[docId]);
      return;
    }
    cache[docId] = msgs;
    paint(host, msgs);
  }

  /** Compatibility shim — the main pipeline patches the streaming bubble. */
  function updateStream() { /* no-op: consumeChatStream owns live updates */ }

  function getThread(docId) {
    return (cache[docId] || override[docId] || []).map((m) => ({ role: m.role, content: m.content }));
  }
  function setThread(docId, turns) {
    delete liveDirty[docId];
    // Invalidate any renderThread that is already in flight. buildSurface()
    // kicks off an async DB load; when that resolves AFTER setThread it would
    // clobber the override and repaint the empty thread — so a saved thread
    // flashed on screen and was then wiped. Bumping the render token makes
    // stillCurrent() reject it.
    renderGen[docId] = (renderGen[docId] || 0) + 1;
    override[docId] = (turns || []).map((t) => ({ role: t.role, content: t.content }));
    cache[docId] = override[docId].slice();
    if (activeHost[docId]) paint(activeHost[docId], override[docId]);
    emitView(docId);
  }
  /** Release every in-memory map entry for a surface key. Called when an
   *  ephemeral surface (the reader's answer card) goes away: the maps are keyed
   *  by a monotonic id, so without this they grow by one dead entry per press
   *  for the life of the tab. The DB thread is deliberately left alone. */
  function releaseSurface(docId) {
    if (!docId) return;
    delete activeHost[docId];
    delete override[docId];
    delete cache[docId];
    delete surfaceCtx[docId];
    delete liveDirty[docId];
    delete renderGen[docId];
    delete docBusy[docId];
    delete busySubs[docId];
    delete viewSubs[docId];
  }

  function resetThread(docId) {
    override[docId] = null;
    delete liveDirty[docId];
    window.studyChat.newThread(docId);
    cache[docId] = [];
    if (activeHost[docId]) paint(activeHost[docId], []);
    emitView(docId);
  }

  // -- Sending ----------------------------------------------------------------
  /** Reshape a turn through the surface adapter (note AI overrides grounding). */
  function adaptTurn(ctx, args) {
    if (typeof ctx.buildContext !== 'function') return args;
    const out = ctx.buildContext(args);
    if (!out || typeof out !== 'object') return args;
    return {
      text: out.text != null ? out.text : args.text,
      context: out.context || args.context,
    };
  }

  function buildTurn(ctx, opts) {
    // Adapter hook — a non-document surface (note AI) supplies its own visible
    // text + study_context. The turn shape is otherwise identical, which is the
    // whole point: one surface, different grounding payload.
    if (typeof ctx.buildTurn === 'function') return ctx.buildTurn(ctx, opts || {});
    const docId = ctx.getDocId();
    // A surface may live under a synthetic key (the answer card, so it never
    // shares the Study AI thread) while still grounding on a real document.
    // getDocId() keys the per-surface maps; getGroundDocId() keys the request.
    const groundId = (typeof ctx.getGroundDocId === 'function' && ctx.getGroundDocId()) || docId;
    const sctx = surfaceCtx[docId] || {};
    const selection = (opts.selection != null ? opts.selection : (sctx.getSelection && sctx.getSelection())) || '';
    const page = opts.page || (sctx.getCurrentPage ? sctx.getCurrentPage() : 1);
    const scope = opts.scope || (selection ? 'selection' : 'doc');
    // The visible instruction: the user's own question for "ask", otherwise a
    // natural sentence for the preset. Never the bare action code.
    let body = (opts.question || '').trim();
    if (!body) {
      body = (opts.action && opts.action !== 'ask')
        ? (opts.label || opts.action)
        : 'Ask about this document';
    }
    // Mirror the composer's shape exactly: the quoted selection leads, the
    // instruction follows. The action path used to put the label first and the
    // quote second, so the same turn looked different depending on how it was
    // started — that inconsistency is the whole bug.
    let text = body;
    if (scope === 'selection' && selection) {
      text = '> ' + selection.replace(/\n/g, '\n> ').slice(0, 400) + '\n\n' + body;
    }
    return adaptTurn(ctx, {
      text,
      context: {
        doc_id: groundId,
        action: opts.action || 'ask',
        scope,
        page,
        selection,
        ...configPayload(),
      },
      page, selection, scope, ground: false,
    });
  }

  /** The REAL document id for a surface. A surface whose map key is synthetic
   *  (the answer card) still belongs to a real document, and that is what gets
   *  recorded against the thread so reopening it from the sidebar stays
   *  grounded instead of degrading to a free-form study chat. */
  function docMapIdFor(ctx, docId) {
    if (typeof ctx.getGroundDocId === 'function') return ctx.getGroundDocId() || docId;
    return docId;
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
      docMapId: docMapIdFor(ctx, docId),
      threadId: threadIdFor(docId) || window.studyChat.ensureThread(docId),
      text,
      context,
      pane: host,
    });
    emitBusy(docId, false);
    // The live DOM already holds this turn — user bubble, streamed answer and
    // any tool cards. Mark it authoritative so the emitView below (and the
    // reader's subscription it drives) do NOT repaint from the DB, which is
    // what made every finished turn flash back in as a reloaded dump.
    if (host) liveDirty[docId] = { tid: threadIdFor(docId), host };
    await refreshCache(docId);
    emitView(docId);
  }

  /** Re-read this doc's thread from the DB into the local cache.
   *  The live-DOM guard in renderThread skips the DB repaint (and with it the
   *  cache write) once a turn has streamed, so getThread() would otherwise
   *  hand back a stale list — which is exactly why "Save thread" reported
   *  "Nothing to save yet" with a finished answer on screen. */
  async function refreshCache(docId) {
    try {
      const tid = threadIdFor(docId);
      if (!tid) return;
      const msgs = await window.studyChat.loadMessages(tid);
      if (Array.isArray(msgs) && msgs.length) cache[docId] = msgs;
    } catch { /* cache is best-effort */ }
  }

  function askAction(ctx, opts) { return runTurn(ctx, opts || {}); }
  function askFollowup(ctx, text, grounding) {
    return runTurn(ctx, Object.assign({ question: text }, grounding || {}));
  }

  function stopStream(docId) { window.studyChat.stop(threadIdFor(docId)); }

  /** Run a preset action, optionally on a FRESH thread.
   *  A surface that sets caps.newChatPerAction (the note editor) gets a new
   *  chat for every Enhance / Organize / Summarize press, so each action lands
   *  in its own conversation instead of piling onto the previous one. */
  function runPreset(ctx, code, spec) {
    if (ctx && ctx.caps && ctx.caps.newChatPerAction) resetThread(ctx.getDocId());
    runTurn(ctx, { action: code, question: spec[1] });
  }

  // -- Surfaces (popup + full pane) -------------------------------------------
  function buildSurface(ctx, opts) {
    const docId = ctx.getDocId();
    surfaceCtx[docId] = ctx;
    const compact = !!(opts && opts.compact);

    // Context drives every visible difference between the document surface and
    // the note surface. The interaction — thread, composer, attachments, tools —
    // is identical; only the grounding payload and these labels change.
    const caps = Object.assign(
      { page: true, pdf: true, image: true, selection: true, paste: true, newChat: true, saveNote: true },
      ctx.caps || {},
    );
    const title = (ctx.getTitle && ctx.getTitle()) || 'Study AI';
    const icon = ctx.icon || 'book-open';
    const placeholder = (ctx.getPlaceholder && ctx.getPlaceholder()) || 'Ask about this document…';
    const actions = Array.isArray(ctx.actions) && ctx.actions.length ? ctx.actions : null;

    const el = document.createElement('div');
    el.className = 'sa-view' + (compact ? ' sa-compact' : ' sa-full');
    el.innerHTML =
      '<div class="sa-head">' +
        '<span class="sa-head-icon"><i data-lucide="' + escAttr(icon) + '" class="icon-lucide"></i></span>' +
        '<span class="sa-head-title">' + escAttr(title) + '</span>' +
        '<div class="sa-head-spacer"></div>' +
        (actions
          ? '<div class="sa-actions-wrap">' +
              '<button class="sa-icon-btn" data-sa="actions" title="AI actions"><i data-lucide="sparkles" class="icon-lucide"></i></button>' +
              '<div class="sa-actions-menu hidden" data-sa-menu>' +
                actions.map((a) =>
                  '<button data-sa-act="' + escAttr(a[0]) + '"><i data-lucide="' + escAttr(a[2]) + '" class="icon-lucide"></i>' + escAttr(a[1]) + '</button>').join('') +
                (ctx.groundOption
                  ? '<div class="sa-actions-sep"></div>' +
                    '<label class="sa-actions-ground"><input type="checkbox" data-sa-ground> Ground in referenced documents</label>'
                  : '') +
              '</div>' +
            '</div>'
          : '') +
        (caps.saveNote ? '<button class="sa-icon-btn" data-sa="save-note" title="Save answer as note"><i data-lucide="notebook-pen" class="icon-lucide"></i></button>' : '') +
        (caps.newChat ? '<button class="sa-icon-btn" data-sa="new" title="New chat"><i data-lucide="plus" class="icon-lucide"></i></button>' : '') +
        (compact ? '<button class="sa-icon-btn" data-sa="close" title="Close"><i data-lucide="x" class="icon-lucide"></i></button>' : '') +
      '</div>' +
      '<div class="sa-thread" data-sa-thread></div>' +
      '<div class="sa-attachments empty" data-sa-atts></div>' +
      '<div class="sa-composer">' +
        (caps.page ? '<button class="sa-attach-btn" data-sa="page" title="Attach current page"><i data-lucide="file-image" class="icon-lucide"></i></button>' : '') +
        (caps.pdf ? '<button class="sa-attach-btn" data-sa="pdf" title="Attach whole document"><i data-lucide="file-text" class="icon-lucide"></i></button>' : '') +
        (caps.image ? '<button class="sa-attach-btn" data-sa="image" title="Attach images"><i data-lucide="image-plus" class="icon-lucide"></i></button>' +
                      '<input type="file" class="sa-file-input" data-sa-file accept="image/*" multiple hidden>' : '') +
        '<input class="sa-input" data-sa-input type="text" placeholder="' + escAttr(placeholder) + '">' +
        '<button class="sa-send" data-sa="send" title="Send"><i data-lucide="send" class="icon-lucide"></i></button>' +
        '<button class="sa-stop" data-sa="stop" title="Stop" style="display:none;"><i data-lucide="square" class="icon-lucide"></i></button>' +
      '</div>';

    const body = el.querySelector('[data-sa-thread]');
    const attsEl = el.querySelector('[data-sa-atts]');
    const input = el.querySelector('[data-sa-input]');
    const sendBtn = el.querySelector('[data-sa="send"]');
    const stopBtn = el.querySelector('[data-sa="stop"]');
    const fileInput = el.querySelector('[data-sa-file]');

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
      // Pages ACCUMULATE. Re-attaching a page that is already staged is a
      // no-op; attaching a new page appends instead of replacing the rest.
      if (view.pending.some((a) => a.kind === 'page' && a.page === page)) return;
      const item = { kind: 'page', label: 'Page ' + page, page };
      view.pending.push(item);
      if (caps.image && sctx.getPageImage) {
        item.uploading = true;
        view.renderAttachments();
        item.ready = (async () => {
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
          view.renderAttachments();
        })();
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

    /** Stage an image from a paste/drop/pick: preview immediately, upload in
     *  place. Returns a promise that resolves when the upload settles, so a
     *  batch of images can be awaited before the turn is sent. */
    async function stageImageBlob(blob, name) {
      if (!blob) return;
      const item = { kind: 'image', label: name || 'Image', uploading: true };
      view.pending.push(item);
      view.renderAttachments();
      item.ready = (async () => {
        const dataUrl = await blobToDataUrl(blob);
        if (dataUrl) { item.dataUrl = dataUrl; view.renderAttachments(); }
        const up = await window.studyChat.uploadBlob(blob, name || 'pasted-image.png');
        if (up) item.file = { path: up.path };
        item.uploading = false;
        view.renderAttachments();
      })();
      return item.ready;
    }

    function syncBusy(busy) {
      sendBtn.style.display = busy ? 'none' : '';
      stopBtn.style.display = busy ? '' : 'none';
    }

    /** Attach the latest answer to the current selection as a study note.
     *  Creates a note (via /api/study/notes/capture) whose reference is this
     *  document + page + selection, with the answer as the body. The reader
     *  then shows a pin for it on the page. */
    async function saveAsNote() {
      const sctx = surfaceCtx[docId] || {};
      const turns = cache[docId] || override[docId] || [];
      const lastAnswer = [...turns].reverse()
        .find((t) => t.role === 'assistant' && String(t.content || '').trim());
      if (!lastAnswer) return toast('Nothing to save yet', 'error');
      const sel = (sctx.getSelection && sctx.getSelection()) || '';
      const page = (sctx.getCurrentPage && sctx.getCurrentPage()) || 1;
      const answer = String(lastAnswer.content || '').trim();
      const quoted = sel ? '> ' + sel.replace(/\n/g, '\n> ').slice(0, 2000) + '\n\n' : '';
      const body = {
        doc_id: docId,
        doc_title: (ctx.getTitle && ctx.getTitle()) || 'Document',
        page,
        text: sel,
        content: quoted + answer,
      };
      try {
        await ctx.api('/api/study/notes/capture', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        toast('Saved as note', 'success');
        // Let the reader redraw its note pins + Notes pane.
        window.dispatchEvent(new CustomEvent('study-notes-changed'));
      } catch (err) {
        toast('Could not save note: ' + err.message, 'error');
      }
    }

    async function sendCurrent() {
      const text = input.value.trim();
      if (!text) return;
      // Wait for any in-flight attachment uploads to settle — otherwise a fast
      // Send after picking several images would drop the ones still uploading.
      const inflight = view.pending.filter((a) => a.ready).map((a) => a.ready);
      if (inflight.length) await Promise.allSettled(inflight);
      input.value = '';
      const sctx = surfaceCtx[docId] || {};
      const pending = view.pendingTurn;
      view.pendingTurn = null;
      // A staged attachment wins as the scope: a whole-doc chip → doc scope, a
      // page chip → page scope, otherwise fall back to the live selection.
      const staged = view.pending.slice();
      const stagedPdf = staged.find((a) => a.kind === 'pdf');
      // Every staged page, in the order the reader attached them.
      const stagedPages = staged.filter((a) => a.kind === 'page');
      const pages = stagedPages.map((a) => a.page).filter((n) => Number.isFinite(n));
      const stagedPage = stagedPages[0] || null;
      // Real uploaded attachments (page image / pasted image). The server
      // resolves these paths per-model via the /api/chat file pipeline.
      const files = staged.filter((a) => a.file && a.file.path).map((a) => ({ path: a.file.path }));
      const previews = staged.filter((a) => a.dataUrl).map((a) => a.dataUrl);
      const selection = (pending && pending.selection) != null
        ? pending.selection
        : ((sctx.getSelection && sctx.getSelection()) || '');
      const page = (pending && pending.page) || stagedPage && stagedPage.page
        || (sctx.getCurrentPage ? sctx.getCurrentPage() : 1);
      const scope = (pending && pending.scope) || (stagedPdf ? 'doc' : (stagedPages.length ? 'page' : (selection ? 'selection' : 'doc')));
      // Ground checkbox (note surface only) — read live before clearing chips.
      const groundEl = el.querySelector('[data-sa-ground]');
      const ground = !!(groundEl && groundEl.checked);
      view.pending = [];
      view.renderAttachments();
      // Prefix the visible message with what it is grounded on, so the sent
      // bubble records the attachment even after the chip row is cleared.
      let visible = text;
      if (stagedPdf) visible = '[Whole document]\n' + text;
      else if (pages.length) {
        visible = (pages.length > 1 ? `[Pages ${pages.join(', ')}]\n` : `[Page ${pages[0]}]\n`) + text;
      }
      else if (scope === 'selection' && selection) visible = '> ' + selection.replace(/\n/g, '\n> ').slice(0, 400) + '\n\n' + text;
      // Adapter hook: let a non-document surface (note) reshape the visible
      // text and study_context. Everything else (thread, stream, tool cards)
      // is shared, which is why the two AIs look and behave identically.
      let context = {
        doc_id: docId,
        action: (pending && pending.action) || 'ask',
        scope, page, selection,
        // Multi-page grounding: the backend slices EVERY listed page, not just
        // `page`. Omitted for other scopes so the payload stays unchanged.
        ...(scope === 'page' && pages.length ? { pages } : {}),
        ...configPayload(),
      };
      if (typeof ctx.buildContext === 'function') {
        const out = ctx.buildContext({ text: visible, context, page, selection, scope, ground });
        if (out && typeof out === 'object') {
          if (out.text != null) visible = out.text;
          if (out.context) context = out.context;
        }
      }
      // Toggle this surface's Send ⇄ Stop for the duration of the turn (the
      // send path here bypasses runTurn, which is what emits for the card).
      emitBusy(docId, true);
      try {
        await window.studyChat.send({
          docId,
          docMapId: docMapIdFor(ctx, docId),
          threadId: threadIdFor(docId) || window.studyChat.ensureThread(docId),
          text: visible,
          context,
          files: files.length ? files : undefined,
          images: previews.length ? previews : undefined,
          pane: body,
        });
      } finally {
        emitBusy(docId, false);
      }
      // Keep the live DOM as-is (see runTurn): repainting from the DB here is
      // what made finished turns look reloaded rather than streamed.
      liveDirty[docId] = { tid: threadIdFor(docId), host: body };
      await refreshCache(docId);
      if (typeof window._sableLoadChats === 'function') window._sableLoadChats();
    }

    el.addEventListener('click', (e) => {
      const chip = e.target.closest('[data-sa-chip]');
      if (chip) { view.pending.splice(parseInt(chip.dataset.saChip, 10), 1); view.renderAttachments(); return; }
      // An AI action from the menu — same flow as typing, but with a preset
      // action + label. Used by the note surface (Enhance / Summarize / …).
      const actBtn = e.target.closest('[data-sa-act]');
      if (actBtn) {
        const menu = el.querySelector('[data-sa-menu]');
        if (menu) menu.classList.add('hidden');
        const code = actBtn.dataset.saAct;
        const spec = (actions || []).find((a) => a[0] === code) || [code, code];
        view.setTurn({ action: code, label: spec[1], question: code === 'ask' ? '' : spec[1] });
        if (code === 'ask') { input.focus(); return; }
        // Preset actions send immediately — identical to the document quick
        // actions, so both surfaces behave the same way.
        runPreset(surfaceCtx[docId], code, spec);
        return;
      }
      const btn = e.target.closest('[data-sa]');
      if (!btn) return;
      const act = btn.dataset.sa;
      if (act === 'send') sendCurrent();
      else if (act === 'stop') stopStream(docId);
      else if (act === 'page') view.stagePage();
      else if (act === 'pdf') view.stageDoc();
      else if (act === 'image') { if (fileInput) fileInput.click(); }
      else if (act === 'actions') {
        const menu = el.querySelector('[data-sa-menu]');
        if (menu) menu.classList.toggle('hidden');
      }
      else if (act === 'save-note') { saveAsNote(); }
      else if (act === 'new') { resetThread(docId); }
      else if (act === 'close' && opts && opts.onClose) opts.onClose();
    });
    // Click outside the actions menu closes it.
    el.addEventListener('mousedown', (e) => {
      const menu = el.querySelector('[data-sa-menu]');
      if (menu && !menu.classList.contains('hidden') && !e.target.closest('.sa-actions-wrap')) {
        menu.classList.add('hidden');
      }
    });
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendCurrent(); }
    });

    // Paste an image straight into the study composer (same affordance as the
    // main chat input). Without this the surface had no clipboard path at all.
    input.addEventListener('paste', (e) => {
      const items = e.clipboardData && e.clipboardData.items;
      if (!items) return;
      const blobs = [];
      for (const item of items) {
        if (item.type && item.type.startsWith('image/')) {
          const blob = item.getAsFile();
          if (blob) blobs.push(blob);
        }
      }
      if (!blobs.length) return;
      e.preventDefault();
      blobs.forEach((b, i) => view.stageImageBlob(b, 'pasted-image-' + (i + 1) + '.png'));
    });

    // File-picker path: stage EVERY picked image, not just the first. This is
    // the only gesture that lets the user attach several files at once.
    if (fileInput) {
      fileInput.addEventListener('change', () => {
        const picked = Array.from(fileInput.files || []);
        fileInput.value = '';
        picked.forEach((f, i) => view.stageImageBlob(f, f.name || ('image-' + (i + 1) + '.png')));
      });
    }

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

  /** Build a surface and hand back its element + view WITHOUT mounting it into
   *  a floating popup. Used by the reader's answer card, which hosts the exact
   *  same composer/thread UI inline — so each answer card gets its own thread
   *  instead of sharing (and clobbering) the document's Study AI thread. */
  function createSurface(ctx, opts) {
    return buildSurface(ctx, Object.assign({ compact: true }, opts || {}));
  }

  /**
   * Mount the shared surface into an ARBITRARY container as a floating panel.
   * The note editor uses this to host the exact same UI the document reader
   * shows — same thread, composer, attachments, tool cards — instead of the
   * one-off `.sn-ai-*` popup it used to carry. Only the context adapter differs.
   *
   * Returns { el, view, open, close, destroy }.
   */
  function mountSurface(ctx, container, opts) {
    if (!container) return null;
    opts = opts || {};
    // `launcher: false` mounts ONLY the popup — the caller drives it with its
    // own trigger button (the note editor's header "AI" button).
    const showLauncher = opts.launcher !== false;
    const wrap = document.createElement('div');
    wrap.className = 'sa-float sa-float-inline' + (showLauncher ? '' : ' sa-float-headless');
    wrap.innerHTML =
      (showLauncher
        ? '<button class="sa-float-btn" data-sa-launch title="' + (opts.launchTitle || 'Study AI') + '">' +
            '<i data-lucide="' + (ctx.icon || 'book-open') + '" class="icon-lucide"></i>' +
          '</button>'
        : '') +
      '<div class="sa-popup hidden" data-sa-popup></div>';
    container.appendChild(wrap);
    if (window.lucide) window.lucide.createIcons({ nodes: wrap.querySelectorAll('[data-lucide]') });

    const popup = wrap.querySelector('[data-sa-popup]');
    const btn = wrap.querySelector('[data-sa-launch]');
    let built = null;

    function open() {
      loadModels(ctx);
      if (!built) {
        built = buildSurface(ctx, Object.assign({ compact: true, onClose: close }, opts || {}));
        popup.appendChild(built.el);
      }
      popup.classList.remove('hidden');
      // Headless mounts have no launcher button — guard it so open() never
      // throws partway (which used to leave the popup half-built and abort
      // any action triggered right after opening).
      if (btn) btn.classList.add('active');
      // Deliberately NOT forced. When a preset action opens this popup and
      // sends in the same tick (the note Enhance/Organize buttons), the forced
      // async history load resolves AFTER the stream has started and repaints
      // the pane from the DB — detaching the live user bubble and streaming
      // answer, so nothing appears until the turn finishes (or is stopped).
      // Without force, renderThread bails while a turn is in flight.
      renderThread(popup.querySelector('[data-sa-thread]'), ctx.getDocId());
      if (window.lucide) window.lucide.createIcons({ nodes: popup.querySelectorAll('[data-lucide]') });
      if (built && built.view && built.view.input) built.view.input.focus();
      document.addEventListener('mousedown', onDocDown, true);
      document.addEventListener('keydown', onDocKey, true);
    }
    function close() {
      popup.classList.add('hidden');
      if (btn) btn.classList.remove('active');
      document.removeEventListener('mousedown', onDocDown, true);
      document.removeEventListener('keydown', onDocKey, true);
    }
    function onDocDown(e) { if (!wrap.contains(e.target)) close(); }
    function onDocKey(e) { if (e.key === 'Escape') { e.stopPropagation(); close(); } }
    function destroy() { try { close(); } catch {} try { wrap.remove(); } catch {} built = null; }
    if (btn) btn.addEventListener('click', () => (popup.classList.contains('hidden') ? open() : close()));

    // Let an external trigger (header button) run a preset action through the
    // SAME flow the in-popup actions menu uses.
    function runAction(code) {
      try { open(); } catch { /* still attempt the turn */ }
      const spec = (ctx.actions || []).find((a) => a[0] === code) || [code, code];
      if (code === 'ask') { try { built.view.input.focus(); } catch { /* noop */ } return; }
      // Resolve the surface context from THIS mount (not a docId lookup), so a
      // note whose id changed after first save still routes correctly.
      const surfaceCtxRef = surfaceCtx[ctx.getDocId()] || ctx;
      runPreset(surfaceCtxRef, code, spec);
    }

    return { el: wrap, open, close, destroy, runAction, getView: () => (built && built.view) || null };
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
    mountFloating, mountSurface, createPane, createSurface, resetThread, setThread, getThread, refreshCache, releaseSurface,
    subscribe, renderThread, updateStream, loadModels, setConfig, configPayload,
    askAction, askFollowup, stopStream, isBusy, onBusy, currentCaps,
  };
})();
