import base64
import unittest
from typing import ClassVar

from uplink.transport import Transport
from uplink.twilio import TwilioWhatsApp, parse_twilio_webhook, verify_twilio_signature
from uplink.whatsapp import WhatsAppError

from .support import FakeTwilio, twilio_error


def client(twilio: FakeTwilio, token: str = FakeTwilio.auth_token) -> TwilioWhatsApp:
    return TwilioWhatsApp(
        account_sid=FakeTwilio.account_sid,
        auth_token=token,
        transport=Transport(sleep=lambda s: None),
        base_url=twilio.url,
    )


class SignatureTest(unittest.TestCase):
    URL = "https://example.com/myapp.php?foo=1&bar=2"
    PARAMS = (
        ("CallSid", "CA1234567890ABCDE"),
        ("Caller", "+14158675310"),
        ("Digits", "1234"),
        ("From", "+14158675310"),
        ("To", "+18005551212"),
    )

    def test_matches_twilios_documented_example(self):
        self.assertTrue(verify_twilio_signature("12345", self.URL, self.PARAMS, "L/OH5YylLD5NRKLltdqwSvS0BnU="))

    def test_order_of_parameters_does_not_matter(self):
        self.assertTrue(
            verify_twilio_signature("12345", self.URL, reversed(self.PARAMS), "L/OH5YylLD5NRKLltdqwSvS0BnU=")
        )

    def test_anything_changed_fails(self):
        good = "L/OH5YylLD5NRKLltdqwSvS0BnU="
        self.assertFalse(verify_twilio_signature("54321", self.URL, self.PARAMS, good))
        self.assertFalse(verify_twilio_signature("12345", self.URL.replace("https", "http"), self.PARAMS, good))
        self.assertFalse(verify_twilio_signature("12345", self.URL, (*self.PARAMS, ("Body", "x")), good))
        self.assertFalse(verify_twilio_signature("12345", self.URL, self.PARAMS, None))
        self.assertFalse(verify_twilio_signature("", self.URL, self.PARAMS, good))


class SendTest(unittest.TestCase):
    def test_a_text_goes_out_from_the_sandbox_number(self):
        with FakeTwilio() as twilio:
            sid = client(twilio).send_text("+49 151 12345678", "*Daniel5 is down*\n7 nodes")
            request = twilio.requests[0]
        self.assertEqual(sid, "SM" + "0" * 32)
        self.assertEqual(request.path, "/2010-04-01/Accounts/ACtest/Messages.json")
        self.assertEqual(request.headers["content-type"], "application/x-www-form-urlencoded")
        self.assertEqual(request.headers["authorization"], "Basic " + base64.b64encode(b"ACtest:twilio-token").decode())
        self.assertEqual(
            twilio.sent(),
            [{"From": "whatsapp:+14155238886", "To": "whatsapp:+4915112345678", "Body": "*Daniel5 is down*\n7 nodes"}],
        )

    def test_long_messages_are_clipped_to_twilios_limit(self):
        with FakeTwilio() as twilio:
            client(twilio).send_text("4915112345678", "x" * 3000)
        self.assertEqual(len(twilio.bodies()[0]), 1600)

    def test_errors_explain_the_sandbox(self):
        with FakeTwilio() as twilio:
            twilio.fail_with = twilio_error(
                63015, "Channel Sandbox can only send messages to phone numbers that have joined the Sandbox"
            )
            with self.assertRaises(WhatsAppError) as caught:
                client(twilio).send_text("4915112345678", "hi")
        self.assertEqual(caught.exception.code, 63015)
        self.assertIn("join", caught.exception.hint)
        self.assertIn("+1 415 523 8886", caught.exception.hint)

    def test_wrong_credentials_are_named(self):
        with FakeTwilio() as twilio, self.assertRaises(WhatsAppError) as caught:
            client(twilio, token="wrong").send_text("4915112345678", "hi")
        self.assertEqual(caught.exception.code, 20003)
        self.assertIn("TWILIO_AUTH_TOKEN", caught.exception.hint)

    def test_templates_are_not_available_in_the_sandbox(self):
        with FakeTwilio() as twilio:
            with self.assertRaises(WhatsAppError) as caught:
                client(twilio).send_template("4915112345678", "hello_world")
            self.assertEqual(twilio.requests, [])
        self.assertIn("sandbox", str(caught.exception))


class MediaTest(unittest.TestCase):
    def test_credentials_do_not_follow_the_redirect_to_storage(self):
        with FakeTwilio() as twilio:
            twilio.media = b"OggS-voice"
            media = client(twilio).download_media("MM1", f"{twilio.url}/media/MM1")
            paths = [(r.path, "authorization" in r.headers) for r in twilio.requests]
        self.assertEqual((media.content, media.mime_type), (b"OggS-voice", "audio/ogg"))
        self.assertEqual(paths, [("/media/MM1", True), ("/storage/MM1", False)])

    def test_a_missing_url_is_an_error(self):
        with FakeTwilio() as twilio, self.assertRaises(WhatsAppError):
            client(twilio).download_media("MM1")


class WebhookTest(unittest.TestCase):
    BASE: ClassVar[dict[str, str]] = {
        "MessageSid": "SM1",
        "From": "whatsapp:+4915112345678",
        "To": "whatsapp:+14155238886",
        "ProfileName": "Vishnu",
        "WaId": "4915112345678",
    }

    def test_a_text(self):
        message = parse_twilio_webhook({**self.BASE, "Body": "status", "NumMedia": "0"})
        self.assertEqual(
            (message.id, message.kind, message.text, message.sender), ("SM1", "text", "status", "4915112345678")
        )
        self.assertEqual(message.sender_name, "Vishnu")

    def test_a_voice_note(self):
        form = {
            **self.BASE,
            "Body": "",
            "NumMedia": "1",
            "MediaUrl0": "https://api.twilio.com/m/1",
            "MediaContentType0": "audio/ogg",
        }
        message = parse_twilio_webhook(form)
        self.assertEqual(
            (message.kind, message.media_url, message.mime_type), ("audio", "https://api.twilio.com/m/1", "audio/ogg")
        )

    def test_other_media_and_empty_messages(self):
        image = {**self.BASE, "Body": "look", "NumMedia": "1", "MediaUrl0": "u", "MediaContentType0": "image/jpeg"}
        self.assertEqual(parse_twilio_webhook(image).kind, "other")
        self.assertEqual(parse_twilio_webhook({**self.BASE, "Body": "  "}).kind, "other")

    def test_non_whatsapp_or_incomplete_calls_are_ignored(self):
        self.assertIsNone(parse_twilio_webhook({**self.BASE, "From": "+4915112345678", "Body": "sms"}))
        self.assertIsNone(parse_twilio_webhook({"From": "whatsapp:+4915112345678", "Body": "no sid"}))


if __name__ == "__main__":
    unittest.main()
