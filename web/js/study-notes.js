/*
 * study-notes.js — handwritten markdown notes for Study mode.
 *
 * Three surfaces share one editor:
 *   - the "Notes" tab in Study mode (grid of all notes)
 *   - the "Notes" pane in the document reader (notes connected to that doc)
 *   - the quick-capture popup in the reader header (anchored to a selection)
 *
 * Notes are plain .md files on the server (library/notes/<id>.md) indexed by
 * library/notes/notes.json — see server/api/routes/study_notes.py.
 *
 * Rendering goes through window.renderMarkdown + window.postRenderContent, so
 * tables, callouts, ==highlight==, LaTeX math and Mermaid all work exactly as
 * they do everywhere else in Sable.
 *
 * Exposes window.studyNotes = { openEditor, openPicker, capture, close,
 *                               renderTab, renderSidebar, renderReaderPane,
 *                               renderQuickCapture, countForDoc }.
 */
(function () {
  'use strict';

  const API = '/api/study/notes';

  // ── State ──────────────────────────────────────────────────────────────────
  let NOTES = [];            // list of summaries (from GET /api/study/notes)
  let loaded = false;
  let tabFilter = 'all';     // all | pinned | referenced | untagged
  let tabQuery = '';
  let tabDocFilter = '';     // when the reader asks for one doc's notes

  // Editor state (single instance — only one editor can be open at a time)
  let ed = null;             // { id, title, content, refs, tags, pinned, created,
                             //   dirty, saving, mode, stack:[], aiAbort, host, kind }

  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));

  function notify(msg) {
    if (window.showToast) return window.showToast(msg);
    console.log('[study-notes]', msg);
  }

  // ── API helpers ────────────────────────────────────────────────────────────

  async function apiGet(path) {
    const r = await fetch(path);
    if (!r.ok) throw new Error(r.statusText);
    return r.json();
  }

  async function apiSend(path, method, body) {
    const r = await fetch(path, {
      method,
      headers: body ? { 'content-type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!r.ok) {
      let detail = r.statusText;
      try { detail = (await r.json()).detail || detail; } catch {}
      throw new Error(detail);
    }
    return r.json();
  }

  // ── Loading ────────────────────────────────────────────────────────────────

  async function loadNotes(opts) {
    const params = new URLSearchParams();
    const docId = (opts && opts.docId) || tabDocFilter;
    if (docId) params.set('doc_id', docId);
    const q = (opts && opts.q) != null ? opts.q : tabQuery;
    if (q) params.set('q', q);
    if (tabFilter === 'pinned') params.set('pinned', '1');
    try {
      NOTES = await apiGet(API + (params.toString() ? '?' + params : ''));
    } catch (err) {
      NOTES = [];
      notify('Could not load notes: ' + err.message);
    }
    loaded = true;
    return NOTES;
  }

  function refreshViews() {
    // Never repaint the tab while an editor is mounted in it — autosave fires
    // on every keystroke pause and would otherwise wipe the editor.
    const editingInTab = ed && ed.host && ed.host.parentElement
      && ed.host.parentElement.id === 'studyContent';
    if (!editingInTab && window.studyMode && window.studyMode.getTab() === 'notes') {
      window.studyMode.refreshTab();
    }
    // The reader's Notes pane listens for this and redraws if it's showing.
    window.dispatchEvent(new CustomEvent('study-notes-changed'));
  }

  function filtered() {
    return NOTES.filter((n) => {
      if (tabFilter === 'pinned' && !n.pinned) return false;
      if (tabFilter === 'referenced' && !(n.refs || []).length) return false;
      if (tabFilter === 'untagged' && (n.refs || []).length) return false;
      if (tabQuery) {
        const q = tabQuery.toLowerCase();
        if (!(n.title || '').toLowerCase().includes(q)
            && !(n.snippet || '').toLowerCase().includes(q)) return false;
      }
      return true;
    });
  }

  // ── Shared bits of markup ──────────────────────────────────────────────────

  function refChips(refs, opts) {
    const list = refs || [];
    if (!list.length) return '';
    const max = (opts && opts.max) || 3;
    const shown = list.slice(0, max);
    const more = list.length - shown.length;
    return '<span class="sn-ref-chips">' + shown.map((r) => {
      const label = r.title || 'Document';
      const anchor = r.kind === 'selection' && r.page ? ` · p${r.page}`
        : (r.page ? ` · p${r.page}` : '');
      return `<span class="sn-ref-chip" title="${esc(label + anchor)}">`
        + '<i data-lucide="file-text" class="icon-lucide"></i>'
        + esc(label) + esc(anchor) + '</span>';
    }).join('') + (more > 0 ? `<span class="sn-ref-more">+${more}</span>` : '') + '</span>';
  }

  /** The server labels a title-less note "Untitled note" — don't let that
   *  placeholder shout like a real heading. */
  function isUntitled(title) {
    return !title || title === 'Untitled note';
  }

  function relTime(ts) {
    const t = Number(ts) || 0;
    if (!t) return '';
    const d = Date.now() / 1000 - t;
    if (d < 60) return 'just now';
    if (d < 3600) return Math.floor(d / 60) + 'm ago';
    if (d < 86400) return Math.floor(d / 3600) + 'h ago';
    if (d < 604800) return Math.floor(d / 86400) + 'd ago';
    return new Date(t * 1000).toLocaleDateString();
  }

  // ── Study-mode tab: content pane ───────────────────────────────────────────

  function renderTab() {
    const items = filtered();

    if (!loaded) {
      return `<div class="sn-tab"><div class="study-empty"><div class="study-empty-icon">📓</div><h3>Loading…</h3></div></div>`;
    }
    if (!items.length) {
      const empty = tabQuery
        ? ['🔍', 'Nothing matches', 'No note fits this search.']
        : ['📓', 'No notes yet', 'Write your first note, or capture one from a document while reading.'];
      return `<div class="sn-tab">
        <div class="study-empty">
          <div class="study-empty-icon">${empty[0]}</div>
          <h3>${empty[1]}</h3>
          <p>${empty[2]}</p>
          ${tabQuery ? '' : '<button class="study-empty-btn" data-sn="new">Write a note</button>'}
        </div></div>`;
    }

    // The card leads with its CONTENT, not its title — the title is a quiet
    // label on the footer line. A title-less note reads as an em-dash.
    const cards = items.map((n) => `
      <article class="sn-card${n.pinned ? ' pinned' : ''}" data-sn-open="${esc(n.id)}" role="button" tabindex="0">
        ${n.pinned ? '<i data-lucide="pin" class="icon-lucide sn-pin"></i>' : ''}
        <p class="sn-card-snippet">${esc((n.snippet || '').slice(0, 220)) || '<em>Empty note</em>'}</p>
        ${refChips(n.refs)}
        <footer class="sn-card-foot">
          <h4 class="sn-card-title${isUntitled(n.title) ? ' is-untitled' : ''}">${esc(isUntitled(n.title) ? 'Untitled note' : n.title)}</h4>
          <span class="sn-card-time">${esc(relTime(n.updated))}</span>
          <button class="sn-card-del" data-sn-del="${esc(n.id)}" title="Delete">
            <i data-lucide="trash-2" class="icon-lucide"></i>
          </button>
        </footer>
      </article>`).join('');

    return `<div class="sn-tab"><div class="sn-grid">${cards}</div></div>`;
  }

  function renderSidebar() {
    const nAll = NOTES.length;
    const nPinned = NOTES.filter((n) => n.pinned).length;
    const nRef = NOTES.filter((n) => (n.refs || []).length).length;
    const nFree = NOTES.filter((n) => !(n.refs || []).length).length;
    const sel = (f) => (tabFilter === f ? ' active' : '');
    return `
      <div class="study-sidebar-search-wrap">
        <i data-lucide="search" class="icon-lucide"></i>
        <input class="study-sidebar-search" placeholder="Search notes..." id="snSidebarSearch" value="${esc(tabQuery)}">
      </div>
      <div class="study-sidebar-section">
        <div class="study-sidebar-label">Filter</div>
        <button class="study-sidebar-item${sel('all')}" data-sn-filter="all"><i data-lucide="notebook" class="icon-lucide"></i> All notes <span class="sidebar-count">${nAll}</span></button>
        <button class="study-sidebar-item${sel('pinned')}" data-sn-filter="pinned"><i data-lucide="pin" class="icon-lucide"></i> Pinned <span class="sidebar-count">${nPinned}</span></button>
        <button class="study-sidebar-item${sel('referenced')}" data-sn-filter="referenced"><i data-lucide="link" class="icon-lucide"></i> Referenced <span class="sidebar-count">${nRef}</span></button>
        <button class="study-sidebar-item${sel('untagged')}" data-sn-filter="untagged"><i data-lucide="file-text" class="icon-lucide"></i> Standalone <span class="sidebar-count">${nFree}</span></button>
      </div>
      <div class="study-sidebar-section">
        <div class="study-sidebar-label">Actions</div>
        <button class="study-sidebar-item" data-sn="new"><i data-lucide="plus-circle" class="icon-lucide"></i> New note</button>
        <button class="study-sidebar-item" data-sn="refresh"><i data-lucide="refresh-cw" class="icon-lucide"></i> Refresh</button>
      </div>`;
  }

  /** Called by study.js when a sidebar button in the notes tab is clicked. */
  function sidebarAction(action) {
    if (action === 'refresh') { loadNotes().then(refreshViews); return true; }
    if (['all', 'pinned', 'referenced', 'untagged'].includes(action)) {
      tabFilter = action;
      window.studyMode.refreshSidebar();
      window.studyMode.refreshTab();
      return true;
    }
    return false;
  }

  function bindSidebarSearch(panel) {
    const input = panel && panel.querySelector('#snSidebarSearch');
    if (!input) return;
    let t;
    input.addEventListener('input', () => {
      clearTimeout(t);
      t = setTimeout(() => {
        tabQuery = input.value;
        window.studyMode.refreshTab();
      }, 150);
    });
  }

  // ── Editor ─────────────────────────────────────────────────────────────────

  const AI_ACTIONS = [
    ['ask', 'Ask about this note', 'message-circle-question'],
    ['enhance', 'Enhance', 'sparkles'],
    ['refactor', 'Refactor', 'wand-2'],
    ['redesign', 'Redesign layout', 'layout-panel-top'],
    ['organize', 'Organize', 'list-tree'],
    ['expand', 'Expand', 'maximize-2'],
    ['summarize', 'Summarize', 'align-left'],
    ['clarify', 'Clarify', 'lightbulb'],
    ['fix-grammar', 'Fix grammar', 'spell-check'],
    ['add-examples', 'Add examples', 'flask-conical'],
    ['flashcards', 'Make flashcards', 'layers'],
    ['questions', 'Generate questions', 'help-circle'],
  ];

  function blankEditor() {
    return {
      id: '', title: '', content: '', refs: [], tags: [], pinned: false,
      created: 0, dirty: false, saving: false, mode: 'edit', stack: [],
      aiAbort: null, aiBusy: false, kind: 'editor', host: null,
    };
  }

  async function openEditor(noteId, opts) {
    close();
    ed = blankEditor();
    ed.kind = (opts && opts.kind) || 'editor';
    if (noteId) {
      try {
        const note = await apiGet(`${API}/${encodeURIComponent(noteId)}`);
        ed.id = note.id;
        ed.title = note.title || '';
        ed.content = note.content || '';
        ed.refs = note.refs || [];
        ed.tags = note.tags || [];
        ed.pinned = !!note.pinned;
        ed.created = note.created || 0;
      } catch (err) {
        notify('Could not open note: ' + err.message);
        ed = null;
        return;
      }
    }
    buildEditor(opts || {});
  }

  function buildEditor(opts) {
    // Seed a brand-new note from the reader (anchor ref + captured text).
    if (opts.seedRef) ed.refs.push(opts.seedRef);
    if (opts.seedContent) ed.content = opts.seedContent;
    if (opts.seedTitle) ed.title = opts.seedTitle;

    const host = document.createElement('div');
    // Mounting into a host element (the reader's Notes pane) means we render
    // in-place instead of as a full-screen modal.
    const embedded = !!(opts.embedded || opts.mount);
    host.className = 'sn-overlay' + (embedded ? ' sn-embedded' : '');
    host.id = 'snEditorOverlay';
    host.innerHTML = editorMarkup();

    // Clear the mount first. Appending on top left the previous view (the card
    // grid / notes list) in the DOM behind the editor, which kept it in flow
    // and made the pane scroll — the editor then only covered part of it.
    if (opts.mount) opts.mount.replaceChildren(host);
    else document.body.appendChild(host);

    ed.host = host;

    // Belt-and-braces: pin the fill geometry inline so no cascade, order or
    // specificity surprise can leave the editor shrink-wrapped mid-pane.
    if (embedded) {
      Object.assign(host.style, {
        position: 'absolute', inset: '0',
        width: '100%', height: '100%',
        display: 'flex', alignItems: 'stretch', justifyContent: 'flex-start',
        padding: '0',
      });
      const edEl = host.querySelector('.sn-editor');
      if (edEl) {
        Object.assign(edEl.style, {
          flex: '1 1 auto',
          width: 'auto', minWidth: '0',
          height: 'auto', minHeight: '0',
          maxWidth: 'none', maxHeight: 'none',
          borderRadius: '0', border: 'none', boxShadow: 'none',
        });
      }
      // Measure-and-pin: a <textarea> refuses to stretch under flex/grid, so
      // after layout we compute the leftover height (editor minus the chrome
      // rows above the body) and set it explicitly. Runs on the next frame so
      // the chrome has been laid out; a resize observer keeps it honest.
      const fitBody = () => {
        const wrap = host.querySelector('.sn-body-wrap');
        const ta = host.querySelector('.sn-body');
        if (!wrap || !edEl) return;
        let used = 0;
        for (const child of edEl.children) {
          if (child === wrap) continue;
          if (child.classList.contains('hidden')) continue;
          used += child.offsetHeight;
        }
        // Measure against the HOST, not the editor: the editor's own height was
        // set to `auto`, so reading it back is circular and always yields the
        // height we were about to set.
        const avail = (host.clientHeight || window.innerHeight) - used;
        const h = Math.max(120, avail);
        wrap.style.flex = '0 0 auto';
        wrap.style.height = h + 'px';
        // A <textarea> has an intrinsic rows-based height and will NOT stretch
        // to fill its parent — the height has to be stated outright.
        if (ta) ta.style.height = h + 'px';
      };
      requestAnimationFrame(fitBody);
      if (window.ResizeObserver) {
        const ro = new ResizeObserver(fitBody);
        ro.observe(edEl);
        host._snRO = ro;
      }
    }
    wireEditor(host);
    wireRefBarDelegation();
    renderAll();
    if (window.lucide) window.lucide.createIcons();
    const ta = host.querySelector('#snBody');
    if (ta) ta.focus();
  }

  /** Repaint every derived region of the editor at once. */
  function renderAll() {
    renderRefBar();
    setMode(ed.mode || 'edit');
    if (ed.mode && ed.mode !== 'edit') renderPreview();
    const pinBtn = ed.host && ed.host.querySelector('[data-sn="pin"]');
    if (pinBtn) pinBtn.classList.toggle('active', !!ed.pinned);
  }

  function editorMarkup() {
    const isNew = !ed.id;
    return `
      <div class="sn-editor" role="dialog" aria-modal="true">
        <div class="sn-bar">
          <button class="sn-bar-btn" data-sn="close" title="Close"><i data-lucide="arrow-left" class="icon-lucide"></i></button>
          <input class="sn-title-input" id="snTitle" placeholder="Untitled note" value="${esc(ed.title)}">
          <span class="sn-save-pill" id="snSavePill"></span>
          <div class="sn-bar-spacer"></div>
          <button class="sn-bar-btn" data-sn="pin" title="Pin"><i data-lucide="pin" class="icon-lucide"></i></button>
          <button class="sn-bar-btn" data-sn="export" title="Export .md"><i data-lucide="download" class="icon-lucide"></i></button>
          <button class="sn-bar-btn sn-danger" data-sn="delete" title="Delete"><i data-lucide="trash-2" class="icon-lucide"></i></button>
          <button class="study-room-action primary sn-save" data-sn="save">${isNew ? 'Create' : 'Save'}</button>
        </div>

        <div class="sn-refbar" id="snRefBar">
          <span class="sn-refbar-label"><i data-lucide="link" class="icon-lucide"></i> References</span>
          <div class="sn-refbar-chips" id="snRefChips"></div>
          <button class="sn-refbar-add" data-sn="add-ref"><i data-lucide="plus" class="icon-lucide"></i> Add reference</button>
        </div>

        <div class="sn-tools">
          <div class="sn-modes">
            <button class="sn-mode active" data-sn-mode="edit">Edit</button>
            <button class="sn-mode" data-sn-mode="preview">Preview</button>
            <button class="sn-mode" data-sn-mode="split">Split</button>
          </div>
          <div class="sn-format" id="snFormat">
            <button data-sn-fmt="h1" title="Heading 1">H1</button>
            <button data-sn-fmt="h2" title="Heading 2">H2</button>
            <span class="sn-fmt-sep"></span>
            <button data-sn-fmt="bold" title="Bold"><b>B</b></button>
            <button data-sn-fmt="italic" title="Italic"><i>I</i></button>
            <button data-sn-fmt="strike" title="Strikethrough"><s>S</s></button>
            <button data-sn-fmt="mark" title="Highlight">==</button>
            <button data-sn-fmt="code" title="Inline code"><code>&lt;/&gt;</code></button>
            <span class="sn-fmt-sep"></span>
            <button data-sn-fmt="ul" title="Bullet list"><i data-lucide="list" class="icon-lucide"></i></button>
            <button data-sn-fmt="task" title="Task list"><i data-lucide="check-square" class="icon-lucide"></i></button>
            <button data-sn-fmt="quote" title="Quote"><i data-lucide="quote" class="icon-lucide"></i></button>
            <button data-sn-fmt="callout" title="Callout"><i data-lucide="megaphone" class="icon-lucide"></i></button>
            <span class="sn-fmt-sep"></span>
            <button data-sn-fmt="link" title="Link"><i data-lucide="link" class="icon-lucide"></i></button>
            <button data-sn-fmt="math" title="Math">∑</button>
            <button data-sn-fmt="mermaid" title="Mermaid diagram"><i data-lucide="git-branch" class="icon-lucide"></i></button>
            <button data-sn-fmt="table" title="Table"><i data-lucide="table" class="icon-lucide"></i></button>
          </div>
          <div class="sn-bar-spacer"></div>
          <div class="sn-ai-wrap">
            <button class="sn-ai-btn" data-sn="ai-menu"><i data-lucide="sparkles" class="icon-lucide"></i> AI</button>
            <div class="sn-ai-menu hidden" id="snAiMenu">
              ${AI_ACTIONS.map(([a, label, icon]) =>
                `<button data-sn-ai="${a}"><i data-lucide="${icon}" class="icon-lucide"></i>${label}</button>`).join('')}
              <div class="sn-ai-menu-sep"></div>
              <label class="sn-ai-ground">
                <input type="checkbox" id="snAiGround"> Ground in referenced documents
              </label>
            </div>
          </div>
        </div>

        <div class="sn-body-wrap" id="snBodyWrap">
          <textarea class="sn-body" id="snBody" placeholder="Start writing… markdown, math, mermaid diagrams and callouts all work.">${esc(ed.content)}</textarea>
          <div class="sn-preview" id="snPreview"></div>
        </div>
      </div>
      <!-- Note AI runs in its own floating popup, detached from the editor
           layout so it never reflows or covers the text being written. -->
      <div class="sn-ai-pop hidden" id="snAiCard">
        <div class="sn-ai-card-head">
          <span class="sn-ai-pop-icon"><i data-lucide="sparkles" class="icon-lucide"></i></span>
          <span id="snAiCardTitle">AI</span>
          <div class="sn-bar-spacer"></div>
          <button class="sn-bar-btn" data-sn="ai-revert" title="Revert"><i data-lucide="undo-2" class="icon-lucide"></i></button>
          <button class="sn-bar-btn" data-sn="ai-apply" title="Replace note"><i data-lucide="check" class="icon-lucide"></i></button>
          <button class="sn-bar-btn" data-sn="ai-insert" title="Insert below"><i data-lucide="plus" class="icon-lucide"></i></button>
          <button class="sn-bar-btn" data-sn="ai-close"><i data-lucide="x" class="icon-lucide"></i></button>
        </div>
        <div class="sn-ai-card-body" id="snAiBody"></div>
        <div class="sn-ai-card-foot" id="snAiFoot">
          <input class="sn-ai-input" id="snAiAsk" placeholder="Ask about this note…">
          <button class="sn-bar-btn" data-sn="ai-ask-send" title="Send"><i data-lucide="send" class="icon-lucide"></i></button>
        </div>
      </div>
      `;
  }

  function wireEditor(host) {
    const q = (s) => host.querySelector(s);

    q('#snTitle').addEventListener('input', (e) => {
      ed.title = e.target.value; markDirty();
    });
    q('#snBody').addEventListener('input', (e) => {
      ed.content = e.target.value; markDirty();
      if (ed.mode !== 'edit') schedulePreview();
    });
    // Tab key inserts two spaces instead of leaving the textarea.
    q('#snBody').addEventListener('keydown', (e) => {
      if (e.key !== 'Tab') return;
      e.preventDefault();
      const ta = e.target;
      const s = ta.selectionStart;
      ta.value = ta.value.slice(0, s) + '  ' + ta.value.slice(ta.selectionEnd);
      ta.selectionStart = ta.selectionEnd = s + 2;
      ed.content = ta.value; markDirty();
    });

    host.addEventListener('click', onEditorClick);
    q('#snTitle').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); q('#snBody').focus(); }
    });
    document.addEventListener('keydown', editorKeydown);
  }

  function editorKeydown(e) {
    if (!ed || !ed.host) return;
    const mod = e.ctrlKey || e.metaKey;
    if (mod && e.key.toLowerCase() === 's') { e.preventDefault(); saveNote(); return; }
    if (e.key === 'Escape') {
      const card = ed.host.querySelector('#snAiCard');
      if (card && !card.classList.contains('hidden')) {
        card.classList.add('hidden');
        return;
      }
      close();
    }
  }

  function onEditorClick(e) {
    const modeEl = e.target.closest('[data-sn-mode]');
    if (modeEl) { setMode(modeEl.dataset.snMode); return; }

    const fmtEl = e.target.closest('[data-sn-fmt]');
    if (fmtEl) { applyFormat(fmtEl.dataset.snFmt); return; }

    const aiEl = e.target.closest('[data-sn-ai]');
    if (aiEl) { runAi(aiEl.dataset.snAi); return; }

    const el = e.target.closest('[data-sn]');
    if (el) { handleEditorAction(el.dataset.sn); return; }

    // Click outside the AI menu closes it.
    const menu = ed.host.querySelector('#snAiMenu');
    if (menu && !menu.classList.contains('hidden')
        && !e.target.closest('.sn-ai-wrap')) {
      menu.classList.add('hidden');
    }
  }

  function handleEditorAction(action) {
    switch (action) {
      case 'close': close(); break;
      case 'save': saveNote(); break;
      case 'pin': ed.pinned = !ed.pinned; markDirty(); renderRefBar(); saveNote(); break;
      case 'delete': deleteNote(); break;
      case 'export': exportNote(); break;
      case 'add-ref': openPicker({ onPick: addRefs }); break;
      case 'ai-menu': toggleAiMenu(); break;
      case 'ai-apply': applyAi('replace'); break;
      case 'ai-insert': applyAi('insert'); break;
      case 'ai-revert': revertAi(); break;
      case 'ai-close': closeAiPopup(); break;
      case 'ai-ask-send': sendAiAsk(); break;
    }
  }

  // ── Dirty / save ───────────────────────────────────────────────────────────

  let saveTimer = null;

  function markDirty() {
    ed.dirty = true;
    setPill('Unsaved', 'dirty');
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => { if (ed && ed.dirty) saveNote(true); }, 1500);
  }

  function setPill(text, cls) {
    if (!ed || !ed.host) return;
    const pill = ed.host.querySelector('#snSavePill');
    if (!pill) return;
    pill.textContent = text || '';
    pill.className = 'sn-save-pill' + (cls ? ' ' + cls : '');
  }

  async function saveNote(silent) {
    if (!ed) return null;
    if (ed.saving) return null;
    clearTimeout(saveTimer);
    ed.saving = true;
    setPill('Saving…', 'busy');
    const payload = {
      title: ed.title, content: ed.content, refs: ed.refs,
      tags: ed.tags, pinned: ed.pinned,
    };
    try {
      let note;
      if (ed.id) {
        note = await apiSend(`${API}/${encodeURIComponent(ed.id)}`, 'PUT', payload);
      } else {
        note = await apiSend(API, 'POST', payload);
        ed.id = note.id;
      }
      ed.dirty = false;
      setPill('Saved ✓', 'ok');
      setTimeout(() => { if (ed && !ed.dirty) setPill('', ''); }, 1600);
      await loadNotes();
      refreshViews();
      return note;
    } catch (err) {
      setPill('Save failed', 'err');
      if (!silent) notify('Save failed: ' + err.message);
      return null;
    } finally {
      ed.saving = false;
    }
  }

  async function deleteNote() {
    if (!ed.id) { close(); return; }
    if (!(await sableDialog.confirm({
      title: 'Delete this note?',
      message: 'This cannot be undone.',
      okText: 'Delete', danger: true,
    }))) return;
    try {
      await apiSend(`${API}/${encodeURIComponent(ed.id)}`, 'DELETE');
      notify('Note deleted');
      close();
      await loadNotes();
      refreshViews();
    } catch (err) {
      notify('Delete failed: ' + err.message);
    }
  }

  function exportNote() {
    if (!ed.id) { notify('Save the note first'); return; }
    const a = document.createElement('a');
    a.href = `${API}/${encodeURIComponent(ed.id)}/export`;
    a.download = (ed.title || 'note') + '.md';
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  // ── Rendering modes ────────────────────────────────────────────────────────

  let previewTimer = null;

  function setMode(mode) {
    ed.mode = ['edit', 'preview', 'split'].includes(mode) ? mode : 'edit';
    const wrap = ed.host.querySelector('#snBodyWrap');
    wrap.className = 'sn-body-wrap mode-' + ed.mode;
    ed.host.querySelectorAll('.sn-mode').forEach((b) =>
      b.classList.toggle('active', b.dataset.snMode === ed.mode));
    // The formatting bar is meaningless in pure preview mode.
    const fmt = ed.host.querySelector('#snFormat');
    if (fmt) fmt.style.display = ed.mode === 'preview' ? 'none' : '';
    if (ed.mode !== 'edit') renderPreview();
  }

  function schedulePreview() {
    clearTimeout(previewTimer);
    previewTimer = setTimeout(renderPreview, 300);
  }

  function renderPreview() {
    if (!ed || !ed.host) return;
    const box = ed.host.querySelector('#snPreview');
    if (!box) return;
    const src = ed.content || '';
    if (!src.trim()) {
      box.innerHTML = '<p class="sn-preview-empty">Nothing to preview yet.</p>';
      return;
    }
    try {
      box.innerHTML = window.renderMarkdown ? window.renderMarkdown(src) : esc(src);
    } catch (err) {
      box.innerHTML = '<p class="sn-preview-empty">Render error: ' + esc(err.message) + '</p>';
      return;
    }
    if (window.postRenderContent) window.postRenderContent(box);
    if (window.lucide) window.lucide.createIcons();
  }

  // ── Markdown formatting helpers ────────────────────────────────────────────

  function applyFormat(kind) {
    const ta = ed.host.querySelector('#snBody');
    if (!ta) return;
    const s = ta.selectionStart;
    const e = ta.selectionEnd;
    const val = ta.value;
    const sel = val.slice(s, e);
    let out = null;
    let caret = null;

    const wrap = (before, after, placeholder) => {
      const body = sel || placeholder || '';
      out = val.slice(0, s) + before + body + after + val.slice(e);
      caret = sel ? s + before.length + body.length + after.length : s + before.length;
    };
    const linePrefix = (prefix, placeholder) => {
      // Expand the selection to whole lines, then prefix each.
      const lineStart = val.lastIndexOf('\n', s - 1) + 1;
      const lineEnd = val.indexOf('\n', e) === -1 ? val.length : val.indexOf('\n', e);
      const block = val.slice(lineStart, lineEnd) || placeholder || '';
      const next = block.split('\n').map((l) => prefix + l).join('\n');
      out = val.slice(0, lineStart) + next + val.slice(lineEnd);
      caret = lineStart + next.length;
    };

    switch (kind) {
      case 'h1': linePrefix('# '); break;
      case 'h2': linePrefix('## '); break;
      case 'bold': wrap('**', '**', 'bold text'); break;
      case 'italic': wrap('*', '*', 'italic text'); break;
      case 'strike': wrap('~~', '~~', 'struck text'); break;
      case 'mark': wrap('==', '==', 'highlighted'); break;
      case 'code': wrap('`', '`', 'code'); break;
      case 'ul': linePrefix('- '); break;
      case 'task': linePrefix('- [ ] '); break;
      case 'quote': linePrefix('> '); break;
      case 'callout':
        out = val.slice(0, s) + '> [!note] Title\n> ' + (sel || 'Content') + val.slice(e);
        caret = s + 20;
        break;
      case 'link': wrap('[', '](https://)', 'link text'); break;
      case 'math': wrap('$', '$', 'x^2'); break;
      case 'mermaid':
        out = val.slice(0, s) + '```mermaid\ngraph LR\n  A --> B\n```' + val.slice(e);
        caret = s + 12;
        break;
      case 'table':
        out = val.slice(0, s) + '| Column | Column |\n| --- | --- |\n| Cell | Cell |' + val.slice(e);
        caret = s + 1;
        break;
      default: return;
    }
    if (out == null) return;
    ta.value = out;
    ed.content = out;
    ta.focus();
    if (caret != null) ta.selectionStart = ta.selectionEnd = caret;
    markDirty();
    if (ed.mode !== 'edit') renderPreview();
  }

  // ── Reference bar ──────────────────────────────────────────────────────────

  function renderRefBar() {
    if (!ed || !ed.host) return;
    const box = ed.host.querySelector('#snRefChips');
    if (!box) return;
    if (!ed.refs.length) {
      box.innerHTML = '<span class="sn-ref-empty">No documents connected</span>';
      return;
    }
    box.innerHTML = ed.refs.map((r, i) => {
      const label = r.title || 'Document';
      const anchor = r.page ? ` · page ${r.page}` : '';
      const kind = r.kind === 'selection' ? 'selection' : (r.page ? 'page' : 'document');
      return `<span class="sn-ref-chip sn-ref-${kind}" title="${esc(label + anchor)}">
        <i data-lucide="${r.kind === 'selection' ? 'highlighter' : 'file-text'}" class="icon-lucide"></i>
        <span class="sn-ref-chip-label">${esc(label)}${esc(anchor)}</span>
        ${r.page ? `<button class="sn-ref-jump" data-sn-jump="${i}" title="Open in reader"><i data-lucide="external-link" class="icon-lucide"></i></button>` : ''}
        <button class="sn-ref-x" data-sn-unref="${i}" title="Remove"><i data-lucide="x" class="icon-lucide"></i></button>
      </span>`;
    }).join('');
    if (window.lucide) window.lucide.createIcons();
  }

  function addRefs(refs) {
    for (const r of refs) {
      const dupe = ed.refs.some((x) => x.doc_id === r.doc_id && x.page === r.page);
      if (!dupe) ed.refs.push(r);
    }
    renderRefBar();
    markDirty();
  }

  function wireRefBarDelegation() {
    // Bound once on the host: chip buttons are re-rendered constantly.
    if (!ed || !ed.host) return;
    ed.host.addEventListener('click', (e) => {
      const unref = e.target.closest('[data-sn-unref]');
      if (unref) { ed.refs.splice(Number(unref.dataset.snUnref), 1); renderRefBar(); markDirty(); return; }
      const jump = e.target.closest('[data-sn-jump]');
      if (jump) {
        const ref = ed.refs[Number(jump.dataset.snJump)];
        if (ref && window.studyReader) {
          const opts = ref.page ? { page: ref.page } : undefined;
          window.studyReader.open(ref.doc_id, opts);
        }
      }
    });
  }

  // ── Document picker (for references) ───────────────────────────────────────

  async function openPicker(opts) {
    let docs = [];
    try { docs = await apiGet('/api/study/docs'); } catch { docs = []; }
    const overlay = document.createElement('div');
    overlay.className = 'sn-overlay sn-picker-overlay';
    overlay.innerHTML = `
      <div class="sn-picker" role="dialog" aria-modal="true">
        <div class="sn-picker-head">
          <h3>Connect a document</h3>
          <button class="sn-bar-btn" data-sn-pick-close><i data-lucide="x" class="icon-lucide"></i></button>
        </div>
        <input class="sn-picker-search" placeholder="Search documents…" id="snPickSearch">
        <div class="sn-picker-list" id="snPickList"></div>
        <div class="sn-picker-foot">
          <span class="sn-picker-count" id="snPickCount">0 selected</span>
          <button class="study-room-action primary" data-sn-pick-add>Add selected</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    if (window.lucide) window.lucide.createIcons();

    const selected = new Set();
    const list = overlay.querySelector('#snPickList');
    const count = overlay.querySelector('#snPickCount');

    const draw = (filter) => {
      const q = (filter || '').toLowerCase();
      const items = docs.filter((d) => !q || (d.title || '').toLowerCase().includes(q));
      if (!items.length) {
        list.innerHTML = '<div class="study-acad-empty">No documents found</div>';
        return;
      }
      list.innerHTML = items.map((d) => `
        <label class="sn-pick-row${selected.has(d.id) ? ' sel' : ''}">
          <input type="checkbox" data-sn-pick="${esc(d.id)}"${selected.has(d.id) ? ' checked' : ''}>
          <span class="sn-pick-title">${esc(d.title || d.filename || 'Document')}</span>
          <span class="sn-pick-meta">${d.pages ? d.pages + 'p' : ''}</span>
        </label>`).join('');
    };
    draw('');

    overlay.querySelector('#snPickSearch').addEventListener('input', (e) => draw(e.target.value));
    list.addEventListener('change', (e) => {
      const cb = e.target.closest('[data-sn-pick]');
      if (!cb) return;
      if (cb.checked) selected.add(cb.dataset.snPick); else selected.delete(cb.dataset.snPick);
      cb.closest('.sn-pick-row').classList.toggle('sel', cb.checked);
      count.textContent = selected.size + ' selected';
    });

    const finish = () => {
      const refs = docs.filter((d) => selected.has(d.id))
        .map((d) => ({ doc_id: d.id, title: d.title || d.filename || 'Document', kind: 'doc', page: 0 }));
      overlay.remove();
      if (opts && opts.onPick) opts.onPick(refs);
    };

    overlay.querySelector('[data-sn-pick-close]').onclick = () => overlay.remove();
    overlay.querySelector('[data-sn-pick-add]').onclick = finish;
    overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });
  }

  // ── AI ─────────────────────────────────────────────────────────────────────

  function toggleAiMenu() {
    const menu = ed.host.querySelector('#snAiMenu');
    menu.classList.toggle('hidden');
  }

  /**
   * Show the note-AI popup.
   *
   * It is a fixed panel pinned to the bottom-right (like the document Study AI
   * launcher) so it floats above the editor without reflowing the text or the
   * toolbar. Opening it once remembers its position for the session.
   */
  function openAiPopup(card) {
    card.classList.remove('hidden');
    // Only auto-position on first open; afterwards the user may have moved it.
    if (!card.dataset.positioned) {
      card.dataset.positioned = '1';
    }
  }

  function closeAiPopup() {
    const card = ed && ed.host && ed.host.querySelector('#snAiCard');
    if (card) card.classList.add('hidden');
  }

  async function runAi(action) {
    ed.host.querySelector('#snAiMenu').classList.add('hidden');
    const card = ed.host.querySelector('#snAiCard');
    const title = card.querySelector('#snAiCardTitle');
    const body = card.querySelector('#snAiBody');
    const foot = card.querySelector('#snAiFoot');
    const label = (AI_ACTIONS.find((a) => a[0] === action) || [action, action])[1];

    openAiPopup(card);
    title.textContent = label;
    // A previous tool edit may have hidden apply/insert — restore them for
    // this new action so the buttons reflect the current turn's state.
    card.querySelectorAll('[data-sn="ai-apply"], [data-sn="ai-insert"]')
      .forEach((b) => { b.style.display = ''; });
    body.innerHTML = '<div class="sn-ai-thinking"><i data-lucide="loader-circle" class="icon-lucide"></i> Thinking…</div>';
    if (window.lucide) window.lucide.createIcons();
    foot.hidden = action !== 'ask';
    if (action === 'ask') setTimeout(() => {
      const ask = card.querySelector('#snAiAsk');
      if (ask) ask.focus();
    }, 40);

    const ground = ed.host.querySelector('#snAiGround').checked;
    // Snapshot for revert before any edit lands.
    ed.stack.push({ title: ed.title, content: ed.content });

    const payload = { action, content: ed.content, ground, question: '' };
    if (action === 'ask') payload.question = '';

    await streamAi(payload, body, action);
  }

  async function sendAiAsk() {
    const input = ed.host.querySelector('#snAiAsk');
    const q = input.value.trim();
    if (!q) return;
    const body = ed.host.querySelector('#snAiBody');
    body.innerHTML = '<div class="sn-ai-thinking"><i data-lucide="loader-circle" class="icon-lucide"></i> Thinking…</div>';
    if (window.lucide) window.lucide.createIcons();
    ed.stack.push({ title: ed.title, content: ed.content });
    await streamAi({ action: 'ask', content: ed.content, question: q, ground: false }, body, 'ask');
  }

  /**
   * Re-read the note body after the model edited it with the study_note tool.
   * The model no longer returns the note as chat text — it edits the file, and
   * the editor must pick up the new content so the user sees the live result.
   */
  async function refreshNoteBody() {
    if (!ed || !ed.id) return;
    try {
      const note = await apiGet(`${API}/${encodeURIComponent(ed.id)}`);
      ed.title = note.title || ed.title;
      ed.content = note.content || '';
      const ti = ed.host.querySelector('#snTitle');
      const ta = ed.host.querySelector('#snBody');
      if (ti) ti.value = ed.title;
      if (ta) ta.value = ed.content;
      if (ed.mode !== 'edit') renderPreview();
      // The edit already landed in the file — the "replace/insert" buttons
      // would apply it a second time, so hide them and keep only Revert.
      const card = ed.host.querySelector('#snAiCard');
      if (card) {
        card.querySelectorAll('[data-sn="ai-apply"], [data-sn="ai-insert"]')
          .forEach((b) => { b.style.display = 'none'; });
      }
      notify('Note updated');
    } catch (err) {
      notify('Could not reload note: ' + err.message);
    }
  }

  async function streamAi(payload, bodyEl, action) {
    if (ed.aiAbort) { try { ed.aiAbort.abort(); } catch {} }
    const ctrl = new AbortController();
    ed.aiAbort = ctrl;
    ed.aiBusy = true;
    let acc = '';
    let thinking = '';
    let edited = false;
    const toolsAcc = [];

    const paintTools = () => {
      let html = '';
      for (const t of toolsAcc) {
        const cls = t.status === 'error' ? 'err' : (t.status === 'ok' ? 'ok' : 'run');
        html += '<div class="sn-ai-tool ' + cls + '"><span class="sn-ai-tool-dot"></span>'
          + esc((t.name || 'tool').replace(/_/g, ' '))
          + (t.error ? '<span class="sn-ai-tool-err">' + esc(t.error) + '</span>' : '')
          + '</div>';
      }
      let host = bodyEl.querySelector('.sn-ai-tools');
      if (!host) {
        host = document.createElement('div');
        host.className = 'sn-ai-tools';
        bodyEl.appendChild(host);
      }
      host.innerHTML = html;
    };

    const paint = () => {
      let html = '';
      if (thinking) {
        html += '<details class="sn-ai-think"><summary>Thinking</summary>'
          + '<div class="sn-ai-think-body">' + esc(thinking) + '</div></details>';
      }
      html += '<div class="sn-ai-out">'
        + (window.renderMarkdown ? window.renderMarkdown(acc) : esc(acc))
        + '</div>';
      bodyEl.innerHTML = html;
      paintTools();
      if (window.postRenderContent) window.postRenderContent(bodyEl.querySelector('.sn-ai-out'));
    };

    try {
      const res = await fetch(`${API}/${encodeURIComponent(ed.id || 'new')}/ai`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(payload),
        signal: ctrl.signal,
      });
      if (!res.ok) {
        let detail = res.statusText;
        try { detail = (await res.json()).detail || detail; } catch {}
        throw new Error(detail);
      }

      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = '';
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const parts = buf.split('\n\n');
        buf = parts.pop();
        for (const chunk of parts) {
          const line = chunk.split('\n').find((l) => l.startsWith('data: '));
          if (!line) continue;
          let ev;
          try { ev = JSON.parse(line.slice(6)); } catch { continue; }
          if (ev.type === 'answer') { acc += ev.text || ''; paint(); }
          else if (ev.type === 'thinking') { thinking += ev.text || ''; paint(); }
          else if (ev.type === 'skill_start') { toolsAcc.push({ name: ev.name, status: 'running' }); paintTools(); }
          else if (ev.type === 'skill_end') {
            const t = toolsAcc[toolsAcc.length - 1];
            if (t) { t.status = ev.ok ? 'ok' : 'error'; t.error = ev.error || ''; paintTools(); }
          }
          else if (ev.type === 'error') { throw new Error(ev.message || 'AI error'); }
          else if (ev.type === 'done') {
            acc = ev.text || acc;
            if (ev.edited) edited = true;
            paint();
          }
        }
      }
      ed.aiResult = acc;
      // A tool edit already changed the note on disk — reload it so the editor
      // shows the live result instead of an "apply" prompt for stale text.
      if (edited) await refreshNoteBody();
    } catch (err) {
      if (err.name === 'AbortError') return;
      bodyEl.innerHTML = '<div class="sn-ai-err">' + esc(err.message) + '</div>';
    } finally {
      ed.aiBusy = false;
      ed.aiAbort = null;
    }
  }

  function applyAi(mode) {
    if (!ed.aiResult) { notify('Nothing to apply'); return; }
    if (mode === 'replace') {
      ed.content = ed.aiResult;
    } else {
      ed.content = (ed.content || '').replace(/\s*$/, '') + '\n\n' + ed.aiResult;
    }
    const ta = ed.host.querySelector('#snBody');
    if (ta) ta.value = ed.content;
    ed.host.querySelector('#snAiCard').classList.add('hidden');
    ed.aiResult = null;
    markDirty();
    if (ed.mode !== 'edit') renderPreview();
    notify(mode === 'replace' ? 'Note replaced' : 'Inserted below');
  }

  function revertAi() {
    const snap = ed.stack.pop();
    if (!snap) { notify('Nothing to revert'); return; }
    ed.title = snap.title;
    ed.content = snap.content;
    const ti = ed.host.querySelector('#snTitle');
    const ta = ed.host.querySelector('#snBody');
    if (ti) ti.value = ed.title;
    if (ta) ta.value = ed.content;
    markDirty();
    if (ed.mode !== 'edit') renderPreview();
    notify('Reverted');
  }

  // ── Reader integration ─────────────────────────────────────────────────────

  /** Notes connected to one document (used by the reader's Notes pane). */
  async function notesForDoc(docId) {
    try {
      return await apiGet(API + '?doc_id=' + encodeURIComponent(docId));
    } catch { return []; }
  }

  async function renderReaderPane(host, ctx) {
    const docId = ctx.getDocId();
    const docTitle = ctx.getTitle();

    // Don't blow away an editor the user opened inside this pane.
    if (host.querySelector('#snEditorOverlay')) return;

    host.innerHTML = '<div class="sn-reader-loading">Loading notes…</div>';
    const items = await notesForDoc(docId);

    const list = items.length ? items.map((n) => `
      <article class="sn-reader-note" data-sn-open="${esc(n.id)}" role="button" tabindex="0">
        <header>
          ${n.pinned ? '<i data-lucide="pin" class="icon-lucide sn-pin"></i>' : ''}
          <h4>${esc(n.title)}</h4>
        </header>
        ${n.refs && n.refs.length ? `<div class="sn-reader-note-anchor">${refChips(n.refs, { max: 1 })}</div>` : ''}
        <p>${esc((n.snippet || '').slice(0, 200)) || '<em>Empty note</em>'}</p>
        <span class="sn-card-time">${esc(relTime(n.updated))}</span>
      </article>`).join('')
      : `<div class="study-empty sn-reader-empty">
           <div class="study-empty-icon">📓</div>
           <h3>No notes for this document</h3>
           <p>Take one from a selection, or write from scratch.</p>
         </div>`;

    host.innerHTML = `
      <div class="sn-reader-pane">
        <div class="sn-reader-head">
          <div>
            <h3>Notes</h3>
            <p class="sn-reader-sub">Connected to <strong>${esc(docTitle)}</strong></p>
          </div>
          <button class="study-room-action primary" data-sn="new-for-doc">
            <i data-lucide="plus" class="icon-lucide"></i> New note
          </button>
        </div>
        <div class="sn-reader-list">${list}</div>
      </div>`;
    if (window.lucide) window.lucide.createIcons();

    // Bound once per host — re-binding on every render stacked duplicate
    // listeners (and duplicate editor opens).
    if (!host.dataset.snWired) {
      host.dataset.snWired = '1';
      host.addEventListener('click', (e) => {
        const open = e.target.closest('[data-sn-open]');
        if (open) {
          openEditor(open.dataset.snOpen, { kind: 'reader', mount: ctx.getPaneHost() });
          return;
        }
        const nw = e.target.closest('[data-sn="new-for-doc"]');
        if (nw) {
          const id = (ctx.getDocId() || '');
          openEditor(null, {
            kind: 'reader',
            mount: ctx.getPaneHost(),
            seedRef: { doc_id: id, title: ctx.getTitle(), kind: 'doc', page: 0 },
          });
        }
      });
    }
  }

  /**
   * Quick-capture popup in the reader header. Small, anchored to the current
   * page/selection; saving creates a note with that anchor as its reference.
   */
  function renderQuickCapture(host, ctx) {
    const docId = ctx.getDocId();
    const docTitle = ctx.getTitle();
    const page = ctx.getCurrentPage();
    const sel = ctx.getPendingSelection && ctx.getPendingSelection();
    const hasSel = !!(sel && sel.text);
    const anchorLabel = hasSel
      ? `Selection · ${sel.text.trim().slice(0, 60)}${sel.text.length > 60 ? '…' : ''}`
      : `Page ${page}`;

    host.innerHTML = `
      <div class="sn-cap">
        <div class="sn-cap-head">
          <i data-lucide="sticky-note" class="icon-lucide"></i>
          <span>Quick note</span>
          <div class="sn-bar-spacer"></div>
          <button class="sn-bar-btn" data-sn-cap-close><i data-lucide="x" class="icon-lucide"></i></button>
        </div>
        <div class="sn-cap-anchor">${esc(anchorLabel)}</div>
        <textarea class="sn-cap-body" id="snCapBody" placeholder="Write a note…"></textarea>
        <div class="sn-cap-foot">
          <button class="sn-cap-link" data-sn-cap-open>Open in editor</button>
          <button class="study-room-action primary" data-sn-cap-save>Save note</button>
        </div>
      </div>`;
    if (window.lucide) window.lucide.createIcons();

    const bodyEl = host.querySelector('#snCapBody');
    bodyEl.focus();

    host.querySelector('[data-sn-cap-close]').onclick = () => ctx.closePopup();
    host.querySelector('[data-sn-cap-save]').onclick = async () => {
      const content = bodyEl.value.trim();
      if (!content) { notify('Nothing to save'); return; }
      const payload = {
        doc_id: docId,
        doc_title: docTitle,
        page: hasSel && sel.page ? sel.page : page,
        text: hasSel ? sel.text : '',
        rects: hasSel && sel.rects ? sel.rects : undefined,
        content,
      };
      try {
        await apiSend(API + '/capture', 'POST', payload);
        notify('Note saved');
        bodyEl.value = '';
        ctx.closePopup();
        await loadNotes();
        refreshViews();
      } catch (err) {
        notify('Could not save: ' + err.message);
      }
    };
    host.querySelector('[data-sn-cap-open]').onclick = () => {
      const content = bodyEl.value.trim();
      ctx.closePopup();
      openEditor(null, {
        kind: 'reader', mount: ctx.getPaneHost(),
        seedRef: { doc_id: docId, title: docTitle, kind: hasSel ? 'selection' : 'page', page: hasSel && sel.page ? sel.page : page, text: hasSel ? sel.text : '' },
        seedContent: content,
      });
    };
  }

  // ── Open / close ───────────────────────────────────────────────────────────

  function close() {
    document.removeEventListener('keydown', editorKeydown);
    clearTimeout(saveTimer);
    clearTimeout(previewTimer);
    if (ed && ed.aiAbort) { try { ed.aiAbort.abort(); } catch {} }
    const wasReaderKind = ed && ed.kind === 'reader';
    const mount = ed && ed.host ? ed.host.parentElement : null;
    if (ed && ed.host) {
      if (ed.host._snRO) { try { ed.host._snRO.disconnect(); } catch {} }
      ed.host.remove();
    }
    ed = null;

    if (!mount) return;

    // Closing an editor that lived inside the reader's Notes pane would leave
    // that pane blank — put the connected-notes list back.
    if (mount.id === 'rdrNotesPane' && wasReaderKind && window.studyReader
        && typeof window.studyReader.getDocMeta === 'function') {
      const m = window.studyReader.getDocMeta();
      if (m && m.id) {
        renderReaderPane(mount, {
          getDocId: () => m.id,
          getTitle: () => m.title || 'Document',
          getPaneHost: () => mount,
        });
      }
      return;
    }

    // Closing an editor mounted in the Notes tab restores the card grid.
    if (mount.id === 'studyContent' && window.studyMode) {
      window.studyMode.refreshTab();
    }
  }

  // ── Public API ─────────────────────────────────────────────────────────────

  /** Delete a note from a card button, without opening the editor first. */
  async function deleteById(noteId) {
    if (!noteId) return;
    if (!(await sableDialog.confirm({
      title: 'Delete this note?',
      message: 'This cannot be undone.',
      okText: 'Delete', danger: true,
    }))) return;
    try {
      await apiSend(`${API}/${encodeURIComponent(noteId)}`, 'DELETE');
      notify('Note deleted');
      if (ed && ed.id === noteId) close();
      await loadNotes();
      refreshViews();
    } catch (err) {
      notify('Delete failed: ' + err.message);
    }
  }

  window.studyNotes = {
    openEditor,
    openPicker,
    close,
    deleteById,
    renderTab,
    renderSidebar,
    sidebarAction,
    bindSidebarSearch,
    notesForDoc,
    renderReaderPane,
    renderQuickCapture,
    loadNotes,
    refreshViews,
    isLoaded: () => loaded,
    reload: () => loadNotes().then(() => { refreshViews(); return NOTES; }),
  };
})();
