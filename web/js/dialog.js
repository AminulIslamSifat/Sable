/**
 * dialog.js — Sable's own dialog system, replacing native browser prompts.
 *
 * Native window.prompt/confirm/alert are blocking, unstyled OS chrome that
 * looks nothing like the rest of the shell. This module gives every dialog the
 * same look as the dashboard modals (var(--panel), --border-soft, --accent)
 * and the same keyboard behaviour.
 *
 * All functions return Promises, so call sites read naturally with async/await:
 *
 *     const name = await sableDialog.prompt({title: 'Deck name'});
 *     if (!name) return;                       // null = cancelled
 *
 *     if (!await sableDialog.confirm({title: 'Delete deck?', danger: true})) return;
 *
 *     await sableDialog.alert({title: 'Upload failed', message: err.message});
 *
 *     const pick = await sableDialog.pick({
 *       title: 'Open which document?',
 *       options: docs.map(d => ({label: d.title, value: d.id, sub: d.pages + ' pages'})),
 *     });
 *
 *     const vals = await sableDialog.form({
 *       title: 'New card',
 *       fields: [{key:'front', label:'Front'}, {key:'back', label:'Back', type:'textarea'}],
 *     });
 *
 * Escape cancels, Enter submits. Focus is trapped in the dialog and restored on
 * close, and the overlay is the only interactive surface while open.
 *
 * Exposes window.sableDialog = {prompt, confirm, alert, pick, form, isOpen}.
 */
(function () {
  'use strict';

  let openStack = [];   // supports a dialog opening on top of another

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  /**
   * Build and show one dialog.
   *
   * opts:
   *   title       heading text
   *   message     body paragraph (escaped)
   *   bodyHtml    raw HTML body (caller must escape their own data)
   *   fields      [{key, label, value, placeholder, type, rows, autofocus}]
   *   options     [{label, value, sub, icon}]  — renders a selectable list
   *   okText      confirm button label (default 'OK')
   *   cancelText  cancel button label (default 'Cancel')
   *   danger      red confirm button
   *   width       max width in px (default 440)
   *
   * Returns a Promise resolving to:
   *   string  (prompt)  · true/false (confirm)  · null (alert)
   *   value   (pick)    · object (form)
   */
  function open(opts) {
    const o = opts || {};
    return new Promise((resolve) => {
      const previouslyFocused = document.activeElement;

      const overlay = document.createElement('div');
      overlay.className = 'sdlg-overlay';

      const fields = o.fields || [];
      const hasFields = fields.length > 0;
      const options = o.options || [];
      const hasOptions = options.length > 0;

      const fieldHtml = fields.map((f) => {
        const id = 'sdlg-f-' + Math.random().toString(36).slice(2, 9);
        f._id = id;
        const common = `id="${id}" class="sdlg-input"`;
        if (f.type === 'textarea') {
          return `<label class="sdlg-field">
            <span class="sdlg-label">${esc(f.label || '')}</span>
            <textarea ${common} rows="${f.rows || 3}" placeholder="${esc(f.placeholder || '')}">${esc(f.value || '')}</textarea>
          </label>`;
        }
        return `<label class="sdlg-field">
          <span class="sdlg-label">${esc(f.label || '')}</span>
          <input ${common} type="${f.type || 'text'}" value="${esc(f.value || '')}" placeholder="${esc(f.placeholder || '')}" autocomplete="off">
        </label>`;
      }).join('');

      const optionHtml = options.map((opt, i) => `
        <button class="sdlg-option" data-idx="${i}" type="button">
          <span class="sdlg-option-main">
            <span class="sdlg-option-label">${esc(opt.label || '')}</span>
            ${opt.sub ? `<span class="sdlg-option-sub">${esc(opt.sub)}</span>` : ''}
          </span>
          ${opt.badge ? `<span class="sdlg-option-badge">${esc(opt.badge)}</span>` : ''}
        </button>`).join('');

      const bodyHtml = hasFields ? fieldHtml
        : hasOptions ? `<div class="sdlg-options">${optionHtml}</div>`
        : o.bodyHtml ? o.bodyHtml
        : o.message ? `<p class="sdlg-message">${esc(o.message)}</p>`
        : '';

      overlay.innerHTML = `
        <div class="sdlg" role="dialog" aria-modal="true" style="max-width:${o.width || 440}px">
          <div class="sdlg-head">
            <span class="sdlg-title">${esc(o.title || '')}</span>
            <button class="sdlg-close" type="button" aria-label="Close">
              <i data-lucide="x" class="icon-lucide"></i>
            </button>
          </div>
          ${bodyHtml ? `<div class="sdlg-body">${bodyHtml}</div>` : ''}
          <div class="sdlg-foot">
            <button class="sdlg-btn" data-act="cancel" type="button">${esc(o.cancelText || 'Cancel')}</button>
            <button class="sdlg-btn primary${o.danger ? ' danger' : ''}" data-act="ok" type="button">${esc(o.okText || 'OK')}</button>
          </div>
        </div>`;

      document.body.appendChild(overlay);
      if (window.lucide) window.lucide.createIcons({ nodes: [overlay] });

      const box = overlay.querySelector('.sdlg');
      const okBtn = overlay.querySelector('[data-act="ok"]');
      const cancelBtn = overlay.querySelector('[data-act="cancel"]');
      const closeBtn = overlay.querySelector('.sdlg-close');

      openStack.push(overlay);

      // ── close ──
      let settled = false;
      function finish(value) {
        if (settled) return;
        settled = true;
        document.removeEventListener('keydown', onKey, true);
        overlay.remove();
        openStack = openStack.filter((el) => el !== overlay);
        try { if (previouslyFocused && previouslyFocused.focus) previouslyFocused.focus(); } catch {}
        resolve(value);
      }

      function submit() {
        if (hasFields) {
          const out = {};
          fields.forEach((f) => {
            const el = overlay.querySelector('#' + f._id);
            out[f.key] = el ? el.value.trim() : '';
          });
          finish(out);
        } else if (hasOptions) {
          const sel = overlay.querySelector('.sdlg-option.selected');
          if (sel) finish(options[parseInt(sel.dataset.idx, 10)].value);
          else finish(null);
        } else if (o.mode === 'confirm') {
          finish(true);
        } else if (o.mode === 'prompt') {
          const input = overlay.querySelector('.sdlg-input');
          finish(input ? input.value.trim() : '');
        } else {
          finish(null);
        }
      }

      // ── keyboard ──
      function onKey(e) {
        // Only the topmost dialog reacts.
        if (openStack[openStack.length - 1] !== overlay) return;
        if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); finish(null); return; }
        if (e.key === 'Enter') {
          const tag = (e.target.tagName || '').toLowerCase();
          // Enter in a textarea inserts a newline; Ctrl/Cmd+Enter submits.
          if (tag === 'textarea' && !(e.ctrlKey || e.metaKey)) return;
          if (hasOptions && !e.target.classList.contains('sdlg-option')) return;
          e.preventDefault();
          e.stopPropagation();
          submit();
        }
        if (e.key === 'Tab') {
          // Trap focus inside the dialog.
          const focusables = box.querySelectorAll(
            'button, input, textarea, select, [tabindex]:not([tabindex="-1"])'
          );
          if (!focusables.length) return;
          const first = focusables[0];
          const last = focusables[focusables.length - 1];
          if (e.shiftKey && document.activeElement === first) {
            e.preventDefault(); last.focus();
          } else if (!e.shiftKey && document.activeElement === last) {
            e.preventDefault(); first.focus();
          }
        }
      }
      document.addEventListener('keydown', onKey, true);

      // ── clicks ──
      okBtn.addEventListener('click', submit);
      cancelBtn.addEventListener('click', () => finish(null));
      closeBtn.addEventListener('click', () => finish(null));
      overlay.addEventListener('mousedown', (e) => {
        if (e.target === overlay) finish(null);   // click the backdrop
      });

      if (hasOptions) {
        overlay.querySelectorAll('.sdlg-option').forEach((btn) => {
          btn.addEventListener('click', () => {
            overlay.querySelectorAll('.sdlg-option').forEach((b) =>
              b.classList.toggle('selected', b === btn));
            finish(options[parseInt(btn.dataset.idx, 10)].value);
          });
          btn.addEventListener('mouseenter', () => {
            overlay.querySelectorAll('.sdlg-option').forEach((b) =>
              b.classList.toggle('selected', b === btn));
          });
        });
      }

      // ── focus ──
      requestAnimationFrame(() => {
        const target = overlay.querySelector('.sdlg-input')
          || overlay.querySelector('.sdlg-option')
          || okBtn;
        if (target) {
          target.focus();
          if (target.select && target.tagName === 'INPUT') target.select();
        }
      });
    });
  }

  // ── public API ───────────────────────────────────────────────────────────

  function prompt(opts) {
    const o = typeof opts === 'string' ? { title: opts } : (opts || {});
    return open({
      title: o.title || 'Enter a value',
      message: o.message,
      mode: 'prompt',
      okText: o.okText || 'Save',
      width: o.width,
      fields: [{
        key: 'value',
        label: o.label || '',
        value: o.value || '',
        placeholder: o.placeholder || '',
        type: o.type || 'text',
      }],
    }).then((res) => (res && typeof res === 'object' ? res.value : res));
  }

  function confirm(opts) {
    const o = typeof opts === 'string' ? { title: opts } : (opts || {});
    return open({
      title: o.title || 'Are you sure?',
      message: o.message,
      mode: 'confirm',
      okText: o.okText || 'Confirm',
      cancelText: o.cancelText || 'Cancel',
      danger: o.danger !== false,
      width: o.width,
    });
  }

  function alert(opts) {
    const o = typeof opts === 'string' ? { title: opts } : (opts || {});
    return open({
      title: o.title || 'Notice',
      message: o.message,
      mode: 'alert',
      okText: o.okText || 'OK',
      cancelText: o.cancelText || '',
      width: o.width,
    });
  }

  function pick(opts) {
    const o = opts || {};
    return open({
      title: o.title || 'Choose one',
      options: o.options || [],
      mode: 'pick',
      okText: o.okText || 'Open',
      width: o.width || 480,
    });
  }

  function form(opts) {
    const o = opts || {};
    return open({
      title: o.title || 'Edit',
      fields: o.fields || [],
      mode: 'form',
      okText: o.okText || 'Save',
      width: o.width,
    });
  }

  window.sableDialog = {
    prompt,
    confirm,
    alert,
    pick,
    form,
    isOpen: () => openStack.length > 0,
  };
})();
