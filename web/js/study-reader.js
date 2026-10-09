/**
 * study-reader.js — full-screen PDF reader for Study mode.
 *
 * Layout: fixed overlay (covers the app sidebar too, so the reader owns the
 * whole viewport). Left panel = page thumbnails / outline / highlights / info.
 * Main pane = continuously rendered PDF pages with a real text layer, which is
 * what makes selection + highlighting possible.
 *
 * Exposes: window.studyReader = { open(docId), close(), isOpen() }
 *
 * Loaded as an ES module (pdf.js 6.x ships .mjs only), so it must stay out of
 * the classic-script bundle and be wired via <script type="module">.
 */

import * as pdfjsLib from '/static/vendor/pdfjs/build/pdf.min.mjs';

pdfjsLib.GlobalWorkerOptions.workerSrc = '/static/vendor/pdfjs/build/pdf.worker.min.mjs';

const CMAP_URL = '/static/vendor/pdfjs/cmaps/';
const FONT_URL = '/static/vendor/pdfjs/standard_fonts/';

const ZOOM_MIN = 0.5;
const ZOOM_MAX = 3;
const ZOOM_STEP = 0.15;
const HIGHLIGHT_COLORS = ['#ffd400', '#7ee787', '#79c0ff', '#ff9ec7', '#ffab70'];

// ── State ────────────────────────────────────────────────────────────────────

let overlay = null;
let pdfDoc = null;
let meta = null;        // backend meta record
let scale = 1.2;
let currentPage = 1;
// Scroll-driven page sync. While a programmatic scroll (thumb / prev / next /
// page jump) is animating, ignore scroll-derived updates so the smooth-scroll
// intermediate positions don't yank the sidebar to a page we're passing over.
let scrollSyncSuppressUntil = 0;
let pageSyncRaf = 0;
let selectionDebounce = 0;   // debounce handle for the selectionchange listener
let lastPointer = null;      // last pointer position — selectionchange carries none
let pageEls = [];       // index 0 = page 1
let pageObserver = null;
let thumbObserver = null;
// In-flight page.render() / TextLayer.render() calls. `pdfDoc.cleanup()`
// THROWS if any page proxy is mid-render, so this is the guard that keeps the
// debounced cleanup honest. The old `rendering` boolean was declared, reset to
// false once, and never set true — a dead flag that guarded nothing.
let activeRenders = 0;
let docCleanupTimer = 0;   // debounced pdfDoc.cleanup() — see scheduleDocCleanup()
let renderGen = 0;      // bumps on every renderAllPages rebuild — stale renders bail
let progressTimer = null;
let activeSideTab = 'pages';
let topicDocs = null;       // [{id,title,pages,progress,...}] siblings of the open doc
let topicName = '';         // resolved topic name for the open doc
let topicQuery = '';        // persisted search text in the Topic panel
let topicResults = null;    // last /api/study/search payload while searching
let topicSearchBusy = false;
let topicDebounce = null;
let topicGen = 0;           // bumped on every open() — invalidates in-flight work
let pendingSelection = null;
let built = false;

// ── AI state ─────────────────────────────────────────────────────────────────
let aiConfig = null;              // {model, thinking_mode, persona, custom_prompt}
let aiModels = [];                // [{id,label,thinking_modes}]
let aiPersonas = [];              // [name, ...]
let aiCurrent = null;             // card metadata: {action, scope, page, selection, rects, question, model}
let aiNotes = [];                 // saved answers for this doc
let aiConfigLoaded = false;
let isRemote = false;   // true when reading a Drive doc not yet in the library
let lastRemoteSaved = false;  // did the server auto-ingest this remote download?

// Formats a remote Drive file can be opened as after a server-side conversion
// to PDF. Anything else must already be a PDF (or an image, which never
// reaches the reader). Mirrors the backend's CONVERTIBLE_EXT in
// engine/library_ingest.py — keep the two in step.
const REMOTE_DOC_EXT = /\.(pptx?|docx?|odt|odp|ods|rtf|txt|csv|md|epub|xlsx?)$/i;

// Session analytics — when this reader opened, and which pages were visited.
// Reported to /api/study/doc/<id>/session on close so the dashboard can show
// real study time instead of a per-device localStorage counter.
let sessionStart = 0;
let sessionPages = new Set();
let speaking = false;   // is read-aloud currently playing?

const AI_ACTION_LABELS = {
  explain: 'Explain', meaning: 'Meaning', summarize: 'Summarize',
  define: 'Define', ask: 'Ask', summary: 'Summary',
  concepts: 'Key concepts', exam: 'Exam questions', glossary: 'Glossary',
};

// ── Small helpers ────────────────────────────────────────────────────────────

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function toast(msg, kind) {
  if (typeof window.showToast === 'function') return window.showToast(msg, kind);
  console.log('[study-reader]', msg);
}

function fmtBytes(n) {
  if (!n) return '0 B';
  const u = ['B', 'KB', 'MB', 'GB'];
  const i = Math.min(u.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
  return `${(n / 1024 ** i).toFixed(i ? 1 : 0)} ${u[i]}`;
}

function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

/**
 * Fetch a URL and render a live progress bar into `host` while it downloads.
 *
 * Uses XHR rather than fetch: only XHR exposes byte-level upload/download
 * progress. Falls back to an indeterminate bar when the server sends no
 * Content-Length (Drive doesn't always).
 */
function downloadWithProgress(url, host) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('GET', url, true);
    xhr.responseType = 'arraybuffer';

    // Bypass the global fetch auth wrapper — set the bearer token by hand.
    const token = localStorage.getItem('sable_token') || '';
    if (token) xhr.setRequestHeader('Authorization', 'Bearer ' + token);

    let started = false;
    const paint = (pct, got, total) => {
      if (!host) return;
      const known = total > 0;
      const label = known
        ? `${fmtBytes(got)} of ${fmtBytes(total)}`
        : `${fmtBytes(got)} downloaded`;
      host.innerHTML = `
        <div class="rdr-dl">
          <div class="rdr-dl-icon"><i data-lucide="hard-drive-download" class="icon-lucide"></i></div>
          <div class="rdr-dl-title">Downloading from Drive…</div>
          <div class="rdr-dl-track${known ? '' : ' indeterminate'}">
            <div class="rdr-dl-fill" style="width:${known ? pct : 30}%"></div>
          </div>
          <div class="rdr-dl-meta">${label}${known ? ` · ${pct}%` : ''}</div>
        </div>`;
      if (window.lucide) window.lucide.createIcons();
    };

    xhr.onprogress = (e) => {
      started = true;
      const total = e.lengthComputable ? e.total : 0;
      const pct = total ? Math.round((e.loaded / total) * 100) : 0;
      paint(pct, e.loaded, total);
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        // The server reports whether it also saved this download to the
        // library, so the caller knows to refresh the library on close.
        lastRemoteSaved = xhr.getResponseHeader('X-Drive-Save') === '1';
        return resolve(xhr.response);
      }
      let detail = xhr.statusText;
      try {
        detail = JSON.parse(new TextDecoder().decode(xhr.response)).detail || detail;
      } catch (_) { /* non-JSON error body */ }
      reject(new Error(detail));
    };
    xhr.onerror = () => reject(new Error('network error'));
    xhr.onabort = () => reject(new Error('download cancelled'));
    xhr.send();
    // Paint immediately so the user sees the bar before the first chunk lands.
    paint(0, 0, 0);
  });
}

async function api(path, opts) {
  // A remote (un-imported) Drive doc has no library record, so every
  // /api/study/doc/<id>/... write would 404. Swallow them — the doc is
  // view-only until it's imported.
  if (isRemote && String(path).startsWith('/api/study/doc/')) {
    return null;
  }
  const res = await fetch(path, opts);          // global fetch injects the bearer token
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

// ── Build the overlay shell (once) ───────────────────────────────────────────

function build() {
  if (built) return;
  built = true;

  overlay = document.createElement('div');
  overlay.id = 'studyReader';
  overlay.className = 'rdr hidden';
  overlay.innerHTML = `
    <div class="rdr-header">
      <button class="rdr-btn" id="rdrBack" title="Back to library (Esc)">
        <i data-lucide="arrow-left" class="icon-lucide"></i>
      </button>
      <div class="rdr-title" id="rdrTitle">Document</div>
      <div class="rdr-docchat" id="rdrDocChat" role="tablist" aria-label="Document or chat">
        <button class="rdr-docchat-btn active" id="rdrTabDoc" role="tab" aria-selected="true">
          <i data-lucide="file-text" class="icon-lucide"></i> Doc
        </button>
        <button class="rdr-docchat-btn" id="rdrTabChat" role="tab" aria-selected="false">
          <i data-lucide="message-square" class="icon-lucide"></i> Chat
        </button>
        <button class="rdr-docchat-btn" id="rdrTabNotes" role="tab" aria-selected="false">
          <i data-lucide="notebook-pen" class="icon-lucide"></i> Notes
        </button>
      </div>
      <div class="rdr-spacer"></div>
      <div class="rdr-pager">
        <button class="rdr-btn" id="rdrPrev" title="Previous page"><i data-lucide="chevron-left" class="icon-lucide"></i></button>
        <span class="rdr-pageno"><input id="rdrPageInput" type="text" inputmode="numeric" value="1"> / <span id="rdrPageTotal">1</span></span>
        <button class="rdr-btn" id="rdrNext" title="Next page"><i data-lucide="chevron-right" class="icon-lucide"></i></button>
      </div>
      <div class="rdr-zoom">
        <button class="rdr-btn" id="rdrZoomOut" title="Zoom out"><i data-lucide="minus" class="icon-lucide"></i></button>
        <span class="rdr-zoomval" id="rdrZoomVal">120%</span>
        <button class="rdr-btn" id="rdrZoomIn" title="Zoom in"><i data-lucide="plus" class="icon-lucide"></i></button>
      </div>
      <div id="rdrTimerHost"></div>
      <button class="rdr-btn" id="rdrAi" title="AI settings"><i data-lucide="sparkles" class="icon-lucide"></i></button>
      <button class="rdr-btn" id="rdrBookmark" title="Bookmark"><i data-lucide="bookmark" class="icon-lucide"></i></button>
      <!-- Secondary actions live in one menu. Seven trailing icon buttons
           crowded the timer pill and zoom controls off narrow headers. -->
      <div class="rdr-more" id="rdrMoreWrap">
        <button class="rdr-btn" id="rdrMore" title="More actions"
                aria-haspopup="true" aria-expanded="false">
          <i data-lucide="more-horizontal" class="icon-lucide"></i>
        </button>
        <div class="rdr-menu hidden" id="rdrMoreMenu" role="menu">
          <button class="rdr-menu-item" id="rdrFind" role="menuitem"><i data-lucide="search" class="icon-lucide"></i> Search in document</button>
          <button class="rdr-menu-item" id="rdrSpeak" role="menuitem"><i data-lucide="volume-2" class="icon-lucide"></i> Read page aloud</button>
          <button class="rdr-menu-item" id="rdrExport" role="menuitem"><i data-lucide="file-down" class="icon-lucide"></i> Export as Markdown</button>
          <button class="rdr-menu-item" id="rdrNote" role="menuitem"><i data-lucide="notebook-pen" class="icon-lucide"></i> Quick note</button>
          <button class="rdr-menu-item" id="rdrDownload" role="menuitem"><i data-lucide="download" class="icon-lucide"></i> Download PDF</button>
        </div>
      </div>
    </div>
    <div class="rdr-body">
      <aside class="rdr-side">
        <div class="rdr-side-tabs">
          <button class="rdr-side-tab active" data-rdr-tab="pages"><i data-lucide="layout-grid" class="icon-lucide"></i> Pages</button>
          <button class="rdr-side-tab" data-rdr-tab="topic"><i data-lucide="layers" class="icon-lucide"></i> Topic</button>
          <button class="rdr-side-tab" data-rdr-tab="outline"><i data-lucide="list-tree" class="icon-lucide"></i> Outline</button>
          <button class="rdr-side-tab" data-rdr-tab="notes"><i data-lucide="highlighter" class="icon-lucide"></i> Notes</button>
          <button class="rdr-side-tab" data-rdr-tab="ai"><i data-lucide="sparkles" class="icon-lucide"></i> AI</button>
          <button class="rdr-side-tab" data-rdr-tab="info"><i data-lucide="info" class="icon-lucide"></i> Info</button>
        </div>
        <div class="rdr-side-panel" id="rdrSidePanel"></div>
      </aside>
      <main class="rdr-main" id="rdrMain">
        <div class="rdr-pages" id="rdrPages"></div>
      </main>
      <!-- Study AI pane — swaps with the PDF area via the header Doc/Chat toggle -->
      <section class="rdr-chatpane hidden" id="rdrChatPane"></section>
      <!-- Notes pane — swaps with the PDF area via the header Doc/Chat/Notes toggle -->
      <section class="rdr-notespane hidden" id="rdrNotesPane"></section>
    </div>
    <div class="rdr-hl-bar hidden" id="rdrHlBar"></div>
    <!-- Right-click menu for the document pane -->
    <div class="rdr-ctx hidden" id="rdrCtxMenu" role="menu">
      <div class="rdr-ctx-label">Copy</div>
      <button class="rdr-ctx-item" data-rdr-ctx="copy" role="menuitem">
        <i data-lucide="clipboard-copy" class="icon-lucide"></i> Copy
        <span class="rdr-ctx-hint">text</span>
      </button>
      <button class="rdr-ctx-item" data-rdr-ctx="copy-selection-image" role="menuitem">
        <i data-lucide="scan-text" class="icon-lucide"></i> Copy selection
        <span class="rdr-ctx-hint">image</span>
      </button>
      <button class="rdr-ctx-item" data-rdr-ctx="copy-page" role="menuitem">
        <i data-lucide="image" class="icon-lucide"></i> Copy page image
      </button>
      <button class="rdr-ctx-item" data-rdr-ctx="copy-page-text" role="menuitem">
        <i data-lucide="text" class="icon-lucide"></i> Copy page text
      </button>
      <div class="rdr-ctx-sep"></div>
      <div class="rdr-ctx-label"><i data-lucide="sparkles" class="icon-lucide"></i> Study AI</div>
      <button class="rdr-ctx-item" data-rdr-ctx="ask-page" role="menuitem">
        <i data-lucide="message-circle-question" class="icon-lucide"></i> Ask about this page
      </button>
      <button class="rdr-ctx-item" data-rdr-ctx="ask-pdf" role="menuitem">
        <i data-lucide="file-search" class="icon-lucide"></i> Ask about this PDF
        <span class="rdr-ctx-hint">page + doc</span>
      </button>
      <button class="rdr-ctx-item" data-rdr-ctx="explain-page" role="menuitem">
        <i data-lucide="lightbulb" class="icon-lucide"></i> Explain this page
      </button>
      <button class="rdr-ctx-item" data-rdr-ctx="summarize-page" role="menuitem">
        <i data-lucide="list" class="icon-lucide"></i> Summarize this page
      </button>
      <button class="rdr-ctx-item" data-rdr-ctx="explain-selection" role="menuitem">
        <i data-lucide="wand-2" class="icon-lucide"></i> Explain selection
      </button>
      <div class="rdr-ctx-sep"></div>
      <button class="rdr-ctx-item" data-rdr-ctx="highlight" role="menuitem">
        <i data-lucide="highlighter" class="icon-lucide"></i> Highlight selection
      </button>
      <button class="rdr-ctx-item" data-rdr-ctx="save-note" role="menuitem">
        <i data-lucide="notebook-pen" class="icon-lucide"></i> Save selection as note
      </button>
    </div>
    <!-- Floating Study AI launcher (bottom-right) -->
    <div class="rdr-ai-launch" id="rdrAiLaunch"></div>
    <!-- Floating quick-note launcher — sits beside Study AI. Pressing it opens
         the quick-note taker (selection/page → note). -->
    <button class="rdr-quicknote" id="rdrQuickNote" title="Quick note" aria-label="Quick note">
      <i data-lucide="notebook-pen" class="icon-lucide"></i>
    </button>

    <!-- AI answer card — full thread, minimal controls (new chat + send) -->
    <div class="rdr-ai-card hidden" id="rdrAiCard">
      <div class="rdr-ai-card-head">
        <span class="rdr-ai-card-title" id="rdrAiCardTitle">AI</span>
        <span class="rdr-ai-card-model" id="rdrAiCardModel"></span>
        <div class="rdr-ai-card-spacer"></div>
        <button class="rdr-ai-card-btn" id="rdrAiSave" title="Save thread"><i data-lucide="bookmark-plus" class="icon-lucide"></i></button>
        <button class="rdr-ai-card-btn" id="rdrAiNew" title="New chat"><i data-lucide="plus" class="icon-lucide"></i></button>
        <button class="rdr-ai-card-btn" id="rdrAiClose" title="Close"><i data-lucide="x" class="icon-lucide"></i></button>
      </div>
      <div class="rdr-ai-card-body" id="rdrAiCardBody"></div>
      <div class="rdr-ai-card-foot">
        <input class="rdr-ai-followup" id="rdrAiFollowup" type="text" placeholder="Ask a follow-up…">
        <button class="rdr-ai-card-btn rdr-ai-send" id="rdrAiFollowupSend" title="Send"><i data-lucide="send" class="icon-lucide"></i></button>
        <button class="rdr-ai-card-btn rdr-ai-stop" id="rdrAiStop" title="Stop" style="display:none;"><i data-lucide="square" class="icon-lucide"></i></button>
      </div>
    </div>

    <!-- AI config popover -->
    <div class="rdr-ai-config hidden" id="rdrAiConfig">
      <div class="rdr-ai-config-head">
        <i data-lucide="sparkles" class="icon-lucide"></i>
        <span>Study AI</span>
        <div class="rdr-ai-card-spacer"></div>
        <button class="rdr-ai-card-btn" id="rdrAiConfigClose"><i data-lucide="x" class="icon-lucide"></i></button>
      </div>
      <div class="rdr-ai-config-body">
        <label class="rdr-ai-field">
          <span>Model</span>
          <select id="rdrAiModel"></select>
        </label>
        <label class="rdr-ai-field">
          <span>Thinking</span>
          <select id="rdrAiThinking"></select>
        </label>
        <label class="rdr-ai-field">
          <span>Persona</span>
          <select id="rdrAiPersona"></select>
        </label>
        <label class="rdr-ai-field rdr-ai-field-col">
          <span>Custom instructions</span>
          <textarea id="rdrAiCustom" rows="3" placeholder="e.g. answer like I'm 10, use Bangla for explanations…"></textarea>
        </label>
      </div>
    </div>

    <!-- Dictionary card — pure lookup (English defs + Bangla), never touches AI -->
    <div class="rdr-dict-card hidden" id="rdrDictCard">
      <div class="rdr-dict-head">
        <i data-lucide="book-a" class="icon-lucide"></i>
        <span class="rdr-dict-word" id="rdrDictWord"></span>
        <span class="rdr-dict-src" id="rdrDictSrc"></span>
        <div class="rdr-ai-card-spacer"></div>
        <button class="rdr-ai-card-btn" id="rdrDictClose" title="Close"><i data-lucide="x" class="icon-lucide"></i></button>
      </div>
      <div class="rdr-dict-body" id="rdrDictBody"></div>
    </div>
  `;
  document.body.appendChild(overlay);

  // Highlight colour choices in the selection popup
  const bar = overlay.querySelector('#rdrHlBar');
  bar.innerHTML =
    HIGHLIGHT_COLORS.map((c) =>
      `<button class="rdr-hl-dot" data-rdr-color="${c}" style="background:${c}" title="Highlight"></button>`
    ).join('') +
    '<button class="rdr-hl-note" data-rdr-color="note"><i data-lucide="sticky-note" class="icon-lucide"></i></button>' +
    '<span class="rdr-hl-sep"></span>' +
    '<button class="rdr-hl-ai" data-rdr-ai="explain"><i data-lucide="sparkles" class="icon-lucide"></i>Explain</button>' +
    '<button class="rdr-hl-ai" data-rdr-ai="meaning"><i data-lucide="book-open" class="icon-lucide"></i>Meaning</button>' +
    '<button class="rdr-hl-ai" data-rdr-ai="summarize"><i data-lucide="list" class="icon-lucide"></i>Summarize</button>' +
    '<button class="rdr-hl-ai" data-rdr-ai="define"><i data-lucide="type" class="icon-lucide"></i>Define</button>' +
    '<button class="rdr-hl-ai" data-rdr-ai="ask"><i data-lucide="message-circle-question" class="icon-lucide"></i>Ask…</button>' +
    '<span class="rdr-hl-sep"></span>' +
    '<button class="rdr-hl-ai rdr-hl-dict" data-rdr-dict="1"><i data-lucide="book-a" class="icon-lucide"></i>Dictionary</button>' +
    '<button class="rdr-hl-cancel" id="rdrHlCancel"><i data-lucide="x" class="icon-lucide"></i></button>';

  wire();
  if (window.lucide) window.lucide.createIcons();
}

// ── Event wiring ─────────────────────────────────────────────────────────────

function wire() {
  const $ = (id) => overlay.querySelector(id);
  const bar = $('#rdrHlBar');

  $("#rdrBack").addEventListener('click', close);
  $("#rdrPrev").addEventListener('click', () => scrollToPage(currentPage - 1));
  $("#rdrNext").addEventListener('click', () => scrollToPage(currentPage + 1));
  $("#rdrZoomIn").addEventListener('click', () => setScale(scale + ZOOM_STEP));
  $("#rdrZoomOut").addEventListener('click', () => setScale(scale - ZOOM_STEP));
  $("#rdrBookmark").addEventListener('click', toggleBookmark);
  $("#rdrDownload").addEventListener('click', () => {
    if (!meta) return;
    const a = document.createElement('a');
    a.href = isRemote
      ? `/api/drive/file/${encodeURIComponent(meta.id)}/pdf`
      : `/api/study/doc/${meta.id}/pdf`;
    a.download = meta.filename || `${meta.title}.pdf`;
    document.body.appendChild(a);
    a.click();
    a.remove();
  });

  const dictClose = $("#rdrDictClose");
  if (dictClose) dictClose.addEventListener('click', closeDictCard);

  const pageInput = $("#rdrPageInput");
  pageInput.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter') return;
    e.preventDefault();
    const n = parseInt(pageInput.value, 10);
    if (Number.isFinite(n)) scrollToPage(n);
    pageInput.blur();
  });
  pageInput.addEventListener('blur', () => { pageInput.value = currentPage; });

  overlay.querySelectorAll('.rdr-side-tab').forEach((btn) => {
    btn.addEventListener('click', () => {
      activeSideTab = btn.dataset.rdrTab;
      overlay.querySelectorAll('.rdr-side-tab').forEach((b) =>
        b.classList.toggle('active', b === btn));
      renderSidePanel();
    });
  });

  // The Topic panel uses event delegation so re-renders never drop listeners,
  // and typing in its search box keeps focus across repaints.
  const sidePanelHost = overlay.querySelector('#rdrSidePanel');
  sidePanelHost.addEventListener('click', onTopicPanelClick);
  sidePanelHost.addEventListener('input', onTopicPanelInput);

  // Study tools run by the model can mutate this document (highlights, notes,
  // progress). When one does, the route emits doc_changed — refresh the panes
  // so the change appears without the user reloading.
  window.addEventListener('study-doc-changed', (e) => {
    const detail = (e && e.detail) || {};
    if (!meta) return;
    if (detail.doc_id && detail.doc_id !== meta.id) return;
    if (detail.kind === 'highlights' || detail.kind === 'bookmark') {
      api(`/api/study/doc/${meta.id}`).then((fresh) => {
        if (!fresh || !meta) return;
        meta.highlights = fresh.highlights || [];
        meta.bookmarked = fresh.bookmarked;
        layoutHighlights();
        if (activeSideTab === 'notes') renderSidePanel();
      }).catch(() => {});
    } else if (detail.kind === 'progress') {
      api(`/api/study/doc/${meta.id}`).then((fresh) => {
        if (fresh && meta) meta.progress = fresh.progress || meta.progress;
      }).catch(() => {});
    } else if (detail.kind === 'notes') {
      // The Notes pane may be showing a doc's notes — let it redraw.
      window.dispatchEvent(new CustomEvent('study-notes-changed'));
    }
  });

  // Selection → highlight bar.
  // `mouseup` on the scroll container alone missed every release that happened
  // outside #rdrMain (over the sidebar, past the window edge) and every
  // keyboard selection. Bind on document and cover both paths: pointerup for
  // the instant case, debounced selectionchange for drag + Shift+Arrow.
  const main = $("#rdrMain");
  // Remember where the pointer last landed. selectionchange carries no
  // coordinates, and its 120ms debounce lands AFTER pointerup — without this
  // the second pass would overwrite the caret-exact word from the first with
  // the inflated native selection text.
  document.addEventListener('pointerdown', (e) => {
    if (Number.isFinite(e.clientX)) lastPointer = { x: e.clientX, y: e.clientY };
  }, true);
  document.addEventListener('pointerup', onSelection);
  document.addEventListener('selectionchange', () => {
    if (!overlay || overlay.classList.contains('hidden')) return;
    clearTimeout(selectionDebounce);
    selectionDebounce = setTimeout(() => onSelection(null), 120);
  });
  main.addEventListener('scroll', onMainScroll, { passive: true });

  // Right-click context menu on the document pane.
  main.addEventListener('contextmenu', onDocContextMenu);
  const ctxMenu = $("#rdrCtxMenu");
  ctxMenu.addEventListener('click', onCtxMenuClick);
  ctxMenu.addEventListener('contextmenu', (e) => e.preventDefault());
  // Dismiss on outside click / Escape / scroll / resize.
  document.addEventListener('mousedown', (e) => {
    if (!ctxMenu.classList.contains('hidden') && !ctxMenu.contains(e.target)) hideCtxMenu();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') hideCtxMenu();
  });
  main.addEventListener('scroll', hideCtxMenu, { passive: true });
  window.addEventListener('resize', hideCtxMenu);
  document.addEventListener('mousedown', (e) => {
    if (!overlay.classList.contains('hidden') && !bar.contains(e.target)) hideHlBar();
  });

  // Dismiss the floating panels on any press outside them. The highlight bar
  // and the Study AI launcher are exempt: they are the triggers that open
  // these cards, so a press on them must not close what it is about to open.
  document.addEventListener('mousedown', (e) => {
    if (overlay.classList.contains('hidden')) return;
    const t = e.target;
    const onTrigger = bar.contains(t) ||
      (t.closest && (t.closest('#rdrAiLaunch') || t.closest('#rdrQuickNote')));

    const aiCard = overlay.querySelector('#rdrAiCard');
    if (aiCard && !aiCard.classList.contains('hidden') &&
        !aiCard.contains(t) && !onTrigger) {
      closeAiCard();
    }

    const dictCard = overlay.querySelector('#rdrDictCard');
    if (dictCard && !dictCard.classList.contains('hidden') &&
        !dictCard.contains(t) && !onTrigger) {
      closeDictCard();
    }
  });

  bar.addEventListener('click', onHighlightBarClick);

  // Doc/Chat/Notes toggle — swaps the PDF area for the AI or Notes pane.
  $("#rdrTabDoc").addEventListener('click', () => setReaderTab('doc'));
  $("#rdrTabChat").addEventListener('click', () => setReaderTab('chat'));
  const tabNotes = $("#rdrTabNotes");
  if (tabNotes) tabNotes.addEventListener('click', () => setReaderTab('notes'));

  // Quick-note capture popup (header menu item + floating launcher).
  const noteBtn = $("#rdrNote");
  if (noteBtn) noteBtn.addEventListener('click', toggleNotePopup);
  const quickNoteBtn = $("#rdrQuickNote");
  if (quickNoteBtn) quickNoteBtn.addEventListener('click', toggleNotePopup);

  // More-actions menu. Keeps the header to three trailing buttons instead of
  // seven so the timer pill and zoom controls always have room.
  const moreBtn = $("#rdrMore");
  const moreMenu = $("#rdrMoreMenu");
  const moreWrap = $("#rdrMoreWrap");
  if (moreBtn && moreMenu) {
    const closeMenu = () => {
      moreMenu.classList.add('hidden');
      moreBtn.setAttribute('aria-expanded', 'false');
    };
    moreBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      const nowHidden = moreMenu.classList.toggle('hidden');
      moreBtn.setAttribute('aria-expanded', String(!nowHidden));
    });
    // Any item click runs its own handler first, then dismisses the menu.
    moreMenu.addEventListener('click', () => closeMenu());
    document.addEventListener('click', (e) => {
      if (moreWrap && !moreWrap.contains(e.target)) closeMenu();
    });
  }

  // Search inside this document — hands off to the library search tab with a
  // doc scope so results stay anchored to the open file.
  const findBtn = $("#rdrFind");
  if (findBtn) findBtn.addEventListener('click', () => {
    if (!meta) return;
    window.studyReader.close();
    // The in-study Search tab is gone — search now lives in the rail panel.
    // Open it scoped to this document so "search inside this doc" keeps
    // meaning what it always did.
    if (window.searchPanel && window.searchPanel.searchIn) {
      window.searchPanel.searchIn(meta.id, meta.title);
    }
  });

  // Read the current page aloud (sentence-chunked streaming TTS).
  const speakBtn = $("#rdrSpeak");
  if (speakBtn) speakBtn.addEventListener('click', speakCurrentPage);

  // Export highlights + notes + saved AI answers as a Markdown download.
  const exportBtn = $("#rdrExport");
  if (exportBtn) exportBtn.addEventListener('click', () => {
    if (!meta) return;
    const a = document.createElement('a');
    a.href = `/api/study/doc/${meta.id}/export`;
    a.download = `${meta.slug || meta.title || 'document'}.md`;
    document.body.appendChild(a);
    a.click();
    a.remove();
  });

  // AI buttons
  $("#rdrAi").addEventListener('click', toggleAiConfig);
  $("#rdrAiConfigClose").addEventListener('click', () => $("#rdrAiConfig").classList.add('hidden'));
  $("#rdrAiClose").addEventListener('click', closeAiCard);
  $("#rdrAiSave").addEventListener('click', saveAiNote);
  $("#rdrAiNew").addEventListener('click', newAiChat);
  $("#rdrAiFollowupSend").addEventListener('click', sendFollowup);
  $("#rdrAiStop").addEventListener('click', () => {
    if (meta && window.studyAI) window.studyAI.stopStream(meta.id);
  });
  $("#rdrAiFollowup").addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); sendFollowup(); }
  });
  $("#rdrAiModel").addEventListener('change', onAiModelChange);
  $("#rdrAiThinking").addEventListener('change', persistAiConfig);
  $("#rdrAiPersona").addEventListener('change', persistAiConfig);
  $("#rdrAiCustom").addEventListener('blur', persistAiConfig);

  document.addEventListener('keydown', onKeydown);
  window.addEventListener('resize', debounce(() => { if (isOpen()) relayout(); }, 200));

  // A note created/edited/deleted anywhere refreshes the Notes pane live.
  window.addEventListener('study-notes-changed', () => {
    if (isOpen() && readerTab === 'notes') renderNotesPane();
  });
}

/**
 * Toggle read-aloud for the current page.
 *
 * Reuses the global streaming TTS player (window.playAutoTTS), which chunks
 * the text into sentences and prefetches ahead so playback has no gaps.
 */
async function speakCurrentPage() {
  const btn = overlay.querySelector('#rdrSpeak');
  if (!btn) return;

  if (speaking) {
    if (typeof window.stopGlobalTTS === 'function') {
      try { window.stopGlobalTTS(); } catch { /* best effort */ }
    }
    speaking = false;
    btn.classList.remove('active');
    // Mirror the state onto the menu button — the item itself is hidden while
    // the menu is closed, so otherwise nothing shows that TTS is running.
    const more = overlay.querySelector('#rdrMore');
    if (more) more.classList.remove('active');
    return;
  }

  if (typeof window.playAutoTTS !== 'function') {
    toast('Text-to-speech is unavailable', 'error');
    return;
  }

  let text = '';
  try { text = await pageToText(currentPage); } catch { text = ''; }
  if (!text || !text.trim()) {
    toast('No text on this page to read', 'error');
    return;
  }

  speaking = true;
  btn.classList.add('active');
  const moreBtn = overlay.querySelector('#rdrMore');
  if (moreBtn) moreBtn.classList.add('active');
  window.playAutoTTS(text);
}

function onKeydown(e) {
  if (!isOpen()) return;
  const tag = (e.target.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea') return;
  if (e.key === 'Escape') {
    // Escape closes the AI card / config first, then the highlight bar, then the reader.
    const moreMenu = overlay.querySelector('#rdrMoreMenu');
    if (moreMenu && !moreMenu.classList.contains('hidden')) {
      moreMenu.classList.add('hidden');
      const mb = overlay.querySelector('#rdrMore');
      if (mb) mb.setAttribute('aria-expanded', 'false');
      return;
    }
    if (notePopup) { closeNotePopup(); return; }
    if (!overlay.querySelector('#rdrAiConfig').classList.contains('hidden')) {
      overlay.querySelector('#rdrAiConfig').classList.add('hidden');
      return;
    }
    if (!overlay.querySelector('#rdrDictCard').classList.contains('hidden')) {
      closeDictCard();
      return;
    }
    if (!overlay.querySelector('#rdrAiCard').classList.contains('hidden')) {
      closeAiCard();
      return;
    }
    hideHlBar();
    close();
    return;
  }
  if (e.key === 'ArrowRight' || e.key === 'PageDown') { e.preventDefault(); scrollToPage(currentPage + 1); }
  if (e.key === 'ArrowLeft' || e.key === 'PageUp') { e.preventDefault(); scrollToPage(currentPage - 1); }
}

// ── Open / close ─────────────────────────────────────────────────────────────

/**
 * open(docId)                    — a library document
 * open(fileId, { remote: true }) — a Drive file streamed straight from Drive
 *
 * Remote mode renders the PDF without importing it: bytes come from
 * /api/drive/file/<id>/pdf, and every study-library write (progress,
 * highlights, notes, AI) is skipped since there is no library record yet.
 */
async function open(docId, opts) {
  // studyMode.openDoc() deep-links with a page number as the second argument,
  // while the Drive path passes an options object. Accept both shapes.
  let startPage = 0;
  if (typeof opts === 'number') { startPage = opts; opts = {}; }
  isRemote = !!(opts && opts.remote);
  build();
  overlay.classList.remove('hidden');
  document.body.classList.add('rdr-open');
  overlay.querySelector('#rdrPages').innerHTML = '<div class="rdr-loading">Loading document…</div>';
  overlay.querySelector('#rdrSidePanel').innerHTML = '';
  teardown();
  // Invalidate any in-flight topic work from the previous document.
  topicGen++;
  topicDocs = null;
  topicName = '';
  topicQuery = '';
  topicResults = null;
  topicSearchBusy = false;

  if (isRemote) {
    // Synthesize a meta record so the rest of the viewer works unchanged.
    meta = {
      id: docId,
      title: (opts && opts.title) || 'Drive document',
      filename: (opts && opts.title) || 'document.pdf',
      remote: true,
      progress: { current_page: 1, percent: 0 },
      highlights: [],
      outline: [],
    };
  } else {
    try {
      meta = await api(`/api/study/doc/${docId}`);
    } catch (err) {
      overlay.querySelector('#rdrPages').innerHTML =
        `<div class="rdr-loading rdr-error">Could not load document: ${esc(err.message)}</div>`;
      return;
    }
  }

  // Fetch bytes ourselves so the auth-wrapped fetch applies the bearer token.
  let buffer;
  try {
    if (isRemote) {
      // Drive files are downloaded on first open. Show real progress — this can
      // be megabytes, and a dead spinner reads as a hang.
      const title = (opts && opts.title) || '';
      // pdf.js can only parse actual PDFs. A .pptx or .docx arriving from
      // /pdf is OOXML bytes wearing a pdf/octet-stream label, so the parse
      // dies. Those formats go through /docpdf, which runs the same
      // LibreOffice conversion an upload would.
      const src = REMOTE_DOC_EXT.test(title)
        ? `/api/drive/file/${encodeURIComponent(docId)}/docpdf?title=${encodeURIComponent(title)}&save=true`
        : `/api/drive/file/${encodeURIComponent(docId)}/pdf?title=${encodeURIComponent(title || 'document.pdf')}&save=true`;
      buffer = await downloadWithProgress(src, overlay.querySelector('#rdrPages'));
    } else {
      const res = await fetch(`/api/study/doc/${docId}/pdf`);
      if (!res.ok) throw new Error(res.statusText);
      buffer = await res.arrayBuffer();
    }
  } catch (err) {
    overlay.querySelector('#rdrPages').innerHTML =
      `<div class="rdr-loading rdr-error">Could not fetch document: ${esc(err.message)}</div>`;
    return;
  }

  try {
    pdfDoc = await pdfjsLib.getDocument({
      data: buffer,
      cMapUrl: CMAP_URL,
      cMapPacked: true,
      standardFontDataUrl: FONT_URL,
    }).promise;
  } catch (err) {
    overlay.querySelector('#rdrPages').innerHTML =
      `<div class="rdr-loading rdr-error">Could not parse PDF: ${esc(err.message)}</div>`;
    return;
  }

  meta.pages = pdfDoc.numPages;
  // A remote open streams through /api/drive/file/<id>/pdf, which ingests the
  // bytes on the way past. By now the library may already hold a record for this
  // file — adopt it BEFORE the toolbar is wired, because leaving remote mode is
  // what unlocks Study AI, notes, progress and bookmarks on a Drive document
  // without the user ever pressing Import.
  if (isRemote) await adoptRemoteDoc();
  aiNotes = meta.ai_notes || [];
  if (!isRemote) loadAiConfig();
  // Load this document's topic siblings for the Topic side panel.
  if (!isRemote) loadTopicSiblings();
  // Anything still remote at this point genuinely has no library record to hang
  // state off, so it stays view-only.
  for (const sel of ['#rdrBookmark', '#rdrAi', '#rdrDocChat', '#rdrAiLaunch', '#rdrExport', '#rdrFind', '#rdrSpeak']) {
    const el = overlay.querySelector(sel);
    if (el) el.style.display = isRemote ? 'none' : '';
  }
  // Cap the header title so a long filename can't push the pager / zoom / AI
  // buttons out of the bar. The full name stays available on hover.
  const fullTitle = meta.title || 'Document';
  overlay.querySelector('#rdrTitle').textContent =
    fullTitle.length > 25 ? fullTitle.slice(0, 25).trimEnd() + '…' : fullTitle;
  overlay.querySelector('#rdrTitle').title = fullTitle;
  // Fresh AI surfaces for this doc (the context resolves meta lazily).
  aiPane = null;
  aiFloat = null;
  const chatHost = overlay.querySelector('#rdrChatPane');
  if (chatHost) chatHost.innerHTML = '';
  const launchHost = overlay.querySelector('#rdrAiLaunch');
  if (launchHost) launchHost.innerHTML = '';
  setReaderTab('doc');
  if (window.studyAI && !isRemote) {
    window.studyAI.loadModels(aiContext(overlay));
    // Mount the floating launcher now that the shell exists.
    ensureAiFloat();
  }
  // Timer / stopwatch / alarm widget in the reader header (available for any
  // doc, remote or not).
  const timerHost = overlay.querySelector('#rdrTimerHost');
  if (timerHost && window.studyTimer && !timerHost.dataset.mounted) {
    timerHost.dataset.mounted = '1';
    window.studyTimer.mount(timerHost);
  }
  overlay.querySelector('#rdrPageTotal').textContent = pdfDoc.numPages;
  overlay.querySelector('#rdrZoomVal').textContent = Math.round(scale * 100) + '%';
  overlay.querySelector('#rdrBookmark').classList.toggle('active', !!meta.bookmarked);

  await renderAllPages();
  renderSidePanel();

  // A deep-linked page wins over the stored reading position.
  const start = startPage || (meta.progress && meta.progress.current_page) || 1;
  sessionStart = Date.now();
  sessionPages = new Set([start]);
  speaking = false;
  requestAnimationFrame(() => scrollToPage(start, false));
}

/**
 * Report this reading session to the analytics store.
 *
 * Called from close(), before isRemote is reset — remote Drive docs are
 * view-only and have no library record to attach time to. `keepalive` lets the
 * request outlive the page/tab teardown that follows.
 */
function recordSession() {
  if (!meta || isRemote || !sessionStart) return;
  const seconds = (Date.now() - sessionStart) / 1000;
  sessionStart = 0;
  if (seconds < 5) { sessionPages = new Set(); return; }
  try {
    fetch(`/api/study/doc/${meta.id}/session`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${localStorage.getItem('sable_token') || ''}`,
      },
      body: JSON.stringify({ seconds, pages: [...sessionPages] }),
      keepalive: true,
    }).catch(() => {});
  } catch { /* analytics must never break closing the reader */ }
  sessionPages = new Set();
}

function close() {
  if (!overlay) return;
  recordSession();
  if (speaking && typeof window.stopGlobalTTS === 'function') {
    try { window.stopGlobalTTS(); } catch { /* best effort */ }
  }
  speaking = false;
  overlay.classList.add('hidden');
  document.body.classList.remove('rdr-open');
  hideHlBar();
  teardown();
  const wasRemote = isRemote;
  const savedRemote = wasRemote && lastRemoteSaved;
  isRemote = false;
  lastRemoteSaved = false;
  if (!wasRemote) {
    window.dispatchEvent(new CustomEvent('study-reader-closed'));
  } else if (savedRemote) {
    // A remote Drive doc is auto-ingested server-side while it streams
    // (X-Drive-Save). That just added/changed a library entry, so tell the
    // library to reload — otherwise the new doc only appears after a restart.
    window.dispatchEvent(new CustomEvent('study-library-changed'));
  }
}

function teardown() {
  closeNotePopup();
  if (window.studyNotes) { try { window.studyNotes.close(); } catch {} }
  if (pageObserver) { pageObserver.disconnect(); pageObserver = null; }
  if (thumbObserver) { thumbObserver.disconnect(); thumbObserver = null; }
  clearTimeout(docCleanupTimer);
  for (const rec of pageEls) releaseCanvas(rec);
  if (pdfDoc) { try { pdfDoc.destroy(); } catch {} pdfDoc = null; }
  if (aiUnsub) { try { aiUnsub(); } catch { /* noop */ } aiUnsub = null; }
  for (const pg of pageCache.values()) { try { pg.cleanup(); } catch { /* noop */ } }
  pageCache.clear();
  pageEls = [];
  pendingSelection = null;
  aiCurrent = null;
  aiNotes = [];
  activeRenders = 0;
  // AI pane/popup are per-document; drop them so the next open rebuilds.
  if (aiPane && aiPane.view && aiPane.view.dispose) { try { aiPane.view.dispose(); } catch {} }
  if (aiFloat && aiFloat.getView && aiFloat.getView()) {
    const v = aiFloat.getView();
    if (v && v.dispose) { try { v.dispose(); } catch {} }
  }
  aiPane = null;
  aiFloat = null;
}

function isOpen() {
  return !!overlay && !overlay.classList.contains('hidden');
}

// ── Page rendering ───────────────────────────────────────────────────────────

// A cached PDFPageProxy pins the page's parsed operator list AND its decoded
// embedded images. These slide decks carry ~2 full-bleed PNGs per page, which
// expand to ~14 MB of RGBA each — so an unbounded cache over 74 pages is a
// gigabyte the reader never gives back. Small LRU instead; pdf.js re-parses
// on demand.
const PAGE_CACHE_MAX = 10;
const pageCache = new Map();

async function getPage(n) {
  const hit = pageCache.get(n);
  if (hit) {
    pageCache.delete(n);          // LRU touch
    pageCache.set(n, hit);
    return hit;
  }
  const p = await pdfDoc.getPage(n);
  pageCache.set(n, p);
  while (pageCache.size > PAGE_CACHE_MAX) {
    const [oldest, pg] = pageCache.entries().next().value;
    pageCache.delete(oldest);
    if (pg) { try { pg.cleanup(); } catch { /* already gone */ } }
  }
  return p;
}

/**
 * Release a page's canvas backing store.
 *
 * `canvas.width = 0` is what actually drops the GPU texture. Detaching the
 * element only marks it collectable, and the browser is in no hurry.
 */
function releaseCanvas(rec) {
  if (!rec || !rec.el) return;
  const canvas = rec.el.querySelector('.rdr-canvas');
  if (canvas) { canvas.width = 0; canvas.height = 0; }
}

/**
 * Demote a page that scrolled out of range back to an empty placeholder.
 *
 * Without this the reader is a roach motel: pages check in and never leave, so
 * memory grows with scroll distance and a zoom rebuild multiplies every
 * retained canvas by the new scale factor.
 */
function unrenderPage(n) {
  const rec = pageEls[n - 1];
  if (!rec || !rec.rendered) return;
  // Never tear down the page a live selection lives in. `innerHTML = ''`
  // removes the very nodes the Range points at, which collapses the selection
  // and yanks the highlight bar away mid-gesture. This is why scrolling with
  // text selected felt like it "disabled" selection on the pages above.
  const liveSel = window.getSelection();
  if (liveSel && liveSel.rangeCount && !liveSel.isCollapsed) {
    const node = liveSel.getRangeAt(0).startContainer;
    const host = node.nodeType === 3 ? node.parentNode : node;
    if (host && host.closest && host.closest('.rdr-page') === rec.el) return;
  }
  if (rec.textLayer) { try { rec.textLayer.cancel(); } catch {} rec.textLayer = null; }
  const tl = rec.el.querySelector('.rdr-text-layer');
  if (tl) tl.innerHTML = '';
  releaseCanvas(rec);
  rec.rendered = false;
  const pg = pageCache.get(n);
  if (pg) {
    pageCache.delete(n);
    try { pg.cleanup(); } catch { /* best effort */ }
  }
  // `page.cleanup()` above only frees ONE page's render-time objects. The
  // transport keeps the parsed page + operator lists for every page you have
  // ever visited — that is the real RAM growth. `pdfDoc.cleanup()` releases it.
  scheduleDocCleanup();
}

/**
 * Debounced document-level cleanup.
 *
 * Do NOT call `pdfDoc.cleanup()` inline here. Reading pdf.js's own
 * `startCleanup()`:
 *   - it THROWS if any page is mid-render, and it returns a *promise*, so an
 *     inline try/catch around the call catches nothing — you get an unhandled
 *     rejection instead;
 *   - `cleanup(false)` calls `fontLoader.clear()`, so firing it on every
 *     eviction would drop the font cache and force every subsequent page to
 *     re-parse its fonts — slower and flickerier than the RAM it saves.
 * So: debounce it, and pass `true` to keep the font loader alive.
 */
function scheduleDocCleanup() {
  clearTimeout(docCleanupTimer);
  docCleanupTimer = setTimeout(() => {
    // Never while a page is rendering — startCleanup throws on that.
    if (!pdfDoc || activeRenders > 0) return scheduleDocCleanup();
    Promise.resolve()
      .then(() => pdfDoc.cleanup(true))
      .catch(() => { /* rejects if a render slipped in — next tick retries */ });
  }, 2000);
}

async function renderAllPages() {
  const gen = ++renderGen;
  // Tear down the previous observer; otherwise the old one keeps firing
  // renderPage on detached nodes after a zoom rebuild.
  if (pageObserver) {
    pageObserver.disconnect();
    pageObserver = null;
  }

  const host = overlay.querySelector('#rdrPages');
  // Zero every canvas before detaching. innerHTML = '' drops the nodes, but a
  // canvas backing store is a GPU texture that only goes away on the next GC
  // pass — and one zoom gesture can queue dozens of them.
  for (const rec of pageEls) releaseCanvas(rec);
  host.innerHTML = '';
  pageEls = [];

  // Placeholder geometry from page 1 only. Awaiting getPage() for every page
  // here serialises N round-trips into the worker and stalls the whole reader
  // on large documents — the real viewport is applied lazily in renderPage().
  const first = await getPage(1);
  if (gen !== renderGen) return;  // a newer rebuild superseded this one
  const vp0 = first.getViewport({ scale });
  const w = Math.floor(vp0.width);
  const h = Math.floor(vp0.height);

  const frag = document.createDocumentFragment();
  for (let n = 1; n <= pdfDoc.numPages; n++) {
    const el = document.createElement('div');
    el.className = 'rdr-page';
    el.dataset.page = String(n);
    el.style.width = `${w}px`;
    el.style.height = `${h}px`;
    el.innerHTML = `
      <canvas class="rdr-canvas"></canvas>
      <div class="rdr-text-layer"></div>
      <div class="rdr-hl-layer"></div>
      <div class="rdr-ai-layer"></div>
      <div class="rdr-page-num">${n}</div>
    `;
    frag.appendChild(el);
    pageEls.push({ n, el, rendered: false });
  }
  host.appendChild(frag);

  layoutHighlights();
  layoutAiNotes();

  // Render only what's near the viewport.
  const main = overlay.querySelector('#rdrMain');
  // ONE band for both rasterise and eviction.
  // The old split — render in at 600px, evict out at 1500px — left a hole. A
  // page evicted past 1500px never came back at 1400px, because the evict
  // observer only evicted and the render observer's band was tighter. Pages
  // sat blank across a ~900px band and popped in late. That pop-in is the
  // flicker you were seeing while scrolling. Same rootMargin, one decision.
  pageObserver = new IntersectionObserver((entries) => {
    if (gen !== renderGen) return;  // stale observer from an older layout
    for (const entry of entries) {
      const n = parseInt(entry.target.dataset.page, 10);
      // Active-page tracking stays scroll-driven (see schedulePageSync) — a
      // wide rootMargin makes several off-screen pages report as intersecting,
      // so the observer can't be trusted to pick the current one.
      if (entry.isIntersecting) renderPage(n);
      else unrenderPage(n);
    }
  }, { root: main, rootMargin: '1000px 0px', threshold: 0 });

  pageEls.forEach((p) => pageObserver.observe(p.el));
  schedulePageSync();
}

async function renderPage(n) {
  const rec = pageEls[n - 1];
  if (!rec || rec.rendered) return;
  rec.rendered = true;
  const gen = renderGen;

  const page = await getPage(n);
  if (gen !== renderGen) return;  // layout changed while we were fetching
  if (!rec.rendered) return;      // evicted while the page was loading
  const canvas = rec.el.querySelector('.rdr-canvas');
  const ctx = canvas.getContext('2d', { alpha: false });
  // Canvas area grows with the square of scale. At 3x zoom on a HiDPI panel a
  // single page is 4320x3240 = 14 Mpx = 56 MB. Retina sharpness is worth
  // paying for at 1x, not at 3x.
  // Never drop below native DPR. The old `scale >= 2 ? 1 : 2` cap threw away
  // half the resolution exactly when the user zoomed in to read — that was the
  // soft, mushy text at high zoom. Memory is bounded by the eviction band
  // instead (only ~1 page in flight at high zoom, ~14 MB worst case), which is
  // the right lever for this. 2x ceiling stays for the 3x-zoom 4K case.
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const viewport = page.getViewport({ scale });

  // Correct the placeholder size now that the true viewport is known.
  rec.el.style.width = `${Math.floor(viewport.width)}px`;
  rec.el.style.height = `${Math.floor(viewport.height)}px`;

  canvas.width = Math.floor(viewport.width * dpr);
  canvas.height = Math.floor(viewport.height * dpr);
  canvas.style.width = `${Math.floor(viewport.width)}px`;
  canvas.style.height = `${Math.floor(viewport.height)}px`;

  activeRenders++;
  try {
    await page.render({
      canvasContext: ctx,
      viewport,
      transform: dpr !== 1 ? [dpr, 0, 0, dpr, 0, 0] : null,
    }).promise;
  } catch {
    return;
  } finally {
    activeRenders--;
  }

  await renderTextLayer(rec, page, viewport);
  layoutHighlights();
  layoutAiNotes();
}

async function renderTextLayer(rec, page, viewport) {
  let container = rec.el.querySelector('.rdr-text-layer');
  // Cancel any in-flight layer before wiping — two renders into one container
  // leaves duplicated spans and broken selection geometry.
  if (rec.textLayer) { try { rec.textLayer.cancel(); } catch {} rec.textLayer = null; }
  // Clone-and-swap instead of innerHTML = ''. TextLayer.render() schedules work
  // across rAF ticks, so a cancelled layer can still append its .endOfContent
  // marker after the wipe. Those orphans pile up on every evict/re-render and
  // poison range.getClientRects() — which is why selection geometry drifted
  // worse the longer a session ran. The old node dies with all its spans.
  const freshLayer = container.cloneNode(false);
  container.replaceWith(freshLayer);
  container = freshLayer;
  // pdf.js positions text spans using these CSS vars — both are needed across v5/v6.
  rec.el.style.setProperty('--scale-factor', String(scale));
  rec.el.style.setProperty('--total-scale-factor', String(scale));
  container.style.setProperty('--scale-factor', String(scale));
  container.style.setProperty('--total-scale-factor', String(scale));
  container.style.width = `${Math.floor(viewport.width)}px`;
  container.style.height = `${Math.floor(viewport.height)}px`;

  try {
    const textContent = await page.getTextContent();
    const layer = new pdfjsLib.TextLayer({
      textContentSource: textContent,
      container,
      viewport,
    });
    // Counted too: startCleanup() also clears TextLayer's shared static caches,
    // which would corrupt a layer that is still mid-render.
    activeRenders++;
    try {
      await layer.render();
    } finally {
      activeRenders--;
    }
    rec.textLayer = layer;
  } catch (err) {
    console.warn('[study-reader] text layer failed', err);
  }
}

// ── Zoom / relayout ──────────────────────────────────────────────────────────

function setScale(next) {
  const clamped = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, next));
  if (Math.abs(clamped - scale) < 0.001) return;
  const anchorPage = currentPage;
  scale = clamped;
  overlay.querySelector('#rdrZoomVal').textContent = Math.round(scale * 100) + '%';

  // renderAllPages() resets every page's `rendered` flag and re-observes, so
  // the IntersectionObserver re-rasterises the visible pages itself. Resetting
  // the flags again here raced the observer into double-rendering (flicker).
  renderAllPages().then(() => requestAnimationFrame(() => scrollToPage(anchorPage, false)));
}

function relayout() {
  if (!pdfDoc) return;
  const anchor = currentPage;
  renderAllPages().then(() => requestAnimationFrame(() => scrollToPage(anchor, false)));
}

// ── Navigation ───────────────────────────────────────────────────────────────

function scrollToPage(n, smooth = true) {
  const target = Math.max(1, Math.min(n, pageEls.length));
  const rec = pageEls[target - 1];
  if (!rec) return;
  const main = overlay.querySelector('#rdrMain');
  const top = rec.el.offsetTop - 16;
  // Hold off scroll-derived sync until the animation settles, then the probe
  // confirms the target. Instant jumps need no suppression window.
  scrollSyncSuppressUntil = smooth ? performance.now() + 700 : 0;
  main.scrollTo({ top, behavior: smooth ? 'smooth' : 'auto' });
  setCurrentPage(target);
}

function setCurrentPage(n) {
  if (n === currentPage) return;
  currentPage = n;
  sessionPages.add(n);
  overlay.querySelector('#rdrPageInput').value = n;
  renderThumbActive();
  renderOutlineActive();
  scheduleProgress();
}

/**
 * Which page currently owns the top of the viewport.
 *
 * Probes ~28% down the scroll container rather than at its very edge, so a
 * page counts as current once it owns the upper third of the view — matching
 * what the reader actually sees, and avoiding a flick back to a page whose
 * last sliver is still on screen.
 */
function pageAtViewportTop() {
  const main = overlay.querySelector('#rdrMain');
  if (!main || !pageEls.length) return currentPage;
  const probe = main.scrollTop + main.clientHeight * 0.28;
  let best = pageEls[0].n;
  for (const p of pageEls) {
    if (p.el.offsetTop <= probe) best = p.n;
    else break;  // pages are in document order — first one past the probe ends it
  }
  return best;
}

/** rAF-throttled scroll handler: one page computation per frame, max. */
function schedulePageSync() {
  if (pageSyncRaf) return;
  pageSyncRaf = requestAnimationFrame(() => {
    pageSyncRaf = 0;
    if (performance.now() < scrollSyncSuppressUntil) return;
    setCurrentPage(pageAtViewportTop());
  });
}

/** Highlight the outline entry that owns the current page. */
function renderOutlineActive() {
  const items = overlay.querySelectorAll('.rdr-outline-item');
  if (!items.length) return;
  // The active entry is the LAST one starting at or before the current page —
  // a section stays active until the next heading is actually reached.
  let activeBtn = null;
  items.forEach((b) => {
    if (parseInt(b.dataset.page, 10) <= currentPage) activeBtn = b;
  });
  items.forEach((b) => b.classList.toggle('active', b === activeBtn));
  if (activeBtn && activeBtn.scrollIntoView) activeBtn.scrollIntoView({ block: 'nearest' });
}

function onMainScroll() {
  hideHlBar();
  schedulePageSync();
}

function scheduleProgress() {
  if (!meta) return;
  clearTimeout(progressTimer);
  progressTimer = setTimeout(async () => {
    const total = pageEls.length || 1;
    const percent = Math.round(((currentPage - 1) / total) * 100);
    meta.progress = { current_page: currentPage, percent };
    try {
      await api(`/api/study/doc/${meta.id}/progress`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ current_page: currentPage, percent }),
      });
    } catch { /* progress is best-effort */ }
    // Share the reading position with the room. Debounced by the same 700ms
    // timer, so scrolling does not spam the op-log.
    pushToRoom('progress', { doc_id: meta.id, page: currentPage, percent });
  }, 700);
}

/**
 * Push a change into the open study room, if there is one.
 *
 * Guarded so every reader action can call it unconditionally: when the user is
 * not in a room this is a cheap no-op, and study-rooms.js is loaded before the
 * reader.
 */
function pushToRoom(kind, payload) {
  if (!window.studyRooms || !window.studyRooms.isInRoom()) return;
  try { window.studyRooms.pushOp(kind, payload); } catch { /* never break reading */ }
}

async function toggleBookmark() {
  if (!meta) return;
  try {
    const r = await api(`/api/study/doc/${meta.id}/bookmark`, { method: 'POST' });
    meta.bookmarked = r.bookmarked;
    overlay.querySelector('#rdrBookmark').classList.toggle('active', !!r.bookmarked);
    toast(r.bookmarked ? 'Bookmarked' : 'Bookmark removed', 'success');
  } catch (err) {
    toast('Bookmark failed: ' + err.message, 'error');
  }
}

// ── Sidebar ──────────────────────────────────────────────────────────────────

function renderSidePanel() {
  const host = overlay.querySelector('#rdrSidePanel');
  if (!meta) { host.innerHTML = ''; return; }

  if (activeSideTab === 'pages') return renderThumbs(host);
  if (activeSideTab === 'topic') return renderTopicPanel(host);
  if (activeSideTab === 'outline') return renderOutline(host);
  if (activeSideTab === 'notes') return renderHighlights(host);
  if (activeSideTab === 'ai') return renderAiPanel(host);
  return renderInfo(host);
}

// ── AI side panel ────────────────────────────────────────────────────────────

function renderAiPanel(host) {
  const notes = aiNotes.slice().sort((a, b) => (a.page || 0) - (b.page || 0) || a.created - b.created);
  host.innerHTML = `
    <div class="rdr-ai-panel">
      <div class="rdr-ai-actions">
        <button class="rdr-ai-action" data-ai-doc="summary"><i data-lucide="file-text" class="icon-lucide"></i>Summarize doc</button>
        <button class="rdr-ai-action" data-ai-doc="concepts"><i data-lucide="lightbulb" class="icon-lucide"></i>Key concepts</button>
        <button class="rdr-ai-action" data-ai-doc="exam"><i data-lucide="graduation-cap" class="icon-lucide"></i>Exam questions</button>
        <button class="rdr-ai-action" data-ai-doc="glossary"><i data-lucide="book-a" class="icon-lucide"></i>Glossary</button>
        <button class="rdr-ai-action" data-ai-page="1"><i data-lucide="file-search" class="icon-lucide"></i>Summarize this page</button>
      </div>
      <div class="rdr-ai-notes-title">Saved answers (${notes.length})</div>
      <div class="rdr-ai-notes">
        ${notes.length ? notes.map((n) => `
          <div class="rdr-ai-note" data-page="${n.page}" data-note="${n.id}">
            <div class="rdr-ai-note-top">
              <span class="rdr-ai-note-badge">${esc(AI_ACTION_LABELS[n.action] || n.action)}</span>
              <span class="rdr-ai-note-page">${n.page ? 'p.' + n.page : 'doc'}</span>
              <button class="rdr-hl-del" data-del-ai="${n.id}" title="Delete"><i data-lucide="trash-2" class="icon-lucide"></i></button>
            </div>
            <div class="rdr-ai-note-q">${esc((n.question || n.text || '').slice(0, 140))}</div>
            <button class="rdr-ai-note-view" data-view-ai="${n.id}"><i data-lucide="eye" class="icon-lucide"></i>View answer</button>
          </div>`).join('')
          : '<div class="rdr-empty">No saved answers yet.<br><span>Select text and pick an AI action, then hit save.</span></div>'}
      </div>
    </div>`;

  host.querySelectorAll('[data-ai-doc]').forEach((b) =>
    b.addEventListener('click', () => runAi({ action: b.dataset.aiDoc, scope: 'doc' })));
  host.querySelectorAll('[data-ai-page]').forEach((b) =>
    b.addEventListener('click', () => runAi({ action: 'summarize', scope: 'page', page: currentPage })));
  host.querySelectorAll('[data-view-ai]').forEach((b) =>
    b.addEventListener('click', (e) => { e.stopPropagation(); viewSavedNote(b.dataset.viewAi); }));
  host.querySelectorAll('[data-del-ai]').forEach((b) =>
    b.addEventListener('click', async (e) => {
      e.stopPropagation();
      try {
        await api(`/api/study/doc/${meta.id}/notes/${b.dataset.delAi}`, { method: 'DELETE' });
        aiNotes = aiNotes.filter((n) => n.id !== b.dataset.delAi);
        meta.ai_notes = aiNotes;
        layoutAiNotes();
        renderAiPanel(host);
        if (window.lucide) window.lucide.createIcons();
      } catch (err) { toast('Delete failed: ' + err.message, 'error'); }
    }));
  if (window.lucide) window.lucide.createIcons();
}

function renderThumbs(host) {
  host.innerHTML = `<div class="rdr-thumbs" id="rdrThumbs">${pageEls.map((p) => `
    <button class="rdr-thumb" data-page="${p.n}">
      <canvas class="rdr-thumb-canvas" width="120" height="160"></canvas>
      <span class="rdr-thumb-num">${p.n}</span>
    </button>`).join('')}</div>`;

  host.querySelectorAll('.rdr-thumb').forEach((btn) => {
    btn.addEventListener('click', () => scrollToPage(parseInt(btn.dataset.page, 10)));
  });

  const scroller = host.querySelector('#rdrThumbs');
  thumbObserver = new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (e.isIntersecting) drawThumb(parseInt(e.target.dataset.page, 10));
    }
  }, { root: scroller, rootMargin: '200px 0px' });
  host.querySelectorAll('.rdr-thumb').forEach((b) => thumbObserver.observe(b));
  renderThumbActive();
}

async function drawThumb(n) {
  const btn = overlay.querySelector(`.rdr-thumb[data-page="${n}"]`);
  if (!btn || btn.dataset.drawn === '1') return;
  btn.dataset.drawn = '1';
  try {
    const page = await getPage(n);
    const canvas = btn.querySelector('canvas');
    const ctx = canvas.getContext('2d', { alpha: false });
    const base = page.getViewport({ scale: 1 });
    const s = Math.min(120 / base.width, 160 / base.height);
    const vp = page.getViewport({ scale: s });
    // Thumbnails were rendering at 1 CSS px per device px on a HiDPI panel and
    // looked like smudges next to the page. Match the main render path.
    const tdpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.floor(vp.width * tdpr);
    canvas.height = Math.floor(vp.height * tdpr);
    canvas.style.width = `${Math.floor(vp.width)}px`;
    canvas.style.height = `${Math.floor(vp.height)}px`;
    await page.render({
      canvasContext: ctx,
      viewport: vp,
      transform: tdpr !== 1 ? [tdpr, 0, 0, tdpr, 0, 0] : null,
    }).promise;
  } catch { /* ignore */ }
}

function renderThumbActive() {
  overlay.querySelectorAll('.rdr-thumb').forEach((b) =>
    b.classList.toggle('active', parseInt(b.dataset.page, 10) === currentPage));
  const active = overlay.querySelector('.rdr-thumb.active');
  if (active && active.scrollIntoView) active.scrollIntoView({ block: 'nearest' });
}

function renderOutline(host) {
  const outline = meta.outline || [];
  if (!outline.length) {
    host.innerHTML = '<div class="rdr-empty">This PDF has no outline.</div>';
    return;
  }
  host.innerHTML = `<div class="rdr-outline">${outline.map((o) => `
    <button class="rdr-outline-item" data-page="${o.page}" style="padding-left:${8 + (o.level - 1) * 12}px">
      ${esc(o.title)}
    </button>`).join('')}</div>`;
  host.querySelectorAll('.rdr-outline-item').forEach((b) =>
    b.addEventListener('click', () => scrollToPage(parseInt(b.dataset.page, 10))));
  renderOutlineActive();
}

function renderHighlights(host) {
  const hls = (meta.highlights || []).slice().sort((a, b) => a.page - b.page || a.created - b.created);
  if (!hls.length) {
    host.innerHTML = '<div class="rdr-empty">No highlights yet.<br><span>Select text in the document to add one.</span></div>';
    return;
  }
  host.innerHTML = `<div class="rdr-hl-list">${hls.map((h) => `
    <div class="rdr-hl-item" data-page="${h.page}" data-hl="${h.id}">
      <div class="rdr-hl-top">
        <span class="rdr-hl-swatch" style="background:${esc(h.color)}"></span>
        <span class="rdr-hl-page">Page ${h.page}</span>
        <button class="rdr-hl-del" data-del="${h.id}" title="Delete"><i data-lucide="trash-2" class="icon-lucide"></i></button>
      </div>
      <div class="rdr-hl-text">${esc(h.text || '(no text)')}</div>
      ${h.note ? `<div class="rdr-hl-note">${esc(h.note)}</div>` : ''}
    </div>`).join('')}</div>`;

  host.querySelectorAll('.rdr-hl-item').forEach((el) =>
    el.addEventListener('click', (e) => {
      if (e.target.closest('[data-del]')) return;
      scrollToPage(parseInt(el.dataset.page, 10));
    }));
  host.querySelectorAll('[data-del]').forEach((b) =>
    b.addEventListener('click', async (e) => {
      e.stopPropagation();
      const id = b.dataset.del;
      try {
        await api(`/api/study/doc/${meta.id}/highlights/${id}`, { method: 'DELETE' });
        meta.highlights = (meta.highlights || []).filter((h) => h.id !== id);
        renderHighlights(host);
        layoutHighlights();
        if (window.lucide) window.lucide.createIcons();
      } catch (err) {
        toast('Delete failed: ' + err.message, 'error');
      }
    }));
  if (window.lucide) window.lucide.createIcons();
}

function renderInfo(host) {
  const p = meta.progress || {};
  const added = meta.added ? new Date(meta.added * 1000).toLocaleString() : '—';
  host.innerHTML = `
    <div class="rdr-info">
      <div class="rdr-info-title">${esc(meta.title || '')}</div>
      <div class="rdr-info-grid">
        <span>Pages</span><b>${meta.pages || 0}</b>
        <span>Size</span><b>${fmtBytes(meta.size)}</b>
        <span>Added</span><b>${esc(added)}</b>
        <span>Progress</span><b>${p.percent || 0}%</b>
        <span>Highlights</span><b>${(meta.highlights || []).length}</b>
        <span>Bookmarked</span><b>${meta.bookmarked ? 'Yes' : 'No'}</b>
      </div>
      <div class="rdr-info-file">${esc(meta.filename || '')}</div>
    </div>`;
}

// ── Topic panel — siblings of the open document + scoped search ──────────────

/** Resolve a doc's topic name, defaulting to the catch-all bucket. */
function docTopic(d) {
  const t = ((((d || {}).group || {}).topic) || '').trim();
  return t || 'Others';
}

/** Fetch every document sharing this doc's topic (self included). */
async function loadTopicSiblings() {
  if (!meta || isRemote) { topicDocs = []; topicName = ''; return; }
  const gen = topicGen;
  const want = docTopic(meta);
  try {
    const docs = await api('/api/study/docs');
    if (gen !== topicGen) return;
    topicName = want;
    topicDocs = docs.filter((d) => docTopic(d) === want);
  } catch {
    if (gen !== topicGen) return;
    topicName = want;
    topicDocs = [];
  }
  if (activeSideTab === 'topic') renderTopicPanel(overlay.querySelector('#rdrSidePanel'));
}

function renderTopicPanel(host) {
  if (isRemote) {
    host.innerHTML = '<div class="rdr-empty">Drive document.<br><span>Import it to file under a topic.</span></div>';
    return;
  }
  if (!topicDocs) {
    host.innerHTML = '<div class="rdr-empty">Loading topic\u2026</div>';
    return;
  }
  host.innerHTML = `
    <div class="rdr-topic">
      <div class="rdr-topic-head">
        <i data-lucide="layers" class="icon-lucide"></i>
        <span class="rdr-topic-name" title="${esc(topicName || 'Others')}">${esc(topicName || 'Others')}</span>
        <span class="rdr-topic-count">${(topicDocs || []).length}</span>
      </div>
      <div class="rdr-topic-search">
        <i data-lucide="search" class="icon-lucide"></i>
        <input id="rdrTopicSearch" type="search" placeholder="Search this topic\u2026"
               value="${esc(topicQuery)}" autocomplete="off" spellcheck="false">
      </div>
      <div class="rdr-topic-list" id="rdrTopicList">${topicListHtml()}</div>
      <button class="rdr-topic-all" data-rdr-topic="search-all">
        <i data-lucide="search" class="icon-lucide"></i> Search every document
      </button>
    </div>`;
  if (window.lucide) window.lucide.createIcons();
}

/** Repaint ONLY the list region so the search box keeps focus while typing. */
function repaintTopicList() {
  if (activeSideTab !== 'topic') return;
  const listEl = overlay && overlay.querySelector('#rdrTopicList');
  if (!listEl) return;
  listEl.innerHTML = topicListHtml();
  if (window.lucide) window.lucide.createIcons();
}

function topicListHtml() {
  if (topicQuery.trim()) return topicSearchHtml(topicQuery.trim());
  const selfId = (meta && meta.id) || '';
  const items = topicDocs || [];
  if (items.length <= 1 && items.every((d) => d.id === selfId)) {
    return '<div class="rdr-empty">This topic holds no other documents yet.</div>';
  }
  return items.map((d) => topicDocItem(d, d.id === selfId)).join('');
}

/** One browsable document row inside a topic. */
function topicDocItem(d, isSelf) {
  const prog = Math.round(((d.progress || {}).percent) || 0);
  return `
    <button class="rdr-topic-item${isSelf ? ' active' : ''}" data-rdr-doc="${esc(d.id)}"
            title="${esc(d.title || 'Untitled')}"${isSelf ? ' aria-current="true"' : ''}>
      <span class="rdr-topic-item-title">${esc(d.title || 'Untitled')}</span>
      <span class="rdr-topic-item-meta">
        <span>${d.pages || 0}p</span>
        ${prog ? `<span class="rdr-topic-item-prog">${prog}%</span>` : ''}
        ${isSelf ? '<span class="rdr-topic-item-you">open</span>' : ''}
      </span>
    </button>`;
}

/** Snippets arrive with <mark> tags from the FTS highlighter; escape the rest. */
function topicSnippet(html) {
  return String(html || '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/&lt;mark&gt;/g, '<mark>').replace(/&lt;\/mark&gt;/g, '</mark>');
}

/**
 * Search results, ordered by proximity: the open PDF first, then the rest of
 * its topic, then everything else in the library.
 */
function topicSearchHtml(q) {
  if (topicSearchBusy && !topicResults) return '<div class="rdr-empty">Searching\u2026</div>';
  const res = topicResults;
  if (!res) return '';
  if (res.error) {
    return `<div class="rdr-empty">Search failed.<br><span>${esc(res.error)}</span></div>`;
  }
  const hits = res.hits || [];
  if (!hits.length) return `<div class="rdr-empty">No matches for \u201c${esc(q)}\u201d.</div>`;

  const selfId = (meta && meta.id) || '';
  const topicIds = new Set((topicDocs || []).map((d) => d.id));
  const titles = new Map();
  for (const d of (topicDocs || [])) titles.set(d.id, d.title || 'Untitled');
  for (const d of (res.docs || [])) if (!titles.has(d.doc_id)) titles.set(d.doc_id, d.title || 'Untitled');

  const selfHits = [], topicHits = [], otherHits = [];
  for (const h of hits) {
    if (h.doc_id === selfId) selfHits.push(h);
    else if (topicIds.has(h.doc_id)) topicHits.push(h);
    else otherHits.push(h);
  }

  const section = (label, arr) => {
    const groups = groupHits(arr);
    if (!groups.length) return '';
    return `<div class="rdr-topic-sec">${esc(label)}</div>` + groups.map((g) => `
      <div class="rdr-ts-group">
        <button class="rdr-ts-doc" data-rdr-doc="${esc(g.doc_id)}">
          <i data-lucide="file-text" class="icon-lucide"></i>
          <span class="rdr-ts-doc-title">${esc(titles.get(g.doc_id) || 'Untitled')}</span>
          <span class="rdr-ts-doc-count">${g.hits.length}</span>
        </button>
        ${g.hits.map((h) => `
          <button class="rdr-ts-hit" data-rdr-doc="${esc(h.doc_id)}" data-rdr-page="${h.page}">
            <span class="rdr-ts-hit-page">p${h.page}</span>
            <span class="rdr-ts-hit-text">${topicSnippet(h.snippet)}</span>
          </button>`).join('')}
      </div>`).join('');
  };

  return section('This document', selfHits)
    + section('In this topic', topicHits)
    + section('Everywhere else', otherHits);
}

function groupHits(hits) {
  const byDoc = new Map();
  for (const h of hits) {
    if (!byDoc.has(h.doc_id)) byDoc.set(h.doc_id, { doc_id: h.doc_id, hits: [] });
    byDoc.get(h.doc_id).hits.push(h);
  }
  return [...byDoc.values()];
}

async function runTopicSearch() {
  const q = topicQuery.trim();
  if (!q) { topicResults = null; topicSearchBusy = false; repaintTopicList(); return; }
  const gen = topicGen;
  topicSearchBusy = true;
  repaintTopicList();
  try {
    const res = await api(`/api/study/search?q=${encodeURIComponent(q)}&limit=80`);
    if (gen !== topicGen || q !== topicQuery.trim()) return;
    topicResults = res;
  } catch (err) {
    if (gen !== topicGen || q !== topicQuery.trim()) return;
    topicResults = { error: err.message, hits: [], docs: [], total: 0 };
  } finally {
    if (gen === topicGen) { topicSearchBusy = false; repaintTopicList(); }
  }
}

function onTopicPanelInput(e) {
  if (!e.target || e.target.id !== 'rdrTopicSearch') return;
  topicQuery = e.target.value;
  if (topicDebounce) clearTimeout(topicDebounce);
  if (!topicQuery.trim()) { topicResults = null; topicSearchBusy = false; repaintTopicList(); return; }
  topicDebounce = setTimeout(runTopicSearch, 220);
}

function onTopicPanelClick(e) {
  const docEl = e.target.closest('[data-rdr-doc]');
  if (docEl) {
    const id = docEl.dataset.rdrDoc;
    const page = parseInt(docEl.dataset.rdrPage, 10);
    if (!id) return;
    if (id === ((meta && meta.id) || '')) {
      if (Number.isFinite(page)) scrollToPage(page);
      return;
    }
    open(id, Number.isFinite(page) ? page : 1);
    return;
  }
  const actEl = e.target.closest('[data-rdr-topic]');
  if (actEl && actEl.dataset.rdrTopic === 'search-all') {
    const q = topicQuery.trim();
    const id = (meta && meta.id) || '';
    const title = (meta && meta.title) || 'Document';
    close();
    if (window.searchPanel && window.searchPanel.searchIn) {
      window.searchPanel.searchIn(id, title);
      if (q && window.searchPanel.setQuery) window.searchPanel.setQuery(q);
    }
  }
}

// ── Selection & highlighting ─────────────────────────────────────────────────

/**
 * The word directly under a viewport point, via the caret API.
 *
 * A pdf.js text layer cannot be trusted for selection text: its spans are
 * absolutely positioned and neighbouring runs overlap, so the browser's own
 * selection happily returns a whole line when you clicked one word. The
 * caret under the pointer is the only exact answer, so the dictionary reads
 * from here instead of from sel.toString().
 *
 * Returns { word, range } or null.
 */
function wordAtPoint(x, y, pageEl) {
  let node = null;
  let off = 0;
  if (document.caretPositionFromPoint) {
    const p = document.caretPositionFromPoint(x, y);
    if (p && p.offsetNode) { node = p.offsetNode; off = p.offset; }
  } else if (document.caretRangeFromPoint) {
    const r = document.caretRangeFromPoint(x, y);
    if (r) { node = r.startContainer; off = r.startOffset; }
  }
  if (!node) return null;
  // The caret API sometimes hands back the span element rather than the text
  // node sitting at that offset — descend to the child that owns the caret.
  if (node.nodeType === Node.ELEMENT_NODE) {
    const kids = node.childNodes;
    const child = kids[Math.min(off, kids.length - 1)];
    if (child && child.nodeType === Node.TEXT_NODE) { node = child; off = 0; }
    else return null;
  }
  if (node.nodeType !== Node.TEXT_NODE || !pageEl.contains(node)) return null;

  const data = node.data;
  const WORD = /[A-Za-z0-9'\u2019\-]/;
  off = Math.max(0, Math.min(off, data.length));
  let s = off;
  let e = off;
  // Clamped to THIS node on both sides, so the walk cannot cross into the
  // next word even when the layer carries no space character between them.
  while (s > 0 && WORD.test(data[s - 1])) s--;
  while (e < data.length && WORD.test(data[e])) e++;
  // A hyphen on either edge is punctuation: the word is "Breadth-First",
  // not "-First". Trim it, then insist a real letter survives.
  while (s < e && !/[A-Za-z0-9'\u2019]/.test(data[s])) s++;
  while (e > s && !/[A-Za-z0-9'\u2019]/.test(data[e - 1])) e--;
  if (e <= s || !/[A-Za-z]/.test(data.slice(s, e))) return null;

  const range = document.createRange();
  try { range.setStart(node, s); range.setEnd(node, e); }
  catch { return null; }
  return { word: data.slice(s, e), range };
}

function onSelection(e) {
  if (!overlay || overlay.classList.contains('hidden')) return;
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed || !sel.rangeCount) return hideHlBar();

  let range = sel.getRangeAt(0);
  let text = sel.toString().trim();
  if (!text) return hideHlBar();

  let node = range.startContainer;
  if (node.nodeType === 3) node = node.parentNode;
  const pageEl = node.closest && node.closest('.rdr-page');
  if (!pageEl) return hideHlBar();

  // Exact word under the pointer — survives the overlapping-span problem the
  // native selection text suffers from. Kept separately from `text` so a
  // deliberate phrase selection still reaches the AI actions intact.
  const px = (e && Number.isFinite(e.clientX)) ? e.clientX : (lastPointer && lastPointer.x);
  const py = (e && Number.isFinite(e.clientY)) ? e.clientY : (lastPointer && lastPointer.y);
  const hit = Number.isFinite(px) ? wordAtPoint(px, py, pageEl) : null;
  const exactWord = hit ? hit.word : '';

  // No whole-word snap. It used to expand the selection by walking the
  // FLATTENED text-layer string, which is unsafe: pdf.js separates words by
  // geometry, not by space characters, so the flat text can read
  // "Breadth-FirstSearch". The walk crossed from "Search" straight into its
  // neighbour and the dictionary looked up "Breadth-First".
  //
  // `text` now stays exactly what the user selected (so AI actions still get
  // a real phrase). The single-word lookup is resolved separately from the
  // caret by wordAtPoint() below, which can never reach a neighbour.

  const pageRect = pageEl.getBoundingClientRect();
  const rects = Array.from(range.getClientRects())
    .filter((r) => r.width > 0 && r.height > 0)
    .map((r) => ({
      x: (r.left - pageRect.left) / pageRect.width,
      y: (r.top - pageRect.top) / pageRect.height,
      w: r.width / pageRect.width,
      h: r.height / pageRect.height,
    }))
    // Drop rects that fall outside this page (multi-page selections).
    .filter((r) => r.y > -0.02 && r.y < 1.02 && r.w > 0 && r.h > 0);

  if (!rects.length) return hideHlBar();

  pendingSelection = {
    page: parseInt(pageEl.dataset.page, 10),
    rects,
    text: text.slice(0, 2000),
    // The single word under the pointer, when we have one. The dictionary
    // prefers this over `text` so it never looks up a neighbour of the word
    // you actually clicked.
    word: exactWord,
    anchor: rects[rects.length - 1],
    pageRect,
  };
  showHlBar(pendingSelection);
}

function showHlBar(sel) {
  const bar = overlay.querySelector('#rdrHlBar');
  bar.classList.remove('hidden');
  // Icons first — swapping <i> for SVGs changes the bar's real size.
  if (window.lucide) window.lucide.createIcons();
  const a = sel.anchor;
  const x = sel.pageRect.left + (a.x + a.w / 2) * sel.pageRect.width;
  const y = sel.pageRect.top + a.y * sel.pageRect.height;
  const bottom = sel.pageRect.top + (a.y + a.h) * sel.pageRect.height;
  // Measure the bar instead of guessing: with the AI buttons it's much wider
  // than the old 220px constant and used to run off the window's right edge.
  const bw = bar.offsetWidth || 220;
  const bh = bar.offsetHeight || 40;
  bar.style.left = `${Math.max(8, Math.min(window.innerWidth - bw - 8, x - bw / 2))}px`;
  // Sit above the selection; flip below it when there's no headroom.
  let top = y - bh - 10;
  if (top < 8) top = bottom + 10;
  bar.style.top = `${Math.max(8, Math.min(top, window.innerHeight - bh - 8))}px`;
}

function hideHlBar() {
  if (!overlay) return;
  const bar = overlay.querySelector('#rdrHlBar');
  if (bar) bar.classList.add('hidden');
  pendingSelection = null;
}

// ── Document right-click menu ────────────────────────────────────────────────

let ctxPage = 0;        // page the menu was opened on
let ctxSelection = '';  // text selected when the menu opened (may be empty)

function hideCtxMenu() {
  if (!overlay) return;
  const menu = overlay.querySelector('#rdrCtxMenu');
  if (menu) menu.classList.add('hidden');
}

function onDocContextMenu(e) {
  if (!meta) return;
  const pageEl = e.target.closest && e.target.closest('.rdr-page');
  // Only act inside a rendered page — the sidebar and margins keep the
  // browser's own menu so text selection there behaves normally.
  if (!pageEl) return;
  e.preventDefault();
  // Stop Sable's global context menu (appearance.js) from also firing on
  // document — otherwise it draws its own menu over this one.
  e.stopPropagation();

  ctxPage = parseInt(pageEl.dataset.page, 10) || currentPage;
  const sel = window.getSelection();
  ctxSelection = sel && !sel.isCollapsed ? sel.toString().trim() : '';

  const menu = overlay.querySelector('#rdrCtxMenu');
  // Selection-dependent items hide themselves when nothing is selected, so
  // the menu always shows only actions that can actually run.
  const selOnly = ['copy', 'copy-selection-image', 'explain-selection', 'highlight', 'save-note'];
  for (const a of selOnly) {
    const el = menu.querySelector('[data-rdr-ctx="' + a + '"]');
    if (el) el.style.display = ctxSelection ? '' : 'none';
  }

  menu.classList.remove('hidden');
  if (window.lucide) window.lucide.createIcons({ nodes: menu.querySelectorAll('[data-lucide]') });

  // Measure after unhiding so the menu never lands off-screen.
  const mw = menu.offsetWidth || 210;
  const mh = menu.offsetHeight || 180;
  const x = Math.max(8, Math.min(e.clientX, window.innerWidth - mw - 8));
  const y = Math.max(8, Math.min(e.clientY, window.innerHeight - mh - 8));
  menu.style.left = `${x}px`;
  menu.style.top = `${y}px`;
}

async function onCtxMenuClick(e) {
  const btn = e.target.closest('[data-rdr-ctx]');
  if (!btn) return;
  const action = btn.dataset.rdrCtx;
  const page = ctxPage || currentPage;
  const selection = ctxSelection;
  hideCtxMenu();

  try {
    if (action === 'copy') {
      await navigator.clipboard.writeText(selection);
      toast('Copied selection', 'success');
      return;
    }

    if (action === 'copy-page-text') {
      const text = await pageToText(page);
      if (!text) return toast('No text on this page', 'error');
      await navigator.clipboard.writeText(text);
      toast(`Page ${page} text copied`, 'success');
      return;
    }

    if (action === 'copy-page') {
      const dataUrl = await pageToImage(page);
      if (!dataUrl) return toast('Could not render this page', 'error');
      // Clipboard images need a PNG blob; fall back to a download when the
      // async clipboard API is unavailable (older browsers / no permission).
      try {
        const blob = await (await fetch(dataUrl)).blob();
        await navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })]);
        toast(`Page ${page} copied as image`, 'success');
      } catch (_) {
        const a = document.createElement('a');
        a.href = dataUrl;
        a.download = `${(meta && (meta.slug || meta.title)) || 'page'}-p${page}.png`;
        document.body.appendChild(a);
        a.click();
        a.remove();
        toast('Clipboard blocked — image downloaded instead', 'error');
      }
      return;
    }

    if (action === 'ask-page' || action === 'ask-pdf' || action === 'explain-page'
        || action === 'summarize-page' || action === 'explain-selection') {
      await runContextAi(action, page, selection);
      return;
    }

    if (action === 'highlight') {
      if (!selection) return toast('Select some text first', 'error');
      const rects = await textToRects(page, selection);
      if (!rects.length) return toast('Could not locate that text', 'error');
      await api(`/api/study/doc/${meta.id}/highlights`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ page, rects, color: '#ffd400', text: selection, note: '' }),
      });
      const fresh = await api(`/api/study/doc/${meta.id}`).catch(() => null);
      if (fresh) meta.highlights = fresh.highlights || [];
      layoutHighlights();
      toast('Highlighted', 'success');
      return;
    }

    if (action === 'save-note') {
      if (!selection) return toast('Select some text first', 'error');
      await saveSelectionNote(page, selection);
      return;
    }
  } catch (err) {
    toast('Action failed: ' + err.message, 'error');
  }
}

/**
 * Run one of the AI context actions from the right-click menu.
 *
 * Stages the page (image for vision models, extracted text otherwise) into the
 * Study AI composer. "ask about this PDF" additionally stages the whole
 * document as text. explain/summarize pre-fill and send; ask just focuses.
 */
async function runContextAi(action, page, selection) {
  // Open the floating Study AI popup and STAGE the context — never hijack the
  // reader into the full chat pane and never auto-send. The user composes the
  // question and hits send themselves; the old synthetic-Enter send was
  // fragile and fired a message the user never asked for.
  const float = ensureAiFloat();
  if (!float) return toast('Study AI unavailable', 'error');
  float.open();
  const view = float.getView && float.getView();
  if (!view) return toast('Study AI unavailable', 'error');

  // Grounding (page / selection / whole doc) is resolved server-side from the
  // study_context we send with the turn, so the surface only needs to record
  // WHICH action and scope this composer will run. Nothing is sent until the
  // user presses enter — no view hijack, no synthetic Enter.
  const scope = action === 'explain-selection' ? 'selection'
    : action === 'ask-pdf' ? 'doc'
    : 'page';
  const actionName = action === 'ask-page' || action === 'ask-pdf' ? 'ask'
    : action === 'explain-selection' ? 'explain'
    : action === 'explain-page' ? 'explain'
    : action === 'summarize-page' ? 'summarize'
    : 'ask';

  if (view.setTurn) {
    view.setTurn({ action: actionName, scope, page, selection: selection || '' });
  }

  // Materialise the attachment so the model actually receives it: a rendered
  // page image for vision models (uploaded), the whole-document text for
  // "ask about this PDF", and nothing extra for a selection (the selection
  // rides in study_context). The surface decides image vs text from caps.
  try {
    if (action === 'ask-pdf') {
      if (view.stageDoc) view.stageDoc();
    } else if (action !== 'explain-selection') {
      if (view.stagePage) await view.stagePage(page);
    }
  } catch { /* staging is best-effort; scope still grounds the turn */ }

  // Pre-fill a sensible prompt for the one-shot actions; leave "ask" blank so
  // the user types their own question. Nothing is sent until they press enter.
  const preset = {
    'explain-page': 'Explain this page in clear, simple terms.',
    'summarize-page': 'Summarize this page into key points.',
    'explain-selection': 'Explain the selected text.',
    'ask-pdf': '',
    'ask-page': '',
  }[action];

  if (view.input) {
    if (preset) view.input.value = preset;
    view.input.focus();
    // Caret to end so a typed follow-up appends rather than overwrites.
    try { view.input.setSelectionRange(view.input.value.length, view.input.value.length); } catch { /* noop */ }
  }

  const msg = action === 'ask-pdf' ? 'Whole document attached — ask away'
    : action === 'ask-page' ? `Page ${page} attached — ask away`
    : 'Ready — press enter to send';
  toast(msg, 'success');
}

/** Save the current selection as a new note anchored to this page. */
async function saveSelectionNote(page, selection) {
  try {
    await api('/api/study/notes', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        title: (selection.slice(0, 60) || `Page ${page} note`),
        content: selection,
        refs: [{ doc_id: meta.id, title: meta.title, kind: 'selection', page, text: selection }],
      }),
    });
    window.dispatchEvent(new CustomEvent('study-notes-changed'));
    toast('Saved as note', 'success');
  } catch (err) {
    toast('Could not save note: ' + err.message, 'error');
  }
}

/**
 * Rasterise just the selected region of a page into a PNG data URL.
 * Uses the same rects as a highlight, so the cropped image matches the text
 * the user picked. Returns null when the selection can't be located.
 */
async function selectionToImage(page, text) {
  try {
    const rects = await textToRects(page, text);
    if (!rects.length) return null;
    const p = await getPage(page);
    const vp = p.getViewport({ scale: 1.6 });
    const canvas = document.createElement('canvas');
    canvas.width = Math.floor(vp.width);
    canvas.height = Math.floor(vp.height);
    const ctx2d = canvas.getContext('2d', { alpha: false });
    ctx2d.fillStyle = '#fff';
    ctx2d.fillRect(0, 0, canvas.width, canvas.height);
    await p.render({ canvasContext: ctx2d, viewport: vp }).promise;

    // Union of the normalized rects → a pixel crop with a small margin.
    const pad = 4;
    let minX = 1, minY = 1, maxX = 0, maxY = 0;
    for (const r of rects) {
      minX = Math.min(minX, r.x);
      minY = Math.min(minY, r.y);
      maxX = Math.max(maxX, r.x + r.w);
      maxY = Math.max(maxY, r.y + r.h);
    }
    const sx = Math.max(0, minX * canvas.width - pad);
    const sy = Math.max(0, minY * canvas.height - pad);
    const sw = Math.min(canvas.width - sx, (maxX - minX) * canvas.width + pad * 2);
    const sh = Math.min(canvas.height - sy, (maxY - minY) * canvas.height + pad * 2);
    if (sw < 2 || sh < 2) return null;

    const crop = document.createElement('canvas');
    crop.width = Math.floor(sw);
    crop.height = Math.floor(sh);
    crop.getContext('2d').drawImage(canvas, sx, sy, sw, sh, 0, 0, crop.width, crop.height);
    return crop.toDataURL('image/png');
  } catch (_) {
    return null;
  }
}

/**
 * Turn a text selection into normalised page rects without a live DOM range.
 * Re-renders the page's text layer and matches by substring, so a highlight
 * requested from the menu lands exactly like a drag-selected one.
 */
async function textToRects(page, text) {
  try {
    const p = await getPage(page);
    const vp = p.getViewport({ scale });
    const tc = await p.getTextContent();
    const needle = text.trim();
    if (!needle) return [];
    // Build a char-offset index over the page's text items, then find the
    // needle and map the covered items to their transform rects.
    const items = tc.items || [];

    // Anchor on the first and last WORD and take every item between them.
    // The old test was `needle.includes(str) || str.includes(needle.split('\n')[0])`
    // — for any multi-line selection that collapses to "does this item contain
    // the first line", so it matched unrelated items all over the page and
    // painted highlight rects in the wrong places. This is a heuristic, not a
    // char-offset mapping (getTextContent gives no stable per-char positions),
    // but it is bounded to the actual selection span.
    const words = needle.split(/\s+/).filter(Boolean);
    if (!words.length) return [];
    const firstWord = words[0];
    const lastWord = words[words.length - 1];

    const has = (it, w) => (it.str || '').includes(w);
    const lo = items.findIndex((it) => has(it, firstWord));
    if (lo < 0) return [];
    let hi = lo;
    for (let i = items.length - 1; i >= lo; i--) {
      if (has(items[i], lastWord)) { hi = i; break; }
    }

    const rects = [];
    for (let i = lo; i <= hi && rects.length < 60; i++) {
      const it = items[i];
      if (!(it.str || '').trim()) continue;
      const tx = pdfjsLib.Util.transform(vp.transform, it.transform);
      const h = Math.hypot(tx[2], tx[3]);
      const w = it.width * scale;
      rects.push({
        x: tx[4] / vp.width,
        y: (tx[5] - h) / vp.height,
        w: w / vp.width,
        h: (h * 1.15) / vp.height,
      });
    }
    return rects.filter((r) => r.w > 0 && r.h > 0 && r.y > -0.02 && r.y < 1.02);
  } catch (_) {
    return [];
  }
}

async function onHighlightBarClick(e) {
  if (e.target.closest('#rdrHlCancel')) {
    hideHlBar();
    window.getSelection()?.removeAllRanges();
    return;
  }

  // Dictionary — a pure, model-free lookup. Deliberately checked BEFORE the
  // AI branch so it can never fall through into runAi().
  const dictBtn = e.target.closest('[data-rdr-dict]');
  if (dictBtn) {
    if (!pendingSelection) return;
    const sel = pendingSelection;
    hideHlBar();
    // Selection is deliberately NOT collapsed here. Clearing it on every
    // action is what made the reader feel like it "disables all selection" —
    // you pick a word, the card opens, and your highlight vanishes.
    //
    // `word` is the caret-exact word under the pointer. `text` is the native
    // selection, which a pdf.js text layer routinely inflates to a whole
    // line — so the word wins whenever we have it.
    openDictCard(sel.word || sel.text);
    return;
  }

  // AI action buttons (Explain / Meaning / Summarize / Define / Ask…)
  const aiBtn = e.target.closest('[data-rdr-ai]');
  if (aiBtn) {
    if (!pendingSelection) return;
    const action = aiBtn.dataset.rdrAi;
    const sel = pendingSelection;
    hideHlBar();
    // Keep the selection alive — same reason as the dictionary branch above.
    runAi({
      action,
      scope: 'selection',
      page: sel.page,
      selection: sel.text,
      rects: sel.rects,
    });
    return;
  }

  const btn = e.target.closest('[data-rdr-color]');
  if (!btn || !pendingSelection || !meta) return;

  const isNote = btn.dataset.rdrColor === 'note';
  let note = '';
  if (isNote) {
    // Sticky-note button → open the quick-note popup with this selection
    // pre-anchored. hideHlBar() clears pendingSelection, so stash a copy of
    // the anchor first and let the popup read from noteAnchor instead.
    noteAnchor = pendingSelection;
    hideHlBar();
    toggleNotePopup();
    return;
  }

  const payload = {
    page: pendingSelection.page,
    rects: pendingSelection.rects,
    color: isNote ? '#79c0ff' : btn.dataset.rdrColor,
    text: pendingSelection.text,
    note,
  };

  try {
    const saved = await api(`/api/study/doc/${meta.id}/highlights`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    meta.highlights = meta.highlights || [];
    meta.highlights.push(saved);
    layoutHighlights();
    if (activeSideTab === 'notes') renderSidePanel();
    toast('Highlight saved', 'success');
    // Mirror into the open room so everyone sees it live. No-op when the user
    // is not in a room, so this costs one truthiness check otherwise.
    pushToRoom('highlight', {
      hl_id: saved.id,
      doc_id: meta.id,
      doc_title: meta.title,
      page: saved.page,
      text: saved.text || '',
      color: saved.color || '',
    });
  } catch (err) {
    toast('Highlight failed: ' + err.message, 'error');
  }
  hideHlBar();
  // No removeAllRanges() here either — the selection stays put so you can see
  // what you just highlighted, and chain another action off it. It used to be
  // wiped here, which is why the reader felt like it ate your selection.
}

function layoutHighlights() {
  if (!meta || !pageEls.length) return;
  const byPage = {};
  for (const h of meta.highlights || []) {
    (byPage[h.page] = byPage[h.page] || []).push(h);
  }
  pageEls.forEach((rec) => {
    const layer = rec.el.querySelector('.rdr-hl-layer');
    if (!layer) return;
    layer.innerHTML = '';
    for (const h of byPage[rec.n] || []) {
      for (const r of h.rects || []) {
        const d = document.createElement('div');
        d.className = 'rdr-hl';
        d.style.left = `${r.x * 100}%`;
        d.style.top = `${r.y * 100}%`;
        d.style.width = `${r.w * 100}%`;
        d.style.height = `${r.h * 100}%`;
        d.style.background = h.color;
        d.title = h.note ? `${h.text}\n\nNote: ${h.note}` : h.text;
        layer.appendChild(d);
      }
    }
  });
}

// ── AI: on-page answer pins ──────────────────────────────────────────────────
// Saved answers don't clutter the document — each one leaves a small sparkle
// marker on the page. Press it to reopen the answer; press elsewhere to ignore.
function layoutAiNotes() {
  if (!meta || !pageEls.length) return;
  const byPage = {};
  for (const n of aiNotes) {
    if (n.page) (byPage[n.page] = byPage[n.page] || []).push(n);
  }
  pageEls.forEach((rec) => {
    const layer = rec.el.querySelector('.rdr-ai-layer');
    if (!layer) return;
    layer.innerHTML = '';
    // Pins live in the page's right margin so they never sit on the text they
    // belong to. The page clips its overflow, so placement is computed in px
    // against the real page box and clamped inside it — a pin can't be cut
    // off at any edge, at any zoom. Multiple pins stack down the margin.
    const notes = (byPage[rec.n] || []).slice().sort((a, b) =>
      ((a.rects && a.rects[0] ? a.rects[0].y : 0.03) - (b.rects && b.rects[0] ? b.rects[0].y : 0.03)));
    const PW = rec.el.clientWidth || 1;
    const PH = rec.el.clientHeight || 1;
    const PIN = 20, PAD = 4, STEP = 23;
    const x = Math.max(PAD, PW - PIN - PAD);
    let prevY = -Infinity;
    notes.forEach((note) => {
      const anchor = (note.rects && note.rects[0]) || { y: 0.03 };
      // Centre on the anchor's line, then push clear of the previous pin.
      let y = anchor.y * PH - PIN / 2;
      y = Math.max(PAD, Math.min(y, PH - PIN - PAD));
      if (y < prevY + STEP) y = Math.min(prevY + STEP, PH - PIN - PAD);
      prevY = y;
      const pin = document.createElement('button');
      pin.className = 'rdr-ai-pin';
      pin.style.left = `${x}px`;
      pin.style.top = `${y}px`;
      pin.title = `${AI_ACTION_LABELS[note.action] || 'AI'} — press to view`;
      pin.innerHTML = '<i data-lucide="sparkles" class="icon-lucide"></i>';
      pin.addEventListener('click', (e) => { e.stopPropagation(); viewSavedNote(note.id); });
      layer.appendChild(pin);
    });
  });
  if (window.lucide) window.lucide.createIcons({ nodes: overlay.querySelectorAll('.rdr-ai-layer [data-lucide]') });
}

// ── AI: config ───────────────────────────────────────────────────────────────

async function loadAiConfig(force = false) {
  if (aiConfigLoaded && !force) return;
  try {
    const [cfg, modelsRes, personasRes] = await Promise.all([
      api('/api/study/ai/config'),
      api('/api/study/ai/models'),
      api('/api/study/ai/personas'),
    ]);
    aiConfig = cfg;
    aiModels = modelsRes.models || [];
    aiPersonas = personasRes.personas || [];
    aiConfigLoaded = true;
    fillAiConfigForm();
  } catch (err) {
    console.warn('[study-reader] ai config load failed', err);
  }
}

function fillAiConfigForm() {
  const modelSel = overlay.querySelector('#rdrAiModel');
  const personaSel = overlay.querySelector('#rdrAiPersona');
  const custom = overlay.querySelector('#rdrAiCustom');
  if (!modelSel || !aiConfig) return;

  modelSel.innerHTML = aiModels.map((m) =>
    `<option value="${esc(m.id)}"${m.id === aiConfig.model ? ' selected' : ''}>${esc(m.label)}</option>`).join('');
  fillThinkingOptions();

  personaSel.innerHTML = '<option value="">Default</option>' +
    aiPersonas.map((p) => `<option value="${esc(p)}"${p === aiConfig.persona ? ' selected' : ''}>${esc(p)}</option>`).join('');

  custom.value = aiConfig.custom_prompt || '';
}

function fillThinkingOptions() {
  const thinkSel = overlay.querySelector('#rdrAiThinking');
  const m = aiModels.find((x) => x.id === aiConfig.model) || aiModels[0];
  const modes = (m && m.thinking_modes) || [];
  thinkSel.innerHTML = modes.map((t) =>
    `<option value="${esc(t.id)}"${t.id === aiConfig.thinking_mode ? ' selected' : ''}>${esc(t.label)}</option>`).join('');
  // Fall back to the model's first mode when the saved one isn't supported.
  if (modes.length && !modes.some((t) => t.id === aiConfig.thinking_mode)) {
    aiConfig.thinking_mode = modes[0].id;
    thinkSel.value = modes[0].id;
  }
}

async function persistAiConfig() {
  if (!aiConfig) return;
  aiConfig.model = overlay.querySelector('#rdrAiModel').value;
  aiConfig.thinking_mode = overlay.querySelector('#rdrAiThinking').value;
  aiConfig.persona = overlay.querySelector('#rdrAiPersona').value;
  aiConfig.custom_prompt = overlay.querySelector('#rdrAiCustom').value;
  try {
    await api('/api/study/ai/config', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(aiConfig),
    });
    // Push the new config into the shared Study AI module so the pane/popup
    // send the chosen model and drop stale image/document capabilities.
    if (window.studyAI && window.studyAI.setConfig) window.studyAI.setConfig(aiConfig);
  } catch (err) { toast('Could not save AI config: ' + err.message, 'error'); }
}

function onAiModelChange() {
  if (!aiConfig) return;
  aiConfig.model = overlay.querySelector('#rdrAiModel').value;
  aiConfig.thinking_mode = '';
  fillThinkingOptions();
  persistAiConfig();
}

function toggleAiConfig() {
  const pop = overlay.querySelector('#rdrAiConfig');
  const opening = pop.classList.contains('hidden');
  pop.classList.toggle('hidden', !opening);
  if (opening) {
    loadAiConfig().then(fillAiConfigForm);
    if (window.lucide) window.lucide.createIcons();
  }
}

// ── AI: running an answer ────────────────────────────────────────────────────
// The card is a COMPACT VIEW of the document's shared Study AI thread — the
// same store the Chat pane and floating popup use — so a question asked here
// shows up there too, and vice-versa. Rendering, streaming and the SSE loop
// all live in study-ai.js; this file only drives the card chrome.

let aiUnsub = null;   // detaches the card's subscription to the shared thread
let aiBusyUnsub = null; // detaches the card's busy-state mirror (Send ⇄ Stop)

/** Context object study-ai.js needs from the reader. */
function aiCtx() {
  return {
    getDocId: () => (meta && meta.id) || '',
    getTitle: () => (meta && meta.title) || 'Document',
    getCurrentPage: () => currentPage,
    // Live selection so adapter-built turns ground on the selected text.
    getSelection: () => (pendingSelection && pendingSelection.text) || (noteAnchor && noteAnchor.text) || '',
    api: (path, opts) => api(path, opts),
    toast: (m, t) => toast(m, t),
  };
}

/** Show the card bound to the shared thread, replacing any prior binding. */
function showAiCard(title, model) {
  const card = overlay.querySelector('#rdrAiCard');
  card.classList.remove('hidden');
  overlay.querySelector('#rdrAiCardTitle').textContent = title || 'Study AI';
  overlay.querySelector('#rdrAiCardModel').textContent = model || (aiConfig?.model || '');
  overlay.querySelector('#rdrAiFollowup').value = '';
  if (aiUnsub) { try { aiUnsub(); } catch { /* noop */ } aiUnsub = null; }
  const body = overlay.querySelector('#rdrAiCardBody');
  const docId = (meta && meta.id) || '';
  if (window.studyAI) {
    window.studyAI.renderThread(body, docId);
    // Re-render on every store change; patch the last bubble during a stream.
    const sub = () => window.studyAI.renderThread(body, docId);
    sub.updateStream = (t) => window.studyAI.updateStream(body, t);
    aiUnsub = window.studyAI.subscribe(docId, sub);
    // Mirror the shared busy state onto this card's composer so a turn started
    // anywhere (card, popup, pane) shows Stop here too.
    if (window.studyAI.onBusy) {
      if (aiBusyUnsub) { try { aiBusyUnsub(); } catch { /* noop */ } }
      const send = overlay.querySelector('#rdrAiFollowupSend');
      const stop = overlay.querySelector('#rdrAiStop');
      aiBusyUnsub = window.studyAI.onBusy(docId, (busy) => {
        if (send) send.style.display = busy ? 'none' : '';
        if (stop) stop.style.display = busy ? '' : 'none';
      });
    }
  }
  if (window.lucide) window.lucide.createIcons();
}

// Async because the ask action opens a dialog. Callers are event handlers that
// ignore the return value, so awaiting here is safe.
async function runAi({ action, scope, page = 0, selection = '', rects = null, question = '' }) {
  if (!meta || !window.studyAI) return;
  if (action === 'ask' && !question) {
    question = (await sableDialog.prompt({
      title: 'Ask about this document',
      label: 'Your question',
      placeholder: 'e.g. explain the proof on page 12',
      okText: 'Ask',
      width: 480,
    })) || '';
    if (!question.trim()) return;
  }

  // Metadata for saving this thread as a note; the turns live in the shared
  // store, not here.
  aiCurrent = {
    action, scope, page, selection, rects, question,
    model: aiConfig?.model || '',
  };
  showAiCard(AI_ACTION_LABELS[action] || 'AI', aiCurrent.model);
  window.studyAI.askAction(aiCtx(), {
    action: action,
    label: AI_ACTION_LABELS[action] || action,
    question: question,
    scope: scope,
    page: page,
    selection: selection,
  });
}

function closeAiCard() {
  if (window.studyAI && meta) window.studyAI.stopStream(meta.id);
  if (aiUnsub) { try { aiUnsub(); } catch { /* noop */ } aiUnsub = null; }
  if (aiBusyUnsub) { try { aiBusyUnsub(); } catch { /* noop */ } aiBusyUnsub = null; }
  overlay.querySelector('#rdrAiCard').classList.add('hidden');
  aiCurrent = null;
}

// ── Dictionary (pure lookup — no model, no tokens) ───────────────────────────
// Independent of the Study AI thread store: it renders exactly what
// /api/study/dictionary returns, with no model in the loop.

let dictReq = 0;   // monotonic token so a slow reply can't overwrite a newer one

function openDictCard(rawText) {
  const card = overlay.querySelector('#rdrDictCard');
  const body = overlay.querySelector('#rdrDictBody');
  if (!card || !body) return;
  // Multi-word selections: look up the first token, not the whole phrase.
  const word = (String(rawText || '').match(/[A-Za-z][A-Za-z'-]*/) || [''])[0];
  overlay.querySelector('#rdrDictWord').textContent = word || '(no word)';
  overlay.querySelector('#rdrDictSrc').textContent = '';
  card.classList.remove('hidden');
  if (window.lucide) window.lucide.createIcons();

  if (!word) {
    body.innerHTML = '<div class="rdr-dict-state">Select a word to look up.</div>';
    return;
  }
  body.innerHTML = '<div class="rdr-dict-state">Looking up…</div>';
  const req = ++dictReq;
  api(`/api/study/dictionary?word=${encodeURIComponent(word)}`)
    .then((data) => { if (req === dictReq) renderDictCard(body, data); })
    .catch((err) => {
      if (req !== dictReq) return;
      body.innerHTML = `<div class="rdr-dict-state err">Lookup failed: ${esc(err.message)}</div>`;
    });
}

function renderDictCard(body, data) {
  const parts = [];

  if (data.phonetic) parts.push(`<div class="rdr-dict-phon">${esc(data.phonetic)}</div>`);

  if (data.bangla && data.bangla.length) {
    parts.push(`
      <div class="rdr-dict-bn-block">
        <div class="rdr-dict-bn-label">বাংলা অর্থ</div>
        <div class="rdr-dict-bn">${data.bangla.map(esc).join(' · ')}</div>
      </div>`);
  } else if ((data.entries || []).length) {
    // English resolved but Wiktionary had no Bangla table for this word.
    parts.push('<div class="rdr-dict-bn-missing">No Bangla entry in Wiktionary for this word.</div>');
  }

  (data.entries || []).forEach((entry) => {
    const defs = (entry.definitions || []).map((d) => `
      <li>
        <span class="rdr-dict-def">${esc(d.text)}</span>
        ${d.example ? `<span class="rdr-dict-eg">“${esc(d.example)}”</span>` : ''}
      </li>`).join('');
    parts.push(`
      <div class="rdr-dict-entry">
        ${entry.pos ? `<div class="rdr-dict-pos">${esc(entry.pos)}</div>` : ''}
        <ol class="rdr-dict-defs">${defs}</ol>
      </div>`);
  });

  if (!data.found) {
    parts.push('<div class="rdr-dict-state">No dictionary entry found for this word.</div>');
  }

  body.innerHTML = parts.join('');
  const src = overlay.querySelector('#rdrDictSrc');
  if (src) src.textContent = data.source || ((data.bangla && data.bangla.length) ? 'en.wiktionary.org' : '');
  if (window.lucide) window.lucide.createIcons();
}

function closeDictCard() {
  dictReq += 1;   // invalidate any in-flight lookup
  const card = overlay.querySelector('#rdrDictCard');
  if (card) card.classList.add('hidden');
}

/** Start a fresh thread in the card, keeping the current document anchors. */
function newAiChat() {
  if (!meta || !window.studyAI) return;
  window.studyAI.stopStream(meta.id);
  window.studyAI.resetThread(meta.id);
  overlay.querySelector('#rdrAiFollowup').value = '';
  aiCurrent = null;
}

function sendFollowup() {
  const input = overlay.querySelector('#rdrAiFollowup');
  const q = input.value.trim();
  if (!q || !meta || !window.studyAI) return;
  input.value = '';
  if (!aiCurrent) {
    aiCurrent = { action: 'ask', scope: 'doc', page: currentPage, selection: '', rects: null, question: q, model: aiConfig?.model || '' };
  } else {
    aiCurrent.question = q;
    aiCurrent.action = 'ask';
  }
  overlay.querySelector('#rdrAiCardTitle').textContent = 'Ask';
  window.studyAI.askFollowup(aiCtx(), q, {
    action: 'ask', scope: aiCurrent.scope, page: aiCurrent.page, selection: aiCurrent.selection,
  });
}

// ── AI: saving / viewing notes ───────────────────────────────────────────────

async function saveAiNote() {
  if (!meta) return;
  // The turns live in the shared thread now, not on aiCurrent.
  const turns = (window.studyAI && window.studyAI.getThread(meta.id)) || [];
  if (!turns.some((t) => t.role === 'assistant' && t.content)) {
    return toast('Nothing to save yet', 'error');
  }
  const lastAnswer = [...turns].reverse().find((t) => t.role === 'assistant');
  const anchor = aiCurrent || { action: 'ask', scope: 'doc', page: currentPage, selection: '', rects: null, question: '', model: aiConfig?.model || '' };
  try {
    const saved = await api(`/api/study/doc/${meta.id}/notes`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        page: anchor.page,
        rects: anchor.rects || [],
        text: anchor.selection,
        action: anchor.action,
        question: anchor.question,
        // Keep the legacy single-answer fields AND the full thread.
        answer: (lastAnswer && lastAnswer.content) || '',
        messages: turns.map((t) => ({ role: t.role, content: t.content })),
        model: anchor.model,
        scope: anchor.scope,
      }),
    });
    aiNotes.push(saved);
    meta.ai_notes = aiNotes;
    layoutAiNotes();
    toast('Thread saved', 'success');
    if (activeSideTab === 'ai') renderSidePanel();
  } catch (err) { toast('Save failed: ' + err.message, 'error'); }
}

function viewSavedNote(id) {
  const note = aiNotes.find((n) => n.id === id);
  if (!note) return;
  // Prefer the stored full thread; fall back to a single Q/A turn.
  let turns = Array.isArray(note.messages) && note.messages.length ? note.messages.slice() : [];
  if (!turns.length) {
    if (note.question || note.text) turns.push({ role: 'user', content: note.question || note.text });
    if (note.answer) turns.push({ role: 'assistant', content: note.answer });
  }
  aiCurrent = {
    action: note.action, scope: note.scope, page: note.page,
    selection: note.text, rects: note.rects, question: note.question,
    model: note.model,
  };
  // Load the saved thread into the shared store, then show it in the card.
  if (window.studyAI && meta) window.studyAI.setThread(meta.id, turns);
  showAiCard(AI_ACTION_LABELS[note.action] || note.action, note.model || '');
}

// ── Doc / Chat toggle + Study AI pane ────────────────────────────────────────

let readerTab = 'doc';        // 'doc' | 'chat' | 'notes'
let aiPane = null;            // { el, view } from studyAI.createPane
let aiFloat = null;           // { open, close } from studyAI.mountFloating
let notePopup = null;         // quick-note capture popup element
let noteAnchor = null;        // selection stashed by the highlight bar's note button

function setReaderTab(tab) {
  readerTab = ['chat', 'notes'].includes(tab) ? tab : 'doc';
  const isDoc = readerTab === 'doc';
  const isChat = readerTab === 'chat';
  const isNotes = readerTab === 'notes';

  const main = overlay.querySelector('#rdrMain');
  const chatPane = overlay.querySelector('#rdrChatPane');
  const notesPane = overlay.querySelector('#rdrNotesPane');
  if (main) main.classList.toggle('hidden', !isDoc);
  if (chatPane) chatPane.classList.toggle('hidden', !isChat);
  if (notesPane) notesPane.classList.toggle('hidden', !isNotes);

  const tabs = { doc: '#rdrTabDoc', chat: '#rdrTabChat', notes: '#rdrTabNotes' };
  for (const [key, sel] of Object.entries(tabs)) {
    const el = overlay.querySelector(sel);
    if (!el) continue;
    const on = readerTab === key;
    el.classList.toggle('active', on);
    el.setAttribute('aria-selected', String(on));
  }

  // The floating launcher is redundant while the full chat pane is showing.
  // It is also hidden in the Notes tab, which has its own AI menu on the
  // editor — two AI entry points at once would just be confusing.
  const launch = overlay.querySelector('#rdrAiLaunch');
  if (launch) launch.style.display = (isChat || isNotes) ? 'none' : '';
  // The quick-note launcher rides beside Study AI — hide it in the same tabs.
  const quickNote = overlay.querySelector('#rdrQuickNote');
  if (quickNote) quickNote.style.display = (isChat || isNotes) ? 'none' : '';
  if (window.lucide) window.lucide.createIcons();

  if (isChat) {
    const p = ensureAiPane();
    if (p) p.view.render();
  } else if (isNotes) {
    renderNotesPane();
  }
}

/** Host element the notes editor mounts into when opened from the reader. */
function notesPaneHost() {
  return overlay.querySelector('#rdrNotesPane');
}

/** Draw the reader's Notes pane: notes connected to this document first. */
function renderNotesPane() {
  const host = notesPaneHost();
  if (!host || !window.studyNotes) return;
  window.studyNotes.renderReaderPane(host, {
    getDocId: () => (meta && meta.id) || '',
    getTitle: () => (meta && meta.title) || 'Document',
    getPaneHost: notesPaneHost,
  });
}

/** Popup lifecycle for the quick-note capture (header button). */
function toggleNotePopup() {
  if (notePopup) { closeNotePopup(); return; }
  if (!window.studyNotes) return;
  const wrap = document.createElement('div');
  wrap.className = 'rdr-note-pop';
  overlay.appendChild(wrap);
  notePopup = wrap;
  window.studyNotes.renderQuickCapture(wrap, {
    getDocId: () => (meta && meta.id) || '',
    getTitle: () => (meta && meta.title) || 'Document',
    getCurrentPage: () => currentPage || 1,
    // The highlight bar stashes its selection in noteAnchor before hiding
    // (hideHlBar clears pendingSelection), so prefer it when present.
    getPendingSelection: () => noteAnchor || pendingSelection,
    getPaneHost: notesPaneHost,
    closePopup: closeNotePopup,
  });
}

function closeNotePopup() {
  if (notePopup) { notePopup.remove(); notePopup = null; }
  noteAnchor = null;
}

/** Render one page to a PNG data URL (vision models get the real image). */
async function pageToImage(page) {
  try {
    const p = await getPage(page);
    const vp = p.getViewport({ scale: 1.6 });
    const canvas = document.createElement('canvas');
    canvas.width = Math.floor(vp.width);
    canvas.height = Math.floor(vp.height);
    const ctx2d = canvas.getContext('2d', { alpha: false });
    ctx2d.fillStyle = '#fff';
    ctx2d.fillRect(0, 0, canvas.width, canvas.height);
    await p.render({ canvasContext: ctx2d, viewport: vp }).promise;
    return canvas.toDataURL('image/png');
  } catch { return null; }
}

/** Extracted text for one page (fallback for non-vision models). */
async function pageToText(page) {
  try {
    const p = await getPage(page);
    const tc = await p.getTextContent();
    return (tc.items || []).map((it) => it.str || '').join(' ').trim();
  } catch { return ''; }
}

/** Whole PDF as base64 (document-capable models). */
async function pdfToBase64() {
  try {
    const src = isRemote
      ? `/api/drive/file/${encodeURIComponent(meta.id)}/pdf?title=${encodeURIComponent(meta.title || '')}&save=false`
      : `/api/study/doc/${meta.id}/pdf`;
    const res = await fetch(src);
    if (!res.ok) return null;
    const buf = await res.arrayBuffer();
    let binary = '';
    const bytes = new Uint8Array(buf);
    const CHUNK = 0x8000;
    for (let i = 0; i < bytes.length; i += CHUNK) {
      binary += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
    }
    return { data: btoa(binary), name: (meta.filename || meta.title || 'document') + '.pdf' };
  } catch { return null; }
}

/** Whole-document extracted text (fallback for non-document models). */
async function pdfToText() {
  try {
    if (isRemote) {
      // Remote docs have no library text store; concatenate page text.
      const parts = [];
      for (let i = 1; i <= pageEls.length; i += 1) parts.push(await pageToText(i));
      return parts.join('\n\n').slice(0, 60000);
    }
    const r = await api(`/api/study/doc/${meta.id}/text`);
    return ((r && r.page_text) || []).join('\n\n').slice(0, 60000);
  } catch { return ''; }
}

/**
 * Claim the library record the Drive stream just created for this file.
 *
 * Remote opens download through /api/drive/file/<id>/pdf, which auto-ingests the
 * bytes as they stream. Once that lands the file exists in the library under a
 * uuid — this swaps meta.id over to it and drops remote mode, so Study AI,
 * notes, bookmarks and progress all start working mid-session.
 *
 * Returns true when a record was adopted.
 */
async function adoptRemoteDoc() {
  if (!isRemote || !meta || !meta.id) return false;
  try {
    // Not under /api/study/doc/, so the remote-swallowing api() lets it through.
    const r = await api(`/api/drive/file/${encodeURIComponent(meta.id)}/doc`);
    if (!r || !r.doc_id) return false;
    const pages = meta.pages;
    meta.id = r.doc_id;
    isRemote = false;
    lastRemoteSaved = true;
    // Pull the real record so highlights/outline/progress are not the synthetic
    // empties this function's caller stubbed in. Must run AFTER the flip, or
    // api() would swallow the read as an unwritable remote call.
    try {
      const fresh = await api(`/api/study/doc/${meta.id}`);
      if (fresh) meta = Object.assign({}, fresh, { pages: pages || fresh.pages });
    } catch { /* keep the synthetic record; the id is what matters */ }
    return true;
  } catch { return false; }
}

/** Build the ctx object study-ai.js needs to read the open document. */
function aiContext(host) {
  return {
    host,
    getDocId: () => (meta && meta.id) || '',
    getTitle: () => (meta && meta.title) || 'Document',
    isRemote: () => isRemote,
    getCurrentPage: () => currentPage,
    api: (path, opts) => api(path, opts),
    toast,
    getPageImage: pageToImage,
    getPageText: pageToText,
    getPdfBase64: pdfToBase64,
    getPdfText: pdfToText,
    // Live text selection, so pane/popup requests ground on the selection
    // instead of always falling back to the whole document.
    getSelection: () => (pendingSelection && pendingSelection.text) || (noteAnchor && noteAnchor.text) || '',
  };
}

function ensureAiPane() {
  if (aiPane || !window.studyAI || !meta) return aiPane;
  const host = overlay.querySelector('#rdrChatPane');
  if (!host) return null;
  aiPane = window.studyAI.createPane(aiContext(host));
  host.appendChild(aiPane.el);
  return aiPane;
}

function ensureAiFloat() {
  if (aiFloat || !window.studyAI || !meta) return aiFloat;
  const host = overlay.querySelector('#rdrAiLaunch');
  if (!host) return null;
  aiFloat = window.studyAI.mountFloating(aiContext(host));
  return aiFloat;
}

// ── Public API ───────────────────────────────────────────────────────────────

window.studyReader = {
  open,
  close,
  isOpen,
  getDocMeta: () => (meta ? { id: meta.id, title: meta.title, pages: meta.pages } : null),
  openRemote: (fileId, title) => open(fileId, { remote: true, title }),
};
