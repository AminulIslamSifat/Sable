/**
 * Account auth — Sable's startup gate (Phase 1).
 *
 * Talks to /api/account/*, which proxies to the Sable Bridge (identity +
 * sessions + per-user DEK). On success the bridge session token becomes the
 * local bearer token, so the app reloads into a normal authed state.
 *
 * Google: button stays hidden until /api/account/config says it's enabled.
 * The OAuth code arrives on ?code= after Google redirects to /oauth/callback.
 */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const REDIRECT_URI = () => window.location.origin + "/oauth/callback";

  let _mode = "signin"; // "signin" | "signup"

  function _err(msg) {
    const el = $("acctError");
    if (!el) return;
    if (!msg) { el.classList.add("hidden"); el.textContent = ""; return; }
    el.textContent = msg;
    el.classList.remove("hidden");
  }

  function _setMode(mode) {
    _mode = mode;
    const signin = mode === "signin";
    $("acctTitle").textContent = signin ? "Welcome to Sable" : "Create your account";
    $("acctSub").textContent = signin ? "Sign in to your account" : "One account, one Sable workspace";
    $("acctName").style.display = signin ? "none" : "";
    $("acctSubmit").textContent = signin ? "Sign in" : "Create account";
    $("acctPassword").setAttribute("autocomplete", signin ? "current-password" : "new-password");

    $("acctTabSignin").style.background = signin ? "var(--accent)" : "transparent";
    $("acctTabSignin").style.color = signin ? "#fff" : "var(--text)";
    $("acctTabSignup").style.background = signin ? "transparent" : "var(--accent)";
    $("acctTabSignup").style.color = signin ? "var(--text)" : "#fff";
    _err("");
  }

  async function _submit() {
    const email = ($("acctEmail").value || "").trim();
    const password = $("acctPassword").value || "";
    const name = ($("acctName").value || "").trim();
    const btn = $("acctSubmit");

    if (!email || !password) { _err("Email and password are required"); return; }
    if (_mode === "signup" && password.length < 8) {
      _err("Password must be at least 8 characters"); return;
    }

    btn.disabled = true;
    const label = btn.textContent;
    btn.textContent = _mode === "signin" ? "Signing in…" : "Creating…";
    _err("");

    try {
      const path = _mode === "signin" ? "/api/account/login" : "/api/account/register";
      const body = _mode === "signin"
        ? { email, password }
        : { email, password, name };
      const res = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        _err(data.detail || "Authentication failed");
        return;
      }
      localStorage.setItem("sable_token", data.token);
      // Hand control back to core.js so the provider-setup phase can run in
      // this same page load (no reload needed — the server already stored it).
      if (window.__acctResolve) { window.__acctResolve(); window.__acctResolve = null; }
    } catch (e) {
      _err("Connection error: " + e.message);
    } finally {
      btn.disabled = false;
      btn.textContent = label;
    }
  }

  async function _init() {
    // Wire tabs + form once.
    $("acctTabSignin")?.addEventListener("click", () => _setMode("signin"));
    $("acctTabSignup")?.addEventListener("click", () => _setMode("signup"));
    $("acctForm")?.addEventListener("submit", (e) => { e.preventDefault(); _submit(); });
    _setMode("signin");

    // Google availability + button.
    try {
      const res = await fetch("/api/account/config");
      if (res.ok) {
        const cfg = await res.json();
        if (cfg.google_enabled) {
          $("acctGoogle").style.display = "";
          $("acctDivider").style.display = "flex";
          $("acctGoogle").addEventListener("click", () => {
            const cid = cfg.google_client_id || window.__GOOGLE_CLIENT_ID;
            if (!cid) { _err("Google client ID not configured"); return; }
            const url = "https://accounts.google.com/o/oauth2/v2/auth"
              + "?client_id=" + encodeURIComponent(cid)
              + "&redirect_uri=" + encodeURIComponent(REDIRECT_URI())
              + "&response_type=code"
              + "&scope=" + encodeURIComponent("openid email profile")
              + "&prompt=select_account";
            window.location.href = url;
          });
        }
      }
    } catch {}
  }

  /** Handle ?code= from Google's redirect (page loads at /oauth/callback). */
  async function _handleOAuthCode() {
    const params = new URLSearchParams(window.location.search);
    const code = params.get("code");
    if (!code) return false;
    try {
      const res = await fetch("/api/account/google", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code, redirect_uri: REDIRECT_URI() }),
      });
      const data = await res.json().catch(() => ({}));
      if (res.ok && data.token) {
        localStorage.setItem("sable_token", data.token);
        window.location.href = "/";
        return true;
      }
      history.replaceState({}, "", "/");
      _err(data.detail || "Google sign-in failed");
    } catch (e) {
      _err("Google sign-in error: " + e.message);
    }
    return false;
  }

  window.AccountAuth = {
    /** Run the gate. Resolves only if the user skips (success reloads). */
    async run() {
      const handled = await _handleOAuthCode();
      if (handled) return;
      await _init();
      await new Promise((resolve) => {
        // No skip button currently — resolve is a safety valve only.
        window.__acctResolve = resolve;
      });
    },
  };
})();
