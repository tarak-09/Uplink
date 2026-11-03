"""WhatsApp through Twilio's Sandbox, for when a Meta developer account is not available.

It does the same job as `whatsapp.WhatsAppClient` (send a text, fetch a voice note) with
different plumbing: form-encoded requests with HTTP Basic auth, and webhooks signed with
HMAC-SHA1 (X-Twilio-Signature) instead of Meta's HMAC-SHA256.

Sandbox rules worth knowing: your number joins by sending "join <code>" to +1 415 523 8886;
the membership lasts three days; free text reaches you within 24 hours of your last message.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Iterable, Mapping
from dataclasses import replace
from urllib.parse import urlencode

from .transport import Transport, TransportError
from .whatsapp import InboundMessage, Media, WhatsAppError, normalize_number

SANDBOX_NUMBER = "14155238886"
MAX_BODY = 1600  # Twilio's limit for a WhatsApp message body

HINTS = {
    20003: "Twilio rejected the credentials. Copy TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN from the Twilio Console home page.",
    21211: "WHATSAPP_TO is not a valid number. Use digits with the country code, for example 14155550123.",
    63007: "Twilio has no WhatsApp sender for TWILIO_WHATSAPP_FROM. For the sandbox it is 14155238886.",
    63015: 'Your number has not joined the sandbox, or its three days ran out. From WhatsApp, send "join <your code>" to +1 415 523 8886.',
    63016: 'More than 24 hours since you last messaged the sandbox. Send it any message ("hi") on WhatsApp.',
}


class TwilioWhatsApp:
    def __init__(
        self,
        *,
        account_sid: str,
        auth_token: str,
        transport: Transport,
        sender: str = SANDBOX_NUMBER,
        base_url: str = "https://api.twilio.com",
    ) -> None:
        credentials = base64.b64encode(f"{account_sid}:{auth_token}".encode()).decode()
        self._headers = {"Authorization": f"Basic {credentials}"}
        self._transport = transport
        self._messages_url = f"{base_url.rstrip('/')}/2010-04-01/Accounts/{account_sid}/Messages.json"
        self._from = f"whatsapp:+{normalize_number(sender)}"

    def send_text(self, to: str, body: str, *, reply_to: str | None = None) -> str:
        """Send a free-form message. `reply_to` is accepted for symmetry; the sandbox cannot quote."""
        clipped = body if len(body) <= MAX_BODY else body[: MAX_BODY - 1].rstrip() + "…"
        form = {"From": self._from, "To": f"whatsapp:+{normalize_number(to)}", "Body": clipped}
        headers = {**self._headers, "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
        try:
            response = self._transport.request(
                "POST", self._messages_url, headers=headers, body=urlencode(form).encode()
            )
            data = response.json()
        except TransportError as exc:
            raise _as_error(exc) from None
        sid = data.get("sid") if isinstance(data, dict) else None
        if not isinstance(sid, str):
            raise WhatsAppError("Twilio accepted the request but returned no message sid")
        return sid

    def send_template(self, to: str, name: str, language: str = "en_US", body_parameters: tuple[str, ...] = ()) -> str:
        raise WhatsAppError(
            "The Twilio sandbox cannot send your own templates. Message the sandbox number once a day "
            "instead, so alerts arrive as normal messages."
        )

    def download_media(self, media_id: str, url: str = "") -> Media:
        """Fetch a voice note. Twilio redirects to storage; the credentials stay on Twilio's host."""
        if not url:
            raise WhatsAppError(f"no download URL for media {media_id}")
        try:
            response = self._transport.request("GET", url, headers=self._headers, timeout=60)
        except TransportError as exc:
            raise _as_error(exc) from None
        return Media(response.body, response.header("Content-Type") or "application/octet-stream")


def verify_twilio_signature(
    auth_token: str, url: str, params: Iterable[tuple[str, str]], signature: str | None
) -> bool:
    """Check X-Twilio-Signature: base64 HMAC-SHA1 over the URL plus every sorted name and value."""
    if not auth_token or not signature:
        return False
    payload = url + "".join(name + value for name, value in sorted(params))
    expected = base64.b64encode(hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()).decode()
    return hmac.compare_digest(expected, signature.strip())


def parse_twilio_webhook(form: Mapping[str, str]) -> InboundMessage | None:
    """Turn Twilio's incoming-message fields into the same InboundMessage the Cloud API produces."""
    message_id = form.get("MessageSid") or form.get("SmsMessageSid") or ""
    sender = form.get("From", "")
    if not message_id or not sender.startswith("whatsapp:"):
        return None
    message = InboundMessage(
        id=message_id,
        sender=normalize_number(sender),
        kind="other",
        sent_at=None,
        sender_name=form.get("ProfileName", ""),
    )
    has_media = _count(form.get("NumMedia")) > 0
    media_type = form.get("MediaContentType0", "")
    if has_media and media_type.startswith("audio/"):
        return replace(
            message, kind="audio", media_id=message_id, media_url=form.get("MediaUrl0", ""), mime_type=media_type
        )
    body = form.get("Body", "")
    if has_media or not body.strip():
        return message
    return replace(message, kind="text", text=body)


def _count(value: str | None) -> int:
    return int(value) if value and value.isdigit() else 0


def _as_error(error: TransportError) -> WhatsAppError:
    detail = error.json()
    if not isinstance(detail, dict) or not detail.get("message"):
        return WhatsAppError(str(error), status=error.status)
    raw_code = detail.get("code")
    code = raw_code if isinstance(raw_code, int) else None
    hint = HINTS.get(code) if code is not None else None
    return WhatsAppError(f"{detail['message']} (Twilio code {code})", code=code, status=error.status, hint=hint)
