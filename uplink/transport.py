"""A small HTTP client on urllib: timeouts, bounded retries, gzip, size limits, multipart.

Uplink talks to three services (the community map, the Meta Model API, the WhatsApp Cloud
API) and needs the same care for each, without third-party dependencies.
"""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.request
import uuid
import zlib
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit

RETRYABLE = frozenset({429, 500, 502, 503, 504})
NOT_PROCESSED = frozenset({429, 503})  # safe to retry even for requests with side effects
MAX_RETRY_AFTER = 30.0
_ERROR_BODY_LIMIT = 64 * 1024


class TransportError(Exception):
    """The network failed, or the server answered with an error status."""

    def __init__(self, message: str, *, status: int | None = None, body: bytes = b"", retry_after: float | None = None):
        super().__init__(message)
        self.status = status
        self.body = body
        self.retry_after = retry_after

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None


@dataclass(frozen=True)
class Response:
    status: int
    headers: Mapping[str, str]
    body: bytes

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TransportError(f"expected JSON, got {self.body[:80]!r}", status=self.status) from exc

    def header(self, name: str) -> str | None:
        wanted = name.lower()
        return next((value for key, value in self.headers.items() if key.lower() == wanted), None)


@dataclass(frozen=True)
class Part:
    """One field of a multipart/form-data body."""

    name: str
    content: bytes
    content_type: str
    filename: str | None = None


def encode_multipart(parts: Iterable[Part]) -> tuple[bytes, str]:
    boundary = f"uplink-{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for part in parts:
        disposition = f'form-data; name="{part.name}"'
        if part.filename:
            disposition += f'; filename="{part.filename}"'
        chunks += [
            f"--{boundary}\r\n".encode(),
            f"Content-Disposition: {disposition}\r\n".encode(),
            f"Content-Type: {part.content_type}\r\n\r\n".encode(),
            part.content,
            b"\r\n",
        ]
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


class Transport:
    def __init__(
        self,
        *,
        user_agent: str = "Uplink",
        timeout: float = 20.0,
        retries: int = 2,
        backoff: float = 1.0,
        max_bytes: int = 32 * 1024 * 1024,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.max_bytes = max_bytes
        self._sleep = sleep
        self._opener = urllib.request.build_opener()

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        timeout: float | None = None,
        idempotent: bool | None = None,
    ) -> Response:
        """Send a request and return the response; raise TransportError for errors.

        GET is retried on network errors and 429/5xx. Requests with side effects (sending a
        WhatsApp message) are retried only when the server says it did not process them.
        """
        safe = method.upper() in {"GET", "HEAD"} if idempotent is None else idempotent
        attempt = 0
        while True:
            try:
                return self._send(method, url, headers or {}, body, timeout or self.timeout)
            except TransportError as exc:
                retryable = (exc.status in RETRYABLE or exc.status is None) if safe else exc.status in NOT_PROCESSED
                if not retryable or attempt >= self.retries:
                    raise
                self._sleep(self._delay(exc, attempt))
                attempt += 1

    def json(
        self, method: str, url: str, payload: Any = None, *, headers: Mapping[str, str] | None = None, **options: Any
    ) -> Any:
        all_headers = {"Accept": "application/json", **(headers or {})}
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            all_headers["Content-Type"] = "application/json"
        return self.request(method, url, headers=all_headers, body=body, **options).json()

    def _send(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout: float) -> Response:
        request = urllib.request.Request(
            url,
            data=body,
            method=method.upper(),
            headers={"User-Agent": self.user_agent, "Accept-Encoding": "gzip", **headers},
        )
        label = f"{method.upper()} {_redact(url)}"
        try:
            with self._opener.open(request, timeout=timeout) as response:
                raw = response.read(self.max_bytes + 1)
                headers_out = dict(response.headers.items())
                status = response.status
        except urllib.error.HTTPError as err:
            with err:
                if err.code == 304:
                    return Response(304, dict(err.headers.items()), b"")
                raw = err.read(_ERROR_BODY_LIMIT)
                encoding = err.headers.get("Content-Encoding", "")
                retry_after = _retry_after(err.headers.get("Retry-After"))
            body_out = self._decode(raw, encoding, label, strict=False)
            raise TransportError(
                f"{label} answered HTTP {err.code}", status=err.code, body=body_out, retry_after=retry_after
            ) from None
        except (urllib.error.URLError, http.client.HTTPException, OSError) as err:
            reason = getattr(err, "reason", None) or err
            raise TransportError(f"{label} failed: {reason}") from None
        if len(raw) > self.max_bytes:
            raise TransportError(f"{label} sent more than {self.max_bytes} bytes", status=status)
        return Response(status, headers_out, self._decode(raw, headers_out.get("Content-Encoding", ""), label))

    def _decode(self, raw: bytes, encoding: str, label: str, *, strict: bool = True) -> bytes:
        if encoding.lower() != "gzip":
            return raw
        try:
            inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
            data = inflater.decompress(raw, self.max_bytes + 1)
        except zlib.error as exc:
            if not strict:
                return raw
            raise TransportError(f"{label} sent broken gzip data: {exc}") from None
        if len(data) > self.max_bytes:
            raise TransportError(f"{label} sent more than {self.max_bytes} bytes once decompressed")
        return data

    def _delay(self, error: TransportError, attempt: int) -> float:
        if error.retry_after is not None:
            return min(error.retry_after, MAX_RETRY_AFTER)
        return self.backoff * (1 << attempt)


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, moment.timestamp() - time.time())


def _redact(url: str) -> str:
    """Drop the query string: signed media links carry credentials there."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
