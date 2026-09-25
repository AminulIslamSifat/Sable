/**
 * study-srs.js — spaced-repetition decks and the review screen.
 *
 * The scheduling backend (SM-2, grades, atomic deck files) already lives in
 * server/api/routes/study_srs.py. This module is the missing surface:
 *   - a Decks tab listing every deck with its due / new / total counts
 *   - a deck view to add, edit, and delete cards
 *   - a full-screen review session with flip + again/hard/good/easy grading
 *
 * A card can be anchored to a document + page, so "open source" jumps back
 * into the reader at the exact passage it came from.
 *
 * Exposes window.studySRS = { renderTab, renderSidebar, reload, createDeck,
 *                             addCards, openReview, dueCount }.
 */
(function () {
  'use strict';

  const GRADES = [
    { key: 'again', label: 'Again', hint: '1', cls: 'again' },
    { key: 'hard', label: 'Hard', hint: '2', cls: 'hard' },
    { key: 'good', label: 'Good', hint: '3', cls: 'good' },
    { key: 'easy', label: 'Easy', hint: '4', cls: 'easy' },
  ];

  let DECKS = [];
  let loaded = false;
  let view = 'decks';        // decks | deck
  let activeDeckId = '';
  let cards = [];
  let summary = { due: 0, decks: [] };

  // Review-session state
  let session = null;        // { deck, queue, index, flipped, done, started, correct }

  // ── Helpers ──────────────────────────────────────────────────────────────

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
    console.log('[study-srs]', msg);
  }

  function dueLabel(deck) {
    const due = deck.due || 0;
    const fresh = deck.new || 0;
    if (due) return `<span class="srs-badge due">${due} due</span>`;
    if (fresh) return `<span class="srs-badge new">${fresh} new</span>`;
    return '<span class="srs-badge clean">caught up</span>';
  }

  function relTime(ts) {
    if (!ts) return 'never';
    const diff = Date.now() / 1000 - ts;
    if (diff < 60) return 'just now';
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    return `${Math.floor(diff / 86400)}d ago`;
  }

  function dueIn(card) {
    const due = Number(card.due || 0);
    const delta = due - Date.now() / 1000;
    if (delta <= 0) return 'due now';
    if (delta < 3600) return `in ${Math.ceil(delta / 60)}m`;
    if (delta < 86400) return `in ${Math.ceil(delta / 3600)}h`;
    return `in ${Math.ceil(delta / 86400)}d`;
  }

  // ── Data ─────────────────────────────────────────────────────────────────

  async function reload() {
    const [decks, due] = await Promise.all([
      api('/api/study/srs/decks'),
      api('/api/study/srs/due').catch(() => ({ due: 0, decks: [] })),
    ]);
    DECKS = Array.isArray(decks) ? decks : [];
    summary = due || { due: 0, decks: [] };
    loaded = true;
    return DECKS;
  }

  async function loadCards(deckId, dueOnly) {
    const q = dueOnly ? '?due_only=true' : '';
    cards = await api(`/api/study/srs/decks/${deckId}/cards${q}`);
    return Array.isArray(cards) ? cards : [];
  }

  function deckById(id) {
    return DECKS.find((d) => d.id === id) || null;
  }

  // ── Tab render ───────────────────────────────────────────────────────────

  function renderTab() {
    if (!loaded) {
      reload().then(() => {
        if (window.studyMode && window.studyMode.getTab() === 'decks') {
          window.studyMode.refreshTab();
        }
      }).catch(() => {});
      return '<div class="srs-loading">Loading decks…</div>';
    }
    return view === 'deck' ? renderDeckView() : renderDeckList();
  }

  function renderDeckList() {
    const totalDue = summary.due || 0;
    const header = `
      <div class="srs-head">
        <div>
          <h2>Decks</h2>
          <p class="srs-sub">${
            totalDue
              ? `<strong>${totalDue}</strong> card${totalDue === 1 ? '' : 's'} ready to review`
              : 'Nothing due — you are caught up.'
          }</p>
        </div>
        <div class="srs-head-actions">
          ${totalDue ? '<button class="srs-btn primary" data-srs-action="review-all"><i data-lucide="play" class="icon-lucide"></i> Review all</button>' : ''}
          <button class="srs-btn" data-srs-action="new-deck"><i data-lucide="plus" class="icon-lucide"></i> New deck</button>
        </div>
      </div>`;

    if (!DECKS.length) {
      return header + `
        <div class="study-empty">
          <div class="study-empty-icon">🎴</div>
          <h3>No decks yet</h3>
          <p>Create a deck, then add cards by hand or generate them from a document with Study AI.</p>
          <button class="study-empty-btn" data-srs-action="new-deck">Create your first deck</button>
        </div>`;
    }

    const grid = DECKS.map((deck) => `
      <div class="srs-deck-card" data-srs-deck="${deck.id}" role="button" tabindex="0">
        <div class="srs-deck-top">
          <div class="srs-deck-icon"><i data-lucide="layers" class="icon-lucide"></i></div>
          ${dueLabel(deck)}
        </div>
        <h3 class="srs-deck-name">${esc(deck.name)}</h3>
        ${deck.description ? `<p class="srs-deck-desc">${esc(deck.description)}</p>` : ''}
        <div class="srs-deck-meta">
          <span><strong>${deck.total || 0}</strong> cards</span>
          <span><strong>${deck.new || 0}</strong> new</span>
          <span>${relTime(deck.updated)}</span>
        </div>
      </div>`).join('');

    return header + `<div class="srs-deck-grid">${grid}</div>`;
  }

  function renderDeckView() {
    const deck = deckById(activeDeckId);
    if (!deck) { view = 'decks'; return renderDeckList(); }

    const rows = cards.length ? cards.map((card) => `
      <div class="srs-card-row" data-card-id="${card.id}">
        <div class="srs-card-main">
          <div class="srs-card-front">${esc(card.front)}</div>
          <div class="srs-card-back">${esc(card.back)}</div>
        </div>
        <div class="srs-card-side">
          <span class="srs-card-due">${dueIn(card)}</span>
          <span class="srs-card-reps">${card.reps || 0} reps</span>
          ${card.lapses ? `<span class="srs-card-lapses">${card.lapses} lapses</span>` : ''}
        </div>
        <div class="srs-card-actions">
          ${card.doc_id ? `<button class="srs-icon-btn" data-srs-action="open-source" data-doc="${card.doc_id}" data-page="${card.page || 1}" title="Open source"><i data-lucide="external-link" class="icon-lucide"></i></button>` : ''}
          <button class="srs-icon-btn" data-srs-action="edit-card" data-card="${card.id}" title="Edit"><i data-lucide="pencil" class="icon-lucide"></i></button>
          <button class="srs-icon-btn danger" data-srs-action="del-card" data-card="${card.id}" title="Delete"><i data-lucide="trash-2" class="icon-lucide"></i></button>
        </div>
      </div>`).join('') : `
      <div class="study-empty">
        <div class="study-empty-icon">🃏</div>
        <h3>This deck is empty</h3>
        <p>Add a card below, or generate a set from a document with Study AI.</p>
      </div>`;

    return `
      <div class="srs-head">
        <div class="srs-head-left">
          <button class="srs-btn ghost" data-srs-action="back"><i data-lucide="arrow-left" class="icon-lucide"></i> Decks</button>
          <div>
            <h2>${esc(deck.name)}</h2>
            <p class="srs-sub">${deck.total || 0} cards · ${deck.due || 0} due · ${deck.new || 0} new</p>
          </div>
        </div>
        <div class="srs-head-actions">
          ${(deck.due || 0) ? '<button class="srs-btn primary" data-srs-action="review-deck"><i data-lucide="play" class="icon-lucide"></i> Review</button>' : ''}
          <button class="srs-btn" data-srs-action="add-card"><i data-lucide="plus" class="icon-lucide"></i> Add card</button>
        </div>
      </div>
      <div class="srs-card-list">${rows}</div>`;
  }

  function renderSidebar() {
    if (view === 'deck') {
      const deck = deckById(activeDeckId);
      const stats = deck ? `
        <div class="study-sidebar-section">
          <div class="study-sidebar-label">${esc(deck.name)}</div>
          <button class="study-sidebar-item active"><i data-lucide="layers" class="icon-lucide"></i> All cards <span class="sidebar-count">${deck.total || 0}</span></button>
          <button class="study-sidebar-item"><i data-lucide="alarm-clock" class="icon-lucide"></i> Due now <span class="sidebar-count">${deck.due || 0}</span></button>
          <button class="study-sidebar-item"><i data-lucide="sparkle" class="icon-lucide"></i> New <span class="sidebar-count">${deck.new || 0}</span></button>
        </div>` : '';
      return `
        <div class="study-sidebar-section">
          <button class="study-sidebar-item" data-srs-action="back"><i data-lucide="arrow-left" class="icon-lucide"></i> All decks</button>
        </div>${stats}
        <div class="study-sidebar-section">
          <div class="study-sidebar-label">Actions</div>
          <button class="study-sidebar-item" data-srs-action="add-card"><i data-lucide="plus" class="icon-lucide"></i> Add card</button>
          <button class="study-sidebar-item" data-srs-action="rename-deck"><i data-lucide="pencil" class="icon-lucide"></i> Rename deck</button>
          <button class="study-sidebar-item" data-srs-action="del-deck"><i data-lucide="trash-2" class="icon-lucide"></i> Delete deck</button>
        </div>`;
    }

    const due = summary.due || 0;
    return `
      <div class="study-sidebar-section">
        <div class="study-sidebar-label">Today</div>
        <button class="study-sidebar-item${due ? ' active' : ''}"><i data-lucide="alarm-clock" class="icon-lucide"></i> Due now <span class="sidebar-count">${due}</span></button>
        <button class="study-sidebar-item"><i data-lucide="layers" class="icon-lucide"></i> Decks <span class="sidebar-count">${DECKS.length}</span></button>
        <button class="study-sidebar-item"><i data-lucide="check-circle" class="icon-lucide"></i> Cards <span class="sidebar-count">${DECKS.reduce((n, d) => n + (d.total || 0), 0)}</span></button>
      </div>
      <div class="study-sidebar-section">
        <div class="study-sidebar-label">Actions</div>
        ${due ? '<button class="study-sidebar-item" data-srs-action="review-all"><i data-lucide="play" class="icon-lucide"></i> Review all due</button>' : ''}
        <button class="study-sidebar-item" data-srs-action="new-deck"><i data-lucide="plus-circle" class="icon-lucide"></i> New deck</button>
        <button class="study-sidebar-item" data-srs-action="refresh"><i data-lucide="refresh-cw" class="icon-lucide"></i> Refresh</button>
      </div>`;
  }

  // ── Review session ───────────────────────────────────────────────────────

  function openReview(deckId, allDue) {
    let queue = [];
    let title = 'Review';

    if (allDue) {
      // Pull every due card from every deck into one session.
      const wanted = (summary.decks || []).map((d) => d.id);
      title = 'All due cards';
      Promise.all(wanted.map((id) => loadCards(id, true).then((list) =>
        list.map((c) => ({ ...c, _deck: id }))
      ))).then((groups) => {
        queue = groups.flat();
        startSession({ id: '', name: title }, queue, true);
      }).catch((err) => toast(err.message));
      return;
    }

    const deck = deckById(deckId);
    if (!deck) return;
    loadCards(deckId, true).then((list) => {
      startSession(deck, list.map((c) => ({ ...c, _deck: deckId })), false);
    }).catch((err) => toast(err.message));
  }

  function startSession(deck, queue, mixed) {
    if (!queue.length) { toast('Nothing due in this deck'); return; }
    session = {
      deck,
      mixed,
      queue,
      index: 0,
      flipped: false,
      done: 0,
      graded: { again: 0, hard: 0, good: 0, easy: 0 },
      started: Date.now(),
    };
    renderSession();
  }

  function renderSession() {
    let host = document.getElementById('srsSession');
    if (!host) {
      host = document.createElement('div');
      host.id = 'srsSession';
      host.className = 'srs-session';
      document.body.appendChild(host);
    }
    host.hidden = false;
    host.innerHTML = sessionBody();
    if (window.lucide) window.lucide.createIcons();
    host.querySelector('#srsClose').addEventListener('click', closeSession);
    host.querySelector('#srsReveal').addEventListener('click', () => {
      session.flipped = true;
      refreshSession();
    });
  }

  function sessionBody() {
    if (!session) return '';

    if (session.index >= session.queue.length) {
      const secs = Math.round((Date.now() - session.started) / 1000);
      const mins = Math.floor(secs / 60);
      const label = mins ? `${mins}m ${secs % 60}s` : `${secs}s`;
      return `
        <div class="srs-session-box srs-done">
          <div class="srs-done-icon">🎉</div>
          <h2>Session complete</h2>
          <p>You reviewed <strong>${session.done}</strong> card${session.done === 1 ? '' : 's'} in ${label}.</p>
          <div class="srs-done-grid">
            ${GRADES.map((g) => `<div class="srs-done-stat ${g.cls}"><span>${session.graded[g.key]}</span>${g.label}</div>`).join('')}
          </div>
          <button class="srs-btn primary" id="srsClose">Done</button>
        </div>`;
    }

    const card = session.queue[session.index];
    const total = session.queue.length;
    const pct = Math.round((session.index / total) * 100);

    return `
      <div class="srs-session-box">
        <div class="srs-session-head">
          <div class="srs-session-title">${esc(session.deck.name)}</div>
          <div class="srs-session-progress"><span>${session.index + 1} / ${total}</span></div>
          <button class="srs-icon-btn" id="srsClose" title="End session"><i data-lucide="x" class="icon-lucide"></i></button>
        </div>
        <div class="srs-progress-bar"><div class="srs-progress-fill" style="width:${pct}%"></div></div>
        <div class="srs-flip" data-flipped="${session.flipped}">
          <div class="srs-face srs-front">
            <div class="srs-face-label">Question</div>
            <div class="srs-face-text">${esc(card.front)}</div>
            ${card.doc_id ? `<div class="srs-face-src">page ${card.page || 1}</div>` : ''}
          </div>
          <div class="srs-face srs-back">
            <div class="srs-face-label">Answer</div>
            <div class="srs-face-text">${esc(card.back)}</div>
          </div>
        </div>
        ${session.flipped
          ? `<div class="srs-grades">${GRADES.map((g) =>
              `<button class="srs-grade ${g.cls}" data-grade="${g.key}"><span class="srs-grade-label">${g.label}</span><span class="srs-grade-hint">${g.hint}</span></button>`
            ).join('')}</div>`
          : '<button class="srs-btn primary srs-reveal" id="srsReveal">Show answer <span class="srs-kbd">Space</span></button>'}
      </div>`;
  }

  function refreshSession() {
    const host = document.getElementById('srsSession');
    if (!host || !session) return;
    host.innerHTML = sessionBody();
    if (window.lucide) window.lucide.createIcons();
    const close = host.querySelector('#srsClose');
    if (close) close.addEventListener('click', closeSession);
    const reveal = host.querySelector('#srsReveal');
    if (reveal) reveal.addEventListener('click', () => { session.flipped = true; refreshSession(); });
    host.querySelectorAll('[data-grade]').forEach((btn) => {
      btn.addEventListener('click', () => grade(btn.dataset.grade));
    });
  }

  async function grade(value) {
    if (!session || !session.flipped) return;
    const card = session.queue[session.index];
    const deckId = card._deck || session.deck.id;

    // Advance immediately — the network call must never block the review
    // rhythm. A failed write surfaces as a toast and the card stays due.
    session.done += 1;
    session.graded[value] = (session.graded[value] || 0) + 1;
    session.index += 1;
    session.flipped = false;
    refreshSession();

    try {
      await api(`/api/study/srs/cards/${deckId}/${card.id}/review`, {
        method: 'POST',
        body: JSON.stringify({ grade: value }),
      });
    } catch (err) {
      toast(`Could not save grade: ${err.message}`);
    }
  }

  function closeSession() {
    const host = document.getElementById('srsSession');
    if (host) { host.hidden = true; host.innerHTML = ''; }
    session = null;
    // Counts changed — refresh the deck grid underneath.
    reload().then(() => {
      if (window.studyMode && window.studyMode.getTab() === 'decks') window.studyMode.refreshTab();
    }).catch(() => {});
  }

  // ── Mutations ────────────────────────────────────────────────────────────

  async function createDeck() {
    const name = ((await sableDialog.prompt({
      title: 'New deck', label: 'Deck name',
      placeholder: 'e.g. CSE 3101 — Final', okText: 'Create',
    })) || '').trim();
    if (!name) return null;
    const deck = await api('/api/study/srs/decks', {
      method: 'POST',
      body: JSON.stringify({ name }),
    });
    await reload();
    toast('Deck created');
    return deck;
  }

  async function addCards(deckId, list) {
    if (!deckId || !list || !list.length) return 0;
    const res = await api(`/api/study/srs/decks/${deckId}/cards/bulk`, {
      method: 'POST',
      body: JSON.stringify({ cards: list }),
    });
    return (res && res.added) || 0;
  }

  async function addCardPrompt() {
    const deck = deckById(activeDeckId);
    if (!deck) return;
    // One dialog for both sides — two sequential prompts lost the first answer
    // when the user cancelled the second.
    const res = await sableDialog.form({
      title: `Add card to ${deck.name}`,
      okText: 'Add card',
      fields: [
        { key: 'front', label: 'Front — the question', type: 'textarea', rows: 2,
          placeholder: 'What is the time complexity of binary search?' },
        { key: 'back', label: 'Back — the answer', type: 'textarea', rows: 3,
          placeholder: 'O(log n)' },
      ],
    });
    if (!res || !res.front || !res.back) return;
    await api(`/api/study/srs/decks/${activeDeckId}/cards`, {
      method: 'POST',
      body: JSON.stringify({ front: res.front, back: res.back }),
    });
    await reload();
    await loadCards(activeDeckId, false);
    refresh();
    toast('Card added');
  }

  async function editCard(cardId) {
    const card = cards.find((c) => c.id === cardId);
    if (!card) return;
    const res = await sableDialog.form({
      title: 'Edit card',
      okText: 'Save',
      fields: [
        { key: 'front', label: 'Front — the question', type: 'textarea', rows: 2, value: card.front },
        { key: 'back', label: 'Back — the answer', type: 'textarea', rows: 3, value: card.back },
      ],
    });
    if (!res || !res.front || !res.back) return;
    await api(`/api/study/srs/cards/${activeDeckId}/${cardId}`, {
      method: 'PATCH',
      body: JSON.stringify({ front: res.front, back: res.back }),
    });
    await loadCards(activeDeckId, false);
    refresh();
    toast('Card updated');
  }

  async function deleteCard(cardId) {
    if (!(await sableDialog.confirm({
      title: 'Delete this card?', okText: 'Delete', danger: true,
    }))) return;
    await api(`/api/study/srs/cards/${activeDeckId}/${cardId}`, { method: 'DELETE' });
    await reload();
    await loadCards(activeDeckId, false);
    refresh();
    toast('Card deleted');
  }

  async function deleteDeck() {
    const deck = deckById(activeDeckId);
    if (!deck) return;
    if (!(await sableDialog.confirm({
      title: `Delete "${deck.name}"?`,
      message: 'Every card in this deck is deleted too. This cannot be undone.',
      okText: 'Delete deck', danger: true,
    }))) return;
    await api(`/api/study/srs/decks/${activeDeckId}`, { method: 'DELETE' });
    view = 'decks';
    activeDeckId = '';
    await reload();
    refresh();
    toast('Deck deleted');
  }

  async function renameDeck() {
    const deck = deckById(activeDeckId);
    if (!deck) return;
    const name = ((await sableDialog.prompt({
      title: 'Rename deck', label: 'Deck name', value: deck.name, okText: 'Rename',
    })) || '').trim();
    if (!name || name === deck.name) return;
    await api(`/api/study/srs/decks/${activeDeckId}`, {
      method: 'PATCH',
      body: JSON.stringify({ name }),
    });
    await reload();
    refresh();
    toast('Deck renamed');
  }

  function openDeck(deckId) {
    activeDeckId = deckId;
    view = 'deck';
    loadCards(deckId, false).then(() => refresh()).catch((err) => toast(err.message));
  }

  function refresh() {
    if (window.studyMode && window.studyMode.getTab() === 'decks') {
      window.studyMode.refreshTab();
    }
  }

  // ── Click delegation ─────────────────────────────────────────────────────

  function handleClick(e) {
    const deckCard = e.target.closest('[data-srs-deck]');
    if (deckCard) { openDeck(deckCard.dataset.srsDeck); return true; }

    const el = e.target.closest('[data-srs-action]');
    if (!el) return false;
    const action = el.dataset.srsAction;

    if (action === 'new-deck') {
      createDeck().then((deck) => { if (deck) openDeck(deck.id); }).catch((err) => toast(err.message));
    } else if (action === 'back') {
      view = 'decks'; activeDeckId = ''; refresh();
    } else if (action === 'add-card') {
      addCardPrompt().catch((err) => toast(err.message));
    } else if (action === 'edit-card') {
      editCard(el.dataset.card).catch((err) => toast(err.message));
    } else if (action === 'del-card') {
      deleteCard(el.dataset.card).catch((err) => toast(err.message));
    } else if (action === 'del-deck') {
      deleteDeck().catch((err) => toast(err.message));
    } else if (action === 'rename-deck') {
      renameDeck().catch((err) => toast(err.message));
    } else if (action === 'review-deck') {
      openReview(activeDeckId, false);
    } else if (action === 'review-all') {
      openReview('', true);
    } else if (action === 'refresh') {
      reload().then(() => refresh()).catch((err) => toast(err.message));
    } else if (action === 'open-source') {
      const docId = el.dataset.doc;
      if (window.studyMode && window.studyMode.openDoc) {
        window.studyMode.openDoc(docId, parseInt(el.dataset.page, 10) || 1);
      }
    }
    return true;
  }

  // Keyboard control during a review: space flips, 1-4 grade.
  document.addEventListener('keydown', (e) => {
    if (!session) return;
    if (e.target && /INPUT|TEXTAREA|SELECT/.test(e.target.tagName)) return;
    if (e.code === 'Space') {
      e.preventDefault();
      if (!session.flipped) { session.flipped = true; refreshSession(); }
      return;
    }
    if (e.key === 'Escape') { closeSession(); return; }
    if (session.flipped) {
      const grade = GRADES.find((g) => g.hint === e.key);
      if (grade) { e.preventDefault(); grade_(grade.key); }
    }
  });

  // Named separately from the DOM handler so the keydown path can reuse it.
  function grade_(value) { grade(value); }

  window.studySRS = {
    renderTab,
    renderSidebar,
    reload,
    createDeck,
    addCards,
    openReview,
    handleClick,
    dueCount: () => summary.due || 0,
    isLoaded: () => loaded,
    openDeck,
  };
})();
