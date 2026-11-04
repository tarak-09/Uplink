import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from uplink.config import load_settings
from uplink.correlate import Situation, correlate
from uplink.facts import (
    Namer,
    clock,
    find_nodes,
    network_facts,
    outage_facts,
    resolved_facts,
    stale_feed_facts,
)
from uplink.incidents import Incident
from uplink.templates import render, render_status
from uplink.writer import check_draft

from .support import MUNICH, MeshBuilder, make_settings

STALE = timedelta(seconds=180)
SETTINGS = make_settings(Path("."))


def incidents_in(mesh: MeshBuilder) -> tuple[list[Incident], object]:
    snapshot = mesh.snapshot()
    down = snapshot.down_since(STALE)
    found = correlate(down, Situation(snapshot.nodes, down, snapshot.links))
    return [Incident.from_outage(o, snapshot.nodes, detected_at=snapshot.taken_at) for o in found], snapshot


class OutageFactsTest(unittest.TestCase):
    def test_a_cascade_behind_a_mesh_neighbour(self):
        mesh = (
            MeshBuilder()
            .gateway("gw")
            .node("hub", via="gw")
            .node("a", via="hub")
            .node("b", via="a")
            .node("c", via="a")
            .tick()
        )
        (incident,), snapshot = incidents_in(mesh.drop("a", ago=40).drop("b", "c", ago=30))
        facts = outage_facts(incident, snapshot, SETTINGS)
        self.assertEqual(facts["kind"], "cascade")
        self.assertEqual(
            (facts["nodes_offline"], facts["nodes_behind_root"], facts["failed_within_seconds"]), (3, 2, 10)
        )
        self.assertEqual(facts["still_online_above"], "hub.home")
        self.assertEqual(facts["check_first"], "power and cabling at a.home, then its mesh link to hub.home")
        self.assertIn("the other 2 reach the internet only through a.home", facts["evidence"])
        self.assertNotIn("people_affected", facts, "unknown from a single snapshot")

    def test_a_node_with_its_own_internet_line(self):
        mesh = MeshBuilder().gateway("gw", "gw05.ext.ffmuc.net").node("a", via="gw").node("b", via="a").tick()
        (incident,), snapshot = incidents_in(mesh.drop("a", "b", ago=30))
        facts = outage_facts(incident, snapshot, SETTINGS)
        self.assertEqual(facts["internet_via_gateway"], "gw05.ext.ffmuc.net")
        self.assertEqual(facts["check_first"], "power and cabling at a.home, then its internet line")
        self.assertIn("the other node reaches the internet only through a.home", facts["evidence"])

    def test_a_site_across_gateways(self):
        mesh = (
            MeshBuilder()
            .gateway("g1")
            .gateway("g2")
            .node("p", via="g1", name="Kiosk1")
            .node("q", via="g2", name="Kiosk2")
            .tick()
        )
        (incident,), snapshot = incidents_in(mesh.drop("p", "q", ago=30))
        facts = outage_facts(incident, snapshot, SETTINGS)
        self.assertEqual(facts["kind"], "site")
        self.assertIn("their names form one set (kiosk…)", facts["evidence"])
        self.assertIn("they used 2 different gateways, so the gateways are not the problem", facts["evidence"])

    def test_anonymized_facts_hide_names_and_families(self):
        mesh = (
            MeshBuilder()
            .gateway("g1", "gw01.example.net")
            .node("p1x", via="g1", name="Kiosk1")
            .node("p2x", via="g1", name="Kiosk2")
            .tick()
        )
        (incident,), snapshot = incidents_in(mesh.drop("p1x", "p2x", ago=30))
        facts = outage_facts(incident, snapshot, make_settings(Path("."), anonymize=True))
        text = str(facts)
        self.assertNotIn("Kiosk", text)
        self.assertNotIn("kiosk", text)
        self.assertIn("node p1x", text)
        self.assertIn("gw01.example.net", text, "gateways are public infrastructure")


class HelpersTest(unittest.TestCase):
    def test_clock_shows_the_date_only_when_needed(self):
        reference = datetime(2026, 11, 2, 9, 0, tzinfo=MUNICH)
        self.assertEqual(clock(datetime(2026, 11, 2, 7, 5, tzinfo=MUNICH), reference), "07:05")
        self.assertEqual(clock(datetime(2026, 11, 1, 23, 30, tzinfo=MUNICH), reference), "01/11 23:30")
        utc = datetime(2026, 11, 2, 6, 5, tzinfo=MUNICH.utc)
        self.assertEqual(clock(utc, reference), "07:05", "shown in the network's own time zone")

    def test_clock_names_the_time_zone_when_given_a_label(self):
        reference = datetime(2026, 11, 2, 9, 0, tzinfo=MUNICH)
        self.assertEqual(
            clock(datetime(2026, 11, 2, 7, 5, tzinfo=MUNICH), reference, "Munich time"), "07:05 Munich time"
        )
        self.assertEqual(
            clock(datetime(2026, 11, 1, 23, 30, tzinfo=MUNICH), reference, "Munich time"), "01/11 23:30 Munich time"
        )

    def test_namer_falls_back_to_remembered_names(self):
        name = Namer({}, remembered={"abc123456": "Daniel5"})
        self.assertEqual(name("abc123456"), "Daniel5")
        self.assertEqual(name("ffff00112233"), "node 112233")

    def test_find_nodes_by_numbered_part_full_name_or_id(self):
        snapshot = (
            MeshBuilder()
            .gateway("gw")
            .node("a1b2c3d4e5f6", via="gw", name="ffNoe-Stadt-Daniel5")
            .node("b", via="gw", name="Rathaus")
            .node("c", via="gw", name="ffNoe-Stadt-St.Georg8")
            .snapshot()
        )
        self.assertEqual([n.name for n in find_nodes("is Daniel5 up?", snapshot)], ["ffNoe-Stadt-Daniel5"])
        self.assertEqual([n.name for n in find_nodes("what about the rathaus", snapshot)], ["Rathaus"])
        self.assertEqual([n.name for n in find_nodes("status of a1b2c3d4e5f6", snapshot)], ["ffNoe-Stadt-Daniel5"])
        self.assertEqual(find_nodes("is ffnoe stadt down?", snapshot), [], "common words match nothing")


class TimeZoneLabelTest(unittest.TestCase):
    """Every clock time a message can show names its time zone, so viewers elsewhere are not misled."""

    def cascade(self):
        mesh = MeshBuilder().gateway("gw").node("a", via="gw", clients=3).node("b", via="a").tick()
        return incidents_in(mesh.drop("a", ago=40).drop("b", ago=30))

    def test_outage_facts_and_the_alert_template(self):
        (incident,), snapshot = self.cascade()
        facts = outage_facts(incident, snapshot, SETTINGS)
        self.assertEqual(facts["started"], "12:00 Munich time")
        alert = render(facts, "english")
        self.assertIn("Since 12:00 Munich time · Freifunk München · nodes offline: 2", alert)
        self.assertIn("12:00 Munich time నుండి", render(facts, "telugu"))
        self.assertEqual(check_draft(alert, facts, 700), "", "the label adds no numbers the check would refuse")

    def test_answers_and_the_status_lines(self):
        incidents, snapshot = self.cascade()
        facts = network_facts(snapshot, incidents, [], SETTINGS, question="is b up?")
        self.assertEqual(facts["as_of"], "12:01 Munich time")
        self.assertEqual(facts["open_outages"][0]["started"], "12:00 Munich time")
        self.assertRegex(facts["nodes_asked_about"][0]["last_heard"], r"^\d\d:\d\d Munich time$")
        self.assertIn("• since 12:00 Munich time: a.home (nodes offline: 2)", render_status(facts, "english"))

    def test_recoveries_feed_problems_and_evidence(self):
        mesh = MeshBuilder().gateway("gw").gateway("gw2")
        booted = mesh.now - timedelta(days=2)
        mesh.node("p", via="gw", booted=booted).node("q", via="gw2", booted=booted + timedelta(seconds=25)).tick()
        (incident,), snapshot = incidents_in(mesh.drop("p", "q", ago=30))
        evidence = " ".join(outage_facts(incident, snapshot, SETTINGS)["evidence"])
        self.assertRegex(evidence, r"within the same minute \(\d\d/\d\d \d\d:\d\d Munich time\)")
        stale = stale_feed_facts(snapshot.taken_at, snapshot.taken_at + timedelta(minutes=20), SETTINGS)
        self.assertEqual(stale["since"], "12:01 Munich time")
        resolved = resolved_facts(incident, snapshot, SETTINGS)
        self.assertEqual((resolved["started"], resolved["ended"]), ("12:00 Munich time", "12:01 Munich time"))

    def test_the_label_is_a_setting(self):
        (incident,), snapshot = self.cascade()
        facts = outage_facts(incident, snapshot, make_settings(Path("."), time_zone_label="CET"))
        self.assertEqual(facts["started"], "12:00 CET")

    def test_an_empty_label_turns_it_off(self):
        self.assertEqual(load_settings({}, None).time_zone_label, "Munich time", "left out: the default stays")
        with tempfile.TemporaryDirectory() as folder:
            env_file = Path(folder, ".env")
            env_file.write_text("TIME_ZONE_LABEL=      # no time zone name\n", encoding="utf-8")
            from_file = load_settings({}, env_file)
        off = {"the environment": load_settings({"TIME_ZONE_LABEL": " "}, None), "a .env file": from_file}
        for source, settings in off.items():
            with self.subTest(source=source):
                self.assertEqual(settings.time_zone_label, "")
                (incident,), snapshot = self.cascade()
                facts = outage_facts(incident, snapshot, settings)
                self.assertEqual(facts["started"], "12:00")
                network = network_facts(snapshot, [incident], [], settings, question="is b up?")
                self.assertEqual((network["as_of"], network["open_outages"][0]["started"]), ("12:01", "12:00"))
                self.assertRegex(network["nodes_asked_about"][0]["last_heard"], r"^\d\d:\d\d$")
                resolved = resolved_facts(incident, snapshot, settings)
                self.assertEqual((resolved["started"], resolved["ended"]), ("12:00", "12:01"))
                alert = render(facts, "english")
                self.assertIn("Since 12:00 · Freifunk München · nodes offline: 2", alert)
                self.assertEqual(check_draft(alert, facts, 700), "")


class NetworkFactsTest(unittest.TestCase):
    def test_lone_nodes_are_counted_not_listed(self):
        mesh = (
            MeshBuilder()
            .gateway("gw")
            .node("a", via="gw", clients=3)
            .node("b", via="a")
            .node("x", via="gw")
            .node("y", via="gw", clients=2)
            .tick()
            .drop("a", "b", ago=30)
            .drop("x", ago=900)
        )
        incidents, snapshot = incidents_in(mesh)
        facts = network_facts(snapshot, incidents, [], SETTINGS, question="is y up?")
        self.assertEqual((facts["nodes_online"], facts["nodes_total"], facts["people_online"]), (2, 5, 2))
        self.assertEqual([o["root"] for o in facts["open_outages"]], ["a.home"])
        self.assertEqual(facts["lone_nodes_offline"], 1)
        asked = facts["nodes_asked_about"][0]
        self.assertEqual(
            (asked["name"], asked["online"], asked["people_connected"], asked["path_to_internet"]),
            ("y.home", True, 2, ["gw-gw"]),
        )
        summary = render_status(facts, "english")
        self.assertIn("Single nodes offline recently: 1", summary)
        self.assertIn("• since", summary)


if __name__ == "__main__":
    unittest.main()
