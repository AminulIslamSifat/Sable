/**
 * calendar.js — Full-month calendar view in main chat area.
 *
 * Opens via rail-switch 'calendar' event. Replaces #chat with a month grid.
 * Sidebar shows events for the selected day.
 */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const MONTH_NAMES = [
    'January','February','March','April','May','June',
    'July','August','September','October','November','December'
  ];

  let currentYear, currentMonth; // 0-indexed month
  let selectedDate = null;       // 'YYYY-MM-DD' string
  let eventsCache = {};          // { 'YYYY-MM-DD': [...] }
  let sidebarOriginalContent = null;
  let isOpen = false;

  /* ── API helpers ── */
  async function fetchEvents(year, month1) {
    const r = await fetch(`/api/calendar/events?year=${year}&month=${month1}`);
    if (!r.ok) throw new Error(r.statusText);
    return r.json();
  }

  /* ── Grid rendering ── */
  function renderGrid() {
    const grid = $('calGrid');
    const title = $('calTitle');
    if (!grid || !title) return;

    title.textContent = `${MONTH_NAMES[currentMonth]} ${currentYear}`;
    grid.innerHTML = '';

    const firstDay = new Date(currentYear, currentMonth, 1);
    const lastDayNum = new Date(currentYear, currentMonth + 1, 0).getDate();
    // Monday=0 ... Sunday=6
    let startDow = firstDay.getDay();
    startDow = startDow === 0 ? 6 : startDow - 1;

    const today = new Date();
    const todayStr = `${today.getFullYear()}-${String(today.getMonth()+1).padStart(2,'0')}-${String(today.getDate()).padStart(2,'0')}`;

    // Previous month padding
    const prevMonthLast = new Date(currentYear, currentMonth, 0).getDate();
    for (let i = startDow - 1; i >= 0; i--) {
      const cell = createCell(prevMonthLast - i, true);
      grid.appendChild(cell);
    }

    // Current month days
    for (let d = 1; d <= lastDayNum; d++) {
      const dateStr = `${currentYear}-${String(currentMonth+1).padStart(2,'0')}-${String(d).padStart(2,'0')}`;
      // Only show schedules on the grid (todos stay in sidebar)
      const dayEvents = (eventsCache[dateStr] || []).filter(e => e.type === 'schedule');
      const isToday = dateStr === todayStr;
      const isSelected = dateStr === selectedDate;
      const cell = createCell(d, false, dayEvents, dateStr, isToday, isSelected);
      grid.appendChild(cell);
    }

    // Next month padding to fill 6 rows
    const totalCells = startDow + lastDayNum;
    const remaining = (Math.ceil(totalCells / 7) * 7) - totalCells;
    for (let i = 1; i <= remaining; i++) {
      const cell = createCell(i, true);
      grid.appendChild(cell);
    }
  }

  function createCell(day, isPadding, events = [], dateStr = '', isToday = false, isSelected = false) {
    const cell = document.createElement('div');
    cell.className = 'cal-cell';
    if (isPadding) cell.classList.add('cal-cell-pad');
    if (isToday) cell.classList.add('cal-cell-today');
    if (isSelected) cell.classList.add('cal-cell-selected');

    const num = document.createElement('span');
    num.className = 'cal-day-num';
    num.textContent = day;
    cell.appendChild(num);

    if (!isPadding && events.length > 0) {
      const dots = document.createElement('div');
      dots.className = 'cal-event-dots';
      // Show up to 3 dots by type
      const types = [...new Set(events.map(e => e.type))];
      types.slice(0, 3).forEach(t => {
        const dot = document.createElement('span');
        dot.className = `cal-dot cal-dot-${t}`;
        dots.appendChild(dot);
      });
      if (events.length > 3) {
        const more = document.createElement('span');
        more.className = 'cal-dot-more';
        more.textContent = `+${events.length - 3}`;
        dots.appendChild(more);
      }
      cell.appendChild(dots);

      // First event title preview
      const preview = document.createElement('div');
      preview.className = 'cal-event-preview';
      preview.textContent = events[0].title;
      cell.appendChild(preview);
    }

    if (!isPadding) {
      cell.dataset.date = dateStr;
      cell.addEventListener('click', () => selectDate(dateStr));
    }

    return cell;
  }

  /* ── Day selection → sidebar update ── */
  function selectDate(dateStr) {
    selectedDate = dateStr;
    renderGrid(); // re-render to update selection highlight
    renderSidebarList(dateStr);
  }

  /* ── API helpers for CRUD ── */
  async function apiPost(path, body) {
    const r = await fetch('/api' + path, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) });
    if (!r.ok) throw new Error(r.statusText);
    return r.json();
  }
  async function apiPut(path, body) {
    const r = await fetch('/api' + path, { method: 'PUT', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) });
    if (!r.ok) throw new Error(r.statusText);
    return r.json();
  }
  async function apiDelete(path) {
    const r = await fetch('/api' + path, { method: 'DELETE' });
    if (!r.ok) throw new Error(r.statusText);
    return r.json();
  }

  async function refreshCalendarData() {
    try {
      const data = await fetchEvents(currentYear, currentMonth + 1);
      eventsCache = data.events || {};
    } catch (e) {
      console.warn('[Calendar] refresh failed', e);
    }
    renderGrid();
    if (selectedDate) renderSidebarList(selectedDate);
  }

  function renderSidebarList(dateStr) {
    const container = $('calSidebarContent');
    if (!container) return;

    if (!dateStr) {
      container.innerHTML = '<div class="cal-sidebar-empty">Select a day</div>';
      return;
    }

    // Only show schedules in calendar sidebar (todos are in their own rail)
    const events = (eventsCache[dateStr] || []).filter(e => e.type === 'schedule');
    const d = new Date(dateStr + 'T00:00:00');
    const dayNames = ['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'];

    let html = '';

    // ── Date header ──
    html += `<div class="cal-sidebar-header">
      <span class="cal-sidebar-date">${dayNames[d.getDay()]}, ${d.getDate()} ${MONTH_NAMES[d.getMonth()]}</span>
      <span class="cal-sidebar-count">${events.length} event${events.length !== 1 ? 's' : ''}</span>
    </div>`;

    // ── Quick-add card (schedules only) ──
    html += `<div class="cal-add-card">
      <div class="cal-add-header">
        <span class="cal-add-icon"><i data-lucide="calendar-days" class="icon-lucide"></i></span>
        <span class="cal-add-title">New Schedule</span>
      </div>
      <input id="calAddTitle" class="cal-add-input" type="text" placeholder="What's scheduled?" />
      <div class="cal-add-controls">
        <select id="calAddType">
          <option value="occasional">One-time</option>
          <option value="daily">Daily</option>
          <option value="weekly">Weekly</option>
        </select>
        <input id="calAddTime" type="time" />
        <button id="calAddBtn" class="cal-add-btn">Add</button>
      </div>
    </div>`;

    // ── Event list ──
    if (events.length === 0) {
      html += '<div class="cal-sidebar-empty">No events this day</div>';
    } else {
      const sorted = [...events].sort((a, b) => (a.time || '99:99').localeCompare(b.time || '99:99'));
      html += '<div class="cal-event-list">';
      sorted.forEach(evt => {
        const timeStr = evt.time || 'All day';
        const descHtml = evt.description ? `<div class="cal-evt-desc">${escHtml(evt.description)}</div>` : '';
        const typeLabel = evt.schedule_type === 'daily' ? 'Daily' : evt.schedule_type === 'weekly' ? 'Weekly' : 'One-time';
        html += `<div class="cal-evt-item" data-id="${evt.id}">
          <div class="cal-evt-time">${timeStr}</div>
          <div class="cal-evt-body">
            <div class="cal-evt-title"><i data-lucide="calendar-days" class="icon-lucide"></i> ${escHtml(evt.title)}</div>
            <div class="cal-evt-meta">${typeLabel}${descHtml ? ' · ' + escHtml(evt.description) : ''}</div>
          </div>
          <div class="cal-evt-actions">
            <button class="cal-evt-edit" data-id="${evt.id}" title="Edit">✎</button>
            <button class="cal-evt-del" data-id="${evt.id}" title="Delete">✕</button>
          </div>
        </div>`;
      });
      html += '</div>';
    }

    container.innerHTML = html;
    if (window.lucide) lucide.createIcons({ nodes: container.querySelectorAll('[data-lucide]') });

    // ── Wire up add button (schedules only) ──
    const addBtn = $('calAddBtn');
    if (addBtn) {
      addBtn.addEventListener('click', async () => {
        const title = ($('calAddTitle')?.value || '').trim();
        if (!title) return;
        const schedType = $('calAddType')?.value || 'occasional';
        const time = $('calAddTime')?.value || null;

        try {
          const body = { title, schedule_type: schedType, time };
          if (schedType === 'occasional') body.start_date = dateStr;
          await apiPost('/schedules', body);
          await refreshCalendarData();
        } catch (e) {
          console.error('[Calendar] add FAILED:', e);
        }
      });
    }

    // ── Wire up delete buttons ──
    container.querySelectorAll('.cal-evt-del').forEach(btn => {
      btn.addEventListener('click', async () => {
        try {
          await apiDelete('/schedules/' + btn.dataset.id);
          await refreshCalendarData();
        } catch (e) {
          console.error('[Calendar] delete FAILED:', e);
        }
      });
    });

    // ── Wire up edit buttons (inline title rename) ──
    container.querySelectorAll('.cal-evt-edit').forEach(btn => {
      btn.addEventListener('click', async () => {
        const item = btn.closest('.cal-evt-item');
        const titleEl = item?.querySelector('.cal-evt-title');
        if (!titleEl) return;

        const currentTitle = titleEl.textContent.replace(/^📅\s*/, '');
        const newTitle = prompt('Edit title:', currentTitle);
        if (newTitle === null || newTitle.trim() === '' || newTitle.trim() === currentTitle) return;

        try {
          await apiPut('/schedules/' + btn.dataset.id, { title: newTitle.trim() });
          await refreshCalendarData();
        } catch (e) {
          console.error('[Calendar] edit FAILED:', e);
        }
      });
    });
  }

  function escHtml(s) {
    const d = document.createElement('div');
    d.textContent = s || '';
    return d.innerHTML;
  }

  /* ── Open / Close ── */
  async function openCalendar() {
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


    const now = new Date();
    currentYear = now.getFullYear();
    currentMonth = now.getMonth();
    selectedDate = `${now.getFullYear()}-${String(now.getMonth()+1).padStart(2,'0')}-${String(now.getDate()).padStart(2,'0')}`;

    // Swap sidebar content
    const chatsSection = document.querySelector('.sidebar-chats');
    if (chatsSection) {
      sidebarOriginalContent = chatsSection.innerHTML;
      chatsSection.innerHTML = '<div id="calSidebarContent" class="cal-sidebar-wrap"></div>';
    }

    // Hide other main-area views
    const chatEl = $('chat');
    const inputEl = $('inputArea');
    if (chatEl) chatEl.classList.add('hidden');
    if (inputEl) inputEl.classList.add('hidden');
    ['searchView', 'researchView', 'imageView', 'promptgenView', 'dashboardView', 'knowledgeView', 'ocrView'].forEach(id => {
      const el = $(id);
      if (el) { el.classList.add('hidden'); el.style.display = ''; }
    });

    // Show calendar
    document.body.classList.add('calendar-open');
    const calView = $('calendarView');
    if (calView) calView.classList.remove('hidden');

    // Load events
    try {
      const data = await fetchEvents(currentYear, currentMonth + 1);
      eventsCache = data.events || {};
    } catch (e) {
      console.warn('[Calendar] initial load failed', e);
      eventsCache = {};
    }

    renderGrid();
    renderSidebarList(selectedDate);
  }

  function closeCalendar() {
    if (!isOpen) return;
    isOpen = false;

    document.body.classList.remove('calendar-open');
    const calView = $('calendarView');
    if (calView) calView.classList.add('hidden');
    // Restore chat
    const chatEl = $('chat');
    const inputEl = $('inputArea');
    if (chatEl) chatEl.classList.remove('hidden');
    if (inputEl) inputEl.classList.remove('hidden');

    // Restore sidebar
    const chatsSection = document.querySelector('.sidebar-chats');
    if (chatsSection && sidebarOriginalContent !== null) {
      chatsSection.innerHTML = sidebarOriginalContent;
      sidebarOriginalContent = null;
      // Re-trigger chat list render if needed
      if (window.renderChatList) window.renderChatList();
    }

    selectedDate = null;
    eventsCache = {};
  }

  /* ── Navigation ── */
  async function changeMonth(delta) {
    currentMonth += delta;
    if (currentMonth > 11) { currentMonth = 0; currentYear++; }
    if (currentMonth < 0) { currentMonth = 11; currentYear--; }

    try {
      const data = await fetchEvents(currentYear, currentMonth + 1);
      eventsCache = data.events || {};
    } catch (e) {
      console.warn('Calendar: failed to load events', e);
      eventsCache = {};
    }

    renderGrid();
    // Keep selected date if still in view, otherwise clear
    if (selectedDate) {
      const sd = new Date(selectedDate + 'T00:00:00');
      if (sd.getFullYear() === currentYear && sd.getMonth() === currentMonth) {
        renderSidebarList(selectedDate);
      } else {
        selectedDate = null;
        renderSidebarList(null);
      }
    }
  }

  /* ── Wire up controls ── */
  function init() {
    $('calPrev')?.addEventListener('click', () => changeMonth(-1));
    $('calNext')?.addEventListener('click', () => changeMonth(1));
    $('calToday')?.addEventListener('click', () => {
      const now = new Date();
      currentYear = now.getFullYear();
      currentMonth = now.getMonth();
      selectedDate = `${now.getFullYear()}-${String(now.getMonth()+1).padStart(2,'0')}-${String(now.getDate()).padStart(2,'0')}`;
      changeMonth(0); // reload
    });

    // Listen for rail-switch
    window.addEventListener('rail-switch', (e) => {
      const target = e.detail?.target;
      if (target === 'calendar') {
        if (isOpen) {
          closeCalendar();
        } else {
          openCalendar();
        }
      } else if (isOpen) {
        // Any other rail (or null/deselect) — close calendar
        closeCalendar();
      }
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
