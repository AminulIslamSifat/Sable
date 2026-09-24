/**
 * study-timer.js — Timer / Stopwatch / Alarm widget for Study mode.
 *
 * One shared state, many mount points (study dashboard header, document reader
 * header). Idle shows a small clock pill; when something runs, only the active
 * mode's compact display shows. Alarms fire a popup.
 *
 * State is persisted to localStorage using absolute timestamps, so a running
 * timer/alarm keeps counting across a page reload.
 *
 * Exposes window.studyTimer = { mount }.
 */
(function () {
  'use strict';

  const LS_KEY = 'sable_study_timer_state';
  const TICK_MS = 250;

  // -- State ------------------------------------------------------------------
  // mode: null | 'timer' | 'stopwatch' | 'alarm'
  // timer:     { running, endAt, remaining, duration }   (ms)
  // stopwatch: { running, startedAt, elapsed }            (ms)
  // alarm:     { at, active, ringing }                    (at = "HH:MM")
  let state = load() || {
    timer: { running: false, endAt: 0, remaining: 0, duration: 0 },
    stopwatch: { running: false, startedAt: 0, elapsed: 0 },
    alarm: { at: '', active: false, ringing: false },
  };
  let ticker = null;
  const views = new Set();

  function load() {
    try {
      const raw = localStorage.getItem(LS_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch { return null; }
  }
  function save() {
    try { localStorage.setItem(LS_KEY, JSON.stringify(state)); } catch {}
  }

  // -- Time helpers -----------------------------------------------------------
  function pad(n) { return String(n).padStart(2, '0'); }

  /** ms → "HH:MM:SS" (or "MM:SS" when under an hour). */
  function fmt(ms) {
    ms = Math.max(0, Math.floor(ms / 1000));
    const h = Math.floor(ms / 3600);
    const m = Math.floor((ms % 3600) / 60);
    const s = ms % 60;
    return h ? `${pad(h)}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
  }

  /** Remaining ms on the countdown timer right now. */
  function timerRemaining() {
    const t = state.timer;
    if (t.running && t.endAt) return Math.max(0, t.endAt - Date.now());
    return Math.max(0, t.remaining || 0);
  }

  /** Elapsed ms on the stopwatch right now. */
  function stopwatchElapsed() {
    const s = state.stopwatch;
    if (s.running && s.startedAt) return (s.elapsed || 0) + (Date.now() - s.startedAt);
    return s.elapsed || 0;
  }

  /** Which mode owns the compact pill? Ringing alarm > timer > stopwatch. */
  function activeMode() {
    if (state.alarm.ringing) return 'alarm';
    const t = state.timer;
    if (t.running || (t.remaining > 0 && t.duration > 0)) return 'timer';
    const s = state.stopwatch;
    if (s.running || (s.elapsed > 0)) return 'stopwatch';
    if (state.alarm.active) return 'alarm';
    return null;
  }

  // -- Actions ----------------------------------------------------------------
  function startTimer(minutes) {
    const ms = Math.max(1, Math.round(minutes)) * 60 * 1000;
    state.timer = { running: true, endAt: Date.now() + ms, remaining: ms, duration: ms };
    state.alarm.ringing = false;
    save(); bump();
  }
  function pauseTimer() {
    const t = state.timer;
    if (!t.running) return;
    t.remaining = timerRemaining();
    t.running = false;
    t.endAt = 0;
    save(); bump();
  }
  function resumeTimer() {
    const t = state.timer;
    if (t.running || t.remaining <= 0) return;
    t.endAt = Date.now() + t.remaining;
    t.running = true;
    save(); bump();
  }
  function resetTimer() {
    state.timer = { running: false, endAt: 0, remaining: 0, duration: 0 };
    save(); bump();
  }

  function toggleStopwatch() {
    const s = state.stopwatch;
    if (s.running) {
      s.elapsed = stopwatchElapsed();
      s.running = false;
      s.startedAt = 0;
    } else {
      s.startedAt = Date.now();
      s.running = true;
    }
    save(); bump();
  }
  function resetStopwatch() {
    state.stopwatch = { running: false, startedAt: 0, elapsed: 0 };
    save(); bump();
  }

  function setAlarm(hhmm) {
    if (!/^\d{1,2}:\d{2}$/.test(hhmm)) return;
    state.alarm = { at: hhmm, active: true, ringing: false };
    save(); bump();
  }
  function clearAlarm() {
    state.alarm = { at: '', active: false, ringing: false };
    save(); bump();
  }
  function dismissAlarm() {
    state.alarm.ringing = false;
    state.alarm.active = false;
    state.alarm.at = '';
    save(); bump();
  }

  /** Fire an alarm when its wall-clock time arrives. */
  function checkAlarm() {
    const a = state.alarm;
    if (!a.active || a.ringing || !a.at) return;
    const now = new Date();
    const [h, m] = a.at.split(':').map((x) => parseInt(x, 10));
    if (now.getHours() === h && now.getMinutes() === m) {
      a.ringing = true;
      save();
      showAlarmPopup();
    }
  }

  // -- Ticker -----------------------------------------------------------------
  function tick() {
    checkAlarm();
    // Countdown finished → ring + reset to a clean state.
    const t = state.timer;
    if (t.running && timerRemaining() <= 0) {
      t.running = false;
      t.endAt = 0;
      t.remaining = 0;
      t.duration = 0;
      save();
      showAlarmPopup('Timer finished');
    }
    renderAll();
  }
  function ensureTicker() {
    if (ticker) return;
    ticker = setInterval(tick, TICK_MS);
  }
  function bump() { renderAll(); }

  // -- Alarm popup ------------------------------------------------------------
  let alarmPopup = null;
  function showAlarmPopup(title) {
    if (alarmPopup) alarmPopup.remove();
    alarmPopup = document.createElement('div');
    alarmPopup.className = 'st-alarm-popup';
    alarmPopup.innerHTML =
      '<div class="st-alarm-card">' +
        '<div class="st-alarm-ring"><i data-lucide="alarm-clock" class="icon-lucide"></i></div>' +
        '<div class="st-alarm-title">' + (title || 'Alarm') + '</div>' +
        '<div class="st-alarm-time">' + (state.alarm.at || '') + '</div>' +
        '<button class="st-alarm-dismiss" data-st="dismiss">Dismiss</button>' +
      '</div>';
    document.body.appendChild(alarmPopup);
    if (window.lucide) window.lucide.createIcons({ nodes: alarmPopup.querySelectorAll('[data-lucide]') });
    alarmPopup.addEventListener('click', (e) => {
      if (e.target.closest('[data-st="dismiss"]')) {
        dismissAlarm();
        alarmPopup.remove();
        alarmPopup = null;
      }
    });
  }

  // -- Per-mount rendering ----------------------------------------------------
  function renderAll() {
    views.forEach((v) => renderView(v));
  }

  function pillText() {
    const m = activeMode();
    if (m === 'timer') return fmt(timerRemaining());
    if (m === 'stopwatch') return fmt(stopwatchElapsed());
    if (m === 'alarm') return state.alarm.at || 'Alarm';
    return 'Timer';
  }

  function renderView(v) {
    const m = activeMode();
    v.pill.classList.toggle('active', !!m);
    v.pill.classList.toggle('ringing', state.alarm.ringing);

    // Patch in place — never replace innerHTML on a tick. Rebuilding the pill's
    // children every 250ms destroys the node between mousedown and mouseup,
    // which silently eats click events (the button "sometimes" works).
    const icon = m === 'stopwatch' ? 'timer' : m === 'alarm' ? 'alarm-clock' : 'clock';
    if (v._icon !== icon && v.iconWrap) {
      v._icon = icon;
      v.iconWrap.innerHTML = '<i data-lucide="' + icon + '" class="icon-lucide"></i>';
      if (window.lucide) window.lucide.createIcons({ nodes: [v.iconWrap] });
    }
    const txt = pillText();
    if (v.textEl && v.textEl.textContent !== txt) v.textEl.textContent = txt;

    if (v.panel && !v.panel.classList.contains('hidden')) renderPanel(v);
  }

  /**
   * The panel has two layers:
   *   - structure (tab layout, button labels) — rebuilt only when it changes
   *   - live values (the big time) — patched in place on every tick
   *
   * Rebuilding the whole panel every 250ms destroyed buttons mid-click and
   * wiped the alarm <input> while the user was typing. The signature guards
   * against that: same structure → only the numbers update.
   */
  function panelSignature(v) {
    const t = state.timer, s = state.stopwatch, a = state.alarm;
    const timerState = t.running ? 'run' : (t.remaining > 0 ? 'pause' : 'idle');
    const swState = s.running ? 'run' : (s.elapsed > 0 ? 'pause' : 'idle');
    return [v.tab, timerState, swState, a.active ? 'on' : 'off'].join('|');
  }

  function renderPanel(v) {
    const sig = panelSignature(v);
    if (v._panelSig === sig) { updatePanelValues(v); return; }
    v._panelSig = sig;

    const panel = v.panel;
    const t = state.timer, s = state.stopwatch, a = state.alarm;
    panel.innerHTML =
      '<div class="st-panel-head">' +
        '<span>Timer</span>' +
        '<button class="st-x" data-st="close"><i data-lucide="x" class="icon-lucide"></i></button>' +
      '</div>' +
      '<div class="st-tabs">' +
        '<button class="st-tab' + (v.tab === 'timer' ? ' active' : '') + '" data-st="tab" data-mode="timer">Timer</button>' +
        '<button class="st-tab' + (v.tab === 'stopwatch' ? ' active' : '') + '" data-st="tab" data-mode="stopwatch">Stopwatch</button>' +
        '<button class="st-tab' + (v.tab === 'alarm' ? ' active' : '') + '" data-st="tab" data-mode="alarm">Alarm</button>' +
      '</div>' +
      (v.tab === 'stopwatch'
        ? '<div class="st-body">' +
            '<div class="st-big" data-st-big>00:00</div>' +
            '<div class="st-row">' +
              '<button class="st-btn primary" data-st="sw-toggle">' + (s.running ? 'Pause' : (s.elapsed ? 'Resume' : 'Start')) + '</button>' +
              '<button class="st-btn" data-st="sw-reset">Reset</button>' +
            '</div>' +
          '</div>'
        : v.tab === 'alarm'
        ? '<div class="st-body">' +
            '<div class="st-big small" data-st-big>' + (a.active ? a.at : '--:--') + '</div>' +
            '<input class="st-input" type="time" data-st="alarm-input" value="' + (a.at || '') + '">' +
            '<div class="st-row">' +
              '<button class="st-btn primary" data-st="alarm-set">Set alarm</button>' +
              '<button class="st-btn" data-st="alarm-clear">Clear</button>' +
            '</div>' +
          '</div>'
        : '<div class="st-body">' +
            '<div class="st-big" data-st-big>00:00</div>' +
            '<div class="st-presets">' +
              [5, 10, 15, 25, 45].map((n) => '<button class="st-preset" data-st="preset" data-min="' + n + '">' + n + 'm</button>').join('') +
            '</div>' +
            '<div class="st-row">' +
              (t.running
                ? '<button class="st-btn primary" data-st="timer-pause">Pause</button>'
                : '<button class="st-btn primary" data-st="timer-start">' + (t.remaining ? 'Resume' : 'Start 25m') + '</button>') +
              '<button class="st-btn" data-st="timer-reset">Reset</button>' +
            '</div>' +
          '</div>');
    if (window.lucide) window.lucide.createIcons({ nodes: panel.querySelectorAll('[data-lucide]') });
    updatePanelValues(v);
  }

  /** Patch only the live time readout — safe to call every tick. */
  function updatePanelValues(v) {
    const big = v.panel.querySelector('[data-st-big]');
    if (!big) return;
    if (v.tab === 'stopwatch') {
      const val = fmt(stopwatchElapsed());
      if (big.textContent !== val) big.textContent = val;
    } else if (v.tab === 'timer') {
      const val = fmt(timerRemaining());
      if (big.textContent !== val) big.textContent = val;
    }
    // Alarm tab: its display is static (the set time), no per-tick update.
  }

  // -- Mount ------------------------------------------------------------------
  function mount(host) {
    if (!host) return null;
    const wrap = document.createElement('div');
    wrap.className = 'st-wrap';
    wrap.innerHTML =
      '<button class="st-pill" data-st="pill" title="Timer / Stopwatch / Alarm">' +
        '<span class="st-pill-icon" data-st-icon><i data-lucide="clock" class="icon-lucide"></i></span>' +
        '<span class="st-pill-text">Timer</span>' +
      '</button>' +
      '<div class="st-panel hidden" data-st="panel"></div>';
    host.appendChild(wrap);

    const v = {
      wrap,
      pill: wrap.querySelector('[data-st="pill"]'),
      iconWrap: wrap.querySelector('[data-st-icon]'),
      textEl: wrap.querySelector('.st-pill-text'),
      panel: wrap.querySelector('[data-st="panel"]'),
      tab: 'timer',
      _icon: 'clock',
      _panelSig: '',
    };
    views.add(v);
    ensureTicker();
    renderView(v);

    // Pick the sensible default tab based on what is currently active.
    const m = activeMode();
    if (m) v.tab = m;

    v.pill.addEventListener('click', (e) => {
      e.stopPropagation();
      const opening = v.panel.classList.contains('hidden');
      // Close any other open panels first — only one at a time.
      views.forEach((o) => { if (o !== v) o.panel.classList.add('hidden'); });
      v.panel.classList.toggle('hidden', !opening);
      if (opening) renderPanel(v);
    });

    v.panel.addEventListener('click', (e) => {
      e.stopPropagation();
      const el = e.target.closest('[data-st]');
      if (!el) return;
      const act = el.dataset.st;
      if (act === 'close') { v.panel.classList.add('hidden'); return; }
      if (act === 'tab') { v.tab = el.dataset.mode; renderPanel(v); return; }
      if (act === 'preset') { startTimer(parseInt(el.dataset.min, 10)); v.tab = 'timer'; return; }
      if (act === 'timer-start') { resumeTimer(); if (!state.timer.running) startTimer(25); return; }
      if (act === 'timer-pause') { pauseTimer(); return; }
      if (act === 'timer-reset') { resetTimer(); return; }
      if (act === 'sw-toggle') { toggleStopwatch(); return; }
      if (act === 'sw-reset') { resetStopwatch(); return; }
      if (act === 'alarm-set') {
        const inp = v.panel.querySelector('[data-st="alarm-input"]');
        if (inp && inp.value) setAlarm(inp.value);
        return;
      }
      if (act === 'alarm-clear') { clearAlarm(); return; }
    });

    // Outside click closes the panel (capture so it beats other handlers).
    document.addEventListener('mousedown', (e) => {
      if (!wrap.contains(e.target)) v.panel.classList.add('hidden');
    }, true);

    return { destroy: () => views.delete(v), el: wrap };
  }

  window.studyTimer = { mount: mount };
})();
