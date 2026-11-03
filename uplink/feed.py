"""Where snapshots come from: the live map, a local file, or a folder of recordings."""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Iterator, Protocol
from urllib.parse import unquote, urlparse

from .mesh import FeedFormatError, Snapshot, from_compact, parse_meshviewer
from .transport import Transport, TransportError


class FeedError(Exception):
    """The snapshot could not be fetched or understood."""


class Feed(Protocol):
    def fetch(self) -> Snapshot | None:
        """Return the newest snapshot, or None when nothing changed since the last fetch."""


class LiveFeed:
    """The map's meshviewer.json, fetched politely: conditional requests and gzip."""

    def __init__(self, url: str, transport: Transport) -> None:
        self.url = url
        self._transport = transport
        self._validators: dict[str, str] = {}

    def fetch(self) -> Snapshot | None:
        try:
            response = self._transport.request("GET", self.url, headers=self._validators)
        except TransportError as exc:
            raise FeedError(str(exc)) from None
        if response.status == 304:
            return None
        snapshot = parse_document(response.body)
        self._validators = {
            header: value
            for header, value in (
                ("If-None-Match", response.header("ETag")),
                ("If-Modified-Since", response.header("Last-Modified")),
            )
            if value
        }
        return snapshot


class FileFeed:
    """A snapshot file on disk, re-read on every fetch so a demo can swap it underneath."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def fetch(self) -> Snapshot | None:
        return read_snapshot(self.path)


def open_feed(url: str, transport: Transport) -> Feed:
    parsed = urlparse(url)
    if parsed.scheme == "file":
        return FileFeed(Path(unquote(parsed.path)))
    return LiveFeed(url, transport)


def read_snapshot(path: Path) -> Snapshot:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise FeedError(f"cannot read {path}: {exc.strerror or exc}") from None
    return parse_document(data)


def parse_document(data: bytes) -> Snapshot:
    """Accept meshviewer.json or Uplink's compact format, plain or gzipped."""
    try:
        if data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        document = json.loads(data.decode("utf-8"))
    except (OSError, EOFError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FeedError(f"not a JSON snapshot: {exc}") from None
    try:
        if isinstance(document, dict) and "v" in document and "n" in document:
            return from_compact(document)
        return parse_meshviewer(document)
    except FeedFormatError as exc:
        raise FeedError(str(exc)) from None


def iter_recordings(folder: Path) -> Iterator[Snapshot]:
    """Snapshots recorded by `uplink watch`, oldest first."""
    for path in sorted(folder.rglob("*.json.gz")):
        yield read_snapshot(path)
