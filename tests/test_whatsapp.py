import hashlib
import hmac
import json
import unittest

from uplink.transport import Transport
from uplink.whatsapp import (
    WhatsAppClient,
    WhatsAppError,
    normalize_number,
    parse_webhook,
    template_safe,
    verify_signature,
)

from .support import FakeGraph, json_reply, whatsapp_error


def client(graph: FakeGraph) -> WhatsAppClient:
    return WhatsAppClient(
        token="test-token", phone_number_id="1000001", transport=Transport(sleep=lambda s: None), base_url=graph.url
    )


def webhook(*messages, statuses=(), contacts=()):
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"display_phone_number": "15550000000", "phone_number_id": "1000001"},
                            "contacts": list(contacts),
                            "messages": list(messages),
                            "statuses": list(statuses),
                        },
                    }
                ],
            }
        ],
    }


class SendTest(unittest.TestCase):
    def test_text_messages(self):
        with FakeGraph() as graph:
            ref = client(graph).send_text("+49 151 1234-5678", "hello", reply_to="wamid.in")
        self.assertEqual(ref, "wamid.0")
        request = graph.requests[0]
        self.assertEqual(request.path, "/v26.0/1000001/messages")
        self.assertEqual(request.headers["authorization"], "Bearer test-token")
        self.assertEqual(
            request.json(),
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": "4915112345678",
                "type": "text",
                "text": {"preview_url": False, "body": "hello"},
                "context": {"message_id": "wamid.in"},
            },
        )

    def test_long_texts_are_clipped_to_the_api_limit(self):
        with FakeGraph() as graph:
            client(graph).send_text("4915112345678", "x" * 5000)
        self.assertEqual(len(graph.texts()[0]), 4096)

    def test_templates_carry_a_flattened_parameter(self):
        with FakeGraph() as graph:
            client(graph).send_template(
                "4915112345678", "uplink_alert", "en", ("*Down*\nsince 16:15\n\n  check power",)
            )
        template = graph.sent()[0]["template"]
        self.assertEqual(template["name"], "uplink_alert")
        self.assertEqual(template["components"][0]["parameters"][0]["text"], "*Down* · since 16:15 · check power")

    def test_hello_world_needs_no_parameters(self):
        with FakeGraph() as graph:
            client(graph).send_template("4915112345678", "hello_world", "en_US")
        self.assertEqual(graph.sent()[0]["template"], {"name": "hello_world", "language": {"code": "en_US"}})

    def test_errors_explain_what_to_do(self):
        with FakeGraph() as graph:
            graph.fail_with = whatsapp_error(131030, "Recipient phone number not in allowed list")
            with self.assertRaises(WhatsAppError) as caught:
                client(graph).send_text("4915112345678", "hi")
        error = caught.exception
        self.assertEqual((error.code, error.status), (131030, 400))
        self.assertIn("allowed list", str(error))
        self.assertIn("API Setup", error.hint)

    def test_expired_tokens_are_recognised(self):
        with FakeGraph() as graph:
            graph.fail_with = whatsapp_error(190, "Error validating access token", status=401)
            with self.assertRaises(WhatsAppError) as caught:
                client(graph).send_text("4915112345678", "hi")
        self.assertIn("24 hours", caught.exception.hint)

    def test_a_reply_without_an_id_is_an_error(self):
        with FakeGraph() as graph:
            graph.fail_with = json_reply({"messaging_product": "whatsapp"})
            with self.assertRaises(WhatsAppError):
                client(graph).send_text("4915112345678", "hi")


class MediaTest(unittest.TestCase):
    def test_voice_notes_download_in_two_authorised_steps(self):
        with FakeGraph() as graph:
            graph.media = b"OggS-voice"
            media = client(graph).download_media("m-123")
            paths = [(r.path, r.headers.get("authorization")) for r in graph.requests]
        self.assertEqual((media.content, media.mime_type), (b"OggS-voice", "audio/ogg"))
        self.assertEqual(paths, [("/v26.0/m-123", "Bearer test-token"), ("/media/m-123", "Bearer test-token")])

    def test_a_url_from_the_webhook_saves_a_step(self):
        with FakeGraph() as graph:
            graph.media = b"OggS"
            client(graph).download_media("m-9", url=f"{graph.url}/media/m-9")
            self.assertEqual([r.path for r in graph.requests], ["/media/m-9"])


class WebhookTest(unittest.TestCase):
    def test_signatures(self):
        body = b'{"object": "whatsapp_business_account"}'
        good = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
        self.assertTrue(verify_signature("secret", body, good))
        self.assertTrue(verify_signature("secret", body, good.upper().replace("SHA256=", "sha256=")))
        self.assertFalse(verify_signature("secret", body + b" ", good))
        self.assertFalse(verify_signature("other", body, good))
        self.assertFalse(verify_signature("secret", body, None))
        self.assertFalse(verify_signature("secret", body, good.removeprefix("sha256=")))
        self.assertFalse(verify_signature("", body, good))

    def test_text_and_voice_messages(self):
        payload = webhook(
            {
                "from": "4915112345678",
                "id": "wamid.1",
                "timestamp": "1762081200",
                "type": "text",
                "text": {"body": "status"},
            },
            {
                "from": "4915112345678",
                "id": "wamid.2",
                "timestamp": "1762081260",
                "type": "audio",
                "audio": {"mime_type": "audio/ogg; codecs=opus", "sha256": "abc", "id": "m-1", "voice": True},
            },
            {"from": "4915112345678", "id": "wamid.3", "timestamp": "1762081300", "type": "sticker", "sticker": {}},
            contacts=[{"profile": {"name": "Vishnu"}, "wa_id": "4915112345678"}],
        )
        messages, statuses = parse_webhook(payload)
        self.assertEqual(statuses, [])
        self.assertEqual(
            [(m.id, m.kind) for m in messages], [("wamid.1", "text"), ("wamid.2", "audio"), ("wamid.3", "other")]
        )
        self.assertEqual((messages[0].text, messages[0].sender_name), ("status", "Vishnu"))
        self.assertEqual((messages[1].media_id, messages[1].mime_type), ("m-1", "audio/ogg; codecs=opus"))
        self.assertEqual(messages[0].sent_at.timestamp(), 1762081200)

    def test_delivery_failures(self):
        payload = webhook(
            statuses=[
                {
                    "id": "wamid.out",
                    "status": "failed",
                    "timestamp": "1762081200",
                    "recipient_id": "4915112345678",
                    "errors": [{"code": 131047, "title": "Re-engagement message"}],
                }
            ]
        )
        _, statuses = parse_webhook(payload)
        self.assertEqual(
            [(s.status, s.error_code, s.error_title) for s in statuses], [("failed", 131047, "Re-engagement message")]
        )

    def test_unexpected_shapes_are_ignored(self):
        for payload in (
            None,
            [],
            {"object": "page"},
            {"object": "whatsapp_business_account", "entry": "x"},
            webhook({"id": 5}),
            webhook({"from": "1", "type": "text"}),
            json.loads(
                '{"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages", "value": []}]}]}'
            ),
        ):
            with self.subTest(payload=payload):
                self.assertEqual(parse_webhook(payload), ([], []))

    def test_number_and_template_helpers(self):
        self.assertEqual(normalize_number("+49 (151) 1234-5678"), "4915112345678")
        self.assertEqual(template_safe("a\n\nb\tc     d"), "a · b c d")
        self.assertEqual(len(template_safe("y" * 2000)), 1000)


if __name__ == "__main__":
    unittest.main()
