/*
 * study-humanizer.js — Humanizer tab for Study mode.
 *
 * Two-phase pipeline, matching skills/text_humanizer/instruction.md:
 *   1. LLM rewrite  (persona-conditioned) — POST /api/study/humanize
 *   2. Rule pass    (burstiness / perplexity / n-gram) — server shells
 *                   skills/text_humanizer/scripts/humanize.py
 *
 * The user picks persona, model, thinking mode, rule strength, and can skip
 * the LLM entirely for a fast deterministic pass.
 *
 * Exposes window.studyHumanizer = { renderTab, renderSidebar, reload }.
 */
(function () {
  'use strict';

  const API = '/api/study/humanize';

  // ── State ────────────────────────────────────────────────────────────────
  let loaded = false;
  let busy = false;
  let MODELS = [];
  let PERSONAS = [];
  let cfg = {
    model: '', thinking_mode: '', persona: 'peer',
    strength: 0.6, seed: '',
  };
  let input = '';
  let output = '';
  let stats = null;         // { before, after } from the rule pass
  let phases = null;        // { llm, rule, model, persona, strength }
  let lastError = '';
  let advanced = false;     // advanced options panel open?

  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));

  function toast(msg) {
    if (window.studyMode && window.studyMode.toast) return window.studyMode.toast(msg);
    if (typeof window.showToast === 'function') return window.showToast(msg);
    console.log('[humanizer]', msg);
  }

  // ── API ──────────────────────────────────────────────────────────────────

  async function api(path, opts) {
    const res = await fetch(path, {
      ...(opts || {}),
      headers: {
        'Content-Type': 'application/json',
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

  async function reload() {
    try {
      const [m, p, c] = await Promise.all([
        api(`${API}/models`), api(`${API}/personas`), api(`${API}/config`),
      ]);
      MODELS = (m && m.models) || [];
      PERSONAS = (p && p.personas) || [];
      if (c) {
        cfg.model = c.model || cfg.model;
        cfg.thinking_mode = c.thinking_mode || '';
        cfg.persona = c.persona || 'peer';
        cfg.strength = c.strength != null && c.strength !== '' ? parseFloat(c.strength) : 0.6;
        cfg.seed = c.seed || '';
      }
    } catch (err) {
      toast('Could not load humanizer options: ' + err.message);
    }
    loaded = true;
    return true;
  }

  function currentModel() {
    return MODELS.find((m) => m.id === cfg.model) || MODELS[0] || null;
  }

  async function saveConfig(patch) {
    Object.assign(cfg, patch);
    try { await api(`${API}/config`, { method: 'PUT', body: JSON.stringify(cfg) }); }
    catch { /* non-fatal — the request still carries the values */ }
  }

  async function run(opts) {
    const text = (opts && opts.text != null ? opts.text : input).trim();
    if (!text) { toast('Paste some text first'); return; }
    if (busy) return;

    busy = true;
    lastError = '';
    stats = null;
    phases = null;
    refresh();

    try {
      const body = {
        text,
        persona: cfg.persona,
        model: cfg.model,
        thinking_mode: cfg.thinking_mode,
        strength: cfg.strength,
        seed: cfg.seed === '' ? null : cfg.seed,
        skip_llm: !!(opts && opts.skipLlm),
      };
      const res = await api(API, { method: 'POST', body: JSON.stringify(body) });
      output = (res && res.text) || '';
      stats = (res && res.stats) || null;
      phases = (res && res.phases) || null;
    } catch (err) {
      lastError = err.message || String(err);
      toast('Humanize failed: ' + lastError);
    } finally {
      busy = false;
      refresh();
    }
  }

  async function copyOutput() {
    if (!output) return;
    try {
      await navigator.clipboard.writeText(output);
      toast('Copied');
    } catch {
      toast('Copy failed — select it manually');
    }
  }

  function useOutputAsInput() {
    if (!output) return;
    input = output;
    output = '';
    stats = null;
    phases = null;
    refresh();
  }

  // ── Rendering ────────────────────────────────────────────────────────────

  function pctDelta(before, after) {
    if (before == null || after == null) return '';
    const d = after - before;
    const sign = d > 0 ? '+' : '';
    const cls = d > 0 ? 'up' : d < 0 ? 'down' : 'flat';
    return `<span class="hz-delta ${cls}">${sign}${(d).toFixed(3).replace(/0+$/, '').replace(/\.$/, '')}</span>`;
  }

  function statsBlock() {
    if (!stats) return '';
    const b = stats.before || {};
    const a = stats.after || {};
    const rows = [
      ['Burstiness', b.burstiness, a.burstiness],
      ['AI markers', b.ai_markers, a.ai_markers],
      ['Avg sentence len', b.avg_sentence_len, a.avg_sentence_len],
      ['Sentence stdev', b.sentence_len_stdev, a.sentence_len_stdev],
      ['Words', b.words, a.words],
    ];
    return `
      <div class="hz-stats">
        <div class="hz-stats-title"><i data-lucide="activity" class="icon-lucide"></i> Signal shift</div>
        <table class="hz-stats-table">
          <thead><tr><th></th><th>before</th><th>after</th><th></th></tr></thead>
          <tbody>
            ${rows.map(([label, bv, av]) => `
              <tr>
                <td>${esc(label)}</td>
                <td class="num">${bv == null ? '—' : esc(bv)}</td>
                <td class="num">${av == null ? '—' : esc(av)}</td>
                <td>${pctDelta(bv, av)}</td>
              </tr>`).join('')}
          </tbody>
        </table>
        ${a.burstiness != null ? `<div class="hz-note">${a.burstiness >= 0.5
          ? 'Burstiness is in the human range (>0.5).'
          : 'Burstiness is still low — try strength 0.8+ or run the LLM phase.'}</div>` : ''}
      </div>`;
  }

  function phaseChips() {
    if (!phases) return '';
    const chips = [];
    chips.push(phases.llm
      ? `<span class="hz-chip ok"><i data-lucide="check" class="icon-lucide"></i> LLM rewrite${phases.model ? ` · ${esc(phases.model)}` : ''}</span>`
      : `<span class="hz-chip skip"><i data-lucide="minus" class="icon-lucide"></i> LLM skipped</span>`);
    chips.push(phases.rule
      ? `<span class="hz-chip ok"><i data-lucide="check" class="icon-lucide"></i> Rule pass · strength ${esc(phases.strength)}</span>`
      : `<span class="hz-chip warn"><i data-lucide="alert-triangle" class="icon-lucide"></i> Rule pass unavailable</span>`);
    return `<div class="hz-chips">${chips.join('')}</div>`;
  }

  function advancedPanel() {
    if (!advanced) return '';
    const model = currentModel();
    const modes = (model && model.thinking_modes) || [];
    return `
      <div class="hz-advanced">
        <div class="hz-field">
          <label>Model</label>
          <select data-hz="model">
            ${MODELS.map((m) => `<option value="${esc(m.id)}"${m.id === cfg.model ? ' selected' : ''}>${esc(m.label || m.id)}</option>`).join('')}
          </select>
        </div>
        <div class="hz-field">
          <label>Thinking mode</label>
          <select data-hz="thinking_mode"${modes.length ? '' : ' disabled'}>
            <option value="">Default</option>
            ${modes.map((t) => `<option value="${esc(t.id)}"${t.id === cfg.thinking_mode ? ' selected' : ''}>${esc(t.label || t.id)}</option>`).join('')}
          </select>
        </div>
        <div class="hz-field">
          <label>Rule strength <span class="hz-val">${cfg.strength.toFixed(2)}</span></label>
          <input type="range" min="0" max="1" step="0.05" value="${cfg.strength}" data-hz="strength">
        </div>
        <div class="hz-field">
          <label>Seed <span class="hz-hint">(blank = random)</span></label>
          <input type="text" inputmode="numeric" placeholder="random" value="${esc(cfg.seed)}" data-hz="seed">
        </div>
      </div>`;
  }

  function renderTab() {
    if (!loaded) {
      reload().then(() => refresh());
      return '<div class="study-empty"><div class="study-empty-icon">🪄</div><h3>Loading humanizer…</h3></div>';
    }

    return `
      <div class="hz-pane">
        <div class="hz-head">
          <div class="hz-head-title">
            <i data-lucide="wand-sparkles" class="icon-lucide"></i>
            Humanizer
            <span class="hz-sub">rewrite + structural pass</span>
          </div>
          <div class="hz-personas">
            ${PERSONAS.map((p) => `<button class="hz-persona${p.id === cfg.persona ? ' active' : ''}" data-hz-persona="${esc(p.id)}" title="${esc(p.prompt || '')}">${esc(p.label)}</button>`).join('')}
          </div>
        </div>

        <div class="hz-grid">
          <div class="hz-col">
            <div class="hz-col-head">
              <span>Input</span>
              <span class="hz-count">${input.trim() ? input.trim().split(/\s+/).length + ' words' : ''}</span>
            </div>
            <textarea class="hz-textarea" id="hzInput" placeholder="Paste AI-generated text here…">${esc(input)}</textarea>
          </div>

          <div class="hz-col">
            <div class="hz-col-head">
              <span>Output</span>
              <span class="hz-count">${output ? output.trim().split(/\s+/).length + ' words' : ''}</span>
            </div>
            <div class="hz-output" id="hzOutput">${output ? esc(output) : `<span class="hz-placeholder">${lastError ? esc(lastError) : 'Humanized text appears here.'}</span>`}</div>
          </div>
        </div>

        ${phaseChips()}
        ${statsBlock()}
        ${advancedPanel()}

        <div class="hz-actions">
          <button class="hz-btn primary" data-hz-action="run"${busy ? ' disabled' : ''}>
            <i data-lucide="${busy ? 'loader-circle' : 'wand-sparkles'}" class="icon-lucide${busy ? ' spin' : ''}"></i>
            ${busy ? 'Humanizing…' : 'Humanize'}
          </button>
          <button class="hz-btn" data-hz-action="run-rule"${busy ? ' disabled' : ''} title="Skip the LLM — rule pass only (fast, deterministic)">
            <i data-lucide="zap" class="icon-lucide"></i> Rule only
          </button>
          <button class="hz-btn" data-hz-action="copy"${output ? '' : ' disabled'}>
            <i data-lucide="copy" class="icon-lucide"></i> Copy
          </button>
          <button class="hz-btn" data-hz-action="reuse"${output ? '' : ' disabled'} title="Feed the output back in as input">
            <i data-lucide="repeat" class="icon-lucide"></i> Reuse as input
          </button>
          <button class="hz-btn ghost" data-hz-action="clear"${input || output ? '' : ' disabled'}>
            <i data-lucide="eraser" class="icon-lucide"></i> Clear
          </button>
          <button class="hz-btn ghost" data-hz-action="advanced">
            <i data-lucide="${advanced ? 'chevron-up' : 'settings-2'}" class="icon-lucide"></i> ${advanced ? 'Hide options' : 'Options'}
          </button>
        </div>
      </div>`;
  }

  function renderSidebar() {
    if (!loaded) {
      return `<div class="study-sidebar-section"><div class="study-sidebar-label">Humanizer</div>
        <div class="hz-side-hint">Loading…</div></div>`;
    }
    const burst = stats && stats.after ? stats.after.burstiness : null;
    return `
      <div class="study-sidebar-section">
        <div class="study-sidebar-label">Persona</div>
        ${PERSONAS.map((p) => `
          <button class="study-sidebar-item${p.id === cfg.persona ? ' active' : ''}" data-hz-persona="${esc(p.id)}">
            <i data-lucide="user-round" class="icon-lucide"></i> ${esc(p.label)}
          </button>`).join('')}
      </div>
      <div class="study-sidebar-section">
        <div class="study-sidebar-label">Run</div>
        <button class="study-sidebar-item" data-hz-action="run"><i data-lucide="wand-sparkles" class="icon-lucide"></i> Humanize</button>
        <button class="study-sidebar-item" data-hz-action="run-rule"><i data-lucide="zap" class="icon-lucide"></i> Rule pass only</button>
      </div>
      ${burst != null ? `
      <div class="study-sidebar-section">
        <div class="study-sidebar-label">Last run</div>
        <div class="hz-side-hint">Burstiness <strong>${esc(burst)}</strong><br>
          ${burst >= 0.5 ? 'In the human range.' : 'Still flat — raise strength.'}</div>
      </div>` : ''}
      <div class="study-sidebar-section">
        <div class="study-sidebar-label">Options</div>
        <button class="study-sidebar-item" data-hz-action="advanced"><i data-lucide="settings-2" class="icon-lucide"></i> ${advanced ? 'Hide' : 'Show'} model &amp; strength</button>
      </div>`;
  }

  /** Repaint the tab + sidebar without a full study.js render cycle. */
  function refresh() {
    if (!(window.studyMode && window.studyMode.getTab() === 'humanizer')) return;
    const pane = document.querySelector('.hz-pane');
    const host = pane ? pane.parentElement : null;
    if (host) {
      // Preserve focus + caret in the textarea across the repaint.
      const ta = document.getElementById('hzInput');
      const sel0 = ta ? ta.selectionStart : null;
      const sel1 = ta ? ta.selectionEnd : null;
      host.innerHTML = renderTab();
      const next = document.getElementById('hzInput');
      if (next && sel0 != null) {
        next.focus();
        try { next.setSelectionRange(sel0, sel1); } catch { /* noop */ }
      }
    }
    if (window.lucide) window.lucide.createIcons();
    if (window.studyMode && window.studyMode.refreshSidebar) window.studyMode.refreshSidebar();
  }

  // ── Events ───────────────────────────────────────────────────────────────

  // Typing in the textarea — bound at document level so repaints don't drop it.
  document.addEventListener('input', (e) => {
    if (!e.target || e.target.id !== 'hzInput') return;
    input = e.target.value;
    // Update the word count without repainting (which would fight the caret).
    const head = e.target.closest('.hz-col')?.querySelector('.hz-count');
    if (head) head.textContent = input.trim() ? input.trim().split(/\s+/).length + ' words' : '';
  });

  document.addEventListener('change', (e) => {
    const el = e.target.closest('[data-hz]');
    if (!el) return;
    const key = el.dataset.hz;
    if (key === 'model') {
      saveConfig({ model: el.value, thinking_mode: '' });
      refresh();
    } else if (key === 'thinking_mode') {
      saveConfig({ thinking_mode: el.value });
    } else if (key === 'strength') {
      saveConfig({ strength: parseFloat(el.value) });
      refresh();
    } else if (key === 'seed') {
      saveConfig({ seed: el.value.trim() });
    }
  });

  document.addEventListener('input', (e) => {
    const el = e.target.closest('[data-hz="strength"]');
    if (!el) return;
    cfg.strength = parseFloat(el.value);
    const val = el.closest('.hz-field')?.querySelector('.hz-val');
    if (val) val.textContent = cfg.strength.toFixed(2);
  });

  document.addEventListener('click', (e) => {
    const personaEl = e.target.closest('[data-hz-persona]');
    if (personaEl) {
      saveConfig({ persona: personaEl.dataset.hzPersona });
      refresh();
      return;
    }
    const actionEl = e.target.closest('[data-hz-action]');
    if (!actionEl) return;
    const a = actionEl.dataset.hzAction;
    if (a === 'run') run();
    else if (a === 'run-rule') run({ skipLlm: true });
    else if (a === 'copy') copyOutput();
    else if (a === 'reuse') useOutputAsInput();
    else if (a === 'clear') { input = ''; output = ''; stats = null; phases = null; lastError = ''; refresh(); }
    else if (a === 'advanced') { advanced = !advanced; refresh(); }
  });

  window.studyHumanizer = {
    renderTab,
    renderSidebar,
    reload,
    isLoaded: () => loaded,
  };
})();
