"""Real pages, rendered inside Sable's own page in an iframe.

No browser is launched. The page is fetched over HTTPS, rewritten so it comes
from Sable's origin, and served. The JavaScript that animates it runs in the
browser the user already has open, inside the frame.

Why the page has to come from Sable's origin: DuckDuckGo (and Google, Bing,
Brave) send ``X-Frame-Options: SAMEORIGIN``, so a frame pointing straight at
them is refused by the browser and renders blank. When the HTML is served by
Sable instead, that rule is ours to omit and the frame is allowed.

    Sable's tab  (the browser already open)
      └── <iframe src="/api/search/frame?url=...&token=...">
            └── /api/search/view    -> fetched + rewritten page
                 └── /api/search/asset  -> its CSS, JS, images, fonts

The frame is same-origin with Sable, so Sable's own JavaScript can read the
results straight out of it -- no server-side renderer involved.

DuckDuckGo's ``html.duckduckgo.com/html/`` endpoint is fully server-rendered,
so its results appear the moment the HTML arrives; scripts only decorate it.
"""

from __future__ import annotations

import base64
import logging
import re
import time
from typing import Optional
from urllib.parse import quote, urljoin

from bs4 import BeautifulSoup
from curl_cffi import requests as creq
from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, Response

logger = logging.getLogger(__name__)
router = APIRouter()

IMPERSONATE = "chrome124"
TIMEOUT = 15
MAX_BYTES = 6_000_000

CSS_URL = re.compile(r"url\(\s*(['\"]?)([^)'\"]+)\1\s*\)")

_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}

# Runs in the user's browser, inside the frame. The page believes it is talking
# to its own origin; this points those calls at the proxy instead of letting the
# browser block them as cross-origin.
SHIM = r"""
(function(){
  var T = '__TOKEN__';
  var TAIL = T ? ('&token=' + encodeURIComponent(T)) : '';
  var P = '/api/search/asset?u=';
  function b64(s){
    return btoa(unescape(encodeURIComponent(s)))
      .replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'');
  }
  function abs(u){ try { return new URL(u, document.baseURI).href } catch(e){ return u } }
  function need(u){ return /^https?:/i.test(u) && u.indexOf(location.origin) !== 0; }
  if (window.fetch) {
    var of = window.fetch;
    window.fetch = function(input, init){
      try {
        if (typeof input === 'string' && need(abs(input))) input = P + b64(abs(input)) + TAIL;
        else if (input && input.url && need(abs(input.url))) {
          input = new Request(P + b64(abs(input.url)) + TAIL, input);
        }
      } catch(e){}
      return of.call(this, input, init);
    };
  }
  var OX = window.XMLHttpRequest;
  if (OX) {
    window.XMLHttpRequest = function(){
      var x = new OX(); var op = x.open;
      x.open = function(m, u){
        try { if (need(abs(u))) u = P + b64(abs(u)) + TAIL; } catch(e){}
        var a = Array.prototype.slice.call(arguments, 2);
        return op.apply(x, [m, u].concat(a));
      };
      return x;
    };
  }
})();
"""


def b64e(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def b64d(s: str) -> str:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)).decode()


class _Tokens:
    """Carries the auth token through every URL the proxied page emits.

    A subresource load (script, stylesheet, image, the shim's fetch) is a plain
    browser request and cannot set an Authorization header, so the token travels
    in the query string on each one. Without this every asset 401s and the page
    renders blank or unstyled.
    """

    def __init__(self, token: str = "") -> None:
        self.token = token

    def suffix(self) -> str:
        return f"&token={quote(self.token, safe='')}" if self.token else ""

    def asset(self, path: str) -> str:
        return "/api/search/asset?u=" + b64e(path) + self.suffix()


def _rewrite_css(css: str, base_url: str, tokens: _Tokens) -> str:
    def repl(m: re.Match) -> str:
        raw = m.group(2).strip()
        if raw.startswith(("data:", "#", "blob:", "about:")):
            return m.group(0)
        return f"url('{tokens.asset(urljoin(base_url, raw))}')"
    return CSS_URL.sub(repl, css)


def _fetch(url: str) -> tuple[str, str]:
    resp = creq.get(url, headers=_HEADERS, impersonate=IMPERSONATE,
                    timeout=TIMEOUT, allow_redirects=True)
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}")
    return resp.text[:MAX_BYTES], resp.url


def _proxy_page(url: str, token: str = "") -> str:
    """Fetch a page and make it framable and same-origin with Sable."""
    text, final = _fetch(url)
    soup = BeautifulSoup(text, "html.parser")
    tokens = _Tokens(token)

    for m in soup.find_all("meta", attrs={"http-equiv": re.compile("content-security-policy", re.I)}):
        m.decompose()

    if soup.head is None:
        head = soup.new_tag("head")
        (soup.html or soup).insert(0, head)

    soup.head.insert(0, soup.new_tag("base", href=final))

    for link in soup.find_all("link"):
        href = link.get("href")
        rel = link.get("rel") or []
        if href and any(r in ("stylesheet", "preload", "modulepreload", "icon", "shortcut") for r in rel):
            link["href"] = tokens.asset(urljoin(final, href))

    for script in soup.find_all("script"):
        src = script.get("src")
        if src:
            script["src"] = tokens.asset(urljoin(final, src))

    for img in soup.find_all("img"):
        src = img.get("src")
        if src and not src.startswith(("data:", "blob:")):
            img["src"] = tokens.asset(urljoin(final, src))
        ss = img.get("srcset")
        if ss:
            parts = []
            for piece in ss.split(","):
                piece = piece.strip()
                if not piece:
                    continue
                bits = piece.split()
                bits[0] = tokens.asset(urljoin(final, bits[0]))
                parts.append(" ".join(bits))
            img["srcset"] = ", ".join(parts)

    for style in soup.find_all("style"):
        if style.string:
            style.string = _rewrite_css(style.string, final, tokens)

    for el in soup.find_all(style=True):
        el["style"] = _rewrite_css(el["style"], final, tokens)

    shim = soup.new_tag("script")
    shim.string = SHIM.replace("__TOKEN__", token or "")
    soup.head.insert(0, shim)

    return str(soup)


@router.get("/api/search/frame")
async def search_frame(url: str = Query(...), h: int = 0,
                       token: str = Query("")) -> HTMLResponse:
    """The iframe shell. Point an <iframe> here and the real page opens inside Sable."""
    inner = "/api/search/view?url=" + quote(url, safe="")
    if token:
        inner += "&token=" + quote(token, safe="")
    height = f"{h}px" if h else "100vh"
    return HTMLResponse(
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<style>"
        "*{margin:0;padding:0;box-sizing:border-box}"
        "html,body{width:100%;height:100%;overflow:hidden;background:#fff}"
        f"iframe{{width:100%;height:{height};border:0;display:block}}"
        "</style></head><body>"
        f'<iframe src="{inner}" sandbox="allow-scripts allow-same-origin allow-forms allow-popups"></iframe>'
        "</body></html>"
    )


@router.get("/api/search/view")
async def search_view(url: str = Query(...), token: str = Query("")) -> HTMLResponse:
    """The page itself, rewritten to be framable and same-origin with Sable."""
    import asyncio
    try:
        html = await asyncio.to_thread(_proxy_page, url, token)
    except Exception as exc:  # noqa: BLE001 - show the reason, never a blank frame
        logger.warning("search proxy failed for %s: %s", url, exc)
        html = (
            "<!doctype html><html><body style=\"font:14px system-ui;padding:24px\">"
            f"<p>Could not load this page.</p><pre style=\"opacity:.6\">{type(exc).__name__}: {exc}</pre>"
            "</body></html>"
        )
    return HTMLResponse(
        html,
        headers={
            "X-Frame-Options": "SAMEORIGIN",
            "Content-Security-Policy": "frame-ancestors 'self'",
        },
    )


@router.get("/api/search/asset")
async def search_asset(u: str = Query(...), token: str = Query("")) -> Response:
    """Any resource the framed page needs: CSS, JS, image, font, JSON."""
    try:
        url = b64d(u)
    except Exception:
        return Response(b"", media_type="text/plain", status_code=400)

    try:
        resp = creq.get(url, headers=_HEADERS, impersonate=IMPERSONATE,
                        timeout=TIMEOUT, allow_redirects=True)
    except Exception:  # noqa: BLE001
        return Response(b"", media_type="text/plain", status_code=502)

    ctype = resp.headers.get("content-type", "application/octet-stream")
    headers = {"Access-Control-Allow-Origin": "*", "Cache-Control": "private, max-age=300"}
    if "css" in ctype.lower():
        return Response(_rewrite_css(resp.text, url, _Tokens(token)), media_type="text/css", headers=headers)
    return Response(resp.content, media_type=ctype, headers=headers)


# --------------------------------------------------------------- google passthrough
#
# Google answers a cold request with a JavaScript gate: the page computes a
# proof-of-work token, then redirects to /search with it. Rewriting that page's
# HTML is pointless -- the token has to be produced by a browser. What does work
# is keeping the whole handshake on our own origin: the iframe loads
# /api/search/g/search?q=..., every relative URL and redirect stays under
# /api/search/g/, cookies live in one session per iframe, and the gate completes
# where we can see it. Google's own X-Frame-Options never applies because the
# browser is not framing google.com -- it is framing us.

G_PREFIX = "/api/search/g"
_g_sessions: dict[str, creq.Session] = {}
_g_lock = __import__("threading").Lock()


class _GSession:
    def __init__(self, sid: str) -> None:
        self.sid = sid
        self.http = creq.Session(impersonate=IMPERSONATE)
        self.http.headers.update({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Upgrade-Insecure-Requests": "1",
        })
        self.touched = time.time()


_g_map: dict[str, _GSession] = {}


def _g_get(sid: str) -> _GSession:
    with _g_lock:
        sess = _g_map.get(sid)
        if sess is None:
            sess = _GSession(sid)
            _g_map[sid] = sess
        sess.touched = time.time()
        return sess


def _g_rewrite(text: str) -> str:
    """Keep every URL the page emits inside the proxy prefix."""
    text = text.replace("https://www.google.com", G_PREFIX)
    text = text.replace("https://google.com", G_PREFIX)
    text = text.replace("//www.google.com", G_PREFIX)
    text = text.replace("\\/\\/www.google.com", G_PREFIX)
    return text


def _g_forward(sid: str, path: str, query: str) -> tuple[int, str, str]:
    sess = _g_get(sid)
    url = "https://www.google.com/" + path.lstrip("/")
    if query:
        url += "?" + query
    resp = sess.http.get(url, timeout=TIMEOUT, allow_redirects=True)
    ctype = resp.headers.get("content-type", "text/html")
    body = resp.text
    if "html" in ctype.lower():
        # drop the meta CSP, route all URLs back through us, pin a base so any
        # relative redirect the gate performs resolves into the proxy too
        body = re.sub(r'<meta[^>]+http-equiv=["\']?content-security-policy["\']?[^>]*>', "", body, flags=re.I)
        body = _g_rewrite(body)
        if "<base" not in body.lower():
            body = body.replace("<head>", f'<head><base href="{G_PREFIX}/">', 1)
    return resp.status_code, ctype, body


@router.get(G_PREFIX + "/{path:path}")
async def google_passthrough(request: Request, path: str,
                            token: str = Query(""), sid: str = Query("")):
    """Google, proxied whole, so its JS gate completes on our origin."""
    import asyncio
    from urllib.parse import parse_qsl, urlencode

    sid = sid or "default"
    # rebuild the upstream query verbatim, dropping only our own params
    pairs = [(k, v) for k, v in parse_qsl(request.url.query, keep_blank_values=True)
             if k not in ("token", "sid")]
    qs = urlencode(pairs)
    try:
        status, ctype, body = await asyncio.to_thread(_g_forward, sid, path, qs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("google passthrough failed for %s: %s", path, exc)
        return Response(f"upstream failed: {type(exc).__name__}: {exc}".encode(),
                        media_type="text/plain", status_code=502)
    return Response(body.encode("utf-8"), media_type=ctype, status_code=status,
                    headers={
                        "Cache-Control": "no-store",
                        # ours to set: this response frames inside Sable, while
                        # the browser never frames google.com itself
                        "X-Frame-Options": "SAMEORIGIN",
                        "Content-Security-Policy": "frame-ancestors 'self'",
                    })


@router.get("/api/search/frame/google")
async def google_frame(q: str = Query(...), h: int = 0,
                       token: str = Query("")) -> HTMLResponse:
    """Google, framed directly -- no proxy, no rewriting, no server fetch.

    ``igu=1`` is Google's embed flag: with it, the response carries no
    ``X-Frame-Options`` at all, so the browser is allowed to frame google.com
    itself. That matters because every hop is then made by the user's own
    browser -- their cookies, their TLS fingerprint, their IP -- and Google's
    proof-of-work gate runs there, where it can finish.

    Proxying the gate was the wrong shape: it made every hop look like a server,
    so Google answered with a CAPTCHA. A direct frame makes every hop look like
    the person actually asking.
    """
    from urllib.parse import urlencode
    src = "https://www.google.com/search?" + urlencode({"q": q, "igu": "1", "hl": "en"})
    height = f"{h}px" if h else "100vh"
    return HTMLResponse(
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<style>"
        "*{margin:0;padding:0;box-sizing:border-box}"
        "html,body{width:100%;height:100%;overflow:hidden;background:#fff}"
        f"iframe{{width:100%;height:{height};border:0;display:block}}"
        "</style></head><body>"
        f'<iframe src="{src}" referrerpolicy="no-referrer-when-downgrade" '
        'sandbox="allow-scripts allow-same-origin allow-forms allow-popups"></iframe>'
        "</body></html>"
    )


@router.get("/api/search/status")
async def search_status() -> dict:
    return {"ok": True, "mode": "http-proxy", "browser": "none"}
