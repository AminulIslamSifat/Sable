"""Google Drive — browse public or private drives and import into the library.

KEYLESS BY DEFAULT. Public / "anyone with the link" content needs no API key,
no OAuth, and no Google Cloud project — see engine.drive.public. Credentials
are only required for *private* files, which is the optional path in
engine.drive.client:

    Public  → keyless (embeddedfolderview + usercontent)   ← default
    Private → API key or OAuth 2.0                          ← optional

Every request tries keyless first and only falls back to the authed client when
that fails AND credentials are configured. Imported files go through
engine.library_ingest — the exact pipeline Study uploads use — so a Drive
download lands in the library fully indexed, with zero extra frontend work.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import mimetypes
import re
import secrets
import time
import urllib.parse
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, StreamingResponse

from engine.drive import public as drive_public
from engine.drive import store as drive_store
from engine.drive.client import (
    DriveError,
    build_auth_url,
    exchange_code,
    get_drive_client,
    get_drive_settings,
    revoke_token,
    save_drive_settings,
)
from engine.library_ingest import ingest_bytes, pdf_tool

router = APIRouter()
logger = logging.getLogger("sable.drive")

# In-memory OAuth handshake state — single-user local app, so this is enough.
_oauth_state: dict[str, dict[str, Any]] = {}
_STATE_TTL = 600  # seconds
_STATE_MAX = 32  # hard ceiling so a flood of /callback hits can't grow forever


def _prune_oauth_state() -> None:
    """Drop expired handshakes and enforce the size ceiling."""
    now = time.time()
    for k in [k for k, v in _oauth_state.items() if now - v["ts"] > _STATE_TTL]:
        _oauth_state.pop(k, None)
    if len(_oauth_state) > _STATE_MAX:
        # Evict oldest first.
        for k, _ in sorted(_oauth_state.items(), key=lambda kv: kv[1]["ts"])[:
            len(_oauth_state) - _STATE_MAX
        ]:
            _oauth_state.pop(k, None)


def _redirect_uri(request: Request) -> str:
    """Loopback redirect that Google will send the user back to."""
    base = str(request.base_url).rstrip("/")
    # Google requires a loopback host; normalize 0.0.0.0 which browsers may
    # report when the server is bound to all interfaces.
    base = base.replace("://0.0.0.0", "://localhost").replace("://127.0.0.1", "://localhost")
    return f"{base}/api/drive/oauth/callback"


def _to_summary(f: dict[str, Any]) -> dict[str, Any]:
    """Trim a Drive metadata record to what the browse UI needs."""
    mime = f.get("mimeType", "")
    is_folder = mime == "application/vnd.google-apps.folder"
    caps = f.get("capabilities") or {}
    return {
        "id": f.get("id"),
        "name": f.get("name", "Untitled"),
        "mimeType": mime,
        "is_folder": is_folder,
        "is_google_doc": mime.startswith("application/vnd.google-apps") and not is_folder,
        "size": int(f.get("size") or 0),
        "modified": f.get("modifiedTime", ""),
        "owner": ((f.get("owners") or [{}])[0]).get("displayName", ""),
        "can_download": caps.get("canDownload", True),
        "web_link": f.get("webViewLink", ""),
    }


# ── Status / config ──────────────────────────────────────────────────────────


@router.get("/api/drive/status")
async def drive_status() -> dict[str, Any]:
    """Credential state + whether the converter is available for imports."""
    s = get_drive_settings()
    oauth = s.get("oauth") or {}
    client = get_drive_client()
    out: dict[str, Any] = {
        # Public Drive works with no credentials at all — `connected` only
        # reflects whether the OPTIONAL private-access path is configured.
        "keyless": True,
        "has_api_key": bool(s.get("api_key")),
        "has_oauth": bool(oauth.get("refresh_token")),
        "connected": client.connected,
        "converter": bool(pdf_tool()),
        "account": oauth.get("account") or "",
        "scopes": oauth.get("scopes") or [],
        "oauth_client_id": (s.get("oauth_client_id") or ""),
    }
    # about() asserts a user principal, which API keys cannot do — Google
    # rejects it with "API keys are not supported by this API". Only call it
    # when an OAuth account is actually connected.
    if client.has_oauth:
        try:
            about = await client.about()
            out["user"] = (about.get("user") or {}).get("displayName", "")
            out["email"] = (about.get("user") or {}).get("emailAddress", "")
            q = about.get("storageQuota") or {}
            out["quota"] = {"limit": int(q.get("limit") or 0), "usage": int(q.get("usage") or 0)}
        except DriveError as e:
            out["warning"] = str(e)
    return out


@router.post("/api/drive/config")
async def drive_config(body: dict[str, Any]) -> dict[str, Any]:
    """Save the API key and/or the OAuth client credentials."""
    patch: dict[str, Any] = {}
    if "api_key" in body:
        patch["api_key"] = (body.get("api_key") or "").strip() or None
    if "oauth_client_id" in body:
        patch["oauth_client_id"] = (body.get("oauth_client_id") or "").strip() or None
    if "oauth_client_secret" in body:
        patch["oauth_client_secret"] = (body.get("oauth_client_secret") or "").strip() or None
    if not patch:
        raise HTTPException(status_code=400, detail="nothing to update")
    save_drive_settings(patch)
    return {"ok": True}


@router.post("/api/drive/test")
async def drive_test() -> dict[str, Any]:
    """Verify credentials by hitting the Drive about endpoint."""
    client = get_drive_client()
    if not client.connected:
        raise HTTPException(status_code=400, detail="No Drive credentials configured")
    try:
        about = await client.about()
    except DriveError as e:
        raise HTTPException(status_code=400, detail=str(e))
    user = about.get("user") or {}
    return {"ok": True, "email": user.get("emailAddress", ""), "name": user.get("displayName", "")}


# ── OAuth flow ───────────────────────────────────────────────────────────────


@router.get("/api/drive/oauth/start")
async def drive_oauth_start(request: Request) -> RedirectResponse:
    """Kick off the loopback + PKCE consent flow."""
    s = get_drive_settings()
    client_id = (s.get("oauth_client_id") or "").strip()
    if not client_id:
        raise HTTPException(status_code=400, detail="Set an OAuth Client ID first")
    redirect_uri = _redirect_uri(request)
    state = secrets.token_urlsafe(24)
    url, verifier = build_auth_url(client_id, redirect_uri, state)
    _oauth_state[state] = {"verifier": verifier, "redirect_uri": redirect_uri, "ts": time.time()}
    _prune_oauth_state()
    return RedirectResponse(url)


@router.get("/api/drive/oauth/callback")
async def drive_oauth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    """Receive Google's redirect, exchange the code, persist the refresh token."""
    def _back(msg: str, ok: bool) -> RedirectResponse:
        # msg originates from Google's error query param. The frontend shows it
        # via textContent today, but cap + strip control chars so it stays
        # inert regardless of how the toast is rendered later.
        safe = re.sub(r"[\x00-\x1f\x7f]", "", str(msg or ""))[:200]
        q = urllib.parse.urlencode({"drive": "ok" if ok else "err", "msg": safe})
        return RedirectResponse(f"/?{q}")

    if error:
        return _back(error, False)
    if not code or not state:
        return _back("Missing code or state", False)
    _prune_oauth_state()
    pending = _oauth_state.pop(state, None)
    if not pending:
        return _back("OAuth session expired — try again", False)

    s = get_drive_settings()
    client_id = (s.get("oauth_client_id") or "").strip()
    client_secret = (s.get("oauth_client_secret") or "").strip()
    try:
        tok = await exchange_code(code, client_id, client_secret, pending["redirect_uri"], pending["verifier"])
    except DriveError as e:
        return _back(str(e), False)

    refresh = tok.get("refresh_token")
    if not refresh:
        return _back("No refresh token returned — revoke access and retry", False)

    oauth = {
        "refresh_token": refresh,
        "client_id": client_id,
        "scopes": (tok.get("scope") or "").split(),
        "connected_at": time.time(),
    }
    if client_secret:
        oauth["client_secret"] = client_secret
    # Fetch the account label for display.
    try:
        tmp = get_drive_client()
        tmp.oauth = {**oauth, "refresh_token": refresh}
        about = await tmp.about()
        oauth["account"] = (about.get("user") or {}).get("emailAddress", "")
    except Exception:
        oauth["account"] = ""

    save_drive_settings({"oauth": oauth})
    return _back("Google Drive connected", True)


@router.post("/api/drive/oauth/disconnect")
async def drive_oauth_disconnect() -> dict[str, Any]:
    """Drop local credentials, attempting a best-effort server-side revoke.

    If Google's revoke fails (offline, 5xx) local creds are still removed, but
    the caller is told — otherwise the user believes the grant is gone when the
    refresh token is still live on Google's side.
    """
    s = get_drive_settings()
    oauth = s.get("oauth") or {}
    token = oauth.get("refresh_token")
    revoked = True
    if token:
        try:
            await revoke_token(token)
        except Exception as exc:
            logger.warning("Drive revoke failed: %s", exc)
            revoked = False
    save_drive_settings({"oauth": None})
    return {
        "ok": True,
        "revoked": revoked,
        "warning": None if revoked else (
            "Local credentials removed, but Google's revoke failed — the app may "
            "still have access. Revoke it manually at myaccount.google.com/permissions."
        ),
    }


# ── Saved sources (the cards in the Drive tab) ───────────────────────────────


@router.get("/api/drive/sources")
async def drive_list_sources() -> list[dict[str, Any]]:
    """All saved Drive links, newest first."""
    return drive_store.list_sources()


@router.post("/api/drive/sources")
async def drive_add_source(body: dict[str, Any]) -> dict[str, Any]:
    """Add a Drive link and save it as a card. Resolves it first."""
    url = str(body.get("url") or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="no url provided")
    meta = await _resolve_any(url)
    count = int(meta.get("entries") or 0)
    src = drive_store.add_source(
        url=url,
        drive_id=str(meta.get("id")),
        name=str(meta.get("name") or meta.get("id")),
        kind=str(meta.get("kind") or "file"),
        count=count,
        account="",
    )
    return src


@router.delete("/api/drive/sources/{source_id}")
async def drive_remove_source(source_id: str) -> dict[str, Any]:
    if not drive_store.remove_source(source_id):
        raise HTTPException(status_code=404, detail="source not found")
    return {"ok": True}


@router.get("/api/drive/sources/{source_id}/files")
async def drive_source_files(source_id: str) -> dict[str, Any]:
    """Contents of a saved source: its file list, or the single file itself."""
    src = drive_store.get_source(source_id)
    if not src:
        raise HTTPException(status_code=404, detail="source not found")
    did = str(src.get("drive_id"))
    if src.get("kind") == "folder":
        data = await _browse_any(did)
        return {"source": src, "kind": "folder", **data}
    meta = await _resolve_any(f"https://drive.google.com/file/d/{did}/view")
    return {"source": src, "kind": "file", "items": [meta]}


@router.get("/api/drive/file/{file_id}/pdf")
async def drive_stream_pdf(
    file_id: str, request: Request, title: str = "", save: bool = True,
) -> StreamingResponse:
    """Stream a Drive file to the viewer and save it to the library in one pass.

    Two jobs, one download:

      * stream the bytes to the viewer with live progress headers
      * accumulate them and index the result into the library at the end

    `save=false` skips the import (used when re-viewing an already-imported
    doc). `title` names the doc if Drive sends no filename.
    """
    fid = file_id
    client = get_drive_client()
    hint = (title or "").strip()

    async def chunks():
        total = 0
        buf = bytearray()
        name = hint or f"{fid}.pdf"
        yield b""  # force headers to flush before the first real chunk
        try:
            try:
                hc, resp, rname, length = await drive_public.open_stream(fid)
            except drive_public.PublicDriveError as pub_err:
                if not client.connected:
                    raise
                logger.info("Keyless stream failed (%s) — trying authed", pub_err)
                dmeta = await client.get_file(fid)
                raw, rname = await client.download(dmeta)
                buf.extend(raw)
                total = len(raw)
                name = rname or name
                yield raw
                return
            name = rname or name
            try:
                async for chunk in resp.aiter_bytes(1024 * 256):
                    total += len(chunk)
                    if total > _MAX_IMPORT_BYTES:
                        raise drive_public.PublicDriveError(
                            "File too large (max 100 MB)", status=413
                        )
                    buf.extend(chunk)
                    yield chunk
            finally:
                with contextlib.suppress(Exception):
                    await resp.aclose()
                with contextlib.suppress(Exception):
                    await hc.aclose()
        except Exception:
            # Headers are already committed, so an error here truncates the
            # viewer's copy. Save whatever arrived anyway — "no matter what".
            logger.warning("Drive stream interrupted after %d bytes for %s", total, fid)
        finally:
            if save and buf:
                try:
                    meta = await _ingest_once(bytes(buf), name, fid)
                    if meta.get("id"):
                        _DOC_MAP[fid] = meta["id"]
                except Exception as exc:
                    logger.warning("Auto-save failed for %s: %s", fid, exc)

    # Report the real type instead of a pdf/octet-stream coin flip, so a caller
    # that trusts Content-Type can't be told a PNG is a PDF.
    media = _guess_mime(hint, None) or "application/pdf"
    headers = {
        "Content-Disposition": f'inline; filename="{hint or fid + ".pdf"}"',
        "X-Drive-Save": "1" if save else "0",
    }
    return StreamingResponse(chunks(), media_type=media, headers=headers)


@router.get("/api/drive/file/{file_id}/raw")
async def drive_stream_raw(
    file_id: str, request: Request, title: str = "", mime: str = "",
) -> StreamingResponse:
    """Stream a Drive file's bytes with its real content type. Never ingests.

    The viewer uses this for anything that isn't a PDF (images especially).
    /pdf is the wrong door for those: it mislabels the bytes *and* silently
    imports them into the library as a fake PDF on the way through.
    """
    fid = file_id
    client = get_drive_client()
    hint = (title or "").strip()
    media = _guess_mime(hint, mime) or "application/octet-stream"

    async def chunks():
        yield b""  # flush headers before the first real chunk
        try:
            hc, resp, _rname, _length = await drive_public.open_stream(fid)
            try:
                async for chunk in resp.aiter_bytes(1024 * 256):
                    yield chunk
            finally:
                with contextlib.suppress(Exception):
                    await resp.aclose()
                with contextlib.suppress(Exception):
                    await hc.aclose()
        except drive_public.PublicDriveError:
            if not client.connected:
                raise
            dmeta = await client.get_file(fid)
            raw, _rname = await client.download(dmeta)
            yield raw

    headers = {"Content-Disposition": f'inline; filename="{hint or fid}"'}
    return StreamingResponse(chunks(), media_type=media, headers=headers)


@router.get("/api/drive/file/{file_id}/doc")
async def drive_file_doc(file_id: str) -> dict[str, str]:
    """Library doc id for an already-imported Drive file ("" when not imported).

    The reader asks for this right after a remote download. That stream
    auto-ingests the file, so the doc quietly gains a library record mid-view —
    this is how Study AI attaches to a Drive document without the user ever
    pressing Import.
    """
    doc_id = _existing_doc(file_id)
    if not doc_id:
        return {"doc_id": "", "title": ""}
    from engine.config import LIBRARY_META_DIR
    title = ""
    with contextlib.suppress(Exception):
        rec = json.loads((LIBRARY_META_DIR / f"{doc_id}.json").read_text(encoding="utf-8"))
        title = str(rec.get("title") or "")
    return {"doc_id": doc_id, "title": title}


# ── Source layer: keyless first, authed fallback ─────────────────────────────


def _guess_mime(name: str | None, mime: str | None = None) -> str:
    """Best-effort content type for a Drive entry.

    The keyless browse path reports no mimeType at all, which used to make every
    non-folder entry look identical to the UI. That is why images were handed to
    the PDF viewer and died on parse: nothing downstream could tell them apart.
    Fall back to the filename whenever Drive gives us nothing usable.
    """
    if mime and mime != "application/octet-stream":
        return mime
    guessed, _ = mimetypes.guess_type(str(name or ""))
    return guessed or mime or ""


def _public_summary(meta: dict[str, Any]) -> dict[str, Any]:
    """Normalize a keyless resolve result to the browse/UI shape."""
    return {
        "id": meta.get("id"),
        "name": meta.get("name") or meta.get("id"),
        "mimeType": _guess_mime(meta.get("name"), meta.get("mimeType")),
        "kind": "folder" if meta.get("is_folder") else "file",
        "is_folder": bool(meta.get("is_folder")),
        "is_google_doc": bool(meta.get("is_google_doc")),
        "size": int(meta.get("size") or 0),
        "modified": meta.get("modified", ""),
        "owner": "",
        "can_download": True,
        "web_link": "",
        "entries": meta.get("entries"),
    }


async def _resolve_any(url: str) -> dict[str, Any]:
    """Resolve a link, preferring keyless public access."""
    try:
        meta = await drive_public.resolve(url)
        return _public_summary(meta)
    except drive_public.PublicDriveError as pub_err:
        # Not public (or parsing failed) — try an authed client if configured.
        client = get_drive_client()
        if not client.connected:
            raise HTTPException(
                status_code=pub_err.status or 400,
                detail=str(pub_err) + (
                    "" if pub_err.needs_auth is False else
                    " — if it's your own file, connect an account or API key"
                ),
            )
        try:
            meta = await client.resolve(url)
        except DriveError as e:
            raise HTTPException(status_code=e.status or 400, detail=str(e))
        return _to_summary(meta) | {"kind": meta.get("kind")}


async def _browse_any(folder_id: str) -> dict[str, Any]:
    """List a folder, preferring keyless public access."""
    try:
        entries = await drive_public.list_folder(folder_id)
        return {
            "folder": {"id": folder_id, "name": "", "is_folder": True},
            "items": [
                {
                    "id": e["id"],
                    "name": e["name"],
                    "mimeType": (
                        "application/vnd.google-apps.folder"
                        if e["is_folder"]
                        else _guess_mime(e.get("name"), e.get("mimeType"))
                    ),
                    "is_folder": e["is_folder"],
                    "is_google_doc": False,
                    "size": 0,
                    "modified": e.get("modified", ""),
                    "owner": "",
                    "can_download": True,
                    "web_link": "",
                }
                for e in entries
            ],
            "truncated": False,
            "mode": "public",
        }
    except drive_public.PublicDriveError as pub_err:
        client = get_drive_client()
        if not client.connected:
            raise HTTPException(status_code=pub_err.status or 400, detail=str(pub_err))
        try:
            folder = await client.get_file(folder_id)
            children = await client.list_children(folder_id)
        except DriveError as e:
            raise HTTPException(status_code=e.status or 400, detail=str(e))
        return {
            "folder": _to_summary(folder),
            "items": [_to_summary(c) for c in children],
            "truncated": len(children) >= 5000,
            "mode": "authed",
        }


@router.get("/api/drive/resolve")
async def drive_resolve(url: str) -> dict[str, Any]:
    """Resolve a pasted share link to a file/folder record (no auth needed)."""
    return await _resolve_any(url)


@router.get("/api/drive/browse")
async def drive_browse(folder_id: str) -> dict[str, Any]:
    """List the children of a folder (no auth needed for public folders)."""
    return await _browse_any(folder_id)


# ── Import ───────────────────────────────────────────────────────────────────

_MAX_CONCURRENT = 3
_MAX_IMPORT_BYTES = 100 * 1024 * 1024

# Drive file id → library doc id. Filled on import so a re-import replaces the
# existing doc instead of piling up duplicates.
_DOC_MAP: dict[str, str] = {}


def _existing_doc(fid: str) -> str:
    """Library doc id already created from this Drive file, if any.

    Checks the in-memory map first, then falls back to scanning meta records so
    the mapping survives a server restart.
    """
    if fid in _DOC_MAP:
        return _DOC_MAP[fid]
    from engine.config import LIBRARY_META_DIR
    marker = f"/file/d/{fid}/"
    for f in LIBRARY_META_DIR.glob("*.json"):
        try:
            meta = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if marker in str(meta.get("source_url") or ""):
            _DOC_MAP[fid] = str(meta.get("id"))
            return _DOC_MAP[fid]
    return ""


def _delete_doc(doc_id: str) -> None:
    """Remove a library doc's stored files (used before a re-import replaces it)."""
    from engine.config import (
        LIBRARY_META_DIR, LIBRARY_ORIG_DIR, LIBRARY_PDF_DIR, LIBRARY_TEXT_DIR,
    )
    from engine.library_ingest import thumb_path
    for p in (
        LIBRARY_META_DIR / f"{doc_id}.json",
        LIBRARY_PDF_DIR / f"{doc_id}.pdf",
        LIBRARY_TEXT_DIR / f"{doc_id}.json",
        thumb_path(doc_id),
    ):
        with contextlib.suppress(Exception):
            p.unlink()
    # Converted uploads keep an original alongside the PDF.
    for orig in LIBRARY_ORIG_DIR.glob(f"{doc_id}.*"):
        with contextlib.suppress(Exception):
            orig.unlink()


async def _ingest_once(
    raw: bytes, name: str, fid: str, *, group: dict[str, Any] | None = None,
    collection: str = "", collection_id: str = "",
) -> dict[str, Any]:
    """Index bytes into the library, replacing any prior copy of the same file.

    Used by both the explicit import endpoint and the viewer's auto-save, so
    the two paths can't diverge. Ingestion runs in a worker thread because
    PDF parsing and thumbnail rendering are CPU-bound.
    """
    old = _existing_doc(fid)
    doc = await asyncio.to_thread(
        ingest_bytes, raw, name,
        source_url=f"https://drive.google.com/file/d/{fid}/view",
        source="drive",
        collection=collection,
        collection_id=collection_id,
    )
    _DOC_MAP[fid] = doc["id"]
    if old and old != doc["id"]:
        # Replace: drop the previous copy so the library shows one entry.
        _delete_doc(old)
    if group and any(group.get(k) for k in ("semester", "subject", "topic")):
        _apply_group(doc["id"], group)
    return doc


@router.post("/api/drive/import")
async def drive_import(body: dict[str, Any]) -> dict[str, Any]:
    """Download one or more Drive files into the study library.

    body: { ids: ["..."], group?: {semester, subject, topic} }

    Files are fetched, normalized, and indexed through the same pipeline as
    Study uploads, so they appear in the library immediately. Per-file failures
    are reported without aborting the batch.
    """
    ids = body.get("ids") or []
    if not isinstance(ids, list) or not ids:
        raise HTTPException(status_code=400, detail="no file ids provided")
    ids = [str(i) for i in ids][:50]

    group = body.get("group") or {}
    sem = asyncio.Semaphore(_MAX_CONCURRENT)
    # Names hint at the original filename when Drive doesn't send one.
    names = body.get("names") or {}
    # Which saved Drive folder these came from, so the library can group them.
    collection = str(body.get("collection") or "")
    collection_id = str(body.get("collection_id") or "")

    client = get_drive_client()

    async def fetch(fid: str) -> tuple[bytes, str]:
        """Keyless public download first; authed only if that fails."""
        hint = str(names.get(fid) or "")
        try:
            return await drive_public.download(fid, hint)
        except drive_public.PublicDriveError as pub_err:
            if not client.connected:
                raise
            meta = await client.get_file(fid)
            if meta.get("mimeType") == "application/vnd.google-apps.folder":
                raise DriveError("is a folder", status=400)
            declared = int(meta.get("size") or 0)
            if declared > _MAX_IMPORT_BYTES:
                raise DriveError(
                    f"too large ({declared // (1024*1024)} MB, max 100 MB)", status=413
                )
            logger.info("Keyless fetch failed (%s) — trying authed", pub_err)
            return await client.download(meta)

    async def one(fid: str) -> dict[str, Any]:
        async with sem:
            try:
                raw, name = await fetch(fid)
                doc = await _ingest_once(
                    raw, name, fid, group=group,
                    collection=collection, collection_id=collection_id,
                )
                return {"id": fid, "ok": True, "doc": doc, "name": name}
            except (DriveError, drive_public.PublicDriveError, ValueError) as e:
                return {"id": fid, "ok": False, "error": str(e)}
            except Exception as e:  # noqa: BLE001 — report, don't kill the batch
                logger.exception("Drive import failed for %s", fid)
                return {"id": fid, "ok": False, "error": f"unexpected error: {e}"}

    results = await asyncio.gather(*(one(i) for i in ids))
    ok = [r for r in results if r.get("ok")]
    return {
        "imported": len(ok),
        "failed": len(results) - len(ok),
        "results": results,
        "docs": [r["doc"] for r in ok],
    }


def _apply_group(doc_id: str, group: dict[str, Any]) -> None:
    """Stamp academic grouping onto a freshly-ingested doc's meta record."""
    from engine.config import LIBRARY_META_DIR
    import json

    path = LIBRARY_META_DIR / f"{doc_id}.json"
    if not path.exists():
        return
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
        meta["group"] = {
            "semester": str(group.get("semester") or ""),
            "subject": str(group.get("subject") or ""),
            "topic": str(group.get("topic") or ""),
        }
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:
        logger.warning("Could not apply group to %s: %s", doc_id, exc)
