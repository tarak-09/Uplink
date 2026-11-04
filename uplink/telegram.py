"""A Telegram bot, for when WhatsApp cannot deliver: it sends the alerts and answers questions.

`TelegramBot` does the same job as `whatsapp.WhatsAppClient` (send a text, fetch a voice note)
through Telegram's Bot API. Telegram needs no webhook and no tunnel: `TelegramPoller` long-polls
getUpdates and hands each message from TELEGRAM_CHAT_ID to the reply queue the webhooks feed, so
the assistant, ffmpeg and transcription work exactly as they do for WhatsApp.

Messages go out as HTML, which shows node names as written (Markdown would read their
underscores as italics); WhatsApp's *bold* becomes <b>bold</b>.

The bot token is part of every Bot API URL, and the transport names the URL in its errors. So
errors are rebuilt from Telegram's answer and scrubbed, and the token never reaches a log.
"""

from __future__ import annotations

import html
import logging
import re
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable

from .transport import Transport, TransportError
from .whatsapp import InboundMessage, Media, WhatsAppError

log = logging.getLogger(__name__)

MAX_TEXT = 4096  # Telegram's limit, counted after the HTML is parsed
LONG_POLL_SECONDS = 30
MAX_BACKOFF = 60.0

# WhatsApp's *bold*: the stars hug the text and neither touches a letter or digit outside it, so
# list bullets ("* site-bca3"), "2*3*4" and "* spaced *" stay as they are, as they do on WhatsApp.
_BOLD = re.compile(r"(?<![\w*])\*(?=[^\s*])([^*\n]*?[^\s*])\*(?![\w*])")

# (error code, words in Telegram's description, what to do). The first match wins.
_HINTS: tuple[tuple[int, str, str], ...] = (
    (
        401,
        "",
        "Telegram rejected the bot token. Copy TELEGRAM_BOT_TOKEN again from @BotFather (/mybots, your bot, API Token).",
    ),
    (404, "", "Telegram does not recognise the bot token. Copy TELEGRAM_BOT_TOKEN again from @BotFather, all of it."),
    (
        400,
        "chat not found",
        "TELEGRAM_CHAT_ID is wrong, or nobody in that chat has pressed Start with the bot. Run: uplink telegram-chat-id",
    ),
    (403, "blocked", "The bot is blocked in that chat. Open the chat with the bot in Telegram and press Restart."),
    (403, "", "The bot may not message that chat. Open the chat with the bot in Telegram and press Start."),
    (
        409,
        "webhook",
        "A webhook is set on this bot, so Uplink cannot fetch its messages. Remove it with the Bot API's deleteWebhook method.",
    ),
    (
        409,
        "",
        "Another program is fetching this bot's messages, such as a second uplink serve or run. Stop it: only one can at a time.",
    ),
    (429, "", "Telegram is rate-limiting the bot. Wait a minute."),
)


class TelegramBot:
    def __init__(self, *, token: str, transport: Transport, base_url: str = "https://api.telegram.org") -> None:
        base = base_url.rstrip("/")
        self._token = token
        self._api = f"{base}/bot{token}"
        self._files = f"{base}/file/bot{token}"
        self._transport = transport

    def send_text(self, to: str, body: str, *, reply_to: str | None = None) -> str:
        """Send a message to a chat; `reply_to` (a message id) shows it as the answer to that message."""
        # Clipped before escaping: the limit counts what the reader sees, not "&amp;".
        clipped = body if len(body) <= MAX_TEXT else body[: MAX_TEXT - 1].rstrip() + "…"
        payload: dict[str, Any] = {"chat_id": _chat(to), "text": to_html(clipped), "parse_mode": "HTML"}
        if reply_to and reply_to.isdecimal():
            payload["reply_parameters"] = {"message_id": int(reply_to), "allow_sending_without_reply": True}
        sent = self._call("sendMessage", payload)
        message_id = sent.get("message_id") if isinstance(sent, dict) else None
        if not isinstance(message_id, int):
            raise WhatsAppError("Telegram accepted the message but returned no message_id")
        return str(message_id)

    def send_template(self, to: str, name: str, language: str = "en_US", body_parameters: tuple[str, ...] = ()) -> str:
        raise WhatsAppError(
            "Telegram has no message templates. Alerts and replies go out as ordinary messages, "
            "which reach the chat at any time."
        )

    def download_media(self, media_id: str, url: str = "") -> Media:
        """Fetch a voice note by its file_id. `url` is not used: Telegram's file links contain the token."""
        info = self._call("getFile", {"file_id": media_id}, idempotent=True)
        path = info.get("file_path") if isinstance(info, dict) else None
        if not isinstance(path, str) or not path:
            raise WhatsAppError("Telegram gave no download path for the voice note (bots can fetch files up to 20 MB)")
        try:
            response = self._transport.request("GET", f"{self._files}/{path}", timeout=60)
        except TransportError as exc:
            raise self._error(exc) from None
        return Media(response.body, response.header("Content-Type") or "application/octet-stream")

    def get_updates(
        self, offset: int | None = None, *, timeout: int = 0, allowed_updates: tuple[str, ...] | None = None
    ) -> list[dict[str, Any]]:
        """Updates from `offset` on; asking from an offset confirms every update before it.

        With a `timeout`, this is a long poll: Telegram holds the request until something arrives.
        """
        payload: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            payload["offset"] = offset
        if allowed_updates is not None:
            payload["allowed_updates"] = list(allowed_updates)
        updates = self._call("getUpdates", payload, idempotent=True, timeout=timeout + 15)
        return [u for u in updates if isinstance(u, dict)] if isinstance(updates, list) else []

    def scrub(self, text: str) -> str:
        """`text` with the bot token blanked out."""
        for secret in (self._token, self._token.partition(":")[2]):
            if secret:
                text = text.replace(secret, "<token>")
        return text

    def _call(self, method: str, payload: dict[str, Any], **options: Any) -> Any:
        try:
            answer = self._transport.json("POST", f"{self._api}/{method}", payload, **options)
        except TransportError as exc:
            raise self._error(exc) from None
        if not isinstance(answer, dict) or answer.get("ok") is not True:
            raise self._refusal(answer, None)
        return answer.get("result")

    def _error(self, error: TransportError) -> WhatsAppError:
        detail = error.json()
        if isinstance(detail, dict) and "description" in detail:
            return self._refusal(detail, error.status)
        if error.status is not None and error.status >= 400:
            return WhatsAppError(
                f"Telegram answered HTTP {error.status}", status=error.status, hint=_hint(error.status, "")
            )
        # The transport's message starts with the URL, and the URL holds the token.
        reason = self.scrub(str(error).rpartition(" failed: ")[2])
        if error.status is None:
            return WhatsAppError(f"could not reach Telegram: {reason}")
        return WhatsAppError(f"Telegram sent an unreadable answer: {reason}", status=error.status)

    def _refusal(self, answer: Any, status: int | None) -> WhatsAppError:
        detail = answer if isinstance(answer, dict) else {}
        description = self.scrub(str(detail.get("description") or "Telegram sent an unexpected answer"))
        raw_code = detail.get("error_code")
        code = raw_code if isinstance(raw_code, int) else status
        suffix = f" (Telegram code {code})" if code is not None else ""
        return WhatsAppError(f"{description}{suffix}", code=code, status=status, hint=_hint(code, description))


def to_html(text: str) -> str:
    """WhatsApp-formatted text as Telegram HTML: <, > and & escaped, *bold* as <b>bold</b>."""
    return _BOLD.sub(r"<b>\1</b>", html.escape(text, quote=False))


def parse_update(update: Any) -> InboundMessage | None:
    """Turn one getUpdates entry into the InboundMessage the webhooks produce; None if it is no message."""
    message = update.get("message") if isinstance(update, dict) else None
    if not isinstance(message, dict):
        return None
    chat, message_id, date = message.get("chat"), message.get("message_id"), message.get("date")
    chat_id = chat.get("id") if isinstance(chat, dict) else None
    if not isinstance(chat_id, int) or not isinstance(message_id, int):
        return None
    person = message.get("from")
    inbound = InboundMessage(
        id=str(message_id),
        sender=str(chat_id),
        kind="other",
        sent_at=datetime.fromtimestamp(date, timezone.utc) if isinstance(date, int) else None,
        sender_name=str(person.get("first_name", "")) if isinstance(person, dict) else "",
    )
    voice = message.get("voice")
    if isinstance(voice, dict) and isinstance(voice.get("file_id"), str):
        return replace(inbound, kind="audio", media_id=voice["file_id"], mime_type=str(voice.get("mime_type", "")))
    text = message.get("text")
    if isinstance(text, str) and text.strip():
        return replace(inbound, kind="text", text=text)
    return inbound


@dataclass(frozen=True)
class Chat:
    id: int
    kind: str  # private, group, supergroup or channel
    name: str


def recent_chats(updates: list[dict[str, Any]]) -> list[Chat]:
    """The chats these updates came from, each once, the most recent last."""
    chats: dict[int, Chat] = {}
    for update in updates:
        for value in update.values():
            chat = value.get("chat") if isinstance(value, dict) else None
            if not isinstance(chat, dict) or not isinstance(chat.get("id"), int):
                continue
            person = " ".join(str(chat[key]) for key in ("first_name", "last_name") if chat.get(key))
            chats.pop(chat["id"], None)
            chats[chat["id"]] = Chat(chat["id"], str(chat.get("type", "")), str(chat.get("title") or person))
    return list(chats.values())


class TelegramPoller:
    """Fetches the bot's messages by long polling and hands each one from `chat_id` to `deliver`.

    Asking for updates from an offset confirms the ones before it, so every message is handed on
    once. Messages from other chats are confirmed and dropped.
    """

    def __init__(
        self,
        bot: TelegramBot,
        chat_id: str,
        deliver: Callable[[InboundMessage], None],
        *,
        timeout: int = LONG_POLL_SECONDS,
        backoff: float = 2.0,
    ) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.deliver = deliver
        self.timeout = timeout
        self.backoff = backoff
        self.offset: int | None = None

    def poll_once(self) -> int:
        """Fetch once and hand on what came; returns how many messages were handed on."""
        return self._hand_on(self._fetch())

    def run(self, stop: threading.Event) -> None:
        """Poll until `stop` is set. Failures are logged and retried after growing pauses."""
        log.info("fetching Telegram messages for chat %s", self.chat_id)
        failures = 0
        while not stop.is_set():
            try:
                updates = self._fetch()
            except Exception as exc:  # outlive network trouble; no traceback, which could show the token
                failures += 1
                hint = exc.hint if isinstance(exc, WhatsAppError) else None
                detail = str(exc) if isinstance(exc, WhatsAppError) else f"{type(exc).__name__}: {exc}"
                log.error(
                    "could not fetch Telegram messages: %s%s", self.bot.scrub(detail), f" | {hint}" if hint else ""
                )
                stop.wait(min(MAX_BACKOFF, self.backoff * 2 ** min(failures - 1, 10)))
                continue
            failures = 0
            if stop.is_set():
                break  # leave these unconfirmed, so the next start answers them
            self._hand_on(updates)

    def _fetch(self) -> list[dict[str, Any]]:
        return self.bot.get_updates(self.offset, timeout=self.timeout, allowed_updates=("message",))

    def _hand_on(self, updates: list[dict[str, Any]]) -> int:
        handed = 0
        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                self.offset = max(self.offset or 0, update_id + 1)
            message = parse_update(update)
            if message is None:
                continue
            if message.sender != self.chat_id:
                log.info("ignored a message from Telegram chat %s, which is not TELEGRAM_CHAT_ID", message.sender)
                continue
            self.deliver(message)
            handed += 1
        return handed


def _chat(to: str) -> int | str:
    """Chat ids go out as numbers; anything else (an @channel) as given."""
    return int(to) if re.fullmatch(r"-?\d+", to.strip()) else to


def _hint(code: int | None, description: str) -> str | None:
    words = description.lower()
    return next((hint for number, phrase, hint in _HINTS if number == code and phrase in words), None)
