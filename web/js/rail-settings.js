/**
 * rail-settings.js — Appearance tab: show/hide activity-rail buttons.
 *
 * Pinned rails can never be hidden (Chat, Search, Files, Terminal,
 * Dashboard, Library, Settings). Everything else is user-toggleable.
 *
 * State lives in localStorage (`sable_rail_disabled`) as a JSON array of
 * rail ids. Visibility is applied on load and on every toggle, so the rail
 * is correct before the settings panel is ever opened.
 */
(function () {
  "use strict";

  const KEY = "sable_rail_disabled";

  // rail id -> human label. Order = display order in the tab.
  const TOGGLEABLE = [
    ["projects",  "Projects"],
    ["research",  "Deep Research"],
    ["image",     "Image Generator"],
    ["prompt",    "Prompt Generator"],
    ["ocr",       "OCR"],
    ["notes",     "Notes"],
    ["todo",      "Todo"],
    ["tasks",     "Tasks"],
    ["calendar",  "Calendar"],
    ["knowledge", "Knowledge Base"],
  ];

  const PINNED = new Set([
    "chat", "search", "files", "terminal",       // top
    "dashboard", "library", "settings",           // bottom
    "study",                                      // study-mode home (JS-controlled visibility)
  ]);

  function _getDisabled() {
    try {
      const raw = JSON.parse(localStorage.getItem(KEY) || "[]");
      return new Set(Array.isArray(raw) ? raw.filter((r) => !PINNED.has(r)) : []);
    } catch {
      return new Set();
    }
  }

  function _setDisabled(set) {
    localStorage.setItem(KEY, JSON.stringify([...set]));
  }

  /** Apply visibility to the rail right now. Safe to call any time. */
  function applyRailVisibility() {
    const rail = document.getElementById("activityRail");
    if (!rail) return;
    const disabled = _getDisabled();

    rail.querySelectorAll(".rail-btn[data-rail]").forEach((btn) => {
      const r = btn.dataset.rail;
      if (PINNED.has(r)) return;
      btn.style.display = disabled.has(r) ? "none" : "";
    });

    // A separator whose whole group is hidden should disappear too.
    ["rail-top", "rail-bottom"].forEach((cls) => {
      const container = rail.querySelector("." + cls);
      if (!container) return;
      const kids = Array.from(container.children);
      kids.forEach((el, i) => {
        if (!el.classList.contains("rail-sep")) return;
        let hasVisible = false;
        for (let j = i + 1; j < kids.length; j++) {
          const nxt = kids[j];
          if (nxt.classList.contains("rail-sep")) break;
          if (nxt.classList.contains("rail-btn") && nxt.style.display !== "none") {
            hasVisible = true;
            break;
          }
        }
        el.style.display = hasVisible ? "" : "none";
      });
    });

    // If the panel currently open just got hidden, fall back to Chat.
    const active = rail.querySelector(".rail-btn.active[data-rail]");
    if (active && active.style.display === "none") {
      rail.querySelector('.rail-btn[data-rail="chat"]')?.click();
    }
  }

  function _renderList() {
    const list = document.getElementById("railToggleList");
    if (!list) return;
    const disabled = _getDisabled();
    list.innerHTML = TOGGLEABLE.map(([id, label]) => `
      <label style="display:flex;align-items:center;gap:10px;padding:9px 12px;border:1px solid var(--border);border-radius:8px;background:var(--panel);font-size:13px;cursor:pointer;">
        <input type="checkbox" data-rail-toggle="${id}" ${disabled.has(id) ? "" : "checked"} style="cursor:pointer;">
        <span>${label}</span>
      </label>`).join("");

    list.querySelectorAll("input[data-rail-toggle]").forEach((cb) => {
      cb.addEventListener("change", () => {
        const set = _getDisabled();
        if (cb.checked) set.delete(cb.dataset.railToggle);
        else set.add(cb.dataset.railToggle);
        _setDisabled(set);
        applyRailVisibility();
        const st = document.getElementById("railStatus");
        if (st) { st.textContent = "✓ Saved"; st.style.color = "#4ade80"; setTimeout(() => { st.textContent = ""; }, 1500); }
      });
    });
  }

  window.loadRailTab = function () {
    _renderList();
    const reset = document.getElementById("railResetBtn");
    if (reset && !reset._wired) {
      reset._wired = true;
      reset.addEventListener("click", () => {
        _setDisabled(new Set());
        applyRailVisibility();
        _renderList();
        const st = document.getElementById("railStatus");
        if (st) { st.textContent = "✓ Restored"; st.style.color = "#4ade80"; setTimeout(() => { st.textContent = ""; }, 1500); }
      });
    }
  };

  // Apply immediately (rail is static markup, so this is safe at parse time)
  // and again after full load in case icons/DOM shift.
  window.applyRailVisibility = applyRailVisibility;
  applyRailVisibility();
  document.addEventListener("DOMContentLoaded", applyRailVisibility);
})();
