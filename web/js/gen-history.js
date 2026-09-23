/* gen-history.js — shared prompt/query history for Image Gen, Research & Prompt Gen.
 *
 * Sidebar widgets render this history; the main window shows the *result*.
 * Each entry: { text, meta, ts }. `meta` carries whatever the panel needs to
 * re-display a past result (image result object, research session id, output).
 *
 * Exposes: window.genHistory = { push, update, list, clear, renderInto }
 * Emits:   'gen-history-change' { detail: { kind } }
 */
(function () {
  'use strict';

  var KEY = 'sable_gen_history';
  var MAX = 40;
  var KINDS = ['image', 'research', 'prompt'];

  function _esc(s) {
    var d = document.createElement('div');
    d.textContent = s == null ? '' : s;
    return d.innerHTML;
  }

  function _all() {
    try { return JSON.parse(localStorage.getItem(KEY)) || {}; } catch (e) { return {}; }
  }
  function _save(all) {
    try { localStorage.setItem(KEY, JSON.stringify(all)); } catch (e) { /* quota */ }
  }

  function push(kind, text, meta) {
    text = (text || '').trim();
    if (!KINDS.includes(kind) || !text) return;
    var all = _all();
    var list = all[kind] || [];
    // Dedupe by text — re-running a prompt bumps it to the top.
    var filtered = list.filter(function (x) { return x.text !== text; });
    filtered.unshift({ text: text, meta: meta || {}, ts: Date.now() });
    all[kind] = filtered.slice(0, MAX);
    _save(all);
    window.dispatchEvent(new CustomEvent('gen-history-change', { detail: { kind: kind } }));
  }

  // Merge extra meta into the most recent entry matching `text`
  // (used to attach results after an async generation finishes).
  function update(kind, text, patch) {
    text = (text || '').trim();
    var all = _all();
    var list = all[kind] || [];
    var hit = null;
    for (var i = 0; i < list.length; i++) {
      if (list[i].text === text) { hit = list[i]; break; }
    }
    if (!hit) return;
    hit.meta = Object.assign({}, hit.meta || {}, patch || {});
    _save(all);
    window.dispatchEvent(new CustomEvent('gen-history-change', { detail: { kind: kind } }));
  }

  function list(kind) {
    var all = _all();
    return all[kind] || [];
  }

  function clear(kind) {
    var all = _all();
    all[kind] = [];
    _save(all);
    window.dispatchEvent(new CustomEvent('gen-history-change', { detail: { kind: kind } }));
  }

  function _when(ts) {
    try {
      return new Date(ts).toLocaleString(undefined, {
        month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
      });
    } catch (e) { return ''; }
  }

  /** Render the history list for `kind` into `container`. onPick(item) on click. */
  function renderInto(kind, container, onPick) {
    if (!container) return;
    var items = list(kind);
    if (!items.length) {
      container.innerHTML = '<div class="tracknote-empty">No history yet.<br>Your prompts will appear here.</div>';
      return;
    }
    container.innerHTML = '';
    var listEl = document.createElement('div');
    listEl.className = 'tn-list';
    items.forEach(function (item) {
      var card = document.createElement('div');
      card.className = 'tn-item tn-note-card gen-history-item';
      var title = item.text.length > 96 ? item.text.slice(0, 95) + '\u2026' : item.text;
      var hasResult = !!(item.meta && (item.meta.result || item.meta.sid || item.meta.output));
      card.innerHTML =
        '<div class="tn-item-title">' + _esc(title) + '</div>' +
        '<div class="tn-item-meta">' + _esc(_when(item.ts)) +
          (hasResult ? ' \u00b7 <span class="gen-history-has-result">result saved</span>' : '') +
        '</div>';
      card.addEventListener('click', function () { if (onPick) onPick(item); });
      listEl.appendChild(card);
    });
    container.appendChild(listEl);
  }

  window.genHistory = {
    push: push,
    update: update,
    list: list,
    clear: clear,
    renderInto: renderInto,
  };
})();
