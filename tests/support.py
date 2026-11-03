"""Test helpers: a mesh builder, programmable HTTP fakes for the three services, a fixed clock."""

from __future__ import annotations

import gzip
import json
import re
import threading
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
