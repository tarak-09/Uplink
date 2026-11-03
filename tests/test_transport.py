import socket
import unittest

from uplink.transport import Part, Transport, TransportError, encode_multipart

from .support import FakeFeed, FakeServer, Recorded, json_reply


class FlakyServer(FakeServer):
    """Fails the first `failures` requests with `status`, then answers 200."""

    def __init__(self, status: int, failures: int, headers: dict[str, str] | None = None) -> None:
        super().__init__()
        self.remaining = failures

        def handler(request: Recorded, match):
            if self.remaining > 0:
                self.remaining -= 1
                return status, headers or {}, b'{"error": {"message": "busy"}}'
            return json_reply({"ok": True})

        self.route("GET", "/thing", handler)
        self.route("POST", "/thing", handler)


class TransportTest(unittest.TestCase):
    def transport(self, **options) -> tuple[Transport, list[float]]:
        sleeps: list[float] = []
        return Transport(sleep=sleeps.append, **options), sleeps

    def test_gzip_responses_are_decoded(self):
        with FakeFeed() as feed:
            feed.document = {"timestamp": "x", "nodes": []}
            transport, _ = self.transport()
            response = transport.request("GET", feed.feed_url)
        self.assertEqual(response.json(), {"timestamp": "x", "nodes": []})
        self.assertEqual(response.header("content-encoding"), "gzip")

    def test_not_modified_is_a_response_not_an_error(self):
        with FakeFeed() as feed:
            feed.document = {"timestamp": "x", "nodes": []}
            transport, _ = self.transport()
            etag = transport.request("GET", feed.feed_url).header("ETag")
            again = transport.request("GET", feed.feed_url, headers={"If-None-Match": etag})
        self.assertEqual((again.status, again.body), (304, b""))

    def test_reads_are_retried_with_backoff(self):
        with FlakyServer(503, failures=2) as server:
            transport, sleeps = self.transport(backoff=0.5)
            response = transport.request("GET", f"{server.url}/thing")
        self.assertEqual(response.json(), {"ok": True})
        self.assertEqual(sleeps, [0.5, 1.0])

    def test_retries_are_bounded(self):
        with FlakyServer(502, failures=5) as server:
            transport, sleeps = self.transport(retries=2)
            with self.assertRaises(TransportError) as caught:
                transport.request("GET", f"{server.url}/thing")
        self.assertEqual(caught.exception.status, 502)
        self.assertEqual(len(sleeps), 2)

    def test_writes_are_retried_only_when_the_server_did_not_process_them(self):
        with FlakyServer(500, failures=1) as server:
            transport, sleeps = self.transport()
            with self.assertRaises(TransportError):
                transport.request("POST", f"{server.url}/thing", body=b"{}")
            self.assertEqual(sleeps, [])
        with FlakyServer(503, failures=1) as server:
            transport, sleeps = self.transport()
            self.assertEqual(transport.request("POST", f"{server.url}/thing", body=b"{}").status, 200)
            self.assertEqual(len(sleeps), 1)

    def test_retry_after_is_honoured_and_capped(self):
        with FlakyServer(429, failures=1, headers={"Retry-After": "2"}) as server:
            transport, sleeps = self.transport()
            transport.request("GET", f"{server.url}/thing")
        self.assertEqual(sleeps, [2.0])
        with FlakyServer(429, failures=1, headers={"Retry-After": "600"}) as server:
            transport, sleeps = self.transport()
            transport.request("GET", f"{server.url}/thing")
        self.assertEqual(sleeps, [30.0])

    def test_error_bodies_are_kept_for_explanations(self):
        with FlakyServer(400, failures=1) as server:
            transport, _ = self.transport()
            with self.assertRaises(TransportError) as caught:
                transport.request("GET", f"{server.url}/thing")
        self.assertEqual(caught.exception.json(), {"error": {"message": "busy"}})
        self.assertIn("HTTP 400", str(caught.exception))

    def test_connection_failures_become_transport_errors(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        transport, sleeps = self.transport(retries=1, timeout=2)
        with self.assertRaises(TransportError) as caught:
            transport.request("GET", f"http://127.0.0.1:{port}/nothing?token=secret")
        self.assertIsNone(caught.exception.status)
        self.assertNotIn("secret", str(caught.exception), "query strings are kept out of error messages")
        self.assertEqual(len(sleeps), 1)

    def test_oversized_responses_are_refused(self):
        with FakeFeed() as feed:
            feed.document = {"timestamp": "x", "nodes": [{"node_id": str(i)} for i in range(2000)]}
            transport, _ = self.transport(max_bytes=1000)
            with self.assertRaises(TransportError) as caught:
                transport.request("GET", feed.feed_url)
        self.assertIn("more than 1000 bytes", str(caught.exception))

    def test_multipart_bodies_parse_back(self):
        body, content_type = encode_multipart(
            [
                Part("request", b'{"model": "m"}', "application/json"),
                Part("audio", b"RIFF\x00\x01", "audio/wav", filename="voice.wav"),
            ]
        )
        recorded = Recorded("POST", "/", {}, {"content-type": content_type}, body)
        parts = recorded.multipart()
        self.assertEqual(parts["request"], ("application/json", b'{"model": "m"}', None))
        self.assertEqual(parts["audio"], ("audio/wav", b"RIFF\x00\x01", "voice.wav"))


if __name__ == "__main__":
    unittest.main()
