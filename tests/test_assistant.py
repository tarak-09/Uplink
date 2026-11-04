"""Replies: the last message sent comes along for 24 hours, each chat's last exchange for ten minutes."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from uplink.assistant import Assistant
from uplink.store import Store
from uplink.writer import Writer

from .support import Clock, MeshBuilder, ScriptedModel, make_settings

# The facts behind the rooftop cascade alert from the real 23 October snapshot.
ALERT = {
    "event": "outage",
    "kind": "cascade",
    "root": "site-bcg5",
    "started": "22/10 16:15 Munich time",
    "nodes_offline": 7,
    "failed_within_seconds": 15,
    "evidence": [
        "the other 6 reach the internet only through site-bcg5",
        "their names form one set (site-bca…, site-bcg…)",
    ],
    "likely_cause": "site-bcg5 failed (power, cabling or its own uplink) and the nodes behind it had no other path",
    "check_first": "power and cabling at site-bcg5, then its mesh link to site-bkl",
}
WHY = (
    "site-bcg5 failed first, and the 6 nodes behind it reach the internet only through it: "
    "all 7 went quiet within 15 seconds."
)


class LastAlertStoreTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.store = Store(Path(self.folder.name), clock=self.clock)

    def tearDown(self):
        self.folder.cleanup()

    def test_round_trip(self):
        self.assertIsNone(self.store.load_last_alert(), "nothing sent yet")
        self.store.save_last_alert(ALERT)
        self.assertEqual(self.store.load_last_alert(), ALERT)

    def test_it_expires_after_24_hours(self):
        self.store.save_last_alert(ALERT)
        self.clock.advance(hours=24)
        self.assertEqual(self.store.load_last_alert(), ALERT)
        self.clock.advance(seconds=1)
        self.assertIsNone(self.store.load_last_alert())

    def test_the_wall_clock_dates_it_not_the_snapshot(self):
        store = Store(Path(self.folder.name))  # Uplink's own clock; the alert's facts are from 22 October
        store.save_last_alert(ALERT)
        saved = json.loads(Path(self.folder.name, "last_alert.json").read_text(encoding="utf-8"))
        self.assertLess(
            abs(datetime.now(timezone.utc) - datetime.fromisoformat(saved["sent_at"])), timedelta(minutes=1)
        )
        self.assertEqual(store.load_last_alert(), ALERT)

    def test_an_unreadable_file_counts_as_no_alert(self):
        path = Path(self.folder.name, "last_alert.json")
        now = self.clock.now.isoformat()
        for content in (
            "{not json",
            "[]",
            json.dumps({"sent_at": "yesterday", "facts": ALERT}),
            json.dumps({"sent_at": self.clock.now.replace(tzinfo=None).isoformat(), "facts": ALERT}),  # no zone
            json.dumps({"sent_at": now, "facts": "not a fact sheet"}),
        ):
            with self.subTest(content=content):
                path.write_text(content, encoding="utf-8")
                self.assertIsNone(self.store.load_last_alert())


class AssistantTestCase(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.folder.name))
        self.store.save_snapshot(MeshBuilder().gateway("gw").node("a", via="gw", clients=3).snapshot())
        self.model = ScriptedModel()
        self.clock = Clock()
        writer = Writer(self.model, language="english", network="Freifunk München")
        self.assistant = Assistant(
            store=self.store, settings=make_settings(Path(self.folder.name)), writer=writer, clock=self.clock
        )

    def tearDown(self):
        self.folder.cleanup()

    def ask(self, question: str, chat: str = "4242", reply: str = "Nothing is down right now."):
        self.model.replies.append(reply)
        return self.assistant.reply(question, chat=chat)

    def earlier_turns(self, call: int) -> list[tuple[str, str]]:
        """What the model saw between the network state and the question."""
        return [(m["role"], m["content"]) for m in self.model.calls[call][2:-1]]

    def state_seen(self, call: int) -> dict:
        """The network state the model was given."""
        return json.loads(self.model.calls[call][1]["content"].removeprefix("Network state:\n"))


class ReplyToAlertTest(AssistantTestCase):
    def test_why_is_answered_from_the_last_alert(self):
        self.store.save_last_alert(ALERT)
        draft = self.ask("why?", reply=WHY)
        self.assertEqual(self.state_seen(0)["last_alert"]["evidence"], ALERT["evidence"])
        self.assertEqual((draft.text, draft.source), (WHY, "muse"), "its 15 seconds pass the number check")

    def test_without_a_last_alert_the_same_numbers_are_refused(self):
        self.model.replies.append(WHY)  # the rewrite repeats them
        draft = self.ask("why?", reply=WHY)
        self.assertNotIn("last_alert", self.state_seen(0))
        self.assertEqual(draft.source, "template")
        self.assertIn("15", draft.note)


class FollowUpTest(AssistantTestCase):
    def test_a_follow_up_sees_the_last_question_and_answer(self):
        first = self.ask("what is down right now?")
        second = self.ask("can you say that in Telugu?", reply="ప్రస్తుతం ఏదీ డౌన్ లేదు.")
        self.assertEqual(self.earlier_turns(0), [])
        self.assertEqual(self.earlier_turns(1), [("user", "what is down right now?"), ("assistant", first.text)])
        self.assertEqual((second.text, second.source), ("ప్రస్తుతం ఏదీ డౌన్ లేదు.", "muse"))

    def test_the_last_exchange_is_in_the_state_as_previous_exchange(self):
        first = self.ask("what is down right now?")
        self.ask("say that in Telugu")
        self.assertNotIn("previous_exchange", self.state_seen(0))
        self.assertEqual(
            self.state_seen(1)["previous_exchange"], {"question": "what is down right now?", "answer": first.text}
        )

    def test_template_answers_are_remembered_too(self):
        status = self.assistant.reply("status", chat="4242")
        self.ask("say that in Hindi")
        self.assertEqual(status.source, "template")
        self.assertEqual(self.earlier_turns(0), [("user", "status"), ("assistant", status.text)])

    def test_the_last_exchange_is_forgotten_after_ten_minutes(self):
        self.ask("what is down?")
        self.clock.advance(minutes=9)
        self.ask("and now?")
        self.clock.advance(minutes=10, seconds=1)
        self.ask("say that in Telugu")
        self.assertEqual([len(self.earlier_turns(call)) for call in range(3)], [0, 2, 0])
        self.assertNotIn("previous_exchange", self.state_seen(2))

    def test_chats_do_not_share_their_exchanges(self):
        self.ask("what is down?", chat="4242")
        self.ask("say that in Telugu", chat="5550001")
        self.assertEqual(self.earlier_turns(1), [])

    def test_without_a_chat_nothing_is_remembered(self):
        self.assistant.reply("status")  # `uplink ask` in the terminal has no chat
        self.ask("say that in Telugu", chat="")
        self.assertEqual(self.earlier_turns(0), [])


if __name__ == "__main__":
    unittest.main()
