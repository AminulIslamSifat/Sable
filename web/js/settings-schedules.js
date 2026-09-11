/* ── Online Schedule Sources Settings Tab ─────────────────────────────────── */

(function () {
  let _loaded = false;

  window._schedulesSettingsInit = async function (force) {
    if (_loaded && !force) return;
    const panel = document.getElementById('schedulesSettingsPanel');
    if (!panel) return;
    panel.innerHTML = '<p class="muted" style="font-size:12px;">Loading…</p>';

    try {
      const res = await fetch('/api/online-schedule-sources');
      const data = await res.json();
      renderSchedulesSettings(panel, data.sources || []);
      _loaded = true;
    } catch (e) {
      panel.innerHTML = '<p style="color:var(--danger,#ff5050);font-size:12px;">Failed to load schedule sources.</p>';
    }
  };

  function esc(s) {
    const d = document.createElement('div');
    d.textContent = s || '';
    return d.innerHTML;
  }

  function renderSchedulesSettings(panel, sources) {
    panel.innerHTML = `
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:16px;">
        <span style="font-size:24px;">📅</span>
        <div>
          <h3 style="margin:0;font-size:15px;color:var(--text);">Online Schedule Sources</h3>
          <p class="muted" style="margin:2px 0 0;font-size:11px;">Add MongoDB or HTTP JSON sources. Pulled into your calendar as read-only events.</p>
        </div>
      </div>

      <!-- Source list -->
      <div id="schedSourceList" style="display:flex;flex-direction:column;gap:8px;margin-bottom:16px;">
        ${sources.length === 0 ? '<p class="muted" style="font-size:12px;text-align:center;padding:20px;">No sources configured yet.</p>' : ''}
        ${sources.map(s => `
          <div class="sched-source-card" data-id="${esc(s.id)}" style="background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:12px;display:flex;align-items:center;gap:10px;">
            <span style="width:8px;height:8px;border-radius:50%;background:${s.enabled !== false ? 'var(--success,#4caf50)' : 'var(--muted,#888)'};flex-shrink:0;"></span>
            <div style="flex:1;min-width:0;">
              <div style="font-size:13px;font-weight:600;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${esc(s.name)}</div>
              <div style="font-size:11px;color:var(--muted);font-family:var(--font-mono);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${esc(s.uri)}${s.db_name ? ' → ' + esc(s.db_name) : ''}</div>
            </div>
            <label style="display:flex;align-items:center;gap:4px;font-size:11px;color:var(--text-dim);cursor:pointer;flex-shrink:0;">
              <input type="checkbox" class="sched-toggle" data-id="${esc(s.id)}" ${s.enabled !== false ? 'checked' : ''} />
              On
            </label>
            <button class="sched-del-btn" data-id="${esc(s.id)}" title="Delete" style="width:26px;height:26px;border-radius:6px;border:none;background:transparent;color:var(--muted);cursor:pointer;font-size:14px;display:flex;align-items:center;justify-content:center;flex-shrink:0;">✕</button>
          </div>
        `).join('')}
      </div>

      <!-- Add new source form -->
      <div style="background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:14px;">
        <h4 style="margin:0 0 10px;font-size:13px;font-weight:600;color:var(--text);">Add Source</h4>
        <div style="display:flex;flex-direction:column;gap:8px;">
          <input id="schedName" type="text" placeholder="Name (e.g. RUET CSE)" style="background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:8px;padding:8px 12px;font-size:12px;" />
          <input id="schedUri" type="text" placeholder="MongoDB URI or HTTP URL" style="background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:8px;padding:8px 12px;font-size:12px;font-family:var(--font-mono);" />
          <input id="schedDb" type="text" placeholder="DB name (MongoDB only, default: schedule)" value="schedule" style="background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:8px;padding:8px 12px;font-size:12px;" />
          <div style="display:flex;gap:8px;align-items:center;">
            <button id="schedAddBtn" style="padding:7px 18px;font-size:12px;font-weight:600;border-radius:8px;border:none;cursor:pointer;background:var(--accent);color:#fff;">+ Add Source</button>
            <span id="schedMsg" style="font-size:11px;"></span>
          </div>
        </div>
      </div>
    `;

    // Add source
    panel.querySelector('#schedAddBtn').addEventListener('click', async () => {
      const name = panel.querySelector('#schedName').value.trim();
      const uri = panel.querySelector('#schedUri').value.trim();
      const db = panel.querySelector('#schedDb').value.trim() || 'schedule';
      const msg = panel.querySelector('#schedMsg');

      if (!name || !uri) {
        msg.style.color = 'var(--danger,#ff5050)';
        msg.textContent = 'Name and URI required.';
        return;
      }

      msg.style.color = 'var(--muted)';
      msg.textContent = 'Saving…';
      try {
        const res = await fetch('/api/online-schedule-sources', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name, uri, db_name: db }),
        });
        const data = await res.json();
        if (data.ok) {
          _loaded = false;
          window._schedulesSettingsInit(true);
        } else {
          msg.style.color = 'var(--danger,#ff5050)';
          msg.textContent = data.detail || 'Failed.';
        }
      } catch {
        msg.style.color = 'var(--danger,#ff5050)';
        msg.textContent = 'Network error.';
      }
    });

    // Toggle enable/disable
    panel.querySelectorAll('.sched-toggle').forEach(cb => {
      cb.addEventListener('change', async (e) => {
        const id = e.target.dataset.id;
        try {
          await fetch(`/api/online-schedule-sources/${encodeURIComponent(id)}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled: e.target.checked }),
          });
        } catch {}
      });
    });

    // Delete
    panel.querySelectorAll('.sched-del-btn').forEach(btn => {
      btn.addEventListener('click', async () => {
        const id = btn.dataset.id;
        if (!confirm('Delete this source?')) return;
        try {
          await fetch(`/api/online-schedule-sources/${encodeURIComponent(id)}`, { method: 'DELETE' });
          _loaded = false;
          window._schedulesSettingsInit(true);
        } catch {}
      });
    });
  }
})();
