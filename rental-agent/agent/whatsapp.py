"""WhatsApp Cloud API (Meta): webhook parsing, signature check, sending messages."""

import hashlib
import hmac
import json
import logging
import urllib.request
from dataclasses import dataclass

log = logging.getLogger(__name__)

WHATSAPP_MAX_TEXT = 4096


@dataclass
class IncomingMessage:
    message_id: str
    phone: str
    profile_name: str | None
    kind: str  # "text", "image", "audio", "location", ...
    text: str | None


def verify_signature(app_secret: str, body: bytes, header: str | None) -> bool:
    """Check Meta's X-Hub-Signature-256 header. Skipped only when no secret is configured."""
    if not app_secret:
        return True
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def parse_webhook(payload: dict) -> list[IncomingMessage]:
    """Pull customer messages out of a webhook payload; ignores delivery/read statuses."""
    out = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            names = {c.get("wa_id"): c.get("profile", {}).get("name") for c in value.get("contacts", [])}
            for msg in value.get("messages", []):
                kind = msg.get("type", "unknown")
                text = None
                if kind == "text":
                    text = msg.get("text", {}).get("body")
                elif kind == "button":
                    text = msg.get("button", {}).get("text")
                elif kind == "interactive":
                    inter = msg.get("interactive", {})
                    reply = inter.get("button_reply") or inter.get("list_reply") or {}
                    text = reply.get("title")
                elif kind in ("image", "video", "document"):
                    text = msg.get(kind, {}).get("caption")
                out.append(
                    IncomingMessage(
                        message_id=msg["id"],
                        phone=msg["from"],
                        profile_name=names.get(msg["from"]),
                        kind=kind,
                        text=text,
                    )
                )
    return out


def send_text(token: str, phone_number_id: str, api_version: str, to: str, text: str) -> None:
    url = f"https://graph.facebook.com/{api_version}/{phone_number_id}/messages"
    for i in range(0, len(text), WHATSAPP_MAX_TEXT):
        body = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": text[i : i + WHATSAPP_MAX_TEXT], "preview_url": False},
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            log.info("sent to %s: HTTP %s", to, resp.status)
