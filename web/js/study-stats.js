/**
 * study-stats.js — study-time analytics.
 *
 * Was a standalone tab inside study mode; that tab is gone. It now renders as
 * a dashboard section, because in study mode the dashboard shows study time +
 * token usage and nothing else.
 *
 * Reuses the `.st-*` rules already in study-features.css so the visual
 * language matches what the tab looked like, and so there is only one place to
 * restyle it.
 *
 * Exposes window.studyStats = { renderCard, reload, setDays, isLoaded }.
 */
(function () {
  'use strict';

  let data = null;
  let days = 30;
  let busy = false;

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
    if (window.showToast) return window.showToast(msg);
    console.log('[study-stats]', msg);
  }

  function fmtDuration(seconds) {
    const s = Math.max(0, Math.round(Number(seconds) || 0));
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m}m`;
    const h = Math.floor(m / 60);
    const rem = m % 60;
    return rem ? `${h}h ${rem}m` : `${h}h`;
  }

  async function reload() {
    if (busy) return data;
    busy = true;
    try {
      data = await api(`/api/study/stats?days=${days}`);
    } catch (err) {
      toast(err.message);
    } finally {
      busy = false;
    }
    return data;
  }

  function setDays(n) {
    days = parseInt(n, 10) || 30;
    data = null;
  }

  function chart(series) {
    if (!series || !series.length) return '';
    const peak = Math.max(...series.map((p) => p.seconds), 1);
    // Cap the bar count so a 1y window doesn't turn into a solid block.
    const points = series.slice(-30);
    return `
      <div class="st-chart">
        ${points.map((p) => {
          const h = Math.max(2, Math.round((p.seconds / peak) * 100));
          const day = p.date.slice(5);
          const label = p.minutes >= 1 ? `${Math.round(p.minutes)}m` : `${Math.round(p.seconds)}s`;
          return `<div class="st-bar-wrap" title="${p.date} — ${label}">
                    <div class="st-bar" style="height:${h}%"></div>
                    <span class="st-bar-label">${day}</span>
                  </div>`;
        }).join('')}
      </div>`;
  }

  function rangeButtons() {
    return [7, 30, 90, 365].map((n) =>
      `<button class="st-range${n === days ? ' active' : ''}" data-st-days="${n}">${n === 365 ? '1y' : n + 'd'}</button>`
    ).join('');
  }

  /**
   * The dashboard card. Returns a loading state when data hasn't arrived yet,
   * so dashboard.js can drop it straight into a slot.
   */
  function renderCard() {
    const header = `
      <div class="dash-card-header">
        <i data-lucide="graduation-cap" class="dash-card-icon-lucide"></i>
        <span class="dash-card-title">Study Time</span>
        <div class="st-ranges dst-ranges">${rangeButtons()}</div>
      </div>`;

    if (!data) {
      return `<div class="dash-card dst-span" id="slot-study">${header}
        <div class="dash-loading">Loading study stats…</div>
      </div>`;
    }

    const empty = !data.sessions;
    const body = empty ? `
      <div class="study-empty">
        <div class="study-empty-icon">⏱️</div>
        <h3>No study time recorded yet</h3>
        <p>Open a document and read for a bit — sessions are recorded automatically when you close it.</p>
      </div>` : `

      <div class="st-cards">
        <div class="st-card">
          <div class="st-card-value">${fmtDuration(data.total_seconds)}</div>
          <div class="st-card-label">Total</div>
        </div>
        <div class="st-card">
          <div class="st-card-value">${data.streak}</div>
          <div class="st-card-label">Day streak 🔥</div>
        </div>
        <div class="st-card">
          <div class="st-card-value">${fmtDuration(data.today_seconds)}</div>
          <div class="st-card-label">Today</div>
        </div>
        <div class="st-card">
          <div class="st-card-value">${data.sessions}</div>
          <div class="st-card-label">Sessions</div>
        </div>
      </div>

      <div class="st-panel-label">Daily activity · last ${data.days} days · ${data.active_days} active</div>
      ${chart(data.series)}

      ${(data.top_docs || []).length ? `
      <div class="st-panel-label dst-top-label">Most studied</div>
      <div class="st-docs">
        ${data.top_docs.map((doc) => `
          <div class="st-doc" data-st-doc="${esc(doc.doc_id)}">
            <i data-lucide="file-text" class="icon-lucide"></i>
            <span class="st-doc-title">${esc(doc.title || 'Deleted document')}</span>
            <span class="st-doc-time">${fmtDuration(doc.seconds)}</span>
          </div>`).join('')}
      </div>` : ''}

      <div class="st-foot">
        <button class="st-clear" data-st-clear="1">Clear all study stats</button>
      </div>`;

    return `<div class="dash-card dst-span" id="slot-study">${header}${body}</div>`;
  }

  window.studyStats = {
    renderCard,
    reload,
    setDays,
    isLoaded: () => !!data,
  };
})();
