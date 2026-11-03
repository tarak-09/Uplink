import json
import unittest

from uplink.muse import MetaModelAPI, ModelAPIError
from uplink.transport import Transport

from .support import FakeMeta, json_reply


def client(meta: FakeMeta, **options) -> MetaModelAPI:
    return MetaModelAPI(api_key="key-123", transport=Transport(sleep=lambda s: None), base_url=meta.base_url, **options)


class ChatTest(unittest.TestCase):
    def test_sends_the_documented_request(self):
        with FakeMeta() as meta:
            meta.replies = ["*Daniel5 is down*"]
            reply = client(meta, reasoning_effort="minimal").chat(
                [
                    {"role": "developer", "content": "rules"},
                    {"role": "user", "content": "facts"},
                ]
            )
        self.assertEqual(reply, "*Daniel5 is down*")
        request = meta.chats()[0]
        self.assertEqual(request.headers["authorization"], "Bearer key-123")
        self.assertEqual(request.headers["content-type"], "application/json")
        body = request.json()
        self.assertEqual(body["model"], "muse-spark-1.3")
        self.assertEqual(body["reasoning_effort"], "minimal")
        self.assertEqual(body["max_completion_tokens"], 4096, "room for reasoning plus a Telugu alert")
        self.assertEqual([m["role"] for m in body["messages"]], ["developer", "user"])
        for unsupported in ("temperature", "stop", "n", "logprobs", "frequency_penalty", "presence_penalty"):
            self.assertNotIn(unsupported, body)

    def test_content_parts_are_joined(self):
        with FakeMeta() as meta:
            meta.replies = [
                json_reply(
                    {
                        "choices": [
                            {"message": {"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}}
                        ]
                    }
                )
            ]
            self.assertEqual(client(meta).chat([{"role": "user", "content": "q"}]), "ab")

    def test_busy_servers_are_retried(self):
        with FakeMeta() as meta:
            meta.replies = [json_reply({"error": {"message": "slow down"}}, 429, {"Retry-After": "0"}), "ok"]
            self.assertEqual(client(meta).chat([{"role": "user", "content": "q"}]), "ok")
            self.assertEqual(len(meta.chats()), 2)

    def test_errors_carry_the_api_message(self):
        with FakeMeta() as meta:
            meta.replies = [json_reply({"error": {"message": "reasoning_effort 'none' is not supported"}}, 400)]
            with self.assertRaises(ModelAPIError) as caught:
                client(meta).chat([{"role": "user", "content": "q"}])
            self.assertEqual(len(meta.chats()), 1, "client errors are not retried")
        self.assertIn("not supported", str(caught.exception))

    def test_a_reply_cut_off_at_the_token_limit_is_an_error(self):
        # Muse Spark's real replies to the Telugu cascade alert with a 1024-token cap: reasoning took
        # 952 tokens, and the text stopped mid-name, or never started.
        for content in ("*site-bcg5 వైఫల్యం, 7 నోడ్లు ఆఫ్‌లైన్*\n\n22/10 16:15 నుండి 7 నోడ్లు ఆఫ్‌లైన్: site-b", None):
            with self.subTest(content=content), FakeMeta() as meta:
                meta.replies = [
                    json_reply(
                        {
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {"role": "assistant", "content": content, "refusal": None},
                                    "finish_reason": "length",
                                }
                            ],
                            "usage": {"completion_tokens": 1024, "completion_tokens_details": {"reasoning_tokens": 952}},
                        }
                    )
                ]
                with self.assertRaises(ModelAPIError) as caught:
                    client(meta, max_completion_tokens=1024).chat([{"role": "user", "content": "q"}])
                self.assertIn("cut off at the 1024-token limit", str(caught.exception))
                self.assertEqual(len(meta.chats()), 1, "not retried: the same cap would cut it off again")

    def test_malformed_replies_are_errors(self):
        for body in ({"choices": []}, {"nothing": 1}, {"choices": [{"message": {"content": 5}}]}):
            with self.subTest(body=body), FakeMeta() as meta:
                meta.replies = [json_reply(body)]
                with self.assertRaises(ModelAPIError):
                    client(meta).chat([{"role": "user", "content": "q"}])

    def test_an_api_key_is_required(self):
        with self.assertRaises(ValueError):
            MetaModelAPI(api_key="", transport=Transport())


class TranscribeTest(unittest.TestCase):
    def test_sends_audio_and_request_as_multipart(self):
        with FakeMeta() as meta:
            meta.transcript = "  ఇప్పుడు ఏమి డౌన్ అయింది?  "
            transcript = client(meta).transcribe(
                b"RIFF....WAVE", languages=["Telugu", "English"], keywords=["Freifunk"]
            )
        self.assertEqual(transcript.text, "ఇప్పుడు ఏమి డౌన్ అయింది?")
        self.assertEqual((transcript.duration_ms, transcript.session_id), (2100, "s-1"))
        request = meta.requests_to(r"/v1/asr/transcribe")[0]
        self.assertEqual(request.headers["authorization"], "Bearer key-123")
        parts = request.multipart()
        kind, content, _ = parts["request"]
        self.assertEqual(kind, "application/json")
        self.assertEqual(
            json.loads(content),
            {
                "model": "muse-voice-transcribe-1.0",
                "audioEncoding": "WAV",
                "mode": "PUSH_TO_TALK",
                "languageBias": ["Telugu", "English"],
                "keywords": ["Freifunk"],
            },
        )
        self.assertEqual(parts["audio"], ("audio/wav", b"RIFF....WAVE", "voice.wav"))

    def test_optional_fields_are_left_out(self):
        with FakeMeta() as meta:
            client(meta).transcribe(b"RIFF")
        _, content, _ = meta.requests_to(r"/v1/asr/transcribe")[0].multipart()["request"]
        self.assertEqual(set(json.loads(content)), {"model", "audioEncoding", "mode"})


if __name__ == "__main__":
    unittest.main()
