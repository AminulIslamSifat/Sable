/* ── Telegram Bot Settings Tab ─────────────────────────────────────────────── */

(function () {
  let _loaded = false;

  window._botSettingsInit = async function (force) {
    if (_loaded && !force) return;
    const panel = document.getElementById('botSettingsPanel');
    if (!panel) return;
    panel.innerHTML = '<p class="muted" style="font-size:12px;">Loading…</p>';

    try {
      const [cfgRes, statusRes] = await Promise.all([
        fetch('/api/telegram-bot/config'),
        fetch('/api/telegram-bot/status'),
      ]);
      const cfg = await cfgRes.json();
      const status = await statusRes.json();
      renderBotSettings(panel, cfg, status);
      _loaded = true;
    } catch (e) {
      panel.innerHTML = '<p style="color:var(--danger,#ff5050);font-size:12px;">Failed to load bot settings.</p>';
    }
  };

  function renderBotSettings(panel, cfg, status) {
    const hasToken = cfg.has_token || false;
    const maskedToken = cfg.bot_token_masked || '';
    const running = status.running || false;
    const enabled = cfg.enabled !== false;
    const allowedUsers = cfg.allowed_users || [];

    panel.innerHTML = `
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:16px;">
        <span style="font-size:24px;">🤖</span>
        <div>
          <h3 style="margin:0;font-size:15px;color:var(--text);">Telegram Bot</h3>
          <p class="muted" style="margin:2px 0 0;font-size:11px;">Chat with Sable directly from Telegram. Get a bot token from <a href="https://t.me/BotFather" target="_blank" style="color:var(--accent);">@BotFather</a>.</p>
        </div>
      </div>

      <!-- Status indicator -->
      <div style="display:flex;align-items:center;gap:8px;padding:8px 12px;border-radius:8px;background:var(--bg-secondary);margin-bottom:16px;font-size:12px;">
        <span style="width:8px;height:8px;border-radius:50%;background:${running ? 'var(--success,#4caf50)' : 'var(--muted,#888)'};flex-shrink:0;"></span>
        <span style="color:var(--text);">${running ? 'Bot is running' : 'Bot is not running'}</span>
        ${hasToken ? `<span style="margin-left:auto;color:var(--muted);font-family:var(--font-mono);font-size:11px;">${maskedToken}</span>` : ''}
      </div>

      <!-- Token input -->
      <div style="margin-bottom:14px;">
        <label style="display:block;font-size:12px;color:var(--text-dim);margin-bottom:4px;font-weight:600;">Bot Token</label>
        <div style="display:flex;gap:8px;">
          <input id="botTokenInput" type="password" placeholder="${hasToken ? 'Enter new token to replace' : 'Paste bot token from @BotFather'}"
            style="flex:1;background:var(--panel);color:var(--text);border:1px solid var(--border);border-radius:8px;padding:8px 12px;font-size:12px;font-family:var(--font-mono);" />
          <button id="botSaveTokenBtn" class="icon-btn" style="padding:6px 16px;font-size:12px;white-space:nowrap;">💾 Save</button>
        </div>
        <div id="botTokenMsg" style="font-size:11px;margin-top:4px;"></div>
      </div>

      <!-- Toggle -->
      <div style="display:flex;align-items:center;justify-content:space-between;padding:8px 0;border-top:1px solid var(--border);">
        <label style="font-size:12px;color:var(--text-dim);">Enable Bot</label>
        <input type="checkbox" id="botEnabledToggle" ${enabled ? 'checked' : ''} style="cursor:pointer;" />
      </div>

      <!-- Allowed users -->
      <div style="padding:8px 0;border-top:1px solid var(--border);">
        <label style="display:block;font-size:12px;color:var(--text-dim);margin-bottom:4px;font-weight:600;">Allowed User IDs</label>
        <input id="botAllowedUsers" type="text" value="${allowedUsers.join(', ')}"
          placeholder="Leave empty to allow all users"
          style="width:100%;background:var(--panel);color:var(--text);border:1px solid var(--border);border-radius:8px;padding:8px 12px;font-size:12px;font-family:var(--font-mono);box-sizing:border-box;" />
        <p class="muted" style="font-size:10px;margin:4px 0 0;">Comma-separated Telegram user IDs. Empty = anyone can use the bot.</p>
      </div>

      <!-- Actions -->
      <div style="display:flex;gap:8px;margin-top:14px;padding-top:12px;border-top:1px solid var(--border);">
        <button id="botRefreshBtn" class="icon-btn" style="padding:6px 14px;font-size:11px;">↻ Refresh Status</button>
      </div>
    `;

    // Save token
    panel.querySelector('#botSaveTokenBtn').addEventListener('click', async () => {
      const token = panel.querySelector('#botTokenInput').value.trim();
      const msgEl = panel.querySelector('#botTokenMsg');
      if (!token) { msgEl.style.color = 'var(--danger,#ff5050)'; msgEl.textContent = 'Enter a token first.'; return; }
      msgEl.style.color = 'var(--muted)'; msgEl.textContent = 'Saving…';
      try {
        const res = await fetch('/api/telegram-bot/config', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ bot_token: token }),
        });
        const data = await res.json();
        if (data.ok) {
          msgEl.style.color = 'var(--success,#4caf50)'; msgEl.textContent = '✓ Token saved. Restart server to activate.';
          panel.querySelector('#botTokenInput').value = '';
        } else {
          msgEl.style.color = 'var(--danger,#ff5050)'; msgEl.textContent = 'Failed to save.';
        }
      } catch { msgEl.style.color = 'var(--danger,#ff5050)'; msgEl.textContent = 'Network error.'; }
    });

    // Enabled toggle
    panel.querySelector('#botEnabledToggle').addEventListener('change', async (e) => {
      try {
        await fetch('/api/telegram-bot/config', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ enabled: e.target.checked }),
        });
      } catch {}
    });

    // Allowed users
    panel.querySelector('#botAllowedUsers').addEventListener('change', async (e) => {
      const raw = e.target.value.trim();
      const ids = raw ? raw.split(/[,\s]+/).filter(s => /^\d+$/.test(s)).map(Number) : [];
      try {
        await fetch('/api/telegram-bot/config', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ allowed_users: ids }),
        });
      } catch {}
    });

    // Refresh
    panel.querySelector('#botRefreshBtn').addEventListener('click', () => {
      _loaded = false;
      window._botSettingsInit(true);
    });
  }
})();
