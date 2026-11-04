"""The watch loop end to end: a fake map, Muse Spark and WhatsApp on real HTTP, a real data folder."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

from uplink.app import make_monitor
from uplink.report import build_report
from uplink.store import Store
from uplink.transport import Transport

from .support import (
    Clock,
    FakeFeed,
    FakeGraph,
    FakeMeta,
    FakeTelegram,
    FakeTwilio,
    MeshBuilder,
    json_reply,
    make_settings,
)


def faithful_reply(request) -> str:
    """What a well-behaved model writes: only facts from the sheet."""
    facts = json.loads(request.json()["messages"][-1]["content"])
    if facts["event"] == "outage":
        return f"*{facts['root']} is down*\n{facts['nodes_offline']} nodes offline since {facts['started']}."
    return f"*Back online: {facts['root']}*\nIt lasted {facts['duration']}."


class MonitorTestCase(unittest.TestCase):
    settings_overrides: ClassVar[dict[str, str]] = {}

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.feed = FakeFeed().__enter__()
        self.meta = FakeMeta().__enter__()
        self.graph = FakeGraph().__enter__()
        self.twilio = FakeTwilio().__enter__()
        self.telegram = FakeTelegram().__enter__()
        self.meta.default_reply = faithful_reply
        self.clock = Clock()
        self.mesh = (
            MeshBuilder()
            .gateway("gw")
            .node("a", via="gw", clients=5)
            .node("b", via="a", clients=3)
            .node("c", via="b", clients=2)
            .node("d", via="gw", clients=1)
        )
        self.settings = make_settings(
            Path(self.folder.name),
            feed_url=self.feed.feed_url,
            model_api_key="key-123",
            model_base_url=self.meta.base_url,
            graph_base_url=self.graph.url,
            twilio_base_url=self.twilio.url,
            telegram_base_url=self.telegram.url,
            record_snapshots=True,
            **self.settings_overrides,
        )
        self.store = Store(Path(self.folder.name))
        self.monitor = self.new_monitor()

    def tearDown(self):
        for fake in (self.feed, self.meta, self.graph, self.twilio, self.telegram):
            fake.__exit__()
        self.folder.cleanup()

    def new_monitor(self):
        transport = Transport(sleep=lambda s: None)
        return make_monitor(self.settings, store=self.store, transport=transport, clock=self.clock)

    def poll(self, seconds: int = 60):
        """Let a minute pass on the map and in the wall clock, then poll."""
        self.feed.document = self.mesh.document()
        result = self.monitor.poll()
        self.mesh.tick(seconds)
        self.clock.advance(seconds=seconds)
        return result

    def events(self, name: str) -> list[dict]:
        return [e for e in self.store.events() if e["event"] == name]


class StoryTest(MonitorTestCase):
    def test_a_cascade_from_first_poll_to_recovery(self):
        self.assertEqual(self.poll().status, "baseline")
        self.assertEqual(self.poll().status, "ok")
        self.mesh.drop("a", ago=40).drop("b", ago=35).drop("c", ago=30)
        self.assertEqual(self.poll().sent, 0, "confirmed one poll later")
        alert = self.poll()
        self.assertEqual((alert.status, alert.sent, alert.open_incidents), ("ok", 1, 1))
        self.assertEqual(self.graph.texts(), ["*a.home is down*\n3 nodes offline since 12:01 Munich time."])
        self.mesh.restore("a", "b", "c")
        recovery = self.poll()
        self.assertEqual((recovery.sent, recovery.open_incidents), (1, 0))
        self.assertEqual(self.graph.texts()[1], "*Back online: a.home*\nIt lasted 3 min.")

        facts = json.loads(self.meta.chats()[0].json()["messages"][-1]["content"])
        self.assertEqual((facts["kind"], facts["people_affected"], facts["nodes_behind_root"]), ("cascade", 10, 2))
        sent = self.events("alert_sent")
        self.assertEqual([(e["notice"], e["source"]) for e in sent], [("alert", "muse"), ("resolved", "muse")])
        report = build_report(self.store.events())
        self.assertEqual(
            (report["incidents"]["total"], report["alerts"]["sent"], report["alerts"]["recoveries_sent"]), (1, 1, 1)
        )
        self.assertEqual(len(list(Path(self.folder.name, "recordings").rglob("*.json.gz"))), 5)

    def test_a_restart_picks_up_where_it_left_off(self):
        self.poll()
        self.mesh.drop("a", "b", ago=40)
        self.poll()
        self.monitor = self.new_monitor()
        self.assertEqual(self.poll().sent, 1)
        self.mesh.restore("a", "b")
        self.assertEqual(self.poll().sent, 1)
        self.assertEqual(len(self.graph.texts()), 2)

    def test_each_message_sent_is_kept_for_replies(self):
        self.poll()
        self.mesh.drop("a", "b", ago=40)
        self.poll()
        self.assertIsNone(self.store.load_last_alert(), "nothing sent yet")
        self.poll()
        alert = self.store.load_last_alert()
        self.assertEqual((alert["event"], alert["root"], alert["nodes_offline"]), ("outage", "a.home", 2))
        self.mesh.restore("a", "b")
        self.poll()
        self.assertEqual(self.store.load_last_alert()["event"], "resolved", "the back-online notice replaces it")

    def test_a_dry_run_keeps_nothing_for_replies(self):
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            transport = Transport(sleep=lambda s: None)
            self.monitor = make_monitor(
                self.settings, store=self.store, transport=transport, clock=self.clock, dry_run=True
            )
            self.poll()
            self.mesh.drop("a", "b", ago=40)
            self.poll()
            self.poll()
        self.assertIn("*a.home is down*", printed.getvalue())
        self.assertIsNone(self.store.load_last_alert())

    def test_an_unchanged_map_is_not_reprocessed(self):
        self.feed.document = self.mesh.document()
        self.monitor.poll()
        self.assertEqual(self.monitor.poll().status, "unchanged")

    def test_a_long_gap_starts_over_instead_of_reporting_old_news(self):
        self.poll()
        self.mesh.tick(20 * 60).drop("a", "b", ago=15 * 60)
        self.assertEqual(self.poll().status, "gap")
        self.poll()
        self.assertEqual(self.monitor.tracker.open, {})
        self.assertEqual(self.graph.sent(), [])


class FallbackTest(MonitorTestCase):
    def test_the_template_goes_out_when_the_model_fails(self):
        self.meta.default_reply = lambda request: "x"
        self.meta.replies = [json_reply({"error": {"message": "quota exceeded"}}, 400)]
        self.poll()
        self.mesh.drop("a", "b", ago=40)
        self.poll()
        self.poll()
        self.assertTrue(self.graph.texts()[0].startswith("*a.home is down, and the node behind it went offline too*"))
        event = self.events("alert_sent")[0]
        self.assertEqual(event["source"], "template")
        self.assertIn("quota exceeded", event["note"])

    def test_a_refused_message_is_retried_on_the_next_poll(self):
        self.graph.fail_with = json_reply({"error": {"message": "bad", "code": 131030}}, 400)
        self.poll()
        self.mesh.drop("a", "b", ago=40)
        self.poll()
        self.assertEqual(self.poll().sent, 0)
        self.assertIn("recipient list", self.events("alert_failed")[0]["error"])
        self.assertIsNone(self.store.load_last_alert(), "a refused message is not kept for replies")
        self.graph.fail_with = None
        self.assertEqual(self.poll().sent, 1)
        self.assertEqual(self.store.load_last_alert()["event"], "outage")

    def test_a_closed_service_window_is_flagged(self):
        self.poll()
        self.mesh.drop("a", "b", ago=40)
        self.poll()
        with self.assertLogs("uplink.monitor", "WARNING") as logs:
            self.poll()
        self.assertIn("24 hours", logs.output[0])


class TemplateModeTest(MonitorTestCase):
    settings_overrides: ClassVar[dict[str, str]] = {"whatsapp_alert_template": "uplink_alert"}

    def test_alerts_go_out_as_an_approved_template(self):
        self.poll()
        self.mesh.drop("a", "b", ago=40)
        self.poll()
        self.poll()
        template = self.graph.sent()[0]["template"]
        self.assertEqual(template["name"], "uplink_alert")
        self.assertEqual(
            template["components"][0]["parameters"][0]["text"],
            "*a.home is down* · 2 nodes offline since 12:00 Munich time.",
        )


class TwilioModeTest(MonitorTestCase):
    settings_overrides: ClassVar[dict[str, str]] = {
        "whatsapp_provider": "twilio",
        "twilio_account_sid": FakeTwilio.account_sid,
        "twilio_auth_token": FakeTwilio.auth_token,
        "whatsapp_alert_template": "ignored_by_the_sandbox",
    }

    def test_alerts_and_recoveries_go_through_the_sandbox(self):
        self.poll()
        self.mesh.drop("a", "b", ago=40)
        self.poll()
        self.assertEqual(self.poll().sent, 1)
        self.mesh.restore("a", "b")
        self.assertEqual(self.poll().sent, 1)
        self.assertEqual(
            self.twilio.bodies(),
            ["*a.home is down*\n2 nodes offline since 12:00 Munich time.", "*Back online: a.home*\nIt lasted 3 min."],
        )
        self.assertEqual({m["To"] for m in self.twilio.sent()}, {"whatsapp:+4915112345678"})
        self.assertEqual(self.graph.sent(), [])


class TelegramModeTest(MonitorTestCase):
    settings_overrides: ClassVar[dict[str, str]] = {
        "whatsapp_provider": "telegram",
        "telegram_bot_token": FakeTelegram.token,
        "telegram_chat_id": str(FakeTelegram.chat_id),
        "whatsapp_alert_template": "ignored_by_telegram",
    }

    def test_alerts_and_recoveries_reach_the_chat(self):
        self.poll()
        self.mesh.drop("a", "b", ago=40)
        self.poll()
        with self.assertNoLogs("uplink.monitor", "WARNING"):  # Telegram has no 24-hour window to warn about
            self.assertEqual(self.poll().sent, 1)
        self.mesh.restore("a", "b")
        self.assertEqual(self.poll().sent, 1)
        self.assertEqual(
            self.telegram.texts(),
            [
                "<b>a.home is down</b>\n2 nodes offline since 12:00 Munich time.",
                "<b>Back online: a.home</b>\nIt lasted 3 min.",
            ],
        )
        self.assertEqual({m["chat_id"] for m in self.telegram.sent()}, {FakeTelegram.chat_id})
        self.assertEqual([e["ref"] for e in self.events("alert_sent")], ["1", "2"])
        self.assertEqual((self.graph.sent(), self.twilio.sent()), ([], []))

    def test_a_refused_alert_is_retried_on_the_next_poll(self):
        self.telegram.chats = set()  # nobody has pressed Start yet
        self.poll()
        self.mesh.drop("a", "b", ago=40)
        self.poll()
        self.assertEqual(self.poll().sent, 0)
        self.assertIn("telegram-chat-id", self.events("alert_failed")[0]["error"])
        self.telegram.chats = {FakeTelegram.chat_id}
        self.assertEqual(self.poll().sent, 1)


class FeedHealthTest(MonitorTestCase):
    def test_a_silent_map_is_reported_once_and_its_recovery_noted(self):
        self.poll()
        self.feed.fail_with = 503
        self.assertEqual(self.poll().status, "failed")
        self.clock.advance(minutes=16)
        self.poll()
        self.poll()
        self.assertEqual(len(self.graph.texts()), 1)
        self.assertIn("Uplink can't see the network", self.graph.texts()[0])
        self.assertRegex(self.graph.texts()[0], r"has not updated since \d\d:\d\d Munich time\.")
        self.feed.fail_with = None
        self.assertEqual(self.poll().status, "ok")
        self.assertEqual(len(self.events("feed_recovered")), 1)
        self.assertGreaterEqual(len(self.events("poll_failed")), 3)


class GlitchTest(MonitorTestCase):
    def setUp(self):
        super().setUp()
        for i in range(100):
            self.mesh.node(f"n{i:03d}x", via="gw")

    def test_half_the_map_vanishing_at_once_is_held_back(self):
        self.poll()
        vanished = [f"n{i:03d}x" for i in range(60)]
        self.mesh.drop(*vanished, ago=30)
        self.assertEqual(self.poll().status, "glitch")
        self.mesh.restore(*vanished)
        self.assertEqual(self.poll().status, "ok")
        self.assertEqual(self.monitor.tracker.open, {})
        self.assertEqual(self.graph.sent(), [])

    def test_a_lasting_mass_outage_is_reported_once(self):
        self.poll()
        self.mesh.drop(*[f"n{i:03d}x" for i in range(60)], ago=30)
        statuses = [self.poll().status for _ in range(3)]
        self.assertEqual(statuses, ["glitch", "glitch", "glitch"])
        self.assertEqual(len(self.graph.texts()), 1)
        self.assertIn("60 nodes went offline at once", self.graph.texts()[0])
        self.assertRegex(self.graph.texts()[0], r"Since \d\d:\d\d Munich time\.$")
        self.assertEqual(self.poll().status, "ok")
        self.assertEqual(self.monitor.tracker.open, {}, "the outage is not reported again node by node")


if __name__ == "__main__":
    unittest.main()
