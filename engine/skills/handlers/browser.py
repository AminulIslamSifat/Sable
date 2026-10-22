"""browser handler — native Playwright automation, fully in-process.

No subprocess spawn, no unix socket, no daemon PID file. The persistent browser
lives inside the Sable process (engine/browser/session.py) on its own background
event loop, so state survives between tool calls while this handler stays a plain
synchronous generator.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Generator
from pathlib import Path
from typing import Any

from engine.skills.handlers.common import ASSETS_DIR, _end_event, _output_event

DEFAULT_USER_DATA_DIR = Path.home() / "sable_output" / "browser" / "profile"


def _truthy(v: Any) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _as_list(v: Any) -> list[str]:
    """Parse args/argv from an attr. Accepts JSON array, comma list, or space list."""
    if v is None or v == "":
        return []
    if isinstance(v, list):
        return [str(x) for x in v]
    s = str(v).strip()
    if s.startswith("["):
        try:
            loaded = json.loads(s)
            if isinstance(loaded, list):
                return [str(x) for x in loaded]
        except Exception:
            pass
    if "," in s:
        return [p.strip() for p in s.split(",") if p.strip()]
    return [p for p in s.split() if p]


def _as_dict(v: Any) -> dict | None:
    if isinstance(v, dict):
        return v
    if not v:
        return None
    try:
        loaded = json.loads(str(v))
        return loaded if isinstance(loaded, dict) else None
    except Exception:
        return None


def _resolve_binary(attrs: dict[str, str]) -> str | None:
    """Pick the browser binary: explicit path wins, then name lookup, then None.

    None means 'let Playwright use its own bundled Chromium', which is the
    correct fallback when the machine has no system Chrome.
    """
    from engine.browser.session import find_browser_binary

    explicit = (attrs.get("executable_path") or "").strip()
    if explicit:
        return explicit
    which = (attrs.get("browser_binary") or "").strip()
    if which:
        return find_browser_binary(which) or which
    return None


def _launch_opts(attrs: dict[str, str]) -> dict[str, Any]:
    """Build ensure_started() kwargs from tag attrs."""
    headless = True
    if "headless" in attrs:
        headless = _truthy(attrs.get("headless"))

    udd = (attrs.get("user_data_dir") or "").strip()
    if not udd:
        udd = str(DEFAULT_USER_DATA_DIR)

    opts: dict[str, Any] = {
        "browser": (attrs.get("browser") or "chromium").strip().lower(),
        "headless": headless,
        "user_data_dir": udd,
        "executable_path": _resolve_binary(attrs),
        "args": _as_list(attrs.get("args")),
    }
    proxy = _as_dict(attrs.get("proxy"))
    if proxy:
        opts["proxy"] = proxy
    vp = _as_dict(attrs.get("viewport"))
    if vp:
        opts["viewport"] = vp
    elif attrs.get("width") or attrs.get("height"):
        opts["viewport"] = {
            "width": int(attrs.get("width") or 1440),
            "height": int(attrs.get("height") or 900),
        }
    ua = (attrs.get("user_agent") or "").strip()
    if ua:
        opts["user_agent"] = ua
    return opts


def _save_screenshot(session, attrs: dict[str, str]) -> dict:
    """Capture and copy into ASSETS_DIR so the chat can display it."""
    import shutil

    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = ASSETS_DIR / f".shot_{uuid.uuid4().hex}.png"

    full_page = _truthy(attrs.get("full_page")) if "full_page" in attrs else False
    selector = (attrs.get("selector") or "").strip() or None
    clip = _as_dict(attrs.get("clip"))

    annotate = _truthy(attrs.get("annotate")) if "annotate" in attrs else False
    if annotate:
        session.annotate()
    try:
        info = session.screenshot(str(tmp), full_page=full_page, selector=selector, clip=clip)
    finally:
        if annotate:
            session.unannotate()

    name = (attrs.get("filename") or "").strip()
    if not name:
        name = f"shot_{int(time.time() * 1000)}.png"
    if not name.endswith(".png"):
        name += ".png"
    dest = ASSETS_DIR / name
    shutil.move(str(tmp), str(dest))

    info["path"] = str(dest)
    info["url"] = f"/assets/{dest.name}"
    return info


def handle_browser(
    tag_id: str, name: str, attrs: dict[str, str], content: str
) -> Generator[dict[str, Any], None, None]:
    started = time.time()

    action = (attrs.get("action") or "").strip().lower()
    if not action:
        yield _output_event(tag_id, "Missing 'action' parameter\n", "stderr")
        yield _end_event(tag_id, name, False, started, error="Missing action")
        return

    try:
        from engine.browser.session import get_session
    except Exception as exc:
        yield _output_event(tag_id, f"Playwright unavailable: {exc}\n", "stderr")
        yield _end_event(tag_id, name, False, started, error=f"playwright missing: {exc}")
        return

    session = get_session()

    # ── lifecycle verbs that must NOT auto-launch ───────────────────────
    if action == "status":
        info = session.status()
        yield _output_event(tag_id, json.dumps(info, indent=2)[:4000] + "\n")
        yield _end_event(tag_id, name, True, started, info)
        return

    if action == "close":
        session.stop()
        yield _output_event(tag_id, "browser closed\n")
        yield _end_event(tag_id, name, True, started, {"closed": True})
        return

    # ── everything else needs a live browser ────────────────────────────
    launched_now = not session.alive
    if launched_now:
        opts = _launch_opts(attrs)
        target = opts.get("executable_path") or f"playwright:{opts['browser']}"
        yield _output_event(
            tag_id,
            f"launching {opts['browser']} ({'headless' if opts['headless'] else 'headed'}) -> {target}\n",
            "command",
        )
        try:
            session.ensure_started(**opts)
        except Exception as exc:
            yield _output_event(tag_id, f"launch failed: {exc}\n", "stderr")
            yield _end_event(tag_id, name, False, started, error=str(exc))
            return

    try:
        result: dict[str, Any]

        if action == "open":
            url = (attrs.get("url") or "").strip() or content.strip()
            if not url:
                raise ValueError("open needs a url")
            wait_until = (attrs.get("wait_until") or "domcontentloaded").strip()
            result = session.open(url, wait_until=wait_until)
            if _truthy(attrs.get("screenshot")):
                result["screenshot"] = _save_screenshot(session, attrs)

        elif action == "back":
            result = session.back()
        elif action == "forward":
            result = session.forward()
        elif action == "reload":
            result = session.reload()

        elif action == "wait":
            if attrs.get("selector"):
                result = session.wait_selector(
                    attrs["selector"],
                    state=(attrs.get("state") or "visible"),
                    timeout_ms=int(attrs.get("timeout_ms") or 30000),
                )
            elif attrs.get("url_pattern"):
                result = session.wait_url(
                    attrs["url_pattern"], timeout_ms=int(attrs.get("timeout_ms") or 30000)
                )
            else:
                result = session.wait(float(attrs.get("seconds") or 1))

        elif action == "screenshot":
            shot = _save_screenshot(session, attrs)
            result = {"screenshot": shot, "bytes": shot["bytes"]}

        elif action == "find":
            result = session.find(
                by=(attrs.get("by") or "role"),
                value=(attrs.get("value") or attrs.get("selector") or ""),
                name=(attrs.get("name") or None),
                do=(attrs.get("do") or "count"),
                text=(attrs.get("text") or None),
                exact=_truthy(attrs.get("exact")) if "exact" in attrs else False,
                nth=(int(attrs["nth"]) if attrs.get("nth") not in (None, "") else None),
            )

        elif action == "scroll":
            result = session.scroll(
                direction=(attrs.get("direction") or "down"),
                amount=int(attrs.get("amount") or 500),
                selector=(attrs.get("selector") or None),
            )

        elif action == "drag":
            result = session.drag(attrs["source"], attrs["target"])

        elif action == "upload":
            files = _as_list(attrs.get("files"))
            if not files:
                raise ValueError("upload needs 'files'")
            result = session.upload(attrs["selector"], files)

        elif action in ("check", "uncheck"):
            result = session.check(attrs["selector"], checked=(action == "check"))

        elif action == "focus":
            result = session.focus(attrs["selector"])

        elif action == "dblclick":
            result = session.dblclick(attrs["selector"])

        elif action == "set":
            what = (attrs.get("what") or attrs.get("setting") or "").lower()
            if what == "viewport":
                result = session.set_viewport(int(attrs.get("width") or 1440), int(attrs.get("height") or 900))
            elif what == "media":
                result = session.set_media(
                    color_scheme=(attrs.get("color_scheme") or None),
                    reduced_motion=(attrs.get("reduced_motion") or None),
                )
            elif what == "headers":
                hdrs = _as_dict(attrs.get("headers"))
                if not hdrs:
                    raise ValueError("set what=headers needs a 'headers' JSON object")
                result = session.set_headers(hdrs)
            elif what == "offline":
                result = session.set_offline(_truthy(attrs.get("offline", "true")))
            elif what == "geolocation":
                result = session.set_geolocation(
                    float(attrs.get("latitude") or 0), float(attrs.get("longitude") or 0)
                )
            else:
                raise ValueError("set needs what=viewport|media|headers|offline|geolocation")

        elif action == "route":
            result = session.route(
                pattern=attrs["url_pattern"],
                action=(attrs.get("route_action") or "abort"),
                body=(attrs.get("body") or None),
                status=int(attrs.get("status") or 200),
                content_type=(attrs.get("content_type") or "application/json"),
            )

        elif action == "unroute":
            result = session.unroute(attrs.get("url_pattern") or None)

        elif action == "har_replay":
            result = session.har_replay(attrs["har_path"], not_found=(attrs.get("not_found") or "abort"))

        elif action == "snapshot":
            snap = session.snapshot(
                interactive_only=(_truthy(attrs.get("interactive_only", "true"))),
                scope=(attrs.get("scope") or None),
                max_depth=int(attrs.get("max_depth") or 12),
            )
            yield _output_event(tag_id, snap["text"] + chr(10))
            # Only the count goes back to the model. The refs/url/title are
            # already in the text above; echoing them as JSON doubled the
            # output without adding a single fact.
            result = {"count": snap.get("count", 0)}
            below = snap.get("below_fold") or 0
            if below:
                result["below_fold"] = below

        elif action == "annotate":
            result = session.annotate()

        elif action == "dump_html":
            result = session.dump_html(attrs.get("selector") or "body")
            result["html"] = result["html"][: int(attrs.get("max_chars") or 20000)]

        elif action == "extract":
            result = session.extract(attrs.get("selector") or "body")
            result["text"] = result["text"][: int(attrs.get("max_chars") or 20000)]

        elif action == "source":
            result = session.source()
            result["html"] = result["html"][: int(attrs.get("max_chars") or 20000)]

        elif action == "eval":
            js = (attrs.get("js") or "").strip() or content.strip()
            if not js:
                raise ValueError("eval needs 'js'")
            result = session.eval(js)

        elif action == "click":
            sel = (attrs.get("selector") or "").strip()
            if not sel:
                raise ValueError("click needs a selector")
            result = session.click(sel, timeout_ms=int(attrs.get("timeout_ms") or 3000))
            if _truthy(attrs.get("screenshot")):
                # Drop the selector: the element we just clicked is usually gone
                # after a navigation, and screenshotting it stalls 30s waiting
                # for a detached handle. Post-click shots mean the page.
                result["screenshot"] = _save_screenshot(
                    session, {k: v for k, v in attrs.items() if k != "selector"}
                )

        elif action == "type":
            sel = (attrs.get("selector") or "").strip()
            if not sel:
                raise ValueError("type needs a selector")
            result = session.type_text(
                sel, attrs.get("text") or "", clear=_truthy(attrs.get("clear", "true"))
            )

        elif action == "press":
            key = (attrs.get("key") or attrs.get("keys") or "").strip()
            if not key:
                raise ValueError("press needs a key")
            result = session.press(key, attrs.get("selector") or None)

        elif action == "hover":
            result = session.hover(attrs["selector"])

        elif action == "select":
            result = session.select(attrs["selector"], attrs.get("value") or "")

        elif action == "tabs":
            result = session.tabs()
        elif action == "tab_new":
            result = session.tab_new(attrs.get("url") or None)
            if _truthy(attrs.get("screenshot")):
                result["screenshot"] = _save_screenshot(session, attrs)
        elif action == "tab_switch":
            result = session.tab_switch(int(attrs.get("index") or 0))
        elif action == "tab_close":
            result = session.tab_close(int(attrs.get("index") or 0))

        elif action == "cookies":
            result = session.cookies()
        elif action == "set_cookies":
            raw = _as_dict(attrs.get("cookies"))
            cookie_list = raw.get("cookies") if raw and "cookies" in raw else raw
            if isinstance(cookie_list, dict):
                cookie_list = [cookie_list]
            if not isinstance(cookie_list, list):
                raise ValueError("set_cookies needs a JSON array")
            result = session.set_cookies(cookie_list)
        elif action == "storage":
            result = session.storage()

        elif action == "network_start":
            result = session.network_start()
        elif action == "network_stop":
            result = session.network_stop()
        elif action == "network_log":
            result = session.network_log(
                limit=int(attrs.get("limit") or 100),
                only_errors=_truthy(attrs.get("only_errors")),
            )
        elif action == "network_clear":
            result = session.network_clear()

        elif action == "console_start":
            result = session.console_start()
        elif action == "console_stop":
            result = session.console_stop()
        elif action == "console_log":
            result = session.console_log(
                limit=int(attrs.get("limit") or 100),
                level=(attrs.get("level") or None),
            )
        elif action == "console_clear":
            result = session.console_clear()

        else:
            raise ValueError(f"unknown action: {action}")

    except Exception as exc:
        yield _output_event(tag_id, f"{type(exc).__name__}: {exc}\n", "stderr")
        yield _end_event(tag_id, name, False, started, error=f"{type(exc).__name__}: {exc}")
        return

    if not isinstance(result, dict):
        # Defensive: a session method that forgets to return a dict should not
        # turn a successful action into a TypeError two lines later.
        result = {"result": result}
    result["launched_now"] = launched_now

    shot = result.pop("screenshot", None)
    if shot:
        result["screenshot_path"] = shot["path"]
        result["screenshot_url"] = shot["url"]
        yield _output_event(tag_id, f"screenshot -> {shot['url']} ({shot['bytes']} bytes)\n")

    preview = json.dumps(result, indent=2, ensure_ascii=False, default=str)
    if len(preview) > 12000:
        preview = preview[:12000] + "\n... truncated"
    yield _output_event(tag_id, preview + "\n")
    yield _end_event(tag_id, name, True, started, result)
