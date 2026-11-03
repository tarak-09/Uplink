"""The WhatsApp webhook: verified requests in, answers (typed or spoken questions) out.

`/webhook` takes Meta's Cloud API calls, `/twilio` takes Twilio's sandbox calls. Both expect a
fast answer, so requests are checked and queued, and a worker thread does the slow part:
downloading voice notes, transcribing, asking Muse Spark, replying. Telegram needs no webhook:
`telegram.TelegramPoller` fetches the bot's messages and puts them in the same queue.
"""

from __future__ import annotations

import hmac
import json
import logging
import queue
import threading
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Mapping
from urllib.parse import parse_qsl, urlsplit

from . import templates
from .assistant import Assistant
from .config import Settings
from .muse import MetaModelAPI, ModelAPIError
from .store import Store
from .twilio import parse_twilio_webhook, verify_twilio_signature
from .voice import AudioError, ffmpeg_available, to_wav
from .whatsapp import (
    DeliveryStatus,
    InboundMessage,
    Messenger,
    WhatsAppError,
    normalize_number,
    parse_webhook,
    verify_signature,
)
from .writer import Draft

log = logging.getLogger(__name__)

MAX_BODY = 1024 * 1024
EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
ASR_LANGUAGES = {
    "english": ["English", "Telugu", "Hindi"],
    "telugu": ["Telugu", "English"],
    "hindi": ["Hindi", "English"],
}
ASR_KEYWORDS = ["Freifunk", "Uplink", "mesh", "node", "gateway", "offline", "online"]


class WebhookApp:
    def __init__(
        self,
        *,
        settings: Settings,
        whatsapp: Messenger,
        assistant: Assistant,
        store: Store,
        model: MetaModelAPI | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.whatsapp = whatsapp
        self.assistant = assistant
        self.store = store
        self.model = model
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        # Telegram chat ids keep their minus sign (groups); WhatsApp numbers are digits only.
        allowed = settings.telegram_chat_id if settings.uses_telegram else normalize_number(settings.whatsapp_to)
        self._allowed = {allowed} if allowed else set()
        self._queue: queue.Queue[InboundMessage | None] = queue.Queue(maxsize=100)
        self._seen: deque[str] = deque(maxlen=1000)
        self._seen_lock = threading.Lock()
        self._worker = threading.Thread(target=self._work, name="uplink-replies", daemon=True)

    def start(self) -> None:
        self._worker.start()

    def close(self, timeout: float = 10) -> None:
        """Finish queued replies, then stop the worker."""
        if self._worker.is_alive():
            self._queue.put(None)
            self._worker.join(timeout)

    def verify(self, params: Mapping[str, str]) -> tuple[int, str]:
        """Answer Meta's subscription check (GET with hub.mode, hub.verify_token, hub.challenge)."""
        expected = self.settings.whatsapp_verify_token
        offered = params.get("hub.verify_token", "")
        if params.get("hub.mode") == "subscribe" and expected and hmac.compare_digest(offered, expected):
            return 200, params.get("hub.challenge", "")
        return 403, "verification failed"

    def receive(self, body: bytes, signature: str | None) -> int:
        if not verify_signature(self.settings.whatsapp_app_secret, body, signature):
            log.warning("rejected a webhook call with a missing or wrong signature")
            return 403
        try:
            payload = json.loads(body)
        except ValueError:
            return 400
        messages, statuses = parse_webhook(payload)
        for status in statuses:
            self._note_status(status)
        for message in messages:
            self._enqueue(message)
        return 200

    def receive_twilio(self, body: bytes, url: str, signature: str | None) -> int:
        """Accept a call from Twilio's sandbox. `url` must be the public URL Twilio called."""
        try:
            params = parse_qsl(body.decode("utf-8"), keep_blank_values=True)
        except UnicodeDecodeError:
            return 400
        if not verify_twilio_signature(self.settings.twilio_auth_token, url, params, signature):
            log.warning("rejected a Twilio call with a missing or wrong signature (checked against %s)", url)
            return 403
        message = parse_twilio_webhook(dict(params))
        if message is not None:
            self._enqueue(message)
        return 200

    def _enqueue(self, message: InboundMessage) -> None:
        if self._already_seen(message.id):
            return  # providers retry deliveries they think failed
        if message.sender not in self._allowed:
            log.info("ignored a message from a number that is not WHATSAPP_TO")
            return
        try:
            self._queue.put_nowait(message)
        except queue.Full:
            log.warning("reply queue is full; dropped a message")

    def handle(self, message: InboundMessage) -> None:
        self.store.write_json("inbox.json", {"last_inbound_at": self._clock().isoformat(timespec="seconds")})
        self.store.log("message_in", kind=message.kind, chars=len(message.text))
        if message.kind == "text":
            draft = self.assistant.reply(message.text, chat=message.sender)
        elif message.kind == "audio":
            draft = self._answer_voice(message)
        else:
            draft = Draft(templates.text(self.settings.language, "help"), "template")
        try:
            self.whatsapp.send_text(message.sender, draft.text, reply_to=message.id)
        except WhatsAppError as exc:
            self.store.log("reply_failed", kind=message.kind, error=str(exc))
            log.error("could not reply: %s%s", exc, f" | {exc.hint}" if exc.hint else "")
            return
        self.store.log("reply_sent", kind=message.kind, source=draft.source, chars=len(draft.text))

    def _answer_voice(self, message: InboundMessage) -> Draft:
        language = self.settings.language
        if self.model is None or not ffmpeg_available(self.settings.ffmpeg):
            return Draft(templates.text(language, "voice_unavailable"), "template")
        try:
            media = self.whatsapp.download_media(message.media_id, message.media_url)
            wav = to_wav(media.content, ffmpeg=self.settings.ffmpeg)
            transcript = self.model.transcribe(wav, languages=ASR_LANGUAGES[language], keywords=ASR_KEYWORDS)
        except (WhatsAppError, AudioError, ModelAPIError) as exc:
            log.warning("voice note failed: %s", exc)
            return Draft(templates.text(language, "voice_failed"), "template")
        if not transcript.text:
            return Draft(templates.text(language, "voice_failed"), "template")
        self.store.log("voice_transcribed", audio_ms=transcript.duration_ms, chars=len(transcript.text))
        heard = templates.text(
            templates.detect_language(transcript.text) or language, "heard", transcript=transcript.text[:300]
        )
        answer = self.assistant.reply(transcript.text, chat=message.sender)
        return Draft(f"{heard}\n\n{answer.text}", answer.source, answer.note)

    def _work(self) -> None:
        while (message := self._queue.get()) is not None:
            try:
                self.handle(message)
            except Exception:  # one bad message must not stop the replies; the traceback goes to the log
                log.exception("failed to answer a message")

    def _already_seen(self, message_id: str) -> bool:
        with self._seen_lock:
            if message_id in self._seen:
                return True
            self._seen.append(message_id)
            return False

    def _note_status(self, status: DeliveryStatus) -> None:
        if status.status != "failed":
            return
        hint = WhatsAppError("", code=status.error_code).hint or ""
        self.store.log("delivery_failed", code=status.error_code, title=status.error_title)
        log.error("WhatsApp could not deliver a message: %s (code %s). %s", status.error_title, status.error_code, hint)


def make_server(app: WebhookApp, host: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "Uplink"
        sys_version = ""

        def do_GET(self) -> None:
            url = urlsplit(self.path)
            if url.path == "/healthz":
                self._reply(200, "ok")
            elif url.path == "/webhook":
                self._reply(*app.verify(dict(parse_qsl(url.query))))
            else:
                self._reply(404, "not found")

        def do_POST(self) -> None:
            path = urlsplit(self.path).path
            if path not in {"/webhook", "/twilio"}:
                self._reply(404, "not found")
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if not 0 < length <= MAX_BODY:
                self._reply(413 if length > MAX_BODY else 400, "bad request")
                return
            body = self.rfile.read(length)
            if path == "/twilio":
                status = app.receive_twilio(body, self._public_url(), self.headers.get("X-Twilio-Signature"))
                self._reply(status, EMPTY_TWIML if status == 200 else "rejected", "text/xml")
                return
            status = app.receive(body, self.headers.get("X-Hub-Signature-256"))
            self._reply(status, "ok" if status == 200 else "rejected")

        def _public_url(self) -> str:
            """The URL Twilio signed: as configured, or as seen through the tunnel."""
            if app.settings.twilio_webhook_url:
                return app.settings.twilio_webhook_url
            proto = self.headers.get("X-Forwarded-Proto", "https")
            host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host", "")
            return f"{proto}://{host}{self.path}"

        def _reply(self, status: int, text: str, content_type: str = "text/plain") -> None:
            body = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            log.debug("%s %s", self.address_string(), format % args)

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server
