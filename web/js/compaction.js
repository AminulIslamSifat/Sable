/* ---------- Context compaction UI ----------
 *
 * Three SSE events drive this:
 *   compaction_start   -> a card appears saying the transcript is being rewritten
 *   compaction_summary -> the summary renders as a normal model message
 *   compaction_done    -> the card settles, the context ring resets
 *
 * The ring is reset by writing window._statusContextChars. That variable is
 * the single source of truth read by statusbar.js, so the ring, the tooltip
 * and the breakdown popup all agree after a reload too (the server computes
 * context_chars from the same cutoff).
 */

(function () {
  if (window._sableCompactionStart) return; // already installed

  function _fmt(n) {
    n = Number(n) || 0;
    if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
    if (n >= 1e3) return (n / 1e3).toFixed(0) + "k";
    return String(n);
  }

  function _turnOf(pane) {
    if (!pane) return null;
    return pane.querySelector(".turn:last-child") || pane;
  }

  function _card(pane) {
    const turn = _turnOf(pane);
    if (!turn) return null;
    let card = turn.querySelector(".compaction-card");
    if (card) return card;

    card = document.createElement("div");
    card.className = "compaction-card";
    card.innerHTML =
      '<div class="compaction-head">' +
        '<span class="compaction-icon">&#9680;</span>' +
        '<span class="compaction-title">Compacting context</span>' +
        '<span class="compaction-pct"></span>' +
      '</div>' +
      '<div class="compaction-body">Rewriting the transcript into a structured summary&hellip;</div>' +
      '<div class="compaction-stats"></div>';
    turn.appendChild(card);
    return card;
  }

  window._sableCompactionStart = function (pane, evt) {
    const card = _card(pane);
    if (!card) return;
    card.classList.remove("done");
    card.classList.add("running");
    const pctEl = card.querySelector(".compaction-pct");
    if (pctEl) pctEl.textContent = (evt && evt.pct != null) ? evt.pct + "% used" : "";
    const body = card.querySelector(".compaction-body");
    if (body) body.textContent = "Rewriting the transcript into a structured summary\u2026";
    if (typeof window._sableScrollBottom === "function") window._sableScrollBottom();
  };

  window._sableCompactionSummary = function (pane, text) {
    if (!text) return;
    // The summary is the model's message. Render it through the normal path
    // so it looks, and reloads, exactly like any other answer.
    if (typeof window._sableAddMessage === "function") {
      window._sableAddMessage("bot", text, undefined, pane);
    }
    if (typeof window._sableScrollBottom === "function") window._sableScrollBottom();
  };

  window._sableCompactionDone = function (pane, evt) {
    const card = _card(pane);
    if (card) {
      card.classList.remove("running");
      card.classList.add("done");
      const title = card.querySelector(".compaction-title");
      if (title) title.textContent = "Context compacted";
      // The "N% used" chip describes the state that TRIGGERED compaction. Once
      // it completes that number is stale and actively misleading - it says
      // 112% while the ring has already dropped to 14%. Clear it.
      const pct = card.querySelector(".compaction-pct");
      if (pct) pct.textContent = "";
      const body = card.querySelector(".compaction-body");
      if (body) body.textContent = "Older messages were replaced by the summary below.";
      const stats = card.querySelector(".compaction-stats");
      if (stats) {
        const freed = _fmt(evt && evt.freed_chars);
        const now = _fmt(evt && evt.summary_chars);
        stats.innerHTML =
          '<span class="cstat"><b>' + freed + '</b> chars freed</span>' +
          '<span class="cstat"><b>' + now + '</b> chars now in context</span>' +
          (evt && evt.kept_messages != null
            ? '<span class="cstat"><b>' + evt.kept_messages + '</b> recent messages kept</span>'
            : '');
      }
    }

    // Reset the ring to what the MODEL now sees. Cumulative usage is a SQL sum
    // over every message and is deliberately NOT touched - it keeps climbing.
    const chars = (evt && evt.summary_chars) || 0;
    window._statusContextChars = chars;
    if (typeof window._sableUpdateStatusBarContext === "function") {
      window._sableUpdateStatusBarContext();
    }

    if (typeof window._sableScrollBottom === "function") window._sableScrollBottom();
  };

  window._sableCompactionFailed = function (pane, evt) {
    // The summary never arrived. Settle the card so it does not spin forever;
    // the conversation continues underneath, just without the rewrite.
    const card = _card(pane);
    if (!card) return;
    card.classList.remove("running");
    card.classList.add("failed");
    const title = card.querySelector(".compaction-title");
    if (title) title.textContent = "Compaction skipped";
    const pct = card.querySelector(".compaction-pct");
    if (pct) pct.textContent = "";
    const body = card.querySelector(".compaction-body");
    if (body) {
      body.textContent =
        "Could not build a summary (" + ((evt && evt.reason) || "unknown reason") +
        "). The conversation continues with its full history.";
    }
    // Context was NOT reduced, so the ring must be left exactly as it was.
    if (typeof window._sableScrollBottom === "function") window._sableScrollBottom();
  };
})();