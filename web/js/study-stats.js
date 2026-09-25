/**
 * study-stats.js — study-time analytics for the dashboard.
 *
 * Renders the study-time panel that hangs off the library view: total time,
 * streak, a per-day bar chart, and the most-studied documents. Data comes from
 * /api/study/stats, which aggregates the sessions recorded by the reader.
 *
 * Exposes window.studyStats = { render, reload, handleClick }.
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
    if (window.studyMode && window.studyMode.toast) return window.studyMode.toast(msg);
    if (typeof window.showToast === 'function') return window.showToast(msg);
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

  async function reload(force) {
    if (busy) return data;
    busy = true;
    try {
      data = await api(`/api/study/stats?days=${days}`);
      if (force && window.studyMode) window.studyMode.refreshTab();
    } catch (err) {
      toast(err.message);
    } finally {
      busy = false;
    }
    return data;
  }

  function chart(series) {
    if (!series || !series.length) return '';
    const peak = Math.max(...series.map((p) => p.seconds), 1);
    // Show at most the last 30 bars so the strip stays readable on narrow panes.
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

  function render() {
    if (!data) {
      reload(true);
      return '<div class="st-loading">Loading study stats…</div>';
    }

    const empty = !data.sessions;
    return `
      <div class="st-head">
        <div>
          <h2>Study time</h2>
          <p class="st-sub">Last ${data.days} days · ${data.active_days} active day${data.active_days === 1 ? '' : 's'}</p>
        </div>
        <div class="st-ranges">
          ${[7, 30, 90, 365].map((n) =>
            `<button class="st-range${n === days ? ' active' : ''}" data-st-days="${n}">${n === 365 ? '1y' : n + 'd'}</button>`
          ).join('')}
        </div>
      </div>

      ${empty ? `
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

      <div class="st-panel">
        <div class="st-panel-label">Daily activity</div>
        ${chart(data.series)}
      </div>

      ${(data.top_docs || []).length ? `
      <div class="st-panel">
        <div class="st-panel-label">Most studied</div>
        <div class="st-docs">
          ${data.top_docs.map((doc) => `
            <div class="st-doc" data-st-doc="${doc.doc_id}">
              <i data-lucide="file-text" class="icon-lucide"></i>
              <span class="st-doc-title">${esc(doc.title || 'Deleted document')}</span>
              <span class="st-doc-time">${fmtDuration(doc.seconds)}</span>
            </div>`).join('')}
        </div>
      </div>` : ''}

      <div class="st-foot">
        <button class="st-clear" data-st-clear="1">Clear all study stats</button>
      </div>`}
    `;
  }

  function handleClick(e) {
    const rangeEl = e.target.closest('[data-st-days]');
    if (rangeEl) {
      days = parseInt(rangeEl.dataset.stDays, 10) || 30;
      data = null;
      reload(true);
      return true;
    }

    const docEl = e.target.closest('[data-st-doc]');
    if (docEl) {
      if (window.studyMode && window.studyMode.openDoc) {
        window.studyMode.openDoc(docEl.dataset.stDoc, 1);
      }
      return true;
    }

    const clearEl = e.target.closest('[data-st-clear]');
    if (clearEl) {
      // handleClick must stay synchronous — study.js uses its boolean return to
      // decide whether the click was consumed. So confirm asynchronously and
      // return true now; the dialog resolves on its own.
      (async () => {
        const ok = await sableDialog.confirm({
          title: 'Clear all study time?',
          message: 'Every recorded session is deleted. This cannot be undone.',
          okText: 'Clear everything', danger: true,
        });
        if (!ok) return;
        try {
          await api('/api/study/stats', { method: 'DELETE' });
          toast('Study stats cleared');
          data = null;
          reload(true);
        } catch (err) {
          toast(err.message);
        }
      })();
      return true;
    }
    return false;
  }

  window.studyStats = {
    render,
    reload,
    handleClick,
    isLoaded: () => !!data,
  };
})();
