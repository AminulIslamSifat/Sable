/* study-chat.js — Study AI backed by the REAL chat pipeline.
 *
 * Study chats are ordinary chats whose ids are `study-<docId>-<threadId>`. They
 * run through POST /api/chat with layout_mode="study", so:
 *   - messages persist in the `messages` table
 *   - tool calls persist in the `skill_events` table (interleaved in order)
 *   - history replays with the SAME renderers as the main chat
 *
 * This module owns nothing visual of its own — it calls the global chat
 * primitives (addMessage / addBotStreaming / consumeChatStream /
 * addHistoryMessage) exactly the way the critique feature does. "Mirror the main
 * chat" is the whole design.
 */
(function () {
  'use strict';

  const ACTIVE_KEY = 'sable_study_thread_';
  const DOCMAP_KEY = 'sable_study_thread_doc_';
  const PREFIX = 'study-';

  const listeners = {};      // docId → Set(fn)
  const busyByThread = {};   // threadId → bool
  const busyListeners = {};  // threadId → Set(fn)

  // ── Thread ids ────────────────────────────────────────────────────────────
  function threadPrefix(docId) { return PREFIX + docId + '-'; }

  function activeThread(docId) {
    try { return localStorage.getItem(ACTIVE_KEY + docId) || ''; } catch { return ''; }
  }

  function setActiveThread(docId, threadId) {
    try { localStorage.setItem(ACTIVE_KEY + docId, threadId); } catch { /* private mode */ }
  }

  /** A fresh thread id for this document (materialises on first send). */
  function newThread(docId) {
    const tid = threadPrefix(docId) + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
    setActiveThread(docId, tid);
    try { localStorage.setItem(DOCMAP_KEY + tid, docId); } catch { /* private mode */ }
    return tid;
  }

  /** Which document a study thread belongs to (for the main composer path).
   *  Thread ids can't be safely parsed back (doc ids may contain dashes), so
   *  the mapping is written when the thread is created. */
  function docForThread(threadId) {
    if (!threadId || !threadId.startsWith(PREFIX)) return '';
    try { return localStorage.getItem(DOCMAP_KEY + threadId) || ''; } catch { return ''; }
  }

  /** Ensure a thread exists for this doc, creating one lazily. */
  function ensureThread(docId) {
    const existing = activeThread(docId);
    if (existing) return existing;
    return newThread(docId);
  }

  /** Mint a thread under `docId` WITHOUT making it that document's active
   *  thread. The reader's answer card needs its own conversation per press,
   *  but must not hijack the thread the Study AI popup is showing — and the id
   *  must keep the `study-<docId>-` shape, because the backend links a chat to
   *  its document with the regex `^study-([0-9a-f]{32})-` and the sidebar
   *  groups by the same prefix. A synthetic namespace here would silently
   *  orphan every card thread from its document. */
  function newDetachedThread(docId) {
    const tid = threadPrefix(docId) + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
    try { localStorage.setItem(DOCMAP_KEY + tid, docId); } catch { /* private mode */ }
    return tid;
  }

  /** Every thread for a document, newest first, from the global chat list. */
  function listThreads(docId) {
    const list = (typeof chatList !== 'undefined' && chatList) || [];
    const pref = threadPrefix(docId);
    return list
      .filter((c) => (c.id || '').startsWith(pref))
      .sort((a, b) => (b.updated_at || '').localeCompare(a.updated_at || ''));
  }

  // ── Busy / listeners ──────────────────────────────────────────────────────
  function emit(docId) {
    (listeners[docId] || new Set()).forEach((fn) => { try { fn(); } catch {} });
  }
  function subscribe(docId, fn) {
    (listeners[docId] || (listeners[docId] = new Set())).add(fn);
    return () => { (listeners[docId] || new Set()).delete(fn); };
  }
  function onBusy(threadId, fn) {
    (busyListeners[threadId] || (busyListeners[threadId] = new Set())).add(fn);
    try { fn(!!busyByThread[threadId]); } catch {}
    return () => { (busyListeners[threadId] || new Set()).delete(fn); };
  }
  function emitBusy(threadId) {
    (busyListeners[threadId] || new Set()).forEach((fn) => {
      try { fn(!!busyByThread[threadId]); } catch {}
    });
  }
  function isBusy(threadId) { return !!busyByThread[threadId]; }

  // ── Persistence ───────────────────────────────────────────────────────────
  async function loadMessages(threadId) {
    if (!threadId) return [];
    try {
      const r = await fetch(`/api/chats/${encodeURIComponent(threadId)}/messages?include_skill_events=true`);
      if (!r.ok) return [];
      const d = await r.json();
      return Array.isArray(d.messages) ? d.messages : [];
    } catch { return []; }
  }

  /** Paint a list of DB messages into a host element with the main renderers. */
  function renderMessages(host, messages) {
    if (!host) return;
    host.innerHTML = '';
    if (!messages || !messages.length) {
      host.innerHTML = '<div class="empty"><h2>Study AI</h2><p>Ask anything about this document.</p></div>';
      return;
    }
    const prevLoading = window._historyLoading;
    window._historyLoading = false;
    for (const m of messages) {
      try { addHistoryMessage(m, host); } catch (e) { console.warn('[study-chat] render', e); }
    }
    window._historyLoading = prevLoading;
    if (typeof activateLucideIcons === 'function') activateLucideIcons(host);
    host.scrollTop = host.scrollHeight;
  }

  // ── Sending ───────────────────────────────────────────────────────────────
  /**
   * Send one study turn through /api/chat.
   * opts: { docId, threadId, text, context, pane, onDone }
   *   context → { scope, page, selection, action, doc_id } grounding for the turn
   *   pane    → container element to stream into (real chat DOM)
   */
  async function send(opts) {
    const { docId, text, pane } = opts;
    const threadId = opts.threadId || ensureThread(docId);
    if (busyByThread[threadId]) return;
    if (!text || !text.trim()) return;
    // Remember which document/note this thread belongs to, so opening it from
    // the sidebar and typing there routes back through the study bridge.
    // `docMapId` lets a surface whose map key is synthetic (the answer card)
    // still record the REAL document, so a reopened thread stays grounded.
    try { localStorage.setItem(DOCMAP_KEY + threadId, opts.docMapId || docId); } catch { /* private mode */ }

    const controller = startStream(threadId);
    busyByThread[threadId] = true;
    emitBusy(threadId);

    // EVERYTHING from here is inside one try/catch/finally.
    //
    // The busy flag above is the guard `send` checks first
    // (`if (busyByThread[threadId]) return;`), so any throw between setting it
    // and the finally wedges this thread permanently — no send, no stop, no
    // recovery short of a reload. The DOM setup (querySelectorAll, addMessage,
    // addBotStreaming) used to sit outside the try, which was exactly that
    // window. `ui` is also null-guarded in the finally, because a throw from
    // addBotStreaming meant the finally itself threw and cleared nothing.
    let ui = null;
    try {
      if (pane) pane.querySelectorAll('.empty').forEach((e) => e.remove());
      // Show attached images inline in the sent bubble (data URLs / blob URLs),
      // matching how the main composer renders pasted + attached images.
      const images = Array.isArray(opts.images) && opts.images.length ? opts.images : undefined;
      const userMsgDiv = addMessage('user', text, images, pane);
      // addBotStreaming gates its typewriter on activeChatId; pass it so the
      // animation runs (the stream itself is tracked under threadId).
      ui = addBotStreaming(pane, typeof activeChatId !== 'undefined' ? activeChatId : threadId);

      const context = Object.assign({ doc_id: docId }, opts.context || {});
      // Prefer the Study AI config's own model / thinking mode (chosen in the
      // reader's config popover) over the global chat selection, so a study turn
      // uses the model the user picked for studying.
      const body = {
        message: text,
        chat_id: threadId,
        model: context.model || (typeof selectedModel !== 'undefined' && selectedModel) || undefined,
        thinking_mode: context.thinking_mode || (typeof selectedThinkingMode !== 'undefined' && selectedThinkingMode) || undefined,
        stream: true,
        layout_mode: 'study',
        study_context: context,
      };
      // Real attachments (page image / pasted image / uploaded file). These ride
      // through the SAME /api/chat file pipeline as the main chat, so the backend
      // resolves them per-model (vision upload, doc→image conversion, etc.).
      if (Array.isArray(opts.files) && opts.files.length) body.files = opts.files;

      const res = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      if (!res.ok) {
        let detail = '';
        try { detail = await res.text(); } catch {}
        ui.appendAnswer(`\n[error] ${res.status}${detail ? ': ' + detail.slice(0, 300) : ''}`);
        return;
      }
      await consumeChatStream(res, ui, userMsgDiv, threadId, pane);
      // First turn materialised the chat row — refresh the sidebar so the
      // study chat (and its book icon) appears.
      if (typeof window._sableLoadChats === 'function') window._sableLoadChats();
    } catch (err) {
      const msg = err.name === 'AbortError' ? '\n[stopped]' : `\n[client error] ${err.message}`;
      try { if (ui) ui.appendAnswer(msg); } catch { /* renderer already gone */ }
    } finally {
      // Each step is isolated: one throwing must not prevent the busy flag
      // from being cleared, which is the whole point of the block.
      try { if (ui) ui.finalize(); } catch { /* noop */ }
      try { endStream(threadId); } catch { /* noop */ }
      busyByThread[threadId] = false;
      try { emitBusy(threadId); } catch { /* noop */ }
      try { emit(docId); } catch { /* noop */ }
    }
    // onDone fires AFTER the finally has cleared the busy flag — otherwise the
    // adapter's isBusy guard would skip its canonical re-render.
    if (typeof opts.onDone === 'function') opts.onDone();
  }

  function stop(threadId) {
    if (typeof activeStreams !== 'undefined' && activeStreams.has(threadId)) {
      try { activeStreams.get(threadId).abort(); } catch {}
    }
  }

  // ── Attachment upload ───────────────────────────────────────────────────────
  /** Upload a blob to /api/upload and return { path, filename } or null.
   *  The returned path then rides `/api/chat` `files[]`, which resolves it
   *  per-model exactly like the main composer's attachments. */
  async function uploadBlob(blob, filename) {
    try {
      const form = new FormData();
      form.append('file', blob, filename || 'attachment.png');
      const res = await fetch('/api/upload', { method: 'POST', body: form });
      const data = await res.json();
      if (!res.ok || !data.uploaded) return null;
      return { path: data.path, filename: data.filename || filename };
    } catch { return null; }
  }

  /** Convert a data: URL to a Blob (for page renders → upload). */
  function dataUrlToBlob(dataUrl) {
    try {
      const [head, b64] = String(dataUrl).split(',');
      const mime = (head.match(/data:([^;]+)/) || [])[1] || 'image/png';
      const bin = atob(b64);
      const bytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i += 1) bytes[i] = bin.charCodeAt(i);
      return new Blob([bytes], { type: mime });
    } catch { return null; }
  }

  // ── Model config (unchanged: reuses the study config endpoint) ─────────────
  let MODELS = null;
  let AI_CONFIG = null;
  async function loadModels(ctx, force) {
    if (MODELS && !force) return MODELS;
    try {
      const [m, cfg] = await Promise.all([
        ctx.api('/api/study/ai/models'),
        ctx.api('/api/study/ai/config'),
      ]);
      MODELS = (m && m.models) || [];
      AI_CONFIG = cfg || {};
    } catch { MODELS = MODELS || []; AI_CONFIG = AI_CONFIG || {}; }
    return MODELS;
  }
  function setConfig(cfg) { AI_CONFIG = Object.assign({}, AI_CONFIG || {}, cfg || {}); }
  function configPayload() {
    const c = AI_CONFIG || {};
    return { model: c.model || '', thinking_mode: c.thinking_mode || '', persona: c.persona || '', custom_prompt: c.custom_prompt || '' };
  }
  function currentCaps() {
    if (!MODELS || !MODELS.length) return {};
    const id = (AI_CONFIG && AI_CONFIG.model) || (MODELS[0] && MODELS[0].id);
    const m = MODELS.find((x) => x.id === id) || MODELS[0];
    return (m && m.capabilities) || {};
  }

  window.studyChat = {
    threadPrefix, activeThread, setActiveThread, newThread, newDetachedThread, ensureThread, listThreads,
    docForThread, uploadBlob, dataUrlToBlob,
    loadMessages, renderMessages, send, stop, isBusy, subscribe, onBusy,
    loadModels, setConfig, configPayload, currentCaps,
  };
})();
