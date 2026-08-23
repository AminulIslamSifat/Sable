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
        <span class="dash-card-icon">⚡</span>
        <span class="dash-card-title">Overview</span>
      </div>
      <div class="dash-stat-row">
        <span class="dash-stat-value">${totalAgents}</span>
        <span class="dash-stat-label">total agents</span>
      </div>
      <div style="display:flex;gap:var(--sp-5);margin-top:var(--sp-3);font-size:12px;">
        <span style="color:var(--ok)">✓ ${completed}</span>
        <span style="color:var(--danger)">✗ ${failed}</span>
        <span style="color:var(--accent)">⟳ ${running}</span>
      </div>
      <div style="margin-top:var(--sp-4);padding-top:var(--sp-3);border-top:1px solid var(--border-soft);font-size:12px;color:var(--muted);">
        💬 ${stats.chat_stats?.total_chats || 0} chats · ${fmtTokens(stats.chat_stats?.total_messages || 0)} messages
      </div>
    </div>`;
  }

  function renderTokenChart(stats) {
    const daily = stats.daily_tokens || [];
    const maxTokens = Math.max(...daily.map(d => d.tokens), 1);

    let barsHtml = '';
    daily.forEach(d => {
      const pct = Math.max((d.tokens / maxTokens) * 100, 2);
      const dayLabel = d.day.slice(5); // MM-DD
      barsHtml += `<div class="dash-bar" style="height:${pct}%" title="${fmtTokens(d.tokens)} tokens, ${d.runs} runs">
        <span class="dash-bar-label">${dayLabel}</span>
      </div>`;
    });

    if (!barsHtml) {
      barsHtml = '<div style="color:var(--muted);font-size:12px;padding:var(--sp-4);">No token data yet</div>';
    }

    return `<div class="dash-card">
      <div class="dash-card-header">
        <span class="dash-card-icon">🔥</span>
        <span class="dash-card-title">Token Burn (7d)</span>
      </div>
      <div class="dash-bar-chart">${barsHtml}</div>
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
        <span class="dash-card-icon">🤖</span>
        <span class="dash-card-title">Tokens by Model</span>
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
        <span class="dash-card-icon">🧵</span>
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
        <span class="dash-card-icon">⏰</span>
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
        <span class="dash-card-icon">💬</span>
        <span class="dash-card-title">Recent Chats</span>
      </div>
      <div class="dash-chat-list">${itemsHtml}</div>
    </div>`;
  }

  function renderProviderBreakdown(stats) {
    const providers = stats.provider_breakdown || [];

    let badgesHtml = '';
    providers.forEach(p => {
      badgesHtml += `<div class="dash-provider-badge">
        <span>${escHtml(p.provider)}</span>
        <span class="dash-provider-count">${p.count}</span>
      </div>`;
    });

    if (!badgesHtml) {
      badgesHtml = '<div style="color:var(--muted);font-size:12px;">No provider data</div>';
    }

    return `<div class="dash-card">
      <div class="dash-card-header">
        <span class="dash-card-icon">🔌</span>
        <span class="dash-card-title">Providers</span>
      </div>
      <div class="dash-provider-list">${badgesHtml}</div>
    </div>`;
  }

  /* ── Main render ── */
  async function loadDashboard() {
    const grid = $('dashGrid');
    const refreshBtn = $('dashRefreshBtn');
    if (!grid) return;

    if (refreshBtn) refreshBtn.classList.add('spinning');

    try {
      const [stats, agents, ops] = await Promise.all([
        fetchJSON('/api/dashboard/stats'),
        fetchJSON('/api/dashboard/agents?limit=15'),
        fetchJSON('/api/dashboard/ops'),
      ]);

      grid.innerHTML = [
        renderOverviewCard(stats),
        renderTokenChart(stats),
        renderModelBreakdown(stats),
        renderAgentFeed(agents),
        renderOpsHealth(ops),
        renderRecentChats(stats),
        renderProviderBreakdown(stats),
      ].join('');
    } catch (e) {
      console.error('Dashboard load failed:', e);
      grid.innerHTML = `<div class="dash-loading" style="color:var(--danger)">Failed to load dashboard: ${escHtml(e.message)}</div>`;
    } finally {
      if (refreshBtn) refreshBtn.classList.remove('spinning');
    }
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
    ['searchView', 'researchView', 'imageView', 'promptgenView', 'knowledgeView', 'calendarView'].forEach(id => {
      const el = $(id);
      if (el) { el.classList.add('hidden'); el.style.display = ''; }
    });

    document.body.classList.add('dashboard-open');
    const view = $('dashboardView');
    if (view) view.classList.remove('hidden');

    loadDashboard();

    // Auto-refresh every 30s while open
    refreshTimer = setInterval(loadDashboard, 30000);
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
    $('dashRefreshBtn')?.addEventListener('click', () => loadDashboard());

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
