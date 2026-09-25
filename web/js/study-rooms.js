/**
 * study-rooms.js — live study rooms backed by Sable-bridge.
 *
 * A room is persistent and account-owned. Content syncs by chain ID: the room
 * has a hash-chain head over its op-log, and a peer stores the seq it last saw,
 * so reconnecting returns only the ops it missed.
 *
 * Transport split:
 *   - REST (create/join/sync/contents/chat) -> /api/rooms/* on Sable
 *   - live fan-out -> the bridge WebSocket directly (its URL comes from
 *     GET /api/rooms/{id}/ws, which also returns the session token, since a
 *     browser cannot set an Authorization header on a WS handshake)
 *
 * Exposes window.studyRooms = { renderTab, renderSidebar, handleClick, leaveAll }.
 */
(function () {
  'use strict';

  const LS_ACTIVE = 'sable_room_active';     // room_id currently entered
  const LS_SEEN = 'sable_room_seen_';        // + room_id -> last synced seq

  let rooms = [];
  let loaded = false;
  let loading = false;
  let error = '';
  let errorStatus = 0;   // HTTP status behind `error` (0 = network/unknown)

  // Open-room state
  let active = null;        // room view object
  let contents = null;      // {docs, notes, highlights, progress}
  let chat = [];
  let members = [];
  let live = 0;
  let socket = null;
  let reconnectTimer = null;
  let reconnectDelay = 1000;
  let panel = 'docs';       // docs | notes | chat | members

  // ── helpers ──────────────────────────────────────────────────────────────

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  function token() {
    try { return localStorage.getItem('sable_token') || ''; } catch { return ''; }
  }

  function toast(msg) {
    if (window.studyMode && window.studyMode.toast) return window.studyMode.toast(msg);
    if (typeof window.showToast === 'function') return window.showToast(msg);
    console.log('[study-rooms]', msg);
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
      const err = new Error(detail);
      err.status = res.status;   // let callers tell 401 apart from a bridge outage
      throw err;
    }
    return res.status === 204 ? null : res.json().catch(() => null);
  }

  function seenFor(roomId) {
    return parseInt(localStorage.getItem(LS_SEEN + roomId) || '0', 10) || 0;
  }

  function markSeen(roomId, seq) {
    try { localStorage.setItem(LS_SEEN + roomId, String(seq)); } catch { /* ignore */ }
  }

  // ── data ─────────────────────────────────────────────────────────────────

  async function reload() {
    if (loading) return rooms;
    loading = true;
    error = '';
    errorStatus = 0;
    try {
      const res = await api('/api/rooms');
      rooms = (res && res.rooms) || [];
      loaded = true;
    } catch (err) {
      error = err.message;
      errorStatus = err.status || 0;
      rooms = [];
      loaded = true;
    } finally {
      loading = false;
    }
    return rooms;
  }

  async function loadPublic() {
    try {
      const res = await api('/api/rooms/public');
      return (res && res.rooms) || [];
    } catch { return []; }
  }

  async function enterRoom(roomId, { navigate = true } = {}) {
    try {
      active = null; contents = null; chat = []; members = []; live = 0;
      panel = 'docs';
      try { localStorage.setItem(LS_ACTIVE, roomId); } catch { /* ignore */ }

      const detail = await api(`/api/rooms/${roomId}`);
      active = detail.room;
      members = (active && active.members) || [];
      live = detail.live || 0;

      // Full state for a first join; the WS then delivers only the delta.
      const c = await api(`/api/rooms/${roomId}/contents`);
      contents = (c && c.contents) || { docs: [], notes: [], highlights: [], progress: [] };

      const h = await api(`/api/rooms/${roomId}/chat?limit=200`);
      chat = (h && h.messages) || [];

      if (navigate && window.studyMode && window.studyMode.getTab() !== 'rooms') {
        window.studyMode.setTab('rooms');
        return;
      }
      refresh();
      connect();
    } catch (err) {
      toast(`Could not open room: ${err.message}`);
      active = null;
      refresh();
    }
  }

  function exitRoom() {
    disconnect();
    if (active) {
      try { localStorage.removeItem(LS_ACTIVE); } catch { /* ignore */ }
      api(`/api/rooms/${active.room_id}/leave`, { method: 'POST' }).catch(() => {});
    }
    active = null; contents = null; chat = []; members = []; live = 0;
    reload().then(() => refresh());
  }

  // ── websocket ────────────────────────────────────────────────────────────

  async function connect() {
    if (!active) return;
    disconnect();
    let info;
    try {
      info = await api(`/api/rooms/${active.room_id}/ws`);
    } catch (err) {
      toast(`Live sync unavailable: ${err.message}`);
      return;
    }

    const since = seenFor(active.room_id);
    const url = `${info.url}?token=${encodeURIComponent(info.token)}&since=${since}`;
    try {
      socket = new WebSocket(url);
    } catch (err) {
      toast('Could not open live connection');
      return;
    }

    socket.onopen = () => {
      reconnectDelay = 1000;
      setLive(active ? active.member_count : 0);
    };

    socket.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.type === 'catch_up') {
        if (msg.ops && msg.ops.length) applyOps(msg.ops);
        if (msg.head) noteHead(msg.seq, msg.head);
      } else if (msg.type === 'op') {
        applyOps([msg.op]);
        noteHead(msg.op.seq, msg.op.chain);
      } else if (msg.type === 'presence') {
        if (typeof msg.live === 'number') setLive(msg.live);
        if (msg.event === 'join' && msg.name) toast(`${msg.name} joined`);
      }
    };

    socket.onclose = (ev) => {
      socket = null;
      // 4401 = bad/expired token, 4404 = room gone. Neither is worth retrying.
      if (ev && (ev.code === 4401 || ev.code === 4404)) {
        if (ev.code === 4404) toast('Room no longer exists');
        else toast('Session expired — sign in again for live sync');
        return;
      }
      scheduleReconnect();
    };

    socket.onerror = () => { /* onclose handles recovery */ };
  }

  function scheduleReconnect() {
    if (!active || reconnectTimer) return;
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      reconnectDelay = Math.min(reconnectDelay * 1.6, 15000);
      connect();
    }, reconnectDelay);
  }

  function disconnect() {
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    if (socket) {
      try { socket.onclose = null; socket.close(); } catch { /* ignore */ }
      socket = null;
    }
  }

  function setLive(n) {
    live = n;
    const el = document.getElementById('srsRoomLive');
    if (el) el.textContent = String(n);
  }

  function noteHead(seq, chain) {
    if (!active || !seq) return;
    markSeen(active.room_id, seq);
    if (socket && socket.readyState === WebSocket.OPEN) {
      try { socket.send(JSON.stringify({ type: 'ack', seq, chain })); } catch { /* ignore */ }
    }
    if (active) { active.seq = Math.max(active.seq || 0, seq); active.chain = chain || active.chain; }
  }

  /** Apply incoming ops to the local read model, then repaint. */
  function applyOps(ops) {
    if (!contents) contents = { docs: [], notes: [], highlights: [], progress: [] };
    for (const op of ops) {
      const p = op.payload || {};
      if (op.kind === 'doc') {
        const id = p.doc_id;
        if (id && !contents.docs.some((d) => d.doc_id === id)) {
          contents.docs.push({ ...p, added_by: op.author_name, added: op.ts });
        }
      } else if (op.kind === 'note') {
        const id = p.note_id;
        if (id) {
          if (p.deleted) contents.notes = contents.notes.filter((n) => n.note_id !== id);
          else {
            const i = contents.notes.findIndex((n) => n.note_id === id);
            const rec = { ...p, author_name: op.author_name, updated: op.ts };
            if (i >= 0) contents.notes[i] = rec; else contents.notes.unshift(rec);
          }
        }
      } else if (op.kind === 'highlight') {
        const id = p.hl_id;
        if (id) {
          if (p.deleted) contents.highlights = contents.highlights.filter((h) => h.hl_id !== id);
          else {
            const i = contents.highlights.findIndex((h) => h.hl_id === id);
            const rec = { ...p, author_name: op.author_name, updated: op.ts };
            if (i >= 0) contents.highlights[i] = rec; else contents.highlights.push(rec);
          }
        }
      } else if (op.kind === 'progress') {
        const key = `${op.author}:${p.doc_id}`;
        const i = contents.progress.findIndex((x) => `${x.user_id}:${x.doc_id}` === key);
        const rec = { user_id: op.author, name: op.author_name, doc_id: p.doc_id,
                      page: p.page, percent: p.percent, ts: op.ts };
        if (i >= 0) contents.progress[i] = rec; else contents.progress.push(rec);
      } else if (op.kind === 'chat') {
        chat.push({ room_id: active.room_id, user_id: op.author, name: op.author_name,
                    text: p.text, ts: op.ts, seq: op.seq, op_id: op.op_id });
      } else if (op.kind === 'member_join' || op.kind === 'member_leave') {
        // Membership is refreshed from the room record on next open; the live
        // member list is maintained by the presence frames.
      }
    }
    refresh();
  }

  // ── rendering ────────────────────────────────────────────────────────────

  function renderTab() {
    if (!loaded) {
      reload().then(() => {
        if (window.studyMode && window.studyMode.getTab() === 'rooms') refresh();
      }).catch(() => {});
      return '<div class="rm-loading">Loading rooms…</div>';
    }
    return active ? renderRoom() : renderList();
  }

  function renderList() {
    if (error) {
      // A 401 really is a sign-in problem. Anything else (bridge offline,
      // timeout, 5xx) is NOT — saying "sign in" there sends the user chasing
      // an account that already works.
      const needsAuth = errorStatus === 401;
      const icon = needsAuth ? '🔐' : '🔌';
      const title = needsAuth ? 'Rooms need an account' : 'Rooms unavailable';
      const hint = needsAuth
        ? 'Rooms are stored on the Sable bridge. Sign in from Settings → Profile, then reload.'
        : 'This is not a sign-in problem — the bridge could not be reached. Check its status in Settings → Profile, then reload.';
      return `
        <div class="rm-head"><h2>Rooms</h2></div>
        <div class="study-empty">
          <div class="study-empty-icon">${icon}</div>
          <h3>${title}</h3>
          <p>${esc(error)}</p>
          <p class="rm-hint">${hint}</p>
        </div>`;
    }

    const totalPending = rooms.reduce((n, r) => n + (r.pending || 0), 0);
    const header = `
      <div class="rm-head">
        <div>
          <h2>Study rooms</h2>
          <p class="rm-sub">${
            rooms.length
              ? `${rooms.length} room${rooms.length === 1 ? '' : 's'}${totalPending ? ` · <strong>${totalPending}</strong> unsynced change${totalPending === 1 ? '' : 's'}` : ''}`
              : 'Study together — shared documents, notes, highlights, and chat.'
          }</p>
        </div>
        <div class="rm-head-actions">
          <button class="rm-btn" data-rm-action="discover"><i data-lucide="radar" class="icon-lucide"></i> Nearby</button>
          <button class="rm-btn" data-rm-action="join"><i data-lucide="log-in" class="icon-lucide"></i> Join</button>
          <button class="rm-btn primary" data-rm-action="new"><i data-lucide="plus" class="icon-lucide"></i> New room</button>
        </div>
      </div>`;

    if (!rooms.length) {
      return header + `
        <div class="study-empty">
          <div class="study-empty-icon">👥</div>
          <h3>No rooms yet</h3>
          <p>Create a room and share its code — everyone in it sees the same documents, notes and highlights.</p>
          <button class="study-empty-btn" data-rm-action="new">Create your first room</button>
        </div>`;
    }

    const grid = rooms.map((r) => `
      <div class="rm-card" data-rm-room="${r.room_id}" role="button" tabindex="0">
        <div class="rm-card-top">
          <div class="rm-card-icon"><i data-lucide="${r.visibility === 'lan' ? 'wifi' : 'lock'}" class="icon-lucide"></i></div>
          ${r.pending ? `<span class="rm-badge pending">${r.pending} new</span>` : '<span class="rm-badge clean">synced</span>'}
        </div>
        <h3 class="rm-card-name">${esc(r.name)}</h3>
        ${r.description ? `<p class="rm-card-desc">${esc(r.description)}</p>` : ''}
        <div class="rm-card-meta">
          <span><i data-lucide="users" class="icon-lucide"></i> ${r.member_count}</span>
          <span class="rm-code" title="Room code">${esc(r.room_id)}</span>
        </div>
      </div>`).join('');

    return header + `<div class="rm-grid">${grid}</div>`;
  }

  function renderRoom() {
    const r = active;
    const items = contents || { docs: [], notes: [], highlights: [], progress: [] };
    const tabs = [
      ['docs', 'book-open', `Documents ${items.docs.length}`],
      ['notes', 'notebook-pen', `Notes ${items.notes.length}`],
      ['highlights', 'highlighter', `Highlights ${items.highlights.length}`],
      ['chat', 'message-square', `Chat ${chat.length}`],
      ['members', 'users', `Members ${(members || []).length}`],
    ];

    return `
      <div class="rm-head">
        <div class="rm-head-left">
          <button class="rm-btn ghost" data-rm-action="back"><i data-lucide="arrow-left" class="icon-lucide"></i> Rooms</button>
          <div>
            <h2>${esc(r.name)}</h2>
            <p class="rm-sub">
              code <code class="rm-code-inline">${esc(r.room_id)}</code>
              · <span id="srsRoomLive">${live}</span> online
              · <span class="rm-synced" title="Chain head (seq)">seq ${r.seq || 0}</span>
            </p>
          </div>
        </div>
        <div class="rm-head-actions">
          <button class="rm-btn" data-rm-action="copy-code"><i data-lucide="copy" class="icon-lucide"></i> Copy code</button>
          <button class="rm-btn" data-rm-action="push-doc"><i data-lucide="share-2" class="icon-lucide"></i> Share a document</button>
        </div>
      </div>

      <div class="rm-tabs">
        ${tabs.map(([k, icon, label]) => `
          <button class="rm-tab${panel === k ? ' active' : ''}" data-rm-panel="${k}">
            <i data-lucide="${icon}" class="icon-lucide"></i> ${label}
          </button>`).join('')}
      </div>

      <div class="rm-panel">${renderPanel()}</div>`;
  }

  function renderPanel() {
    const c = contents || { docs: [], notes: [], highlights: [], progress: [] };

    if (panel === 'docs') {
      if (!c.docs.length) return empty('📄', 'No documents shared', 'Share one of your library documents so everyone in the room can open it.');
      return `<div class="rm-list">${c.docs.map((d) => `
        <div class="rm-row" data-rm-doc="${esc(d.doc_id)}">
          <i data-lucide="file-text" class="icon-lucide"></i>
          <div class="rm-row-main">
            <div class="rm-row-title">${esc(d.title || 'Document')}</div>
            <div class="rm-row-sub">${d.pages ? d.pages + ' pages · ' : ''}shared by ${esc(d.added_by || '—')}</div>
          </div>
          <i data-lucide="chevron-right" class="icon-lucide rm-row-go"></i>
        </div>`).join('')}</div>`;
    }

    if (panel === 'notes') {
      if (!c.notes.length) return empty('📝', 'No shared notes', 'Notes added by anyone in the room appear here for everyone.');
      return `<div class="rm-list">${c.notes.map((n) => `
        <div class="rm-note">
          <div class="rm-note-head">
            <span class="rm-note-title">${esc(n.title || 'Note')}</span>
            <span class="rm-note-by">${esc(n.author_name || '—')}</span>
          </div>
          <div class="rm-note-body">${esc(n.body || '')}</div>
        </div>`).join('')}</div>`;
    }

    if (panel === 'highlights') {
      if (!c.highlights.length) return empty('🖍️', 'No highlights yet', 'Highlights made in a shared document sync to everyone here.');
      const byDoc = {};
      for (const h of c.highlights) {
        const k = h.doc_id || 'unknown';
        (byDoc[k] = byDoc[k] || []).push(h);
      }
      return Object.entries(byDoc).map(([docId, hs]) => `
        <div class="rm-hl-group">
          <div class="rm-hl-doc">${esc(hs[0].doc_title || docId.slice(0, 8) + '…')}</div>
          ${hs.map((h) => `
            <div class="rm-hl" data-rm-doc="${esc(docId)}" data-rm-page="${h.page || 1}">
              <span class="rm-hl-page">p${h.page || '?'}</span>
              <span class="rm-hl-text">${esc(h.text || '')}</span>
              <span class="rm-hl-by">${esc(h.author_name || '')}</span>
            </div>`).join('')}
        </div>`).join('');
    }

    if (panel === 'chat') {
      return `
        <div class="rm-chat" id="rmChat">
          ${chat.length ? chat.map((m) => `
            <div class="rm-msg">
              <span class="rm-msg-by">${esc(m.name || '—')}</span>
              <span class="rm-msg-text">${esc(m.text || '')}</span>
            </div>`).join('') : '<div class="rm-chat-empty">No messages yet — say hello.</div>'}
        </div>
        <div class="rm-chat-input">
          <input id="rmChatInput" type="text" placeholder="Message the room…" autocomplete="off">
          <button class="rm-btn primary" data-rm-action="send-chat"><i data-lucide="send" class="icon-lucide"></i></button>
        </div>`;
    }

    // members
    return `
      <div class="rm-list">
        ${(members || []).map((m) => `
          <div class="rm-member">
            <div class="rm-avatar">${esc((m.name || '?').slice(0, 1).toUpperCase())}</div>
            <div class="rm-row-main">
              <div class="rm-row-title">${esc(m.name || 'Member')}</div>
              <div class="rm-row-sub">${esc(m.role || 'member')}${m.joined ? ' · joined ' + String(m.joined).slice(0, 10) : ''}</div>
            </div>
            ${m.role === 'owner' ? '<span class="rm-badge clean">owner</span>' : ''}
          </div>`).join('')}
      </div>
      ${active && active.owner_id ? '' : ''}`;
  }

  function empty(icon, title, body) {
    return `<div class="study-empty"><div class="study-empty-icon">${icon}</div><h3>${title}</h3><p>${body}</p></div>`;
  }

  function renderSidebar() {
    if (active) {
      const c = contents || { docs: [], notes: [], highlights: [], progress: [] };
      return `
        <div class="study-sidebar-section">
          <button class="study-sidebar-item" data-rm-action="back"><i data-lucide="arrow-left" class="icon-lucide"></i> All rooms</button>
        </div>
        <div class="study-sidebar-section">
          <div class="study-sidebar-label">${esc(active.name)}</div>
          <button class="study-sidebar-item active"><i data-lucide="book-open" class="icon-lucide"></i> Documents <span class="sidebar-count">${c.docs.length}</span></button>
          <button class="study-sidebar-item"><i data-lucide="notebook-pen" class="icon-lucide"></i> Notes <span class="sidebar-count">${c.notes.length}</span></button>
          <button class="study-sidebar-item"><i data-lucide="highlighter" class="icon-lucide"></i> Highlights <span class="sidebar-count">${c.highlights.length}</span></button>
          <button class="study-sidebar-item"><i data-lucide="message-square" class="icon-lucide"></i> Chat <span class="sidebar-count">${chat.length}</span></button>
          <button class="study-sidebar-item"><i data-lucide="users" class="icon-lucide"></i> Members <span class="sidebar-count">${(members || []).length}</span></button>
        </div>
        <div class="study-sidebar-section">
          <div class="study-sidebar-label">Actions</div>
          <button class="study-sidebar-item" data-rm-action="copy-code"><i data-lucide="copy" class="icon-lucide"></i> Copy room code</button>
          <button class="study-sidebar-item" data-rm-action="push-doc"><i data-lucide="share-2" class="icon-lucide"></i> Share a document</button>
          <button class="study-sidebar-item" data-rm-action="leave"><i data-lucide="log-out" class="icon-lucide"></i> Leave room</button>
        </div>`;
    }

    return `
      <div class="study-sidebar-section">
        <div class="study-sidebar-label">Your rooms</div>
        <button class="study-sidebar-item active"><i data-lucide="users" class="icon-lucide"></i> All rooms <span class="sidebar-count">${rooms.length}</span></button>
        <button class="study-sidebar-item"><i data-lucide="clock" class="icon-lucide"></i> Unsynced <span class="sidebar-count">${rooms.reduce((n, r) => n + (r.pending || 0), 0)}</span></button>
      </div>
      <div class="study-sidebar-section">
        <div class="study-sidebar-label">Actions</div>
        <button class="study-sidebar-item" data-rm-action="new"><i data-lucide="plus-circle" class="icon-lucide"></i> New room</button>
        <button class="study-sidebar-item" data-rm-action="join"><i data-lucide="log-in" class="icon-lucide"></i> Join by code</button>
        <button class="study-sidebar-item" data-rm-action="discover"><i data-lucide="radar" class="icon-lucide"></i> Nearby rooms</button>
        <button class="study-sidebar-item" data-rm-action="refresh"><i data-lucide="refresh-cw" class="icon-lucide"></i> Refresh</button>
      </div>`;
  }

  function refresh() {
    if (window.studyMode && window.studyMode.getTab() === 'rooms') {
      window.studyMode.refreshTab();
      const box = document.getElementById('rmChat');
      if (box) box.scrollTop = box.scrollHeight;
    }
  }

  // ── actions ──────────────────────────────────────────────────────────────

  async function newRoom() {
    const res = await sableDialog.form({
      title: 'New study room',
      okText: 'Create room',
      fields: [
        { key: 'name', label: 'Room name', placeholder: 'e.g. CSE 3101 Final' },
        { key: 'description', label: 'Description (optional)', placeholder: 'What is this room for?' },
      ],
    });
    if (!res || !res.name) return;

    // Visibility is a choice, not a yes/no — a pick reads better than a confirm
    // whose "Cancel" secretly meant "private".
    const visibility = await sableDialog.pick({
      title: 'Who can find this room?',
      okText: 'Create',
      options: [
        { label: 'Private', sub: 'Join by code only — nobody can discover it', value: 'private' },
        { label: 'Local network', sub: 'Anyone on the same Wi-Fi can find and join', value: 'lan' },
      ],
    });
    if (!visibility) return;

    try {
      const created = await api('/api/rooms', {
        method: 'POST',
        body: JSON.stringify({
          name: res.name, description: res.description || '', visibility,
        }),
      });
      await reload();
      toast(`Room created — code ${created.room.room_id}`);
      enterRoom(created.room.room_id);
    } catch (err) {
      toast(`Could not create room: ${err.message}`);
    }
  }

  async function joinRoom() {
    const code = ((await sableDialog.prompt({
      title: 'Join a room', label: 'Room code',
      placeholder: '16-character code', okText: 'Join',
    })) || '').trim();
    if (!code) return;
    try {
      await api(`/api/rooms/${code}/join`, { method: 'POST' });
      enterRoom(code);
    } catch (err) {
      toast(`Could not join: ${err.message}`);
    }
  }

  async function discover() {
    try {
      const list = await loadPublic();
      if (!list.length) {
        await sableDialog.alert({
          title: 'No nearby rooms',
          message: 'Nothing is discoverable on this network right now.',
        });
        return;
      }
      const roomId = await sableDialog.pick({
        title: 'Nearby rooms',
        okText: 'Join',
        options: list.map((r) => ({
          label: r.name,
          sub: `${r.member_count} member${r.member_count === 1 ? '' : 's'} · ${r.room_id}`,
          value: r.room_id,
        })),
      });
      if (!roomId) return;
      await api(`/api/rooms/${roomId}/join`, { method: 'POST' });
      enterRoom(roomId);
    } catch (err) {
      toast(`Discovery failed: ${err.message}`);
    }
  }

  async function shareDoc() {
    if (!active) return;
    let docs = [];
    try {
      const res = await fetch('/api/study/docs');
      docs = await res.json();
    } catch { /* fall through */ }
    if (!Array.isArray(docs) || !docs.length) {
      await sableDialog.alert({ title: 'Library is empty', message: 'Upload a document first.' });
      return;
    }
    const docId = await sableDialog.pick({
      title: 'Share which document?',
      okText: 'Share',
      width: 520,
      options: docs.slice(0, 60).map((d) => ({
        label: d.title || 'Untitled',
        sub: [d.pages ? `${d.pages} pages` : '', (d.group && d.group.subject) || ''].filter(Boolean).join(' · '),
        value: d.id,
      })),
    });
    if (!docId) return;
    const d = docs.find((x) => x.id === docId);
    if (!d) return;
    await sendOp('doc', { doc_id: d.id, title: d.title, pages: d.pages || 0 });
    toast(`Shared "${d.title}"`);
  }

  async function sendOp(kind, payload) {
    if (!active) return;
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({ type: 'op', kind, payload }));
      return;
    }
    // Fall back to REST so an edit is never silently lost on a dropped socket.
    try {
      await api(`/api/rooms/${active.room_id}/op`, {
        method: 'POST',
        body: JSON.stringify({ kind, payload }),
      });
    } catch (err) {
      toast(`Sync failed: ${err.message}`);
    }
  }

  function sendChat() {
    const input = document.getElementById('rmChatInput');
    if (!input) return;
    const text = (input.value || '').trim();
    if (!text) return;
    input.value = '';
    sendOp('chat', { text });
  }

  async function copyCode() {
    if (!active) return;
    try {
      await navigator.clipboard.writeText(active.room_id);
      toast('Room code copied');
    } catch {
      // Clipboard blocked (insecure context) — show it selectable instead.
      await sableDialog.prompt({
        title: 'Room code', label: 'Copy this code',
        value: active.room_id, okText: 'Done',
      });
    }
  }

  function handleClick(e) {
    // panel tabs
    const tab = e.target.closest('[data-rm-panel]');
    if (tab) { panel = tab.dataset.rmPanel; refresh(); return true; }

    // room card
    const card = e.target.closest('[data-rm-room]');
    if (card && !e.target.closest('[data-rm-action]')) {
      enterRoom(card.dataset.rmRoom);
      return true;
    }

    // document / highlight deep link into the reader
    const docEl = e.target.closest('[data-rm-doc]');
    if (docEl && !e.target.closest('[data-rm-action]')) {
      const page = parseInt(docEl.dataset.rmPage, 10) || 1;
      if (window.studyMode && window.studyMode.openDoc) {
        window.studyMode.openDoc(docEl.dataset.rmDoc, page);
      }
      return true;
    }

    const el = e.target.closest('[data-rm-action]');
    if (!el) return false;
    const action = el.dataset.rmAction;

    if (action === 'new') newRoom();
    else if (action === 'join') joinRoom();
    else if (action === 'discover') discover();
    else if (action === 'refresh') reload().then(() => refresh());
    else if (action === 'back') exitRoom();
    else if (action === 'leave') exitRoom();
    else if (action === 'copy-code') copyCode();
    else if (action === 'push-doc') shareDoc();
    else if (action === 'send-chat') sendChat();
    return true;
  }

  document.addEventListener('keydown', (e) => {
    if (e.target && e.target.id === 'rmChatInput' && e.key === 'Enter') {
      e.preventDefault();
      sendChat();
    }
  });

  window.addEventListener('beforeunload', () => { disconnect(); });

  window.studyRooms = {
    renderTab,
    renderSidebar,
    handleClick,
    reload,
    // Lets the reader push a highlight/progress straight into the open room.
    pushOp: sendOp,
    isInRoom: () => !!active,
    roomId: () => (active ? active.room_id : ''),
    leaveAll: () => { exitRoom(); },
  };
})();
