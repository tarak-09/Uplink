import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from uplink import __version__
from uplink.cli import TEST_MESSAGE, main
from uplink.store import Store

from .support import FIXTURE, FakeFeed, FakeGraph, FakeTwilio, MeshBuilder, twilio_error, whatsapp_error


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.data = Path(self.folder.name, "data")
        self.env = Path(self.folder.name, ".env")
        self.write_env()

    def tearDown(self):
        self.folder.cleanup()

    def write_env(self, **values: str) -> None:
        lines = [f"DATA_DIR={self.data}", "FEED_URL=http://127.0.0.1:9/data/meshviewer.json"]
        lines += [f"{key}={value}" for key, value in values.items()]
        self.env.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def run_cli(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main([args[0], "--env-file", str(self.env), *args[1:]])
        return code, out.getvalue(), err.getvalue()


class AnalyzeTest(CliTestCase):
    def test_explains_the_real_snapshot(self):
        code, out, _ = self.run_cli("analyze", "--file", str(FIXTURE))
        self.assertEqual(code, 0)
        self.assertIn("6 outages involving several nodes, and 57 single nodes", out)
        self.assertIn(
            "1. 22/10 16:15  [cascade] site-bcg5 is down, and the 6 nodes behind it went offline too, all within 15 s",
            out,
        )
        self.assertIn("they used 3 different gateways, so the gateways are not the problem", out)

    def test_json_output(self):
        code, out, _ = self.run_cli("analyze", "--file", str(FIXTURE), "--json")
        facts = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual([f["nodes_offline"] for f in facts], [7, 6, 3, 2, 2, 2])
        self.assertNotIn("people_affected", facts[0], "a single snapshot cannot know who was connected")

    def test_anonymized_output_hides_router_names(self):
        self.write_env(ANONYMIZE="true")
        _, out, _ = self.run_cli("analyze", "--file", str(FIXTURE))
        self.assertNotIn("site-", out)
        self.assertIn("node d477b2", out)

    def test_sending_one_outage_as_a_dry_run(self):
        code, out, _ = self.run_cli("analyze", "--file", str(FIXTURE), "--send", "--dry-run", "--pick", "2")
        self.assertEqual(code, 0)
        self.assertIn("*6 nodes at one location went offline together*", out)
        self.assertIn("Alert written by template", out)

    def test_picking_an_outage_that_does_not_exist(self):
        code, _, err = self.run_cli("analyze", "--file", str(FIXTURE), "--send", "--pick", "99")
        self.assertEqual(code, 1)
        self.assertIn("no outage number 99", err)

    def test_a_missing_file_is_a_clean_error(self):
        code, _, err = self.run_cli("analyze", "--file", "/nonexistent.json")
        self.assertEqual(code, 1)
        self.assertIn("cannot read", err)


class LiveCommandsTest(CliTestCase):
    def test_check_reports_the_network_and_the_setup(self):
        with FakeFeed() as feed:
            feed.document = MeshBuilder().gateway("gw").node("a", via="gw", clients=4).node("b", via="a").document()
            self.write_env(FEED_URL=feed.feed_url)
            code, out, _ = self.run_cli("check")
        self.assertEqual(code, 0)
        self.assertIn("3 nodes, 3 reachable, 4 people connected, 1 gateway segments", out)
        self.assertIn("Key nodes  a.home carries 1 node and 0 people", out)
        self.assertIn("not set up: add MODEL_API_KEY", out)
        self.assertIn("missing WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_TO", out)

    def test_watch_once_keeps_state_between_runs(self):
        with FakeFeed() as feed:
            mesh = MeshBuilder().gateway("gw").node("a", via="gw")
            feed.document = mesh.document()
            self.write_env(FEED_URL=feed.feed_url)
            self.assertEqual(
                self.run_cli("watch", "--once", "--dry-run")[1].strip(), "baseline: 0 open incidents, 0 messages sent"
            )
            feed.document = mesh.tick().document()
            self.assertEqual(
                self.run_cli("watch", "--once", "--dry-run")[1].strip(), "ok: 0 open incidents, 0 messages sent"
            )
        self.assertTrue((self.data / "state.json").exists())

    def test_a_feed_that_is_down_fails_cleanly(self):
        with FakeFeed() as feed:
            feed.fail_with = 404
            self.write_env(FEED_URL=feed.feed_url)
            code, out, _ = self.run_cli("watch", "--once", "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("failed: 0 open incidents, 0 messages sent (GET", out)
        self.assertIn("HTTP 404", out)

    def test_replay_runs_recordings_through_uplink(self):
        store = Store(Path(self.folder.name, "recorded"))
        mesh = MeshBuilder().gateway("gw").node("a", via="gw", clients=2).node("b", via="a")
        store.record(mesh.snapshot())
        store.record(mesh.tick().drop("a", "b", ago=40).snapshot())
        store.record(mesh.tick().snapshot())
        store.record(mesh.tick().restore("a", "b").snapshot())
        code, out, _ = self.run_cli("replay", str(store.recordings_dir))
        self.assertEqual(code, 0)
        self.assertIn("Replayed 4 snapshots.", out)
        self.assertIn("*a.home is down, and the node behind it went offline too*", out)
        self.assertIn("*Back online: a.home*", out)
        self.assertIn("1 sent (+1 recoveries)", out)
        self.assertFalse(self.data.exists(), "a replay leaves the real data folder alone")


class WhatsAppCommandsTest(CliTestCase):
    def setUp(self):
        super().setUp()
        self.graph = FakeGraph().__enter__()
        self.write_env(
            WHATSAPP_TOKEN="test-token",
            WHATSAPP_PHONE_NUMBER_ID="1000001",
            WHATSAPP_TO="+49 151 12345678",
            GRAPH_BASE_URL=self.graph.url,
        )

    def tearDown(self):
        self.graph.__exit__()
        super().tearDown()

    def test_send_test_delivers_a_text(self):
        code, out, _ = self.run_cli("send-test")
        self.assertEqual(code, 0)
        self.assertIn("WhatsApp accepted the message (id wamid.0)", out)
        self.assertEqual(self.graph.texts(), [TEST_MESSAGE])
        self.assertEqual(self.graph.sent()[0]["to"], "4915112345678")

    def test_send_test_with_the_hello_world_template(self):
        self.run_cli("send-test", "--template")
        self.assertEqual(self.graph.sent()[0]["template"]["name"], "hello_world")

    def test_send_test_explains_refusals(self):
        self.graph.fail_with = whatsapp_error(131030, "Recipient phone number not in allowed list")
        code, out, _ = self.run_cli("send-test")
        self.assertEqual(code, 1)
        self.assertIn("What to do: Your number is not on the test number's recipient list", out)

    def test_serve_refuses_to_start_without_webhook_secrets(self):
        code, _, err = self.run_cli("serve")
        self.assertEqual(code, 2)
        self.assertIn("WHATSAPP_APP_SECRET, WHATSAPP_VERIFY_TOKEN", err)


class TwilioCommandsTest(CliTestCase):
    def setUp(self):
        super().setUp()
        self.twilio = FakeTwilio().__enter__()
        self.write_env(
            WHATSAPP_PROVIDER="twilio",
            TWILIO_ACCOUNT_SID=FakeTwilio.account_sid,
            TWILIO_AUTH_TOKEN=FakeTwilio.auth_token,
            TWILIO_BASE_URL=self.twilio.url,
            WHATSAPP_TO="+49 151 12345678",
        )

    def tearDown(self):
        self.twilio.__exit__()
        super().tearDown()

    def test_send_test_goes_through_the_sandbox(self):
        code, out, _ = self.run_cli("send-test")
        self.assertEqual(code, 0)
        self.assertEqual(self.twilio.bodies(), [TEST_MESSAGE])
        self.assertIn("join code to +1 415 523 8886", out)

    def test_the_sandbox_has_no_templates(self):
        code, out, _ = self.run_cli("send-test", "--template")
        self.assertEqual(code, 1)
        self.assertIn("sandbox cannot send your own templates", out)
        self.assertEqual(self.twilio.sent(), [])

    def test_send_test_explains_a_number_that_has_not_joined(self):
        self.twilio.fail_with = twilio_error(
            63015, "Channel Sandbox can only send messages to phone numbers that have joined the Sandbox"
        )
        code, out, _ = self.run_cli("send-test")
        self.assertEqual(code, 1)
        self.assertIn("What to do: Your number has not joined the sandbox", out)

    def test_serve_needs_only_the_twilio_credentials(self):
        self.write_env(WHATSAPP_PROVIDER="twilio", TWILIO_ACCOUNT_SID="AC1", WHATSAPP_TO="4915112345678")
        code, _, err = self.run_cli("serve")
        self.assertEqual(code, 2)
        self.assertIn("the webhook needs TWILIO_AUTH_TOKEN", err)


class OtherCommandsTest(CliTestCase):
    def test_ask_answers_from_the_saved_state(self):
        Store(self.data).save_snapshot(MeshBuilder().gateway("gw").node("a", via="gw", clients=2).snapshot())
        code, out, _ = self.run_cli("ask", "status")
        self.assertEqual(code, 0)
        self.assertIn("Nodes online: 2 of 2", out)

    def test_ask_needs_a_question(self):
        code, _, err = self.run_cli("ask")
        self.assertEqual(code, 2)
        self.assertIn("ask a question", err)

    def test_report(self):
        Store(self.data).log("poll", status="ok", taken_at="2026-11-02T12:00:00+01:00", nodes=10, reachable=9)
        code, out, _ = self.run_cli("report")
        self.assertEqual(code, 0)
        self.assertIn("Network now       9 of 10 nodes reachable", out)
        self.assertEqual(json.loads(self.run_cli("report", "--json")[1])["polls"], {"ok": 1})

    def test_configuration_mistakes_exit_with_2(self):
        self.write_env(POLL_INTERVAL_SECONDS="10", FEED_URL="https://map.example.org/data/meshviewer.json")
        code, _, err = self.run_cli("check")
        self.assertEqual(code, 2)
        self.assertIn("POLL_INTERVAL_SECONDS must be at least 60", err)

    def test_version(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as caught:
            main(["--version"])
        self.assertEqual(caught.exception.code, 0)
        self.assertEqual(out.getvalue().strip(), f"uplink {__version__}")


if __name__ == "__main__":
    unittest.main()
