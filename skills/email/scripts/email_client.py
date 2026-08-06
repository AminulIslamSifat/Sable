
#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Email CLI — IMAP read / SMTP send using stdlib only.

Reads credentials from system/.email_config.json (same format as the Sable server).
All output is JSON for agent consumption.
"""

from __future__ import annotations

import argparse
import email
import imaplib
import json
import smtplib
import sys
from email import encoders
from email.header import decode_header
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Any

# ── Config ────────────────────────────────────────────────────────────────────

_CONFIG_PATHS = [
    Path(__file__).resolve().parents[3] / "system" / ".email_config.json",  # PROJECT_ROOT/system/
    Path.home() / "hdd/projects/Sable/system/.email_config.json",
]


def _load_config() -> dict[str, Any]:
    for p in _CONFIG_PATHS:
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
    print(json.dumps({"error": "No .email_config.json found. Configure email first."}))
    sys.exit(1)


# ── Helpers ───────────────────────────────────────────────────────────────────

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


def _get_imap(cfg: dict[str, Any]) -> imaplib.IMAP4_SSL | imaplib.IMAP4:
    if cfg.get("use_ssl", True):
        conn = imaplib.IMAP4_SSL(cfg["imap_host"], cfg.get("imap_port", 993))
    else:
        conn = imaplib.IMAP4(cfg["imap_host"], cfg.get("imap_port", 143))
    conn.login(cfg["username"], cfg["password"])
    return conn


def _quote_folder(name: str) -> str:
    if any(c in name for c in ' /[]"\\'):
        return f'"{name}"'
    return name


def _extract_body(msg: email.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in cd:
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace")
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


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_status(cfg: dict[str, Any]) -> dict[str, Any]:
    """Test IMAP connection and report status."""
    try:
        conn = _get_imap(cfg)
        conn.logout()
        return {
            "configured": True,
            "connected": True,
            "username": cfg.get("username", ""),
            "imap_host": cfg.get("imap_host", ""),
            "smtp_host": cfg.get("smtp_host", ""),
        }
    except Exception as e:
        return {"configured": True, "connected": False, "error": str(e)}


def cmd_folders(cfg: dict[str, Any]) -> list[str]:
    """List mailbox folders."""
    conn = _get_imap(cfg)
    status, folders = conn.list()
    conn.logout()
    if status != "OK":
        return []
    result = []
    for f in folders:
        if isinstance(f, bytes):
            decoded = f.decode("utf-8", errors="replace")
            if "\\Noselect" in decoded or "\\NonExistent" in decoded:
                continue
            parts = decoded.split(' "/" ')
            if len(parts) == 2:
                result.append(parts[1].strip('"'))
            else:
                parts = decoded.split(' "." ')
                if len(parts) == 2:
                    result.append(parts[1].strip('"'))
    return result


def cmd_list(cfg: dict[str, Any], folder: str, limit: int, offset: int, search: str | None) -> dict[str, Any]:
    """List message headers from a folder."""
    conn = _get_imap(cfg)
    status, _ = conn.select(_quote_folder(folder), readonly=True)
    if status != "OK":
        conn.logout()
        return {"error": f"Cannot open folder '{folder}'"}

    criteria = f'(OR SUBJECT "{search}" FROM "{search}")' if search else "ALL"
    status, data = conn.search(None, criteria)
    if status != "OK":
        conn.logout()
        return {"error": "Search failed"}

    msg_ids = data[0].split()
    total = len(msg_ids)
    msg_ids = msg_ids[::-1][offset:offset + limit]

    messages = []
    for mid in msg_ids:
        status, msg_data = conn.fetch(mid, "(RFC822.HEADER)")
        if status != "OK" or not msg_data or not msg_data[0]:
            continue
        raw = msg_data[0][1] if isinstance(msg_data[0], tuple) else msg_data[0]
        msg = email.message_from_bytes(raw)
        has_attach = False
        if msg.is_multipart():
            has_attach = any("attachment" in str(p.get("Content-Disposition", "")).lower() for p in msg.walk())
        messages.append({
            "uid": mid.decode(),
            "from": _decode_mime_header(msg.get("From")),
            "to": _decode_mime_header(msg.get("To")),
            "subject": _decode_mime_header(msg.get("Subject")),
            "date": msg.get("Date", ""),
            "has_attachments": has_attach,
        })

    conn.logout()
    return {"messages": messages, "total": total, "folder": folder}


def cmd_read(cfg: dict[str, Any], uid: str, folder: str) -> dict[str, Any]:
    """Read full message body by UID."""
    conn = _get_imap(cfg)
    status, _ = conn.select(_quote_folder(folder), readonly=True)
    if status != "OK":
        conn.logout()
        return {"error": f"Cannot open folder '{folder}'"}

    status, msg_data = conn.fetch(uid.encode(), "(RFC822)")
    if status != "OK" or not msg_data or not msg_data[0]:
        conn.logout()
        return {"error": "Message not found"}

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


def cmd_send(cfg: dict[str, Any], to: str, subject: str, body: str, cc: str | None, html: bool) -> dict[str, Any]:
    """Send an email via SMTP."""
    msg = MIMEMultipart()
    sender_name = cfg.get("sender_name", "Sable")
    msg["From"] = f"{sender_name} <{cfg['username']}>"
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    if cc:
        msg["Cc"] = cc

    subtype = "html" if html else "plain"
    msg.attach(MIMEText(body, subtype, "utf-8"))

    recipients = [to]
    if cc:
        recipients.extend(a.strip() for a in cc.split(","))

    try:
        port = cfg.get("smtp_port", 587)
        if port == 465:
            srv = smtplib.SMTP_SSL(cfg["smtp_host"], port)
        else:
            srv = smtplib.SMTP(cfg["smtp_host"], port)
            srv.ehlo()
            srv.starttls()
            srv.ehlo()
        srv.login(cfg["username"], cfg["password"])
        srv.sendmail(cfg["username"], recipients, msg.as_string())
        srv.quit()
        return {"ok": True, "to": to, "subject": subject}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Email CLI for Sable")
    sub = parser.add_subparsers(dest="command", required=True)

    # status
    sub.add_parser("status", help="Test connection and show config status")

    # folders
    sub.add_parser("folders", help="List mailbox folders")

    # list
    p_list = sub.add_parser("list", help="List messages in a folder")
    p_list.add_argument("--folder", default="INBOX")
    p_list.add_argument("--limit", type=int, default=20)
    p_list.add_argument("--offset", type=int, default=0)
    p_list.add_argument("--search", default=None)

    # read
    p_read = sub.add_parser("read", help="Read full message by UID")
    p_read.add_argument("uid", help="Message UID")
    p_read.add_argument("--folder", default="INBOX")

    # send
    p_send = sub.add_parser("send", help="Send an email")
    p_send.add_argument("--to", required=True)
    p_send.add_argument("--subject", required=True)
    p_send.add_argument("--body", required=True)
    p_send.add_argument("--cc", default=None)
    p_send.add_argument("--html", action="store_true")

    args = parser.parse_args()
    cfg = _load_config()

    if args.command == "status":
        result = cmd_status(cfg)
    elif args.command == "folders":
        result = cmd_folders(cfg)
    elif args.command == "list":
        result = cmd_list(cfg, args.folder, args.limit, args.offset, args.search)
    elif args.command == "read":
        result = cmd_read(cfg, args.uid, args.folder)
    elif args.command == "send":
        result = cmd_send(cfg, args.to, args.subject, args.body, args.cc, args.html)
    else:
        result = {"error": f"Unknown command: {args.command}"}

    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
