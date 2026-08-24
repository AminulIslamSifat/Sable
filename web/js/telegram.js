    /* ---------- Email Panel ---------- */

    /* ---------- Deep Research Panel ---------- */
    /* Extracted to /static/src/research.js — loaded separately in index.html */


    let _emailState = { folder: 'INBOX', messages: [], configured: false, loaded: false };

    async function renderEmailPanel(container, force) {
      if (_emailState.loaded && !force) return;
      container.innerHTML = '<div class="library-loading">Loading…</div>';
      try {
        const res = await fetch('/api/email/configured');
        const cfg = await res.json();
        _emailState.configured = cfg.configured;
        if (!cfg.configured) {
          renderEmailSetup(container);
        } else {
          renderEmailInbox(container, cfg);
        }
        _emailState.loaded = true;
      } catch {
        container.innerHTML = '<div class="library-empty">Failed to connect to email service.</div>';
      }
    }

    function refreshEmailPanel() {
      const container = document.getElementById('tab-lib-email');
      if (!container) return;
      _emailState.loaded = false;
      renderEmailPanel(container, true);
    }

    function renderEmailSetup(container) {
      container.innerHTML = `
        <div class="email-setup">
          <h3 style="margin-bottom:12px;font-size:15px;"><span class="icon-emoji">📧</span><i data-lucide="mail" class="icon-lucide" style="width:16px;height:16px;margin-right:4px;"></i> Configure Email</h3>
          <p style="font-size:12px;color:var(--muted);margin-bottom:16px;">Connect via IMAP/SMTP. Use an app-specific password for Gmail/Outlook.</p>
          <div class="email-form-grid">
            <label>IMAP Host<input id="em-imap-host" placeholder="imap.gmail.com" /></label>
            <label>IMAP Port<input id="em-imap-port" type="number" value="993" /></label>
            <label>SMTP Host<input id="em-smtp-host" placeholder="smtp.gmail.com" /></label>
            <label>SMTP Port<input id="em-smtp-port" type="number" value="587" /></label>
            <label>Email / Username<input id="em-username" placeholder="you@gmail.com" /></label>
            <label>App Password<input id="em-password" type="password" placeholder="••••••••" /></label>
          </div>
          <div style="display:flex;gap:8px;margin-top:14px;">
            <button class="icon-btn" id="em-save-cfg" style="padding:6px 16px;">Save & Connect</button>
            <label style="display:flex;align-items:center;gap:6px;font-size:12px;color:var(--muted);">
              <input type="checkbox" id="em-use-ssl" checked /> Use SSL
            </label>
          </div>
          <div id="em-setup-error" style="color:var(--danger,#ff5050);font-size:12px;margin-top:8px;"></div>
        </div>
      `;
      if (window.lucide) lucide.createIcons({ nodes: container.querySelectorAll('[data-lucide]') });
      container.querySelector('#em-save-cfg').addEventListener('click', async () => {
        const body = {
          imap_host: container.querySelector('#em-imap-host').value.trim(),
          imap_port: parseInt(container.querySelector('#em-imap-port').value) || 993,
          smtp_host: container.querySelector('#em-smtp-host').value.trim(),
          smtp_port: parseInt(container.querySelector('#em-smtp-port').value) || 587,
          username: container.querySelector('#em-username').value.trim(),
          password: container.querySelector('#em-password').value,
          use_ssl: container.querySelector('#em-use-ssl').checked,
        };
        if (!body.imap_host || !body.smtp_host || !body.username || !body.password) {
          container.querySelector('#em-setup-error').textContent = 'All fields are required.';
          return;
        }
        try {
          const res = await fetch('/api/email/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
          });
          const data = await res.json();
          if (!res.ok) {
            container.querySelector('#em-setup-error').textContent = data.detail || 'Connection failed';
            return;
          }
          showToast('Email connected ✓');
          renderEmailPanel(container);
        } catch (e) {
          container.querySelector('#em-setup-error').textContent = 'Network error';
        }
      });
    }

    async function renderEmailInbox(container, cfg) {
      container.innerHTML = `
        <div class="email-toolbar">
          <select id="em-folder-select" class="email-folder-select">
            <option value="INBOX">Inbox</option>
          </select>
          <input id="em-search" class="email-search" placeholder="Search…" />
          <button class="icon-btn email-compose-btn" id="em-compose-btn" title="Compose"><span class="icon-emoji">✉</span><i data-lucide="pen-line" class="icon-lucide"></i> New</button>
          <button class="icon-btn" id="em-refresh-btn" title="Refresh"><span class="icon-emoji">↻</span><i data-lucide="refresh-cw" class="icon-lucide"></i></button>
          <button class="icon-btn" id="em-disconnect-btn" title="Disconnect" style="margin-left:auto;"><span class="icon-emoji">⚙</span><i data-lucide="settings" class="icon-lucide"></i></button>
        </div>
        <div id="em-message-list" class="email-message-list"><div class="library-loading">Loading…</div></div>
        <div id="em-compose-area" class="email-compose-area" style="display:none;"></div>
      `;

      // Load folders
      try {
        const fRes = await fetch('/api/email/folders');
        if (fRes.ok) {
          const folders = await fRes.json();
          const sel = container.querySelector('#em-folder-select');
          sel.innerHTML = '';
          folders.forEach(f => {
            const opt = document.createElement('option');
            opt.value = f;
            opt.textContent = f;
            if (f === _emailState.folder) opt.selected = true;
            sel.appendChild(opt);
          });
        }
      } catch {}

      // Events
      container.querySelector('#em-folder-select').addEventListener('change', (e) => {
        _emailState.folder = e.target.value;
        loadEmailMessages(container);
      });
      container.querySelector('#em-refresh-btn').addEventListener('click', () => refreshEmailPanel());
      container.querySelector('#em-compose-btn').addEventListener('click', () => showComposeForm(container));
      container.querySelector('#em-disconnect-btn').addEventListener('click', async () => {
        if (await sableConfirm('Disconnect email?')) {
          await fetch('/api/email/config', { method: 'DELETE' });
          renderEmailPanel(container);
        }
      });
      let searchTimeout;
      container.querySelector('#em-search').addEventListener('input', (e) => {
        clearTimeout(searchTimeout);
        searchTimeout = setTimeout(() => loadEmailMessages(container, e.target.value.trim()), 400);
      });

      if (window.lucide) lucide.createIcons({ nodes: container.querySelectorAll('[data-lucide]') });
      loadEmailMessages(container);
    }

    async function loadEmailMessages(container, search) {
      const listEl = container.querySelector('#em-message-list');
      if (!listEl) return;
      listEl.innerHTML = '<div class="library-loading">Loading…</div>';
      try {
        let url = '/api/email/messages?folder=' + encodeURIComponent(_emailState.folder) + '&limit=50';
        if (search) url += '&search=' + encodeURIComponent(search);
        const res = await fetch(url);
        if (!res.ok) { const d = await res.json(); listEl.innerHTML = '<div class="library-empty">' + escHtml(d.detail || 'Error') + '</div>'; return; }
        const data = await res.json();
        _emailState.messages = data.messages;
        if (!data.messages.length) {
          listEl.innerHTML = '<div class="library-empty">No messages.</div>';
          return;
        }
        listEl.innerHTML = '';
        data.messages.forEach(msg => {
          const row = document.createElement('div');
          row.className = 'email-msg-row';
          row.innerHTML = `
            <div class="email-msg-from">${escHtml(msg.from || 'Unknown')}</div>
            <div class="email-msg-subject">${msg.has_attachments ? '<i data-lucide="paperclip" style="width:12px;height:12px;display:inline;vertical-align:middle;margin-right:2px;"></i> ' : ''}${escHtml(msg.subject || '(no subject)')}</div>
            <div class="email-msg-date">${formatEmailDate(msg.date)}</div>
          `;
          row.addEventListener('click', () => openEmailMessage(container, msg.uid));
          listEl.appendChild(row);
        });
        if (window.lucide) lucide.createIcons({ nodes: listEl.querySelectorAll('[data-lucide]') });
      } catch {
        listEl.innerHTML = '<div class="library-empty">Failed to load messages.</div>';
      }
    }

    async function openEmailMessage(container, uid) {
      const listEl = container.querySelector('#em-message-list');
      if (!listEl) return;
      listEl.innerHTML = '<div class="library-loading">Loading message…</div>';
      try {
        const res = await fetch('/api/email/message/' + uid + '?folder=' + encodeURIComponent(_emailState.folder));
        if (!res.ok) { const d = await res.json(); listEl.innerHTML = '<div class="library-empty">' + escHtml(d.detail || 'Not found') + '</div>'; return; }
        const msg = await res.json();
        listEl.innerHTML = `
          <div class="email-reader">
            <div class="email-reader-header">
              <button class="icon-btn email-back-btn" id="em-back"><span class="icon-emoji">←</span><i data-lucide="arrow-left" class="icon-lucide"></i> Back</button>
              <div class="email-reader-meta">
                <div class="email-reader-subject">${escHtml(msg.subject)}</div>
                <div class="email-reader-from">From: ${escHtml(msg.from)}</div>
                <div class="email-reader-to">To: ${escHtml(msg.to)}${msg.cc ? ' | Cc: ' + escHtml(msg.cc) : ''}</div>
                <div class="email-reader-date">${msg.date}</div>
              </div>
            </div>
            <div class="email-reader-body">${escHtml(msg.body).replace(/\n/g, '<br>')}</div>
            ${msg.attachments && msg.attachments.length ? '<div class="email-attachments"><strong>Attachments:</strong> ' + msg.attachments.map(a => escHtml(a.filename) + ' (' + (a.size/1024).toFixed(1) + ' KB)').join(', ') + '</div>' : ''}
          </div>
        `;
        if (window.lucide) lucide.createIcons({ nodes: listEl.querySelectorAll('[data-lucide]') });
        listEl.querySelector('#em-back').addEventListener('click', () => loadEmailMessages(container));
      } catch {
        listEl.innerHTML = '<div class="library-empty">Failed to load message.</div>';
      }
    }

    function showComposeForm(container) {
      const area = container.querySelector('#em-compose-area');
      if (!area) return;
      area.style.display = 'block';
      area.innerHTML = `
        <div class="email-compose-form">
          <h4 style="margin-bottom:10px;font-size:13px;"><span class="icon-emoji">✉</span><i data-lucide="send" class="icon-lucide" style="width:14px;height:14px;margin-right:4px;"></i> Compose</h4>
          <input id="em-to" class="email-input" placeholder="To (email)" />
          <input id="em-cc" class="email-input" placeholder="Cc (optional)" />
          <input id="em-subject" class="email-input" placeholder="Subject" />
          <textarea id="em-body" class="email-textarea" rows="8" placeholder="Message body…"></textarea>
          <div style="display:flex;gap:8px;margin-top:8px;">
            <button class="icon-btn" id="em-send" style="padding:6px 16px;">Send</button>
            <button class="icon-btn" id="em-cancel-compose" style="padding:6px 12px;">Cancel</button>
          </div>
          <div id="em-compose-status" style="font-size:12px;margin-top:6px;"></div>
        </div>
      `;
      if (window.lucide) lucide.createIcons({ nodes: area.querySelectorAll('[data-lucide]') });
      area.querySelector('#em-cancel-compose').addEventListener('click', () => { area.style.display = 'none'; });
      area.querySelector('#em-send').addEventListener('click', async () => {
        const to = area.querySelector('#em-to').value.trim();
        const subject = area.querySelector('#em-subject').value.trim();
        const body = area.querySelector('#em-body').value;
        const cc = area.querySelector('#em-cc').value.trim();
        const statusEl = area.querySelector('#em-compose-status');
        if (!to || !subject) { statusEl.textContent = 'To and Subject required.'; statusEl.style.color = 'var(--danger,#ff5050)'; return; }
        statusEl.textContent = 'Sending…'; statusEl.style.color = 'var(--muted)';
        try {
          const res = await fetch('/api/email/send', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ to, subject, body, cc: cc || null }),
          });
          const data = await res.json();
          if (!res.ok) { statusEl.textContent = data.detail || 'Send failed'; statusEl.style.color = 'var(--danger,#ff5050)'; return; }
          statusEl.textContent = '✓ Sent!'; statusEl.style.color = 'var(--success,#4caf50)';
          setTimeout(() => { area.style.display = 'none'; }, 1200);
        } catch { statusEl.textContent = 'Network error'; statusEl.style.color = 'var(--danger,#ff5050)'; }
      });
    }

    function formatEmailDate(dateStr) {
      if (!dateStr) return '';
      try {
        const d = new Date(dateStr);
        const now = new Date();
        if (d.toDateString() === now.toDateString()) {
          return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        }
        return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
      } catch { return dateStr.slice(0, 16); }
    }

    /* ---------- Telegram Mini Client ---------- */

    let _tgState = { loaded: false, configured: false, enabled: false, connected: false, chats: [], activeChatId: null };

    async function renderTelegramPanel(container, force) {
      if (_tgState.loaded && !force) return;
      container.innerHTML = '<div class="library-loading">Loading…</div>';
      try {
        const res = await fetch('/api/telegram/status');
        const status = await res.json();
        _tgState.configured = status.configured;
        _tgState.enabled = status.enabled;
        _tgState.connected = status.connected;
        if (!status.configured || !status.enabled) {
          renderTgSetup(container);
        } else if (!status.connected) {
          renderTgDisconnected(container);
        } else {
          await renderTgChats(container);
        }
        _tgState.loaded = true;
      } catch (e) {
        container.innerHTML = '<div class="library-empty">Failed to connect to Telegram service.</div>';
      }
    }

    function refreshTgPanel() {
      const container = document.getElementById('tab-lib-telegram');
      if (!container) return;
      _tgState.loaded = false;
      renderTelegramPanel(container, true);
    }

    function renderTgSetup(container) {
      container.innerHTML = `
        <div class="email-setup">
          <h3 style="margin-bottom:12px;font-size:15px;"><span class="icon-emoji">✈️</span><i data-lucide="send" class="icon-lucide"></i> Configure Telegram</h3>
          <p style="font-size:12px;color:var(--muted);margin-bottom:16px;">Get API credentials from <a href="https://my.telegram.org/apps" target="_blank" style="color:var(--accent);">my.telegram.org/apps</a>. This is a read-only mini client — no sending.</p>
          <div class="email-form-grid">
            <label>API ID<input id="tg-api-id" type="number" placeholder="12345678" /></label>
            <label>API Hash<input id="tg-api-hash" placeholder="abcdef1234567890" /></label>
          </div>
          <div style="display:flex;gap:8px;margin-top:14px;">
            <button class="icon-btn" id="tg-save-cfg" style="padding:6px 16px;">Save & Connect</button>
          </div>
          <div id="tg-setup-error" style="color:var(--danger,#ff5050);font-size:12px;margin-top:8px;"></div>
        </div>
      `;
      if (window.lucide) lucide.createIcons({ nodes: container.querySelectorAll('[data-lucide]') });
      container.querySelector('#tg-save-cfg').addEventListener('click', async () => {
        const apiId = parseInt(container.querySelector('#tg-api-id').value);
        const apiHash = container.querySelector('#tg-api-hash').value.trim();
        const errEl = container.querySelector('#tg-setup-error');
        if (!apiId || !apiHash) { errEl.textContent = 'Both fields required.'; return; }
        errEl.textContent = 'Saving…';
        try {
          const res = await fetch('/api/telegram/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ api_id: apiId, api_hash: apiHash, enabled: true }),
          });
          if (!res.ok) { errEl.textContent = 'Failed to save config.'; return; }
          refreshTgPanel();
        } catch { errEl.textContent = 'Network error.'; }
      });
    }

    function renderTgDisconnected(container) {
      container.innerHTML = `
        <div class="email-setup">
          <h3 style="margin-bottom:12px;font-size:15px;"><span class="icon-emoji">🔑</span><i data-lucide="key-round" class="icon-lucide"></i> Sign In to Telegram</h3>
          <p style="font-size:12px;color:var(--muted);margin-bottom:16px;">Enter your phone number to receive a login code.</p>
          <div id="tg-signin-step1">
            <div class="email-form-grid">
              <label>Phone Number<input id="tg-phone" placeholder="+1234567890" /></label>
            </div>
            <button class="icon-btn" id="tg-send-code" style="padding:6px 16px;margin-top:12px;">Send Code</button>
            <div id="tg-signin-error" style="color:var(--danger,#ff5050);font-size:12px;margin-top:8px;"></div>
          </div>
          <div id="tg-signin-step2" style="display:none;">
            <div class="email-form-grid">
              <label>Code<input id="tg-code" placeholder="12345" /></label>
            </div>
            <button class="icon-btn" id="tg-verify-code" style="padding:6px 16px;margin-top:12px;">Verify</button>
            <div id="tg-verify-error" style="color:var(--danger,#ff5050);font-size:12px;margin-top:8px;"></div>
          </div>
          <div id="tg-signin-step3" style="display:none;">
            <p style="font-size:12px;color:var(--muted);margin-bottom:10px;">Two-step verification is enabled. Enter your password.</p>
            <div class="email-form-grid">
              <label>Password<input id="tg-password" type="password" placeholder="Your 2FA password" /></label>
            </div>
            <button class="icon-btn" id="tg-verify-password" style="padding:6px 16px;margin-top:12px;">Sign In</button>
            <div id="tg-password-error" style="color:var(--danger,#ff5050);font-size:12px;margin-top:8px;"></div>
          </div>
        </div>
      `;
      if (window.lucide) lucide.createIcons({ nodes: container.querySelectorAll('[data-lucide]') });
      let _tgPhoneCodeHash = null;
      container.querySelector('#tg-send-code').addEventListener('click', async () => {
        const phone = container.querySelector('#tg-phone').value.trim();
        const errEl = container.querySelector('#tg-signin-error');
        if (!phone) { errEl.textContent = 'Phone required.'; return; }
        errEl.textContent = 'Sending code…';
        try {
          const res = await fetch('/api/telegram/signin/send-code', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ phone }),
          });
          const data = await res.json();
          if (!res.ok) { errEl.textContent = data.detail || 'Failed.'; return; }
          _tgPhoneCodeHash = data.phone_code_hash;
          errEl.textContent = '';
          container.querySelector('#tg-signin-step1').style.display = 'none';
          container.querySelector('#tg-signin-step2').style.display = '';
        } catch { errEl.textContent = 'Network error.'; }
      });
      container.querySelector('#tg-verify-code').addEventListener('click', async () => {
        const phone = container.querySelector('#tg-phone').value.trim();
        const code = container.querySelector('#tg-code').value.trim();
        const errEl = container.querySelector('#tg-verify-error');
        if (!code) { errEl.textContent = 'Code required.'; return; }
        if (!_tgPhoneCodeHash) { errEl.textContent = 'Send code first.'; return; }
        errEl.textContent = 'Verifying…';
        try {
          const res = await fetch('/api/telegram/signin/verify', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ phone, code, phone_code_hash: _tgPhoneCodeHash }),
          });
          const data = await res.json();
          if (!res.ok) { errEl.textContent = data.detail || 'Verification failed.'; return; }
          if (data.needs_password) {
            // Show 2FA password step
            container.querySelector('#tg-signin-step2').style.display = 'none';
            container.querySelector('#tg-signin-step3').style.display = '';
            return;
          }
          refreshTgPanel();
        } catch { errEl.textContent = 'Network error.'; }
      });
      container.querySelector('#tg-verify-password').addEventListener('click', async () => {
        const password = container.querySelector('#tg-password').value;
        const errEl = container.querySelector('#tg-password-error');
        if (!password) { errEl.textContent = 'Password required.'; return; }
        errEl.textContent = 'Signing in…';
        try {
          const res = await fetch('/api/telegram/signin/password', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ phone: '', password }),
          });
          const data = await res.json();
          if (!res.ok) { errEl.textContent = data.detail || 'Wrong password.'; return; }
          refreshTgPanel();
        } catch { errEl.textContent = 'Network error.'; }
      });
    }

    async function renderTgChats(container) {
      container.innerHTML = '<div class="library-loading">Loading chats…</div>';
      try {
        const res = await fetch('/api/telegram/chats?limit=50');
        if (!res.ok) throw new Error('Failed');
        _tgState.chats = await res.json();
      } catch {
        container.innerHTML = '<div class="library-empty">Failed to load chats. <button onclick="document.getElementById(\'tab-lib-telegram\').innerHTML=\'\'; window._tgRefresh && window._tgRefresh();" style="color:var(--accent);background:none;border:none;cursor:pointer;text-decoration:underline;">Retry</button></div>';
        window._tgRefresh = () => refreshTgPanel();
        return;
      }
      renderTgChatList(container);
    }

    function renderTgChatList(container) {
      const chats = _tgState.chats;
      if (!chats.length) {
        container.innerHTML = '<div class="library-empty">No chats found.</div>';
        return;
      }
      container.innerHTML = '';
      const wrapper = document.createElement('div');
      wrapper.style.cssText = 'display:flex;flex-direction:column;gap:2px;max-height:70vh;overflow-y:auto;';
      chats.forEach(chat => {
        const row = document.createElement('div');
        row.style.cssText = 'display:flex;align-items:center;gap:10px;padding:10px 12px;border-radius:8px;cursor:pointer;transition:background 0.15s;';
        row.addEventListener('mouseenter', () => row.style.background = 'var(--panel)');
        row.addEventListener('mouseleave', () => row.style.background = 'transparent');
        const icon = chat.is_channel ? '📢' : chat.is_group ? '👥' : '💬';
        const unread = chat.unread > 0 ? `<span style="background:var(--accent);color:#fff;font-size:10px;padding:2px 6px;border-radius:10px;min-width:16px;text-align:center;">${chat.unread}</span>` : '';
        const date = chat.last_date ? formatEmailDate(chat.last_date) : '';
        row.innerHTML = `
          <span style="font-size:18px;flex-shrink:0;">${icon}</span>
          <div style="flex:1;min-width:0;">
            <div style="display:flex;justify-content:space-between;align-items:center;">
              <span style="font-size:13px;font-weight:600;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${escHtml(chat.name)}</span>
              <span style="font-size:11px;color:var(--muted);flex-shrink:0;margin-left:8px;">${date}</span>
            </div>
            <div style="display:flex;justify-content:space-between;align-items:center;margin-top:2px;">
              <span style="font-size:12px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${escHtml(chat.last_message || '')}</span>
              ${unread}
            </div>
          </div>
        `;
        row.addEventListener('click', () => openTgChat(container, chat));
        wrapper.appendChild(row);
      });
      // Back button area
      const header = document.createElement('div');
      header.style.cssText = 'display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;padding-bottom:8px;border-bottom:1px solid var(--border);';
      header.innerHTML = `<span style="font-size:14px;font-weight:600;"><span class="icon-emoji">✈️</span><i data-lucide="send" class="icon-lucide"></i> Chats</span>
        <button class="icon-btn" id="tg-refresh-chats" title="Refresh" style="width:auto;padding:4px 8px;font-size:11px;">↻</button>`;
      container.appendChild(header);
      container.appendChild(wrapper);
      if (window.lucide) lucide.createIcons({ nodes: container.querySelectorAll('[data-lucide]') });
      container.querySelector('#tg-refresh-chats').addEventListener('click', () => {
        _tgState.loaded = false;
        renderTelegramPanel(container, true);
      });
    }

    let _tgPollTimer = null;

    function stopTgPoll() {
      if (_tgPollTimer) { clearInterval(_tgPollTimer); _tgPollTimer = null; }
    }

    async function openTgChat(container, chat) {
      stopTgPoll();
      _tgState.activeChatId = chat.id;
      _tgState.activeChat = chat;
      _tgState.lastMsgId = 0;
      container.innerHTML = '<div class="library-loading">Loading messages…</div>';
      try {
        const res = await fetch(`/api/telegram/chat/${chat.id}/messages?limit=50`);
        if (!res.ok) throw new Error('Failed');
        const messages = await res.json();
        if (messages.length) _tgState.lastMsgId = messages[messages.length - 1].id;
        renderTgMessages(container, chat, messages);
        // Start polling for new messages every 6s
        _tgPollTimer = setInterval(() => tgPollNew(container, chat), 6000);
      } catch {
        container.innerHTML = '<div class="library-empty">Failed to load messages.</div>';
      }
    }

    async function tgPollNew(container, chat) {
      if (_tgState.activeChatId !== chat.id) { stopTgPoll(); return; }
      try {
        const res = await fetch(`/api/telegram/chat/${chat.id}/messages?limit=20&offset_id=${_tgState.lastMsgId}`);
        if (!res.ok) return;
        const msgs = await res.json();
        // Filter only truly new messages
        const newMsgs = msgs.filter(m => m.id > _tgState.lastMsgId);
        if (!newMsgs.length) return;
        const msgArea = container.querySelector('#tg-msg-area');
        if (!msgArea) return;
        const wasAtBottom = msgArea.scrollHeight - msgArea.scrollTop - msgArea.clientHeight < 60;
        newMsgs.forEach(m => {
          msgArea.appendChild(buildTgBubble(m, chat));
          if (m.id > _tgState.lastMsgId) _tgState.lastMsgId = m.id;
        });
        if (wasAtBottom) msgArea.scrollTop = msgArea.scrollHeight;
      } catch {}
    }

    function renderTgMessages(container, chat, messages) {
      container.innerHTML = '';
      // Header with back button
      const header = document.createElement('div');
      header.style.cssText = 'display:flex;align-items:center;gap:8px;margin-bottom:10px;padding-bottom:10px;border-bottom:1px solid var(--border);';
      const icon = chat.is_channel ? '📢' : chat.is_group ? '👥' : '💬';
      header.innerHTML = `
        <button class="icon-btn" id="tg-back" title="Back" style="width:auto;padding:4px 8px;">←</button>
        <span style="font-size:18px;">${icon}</span>
        <span style="font-size:14px;font-weight:600;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${escHtml(chat.name)}</span>
      `;
      container.appendChild(header);
      header.querySelector('#tg-back').addEventListener('click', () => {
        stopTgPoll();
        _tgState.activeChatId = null;
        _tgState.activeChat = null;
        renderTgChatList(container);
      });
      // Messages area
      const msgArea = document.createElement('div');
      msgArea.id = 'tg-msg-area';
      msgArea.style.cssText = 'display:flex;flex-direction:column;gap:6px;max-height:55vh;overflow-y:auto;padding:4px 0;';
      if (!messages.length) {
        msgArea.innerHTML = '<div style="text-align:center;color:var(--muted);font-size:12px;padding:20px;">No messages.</div>';
      } else {
        messages.forEach(m => msgArea.appendChild(buildTgBubble(m, chat)));
      }
      container.appendChild(msgArea);
      // Send box (not for channels unless admin — but we'll allow it, backend will reject if no perms)
      if (!chat.is_channel) {
        const sendBox = document.createElement('div');
        sendBox.style.cssText = 'display:flex;gap:8px;margin-top:10px;padding-top:10px;border-top:1px solid var(--border);align-items:flex-end;';
        sendBox.innerHTML = `
          <textarea id="tg-input" rows="1" placeholder="Type a message…" style="flex:1;background:var(--panel);color:var(--text);border:1px solid var(--border);border-radius:10px;padding:8px 12px;font-size:13px;font-family:inherit;resize:none;max-height:80px;outline:none;"></textarea>
          <button id="tg-send-btn" class="icon-btn" style="padding:8px 14px;font-size:13px;flex-shrink:0;">Send</button>
        `;
        container.appendChild(sendBox);
        const input = sendBox.querySelector('#tg-input');
        const sendBtn = sendBox.querySelector('#tg-send-btn');
        // Auto-resize textarea
        input.addEventListener('input', () => {
          input.style.height = 'auto';
          input.style.height = Math.min(input.scrollHeight, 80) + 'px';
        });
        // Enter to send (Shift+Enter for newline)
        input.addEventListener('keydown', (e) => {
          if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); doTgSend(container, chat, input, msgArea); }
        });
        sendBtn.addEventListener('click', () => doTgSend(container, chat, input, msgArea));
      }
      // Scroll to bottom
      requestAnimationFrame(() => msgArea.scrollTop = msgArea.scrollHeight);
      if (window.lucide) lucide.createIcons({ nodes: container.querySelectorAll('[data-lucide]') });
    }

    function buildTgBubble(m, chat) {
      const bubble = document.createElement('div');
      const isMe = m.is_out;
      bubble.style.cssText = `align-self:${isMe ? 'flex-end' : 'flex-start'};max-width:75%;padding:8px 12px;border-radius:12px;font-size:13px;line-height:1.45;word-break:break-word;background:${isMe ? 'var(--accent-bg, rgba(154,125,74,0.15))' : 'var(--panel)'};border:1px solid ${isMe ? 'var(--accent-border, rgba(154,125,74,0.3))' : 'var(--border)'};`;
      let html = '';
      // Sender name in groups
      if ((chat.is_group || chat.is_channel) && !isMe && m.sender) {
        html += `<div style="font-size:11px;font-weight:600;color:var(--accent);margin-bottom:2px;">${escHtml(m.sender)}</div>`;
      }
      // Media
      if (m.has_media && m.media_type) {
        const mt = m.media_type;
        if (['photo', 'sticker', 'gif'].includes(mt)) {
          const maxW = mt === 'sticker' ? '120px' : '220px';
          html += `<img src="/api/telegram/chat/${chat.id}/media/${m.id}" loading="lazy" style="max-width:${maxW};border-radius:8px;display:block;margin-bottom:4px;cursor:pointer;" onclick="this.style.maxWidth=this.style.maxWidth==='220px'?'100%':'220px'" />`;
        } else if (mt === 'video') {
          html += `<video src="/api/telegram/chat/${chat.id}/media/${m.id}" controls preload="metadata" style="max-width:220px;border-radius:8px;display:block;margin-bottom:4px;"></video>`;
        } else if (mt === 'voice' || mt === 'audio') {
          html += `<audio src="/api/telegram/chat/${chat.id}/media/${m.id}" controls preload="metadata" style="max-width:220px;display:block;margin-bottom:4px;"></audio>`;
        } else if (mt === 'document') {
          html += `<a href="/api/telegram/chat/${chat.id}/media/${m.id}" download style="color:var(--accent);font-size:12px;">📎 Document</a><br>`;
        } else {
          const icons = { location: '📍', contact: '👤', poll: '📊', webpage: '🔗', other: '📎' };
          html += `<span style="opacity:0.6;font-size:12px;">${icons[mt] || '📎'} ${mt}</span><br>`;
        }
      }
      // Webpage preview
      if (m.webpage_url) {
        html += `<div style="border-left:2px solid var(--accent);padding-left:8px;margin:4px 0;font-size:12px;"><a href="${escAttr(m.webpage_url)}" target="_blank" style="color:var(--accent);">${escHtml(m.webpage_title || m.webpage_url)}</a>${m.webpage_desc ? '<br><span style="color:var(--muted);">' + escHtml(m.webpage_desc) + '</span>' : ''}</div>`;
      }
      // Text
      if (m.text) {
        html += escHtml(m.text).replace(/\n/g, '<br>');
      } else if (!m.has_media && !m.webpage_url) {
        html += '<em style="opacity:0.5;">[empty]</em>';
      }
      // Timestamp
      const time = m.date ? new Date(m.date).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '';
      html += `<div style="font-size:10px;color:var(--muted);margin-top:3px;text-align:right;">${time}</div>`;
      bubble.innerHTML = html;
      return bubble;
    }

    async function doTgSend(container, chat, input, msgArea) {
      const text = input.value.trim();
      if (!text) return;
      input.value = '';
      input.style.height = 'auto';
      // Optimistic local bubble
      const fakeMsg = { id: Date.now(), sender: '', text, date: new Date().toISOString(), is_out: true, media_type: null, has_media: false };
      msgArea.appendChild(buildTgBubble(fakeMsg, chat));
      msgArea.scrollTop = msgArea.scrollHeight;
      try {
        const res = await fetch('/api/telegram/send', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ chat_id: chat.id, text }),
        });
        if (!res.ok) {
          const d = await res.json();
          showToast(d.detail || 'Send failed', 'error');
        }
      } catch { showToast('Network error', 'error'); }
    }

    async function openLibraryReader(section, filename, title) {

      try {
        const res = await fetch(`/api/library/read/${section}/${encodeURIComponent(filename)}`);
        const data = await res.json();
        if (data.error) { showToast(data.error, "error"); return; }
        const htmlContent = renderMarkdownSimple(data.content);
        // Show in a simple modal overlay
        const existing = document.getElementById("libraryReaderOverlay");
        if (existing) existing.remove();
        const overlay = document.createElement("div");
        overlay.id = "libraryReaderOverlay";
        overlay.className = "settings-overlay";
        overlay.innerHTML = `
          <div class="settings-panel library-reader-panel">
            <div class="settings-header">
              <h2>${escHtml(title)}</h2>
              <div style="display:flex;gap:4px;">
                <button class="icon-btn" id="libraryReaderCopy" title="Copy content"><i data-lucide="copy" class="icon-lucide"></i></button>
                <button class="icon-btn" id="libraryReaderDock" title="Dock to sidebar"><i data-lucide="panel-right" class="icon-lucide"></i></button>
                <button class="icon-btn" id="libraryReaderClose"><span class="icon-emoji">✕</span><i data-lucide="x" class="icon-lucide"></i></button>
              </div>
            </div>
            <div class="library-reader-content">${htmlContent}</div>
          </div>
        `;
        document.body.appendChild(overlay);
        overlay.querySelector("#libraryReaderCopy").addEventListener("click", function() {
          const text = data.content || "";
          const btn = this;
          const originalHTML = btn.innerHTML;
          navigator.clipboard.writeText(text).then(
            () => {
              btn.innerHTML = '<i data-lucide="check" class="icon-lucide"></i>';
              btn.style.color = "#4ade80";
              if (window.lucide) lucide.createIcons({ nodes: [btn] });
              setTimeout(() => {
                btn.innerHTML = originalHTML;
                btn.style.color = "";
                if (window.lucide) lucide.createIcons({ nodes: [btn] });
              }, 2000);
            },
            () => {
              btn.innerHTML = '<i data-lucide="x" class="icon-lucide"></i>';
              btn.style.color = "#f87171";
              if (window.lucide) lucide.createIcons({ nodes: [btn] });
              setTimeout(() => {
                btn.innerHTML = originalHTML;
                btn.style.color = "";
                if (window.lucide) lucide.createIcons({ nodes: [btn] });
              }, 2000);
            }
          );
        });
        overlay.querySelector("#libraryReaderClose").addEventListener("click", () => overlay.remove());
        overlay.addEventListener("click", (e) => { if (e.target === overlay) overlay.remove(); });
        overlay.querySelector("#libraryReaderDock").addEventListener("click", () => {
          overlay.remove();
          dockLibraryToSidebar(title, htmlContent);
        });
        lucide.createIcons({ nodes: overlay.querySelectorAll("[data-lucide]") });
      } catch { showToast("Failed to load file", "error"); }
    }
    window.openLibraryReader = openLibraryReader;

    let _libReaderDocked = false;
    let _libReaderTempHidden = false;
    let _diffSidebarOriginalHTML = "";
    let _libReaderCurrentHTML = "";
    // Expose for filesystem.js Ctrl+B handler
    window._libReaderDocked = false;
    window._libReaderTempHidden = false;
    window._tempShowFileViewer = null;
    window._restoreLibReaderContent = null;

    function dockLibraryToSidebar(title, htmlContent) {
      const diffSidebar = document.getElementById("diffSidebar");
      if (!diffSidebar) return;

      if (!_libReaderDocked) {
        _diffSidebarOriginalHTML = diffSidebar.innerHTML;
      }

      _libReaderDocked = true;
      _libReaderTempHidden = false;
      window._libReaderDocked = true;
      window._libReaderTempHidden = false;
      _libReaderCurrentHTML = `
        <div class="diff-sidebar-header">
          <span class="diff-sidebar-title">${escHtml(title)}</span>
          <button class="new-chat-icon sidebar-close-icon" id="libReaderSidebarClose" title="Close"><span class="icon-emoji">✕</span><i data-lucide="x" class="icon-lucide"></i></button>
        </div>
        <div class="library-reader-content" style="flex:1;overflow-y:auto;padding:12px;">${htmlContent}</div>
      `;
      diffSidebar.innerHTML = _libReaderCurrentHTML;
      document.body.classList.add("diff-open");
      diffSidebar.querySelector("#libReaderSidebarClose").addEventListener("click", () => undockLibraryReader());
      lucide.createIcons({ nodes: diffSidebar.querySelectorAll("[data-lucide]") });
    }

    window._tempShowFileViewer = function _tempShowFileViewer() {
      const diffSidebar = document.getElementById("diffSidebar");
      if (!diffSidebar) return;
      _libReaderTempHidden = true;
      window._libReaderTempHidden = true;
      diffSidebar.innerHTML = _diffSidebarOriginalHTML;
      // Re-bind file viewer buttons
      const newCloseBtn = diffSidebar.querySelector("#diffClose");
      const newClearBtn = diffSidebar.querySelector("#diffClear");
      if (newCloseBtn) newCloseBtn.addEventListener("click", () => {
        _restoreLibReaderContent();
        document.body.classList.remove("diff-open");
      });
      if (newClearBtn) newClearBtn.addEventListener("click", () => { const dc = document.getElementById("diffCards"); if (dc) dc.innerHTML = ""; });
      const fsPill = diffSidebar.querySelector("#fsModePill");
      if (fsPill && typeof initFsModePill === "function") initFsModePill();
    }

    window._restoreLibReaderContent = function _restoreLibReaderContent() {
      const diffSidebar = document.getElementById("diffSidebar");
      if (!diffSidebar || !_libReaderDocked) return;
      _libReaderTempHidden = false;
      window._libReaderTempHidden = false;
      diffSidebar.innerHTML = _libReaderCurrentHTML;
      diffSidebar.querySelector("#libReaderSidebarClose").addEventListener("click", () => undockLibraryReader());
      lucide.createIcons({ nodes: diffSidebar.querySelectorAll("[data-lucide]") });
    }

    function undockLibraryReader(closeAfter = true) {
      const diffSidebar = document.getElementById("diffSidebar");
      if (!diffSidebar || !_libReaderDocked) return;

      diffSidebar.innerHTML = _diffSidebarOriginalHTML;
      _diffSidebarOriginalHTML = "";
      _libReaderDocked = false;
      _libReaderTempHidden = false;
      window._libReaderDocked = false;
      window._libReaderTempHidden = false;
      _libReaderCurrentHTML = "";

      const newCloseBtn = diffSidebar.querySelector("#diffClose");
      const newClearBtn = diffSidebar.querySelector("#diffClear");
      if (newCloseBtn) newCloseBtn.addEventListener("click", () => {
        if (_libReaderDocked) { undockLibraryReader(); } else { document.body.classList.remove("diff-open"); }
      });
      if (newClearBtn) newClearBtn.addEventListener("click", () => { const dc = document.getElementById("diffCards"); if (dc) dc.innerHTML = ""; });
      const fsPill = diffSidebar.querySelector("#fsModePill");
      if (fsPill && typeof initFsModePill === "function") initFsModePill();

      if (closeAfter) {
        document.body.classList.remove("diff-open");
      }
    }

    function renderMarkdownSimple(md) {
      // Use the full marked+DOMPurify pipeline for proper tables, code blocks, etc.
      return renderMarkdown(md);
    }

