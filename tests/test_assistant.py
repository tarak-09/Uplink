"""Follow-ups: each chat's last question and answer are remembered for ten minutes, and only there."""

import tempfile
import unittest
from pathlib import Path

from uplink.assistant import Assistant
from uplink.store import Store
from uplink.writer import Writer

from .support import Clock, MeshBuilder, ScriptedModel, make_settings


class FollowUpTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        store = Store(Path(self.folder.name))
        store.save_snapshot(MeshBuilder().gateway("gw").node("a", via="gw", clients=3).snapshot())
        self.model = ScriptedModel()
        self.clock = Clock()
        writer = Writer(self.model, language="english", network="Freifunk München")
        self.assistant = Assistant(
            store=store, settings=make_settings(Path(self.folder.name)), writer=writer, clock=self.clock
        )

    def tearDown(self):
        self.folder.cleanup()

    def ask(self, question: str, chat: str = "4242", reply: str = "Nothing is down right now."):
        self.model.replies.append(reply)
        return self.assistant.reply(question, chat=chat)

    def earlier_turns(self, call: int) -> list[tuple[str, str]]:
        """What the model saw between the network state and the question."""
        return [(m["role"], m["content"]) for m in self.model.calls[call][2:-1]]

    def test_a_follow_up_sees_the_last_question_and_answer(self):
        first = self.ask("what is down right now?")
        second = self.ask("can you say that in Telugu?", reply="ప్రస్తుతం ఏదీ డౌన్ లేదు.")
        self.assertEqual(self.earlier_turns(0), [])
        self.assertEqual(self.earlier_turns(1), [("user", "what is down right now?"), ("assistant", first.text)])
        self.assertEqual((second.text, second.source), ("ప్రస్తుతం ఏదీ డౌన్ లేదు.", "muse"))

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
