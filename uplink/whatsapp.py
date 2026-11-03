"""WhatsApp Cloud API: sending messages, downloading voice notes, reading webhooks."""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Iterator, Literal, Mapping

from .transport import Transport, TransportError

MAX_TEXT = 4096
MAX_TEMPLATE_PARAMETER = 1000

# What the most common Cloud API errors mean for someone setting up a test number.
HINTS = {
    100: "A parameter was rejected. WHATSAPP_PHONE_NUMBER_ID must be the phone number ID from API Setup, not the phone number.",
    190: "The access token expired or is wrong. Temporary tokens last 24 hours: generate a new one in API Setup, or use a system user token.",
    131026: "WhatsApp could not deliver to that number. Check that it has WhatsApp and has accepted the latest terms.",
    131030: "Your number is not on the test number's recipient list. Add it under WhatsApp > API Setup > To, then confirm the code WhatsApp sends you.",
    131047: 'More than 24 hours since your number last messaged the business number. Send it any message ("hi") on WhatsApp, or use an approved template (WHATSAPP_ALERT_TEMPLATE).',
    131056: "Too many messages to the same number in a short time. Wait a minute.",
    130429: "The Cloud API rate limit was reached. Wait a minute.",
    132000: "The template parameters did not match the template.",
    132001: "The template does not exist in that language, or is not approved yet.",
    133010: "The business phone number is not registered with the Cloud API.",
}


class WhatsAppError(Exception):
    def __init__(self, message: str, *, code: int | None = None, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status

    @property
    def hint(self) -> str | None:
        return HINTS.get(self.code) if self.code is not None else None


@dataclass(frozen=True)
class Media:
    content: bytes
    mime_type: str


@dataclass(frozen=True)
class InboundMessage:
    id: str
    sender: str
    kind: Literal["text", "audio", "other"]
    sent_at: datetime | None
    text: str = ""
    media_id: str = ""
    media_url: str = ""
    mime_type: str = ""
    sender_name: str = ""


@dataclass(frozen=True)
class DeliveryStatus:
    message_id: str
    recipient: str
    status: str
    error_code: int | None = None
    error_title: str = ""


class WhatsAppClient:
    def __init__(
        self,
        *,
        token: str,
        phone_number_id: str,
        transport: Transport,
        api_version: str = "v26.0",
        base_url: str = "https://graph.facebook.com",
    ) -> None:
        self._headers = {"Authorization": f"Bearer {token}"}
        self._transport = transport
        self._api = f"{base_url.rstrip('/')}/{api_version}"
        self._messages_url = f"{self._api}/{phone_number_id}/messages"

    def send_text(self, to: str, body: str, *, reply_to: str | None = None) -> str:
        """Send a free-form text; works within 24 hours of the recipient's last message."""
        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": normalize_number(to),
            "type": "text",
            "text": {"preview_url": False, "body": _clip(body, MAX_TEXT)},
        }
        if reply_to:
            payload["context"] = {"message_id": reply_to}
        return self._send(payload)

    def send_template(self, to: str, name: str, language: str = "en_US", body_parameters: tuple[str, ...] = ()) -> str:
        """Send an approved template; templates also reach people outside the 24-hour window."""
        template: dict[str, Any] = {"name": name, "language": {"code": language}}
        if body_parameters:
            template["components"] = [
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": template_safe(p)} for p in body_parameters],
                }
            ]
        return self._send(
            {"messaging_product": "whatsapp", "to": normalize_number(to), "type": "template", "template": template}
        )

    def mark_read(self, message_id: str) -> None:
        self._send({"messaging_product": "whatsapp", "status": "read", "message_id": message_id}, expect_id=False)

    def download_media(self, media_id: str, url: str = "") -> Media:
        """Fetch a voice note: look up its short-lived URL (unless the webhook sent one), then download."""
        mime_type = ""
        try:
            if not url:
                info = self._transport.json("GET", f"{self._api}/{media_id}", headers=self._headers)
                url, mime_type = info.get("url", ""), info.get("mime_type", "")
                if not url:
                    raise WhatsAppError(f"media {media_id} has no download URL")
            response = self._transport.request("GET", url, headers=self._headers, timeout=60)
        except TransportError as exc:
            raise _as_whatsapp_error(exc) from None
        return Media(response.body, mime_type or response.header("Content-Type") or "application/octet-stream")

    def _send(self, payload: dict[str, Any], *, expect_id: bool = True) -> str:
        try:
            data = self._transport.json("POST", self._messages_url, payload, headers=self._headers)
        except TransportError as exc:
            raise _as_whatsapp_error(exc) from None
        if not expect_id:
            return ""
        try:
            return str(data["messages"][0]["id"])
        except (KeyError, IndexError, TypeError):
            raise WhatsAppError("WhatsApp accepted the request but returned no message id") from None


def verify_signature(app_secret: str, body: bytes, header: str | None) -> bool:
    """Check X-Hub-Signature-256, which Meta computes over the raw body with the app secret."""
    if not app_secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256=").strip().lower())


def parse_webhook(payload: Any) -> tuple[list[InboundMessage], list[DeliveryStatus]]:
    """Pull messages and delivery statuses out of a webhook body, ignoring anything unexpected."""
    messages: list[InboundMessage] = []
    statuses: list[DeliveryStatus] = []
    for value in _change_values(payload):
        names = {c.get("wa_id"): (c.get("profile") or {}).get("name", "") for c in _dicts(value.get("contacts"))}
        for raw in _dicts(value.get("messages")):
            message = _parse_message(raw, names)
            if message:
                messages.append(message)
        for raw in _dicts(value.get("statuses")):
            errors = list(_dicts(raw.get("errors")))
            code = errors[0].get("code") if errors else None
            statuses.append(
                DeliveryStatus(
                    message_id=str(raw.get("id", "")),
                    recipient=str(raw.get("recipient_id", "")),
                    status=str(raw.get("status", "")),
                    error_code=code if isinstance(code, int) else None,
                    error_title=str(errors[0].get("title", "")) if errors else "",
                )
            )
    return messages, statuses


def normalize_number(number: str) -> str:
    """Digits only: the form webhooks use for `from`, and that the API accepts for `to`."""
    return re.sub(r"\D", "", number)


def template_safe(text: str) -> str:
    """Template parameters may not contain newlines, tabs or long runs of spaces."""
    flattened = re.sub(r"\s*\n\s*", " · ", text.strip())
    flattened = re.sub(r"[\t ]{2,}", " ", flattened.replace("\t", " "))
    return _clip(flattened, MAX_TEMPLATE_PARAMETER)


def _parse_message(raw: Mapping[str, Any], names: Mapping[Any, str]) -> InboundMessage | None:
    message_id, sender = raw.get("id"), raw.get("from")
    if not isinstance(message_id, str) or not isinstance(sender, str):
        return None
    timestamp = str(raw.get("timestamp", ""))
    message = InboundMessage(
        id=message_id,
        sender=normalize_number(sender),
        kind="other",
        sent_at=datetime.fromtimestamp(int(timestamp), timezone.utc) if timestamp.isdigit() else None,
        sender_name=names.get(sender, ""),
    )
    if raw.get("type") == "text":
        return replace(message, kind="text", text=str((raw.get("text") or {}).get("body", "")))
    if raw.get("type") == "audio":
        audio = raw.get("audio") or {}
        return replace(
            message,
            kind="audio",
            media_id=str(audio.get("id", "")),
            media_url=str(audio.get("url", "")),
            mime_type=str(audio.get("mime_type", "")),
        )
    return message


def _change_values(payload: Any) -> Iterator[Mapping[str, Any]]:
    if not isinstance(payload, dict) or payload.get("object") != "whatsapp_business_account":
        return
    for entry in _dicts(payload.get("entry")):
        for change in _dicts(entry.get("changes")):
            value = change.get("value")
            if change.get("field") == "messages" and isinstance(value, dict):
                yield value


def _dicts(items: Any) -> Iterator[dict[str, Any]]:
    if isinstance(items, list):
        yield from (item for item in items if isinstance(item, dict))


def _as_whatsapp_error(error: TransportError) -> WhatsAppError:
    detail = error.json()
    inner = detail.get("error") if isinstance(detail, dict) else None
    if isinstance(inner, dict):
        code = inner.get("code")
        message = inner.get("error_user_msg") or inner.get("message") or str(error)
        return WhatsAppError(
            f"{message} (code {code})", code=code if isinstance(code, int) else None, status=error.status
        )
    return WhatsAppError(str(error), status=error.status)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
