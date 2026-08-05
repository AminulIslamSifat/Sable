
"""Email endpoints — IMAP read / SMTP send via Library panel."""

from __future__ import annotations

import email
import email.utils
import imaplib
import json
import smtplib
from email.header import decode_header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from engine.config import _ROOT

router = APIRouter()

_CONFIG_PATH = _ROOT / "system" / ".email_config.json"


# ── Models ────────────────────────────────────────────────────────────────────

class EmailConfig(BaseModel):
    imap_host: str
    imap_port: int = 993
    smtp_host: str
    smtp_port: int = 587
    username: str
    password: str  # app password
    use_ssl: bool = True


class SendEmailRequest(BaseModel):
    to: str
    subject: str
    body: str
    cc: str | None = None
    html: bool = False


# ── Config helpers ────────────────────────────────────────────────────────────

def _load_config() -> dict[str, Any] | None:
    if not _CONFIG_PATH.exists():
        return None
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_config(cfg: dict[str, Any]) -> None:
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def _quote_folder(name: str) -> str:
    """Quote folder name for IMAP SELECT if it contains special chars."""
    if any(c in name for c in ' /[]"\\'):
        return f'"{name}"'
    return name


def _find_sent_folder(cfg: dict[str, Any]) -> str | None:
    """Find the Sent folder name for the mail provider."""
    common_names = [
        "[Gmail]/Sent Mail",  # Gmail
        "Sent",               # Outlook/Yahoo
        "Sent Items",         # Outlook alternate
        "Sent Messages",      # Some providers
        "INBOX.Sent",         # Some IMAP servers
    ]
    try:
        conn = _get_imap(cfg)
        status, folders = conn.list()
        conn.logout()
        if status != "OK":
            return None
        available = []
        for f in folders:
            if isinstance(f, bytes):
                decoded = f.decode("utf-8", errors="replace")
                parts = decoded.split(' "/" ')
                if len(parts) == 2:
                    available.append(parts[1].strip('"'))
                else:
                    parts = decoded.split(' "." ')
                    if len(parts) == 2:
                        available.append(parts[1].strip('"'))
        for name in common_names:
            if name in available:
                return name
        return None
    except Exception:
        return None


def _get_imap(cfg: dict[str, Any]) -> imaplib.IMAP4_SSL | imaplib.IMAP4:
    if cfg.get("use_ssl", True):
        conn = imaplib.IMAP4_SSL(cfg["imap_host"], cfg.get("imap_port", 993))
    else:
        conn = imaplib.IMAP4(cfg["imap_host"], cfg.get("imap_port", 143))
    conn.login(cfg["username"], cfg["password"])
    return conn


def _decode_mime_header(raw: str | None) -> str:
    if not raw:
        return ""
    parts = decode_header(raw)
    decoded = []
    for data, charset in parts:
        if isinstance(data, bytes):
            decoded.append(data.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(data)
    return "".join(decoded)


def _extract_body(msg: email.message.Message) -> str:
    """Extract plain text body from email message."""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in cd:
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace")
        # fallback: try html
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if ct == "text/html" and "attachment" not in cd:
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
    return ""


def _list_attachments(msg: email.message.Message) -> list[dict[str, Any]]:
    attachments = []
    if msg.is_multipart():
        for part in msg.walk():
            cd = str(part.get("Content-Disposition", ""))
            if "attachment" in cd:
                filename = part.get_filename()
                if filename:
                    attachments.append({
                        "filename": _decode_mime_header(filename),
                        "size": len(part.get_payload(decode=True) or b""),
                        "content_type": part.get_content_type(),
                    })
    return attachments


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/api/email/configured")
def email_configured() -> dict[str, Any]:
    """Check if email is configured (no password exposed)."""
    cfg = _load_config()
    if not cfg:
        return {"configured": False}
    return {
        "configured": True,
        "username": cfg.get("username", ""),
        "imap_host": cfg.get("imap_host", ""),
        "smtp_host": cfg.get("smtp_host", ""),
    }


@router.post("/api/email/config")
def save_email_config(cfg: EmailConfig) -> dict[str, Any]:
    """Save email credentials. Tests connection before saving."""
    # Test IMAP connection
    try:
        conn = _get_imap(cfg.model_dump())
        conn.logout()
    except imaplib.IMAP4.error as e:
        raise HTTPException(status_code=400, detail=f"IMAP login failed: {e}")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"IMAP connection failed: {e}")

    _save_config(cfg.model_dump())
    return {"status": "saved", "username": cfg.username}


@router.delete("/api/email/config")
def delete_email_config() -> dict[str, Any]:
    if _CONFIG_PATH.exists():
        _CONFIG_PATH.unlink()
    return {"status": "deleted"}


@router.get("/api/email/folders")
def email_folders() -> list[str]:
    cfg = _load_config()
    if not cfg:
        raise HTTPException(status_code=400, detail="Email not configured")
    try:
        conn = _get_imap(cfg)
        status, folders = conn.list()
        conn.logout()
        if status != "OK":
            return []
        result = []
        for f in folders:
            if isinstance(f, bytes):
                decoded = f.decode("utf-8", errors="replace")
                # Skip non-selectable folders (e.g. [Gmail] parent)
                if "\\Noselect" in decoded or "\\NonExistent" in decoded:
                    continue
                # Parse folder name — split on delimiter
                parts = decoded.split(' "/" ')
                if len(parts) == 2:
                    name = parts[1].strip('"')
                    result.append(name)
                else:
                    parts = decoded.split(' "." ')
                    if len(parts) == 2:
                        name = parts[1].strip('"')
                        result.append(name)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/email/messages")
def email_messages(
    folder: str = "INBOX",
    limit: int = 50,
    offset: int = 0,
    search: str | None = None,
) -> dict[str, Any]:
    """Fetch message headers from a folder."""
    cfg = _load_config()
    if not cfg:
        raise HTTPException(status_code=400, detail="Email not configured")

    try:
        conn = _get_imap(cfg)
        status, _ = conn.select(_quote_folder(folder), readonly=True)
        if status != "OK":
            conn.logout()
            raise HTTPException(status_code=400, detail=f"Cannot open folder '{folder}'")

        # Search
        if search:
            criteria = f'(OR SUBJECT "{search}" FROM "{search}")'
        else:
            criteria = "ALL"

        status, data = conn.search(None, criteria)
        if status != "OK":
            conn.logout()
            raise HTTPException(status_code=500, detail="Search failed")

        msg_ids = data[0].split()
        total = len(msg_ids)

        # Reverse for newest first, apply pagination
        msg_ids = msg_ids[::-1][offset:offset + limit]

        messages = []
        for mid in msg_ids:
            status, msg_data = conn.fetch(mid, "(RFC822.HEADER)")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1] if isinstance(msg_data[0], tuple) else msg_data[0]
            msg = email.message_from_bytes(raw)
            messages.append({
                "uid": mid.decode(),
                "from": _decode_mime_header(msg.get("From")),
                "to": _decode_mime_header(msg.get("To")),
                "subject": _decode_mime_header(msg.get("Subject")),
                "date": msg.get("Date", ""),
                "has_attachments": "attachment" in str(msg.get("Content-Disposition", "")).lower()
                    or any("attachment" in str(p.get("Content-Disposition", "")) for p in msg.walk()) if msg.is_multipart() else False,
            })

        conn.logout()
        return {"messages": messages, "total": total, "folder": folder}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/email/message/{uid}")
def email_read_message(uid: str, folder: str = "INBOX") -> dict[str, Any]:
    """Read full message body."""
    cfg = _load_config()
    if not cfg:
        raise HTTPException(status_code=400, detail="Email not configured")

    try:
        conn = _get_imap(cfg)
        status, _ = conn.select(_quote_folder(folder), readonly=True)
        if status != "OK":
            conn.logout()
            raise HTTPException(status_code=400, detail=f"Cannot open folder '{folder}'")
        status, msg_data = conn.fetch(uid.encode(), "(RFC822)")
        if status != "OK" or not msg_data or not msg_data[0]:
            conn.logout()
            raise HTTPException(status_code=404, detail="Message not found")

        raw = msg_data[0][1] if isinstance(msg_data[0], tuple) else msg_data[0]
        msg = email.message_from_bytes(raw)
        conn.logout()

        return {
            "uid": uid,
            "from": _decode_mime_header(msg.get("From")),
            "to": _decode_mime_header(msg.get("To")),
            "cc": _decode_mime_header(msg.get("Cc")),
            "subject": _decode_mime_header(msg.get("Subject")),
            "date": msg.get("Date", ""),
            "body": _extract_body(msg),
            "attachments": _list_attachments(msg),
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/email/send")
def email_send(req: SendEmailRequest) -> dict[str, Any]:
    """Send an email via SMTP."""
    cfg = _load_config()
    if not cfg:
        raise HTTPException(status_code=400, detail="Email not configured")

    try:
        from email.utils import formatdate, make_msgid

        msg = MIMEMultipart()
        # Use display-name format for better deliverability
        sender_name = cfg.get("sender_name", "Sable")
        msg["From"] = f"{sender_name} <{cfg['username']}>"
        msg["To"] = req.to
        msg["Subject"] = req.subject
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain="gmail.com")
        msg["Reply-To"] = cfg["username"]
        msg["X-Mailer"] = "Sable Mail Client"
        if req.cc:
            msg["Cc"] = req.cc

        content_type = "html" if req.html else "plain"
        msg.attach(MIMEText(req.body, content_type, "utf-8"))

        recipients = [req.to]
        if req.cc:
            recipients.extend([a.strip() for a in req.cc.split(",")])

        smtp_port = cfg.get("smtp_port", 587)
        if smtp_port == 465:
            # Direct SSL connection (port 465)
            with smtplib.SMTP_SSL(cfg["smtp_host"], smtp_port) as server:
                server.ehlo()
                server.login(cfg["username"], cfg["password"])
                server.sendmail(cfg["username"], recipients, msg.as_string())
        else:
            # STARTTLS (port 587)
            with smtplib.SMTP(cfg["smtp_host"], smtp_port) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(cfg["username"], cfg["password"])
                server.sendmail(cfg["username"], recipients, msg.as_string())

        # Save copy to Sent folder via IMAP
        try:
            sent_folder = _find_sent_folder(cfg)
            if sent_folder:
                conn = _get_imap(cfg)
                conn.select(_quote_folder(sent_folder))
                conn.append(_quote_folder(sent_folder), "\\Seen", None, msg.as_bytes())
                conn.logout()
        except Exception:
            pass  # Non-fatal: email was sent, just couldn't save copy

        return {"status": "sent", "to": req.to, "subject": req.subject}

    except smtplib.SMTPAuthenticationError as e:
        raise HTTPException(status_code=401, detail=f"SMTP auth failed: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Send failed: {e}")


@router.delete("/api/email/message/{uid}")
def email_delete_message(uid: str, folder: str = "INBOX") -> dict[str, Any]:
    """Delete (move to Trash) a message."""
    cfg = _load_config()
    if not cfg:
        raise HTTPException(status_code=400, detail="Email not configured")

    try:
        conn = _get_imap(cfg)
        status, _ = conn.select(_quote_folder(folder))
        if status != "OK":
            conn.logout()
            raise HTTPException(status_code=400, detail=f"Cannot open folder '{folder}'")
        conn.store(uid.encode(), "+FLAGS", "\\Deleted")
        conn.expunge()
        conn.logout()
        return {"status": "deleted", "uid": uid}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
