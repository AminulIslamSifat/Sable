/* ── Unified Search Panel ──
 * Opens in chat area via search rail button.
 * Searches: messages, skills, memory, notes, todos, schedules, research, agents.
 * Each result is an expandable card. Chat/skill cards have "Open" button.
 */
(function () {
  const $ = (sel) => document.querySelector(sel);

  let isOpen = false;
  let debounceTimer = null;
  const _inlineContent = new Map(); // key: cardId -> full content string

  /* ── Source config: icon, label, color accent ── */
  const SOURCE_META = {
    message:  { icon: 'message-square', label: 'Chat Messages',  color: 'var(--accent)' },
    skill:    { icon: 'zap',            label: 'Skill Events',   color: '#f59e0b' },
    memory:   { icon: 'brain',          label: 'Memory',         color: '#8b5cf6' },
    note:     { icon: 'file-text',      label: 'Notes',          color: '#10b981' },
    todo:     { icon: 'check-square',   label: 'Todos',          color: '#ef4444' },
    schedule: { icon: 'calendar-days',  label: 'Schedules',      color: '#3b82f6' },
    research: { icon: 'flask-conical',  label: 'Research',       color: '#ec4899' },
    agent:    { icon: 'bot',            label: 'Agents',         color: '#06b6d4' },
  };

  /* ── Open / Close ── */
  function openSearch() {
    if (isOpen) return;
    isOpen = true;
    const chatEl = $('#chat');
    const inputEl = $('#inputArea');
    if (chatEl) { chatEl.classList.add('hidden'); chatEl.style.display = 'none'; }
    if (inputEl) { inputEl.classList.add('hidden'); inputEl.style.display = 'none'; }
    if (window.sidebarHost?.isTerminalOpen?.()) window.sidebarHost.closeTerminal();
    if (window.sidebarHost?.getCurrent?.()) window.sidebarHost.unhost();
    // Hide other main-area views
    ['researchView', 'imageView', 'promptgenView', 'dashboardView', 'knowledgeView', 'calendarView', 'ocrView'].forEach(id => {
      const el = document.getElementById(id);
      if (el) { el.classList.add('hidden'); el.style.display = ''; }
    });
    document.body.classList.add('search-open');
    const view = $('#searchView');
    if (view) {
      view.classList.remove('hidden');
      const input = view.querySelector('#searchInput');
      if (input) setTimeout(() => input.focus(), 50);
    }
  }

  function closeSearch() {
    if (!isOpen) return;
    isOpen = false;
    document.body.classList.remove('search-open');
    const view = $('#searchView');
    if (view) { view.classList.add('hidden'); view.style.display = ''; }
    const chatEl = $('#chat');
    const inputEl = $('#inputArea');
    if (chatEl) { chatEl.classList.remove('hidden'); chatEl.style.display = ''; }
    if (inputEl) { inputEl.classList.remove('hidden'); inputEl.style.display = ''; }
  }

  /* ── Search ── */
  async function doSearch(query) {
    const resultsEl = $('#searchResults');
    if (!resultsEl) return;
    if (!query.trim()) {
      resultsEl.innerHTML = '<div class="search-empty">Start typing to search across chats, skills, memory, notes, todos, schedules, research & agents</div>';
      return;
    }
    resultsEl.innerHTML = '<div class="search-loading"><i data-lucide="loader-circle" class="spin"></i> Searching…</div>';
    if (window.lucide) window.lucide.createIcons({ nodes: [resultsEl] });
    try {
      const res = await fetch('/api/search/unified?q=' + encodeURIComponent(query.trim()) + '&limit=20');
      const data = await res.json();
      renderResults(data, query);
    } catch (err) {
      resultsEl.innerHTML = '<div class="search-error">Search failed: ' + escapeHtml(err.message) + '</div>';
    }
  }

  /* ── Render grouped results ── */
  function renderResults(data, query) {
    const el = $('#searchResults');
    if (!el) return;

    // Map internal source names to API response keys
    const SOURCE_KEYS = {
      message: 'messages', skill: 'skills', memory: 'memory',
      note: 'notes', todo: 'todos', schedule: 'schedules',
      research: 'research', agent: 'agents',
    };
    const sources = Object.keys(SOURCE_KEYS);
    const groups = [];
    let total = 0;
    for (const src of sources) {
      const items = data[SOURCE_KEYS[src]] || [];
      if (items.length) {
        groups.push({ source: src, items });
        total += items.length;
      }
    }

    if (total === 0) {
      el.innerHTML = '<div class="search-empty">No results for "' + escapeHtml(query) + '"</div>';
      return;
    }

    const sourceLabels = groups.map(g => g.items.length + ' ' + (SOURCE_META[g.source]?.label || g.source)).join(', ');
    let html = '<div class="search-summary">' + total + ' result' + (total !== 1 ? 's' : '') + ' — ' + sourceLabels + '</div>';

    for (const group of groups) {
      const meta = SOURCE_META[group.source] || { icon: 'circle', label: group.source, color: 'var(--muted)' };
      html += '<div class="search-section">';
      html += '<div class="search-section-header" style="--section-color:' + meta.color + '">';
      html += '<i data-lucide="' + meta.icon + '"></i> ' + meta.label;
      html += '<span class="search-section-count">' + group.items.length + '</span>';
      html += '</div>';

      for (const item of group.items.slice(0, 15)) {
        html += renderCard(item, group.source, query);
      }
      html += '</div>';
    }

    el.innerHTML = html;
    if (window.lucide) window.lucide.createIcons({ nodes: [el] });
    bindCardEvents(el, query);
  }

  /* ── Render a single card ── */
  function renderCard(item, source, query) {
    const canOpen = source === 'message' || source === 'skill';
    // Memory uses key as title; others use title/skill
    const title = source === 'memory' ? (item.key || '') : (item.title || item.skill || '');
    const preview = item.preview || item.content || '';
    const time = formatTime(item.created_at || item.updated_at);
    const roleIcon = item.role === 'user' ? '👤 ' : item.role === 'assistant' ? '🤖 ' : '';
    const catBadge = item.category ? '<span class="search-badge">' + escapeHtml(item.category) + '</span>' : '';
    const dueBadge = item.due_date ? '<span class="search-badge search-due">📅 ' + escapeHtml(item.due_date) + '</span>' : '';
    const schedType = item.schedule_type ? '<span class="search-badge">' + escapeHtml(item.schedule_type) + '</span>' : '';
    const scoreTag = item.score ? '<span class="search-badge">' + Math.round(item.score * 100) + '%</span>' : '';

    let openBtn = '';
    if (canOpen && item.chat_id) {
      openBtn = '<button class="search-open-btn" data-chat-id="' + item.chat_id + '" data-msg-id="' + (item.id || item.message_id || '') + '" title="Open in chat"><i data-lucide="external-link"></i> Open</button>';
    }

    // Store full_content in JS map for memory (avoids HTML attribute escaping issues)
    const cardKey = source + ':' + (item.id || item.message_id || Math.random());
    if (item.full_content) _inlineContent.set(cardKey, item.full_content);

    return '<div class="search-card" data-source="' + source + '" data-id="' + (item.id || item.message_id || '') + '" data-chat-id="' + (item.chat_id || '') + '" data-message-id="' + (item.message_id || '') + '" data-card-key="' + cardKey + '">'
      + '<div class="search-card-head">'
      + '<span class="search-card-title">' + roleIcon + highlightMatch(escapeHtml(title), query) + '</span>'
      + '<span class="search-card-meta">' + catBadge + scoreTag + dueBadge + schedType + (time ? '<span class="search-card-time">' + time + '</span>' : '') + '</span>'
      + '</div>'
      + '<div class="search-card-preview">' + highlightMatch(escapeHtml(preview), query) + '</div>'
      + '<div class="search-card-expanded hidden"></div>'
      + '<div class="search-card-actions">'
      + openBtn
      + '</div>'
      + '</div>';
  }

  /* ── Bind click events after render ── */
  function bindCardEvents(container, query) {
    // Card click → expand/collapse full content
    container.querySelectorAll('.search-card').forEach(card => {
      card.addEventListener('click', (e) => {
        // Don't expand if clicking the Open button
        if (e.target.closest('.search-open-btn')) return;
        toggleExpand(card);
      });
    });

    // Open button → navigate to chat
    container.querySelectorAll('.search-open-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const chatId = btn.dataset.chatId;
        if (!chatId) return;
        closeSearch();
        const railBtn = document.querySelector('.rail-btn[data-rail="search"]');
        if (railBtn) railBtn.classList.remove('active');
        window.dispatchEvent(new CustomEvent('rail-switch', { detail: { target: null } }));
        window.dispatchEvent(new CustomEvent('sable-select-chat', { detail: { chatId } }));
      });
    });
  }

  /* ── Expand card with full content ── */
  async function toggleExpand(card) {
    const expanded = card.querySelector('.search-card-expanded');
    if (!expanded) return;

    // Toggle off if already expanded
    if (!expanded.classList.contains('hidden')) {
      expanded.classList.add('hidden');
      expanded.innerHTML = '';
      card.classList.remove('expanded');
      return;
    }

    const source = card.dataset.source;

    expanded.classList.remove('hidden');
    card.classList.add('expanded');

    // Memory has inline full_content in JS map — no API call needed
    if (source === 'memory') {
      const cardKey = card.dataset.cardKey || '';
      const fullContent = _inlineContent.get(cardKey) || '';
      if (fullContent) {
        expanded.innerHTML = '<div class="search-full-content">' + formatContent(fullContent) + '</div>';
      } else {
        expanded.innerHTML = '<div class="search-expand-empty">No content available</div>';
      }
      return;
    }

    const id = card.dataset.id;
    const chatId = card.dataset.chatId;
    const messageId = card.dataset.messageId;

    expanded.innerHTML = '<div class="search-expand-loading">Loading…</div>';

    try {
      const params = new URLSearchParams({ source, id, chat_id: chatId, message_id: messageId });
      const res = await fetch('/api/search/full-content?' + params);
      const data = await res.json();
      if (data.error) {
        expanded.innerHTML = '<div class="search-expand-error">' + escapeHtml(data.error) + '</div>';
        return;
      }
      renderExpandedContent(expanded, data, source);
    } catch (err) {
      expanded.innerHTML = '<div class="search-expand-error">Failed to load</div>';
    }
  }

  /* ── Render expanded content based on source type ── */
  function renderExpandedContent(container, data, source) {
    if (source === 'message') {
      container.innerHTML = '<div class="search-full-content">' + formatContent(data.content || '') + '</div>';
    } else if (source === 'skill') {
      const events = data.events || [];
      if (!events.length) {
        container.innerHTML = '<div class="search-expand-empty">No skill event data</div>';
        return;
      }
      let html = '';
      for (const ev of events) {
        const name = ev.skill || ev.tool || ev.type || 'event';
        const body = ev.summary || ev.result || ev.output || JSON.stringify(ev, null, 2);
        const bodyStr = typeof body === 'object' ? JSON.stringify(body, null, 2) : String(body);
        html += '<div class="search-skill-event"><div class="search-skill-name">⚡ ' + escapeHtml(name) + '</div>'
          + '<pre class="search-skill-body">' + escapeHtml(bodyStr) + '</pre></div>';
      }
      container.innerHTML = html;
    } else if (source === 'note' || source === 'todo') {
      let html = '';
      if (data.content) html += '<div class="search-full-content">' + formatContent(data.content) + '</div>';
      if (data.items && data.items.length) {
        html += '<ul class="search-checklist">';
        for (const item of data.items) {
          const checked = item.done ? 'checked' : '';
          const text = typeof item === 'string' ? item : (item.text || '');
          html += '<li><input type="checkbox" disabled ' + checked + '> ' + escapeHtml(text) + '</li>';
        }
        html += '</ul>';
      }
      container.innerHTML = html || '<div class="search-expand-empty">Empty note</div>';
    } else if (source === 'schedule') {
      container.innerHTML = '<div class="search-schedule-detail">'
        + '<div><strong>Type:</strong> ' + escapeHtml(data.schedule_type || '') + '</div>'
        + '<div><strong>Time:</strong> ' + escapeHtml(data.time || '—') + '</div>'
        + '<div><strong>Description:</strong> ' + formatContent(data.description || '') + '</div>'
        + (data.day_of_week != null ? '<div><strong>Day:</strong> ' + dayName(data.day_of_week) + '</div>' : '')
        + (data.start_date ? '<div><strong>Start:</strong> ' + escapeHtml(data.start_date) + '</div>' : '')
        + (data.end_date ? '<div><strong>End:</strong> ' + escapeHtml(data.end_date) + '</div>' : '')
        + '</div>';
    } else if (source === 'research' || source === 'agent') {
      container.innerHTML = '<div class="search-full-content search-md-content">' + formatContent(data.content || '') + '</div>';
    } else if (source === 'memory') {
      container.innerHTML = '<div class="search-full-content">' + formatContent(data.content || '') + '</div>';
    } else {
      container.innerHTML = '<div class="search-expand-empty">No expandable content</div>';
    }
  }

  /* ── Helpers ── */
  function escapeHtml(str) {
    if (!str) return '';
    return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function escapeAttr(str) {
    if (!str) return '';
    return String(str).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/\n/g, '&#10;').replace(/\r/g, '');
  }

  function highlightMatch(text, query) {
    if (!query) return text;
    const escaped = query.replace(new RegExp('[.*+?^${}()|[\]\\]', 'g'), '\$&');
    const re = new RegExp('(' + escaped + ')', 'gi');
    return text.replace(re, '<mark class="search-highlight">$1</mark>');
  }

  function formatContent(text) {
    // Basic markdown-ish: preserve newlines, escape HTML
    return escapeHtml(text).replace(/\n/g, '<br>');
  }

  function formatTime(ts) {
    if (!ts) return '';
    try {
      const d = new Date(ts);
      const now = new Date();
      const diff = now - d;
      if (diff < 60000) return 'just now';
      if (diff < 3600000) return Math.floor(diff / 60000) + 'm ago';
      if (diff < 86400000) return Math.floor(diff / 3600000) + 'h ago';
      if (diff < 604800000) return Math.floor(diff / 86400000) + 'd ago';
      return d.toLocaleDateString();
    } catch { return ''; }
  }

  function dayName(n) {
    return ['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'][n] || String(n);
  }

  /* ── Init ── */
  function init() {
    window.addEventListener('rail-switch', (e) => {
      const target = e.detail?.target;
      if (target === 'search') {
        if (isOpen) closeSearch();
        else openSearch();
      } else if (isOpen) {
        closeSearch();
      }
    });

    document.addEventListener('input', (e) => {
      if (e.target.id !== 'searchInput') return;
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(() => doSearch(e.target.value), 250);
    });

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && isOpen) {
        closeSearch();
        const railBtn = document.querySelector('.rail-btn[data-rail="search"]');
        if (railBtn) railBtn.classList.remove('active');
      }
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
