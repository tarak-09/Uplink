import unittest
from datetime import datetime, timedelta
from pathlib import Path

from uplink.correlate import Situation, correlate
from uplink.facts import Namer, clock, find_nodes, network_facts, outage_facts
from uplink.incidents import Incident
from uplink.templates import render_status

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
