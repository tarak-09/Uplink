import hashlib
import hmac
import http.client
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import ClassVar

from uplink.app import make_webhook
from uplink.server import make_server
from uplink.store import Store

from .audio import voice_note
from .support import FakeGraph, FakeMeta, FakeTwilio, MeshBuilder, make_settings, twilio_signature, whatsapp_error
from .test_whatsapp import webhook

SENDER = "4915112345678"


def text_message(body: str, message_id: str = "wamid.in1", sender: str = SENDER) -> dict:
    return {"from": sender, "id": message_id, "timestamp": "1762081200", "type": "text", "text": {"body": body}}


class WebhookTestCase(unittest.TestCase):
    model = True
    extra_settings: ClassVar[dict[str, str]] = {}

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.graph = FakeGraph().__enter__()
        self.twilio = FakeTwilio().__enter__()
        self.meta = FakeMeta().__enter__()
        self.meta.default_reply = "No outages right now."
        self.store = Store(Path(self.folder.name))
        mesh = MeshBuilder().gateway("gw").node("a", via="gw", clients=3).node("b", via="a", name="Daniel5")
        self.store.save_snapshot(mesh.snapshot())
        overrides = {
            "graph_base_url": self.graph.url,
            "twilio_base_url": self.twilio.url,
            "model_base_url": self.meta.base_url,
        }
        if self.model:
            overrides["model_api_key"] = "key-123"
        overrides.update(self.extra_settings)
        self.settings = make_settings(Path(self.folder.name), **overrides)
        self.app = make_webhook(self.settings, store=self.store)
        self.server = make_server(self.app, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        self.app.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.app.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        self.graph.__exit__()
        self.twilio.__exit__()
        self.meta.__exit__()
        self.folder.cleanup()

    def get(self, path: str) -> tuple[int, str]:
        try:
            with urllib.request.urlopen(self.base + path, timeout=5) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as err:
            with err:
                return err.code, err.read().decode()

    def post(self, payload, *, secret: str = "app-secret", raw: bytes | None = None) -> int:
        body = raw if raw is not None else json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        if secret:
            headers["X-Hub-Signature-256"] = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        request = urllib.request.Request(self.base + "/webhook", data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status
        except urllib.error.HTTPError as err:
            with err:
                return err.code

    def post_twilio(
        self, form: dict[str, str], *, token: str = FakeTwilio.auth_token, signed_url: str = "", headers=None
    ):
        """POST like Twilio does: form-encoded, signed over the public URL."""
        port = self.server.server_address[1]
        url = signed_url or f"https://127.0.0.1:{port}/twilio"
        all_headers = {"Content-Type": "application/x-www-form-urlencoded", **(headers or {})}
        if token:
            all_headers["X-Twilio-Signature"] = twilio_signature(token, url, form)
        body = urllib.parse.urlencode(form).encode()
        request = urllib.request.Request(self.base + "/twilio", data=body, headers=all_headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.headers.get("Content-Type"), response.read().decode()
        except urllib.error.HTTPError as err:
            with err:
                return err.code, err.headers.get("Content-Type"), err.read().decode()

    def finish(self) -> None:
        """Wait until every queued message has been answered."""
        self.app.close()

    def events(self, name: str) -> list[dict]:
        return [e for e in self.store.events() if e["event"] == name]


class VerificationTest(WebhookTestCase):
    def test_meta_subscription_check(self):
        self.assertEqual(
            self.get("/webhook?hub.mode=subscribe&hub.verify_token=verify-me&hub.challenge=1158201444"),
            (200, "1158201444"),
        )
        self.assertEqual(self.get("/webhook?hub.mode=subscribe&hub.verify_token=guess&hub.challenge=1")[0], 403)
        self.assertEqual(self.get("/webhook")[0], 403)

    def test_health_and_unknown_paths(self):
        self.assertEqual(self.get("/healthz"), (200, "ok"))
        self.assertEqual(self.get("/admin")[0], 404)

    def test_unsigned_or_forged_calls_are_rejected(self):
        payload = webhook(text_message("status"))
        self.assertEqual(self.post(payload, secret=""), 403)
        self.assertEqual(self.post(payload, secret="wrong"), 403)
        self.finish()
        self.assertEqual(self.graph.sent(), [])

    def test_malformed_bodies(self):
        self.assertEqual(self.post(None, raw=b"{not json"), 400)
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        try:
            connection.putrequest("POST", "/webhook")
            connection.putheader("Content-Length", str(5 * 1024 * 1024))
            connection.endheaders()
            self.assertEqual(connection.getresponse().status, 413)
        finally:
            connection.close()


class TextMessageTest(WebhookTestCase):
    def test_a_question_gets_an_answer(self):
        self.assertEqual(self.post(webhook(text_message("what is down right now?"))), 200)
        self.finish()
        sent = self.graph.sent()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["to"], SENDER)
        self.assertEqual(sent[0]["text"]["body"], "No outages right now.")
        self.assertEqual(sent[0]["context"], {"message_id": "wamid.in1"})
        question = self.meta.chats()[0].json()["messages"][-1]
        self.assertEqual(question, {"role": "user", "content": "what is down right now?"})
        self.assertTrue((Path(self.folder.name) / "inbox.json").exists(), "the 24-hour window is tracked")
        self.assertEqual(self.events("reply_sent")[0]["source"], "muse")
        self.assertNotIn(
            "what is down", (Path(self.folder.name) / "events.jsonl").read_text(), "message text is never logged"
        )

    def test_status_is_answered_without_the_model(self):
        self.post(webhook(text_message("status")))
        self.finish()
        self.assertIn("Nodes online: 3 of 3", self.graph.texts()[0])
        self.assertEqual(self.meta.chats(), [])

    def test_a_node_named_in_the_question_is_looked_up(self):
        self.post(webhook(text_message("is Daniel5 up?")))
        self.finish()
        state = self.meta.chats()[0].json()["messages"][1]["content"]
        self.assertIn('"name": "Daniel5"', state)

    def test_strangers_are_ignored(self):
        self.post(webhook(text_message("status", sender="15550001111")))
        self.finish()
        self.assertEqual(self.graph.sent(), [])

    def test_redelivered_messages_are_answered_once(self):
        payload = webhook(text_message("status"))
        self.post(payload)
        self.post(payload)
        self.finish()
        self.assertEqual(len(self.graph.sent()), 1)

    def test_failed_deliveries_are_logged_with_a_hint(self):
        self.post(
            webhook(
                statuses=[
                    {
                        "id": "wamid.out",
                        "status": "failed",
                        "recipient_id": SENDER,
                        "errors": [{"code": 131047, "title": "Re-engagement message"}],
                    }
                ]
            )
        )
        self.finish()
        self.assertEqual(self.events("delivery_failed")[0]["code"], 131047)

    def test_a_failed_reply_does_not_stop_the_worker(self):
        self.graph.fail_with = whatsapp_error(131030)
        self.post(webhook(text_message("status", "wamid.a")))
        self.post(webhook(text_message("help", "wamid.b")))
        self.finish()
        self.assertEqual(len(self.events("reply_failed")), 2)


class VoiceNoteTest(WebhookTestCase):
    def setUp(self):
        if voice_note() is None:
            self.skipTest("ffmpeg with Opus support is not installed")
        super().setUp()
        self.graph.media = voice_note()

    def audio(self, message_id: str = "wamid.v1") -> dict:
        return {
            "from": SENDER,
            "id": message_id,
            "timestamp": "1762081200",
            "type": "audio",
            "audio": {"mime_type": "audio/ogg; codecs=opus", "id": "m-voice1", "voice": True},
        }

    def test_a_voice_question_is_transcribed_and_answered(self):
        self.post(webhook(self.audio()))
        self.finish()
        reply = self.graph.texts()[0]
        self.assertEqual(reply, '🎙️ I heard: "what is down right now?"\n\nNo outages right now.')
        parts = self.meta.requests_to(r"/v1/asr/transcribe")[0].multipart()
        self.assertEqual(parts["audio"][1][:4], b"RIFF")
        self.assertEqual(json.loads(parts["request"][1])["languageBias"], ["English", "Telugu", "Hindi"])

    def test_telugu_speech_gets_a_telugu_frame(self):
        self.meta.transcript = "ఇప్పుడు ఏమి డౌన్ అయింది?"
        self.post(webhook(self.audio()))
        self.finish()
        self.assertTrue(self.graph.texts()[0].startswith("🎙️ నేను విన్నది"))


class NoModelVoiceTest(WebhookTestCase):
    model = False

    def test_voice_notes_need_the_model(self):
        self.post(
            webhook({"from": SENDER, "id": "wamid.v2", "timestamp": "1", "type": "audio", "audio": {"id": "m-x"}})
        )
        self.finish()
        self.assertIn("need ffmpeg and a Model API key", self.graph.texts()[0])


TWILIO = {
    "whatsapp_provider": "twilio",
    "twilio_account_sid": FakeTwilio.account_sid,
    "twilio_auth_token": FakeTwilio.auth_token,
}


def twilio_text(body: str, sid: str = "SMin1", sender: str = SENDER) -> dict[str, str]:
    return {
        "MessageSid": sid,
        "From": f"whatsapp:+{sender}",
        "To": "whatsapp:+14155238886",
        "Body": body,
        "NumMedia": "0",
    }


class TwilioWebhookTest(WebhookTestCase):
    extra_settings = TWILIO

    def test_a_question_is_answered_through_twilio(self):
        status, content_type, body = self.post_twilio(twilio_text("what is down right now?"))
        self.assertEqual(status, 200)
        self.assertTrue(content_type.startswith("text/xml"))
        self.assertIn("<Response>", body)
        self.finish()
        self.assertEqual(
            self.twilio.sent(),
            [{"From": "whatsapp:+14155238886", "To": f"whatsapp:+{SENDER}", "Body": "No outages right now."}],
        )
        self.assertEqual(self.graph.sent(), [])

    def test_forged_calls_are_rejected(self):
        self.assertEqual(self.post_twilio(twilio_text("status"), token="guess")[0], 403)
        self.assertEqual(self.post_twilio(twilio_text("status"), token="")[0], 403)
        self.finish()
        self.assertEqual(self.twilio.sent(), [])

    def test_the_address_seen_through_a_tunnel_is_what_twilio_signed(self):
        headers = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "quiet-river.trycloudflare.com"}
        status = self.post_twilio(
            twilio_text("status"), signed_url="https://quiet-river.trycloudflare.com/twilio", headers=headers
        )[0]
        self.assertEqual(status, 200)

    def test_strangers_are_ignored(self):
        self.post_twilio(twilio_text("status", sender="15550001111"))
        self.finish()
        self.assertEqual(self.twilio.sent(), [])

    def test_a_voice_note_through_twilio(self):
        note = voice_note()
        if note is None:
            self.skipTest("ffmpeg with Opus support is not installed")
        self.twilio.media = note
        form = {
            **twilio_text(""),
            "NumMedia": "1",
            "MediaUrl0": f"{self.twilio.url}/media/MMvoice",
            "MediaContentType0": "audio/ogg",
        }
        self.assertEqual(self.post_twilio(form)[0], 200)
        self.finish()
        self.assertEqual(self.twilio.bodies(), ['🎙️ I heard: "what is down right now?"\n\nNo outages right now.'])


class TwilioPublicUrlTest(WebhookTestCase):
    extra_settings: ClassVar[dict[str, str]] = {**TWILIO, "twilio_webhook_url": "https://uplink.example.org/twilio"}

    def test_a_configured_public_url_is_used_for_the_signature(self):
        self.assertEqual(
            self.post_twilio(twilio_text("status"), signed_url="https://uplink.example.org/twilio")[0], 200
        )
        self.assertEqual(self.post_twilio(twilio_text("status", sid="SMin2"))[0], 403)


if __name__ == "__main__":
    unittest.main()
