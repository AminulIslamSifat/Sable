"""Keyless Google Drive access — public / "anyone with the link" content only.

No API key. No OAuth. No Google Cloud project. Drive exposes three endpoints
that answer to anyone:

    embeddedfolderview?id=<ID>          folder listing (HTML)
    uc?export=download&id=<ID>          file bytes
    drive.usercontent.google.com/...    file bytes (current, handles big files)

This is the same mechanism `gdown` and shell download scripts use. It is the
DEFAULT path for Drive in Sable — credentials are only needed for *private*
files, which is the optional path in engine.drive.client.

Tradeoff: the folder listing is scraped from HTML, so it's more fragile than
the JSON API. The authed client remains as a fallback when this fails.
"""
from __future__ import annotations

import contextlib
import html as _html
import logging
import re
import urllib.parse
from typing import Any, AsyncIterator

import httpx

logger = logging.getLogger("sable.drive.public")

_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_HEADERS = {"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"}

_EFV = "https://drive.google.com/embeddedfolderview"
_UC = "https://drive.google.com/uc"
_DL = "https://drive.usercontent.google.com/download"

# Hard cap, mirrored from library_ingest.
_MAX_DOWNLOAD = 100 * 1024 * 1024

_ID_RE = re.compile(r"[A-Za-z0-9_-]{5,200}")


class PublicDriveError(Exception):
    """A keyless Drive failure with a user-presentable message."""

    def __init__(self, message: str, *, status: int = 0, needs_auth: bool = False):
        super().__init__(message)
        self.status = status
        self.needs_auth = needs_auth


def _check_id(file_id: str) -> str:
    if not _ID_RE.fullmatch(file_id or ""):
        raise PublicDriveError("Invalid Google Drive file id", status=400)
    return file_id


# ── URL parsing (mirrors DriveClient.parse_id) ───────────────────────────────


def parse_id(url_or_id: str) -> tuple[str, str]:
    """Extract (kind, id) from a Drive share URL or a bare id.

    kind is 'file' or 'folder'; unknown ids default to 'file' and the first
    listing/metadata call corrects it.
    """
    s = (url_or_id or "").strip()
    if not s:
        raise PublicDriveError("No Drive link provided", status=400)
    if "://" not in s and "/" not in s and " " not in s:
        return "file", _check_id(s)
    try:
        u = urllib.parse.urlparse(s)
        q = urllib.parse.parse_qs(u.query)
        if "id" in q and q["id"]:
            return "file", _check_id(q["id"][0])
        parts = [p for p in u.path.split("/") if p]
        for marker, kind in (("d", "file"), ("folders", "folder")):
            if marker in parts:
                i = parts.index(marker)
                if i + 1 < len(parts):
                    return kind, _check_id(parts[i + 1])
    except PublicDriveError:
        raise
    except Exception:
        pass
    raise PublicDriveError(
        "Could not parse a Drive file or folder id from that link", status=400
    )


# ── Folder listing (HTML scrape) ─────────────────────────────────────────────

_ENTRY_SPLIT = re.compile(r'<div class="flip-entry"', re.IGNORECASE)
_ENTRY_ID = re.compile(r'id="entry-([A-Za-z0-9_-]+)"', re.IGNORECASE)
_TITLE = re.compile(r'class="flip-entry-title"[^>]*>(.*?)</div>', re.IGNORECASE | re.DOTALL)
_MODIFIED = re.compile(
    r'class="flip-entry-last-modified"[^>]*>\s*<div[^>]*>(.*?)</div>',
    re.IGNORECASE | re.DOTALL,
)
_FOLDER_HREF = re.compile(r'href="[^"]*?/drive/folders/([A-Za-z0-9_-]+)', re.IGNORECASE)
_FILE_HREF = re.compile(r'href="[^"]*?/file/d/([A-Za-z0-9_-]+)', re.IGNORECASE)


def _strip_tags(s: str) -> str:
    return _html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def parse_folder_html(html: str) -> list[dict[str, Any]]:
    """Parse an embeddedfolderview response into entry dicts.

    Entries carry: id, name, is_folder, modified. A real folder may be empty,
    which legitimately returns an empty list.
    """
    out: list[dict[str, Any]] = []
    # Split into per-entry chunks; each chunk starts at a flip-entry div.
    chunks = _ENTRY_SPLIT.split(html)[1:]
    for chunk in chunks:
        # Keep the chunk bounded to its own entry so fields don't bleed across.
        next_entry = chunk.find('class="flip-entry"')
        body = chunk[:next_entry] if next_entry != -1 else chunk

        m_id = _ENTRY_ID.search('id="entry-' + body) or _ENTRY_ID.search(body)
        # The id attribute lives in the opening tag, which the split consumed —
        # recover it from the leading fragment of the chunk.
        if not m_id:
            continue
        fid = m_id.group(1)

        is_folder = bool(_FOLDER_HREF.search(body))
        m_title = _TITLE.search(body)
        name = _strip_tags(m_title.group(1)) if m_title else fid
        m_mod = _MODIFIED.search(body)
        modified = _strip_tags(m_mod.group(1)) if m_mod else ""

        out.append({
            "id": fid,
            "name": name or fid,
            "is_folder": is_folder,
            "modified": modified,
        })
    return out


def is_folder_html(html: str) -> bool:
    """True when an embeddedfolderview response is a folder listing.

    A *file* id also returns HTTP 200 from this endpoint, but renders a
    single-file landing page with none of the list shell. The list header is
    present for folders, including empty ones.
    """
    return "flip-list-header" in html or "flip-entry-info" in html


async def _fetch_folder_html(folder_id: str) -> str:
    params = {"id": folder_id, "list": ""}
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=_HEADERS) as c:
        r = await c.get(_EFV, params=params)
    if r.status_code == 404:
        raise PublicDriveError(
            "Not found — check the link is correct and shared publicly", status=404
        )
    if r.status_code >= 400:
        raise PublicDriveError(
            f"Drive returned {r.status_code} for this folder", status=r.status_code
        )
    return r.text


async def list_folder(folder_id: str) -> list[dict[str, Any]]:
    """List a public folder's children. Raises PublicDriveError on failure."""
    _check_id(folder_id)
    html = await _fetch_folder_html(folder_id)
    if not is_folder_html(html):
        # A file id renders a landing page here instead of a listing.
        raise PublicDriveError("Not a folder", status=404)
    if "accounts.google.com" in html and not parse_folder_html(html):
        raise PublicDriveError(
            "This folder isn't public — set sharing to 'Anyone with the link'",
            status=403, needs_auth=True,
        )
    return parse_folder_html(html)


# ── Download ─────────────────────────────────────────────────────────────────

_CONFIRM_RE = re.compile(
    r'name="(confirm|uuid|id|export|at)"\s+value="([^"]*)"', re.IGNORECASE
)
_CONFIRM_ACTION = re.compile(r'<form[^>]+action="([^"]+)"', re.IGNORECASE)


def _looks_like_html(resp: httpx.Response) -> bool:
    ctype = (resp.headers.get("content-type") or "").lower()
    if "text/html" in ctype:
        return True
    # Some confirm pages come back as octet-stream but start with '<'.
    head = resp.content[:512].lstrip()
    return head.startswith(b"<") and b"<form" in resp.content[:8192].lower()


def _filename_from(resp: httpx.Response, fallback: str) -> str:
    cd = resp.headers.get("content-disposition") or ""
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', cd, re.IGNORECASE)
    if m:
        name = urllib.parse.unquote(m.group(1)).strip()
        if name:
            return name
    return fallback


async def download(file_id: str, name_hint: str = "") -> tuple[bytes, str]:
    """Fetch a public file's bytes keylessly. Returns (raw, filename).

    Handles the large-file confirmation interstitial: if Drive returns an HTML
    confirm form instead of bytes, the form fields are replayed to get the file.
    """
    fid = _check_id(file_id)
    fallback = name_hint or f"{fid}.bin"

    async with httpx.AsyncClient(timeout=None, follow_redirects=True, headers=_HEADERS) as c:
        r = await c.get(_DL, params={"id": fid, "export": "download"})
        if r.status_code == 404:
            raise PublicDriveError("File not found or not public", status=404)
        if r.status_code >= 400:
            raise PublicDriveError(f"Drive returned {r.status_code}", status=r.status_code)

        if _looks_like_html(r):
            # Large-file confirm interstitial — replay the form.
            action = _CONFIRM_ACTION.search(r.text)
            fields = {k: v for k, v in _CONFIRM_RE.findall(r.text)}
            if fields:
                url = _html.unescape(action.group(1)) if action else _DL
                if url.startswith("/"):
                    url = "https://drive.usercontent.google.com" + url
                r = await c.get(url, params=fields)
            if _looks_like_html(r):
                raise PublicDriveError(
                    "Drive returned a confirmation page instead of the file — "
                    "it may be too large or restricted",
                    status=422, needs_auth=True,
                )

        raw = r.content
        if len(raw) > _MAX_DOWNLOAD:
            raise PublicDriveError("File too large (max 100 MB)", status=413)
        return raw, _filename_from(r, fallback)


async def iter_download(file_id: str) -> AsyncIterator[bytes]:
    """Stream a public file, capped at _MAX_DOWNLOAD bytes."""
    fid = _check_id(file_id)
    total = 0
    async with httpx.AsyncClient(timeout=None, follow_redirects=True, headers=_HEADERS) as c:
        async with c.stream("GET", _DL, params={"id": fid, "export": "download"}) as r:
            if r.status_code >= 400:
                await r.aread()
                raise PublicDriveError(f"Drive returned {r.status_code}", status=r.status_code)
            async for chunk in r.aiter_bytes(1024 * 256):
                total += len(chunk)
                if total > _MAX_DOWNLOAD:
                    raise PublicDriveError("File too large (max 100 MB)", status=413)
                yield chunk


async def open_stream(
    file_id: str,
) -> tuple[httpx.AsyncClient, httpx.Response, str, int]:
    """Start a keyless streaming download of a public file.

    Returns (client, response, filename, total_bytes). The caller owns BOTH the
    client and the response and must close them — usually in the finally block
    of the generator that consumes the stream. total_bytes is 0 when Drive
    doesn't send a Content-Length, which the UI shows as an indeterminate bar.
    """
    fid = _check_id(file_id)
    client = httpx.AsyncClient(timeout=None, follow_redirects=True, headers=_HEADERS)
    try:
        req = client.build_request("GET", _DL, params={"id": fid, "export": "download"})
        resp = await client.send(req, stream=True)
    except Exception:
        with contextlib.suppress(Exception):
            await client.aclose()
        raise
    if resp.status_code >= 400:
        with contextlib.suppress(Exception):
            await resp.aread()
            await resp.aclose()
            await client.aclose()
        raise PublicDriveError(
            f"Drive returned {resp.status_code}", status=resp.status_code
        )
    ctype = (resp.headers.get("content-type") or "").lower()
    if "text/html" in ctype:
        # Large-file / restricted confirmation interstitial, not the file.
        with contextlib.suppress(Exception):
            await resp.aread()
            await resp.aclose()
            await client.aclose()
        raise PublicDriveError(
            "Drive returned a confirmation page instead of the file",
            status=422, needs_auth=True,
        )
    total = int(resp.headers.get("content-length") or 0)
    filename = _filename_from(resp, f"{fid}.bin")
    return client, resp, filename, total


async def get_meta(file_id: str) -> dict[str, Any]:
    """Resolve an id to {id, name, is_folder, is_google_doc} keylessly.

    Folders are detected by whether the embedded listing endpoint answers.
    """
    fid = _check_id(file_id)
    # Distinguish folder vs file from the listing page itself: a file id
    # returns 200 here too, but without the list shell.
    try:
        html = await _fetch_folder_html(fid)
        if is_folder_html(html):
            entries = parse_folder_html(html)
            name = fid
            m = re.search(r"<title>([^<]*)</title>", html, re.IGNORECASE)
            if m:
                name = _strip_tags(m.group(1)) or fid
            return {
                "id": fid, "name": name, "is_folder": True,
                "is_google_doc": False, "entries": len(entries),
            }
    except PublicDriveError as e:
        if e.status not in (404, 403):
            raise
    # Not a folder — probe the download endpoint for a filename.
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=_HEADERS) as c:
        r = await c.get(_DL, params={"id": fid, "export": "download"})
    if r.status_code == 404:
        raise PublicDriveError("Not found or not public", status=404)
    name = _filename_from(r, fid)
    return {
        "id": fid, "name": name, "is_folder": False,
        "is_google_doc": False,
        "size": int(r.headers.get("content-length") or 0),
    }


async def resolve(url_or_id: str) -> dict[str, Any]:
    """Resolve a link to a file/folder record, correcting file vs folder."""
    _, fid = parse_id(url_or_id)
    return await get_meta(fid)
