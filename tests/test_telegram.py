"""Telegram: sending as HTML, voice notes, long polling, and the bot answering through Uplink's reply queue."""

import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from uplink.app import make_telegram_poller, make_webhook
from uplink.store import Store
from uplink.telegram import TelegramBot, TelegramPoller, parse_update, recent_chats, to_html
from uplink.transport import Transport
from uplink.whatsapp import WhatsAppError

from .audio import voice_note
from .support import FakeMeta, FakeTelegram, MeshBuilder, make_settings, telegram_error, telegram_text

CHAT = str(FakeTelegram.chat_id)
TOKEN_SECRET = FakeTelegram.token.partition(":")[2]


def bot(telegram: FakeTelegram, token: str = FakeTelegram.token) -> TelegramBot:
    return TelegramBot(token=token, transport=Transport(sleep=lambda s: None), base_url=telegram.url)


def until(condition, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.01)


class FormattingTest(unittest.TestCase):
    def test_whatsapp_bold_becomes_html_bold(self):
        self.assertEqual(
            to_html("*a.home is down*\n2 nodes offline since 12:00."), "<b>a.home is down</b>\n2 nodes offline since 12:00."
        )
        self.assertEqual(to_html("*7 nodes* offline (*site_bcg5*)"), "<b>7 nodes</b> offline (<b>site_bcg5</b>)")
        telugu = "*site-bcg5 ఆఫ్‌లైన్ అయింది*\nముందుగా చూడండి"
        self.assertEqual(to_html(telugu), "<b>site-bcg5 ఆఫ్‌లైన్ అయింది</b>\nముందుగా చూడండి")

    def test_stars_that_are_not_bold_stay(self):
        for text in ("* site-bca3\n* site-bca5", "2*3*4", "* spaced *", "**", "*two\nlines*", "a*b*c"):
            with self.subTest(text=text):
                self.assertEqual(to_html(text), text)
        self.assertEqual(to_html("* Check first: power at *site-bcg5*"), "* Check first: power at <b>site-bcg5</b>")

    def test_node_names_and_html_characters_are_shown_as_written(self):
        markup = to_html("*ffmuc_<gw> & co_1*: a<b & c>d \"q\" 'x'")
        self.assertEqual(markup, "<b>ffmuc_&lt;gw&gt; &amp; co_1</b>: a&lt;b &amp; c&gt;d \"q\" 'x'")
        self.assertEqual(telegram_text(markup), "ffmuc_<gw> & co_1: a<b & c>d \"q\" 'x'")


class SendTest(unittest.TestCase):
    def test_a_message_goes_to_the_chat_as_html(self):
        with FakeTelegram() as telegram:
            ref = bot(telegram).send_text(CHAT, "*a.home is down*\n2 nodes & 3 people")
            request = telegram.requests[0]
        self.assertEqual(ref, "1")
        self.assertEqual(request.path, f"/bot{FakeTelegram.token}/sendMessage")
        self.assertEqual(request.headers["content-type"], "application/json")
        self.assertEqual(
            request.json(),
            {"chat_id": 4242, "text": "<b>a.home is down</b>\n2 nodes &amp; 3 people", "parse_mode": "HTML"},
        )

    def test_a_reply_points_at_the_question(self):
        with FakeTelegram() as telegram:
            bot(telegram).send_text(CHAT, "No outages right now.", reply_to="7")
        self.assertEqual(telegram.sent()[0]["reply_parameters"], {"message_id": 7, "allow_sending_without_reply": True})

    def test_long_messages_are_clipped_to_what_the_reader_sees(self):
        with FakeTelegram() as telegram:
            bot(telegram).send_text(CHAT, "x" * 5000)
            bot(telegram).send_text(CHAT, "&" * 5000)
        plain = [telegram_text(markup) for markup in telegram.texts()]
        self.assertEqual([len(text) for text in plain], [4096, 4096], "escaping does not count, and no entity is cut")
        self.assertEqual(plain[1][-2:], "&…")

    def test_refusals_explain_what_to_do(self):
        with FakeTelegram() as telegram, self.assertRaises(WhatsAppError) as caught:
            bot(telegram).send_text("5550001", "hi")
        error = caught.exception
        self.assertEqual((error.code, error.status), (400, 400))
        self.assertIn("chat not found", str(error))
        self.assertIn("telegram-chat-id", error.hint)

    def test_the_token_never_appears_in_an_error(self):
        wrong = "999999:not-the-real-token"
        with FakeTelegram() as telegram:
            with self.assertRaises(WhatsAppError) as refused:
                bot(telegram, token=wrong).send_text(CHAT, "hi")
            telegram.fail_with = (502, {"Content-Type": "text/html"}, b"<html>Bad Gateway</html>")
            with self.assertRaises(WhatsAppError) as proxied:
                bot(telegram).send_text(CHAT, "hi")
        with socket.create_server(("127.0.0.1", 0)) as listener:  # takes the connection, then hangs up

            def hang_up():
                connection, _ = listener.accept()
                connection.close()

            hanger = threading.Thread(target=hang_up)
            hanger.start()
            port = listener.getsockname()[1]
            offline = TelegramBot(
                token=FakeTelegram.token, transport=Transport(retries=0, timeout=2), base_url=f"http://127.0.0.1:{port}"
            )
            with self.assertRaises(WhatsAppError) as unreachable:
                offline.send_text(CHAT, "hi")
            hanger.join(5)
        self.assertIn("TELEGRAM_BOT_TOKEN", refused.exception.hint)
        self.assertIn("HTTP 502", str(proxied.exception))
        self.assertIn("could not reach Telegram", str(unreachable.exception))
        for error, secret in (
            (refused.exception, "not-the-real-token"),
            (proxied.exception, TOKEN_SECRET),
            (unreachable.exception, TOKEN_SECRET),
        ):
            with self.subTest(error=str(error)):
                self.assertNotIn(secret, f"{error} {error.hint or ''}")

    def test_telegram_has_no_templates(self):
        with FakeTelegram() as telegram:
            with self.assertRaises(WhatsAppError) as caught:
                bot(telegram).send_template(CHAT, "hello_world")
            self.assertEqual(telegram.requests, [])
        self.assertIn("no message templates", str(caught.exception))


class MediaTest(unittest.TestCase):
    def test_a_voice_note_is_fetched_by_its_file_id(self):
        with FakeTelegram() as telegram:
            telegram.media = b"OggS-voice"
            media = bot(telegram).download_media(FakeTelegram.voice_file_id)
            requests = [(r.path, r.body) for r in telegram.requests]
        self.assertEqual(media.content, b"OggS-voice")
        self.assertEqual(
            requests,
            [
                (f"/bot{FakeTelegram.token}/getFile", b'{"file_id": "AwACAgIAAxkBAAIBvoice"}'),
                (f"/file/bot{FakeTelegram.token}/voice/file_0.oga", b""),
            ],
        )

    def test_an_unknown_file_is_an_error(self):
        with FakeTelegram() as telegram, self.assertRaises(WhatsAppError) as caught:
            bot(telegram).download_media("gone")
        self.assertIn("invalid file_id", str(caught.exception))


class ParseTest(unittest.TestCase):
    def update(self, **fields) -> dict:
        message = {"message_id": 7, "from": {"id": 4242, "first_name": "Vishnu"}, "date": 1762081200}
        return {"update_id": 1, "message": {**message, "chat": {"id": 4242, "type": "private"}, **fields}}

    def test_texts_and_voice_notes(self):
        text = parse_update(self.update(text="status"))
        self.assertEqual(
            (text.id, text.sender, text.kind, text.text, text.sender_name), ("7", "4242", "text", "status", "Vishnu")
        )
        self.assertEqual(text.sent_at.timestamp(), 1762081200)
        voice = parse_update(self.update(voice={"file_id": "AwAC-1", "mime_type": "audio/ogg", "duration": 2}))
        self.assertEqual(
            (voice.kind, voice.media_id, voice.media_url, voice.mime_type), ("audio", "AwAC-1", "", "audio/ogg")
        )

    def test_other_messages_and_updates(self):
        sticker = parse_update(self.update(chat={"id": -1001234, "type": "supergroup"}, sticker={}))
        self.assertEqual((sticker.kind, sticker.sender), ("other", "-1001234"))
        for update in (
            None,
            {},
            {"update_id": 2, "edited_message": self.update(text="x")["message"]},
            {"update_id": 3, "message": {"text": "no chat"}},
            {"update_id": 4, "message": {"message_id": "7", "chat": {"id": 1}}},
        ):
            with self.subTest(update=update):
                self.assertIsNone(parse_update(update))

    def test_recent_chats_once_each_newest_last(self):
        updates = [
            self.update(text="a"),
            {"update_id": 2, "my_chat_member": {"chat": {"id": -1009, "title": "Volunteers", "type": "supergroup"}}},
            self.update(text="b"),
        ]
        self.assertEqual(
            [(c.id, c.kind, c.name) for c in recent_chats(updates)],
            [(-1009, "supergroup", "Volunteers"), (4242, "private", "")],
        )


class PollerTest(unittest.TestCase):
    def setUp(self):
        self.telegram = FakeTelegram().__enter__()
        self.delivered = []
        self.poller = TelegramPoller(bot(self.telegram), CHAT, self.delivered.append, backoff=0.01)

    def tearDown(self):
        self.telegram.__exit__()

    def test_each_poll_confirms_what_the_last_one_fetched(self):
        first = self.telegram.push("status")
        self.telegram.push("what is down?")
        self.assertEqual(self.poller.poll_once(), 2)
        self.telegram.push("thanks")
        self.assertEqual(self.poller.poll_once(), 1)
        self.assertEqual(self.poller.poll_once(), 0)
        self.assertEqual([m.text for m in self.delivered], ["status", "what is down?", "thanks"])
        polls = self.telegram.polls()
        self.assertNotIn("offset", polls[0])
        self.assertEqual([p["offset"] for p in polls[1:]], [first + 2, first + 3])
        self.assertEqual((polls[0]["timeout"], polls[0]["allowed_updates"]), (30, ["message"]))

    def test_other_chats_are_ignored_and_not_fetched_again(self):
        self.telegram.push("hello bot", chat_id=5550001)
        self.telegram.push("status")
        with self.assertLogs("uplink.telegram", "INFO") as logs:
            self.assertEqual(self.poller.poll_once(), 1)
        self.assertEqual([m.text for m in self.delivered], ["status"])
        self.assertIn("5550001", logs.output[0])
        self.assertEqual(self.poller.poll_once(), 0)

    def test_a_group_chat_can_be_the_one(self):
        poller = TelegramPoller(bot(self.telegram), "-1001234", self.delivered.append)
        self.telegram.push("status", chat_id=-1001234)
        self.assertEqual(poller.poll_once(), 1)
        self.assertEqual(self.delivered[0].sender, "-1001234")

    def test_failures_are_logged_without_the_token_and_polling_goes_on(self):
        conflict = "Conflict: terminated by other getUpdates request; make sure that only one bot instance is running"
        self.telegram.fail_updates = [telegram_error(409, conflict)] + [(502, {}, b"Bad Gateway")] * 3  # 3: retries
        self.telegram.push("status")
        stop = threading.Event()
        thread = threading.Thread(target=self.poller.run, args=(stop,))
        with self.assertLogs("uplink.telegram", "ERROR") as logs:
            thread.start()
            until(lambda: self.delivered)
            stop.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual([m.text for m in self.delivered], ["status"])
        self.assertEqual(len(logs.output), 2)
        self.assertIn("only one can at a time", logs.output[0])
        self.assertIn("HTTP 502", logs.output[1])
        self.assertNotIn(TOKEN_SECRET, "\n".join(logs.output))


class AnsweringTest(unittest.TestCase):
    """The bot end to end: polled messages go through the webhooks' reply queue and are answered."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.telegram = FakeTelegram().__enter__()
        self.meta = FakeMeta().__enter__()
        self.meta.default_reply = "No outages right now."
        self.store = Store(Path(self.folder.name))
        self.store.save_snapshot(MeshBuilder().gateway("gw").node("a", via="gw", clients=3).snapshot())
        self.settings = make_settings(
            Path(self.folder.name),
            whatsapp_provider="telegram",
            telegram_bot_token=FakeTelegram.token,
            telegram_chat_id=CHAT,
            telegram_base_url=self.telegram.url,
            model_api_key="key-123",
            model_base_url=self.meta.base_url,
        )
        self.app = make_webhook(self.settings, store=self.store)
        self.poller = make_telegram_poller(self.settings, self.app, transport=Transport(sleep=lambda s: None))
        self.app.start()

    def tearDown(self):
        self.app.close()
        self.telegram.__exit__()
        self.meta.__exit__()
        self.folder.cleanup()

    def test_a_question_is_answered_in_the_chat(self):
        stop = threading.Event()
        thread = threading.Thread(target=self.poller.run, args=(stop,))
        thread.start()
        try:
            self.telegram.push("what is down right now?")
            self.telegram.wait_for(1)
        finally:
            stop.set()
            thread.join(5)
        self.app.close()
        sent = self.telegram.sent()
        self.assertEqual(len(sent), 1)
        self.assertEqual((sent[0]["chat_id"], sent[0]["text"]), (4242, "No outages right now."))
        self.assertEqual(sent[0]["reply_parameters"]["message_id"], 1)
        question = self.meta.chats()[0].json()["messages"][-1]
        self.assertEqual(question, {"role": "user", "content": "what is down right now?"})

    def test_status_comes_back_with_bold_as_html(self):
        self.telegram.push("status")
        self.poller.poll_once()
        self.app.close()
        self.assertEqual(
            self.telegram.texts(), ["<b>Freifunk München right now</b>\nNodes online: 2 of 2\nPeople connected: 3\nNo ongoing outages."]
        )

    def test_a_follow_up_builds_on_the_last_answer(self):
        self.meta.replies = ["No outages right now.", "ప్రస్తుతం అంతరాయాలు ఏవీ లేవు."]
        self.telegram.push("what is down right now?")
        self.telegram.push("can you say that in Telugu?")
        self.assertEqual(self.poller.poll_once(), 2)
        self.app.close()
        self.assertEqual(self.telegram.texts(), ["No outages right now.", "ప్రస్తుతం అంతరాయాలు ఏవీ లేవు."])
        follow_up = self.meta.chats()[1].json()["messages"]
        self.assertEqual(
            [(m["role"], m["content"]) for m in follow_up[2:]],
            [
                ("user", "what is down right now?"),
                ("assistant", "No outages right now."),
                ("user", "can you say that in Telugu?"),
            ],
        )

    def test_strangers_get_no_answer(self):
        self.telegram.push("status", chat_id=5550001)
        self.poller.poll_once()
        self.app.close()
        self.assertEqual(self.telegram.sent(), [])

    def test_a_voice_note_through_the_poller(self):
        note = voice_note()
        if note is None:
            self.skipTest("ffmpeg with Opus support is not installed")
        self.telegram.media = note
        self.telegram.push(voice=True)
        self.assertEqual(self.poller.poll_once(), 1)
        self.app.close()
        self.assertEqual(self.telegram.texts(), ['🎙️ I heard: "what is down right now?"\n\nNo outages right now.'])
        parts = self.meta.requests_to(r"/v1/asr/transcribe")[0].multipart()
        self.assertEqual(parts["audio"][1][:4], b"RIFF")


if __name__ == "__main__":
    unittest.main()
