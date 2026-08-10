
/* Sable integrated terminal — VS Code style bottom panel.
   Problems / Output / Terminal tabs, multi-session shells (stacked list on
   the right when >1), "..." action menu (new/kill/clear), theme-aware xterm
   colors, resizable via drag handle, pushes floating input up when open. */
(() => {
  const $ = (id) => document.getElementById(id);
  const btn = $('terminalToggle');
  const panel = $('terminalPanel');
  const closeBtn = $('terminalClose');
  const slotsWrap = $('terminalContainer');
  const handle = $('terminalResizeHandle');
  const inputArea = $('inputArea');
  const sessionsEl = $('terminalSessions');
  const emptyEl = $('terminalEmpty');
  const outputLog = $('outputLog');
  const problemsView = $('problemsView');
  const badge = $('problemsBadge');
  const menuBtn = $('termMenuBtn');
  const menu = $('termMenu');
  if (!btn || !panel || !slotsWrap) return;

  const MIN_H = 90;
  const DEFAULT_H = 260;
  const MAX_RATIO = 0.85;
  const H_KEY = 'sable.term.h';

  let height = parseInt(localStorage.getItem(H_KEY) || DEFAULT_H, 10) || DEFAULT_H;

  const isIde = () => document.body.dataset.mode === 'ide';
  const maxH = () => Math.floor(window.innerHeight * MAX_RATIO);
  const clamp = (h) => Math.max(MIN_H, Math.min(h, maxH()));
  const token = () => localStorage.getItem('sable_token') || '';
  const icons = () => { if (window.lucide) window.lucide.createIcons(); };

  /* ---------- theme-aware xterm colors ---------- */
  const cssVar = (n, fb) => (getComputedStyle(document.documentElement).getPropertyValue(n) || '').trim() || fb;
  const termTheme = () => ({
    background: cssVar('--bg', '#0d0d0f'),
    foreground: cssVar('--text', '#e6e6e9'),
    cursor: cssVar('--text', '#e6e6e9'),
    selectionBackground: cssVar('--accent', '#9a7d4a') + '55',
  });

  function wsUrl() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    let url = `${proto}://${location.host}/ws/terminal?token=${encodeURIComponent(token())}`;
    // Open the shell where the user is working (IDE folder), else server cwd.
    const cwd = window.getIdeCwd ? window.getIdeCwd() : '';
    if (cwd) url += `&cwd=${encodeURIComponent(cwd)}`;
    return url;
  }

  /* ---------- height / push-up ---------- */
  function setPanelHeight(h) {
    height = clamp(h);
    panel.style.height = height + 'px';
    // Push the floating input row up so it sits right above the terminal.
    if (inputArea) inputArea.style.bottom = isIde() ? '' : height + 'px';
    try { localStorage.setItem(H_KEY, String(height)); } catch (e) {}
  }

  function fitActive() {
    const s = active();
    if (!s || !s.fit) return;
    try { s.fit.fit(); } catch (e) {}
    if (s.ws && s.ws.readyState === WebSocket.OPEN) {
      s.ws.send(JSON.stringify({ type: 'resize', rows: s.term.rows, cols: s.term.cols }));
    }
  }

  const applyHeight = (h) => { setPanelHeight(h); fitActive(); };

  /* ---------- views (tabs) ---------- */
  let currentView = 'terminal';
  const viewEls = { problems: problemsView, output: $('outputView'), terminal: slotsWrap };

  function switchView(name) {
    currentView = name;
    panel.querySelectorAll('.terminal-tab').forEach((t) => t.classList.toggle('active', t.dataset.view === name));
    for (const [k, el] of Object.entries(viewEls)) el.hidden = k !== name;
    updateEmpty();
    if (name === 'terminal') requestAnimationFrame(() => fitActive());
    if (name === 'problems') refreshProblems();
  }

  /* ---------- sessions ---------- */
  let sessions = [];
  let activeId = null;
  let seq = 0;
  const active = () => sessions.find((s) => s.id === activeId) || null;

  function updateEmpty() {
    if (emptyEl) emptyEl.hidden = !(sessions.length === 0 && currentView === 'terminal');
  }

  function createSession() {
    seq += 1;
    const el = document.createElement('div');
    el.className = 'term-slot';
    slotsWrap.appendChild(el);
    const s = { id: seq, name: seq === 1 ? 'fish' : `fish ${seq}`, el, alive: true, ws: null, term: null, fit: null };
    s.term = new Terminal({
      cursorBlink: true,
      fontSize: 13,
      lineHeight: 1.15,
      fontFamily: "'JetBrainsMono Nerd Font','JetBrains Mono','FiraCode Nerd Font','Fira Code',monospace",
      scrollback: 5000,
      allowProposedApi: true,
      theme: termTheme(),
    });
    s.fit = new FitAddon.FitAddon();
    s.term.loadAddon(s.fit);
    try { s.term.loadAddon(new WebLinksAddon.WebLinksAddon()); } catch (e) {}
    s.term.open(el);
    s.term.onData((d) => {
      if (s.ws && s.ws.readyState === WebSocket.OPEN) s.ws.send(JSON.stringify({ type: 'input', data: d }));
    });
    s.ws = new WebSocket(wsUrl());
    s.ws.onopen = () => fitActive();
    s.ws.onmessage = (ev) => {
      let m; try { m = JSON.parse(ev.data); } catch (e) { return; }
      if (m.type === 'output') s.term.write(m.data);
      else if (m.type === 'exit') {
        s.alive = false;
        s.term.write('\r\n\x1b[90m[shell exited]\x1b[0m\r\n');
        renderSessions();
      }
    };
    s.ws.onclose = () => {
      if (s.alive) {
        s.alive = false;
        s.term.write('\r\n\x1b[90m[connection closed]\x1b[0m\r\n');
        renderSessions();
      }
    };
    sessions.push(s);
    setActive(s.id);
    return s;
  }

  function killSession(id) {
    const i = sessions.findIndex((s) => s.id === id);
    if (i < 0) return;
    const s = sessions[i];
    s.alive = false;
    try { s.ws.close(); } catch (e) {}
    try { s.term.dispose(); } catch (e) {}
    s.el.remove();
    sessions.splice(i, 1);
    if (activeId === id) {
      activeId = null;
      const last = sessions[sessions.length - 1];
      if (last) activeId = last.id;
    }
    // re-apply visibility for the newly active slot
    for (const x of sessions) x.el.style.display = x.id === activeId ? '' : 'none';
    renderSessions();
    updateEmpty();
    requestAnimationFrame(() => fitActive());
  }

  function setActive(id) {
    activeId = id;
    for (const s of sessions) s.el.style.display = s.id === id ? '' : 'none';
    renderSessions();
    updateEmpty();
    // Two rAFs so layout settles before measuring for fit().
    requestAnimationFrame(() => requestAnimationFrame(() => {
      const s = active();
      if (s) { try { s.fit.fit(); } catch (e) {} s.term.focus(); }
    }));
  }

  function renderSessions() {
    if (!sessionsEl) return;
    if (sessions.length < 2) { sessionsEl.hidden = true; sessionsEl.innerHTML = ''; return; }
    sessionsEl.hidden = false;
    sessionsEl.innerHTML = '';
    for (const s of sessions) {
      const row = document.createElement('div');
      row.className = 'session-row' + (s.id === activeId ? ' active' : '') + (s.alive ? '' : ' dead');
      row.innerHTML = `<i data-lucide="terminal" class="s-icon"></i><span class="s-name">${s.name}</span><button class="s-kill" title="Kill terminal"><i data-lucide="trash-2"></i></button>`;
      row.addEventListener('click', () => setActive(s.id));
      row.querySelector('.s-kill').addEventListener('click', (e) => { e.stopPropagation(); killSession(s.id); });
      sessionsEl.appendChild(row);
    }
    icons();
  }

  /* ---------- output stream (SSE) ---------- */
  let es = null;
  const serverProblems = [];

  function startOutput() {
    if (es) return;
    es = new EventSource(`/api/logs?token=${encodeURIComponent(token())}`);
    es.onmessage = (e) => {
      let m; try { m = JSON.parse(e.data); } catch (err) { return; }
      if (m.type === 'log') appendLog(m.message);
    };
  }

  function appendLog(msg) {
    const lvl = (msg.match(/\[(\w+)\]/) || [])[1] || '';
    if (lvl === 'ERROR' || lvl === 'CRITICAL' || lvl === 'WARNING') {
      serverProblems.push({ lvl, msg });
      if (serverProblems.length > 200) serverProblems.shift();
      updateBadge();
      if (currentView === 'problems') refreshProblems();
    }
    const near = outputLog.scrollHeight - outputLog.scrollTop - outputLog.clientHeight < 48;
    const div = document.createElement('div');
    div.className = 'log-line' + (lvl === 'ERROR' || lvl === 'CRITICAL' ? ' error' : lvl === 'WARNING' ? ' warn' : '');
    div.textContent = msg;
    outputLog.appendChild(div);
    while (outputLog.childElementCount > 1500) outputLog.firstElementChild.remove();
    if (near) outputLog.scrollTop = outputLog.scrollHeight;
  }

  /* ---------- problems (server logs + monaco markers) ---------- */
  function markerProblems() {
    if (!(window.monaco && monaco.editor)) return [];
    return monaco.editor.getModelMarkers({})
      .filter((m) => m.severity >= 4 && m.resource.scheme === 'file');
  }

  function updateBadge() {
    if (!badge) return;
    const n = serverProblems.length + markerProblems().length;
    badge.hidden = n === 0;
    badge.textContent = String(n);
  }

  function refreshProblems() {
    if (!problemsView) return;
    problemsView.innerHTML = '';
    const mk = markerProblems();
    if (!serverProblems.length && !mk.length) {
      problemsView.innerHTML = '<div class="problems-empty">✓ No problems detected</div>';
      return;
    }
    for (const p of serverProblems) {
      const row = document.createElement('button');
      row.className = 'problem-row';
      row.title = 'Show in Output';
      const sev = p.lvl === 'WARNING' ? 'sev-warning' : 'sev-error';
      row.innerHTML = `<span class="${sev}">${p.lvl === 'WARNING' ? '⚠' : '⛔'}</span><span class="p-msg"></span>`;
      row.querySelector('.p-msg').textContent = p.msg;
      row.addEventListener('click', () => switchView('output'));
      problemsView.appendChild(row);
    }
    for (const m of mk) {
      const row = document.createElement('button');
      row.className = 'problem-row';
      const sev = m.severity === 8 ? 'sev-error' : 'sev-warning';
      row.innerHTML = `<span class="${sev}">${m.severity === 8 ? '⛔' : '⚠'}</span><span class="p-msg"></span>` +
        `<span class="loc">[${m.startLineNumber}, ${m.startColumn}] ${m.resource.path.split('/').pop()}</span>`;
      row.querySelector('.p-msg').textContent = m.message;
      row.addEventListener('click', () => {
        if (window.openIdeFileAt) window.openIdeFileAt(m.resource.path, m.startLineNumber, m.startColumn);
      });
      problemsView.appendChild(row);
    }
    updateBadge();
  }

  let markersHooked = false;
  function hookMonacoMarkers() {
    if (markersHooked || !(window.monaco && monaco.editor)) return;
    markersHooked = true;
    monaco.editor.onDidChangeMarkers(() => {
      updateBadge();
      if (currentView === 'problems') refreshProblems();
    });
  }
  const hookTimer = setInterval(() => {
    hookMonacoMarkers();
    if (markersHooked) clearInterval(hookTimer);
  }, 2000);

  /* ---------- "..." menu ---------- */
  if (menuBtn && menu) {
    menuBtn.addEventListener('click', (e) => { e.stopPropagation(); menu.hidden = !menu.hidden; });
    document.addEventListener('click', (e) => { if (!menu.hidden && !menu.contains(e.target)) menu.hidden = true; });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') menu.hidden = true; });
    menu.addEventListener('click', (e) => {
      const b = e.target.closest('button');
      const act = b && b.dataset.termAct;
      if (!act) return;
      menu.hidden = true;
      if (act === 'new') { switchView('terminal'); createSession(); }
      else if (act === 'clear') { const s = active(); if (s) s.term.clear(); }
      else if (act === 'kill' && activeId != null) killSession(activeId);
    });
  }
  const emptyNew = $('terminalEmptyNew');
  if (emptyNew) emptyNew.addEventListener('click', () => createSession());

  /* ---------- tabs ---------- */
  panel.querySelectorAll('.terminal-tab').forEach((t) => {
    t.addEventListener('click', () => switchView(t.dataset.view));
  });

  /* ---------- open / close / toggle ---------- */
  function open() {
    panel.classList.remove('hidden');
    btn.classList.add('active');
    setPanelHeight(height);
    if (!sessions.length) createSession();
    startOutput();
    hookMonacoMarkers();
    updateEmpty();
  }

  function close() {
    panel.classList.add('hidden');
    btn.classList.remove('active');
    // Keep shells alive in the background (VS Code behaviour).
    if (inputArea) inputArea.style.bottom = '';
  }

  function toggle() { panel.classList.contains('hidden') ? open() : close(); }

  btn.addEventListener('click', toggle);
  if (closeBtn) closeBtn.addEventListener('click', close);

  // VS Code parity: Ctrl/Cmd+` or Ctrl/Cmd+J toggles the terminal.
  window.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && (e.key === '`' || e.key.toLowerCase() === 'j')) { e.preventDefault(); toggle(); }
  });

  /* ---------- resize handle drag ---------- */
  if (handle) {
    let dragging = false, startY = 0, startH = 0, rafPending = false;
    const scheduleRefit = () => {
      if (rafPending) return;
      rafPending = true;
      requestAnimationFrame(() => { rafPending = false; fitActive(); });
    };
    handle.addEventListener('mousedown', (e) => {
      dragging = true; startY = e.clientY; startH = height;
      document.body.classList.add('term-resizing');
      e.preventDefault();
    });
    window.addEventListener('mousemove', (e) => {
      if (!dragging) return;
      setPanelHeight(startH + (startY - e.clientY));
      scheduleRefit();
    });
    window.addEventListener('mouseup', () => {
      if (!dragging) return;
      dragging = false;
      document.body.classList.remove('term-resizing');
      fitActive();
    });
  }

  /* ---------- keep in sync with window, mode & theme changes ---------- */
  window.addEventListener('resize', () => {
    if (!panel.classList.contains('hidden')) applyHeight(height);
  });

  new MutationObserver(() => {
    if (!panel.classList.contains('hidden')) applyHeight(height);
  }).observe(document.body, { attributes: true, attributeFilter: ['data-mode', 'class'] });

  new MutationObserver(() => {
    const th = termTheme();
    sessions.forEach((s) => { s.term.options.theme = th; });
  }).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });

  /* ---------- Exposed: run a command in the active terminal ---------- */
  window.runInTerminal = function (cmd) {
    open();
    // Wait briefly for session to be ready if just opened
    const trySend = () => {
      const s = active();
      if (s && s.ws && s.ws.readyState === WebSocket.OPEN) {
        s.ws.send(JSON.stringify({ type: 'input', data: cmd + '\n' }));
        return true;
      }
      return false;
    };
    if (!trySend()) {
      // Retry after short delay for newly created sessions
      let attempts = 0;
      const iv = setInterval(() => {
        if (trySend() || ++attempts > 20) clearInterval(iv);
      }, 100);
    }
  };
})();
