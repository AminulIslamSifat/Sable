"""Google Drive API v3 client — one code path, two credential types.

    Public  → ?key=<API_KEY>          (anything shared "anyone with the link")
    Private → Authorization: Bearer   (OAuth 2.0, sees everything the account can)

Every list/download/export call is identical apart from how it authenticates,
so the client holds both credentials and picks the right one per request. No
DOM scraping, no browser — pure REST.

OAuth uses the loopback + PKCE flow, which is what Google recommends for
installed apps: the local Sable server receives the redirect, so there is no
out-of-band code copy/paste.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import time
import urllib.parse
from pathlib import Path
from typing import Any, AsyncIterator

import httpx

logger = logging.getLogger("sable.drive")

_API = "https://www.googleapis.com/drive/v3"
_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"

_SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.metadata.readonly",
]

# Google-native MIME types have no raw bytes — they must be exported.
_EXPORT_MAP: dict[str, tuple[str, str]] = {
    "application/vnd.google-apps.document": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"
    ),
    "application/vnd.google-apps.presentation": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.drawing": ("application/pdf", ".pdf"),
}
_FOLDER_MIME = "application/vnd.google-apps.folder"
_SHORTCUT_MIME = "application/vnd.google-apps.shortcut"

_FILE_FIELDS = (
    "id,name,mimeType,size,modifiedTime,createdTime,owners(displayName),"
    "iconLink,webViewLink,shortcutDetails(targetId,targetMimeType),"
    "capabilities(canDownload,canCopy),videoMediaMetadata,imageMediaMetadata"
)


# Hard cap on a single download, mirrored from library_ingest. Enforced while
# streaming so a hostile share can't push GBs into memory before we check.
_MAX_DOWNLOAD = 100 * 1024 * 1024

# Ceiling on a single folder listing.
_MAX_LIST_ITEMS = 5000

# Drive ids are opaque URL-safe tokens. Validating them stops a crafted id
# (e.g. "x/../../v3/permissions") from being normalized into a different
# Google API endpoint that accepts the same credential.
_ID_RE = re.compile(r"[A-Za-z0-9_-]{5,200}")


def _check_id(file_id: str) -> str:
    """Reject anything that isn't a plausible Drive id before it hits a URL."""
    if not _ID_RE.fullmatch(file_id or ""):
        raise DriveError("Invalid Google Drive file id", status=400)
    return file_id


def _drive_creds_path() -> Path:
    """Dedicated credential file, never settings.json.

    Kept separate so (a) a concurrent settings-page save can't clobber the
    refresh token, and (b) it can be locked to 0600 without touching the
    permissions of the much larger settings file.
    """
    from engine.config import PERSISTENT_ROOT
    return PERSISTENT_ROOT / "system" / "drive_credentials.json"


class DriveError(Exception):
    """A Drive API failure with a user-presentable message."""

    def __init__(self, message: str, *, status: int = 0, needs_auth: bool = False):
        super().__init__(message)
        self.status = status
        self.needs_auth = needs_auth


# ── Credential storage ───────────────────────────────────────────────────────


def _read_settings() -> dict[str, Any]:
    p = _drive_creds_path()
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _write_settings(data: dict[str, Any]) -> None:
    """Atomically persist credentials at 0600.

    Holds an OAuth refresh token, client secret, and API key — the file must
    never be world-readable, and the temp file must be created restrictive
    rather than chmod'd afterwards (closing the window where it's 0644).
    """
    p = _drive_creds_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def get_drive_settings() -> dict[str, Any]:
    return _read_settings()


def save_drive_settings(patch: dict[str, Any]) -> dict[str, Any]:
    """Merge `patch` into the credentials file and persist at 0600."""
    data = _read_settings()
    for k, v in patch.items():
        if v is None:
            data.pop(k, None)
        else:
            data[k] = v
    _write_settings(data)
    return data


# ── Client ───────────────────────────────────────────────────────────────────


def _error_reason(resp: httpx.Response) -> str:
    """Pull the machine-readable reason out of a Google error body."""
    try:
        err = resp.json().get("error", {})
        errors = err.get("errors") or []
        if errors and isinstance(errors, list):
            return str(errors[0].get("reason") or "")
        return str(err.get("status") or "")
    except Exception:
        return ""


# Reasons where swapping the API key for an OAuth token can actually help.
_AUTH_FAILURE_REASONS = {
    "forbidden", "authError", "unauthorized",
    "required", "insufficientFilePermissions",
}


def _is_auth_failure(resp: httpx.Response) -> bool:
    """True only when a retry with different credentials could succeed.

    Google returns 403 for auth problems *and* for rate limits / quota. Blindly
    retrying every 403 doubles the request and forces a pointless token refresh.
    """
    if resp.status_code == 401:
        return True
    if resp.status_code != 403:
        return False
    reason = _error_reason(resp)
    if reason in _AUTH_FAILURE_REASONS:
        return True
    # Bare 403 with no parseable reason — treat as auth so the common
    # "needs sign-in" case still falls back.
    return not reason


class DriveClient:
    """Thin async Drive v3 wrapper. Reusable; cheap to construct."""

    def __init__(self, api_key: str | None = None, oauth: dict[str, Any] | None = None):
        self.api_key = (api_key or "").strip() or None
        self.oauth = oauth or {}
        self._token: str | None = None
        self._token_exp: float = 0

    @classmethod
    def from_settings(cls) -> "DriveClient":
        s = get_drive_settings()
        return cls(api_key=s.get("api_key"), oauth=s.get("oauth") or {})

    # ── Auth ──

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key)

    @property
    def has_oauth(self) -> bool:
        return bool(self.oauth.get("refresh_token"))

    @property
    def connected(self) -> bool:
        return self.has_api_key or self.has_oauth

    async def _access_token(self) -> str:
        """Return a valid OAuth access token, refreshing when near expiry."""
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        refresh = self.oauth.get("refresh_token")
        client_id = self.oauth.get("client_id")
        if not refresh or not client_id:
            raise DriveError("Google Drive is not connected", status=401, needs_auth=True)
        payload = {
            "client_id": client_id,
            "refresh_token": refresh,
            "grant_type": "refresh_token",
        }
        if self.oauth.get("client_secret"):
            payload["client_secret"] = self.oauth["client_secret"]
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(_TOKEN_URL, data=payload)
        if r.status_code != 200:
            raise DriveError(
                "Google Drive session expired — reconnect the account",
                status=401, needs_auth=True,
            )
        tok = r.json()
        self._token = tok["access_token"]
        self._token_exp = time.time() + int(tok.get("expires_in", 3600))
        return self._token

    async def _auth(self, *, prefer: str = "key") -> tuple[dict[str, str], dict[str, str]]:
        """Resolve headers + query params for a request.

        `prefer` picks which credential leads. Public files work with either, so
        the API key (cheaper, no refresh round-trip) wins by default.
        """
        if prefer == "oauth" and self.has_oauth:
            return {"Authorization": f"Bearer {await self._access_token()}"}, {}
        if self.has_api_key:
            return {}, {"key": self.api_key or ""}
        if self.has_oauth:
            return {"Authorization": f"Bearer {await self._access_token()}"}, {}
        raise DriveError("Google Drive is not configured", status=401, needs_auth=True)

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        prefer: str = "key",
        **kwargs: Any,
    ) -> httpx.Response:
        """Authenticated request with one automatic auth fallback.

        A 403 on the key path that is specifically an authentication failure
        means the file needs sign-in — retry once with the OAuth token. Other
        403s (rate limit, insufficient permission, quota) are permanent for
        this request, so retrying would just burn a token refresh.
        """
        headers, auth_params = await self._auth(prefer=prefer)
        merged = {**(params or {}), **auth_params}
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as c:
            r = await c.request(method, url, params=merged, headers=headers, **kwargs)
        if prefer == "key" and self.has_oauth and _is_auth_failure(r):
            headers, _ = await self._auth(prefer="oauth")
            async with httpx.AsyncClient(timeout=120, follow_redirects=True) as c:
                r = await c.request(method, url, params=params, headers=headers, **kwargs)
        return r

    @staticmethod
    def _raise(resp: httpx.Response) -> None:
        if resp.status_code < 400:
            return
        detail = ""
        try:
            detail = resp.json().get("error", {}).get("message", "")
        except Exception:
            detail = resp.text[:200]
        needs_auth = resp.status_code in (401, 403)
        if resp.status_code == 404:
            raise DriveError("Not found — check the link and sharing settings", status=404)
        if needs_auth and "has not been used" in detail:
            raise DriveError(
                "Drive API is not enabled for this key. Enable it in Google Cloud Console.",
                status=resp.status_code, needs_auth=True,
            )
        raise DriveError(detail or f"Drive API error ({resp.status_code})", status=resp.status_code, needs_auth=needs_auth)

    # ── URL parsing ──

    @staticmethod
    def parse_id(url_or_id: str) -> tuple[str, str]:
        """Extract (kind, id) from any Drive share URL or a bare id.

        Handles /file/d/<id>, /folders/<id>, open?id=, uc?id=, and raw ids.
        Returns kind 'file' or 'folder' (unknown ids default to 'file' — the
        first metadata fetch corrects it).
        """
        s = (url_or_id or "").strip()
        if not s:
            raise DriveError("No Drive link provided", status=400)
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
        except DriveError:
            raise
        except Exception:
            pass
        raise DriveError("Could not parse a Drive file or folder id from that link", status=400)

    # ── Metadata / listing ──

    async def get_file(self, file_id: str) -> dict[str, Any]:
        _check_id(file_id)
        r = await self._request(
            "GET", f"{_API}/files/{file_id}",
            params={"fields": _FILE_FIELDS, "supportsAllDrives": "true"},
        )
        self._raise(r)
        return r.json()

    async def list_children(self, folder_id: str) -> list[dict[str, Any]]:
        """Non-trashed children of a folder, folders first then name.

        Capped at _MAX_LIST_ITEMS so a hostile share with a huge folder can't
        pin the event loop enumerating it forever.
        """
        _check_id(folder_id)
        out: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {
                "q": f"'{folder_id}' in parents and trashed = false",
                "fields": f"nextPageToken,files({_FILE_FIELDS})",
                "pageSize": "1000",
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
                "orderBy": "folder,name",
            }
            if page_token:
                params["pageToken"] = page_token
            r = await self._request("GET", f"{_API}/files", params=params)
            self._raise(r)
            data = r.json()
            out.extend(data.get("files", []))
            if len(out) >= _MAX_LIST_ITEMS:
                logger.warning("list_children truncated at %d items", _MAX_LIST_ITEMS)
                return out[:_MAX_LIST_ITEMS]
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return out

    async def resolve(self, url_or_id: str) -> dict[str, Any]:
        """Resolve a link/id to a metadata record, correcting file vs folder."""
        _, fid = self.parse_id(url_or_id)
        meta = await self.get_file(fid)
        meta["kind"] = "folder" if meta.get("mimeType") == _FOLDER_MIME else "file"
        return meta

    async def breadcrumb(self, file_id: str, limit: int = 20) -> list[dict[str, str]]:
        """Walk parents up to the root, returning [{id,name}] root→leaf."""
        chain: list[dict[str, str]] = []
        current = file_id
        for _ in range(limit):
            try:
                meta = await self.get_file(current)
            except DriveError:
                break
            parents = meta.get("parents") or []
            if not parents:
                break
            current = parents[0]
            try:
                parent = await self.get_file(current)
            except DriveError:
                break
            chain.append({"id": parent["id"], "name": parent.get("name", "")})
            if parent.get("id") == file_id:
                break
        return list(reversed(chain))

    # ── Download / export ──

    @staticmethod
    def is_google_native(mime: str) -> bool:
        return mime in _EXPORT_MAP

    @staticmethod
    def export_mime(mime: str) -> tuple[str, str]:
        return _EXPORT_MAP.get(mime, ("application/pdf", ".pdf"))

    async def download(self, meta: dict[str, Any]) -> tuple[bytes, str]:
        """Fetch a file's bytes. Returns (raw, filename).

        Google-native docs are exported (they have no raw bytes); everything
        else streams via alt=media. The stream is capped at _MAX_DOWNLOAD while
        it arrives, so an oversized share is rejected without ever being fully
        buffered. Shortcuts are resolved to their target first.
        """
        mime = meta.get("mimeType", "")
        if mime == _SHORTCUT_MIME:
            target = (meta.get("shortcutDetails") or {}).get("targetId")
            if not target:
                raise DriveError("Shortcut has no target", status=422)
            meta = await self.get_file(target)
            mime = meta.get("mimeType", "")

        caps = meta.get("capabilities") or {}
        if caps and caps.get("canDownload") is False:
            raise DriveError(
                f"'{meta.get('name')}' is view-only — the owner disabled downloads",
                status=403,
            )

        fid = _check_id(meta["id"])
        if self.is_google_native(mime):
            export_mime, ext = self.export_mime(mime)
            r = await self._request(
                "GET", f"{_API}/files/{fid}/export",
                params={"mimeType": export_mime},
            )
            self._raise(r)
            if len(r.content) > _MAX_DOWNLOAD:
                raise DriveError("Export too large (max 100 MB)", status=413)
            name = meta.get("name", "document")
            if not name.lower().endswith(ext):
                name += ext
            return r.content, name

        # Regular file — reject early on the advertised size when available,
        # then stream with a hard cap as a backstop.
        declared = int(meta.get("size") or 0)
        if declared > _MAX_DOWNLOAD:
            raise DriveError(
                f"'{meta.get('name')}' is too large ({declared // (1024*1024)} MB, max 100 MB)",
                status=413,
            )

        buf = bytearray()
        async for chunk in self.iter_download(meta):
            buf.extend(chunk)
            if len(buf) > _MAX_DOWNLOAD:
                raise DriveError("File too large (max 100 MB)", status=413)
        return bytes(buf), meta.get("name") or f"{fid}.bin"

    async def iter_download(self, meta: dict[str, Any]) -> AsyncIterator[bytes]:
        """Stream a regular file's bytes without buffering it all at once.

        Google-native docs are materialized via export first (they can't be
        streamed). The caller is responsible for enforcing the total-size cap.
        """
        mime = meta.get("mimeType", "")
        if self.is_google_native(mime) or mime == _SHORTCUT_MIME:
            raw, _ = await self.download(meta)
            yield raw
            return
        fid = _check_id(meta["id"])
        headers, auth_params = await self._auth(prefer="key")
        params = {"alt": "media", "supportsAllDrives": "true", **auth_params}
        async with httpx.AsyncClient(timeout=None, follow_redirects=True) as c:
            async with c.stream("GET", f"{_API}/files/{fid}", params=params, headers=headers) as r:
                if r.status_code >= 400:
                    await r.aread()
                    self._raise(r)
                async for chunk in r.aiter_bytes(1024 * 256):
                    yield chunk

    # ── Change detection (polling) ──

    async def start_page_token(self) -> str:
        r = await self._request(
            "GET", f"{_API}/changes/startPageToken",
            params={"supportsAllDrives": "true"},
        )
        self._raise(r)
        return r.json()["startPageToken"]

    async def list_changes(self, page_token: str) -> tuple[list[dict[str, Any]], str]:
        """Return (changed_files, next_token)."""
        r = await self._request(
            "GET", f"{_API}/changes",
            params={
                "pageToken": page_token,
                "fields": f"nextPageToken,newStartPageToken,changes(fileId,removed,file({_FILE_FIELDS}))",
                "pageSize": "200",
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            },
        )
        self._raise(r)
        data = r.json()
        files = [c["file"] for c in data.get("changes", []) if c.get("file") and not c.get("removed")]
        return files, data.get("newStartPageToken") or data.get("nextPageToken") or page_token

    # ── Stats ──

    async def about(self) -> dict[str, Any]:
        r = await self._request(
            "GET", f"{_API}/about",
            params={"fields": "user(displayName,emailAddress,photoLink),storageQuota(limit,usage)"},
        )
        self._raise(r)
        return r.json()


# ── OAuth helpers (PKCE + loopback) ──────────────────────────────────────────


def build_auth_url(client_id: str, redirect_uri: str, state: str) -> tuple[str, str]:
    """Return (auth_url, code_verifier). PKCE — no client secret required."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{_AUTH_URL}?{urllib.parse.urlencode(params)}", verifier


async def exchange_code(
    code: str, client_id: str, client_secret: str, redirect_uri: str, verifier: str,
) -> dict[str, Any]:
    payload = {
        "code": code,
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    }
    if client_secret:
        payload["client_secret"] = client_secret
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(_TOKEN_URL, data=payload)
    if r.status_code != 200:
        raise DriveError(f"Token exchange failed: {r.text[:200]}", status=r.status_code)
    return r.json()


async def revoke_token(token: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            await c.post("https://oauth2.googleapis.com/revoke", params={"token": token})
    except Exception as exc:
        logger.warning("Drive token revoke failed: %s", exc)


def get_drive_client() -> DriveClient:
    """Fresh client bound to the currently saved credentials."""
    return DriveClient.from_settings()
