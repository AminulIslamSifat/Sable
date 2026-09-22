/**
 * Profile tab — account identity, plan/payment status, bridge health, logout.
 *
 * Reads from the bridge via /api/account/*. No secrets are rendered; the bridge
 * public() projection strips password/google ids before they reach the client.
 */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);

  let _wired = false;

  function _fmtDate(iso) {
    if (!iso) return "—";
    try {
      const d = new Date(iso);
      if (isNaN(d.getTime())) return "—";
      return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
    } catch { return "—"; }
  }

  function _initials(name, email) {
    const src = (name || "").trim();
    if (src) {
      const parts = src.split(/\s+/);
      return (parts[0][0] + (parts[1]?.[0] || "")).toUpperCase();
    }
    return (email || "?")[0].toUpperCase();
  }

  function _showSignedIn(user, bridge) {
    $("profileSignedIn").style.display = "";
    $("profileSignedOut").style.display = "none";

    $("profileAvatar").textContent = _initials(user.name, user.email);
    $("profileName").textContent = user.name || user.email || "—";
    $("profileEmail").textContent = user.email || "—";

    const plan = (user.plan || "free").toLowerCase();
    const badge = $("profilePlanBadge");
    badge.textContent = plan;
    if (plan !== "free") {
      badge.style.background = "var(--accent)";
      badge.style.color = "#fff";
    }

    $("profilePayment").textContent = user.payment_status || "free";
    $("profileCreated").textContent = _fmtDate(user.created);

    if (bridge) {
      $("profileBridgeUrl").textContent = bridge.bridge_url || "—";
      const st = $("profileBridgeStatus");
      if (bridge.error) {
        st.textContent = "⚠ " + bridge.error;
        st.style.color = "#ff6b6b";
      } else if (bridge.ok) {
        st.textContent = "● online" + (bridge.store ? " · " + bridge.store : "");
        st.style.color = "#4ade80";
      } else {
        st.textContent = "● offline";
        st.style.color = "#d29922";
      }
    }
  }

  function _showSignedOut() {
    $("profileSignedIn").style.display = "none";
    $("profileSignedOut").style.display = "";
  }

  async function loadProfileTab() {
    const status = $("profileStatus");
    try {
      const [meRes, bridgeRes] = await Promise.all([
        fetch("/api/account/me"),
        fetch("/api/account/bridge"),
      ]);
      if (!meRes.ok) {
        _showSignedOut();
        return;
      }
      const me = await meRes.json();
      let bridge = null;
      if (bridgeRes.ok) bridge = await bridgeRes.json();
      _showSignedIn(me.user || {}, bridge);
    } catch (e) {
      _showSignedOut();
      if (status) status.textContent = "Failed to load account: " + e.message;
    }
  }

  function _wire() {
    if (_wired) return;
    _wired = true;

    $("profileSignInBtn")?.addEventListener("click", () => {
      // Bounce to the login gate by clearing the stale session and reloading.
      localStorage.removeItem("sable_token");
      location.reload();
    });

    $("profileLogoutBtn")?.addEventListener("click", async () => {
      if (!confirm("Log out of this Sable account?")) return;
      const status = $("profileStatus");
      try {
        await fetch("/api/account/logout", { method: "POST" });
      } catch {}
      localStorage.removeItem("sable_token");
      location.reload();
    });

    $("profileProvisionBtn")?.addEventListener("click", async () => {
      const status = $("profileStatus");
      const btn = $("profileProvisionBtn");
      btn.disabled = true;
      if (status) { status.textContent = "Provisioning…"; status.style.color = "var(--text-dim)"; }
      try {
        const res = await fetch("/api/account/provision", { method: "POST" });
        const data = await res.json().catch(() => ({}));
        if (res.ok) {
          if (status) {
            status.textContent = data.reused
              ? "✅ Vault key already provisioned."
              : "✅ Vault key provisioned.";
            status.style.color = "#4ade80";
          }
        } else {
          if (status) { status.textContent = "❌ " + (data.detail || "Provision failed"); status.style.color = "#ff6b6b"; }
        }
      } catch (e) {
        if (status) { status.textContent = "❌ " + e.message; status.style.color = "#ff6b6b"; }
      } finally {
        btn.disabled = false;
      }
    });
  }

  window.loadProfileTab = function () {
    _wire();
    loadProfileTab();
  };
})();
