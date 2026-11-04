"""Test helpers: a mesh builder, programmable HTTP fakes for each outside service, a fixed clock."""

from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import html
import json
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email import message_from_bytes
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, urlsplit

from uplink.config import Settings
from uplink.mesh import Snapshot, parse_meshviewer

MUNICH = timezone(timedelta(hours=1))
START = datetime(2026, 11, 2, 12, 0, 0, tzinfo=MUNICH)
FIXTURE = Path(__file__).parent / "fixtures" / "munich-2026-10-23.json"


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S%z")


class MeshBuilder:
    """Builds meshviewer.json documents the way the Freifunk map publishes them.

    Online nodes answer every poll; offline nodes keep their last report (next hop, boot
    time) but show zero clients, exactly like the real feed.
    """

    def __init__(self, at: datetime = START) -> None:
        self.now = at
        self.nodes: dict[str, dict[str, Any]] = {}
        self.links: list[dict[str, Any]] = []
        self._clients: dict[str, int] = {}
        self._silent: set[str] = set()

    def gateway(self, node_id: str, name: str | None = None) -> MeshBuilder:
        self.nodes[node_id] = self._raw(node_id, name or f"gw-{node_id}", is_gateway=True)
        return self

    def node(
        self,
        node_id: str,
        via: str,
        *,
        name: str | None = None,
        clients: int = 0,
        booted: datetime | None = None,
        firmware: str = "v2025.12.3",
    ) -> MeshBuilder:
        upstream = self.nodes[via]
        gateway = via if upstream["is_gateway"] else upstream["gateway"]
        raw = self._raw(
            node_id, name or f"{node_id}.home", next_hop=via, gateway=gateway, clients=clients, firmware=firmware
        )
        raw["uptime"] = stamp(booted or self.now - timedelta(days=3, minutes=len(self.nodes) * 7))
        self.nodes[node_id] = raw
        self._clients[node_id] = clients
        return self

    def link(self, a: str, b: str, kind: str = "wifi", quality: float = 1.0) -> MeshBuilder:
        self.links.append({"type": kind, "source": a, "target": b, "source_tq": quality, "target_tq": quality})
        return self

    def tick(self, seconds: int = 60) -> MeshBuilder:
        """Advance time; every node still answering reports again."""
        self.now += timedelta(seconds=seconds)
        for node_id, raw in self.nodes.items():
            if raw["is_online"] and node_id not in self._silent:
                raw["lastseen"] = stamp(self.now - timedelta(seconds=20))
        return self

    def drop(self, *node_ids: str, ago: int = 30) -> MeshBuilder:
        """The map has marked these offline; they were last heard `ago` seconds before now."""
        for node_id in node_ids:
            raw = self.nodes[node_id]
            raw.update(is_online=False, lastseen=stamp(self.now - timedelta(seconds=ago)), clients=0)
            self._silent.discard(node_id)
        return self

    def silence(self, *node_ids: str, ago: int) -> MeshBuilder:
        """Still marked online, but silent for `ago` seconds: the map has not noticed yet."""
        for node_id in node_ids:
            self.nodes[node_id]["lastseen"] = stamp(self.now - timedelta(seconds=ago))
            self._silent.add(node_id)
        return self

    def restore(self, *node_ids: str, firmware: str | None = None) -> MeshBuilder:
        for node_id in node_ids:
            raw = self.nodes[node_id]
            raw.update(
                is_online=True, lastseen=stamp(self.now - timedelta(seconds=10)), clients=self._clients.get(node_id, 0)
            )
            raw["uptime"] = stamp(self.now - timedelta(seconds=40))
            if firmware:
                raw["firmware"]["release"] = firmware
            self._silent.discard(node_id)
        return self

    def reroute(self, node_id: str, via: str) -> MeshBuilder:
        self.nodes[node_id]["gateway_nexthop"] = via
        return self

    def document(self) -> dict[str, Any]:
        return json.loads(
            json.dumps({"timestamp": stamp(self.now), "nodes": list(self.nodes.values()), "links": self.links})
        )

    def snapshot(self) -> Snapshot:
        return parse_meshviewer(self.document())

    def _raw(
        self,
        node_id: str,
        name: str,
        *,
        is_gateway: bool = False,
        next_hop: str | None = None,
        gateway: str | None = None,
        clients: int = 0,
        firmware: str = "v2025.12.3",
    ) -> dict[str, Any]:
        return {
            "node_id": node_id,
            "hostname": name,
            "is_online": True,
            "is_gateway": is_gateway,
            "lastseen": stamp(self.now - timedelta(seconds=20)),
            "firstseen": stamp(self.now - timedelta(days=200)),
            "uptime": stamp(self.now - timedelta(days=10)),
            "clients": clients,
            "gateway": gateway,
            "gateway_nexthop": next_hop,
            "model": "TP-Link CPE210 v1",
            "firmware": {"base": "gluon-v2023.2", "release": firmware},
            "owner": {"contact": "private@example.org"},  # published by real maps; Uplink must never read it
        }


@dataclass
class Recorded:
    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body)

    def multipart(self) -> dict[str, tuple[str, bytes, str | None]]:
        """Parse a multipart/form-data body into {name: (content type, content, filename)}."""
        raw = f"Content-Type: {self.headers['content-type']}\r\n\r\n".encode() + self.body
        parts = {}
        for part in message_from_bytes(raw, policy=HTTP).iter_parts():
            parts[part.get_param("name", header="content-disposition")] = (
                part.get_content_type(),
                part.get_payload(decode=True),
                part.get_filename(),
            )
        return parts


Reply = tuple[int, dict[str, str], bytes]
Route = Callable[[Recorded, re.Match], Reply]


def json_reply(data: Any, status: int = 200, headers: dict[str, str] | None = None) -> Reply:
    return status, {"Content-Type": "application/json", **(headers or {})}, json.dumps(data).encode()


class FakeServer:
    """A real HTTP server on 127.0.0.1 with programmable routes. Records every request."""

    def __init__(self) -> None:
        self.requests: list[Recorded] = []
        self._routes: list[tuple[str, re.Pattern[str], Route]] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def route(self, method: str, pattern: str, handler: Route) -> None:
        self._routes.append((method, re.compile(pattern + "$"), handler))

    @property
    def url(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def requests_to(self, path_pattern: str) -> list[Recorded]:
        return [r for r in self.requests if re.fullmatch(path_pattern, r.path)]

    def __enter__(self) -> FakeServer:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self) -> None:
                url = urlsplit(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                recorded = Recorded(
                    self.command,
                    url.path,
                    dict(parse_qsl(url.query)),
                    {k.lower(): v for k, v in self.headers.items()},
                    self.rfile.read(length),
                )
                fake.requests.append(recorded)
                for method, pattern, handler in fake._routes:
                    match = pattern.match(url.path)
                    if method == self.command and match:
                        status, headers, body = handler(recorded, match)
                        break
                else:
                    status, headers, body = 404, {}, b"no route"
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                self._handle()

            def do_POST(self) -> None:
                self._handle()

            def log_message(self, format: str, *args: object) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        assert self._server is not None and self._thread is not None
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(5)


class FakeFeed(FakeServer):
    """The community map: serves whatever document the test sets, with ETag support."""

    def __init__(self) -> None:
        super().__init__()
        self.document: dict[str, Any] | None = None
        self.fail_with: int | None = None
        self.route("GET", r"/data/meshviewer\.json", self._serve)

    @property
    def feed_url(self) -> str:
        return f"{self.url}/data/meshviewer.json"

    def _serve(self, request: Recorded, _: re.Match) -> Reply:
        if self.fail_with:
            return self.fail_with, {}, b"down for maintenance"
        body = json.dumps(self.document).encode()
        etag = f'"{hash(body) & 0xFFFFFFFF:x}"'
        if request.headers.get("if-none-match") == etag:
            return 304, {"ETag": etag}, b""
        headers = {"Content-Type": "application/json", "ETag": etag}
        if "gzip" in request.headers.get("accept-encoding", ""):
            return 200, {**headers, "Content-Encoding": "gzip"}, gzip.compress(body)
        return 200, headers, body


class FakeMeta(FakeServer):
    """Meta Model API: chat completions answer from a script; transcription returns a fixed text."""

    def __init__(self) -> None:
        super().__init__()
        self.replies: list[str | Reply] = []
        self.default_reply: str | Callable[[Recorded], str] = "*All good.*"
        self.transcript = "what is down right now?"
        self.route("POST", r"/v1/chat/completions", self._chat)
        self.route("POST", r"/v1/asr/transcribe", self._transcribe)

    @property
    def base_url(self) -> str:
        return f"{self.url}/v1"

    def chats(self) -> list[Recorded]:
        return self.requests_to(r"/v1/chat/completions")

    def _chat(self, request: Recorded, _: re.Match) -> Reply:
        reply = self.replies.pop(0) if self.replies else self.default_reply
        if isinstance(reply, tuple):
            return reply
        text = reply(request) if callable(reply) else reply
        return json_reply(
            {
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": text, "refusal": None},
                        "logprobs": None,
                        "finish_reason": "stop",
                    }
                ],
            }
        )

    def _transcribe(self, request: Recorded, _: re.Match) -> Reply:
        return json_reply({"sessionId": "s-1", "transcript": self.transcript, "audioDurationMs": 2100, "turns": []})


class FakeGraph(FakeServer):
    """WhatsApp Cloud API: records sent messages; serves one voice note as media."""

    def __init__(self, version: str = "v26.0", phone_number_id: str = "1000001") -> None:
        super().__init__()
        self.version = version
        self.phone_number_id = phone_number_id
        self.fail_with: Reply | None = None
        self.media = b""
        self.route("POST", rf"/{version}/{phone_number_id}/messages", self._message)
        self.route("GET", rf"/{version}/(?P<media>m-\w+)", self._media_info)
        self.route("GET", r"/media/(?P<media>m-\w+)", self._media_file)

    def sent(self) -> list[dict[str, Any]]:
        return [r.json() for r in self.requests_to(rf"/{self.version}/{self.phone_number_id}/messages")]

    def texts(self) -> list[str]:
        return [m["text"]["body"] for m in self.sent() if m.get("type") == "text"]

    def _message(self, request: Recorded, _: re.Match) -> Reply:
        if self.fail_with:
            return self.fail_with
        number = len(self.requests_to(rf"/{self.version}/{self.phone_number_id}/messages")) - 1
        payload = request.json()
        return json_reply(
            {
                "messaging_product": "whatsapp",
                "contacts": [{"input": payload.get("to"), "wa_id": payload.get("to")}],
                "messages": [{"id": f"wamid.{number}"}],
            }
        )

    def _media_info(self, request: Recorded, match: re.Match) -> Reply:
        media = match["media"]
        return json_reply(
            {"messaging_product": "whatsapp", "url": f"{self.url}/media/{media}", "mime_type": "audio/ogg", "id": media}
        )

    def _media_file(self, request: Recorded, match: re.Match) -> Reply:
        if request.headers.get("authorization") != "Bearer test-token":
            return 401, {}, b"unauthorized"
        return 200, {"Content-Type": "audio/ogg"}, self.media


class FakeTwilio(FakeServer):
    """Twilio's Messages API and media storage: sends are recorded; media redirects to storage
    that, like S3, refuses requests carrying a second set of credentials."""

    account_sid = "ACtest"
    auth_token = "twilio-token"

    def __init__(self) -> None:
        super().__init__()
        self.fail_with: Reply | None = None
        self.media = b""
        self.route("POST", rf"/2010-04-01/Accounts/{self.account_sid}/Messages\.json", self._message)
        self.route("GET", r"/media/(?P<media>\w+)", self._media)
        self.route("GET", r"/storage/(?P<media>\w+)", self._storage)

    def sent(self) -> list[dict[str, str]]:
        return [dict(parse_qsl(r.body.decode())) for r in self.requests_to(r"/2010-04-01/Accounts/\w+/Messages\.json")]

    def bodies(self) -> list[str]:
        return [m["Body"] for m in self.sent()]

    def _authorised(self, request: Recorded) -> bool:
        expected = base64.b64encode(f"{self.account_sid}:{self.auth_token}".encode()).decode()
        return request.headers.get("authorization") == f"Basic {expected}"

    def _message(self, request: Recorded, _: re.Match) -> Reply:
        if not self._authorised(request):
            return json_reply({"code": 20003, "message": "Authenticate", "status": 401}, 401)
        if self.fail_with:
            return self.fail_with
        number = len(self.sent()) - 1  # this request is already recorded
        return json_reply({"sid": f"SM{number:032d}", "status": "queued"}, 201)

    def _media(self, request: Recorded, match: re.Match) -> Reply:
        if not self._authorised(request):
            return 401, {}, b"unauthorized"
        return 307, {"Location": f"{self.url}/storage/{match['media']}?signature=abc"}, b""

    def _storage(self, request: Recorded, match: re.Match) -> Reply:
        if "authorization" in request.headers:
            return 400, {}, b"Only one auth mechanism allowed"
        return 200, {"Content-Type": "audio/ogg"}, self.media


def twilio_error(code: int, message: str = "error", status: int = 400) -> Reply:
    return json_reply(
        {"code": code, "message": message, "more_info": f"https://www.twilio.com/docs/errors/{code}", "status": status},
        status,
    )


def twilio_signature(auth_token: str, url: str, params: dict[str, str]) -> str:
    payload = url + "".join(k + v for k, v in sorted(params.items()))
    return base64.b64encode(hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()).decode()


TELEGRAM_TAGS = frozenset(
    {
        "b",
        "strong",
        "i",
        "em",
        "u",
        "ins",
        "s",
        "strike",
        "del",
        "span",
        "tg-spoiler",
        "a",
        "tg-emoji",
        "code",
        "pre",
        "blockquote",
    }
)
_TELEGRAM_MARKUP = re.compile(r"<(/?)([A-Za-z][\w-]*)[^<>]*>|&(?:#\d+|#x[0-9A-Fa-f]+|lt|gt|amp|quot);|[<>&]")


def telegram_text(markup: str) -> str:
    """Read a message the way Telegram's HTML parse mode does, and refuse what Telegram refuses
    ("can't parse entities"): unknown or unclosed tags, other named entities, a bare <, > or &."""
    plain: list[str] = []
    open_tags: list[str] = []
    position = 0
    for match in _TELEGRAM_MARKUP.finditer(markup):
        plain.append(markup[position : match.start()])
        position = match.end()
        token = match.group(0)
        if match.group(2):
            name = match.group(2).lower()
            if name not in TELEGRAM_TAGS:
                raise ValueError(f'Unsupported start tag "{name}" at byte offset {match.start()}')
            if not match.group(1):
                open_tags.append(name)
            elif not open_tags or open_tags.pop() != name:
                raise ValueError(f"Unmatched end tag at byte offset {match.start()}")
        elif len(token) == 1:
            raise ValueError(f"Unexpected {token!r} at byte offset {match.start()}")
        else:
            plain.append(html.unescape(token))
    if open_tags:
        raise ValueError(f'Can\'t find end tag corresponding to start tag "{open_tags[-1]}"')
    plain.append(markup[position:])
    return "".join(plain)


def telegram_error(code: int, description: str) -> Reply:
    return json_reply({"ok": False, "error_code": code, "description": description}, code)


class FakeTelegram(FakeServer):
    """Telegram's Bot API. sendMessage reads the HTML like Telegram and refuses what Telegram refuses;
    getUpdates serves pushed messages from an offset and forgets confirmed ones, holding an empty
    long poll for a moment; getFile and the file host serve one voice note. Wrong tokens get 401."""

    token = "123456:test-token"
    chat_id = 4242
    voice_file_id = "AwACAgIAAxkBAAIBvoice"
    max_wait = 0.1  # Telegram holds an empty long poll for `timeout` seconds; tests should not wait that long

    def __init__(self) -> None:
        super().__init__()
        self.fail_with: Reply | None = None  # the answer to every sendMessage
        self.fail_updates: list[Reply] = []  # answers to the next getUpdates calls
        self.media = b""
        self.chats = {self.chat_id}  # chats in which someone pressed Start
        self._pending: list[dict[str, Any]] = []
        self._next_update = 805_000_001
        self._next_message = 1
        self._changed = threading.Condition()
        bot = f"/bot{re.escape(self.token)}"
        self.route("POST", f"{bot}/sendMessage", self._send)
        self.route("POST", f"{bot}/getUpdates", self._updates)
        self.route("POST", f"{bot}/getFile", self._file)
        self.route("GET", rf"/file{bot}/(?P<path>.+)", self._download)
        self.route("POST", r"/bot[^/]+/\w+", lambda request, match: telegram_error(401, "Unauthorized"))

    def push(self, text: str | None = None, *, voice: bool = False, chat_id: int | None = None) -> int:
        """Someone messages the bot; returns the update's id."""
        chat_id = self.chat_id if chat_id is None else chat_id
        if chat_id < 0:
            chat = {"id": chat_id, "title": "Freifunk volunteers", "type": "group"}
        else:
            chat = {"id": chat_id, "first_name": "Vishnu", "type": "private"}
        with self._changed:
            message: dict[str, Any] = {
                "message_id": self._next_message,
                "from": {"id": abs(chat_id), "is_bot": False, "first_name": "Vishnu", "language_code": "en"},
                "chat": chat,
                "date": 1762081200,
            }
            if text is not None:
                message["text"] = text
            if voice:
                message["voice"] = {
                    "duration": 2,
                    "mime_type": "audio/ogg",
                    "file_id": self.voice_file_id,
                    "file_unique_id": "AgADvoice",
                    "file_size": len(self.media),
                }
            update = {"update_id": self._next_update, "message": message}
            self._next_update += 1
            self._next_message += 1
            self._pending.append(update)
            self._changed.notify_all()
        return update["update_id"]

    def sent(self) -> list[dict[str, Any]]:
        return [r.json() for r in self.requests if r.path == f"/bot{self.token}/sendMessage"]

    def texts(self) -> list[str]:
        return [m["text"] for m in self.sent()]

    def polls(self) -> list[dict[str, Any]]:
        return [r.json() for r in self.requests if r.path == f"/bot{self.token}/getUpdates"]

    def wait_for(self, count: int, timeout: float = 5.0) -> list[str]:
        """Wait until `count` messages have been sent; returns the texts."""
        deadline = time.monotonic() + timeout
        while len(self.texts()) < count and time.monotonic() < deadline:
            time.sleep(0.01)
        return self.texts()

    def _send(self, request: Recorded, _: re.Match) -> Reply:
        if self.fail_with:
            return self.fail_with
        payload = request.json()
        text = payload.get("text", "")
        if payload.get("parse_mode") == "HTML":
            try:
                text = telegram_text(text)
            except ValueError as exc:
                return telegram_error(400, f"Bad Request: can't parse entities: {exc}")
        if not text.strip():
            return telegram_error(400, "Bad Request: message text is empty")
        if len(text) > 4096:
            return telegram_error(400, "Bad Request: message is too long")
        try:
            chat_id = int(payload.get("chat_id"))  # Telegram takes "4242" as well as 4242
        except (TypeError, ValueError):
            chat_id = None
        if chat_id not in self.chats:
            return telegram_error(400, "Bad Request: chat not found")
        with self._changed:
            message_id = self._next_message
            self._next_message += 1
        chat = {"id": chat_id, "type": "private"}
        return json_reply(
            {"ok": True, "result": {"message_id": message_id, "chat": chat, "date": 1762081260, "text": text}}
        )

    def _updates(self, request: Recorded, _: re.Match) -> Reply:
        if self.fail_updates:
            return self.fail_updates.pop(0)
        payload = request.json() if request.body else {}
        offset = payload.get("offset")
        with self._changed:
            if isinstance(offset, int):
                self._pending = [u for u in self._pending if u["update_id"] >= offset]  # confirmed: forgotten
            if not self._pending and payload.get("timeout"):
                self._changed.wait(min(payload["timeout"], self.max_wait))
            result = list(self._pending[: payload.get("limit", 100)])
        return json_reply({"ok": True, "result": result})

    def _file(self, request: Recorded, _: re.Match) -> Reply:
        file_id = request.json().get("file_id")
        if file_id != self.voice_file_id:
            return telegram_error(400, "Bad Request: invalid file_id")
        info = {
            "file_id": file_id,
            "file_unique_id": "AgADvoice",
            "file_size": len(self.media),
            "file_path": "voice/file_0.oga",
        }
        return json_reply({"ok": True, "result": info})

    def _download(self, request: Recorded, match: re.Match) -> Reply:
        if match["path"] != "voice/file_0.oga":
            return 404, {}, b"Not Found"
        return 200, {"Content-Type": "application/octet-stream"}, self.media


def whatsapp_error(code: int, message: str = "error", status: int = 400) -> Reply:
    return json_reply(
        {"error": {"message": message, "type": "OAuthException", "code": code, "fbtrace_id": "x"}}, status
    )


@dataclass
class Clock:
    now: datetime = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


def make_settings(data_dir: Path, **overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "feed_url": "http://127.0.0.1:9/data/meshviewer.json",
        "data_dir": data_dir,
        "record_snapshots": False,
        "whatsapp_token": "test-token",
        "whatsapp_phone_number_id": "1000001",
        "whatsapp_to": "4915112345678",
        "whatsapp_app_secret": "app-secret",
        "whatsapp_verify_token": "verify-me",
    }
    defaults.update(overrides)
    settings = Settings(**defaults)
    settings.validate()
    return settings


@dataclass
class ScriptedModel:
    """A stand-in ChatModel that returns scripted replies and records the conversations."""

    replies: list[str | Exception] = field(default_factory=list)
    calls: list[list[dict[str, str]]] = field(default_factory=list)

    def chat(self, messages):
        self.calls.append([dict(m) for m in messages])
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply
