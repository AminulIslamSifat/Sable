/**
 * dashboard.js — Dashboard panel for the activity rail.
 *
 * Shows: agent activity feed, token tracker, scheduled ops health,
 * recent chats, provider breakdown.
 */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  let isOpen = false;
  let refreshTimer = null;

  /* ── API helpers ── */
  async function fetchJSON(url) {
    const r = await fetch(url);
    if (!r.ok) throw new Error(r.statusText);
    return r.json();
  }

  function fmtTokens(n) {
    if (n == null || n === 0) return '0';
    if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M';
    if (n >= 1_000) return (n / 1_000).toFixed(1) + 'K';
    return String(n);
  }

  function timeAgo(iso) {
    if (!iso) return '';
    const d = new Date(iso.replace(' ', 'T') + 'Z');
    const now = new Date();
    const diff = Math.floor((now - d) / 1000);
    if (diff < 60) return 'just now';
    if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
    if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
    if (diff < 604800) return Math.floor(diff / 86400) + 'd ago';
    return d.toLocaleDateString();
  }

  function escHtml(s) {
    const d = document.createElement('div');
    d.textContent = s || '';
    return d.innerHTML;
  }

  /* ── Render functions ── */

  function renderOverviewCard(stats) {
    const totalAgents = Object.values(stats.agent_counts || {}).reduce((a, b) => a + b, 0);
    const completed = stats.agent_counts?.completed || 0;
    const failed = (stats.agent_counts?.failed || 0) + (stats.agent_counts?.timed_out || 0) + (stats.agent_counts?.killed || 0);
    const running = (stats.agent_counts?.spawned || 0) + (stats.agent_counts?.running || 0);

    return `<div class="dash-card">
      <div class="dash-card-header">
        <i data-lucide="zap" class="dash-card-icon-lucide"></i>
        <span class="dash-card-title">Overview</span>
      </div>
      <div class="dash-stat-row">
        <span class="dash-stat-value">${totalAgents}</span>
        <span class="dash-stat-label">total agents</span>
      </div>
      <div style="display:flex;gap:var(--sp-5);margin-top:var(--sp-3);font-size:12px;">
        <span style="color:var(--ok)"><i data-lucide="check" style="width:12px;height:12px;display:inline;vertical-align:-1px;margin-right:2px;"></i>${completed}</span>
        <span style="color:var(--danger)"><i data-lucide="x" style="width:12px;height:12px;display:inline;vertical-align:-1px;margin-right:2px;"></i>${failed}</span>
        <span style="color:var(--accent)"><i data-lucide="refresh-cw" style="width:12px;height:12px;display:inline;vertical-align:-1px;margin-right:2px;"></i>${running}</span>
      </div>
      <div style="margin-top:var(--sp-4);padding-top:var(--sp-3);border-top:1px solid var(--border-soft);font-size:12px;color:var(--muted);display:flex;align-items:center;gap:4px;">
        <i data-lucide="message-square" style="width:12px;height:12px;display:inline;"></i>
        ${stats.chat_stats?.total_chats || 0} chats · ${fmtTokens(stats.chat_stats?.total_messages || 0)} messages
      </div>
    </div>`;
  }

  function renderTokenChart(stats) {
    const daily = stats.daily_tokens || [];
    if (!daily.length) {
      return `<div class="dash-card">
        <div class="dash-card-header">
          <i data-lucide="flame" class="dash-card-icon-lucide"></i>
          <span class="dash-card-title">Token Burn (7d)</span>
        </div>
        <div style="color:var(--muted);font-size:12px;padding:var(--sp-4);">No token data yet</div>
      </div>`;
    }

    const maxTokens = Math.max(...daily.map(d => d.tokens), 1);
    // Nice round Y-axis ceiling
    const yMax = maxTokens < 1000 ? 1000 : Math.ceil(maxTokens / 1000) * 1000;
    const yTicks = 4;
    const yStep = yMax / yTicks;

    // Y-axis labels
    let yLabelsHtml = '';
    for (let i = yTicks; i >= 0; i--) {
      const val = Math.round(yStep * i);
      yLabelsHtml += `<span class="dash-y-label">${fmtTokens(val)}</span>`;
    }

    // Bars + grid lines
    let barsHtml = '';
    let gridHtml = '';
    for (let i = 0; i <= yTicks; i++) {
      const pct = (i / yTicks) * 100;
      gridHtml += `<div class="dash-grid-line" style="bottom:${pct}%"></div>`;
    }

    daily.forEach(d => {
      const pct = Math.max((d.tokens / yMax) * 100, 1);
      const dayLabel = d.day.slice(5); // MM-DD
      const valLabel = fmtTokens(d.tokens);
      barsHtml += `<div class="dash-bar-col">
        <div class="dash-bar-val">${valLabel}</div>
        <div class="dash-bar" style="height:${pct}%" title="${valLabel} tokens · ${d.runs} chats"></div>
        <span class="dash-bar-label">${dayLabel}</span>
      </div>`;
    });

    return `<div class="dash-card">
      <div class="dash-card-header">
        <i data-lucide="flame" class="dash-card-icon-lucide"></i>
        <span class="dash-card-title">Token Burn (7d)</span>
      </div>
      <div class="dash-chart-wrap">
        <div class="dash-y-axis">${yLabelsHtml}</div>
        <div class="dash-chart-area">
          <div class="dash-chart-grid">${gridHtml}</div>
          <div class="dash-bars">${barsHtml}</div>
        </div>
      </div>
    </div>`;
  }

  function renderModelBreakdown(stats) {
    const models = stats.token_by_model || [];
    const maxTokens = Math.max(...models.map(m => m.total_tokens), 1);

    let rowsHtml = '';
    models.slice(0, 6).forEach(m => {
      const pct = Math.max((m.total_tokens / maxTokens) * 100, 2);
      rowsHtml += `<div class="dash-model-row">
        <span class="dash-model-name">${escHtml(m.model)}</span>
        <div class="dash-model-bar-wrap"><div class="dash-model-bar" style="width:${pct}%"></div></div>
        <span class="dash-model-tokens">${fmtTokens(m.total_tokens)}</span>
      </div>`;
    });

    if (!rowsHtml) {
      rowsHtml = '<div style="color:var(--muted);font-size:12px;">No model data yet</div>';
    }

    return `<div class="dash-card">
      <div class="dash-card-header">
        <i data-lucide="bot" class="dash-card-icon-lucide"></i>
        <span class="dash-card-title">Tokens by Provider</span>
      </div>
      <div class="dash-model-list">${rowsHtml}</div>
    </div>`;
  }

  function renderAgentFeed(agents) {
    const list = agents.agents || [];

    let itemsHtml = '';
    list.slice(0, 15).forEach(a => {
      const statusClass = a.status || 'spawned';
      itemsHtml += `<div class="dash-agent-item">
        <div class="dash-agent-status ${statusClass}"></div>
        <div class="dash-agent-body">
          <div><span class="dash-agent-role">${escHtml(a.role)}</span><span class="dash-agent-task">${escHtml(a.task)}</span></div>
          <div class="dash-agent-meta">${a.model ? escHtml(a.model) + ' · ' : ''}${fmtTokens(a.tokens_used)} tok · ${timeAgo(a.created_at)}</div>
        </div>
      </div>`;
    });

    if (!itemsHtml) {
      itemsHtml = '<div style="color:var(--muted);font-size:12px;padding:var(--sp-3);">No agent runs yet</div>';
    }

    return `<div class="dash-card" style="grid-column:span 1;">
      <div class="dash-card-header">
        <i data-lucide="git-branch" class="dash-card-icon-lucide"></i>
        <span class="dash-card-title">Recent Agents</span>
      </div>
      <div class="dash-agent-list">${itemsHtml}</div>
    </div>`;
  }

  function renderOpsHealth(opsData) {
    const ops = opsData.ops || [];

    let itemsHtml = '';
    ops.forEach(op => {
      const health = op.health || 'pending';
      const schedule = op.schedule_type === 'daily' ? `Daily ${op.schedule_time || ''}` :
                       op.schedule_type === 'weekly' ? `Weekly` :
                       op.cron_expression ? `Cron: ${op.cron_expression}` : op.schedule_type;
      const lastRun = op.last_run ? timeAgo(op.last_run) : 'never';

      itemsHtml += `<div class="dash-op-item">
        <div class="dash-op-health ${health}"></div>
        <div class="dash-op-body">
          <div class="dash-op-name">${escHtml(op.name)}</div>
          <div class="dash-op-meta">${schedule} · Last: ${lastRun} · ${op.model || '?'}</div>
        </div>
      </div>`;
    });

    if (!itemsHtml) {
      itemsHtml = '<div style="color:var(--muted);font-size:12px;padding:var(--sp-3);">No scheduled ops</div>';
    }

    return `<div class="dash-card">
      <div class="dash-card-header">
        <i data-lucide="clock" class="dash-card-icon-lucide"></i>
        <span class="dash-card-title">Scheduled Ops</span>
      </div>
      <div class="dash-ops-list">${itemsHtml}</div>
    </div>`;
  }

  function renderRecentChats(stats) {
    const chats = stats.recent_chats || [];

    let itemsHtml = '';
    chats.forEach(c => {
      itemsHtml += `<div class="dash-chat-item" data-chat-id="${c.id}">
        <span class="dash-chat-title">${escHtml(c.title || 'Untitled')}</span>
        <span class="dash-chat-time">${timeAgo(c.updated_at)}</span>
      </div>`;
    });

    if (!itemsHtml) {
      itemsHtml = '<div style="color:var(--muted);font-size:12px;padding:var(--sp-3);">No chats yet</div>';
    }

    return `<div class="dash-card">
      <div class="dash-card-header">
        <i data-lucide="message-circle" class="dash-card-icon-lucide"></i>
        <span class="dash-card-title">Recent Chats</span>
      </div>
      <div class="dash-chat-list">${itemsHtml}</div>
    </div>`;
  }



  /* ── Status Cards (tasks, todos, notes, research, calendar) ── */
  function renderStatusCards(statusData) {
    const t = statusData.tasks || {};
    const td = statusData.todos || {};
    const n = statusData.notes || {};
    const r = statusData.research || {};
    const c = statusData.calendar || {};

    return `
    <div class="dash-card dash-status-card">
      <div class="dash-card-header">
        <i data-lucide="list-checks" class="dash-card-icon-lucide"></i>
        <span class="dash-card-title">Tasks</span>
      </div>
      <div class="dash-status-grid">
        <div class="dash-status-item"><span class="dash-status-val accent">${t.running || 0}</span><span class="dash-status-lbl">running</span></div>
        <div class="dash-status-item"><span class="dash-status-val ok">${t.completed || 0}</span><span class="dash-status-lbl">completed</span></div>
        <div class="dash-status-item"><span class="dash-status-val danger">${t.failed || 0}</span><span class="dash-status-lbl">failed</span></div>
        <div class="dash-status-item"><span class="dash-status-val">${t.total || 0}</span><span class="dash-status-lbl">total</span></div>
      </div>
    </div>
    <div class="dash-card dash-status-card">
      <div class="dash-card-header">
        <span class="dash-card-icon">✅</span>
        <span class="dash-card-title">Todos</span>
      </div>
      <div class="dash-status-grid">
        <div class="dash-status-item"><span class="dash-status-val accent">${td.pending_items || 0}</span><span class="dash-status-lbl">pending</span></div>
        <div class="dash-status-item"><span class="dash-status-val ok">${td.done_items || 0}</span><span class="dash-status-lbl">done</span></div>
        <div class="dash-status-item"><span class="dash-status-val">${td.total_notes || 0}</span><span class="dash-status-lbl">lists</span></div>
      </div>
    </div>
    <div class="dash-card dash-status-card">
      <div class="dash-card-header">
        <span class="dash-card-icon">📝</span>
        <span class="dash-card-title">Notes & Research</span>
      </div>
      <div class="dash-status-grid">
        <div class="dash-status-item"><span class="dash-status-val">${n.count || 0}</span><span class="dash-status-lbl">notes</span></div>
        <div class="dash-status-item"><span class="dash-status-val">${r.sessions || 0}</span><span class="dash-status-lbl">research</span></div>
        <div class="dash-status-item"><span class="dash-status-val accent">${c.today_events || 0}</span><span class="dash-status-lbl">today events</span></div>
      </div>
    </div>`;
  }

  /* ── Provider Cards — renders keys immediately, status updates async ── */
  function renderProviderCards(stats, providerData, statusMap) {
    var providers = providerData.providers || [];
    var breakdown = stats.provider_breakdown || [];
    var countMap = {};
    breakdown.forEach(function(b) { countMap[b.provider.toLowerCase()] = b.count; });

    var cardsHtml = '';
    providers.forEach(function(p) {
      var isSystem = !!p.system;
      var alive = statusMap ? (statusMap[p.name] !== undefined ? statusMap[p.name] : false) : null;
      // While status loading: system providers show ok, others show pending
      var statusDot = alive === null ? (isSystem ? 'ok' : 'pending')
                    : (alive ? 'ok' : 'danger');
      var keyCount = p.keys ? p.keys.length : 0;
      var chatCount = countMap[p.name.toLowerCase()] || 0;

      var keysHtml = '';
      if (p.keys && p.keys.length > 0) {
        p.keys.forEach(function(k) {
          // Individual key dots: only show alive if we have per-key data
          var kDot = isSystem ? 'ok' : (alive === null ? 'pending' : (alive ? 'ok' : 'danger'));
          keysHtml += '<div class="dash-pkey-row">'
            + '<span class="dash-key-dot ' + kDot + '"></span>'
            + '<span class="dash-key-masked">' + escHtml(k.masked) + '</span>'
            + (k.active ? '<span class="dash-key-active-badge">active</span>' : '')
            + '</div>';
        });
      } else {
        keysHtml = '<div class="dash-pkey-empty">' + (isSystem ? 'Session-based' : 'No keys configured') + '</div>';
      }

      cardsHtml += '<div class="dash-provider-card" data-provider="' + escHtml(p.name) + '" onclick="this.classList.toggle(\'expanded\')">'
        + '<div class="dash-provider-header">'
        + '<div class="dash-provider-info">'
        + '<span class="dash-key-dot ' + statusDot + '" data-status-dot="' + escHtml(p.name) + '"></span>'
        + '<span class="dash-provider-name">' + escHtml(p.label) + '</span>'
        + (isSystem ? '<span class="dash-provider-system-tag">system</span>' : '')
        + '</div>'
        + '<div class="dash-provider-meta">'
        + '<span class="dash-provider-chats"><i data-lucide="message-square"></i> ' + chatCount + '</span>'
        + '<span class="dash-provider-keycount"><i data-lucide="key"></i> ' + keyCount + '</span>'
        + '<span class="dash-provider-chevron">▾</span>'
        + '</div>'
        + '</div>'
        + '<div class="dash-provider-keys">' + keysHtml + '</div>'
        + '</div>';
    });

    if (!cardsHtml) {
      cardsHtml = '<div style="color:var(--muted);font-size:12px;">No providers configured</div>';
    }

    return '<div class="dash-card" style="grid-column:1/-1;">'
      + '<div class="dash-card-header">'
      + '<i data-lucide="server" class="dash-card-icon"></i>'
      + '<span class="dash-card-title">Providers</span>'
      + '</div>'
      + '<div class="dash-provider-list">' + cardsHtml + '</div>'
      + '</div>';
  }

  /* Update provider status dots in-place without re-rendering */
  function applyProviderStatus(statusMap) {
    for (var name in statusMap) {
      var alive = statusMap[name];
      var dotClass = alive ? 'ok' : 'danger';
      // Update header dot
      var headerDot = document.querySelector('[data-status-dot="' + name + '"]');
      if (headerDot) {
        headerDot.className = 'dash-key-dot ' + dotClass;
      }
      // Update all key row dots within this provider card
      var card = document.querySelector('[data-provider="' + name + '"]');
      if (card) {
        var keyDots = card.querySelectorAll('.dash-pkey-row .dash-key-dot');
        keyDots.forEach(function(d) { d.className = 'dash-key-dot ' + dotClass; });
      }
    }
  }

  /* ── Skeleton placeholder ── */
  function skeleton(label) {
    return '<div class="dash-card dash-skeleton">'
      + '<div class="dash-card-header"><span class="dash-card-title">' + label + '</span></div>'
      + '<div class="dash-skeleton-body"></div></div>';
  }

  /* ── Progressive render ── */
  function setSlot(id, html) {
    var el = document.getElementById(id);
    if (el) { el.outerHTML = html; }
  }

  function _refreshIcons() {
    if (typeof lucide !== 'undefined') lucide.createIcons();
  }

  /** Fetch all dashboard data and update slots. On first load, show skeletons.
   *  On subsequent refreshes, update content in-place without destroying DOM. */
  async function loadDashboard(isRefresh) {
    var grid = $('dashGrid');
    var refreshBtn = $('dashRefreshBtn');
    if (!grid) return;

    if (refreshBtn) refreshBtn.classList.add('spinning');

    // Only render skeletons on initial load
    if (!isRefresh) {
      grid.innerHTML = [
        '<div id="slot-overview">' + skeleton('Overview') + '</div>',
        '<div id="slot-status">' + skeleton('Status') + '</div>',
        '<div id="slot-tokens">' + skeleton('Token Burn') + '</div>',
        '<div id="slot-models">' + skeleton('Models') + '</div>',
        '<div id="slot-agents">' + skeleton('Agents') + '</div>',
        '<div id="slot-ops">' + skeleton('Ops') + '</div>',
        '<div id="slot-chats">' + skeleton('Chats') + '</div>',
        '<div id="slot-providers">' + skeleton('Providers') + '</div>',
      ].join('');
      _refreshIcons();
    }

    var statsDone = false, providerDone = false;
    var cachedStats = null;

    // Fire all fetches independently — each renders its slot on arrival
    fetchJSON('/api/dashboard/stats').then(function(stats) {
      cachedStats = stats;
      setSlot('slot-overview', renderOverviewCard(stats));
      setSlot('slot-tokens', renderTokenChart(stats));
      setSlot('slot-models', renderModelBreakdown(stats));
      setSlot('slot-chats', renderRecentChats(stats));
      statsDone = true;
      // If providers already loaded, re-render with chat counts
      if (providerDone && window._dashProviderData) {
        setSlot('slot-providers', renderProviderCards(stats, window._dashProviderData, window._dashStatusMap || null));
        _refreshIcons();
      }
      _refreshIcons();
    }).catch(function(e) {
      setSlot('slot-overview', '<div class="dash-card" style="color:var(--danger)">Stats failed: ' + escHtml(e.message) + '</div>');
    });

    fetchJSON('/api/dashboard/status').then(function(statusData) {
      setSlot('slot-status', renderStatusCards(statusData));
      _refreshIcons();
    }).catch(function(e) {
      setSlot('slot-status', '<div class="dash-card" style="color:var(--danger)">Status failed</div>');
    });

    fetchJSON('/api/dashboard/agents?limit=15').then(function(agents) {
      setSlot('slot-agents', renderAgentFeed(agents));
      _refreshIcons();
    }).catch(function(e) {
      setSlot('slot-agents', '<div class="dash-card" style="color:var(--danger)">Agents failed</div>');
    });

    fetchJSON('/api/dashboard/ops').then(function(ops) {
      setSlot('slot-ops', renderOpsHealth(ops));
      _refreshIcons();
    }).catch(function(e) {
      setSlot('slot-ops', '<div class="dash-card" style="color:var(--danger)">Ops failed</div>');
    });

    // Providers: render keys instantly, then fetch status async
    fetchJSON('/api/dashboard/providers').then(function(providerData) {
      window._dashProviderData = providerData;
      providerDone = true;
      // Render immediately with pending status dots
      var st = statsDone && cachedStats ? cachedStats : { provider_breakdown: [] };
      setSlot('slot-providers', renderProviderCards(st, providerData, null));
      _refreshIcons();
      // Now fetch status and update dots in-place
      fetchJSON('/api/dashboard/providers/status').then(function(statusResp) {
        window._dashStatusMap = statusResp.status || {};
        applyProviderStatus(window._dashStatusMap);
      }).catch(function() { /* status dots stay pending */ });
    }).catch(function(e) {
      setSlot('slot-providers', '<div class="dash-card" style="color:var(--danger)">Providers failed</div>');
    }).finally(function() {
      if (refreshBtn) refreshBtn.classList.remove('spinning');
    });
  }

  /* ── Open / Close ── */
  function openDashboard() {
    if (isOpen) return;
    isOpen = true;

    // Close terminal if open
    if (window.sidebarHost?.isTerminalOpen?.()) {
      window.sidebarHost.closeTerminal();
    }
    // Close any hosted sidebar panel
    if (window.sidebarHost?.getCurrent?.()) {
      window.sidebarHost.unhost();
    }

    // Hide other main-area views
    const chatEl = $('chat');
    const inputEl = $('inputArea');
    if (chatEl) chatEl.classList.add('hidden');
    if (inputEl) inputEl.classList.add('hidden');
    ['searchView', 'researchView', 'imageView', 'promptgenView', 'knowledgeView', 'calendarView', 'ocrView'].forEach(id => {
      const el = $(id);
      if (el) { el.classList.add('hidden'); el.style.display = ''; }
    });

    document.body.classList.add('dashboard-open');
    const view = $('dashboardView');
    if (view) view.classList.remove('hidden');

    loadDashboard(false);

    // Auto-refresh every 30s while open (in-place, no skeleton teardown)
    refreshTimer = setInterval(function() { loadDashboard(true); }, 30000);
  }

  function closeDashboard() {
    if (!isOpen) return;
    isOpen = false;

    document.body.classList.remove('dashboard-open');
    const view = $('dashboardView');
    if (view) view.classList.add('hidden');
    // Restore chat
    const chatEl = $('chat');
    const inputEl = $('inputArea');
    if (chatEl) chatEl.classList.remove('hidden');
    if (inputEl) inputEl.classList.remove('hidden');

    if (refreshTimer) {
      clearInterval(refreshTimer);
      refreshTimer = null;
    }
  }

  /* ── Init ── */
  function init() {
    // Refresh button
    $('dashRefreshBtn')?.addEventListener('click', () => loadDashboard(true));

    // Chat click delegation — close dashboard and switch to that chat
    $('dashGrid')?.addEventListener('click', (e) => {
      const item = e.target.closest('.dash-chat-item');
      if (!item) return;
      const chatId = item.dataset.chatId;
      if (!chatId) return;
      // Close dashboard first
      closeDashboard();
      // Deselect rail button
      const railBtn = document.querySelector('.rail-btn[data-rail="dashboard"]');
      if (railBtn) railBtn.classList.remove('active');
      window.dispatchEvent(new CustomEvent('rail-switch', { detail: { target: null } }));
      // Dispatch chat selection event
      window.dispatchEvent(new CustomEvent('sable-select-chat', { detail: { chatId } }));
    });

    // Listen for rail-switch
    window.addEventListener('rail-switch', (e) => {
      const target = e.detail?.target;
      if (target === 'dashboard') {
        if (isOpen) {
          closeDashboard();
        } else {
          openDashboard();
        }
      } else if (isOpen) {
        closeDashboard();
      }
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
