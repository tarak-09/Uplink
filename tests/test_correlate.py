import random
import unittest
from datetime import timedelta

from uplink.correlate import DEFAULT_WINDOWS, Situation, Windows, correlate, name_family

from .support import MeshBuilder

STALE = timedelta(seconds=180)


def outages(mesh: MeshBuilder, windows: Windows = DEFAULT_WINDOWS):
    snapshot = mesh.snapshot()
    down = snapshot.down_since(STALE)
    return correlate(down, Situation(snapshot.nodes, down, snapshot.links), windows)


def kinds(found):
    return sorted((o.kind, o.members) for o in found)


def chain() -> MeshBuilder:
    """gw <- a <- b <- c, plus an unrelated d."""
    return (
        MeshBuilder().gateway("gw").node("a", via="gw").node("b", via="a").node("c", via="b").node("d", via="gw").tick()
    )


class CascadeTest(unittest.TestCase):
    def test_nodes_behind_a_failure_join_it(self):
        found = outages(chain().drop("a", ago=40).drop("b", ago=30).drop("c", ago=25))
        self.assertEqual(len(found), 1)
        cascade = found[0]
        self.assertEqual(
            (cascade.kind, cascade.members, cascade.roots, cascade.anchor), ("cascade", ("a", "b", "c"), ("a",), "gw")
        )
        self.assertEqual(cascade.spread, timedelta(seconds=15))
        self.assertEqual({(e.kind, e.a, e.b) for e in cascade.evidence}, {("behind", "b", "a"), ("behind", "c", "b")})

    def test_a_dependent_reported_slightly_earlier_still_counts(self):
        found = outages(chain().drop("b", ago=45).drop("a", ago=30))
        self.assertEqual(kinds(found), [("cascade", ("b", "a"))])
        self.assertEqual(found[0].roots, ("a",))

    def test_an_upstream_that_failed_much_later_was_up_at_the_time(self):
        found = outages(chain().drop("c", ago=600).drop("b", ago=30))
        self.assertEqual(kinds(found), [("single", ("b",)), ("single", ("c",))])
        by_root = {o.roots[0]: o for o in found}
        self.assertEqual(by_root["c"].anchor, "b")
        self.assertEqual(by_root["b"].anchor, "a")

    def test_a_path_through_a_node_dead_for_hours_is_stale_data(self):
        found = outages(chain().drop("b", ago=7200).drop("c", ago=30))
        self.assertEqual(kinds(found), [("single", ("b",)), ("single", ("c",))])
        self.assertIsNone(next(o for o in found if o.members == ("c",)).anchor)

    def test_routing_loops_do_not_hang(self):
        mesh = chain().reroute("a", "c").drop("a", ago=40).drop("b", ago=35).drop("c", ago=30)
        found = outages(mesh)
        self.assertEqual(sum(o.size for o in found), 3)


class SharedNeighbourTest(unittest.TestCase):
    def mesh(self) -> MeshBuilder:
        return (
            MeshBuilder()
            .gateway("gw")
            .node("hub", via="gw")
            .node("x", via="hub")
            .node("y", via="hub")
            .node("z", via="hub")
            .tick()
        )

    def test_siblings_dropping_together_under_a_live_neighbour(self):
        found = outages(self.mesh().drop("x", ago=40).drop("y", ago=35))
        self.assertEqual(kinds(found), [("shared_neighbor", ("x", "y"))])
        self.assertEqual((found[0].anchor, found[0].roots), ("hub", ("x", "y")))

    def test_siblings_dropping_far_apart_are_separate(self):
        found = outages(self.mesh().drop("x", ago=900).drop("y", ago=35))
        self.assertEqual(kinds(found), [("single", ("x",)), ("single", ("y",))])

    def test_sharing_a_gateway_is_not_evidence(self):
        mesh = (
            MeshBuilder()
            .gateway("gw")
            .node("p", via="gw")
            .node("q", via="gw")
            .tick()
            .drop("p", ago=40)
            .drop("q", ago=35)
        )
        self.assertEqual(kinds(outages(mesh)), [("single", ("p",)), ("single", ("q",))])


class SiteTest(unittest.TestCase):
    def vpn_pair(self, **names) -> MeshBuilder:
        return (
            MeshBuilder()
            .gateway("gw")
            .gateway("gw2")
            .node("p", via="gw", **names.get("p", {}))
            .node("q", via="gw2", **names.get("q", {}))
            .tick()
        )

    def test_a_named_set_dropping_together(self):
        mesh = self.vpn_pair(p={"name": "St.Georg3"}, q={"name": "st.georg5"}).drop("p", ago=40).drop("q", ago=33)
        found = outages(mesh)
        self.assertEqual(kinds(found), [("site", ("p", "q"))])
        self.assertEqual([(e.kind, e.detail) for e in found[0].evidence], [("name_family", "st.georg")])

    def test_a_named_set_needs_the_failures_to_line_up(self):
        mesh = self.vpn_pair(p={"name": "St.Georg3"}, q={"name": "st.georg5"}).drop("p", ago=600).drop("q", ago=30)
        self.assertEqual(kinds(outages(mesh)), [("single", ("p",)), ("single", ("q",))])

    def test_nodes_that_booted_together_share_power(self):
        mesh = MeshBuilder().gateway("gw").gateway("gw2")
        booted = mesh.now - timedelta(days=2)
        mesh.node("p", via="gw", booted=booted).node("q", via="gw2", booted=booted + timedelta(seconds=25)).tick()
        found = outages(mesh.drop("p", ago=40).drop("q", ago=30))
        self.assertEqual(kinds(found), [("site", ("p", "q"))])
        self.assertEqual(found[0].evidence[0].kind, "co_booted")

    def test_boot_times_minutes_apart_are_not_evidence(self):
        mesh = MeshBuilder().gateway("gw").gateway("gw2")
        booted = mesh.now - timedelta(days=2)
        mesh.node("p", via="gw", booted=booted).node("q", via="gw2", booted=booted + timedelta(minutes=5)).tick()
        self.assertEqual(
            kinds(outages(mesh.drop("p", ago=40).drop("q", ago=30))), [("single", ("p",)), ("single", ("q",))]
        )

    def test_directly_linked_nodes(self):
        mesh = self.vpn_pair().link("p", "q", kind="other").drop("p", ago=40).drop("q", ago=30)
        found = outages(mesh)
        self.assertEqual(kinds(found), [("site", ("p", "q"))])
        self.assertEqual((found[0].evidence[0].kind, found[0].evidence[0].detail), ("mesh_link", "other"))

    def test_vpn_links_are_not_evidence(self):
        mesh = self.vpn_pair().link("p", "q", kind="vpn").drop("p", ago=40).drop("q", ago=30)
        self.assertEqual(len(outages(mesh)), 2)


class GatewayTest(unittest.TestCase):
    def test_a_gateway_and_what_failed_behind_it(self):
        mesh = MeshBuilder().gateway("gw").gateway("g2").node("r", via="g2").node("s", via="gw").tick()
        found = outages(mesh.drop("g2", ago=40).drop("r", ago=30))
        self.assertEqual(kinds(found), [("gateway", ("g2", "r"))])
        self.assertEqual(found[0].roots, ("g2",))

    def test_segments_of_one_gateway_server_group_by_name(self):
        mesh = (
            MeshBuilder()
            .gateway("g1", "gw05.ext.ffmuc.net")
            .gateway("g2", "gw05.ext.ffmuc.net")
            .gateway("g3", "gw06.ext.ffmuc.net")
            .tick()
        )
        found = outages(mesh.drop("g1", ago=40).drop("g2", ago=39))
        self.assertEqual(kinds(found), [("gateway", ("g1", "g2"))])


class GeneralTest(unittest.TestCase):
    def test_a_lone_failure(self):
        found = outages(chain().drop("d", ago=30))
        self.assertEqual(kinds(found), [("single", ("d",))])
        self.assertEqual(found[0].anchor, "gw")

    def test_nothing_down_means_no_outages(self):
        self.assertEqual(outages(chain()), [])

    def test_candidates_outside_the_down_set_are_ignored(self):
        snapshot = chain().drop("d", ago=30).snapshot()
        down = snapshot.down_since(STALE)
        self.assertEqual(len(correlate(["a", "d", "ghost"], Situation(snapshot.nodes, down))), 1)

    def test_the_answer_does_not_depend_on_input_order(self):
        mesh = (
            MeshBuilder()
            .gateway("gw")
            .node("hub", via="gw")
            .node("x", via="hub")
            .node("y", via="hub")
            .node("k", via="gw", name="Kiosk1")
            .node("l", via="gw", name="Kiosk2")
            .node("a", via="gw")
            .node("b", via="a")
            .tick()
            .drop("x", ago=40)
            .drop("y", ago=35)
            .drop("k", ago=33)
            .drop("l", ago=31)
            .drop("a", ago=30)
            .drop("b", ago=29)
        )
        snapshot = mesh.snapshot()
        down = snapshot.down_since(STALE)
        expected = correlate(down, Situation(snapshot.nodes, down, snapshot.links))
        rng = random.Random(7)
        for _ in range(20):
            ids = list(snapshot.nodes)
            rng.shuffle(ids)
            nodes = {i: snapshot.nodes[i] for i in ids}
            candidates = list(down)
            rng.shuffle(candidates)
            self.assertEqual(correlate(candidates, Situation(nodes, down, snapshot.links)), expected)


class NameFamilyTest(unittest.TestCase):
    def test_families(self):
        self.assertEqual(name_family("ffNoe-Stadt-St.Georg8"), "ffnoe-stadt-st.georg")
        self.assertEqual(name_family("www.ev-jugendhilfe.de-201"), "www.ev-jugendhilfe.de")
        self.assertEqual(name_family("gw05.ext.ffmuc.net"), "gw05.ext.ffmuc.net")
        self.assertEqual(name_family("Kiosk_12"), "kiosk")
        self.assertIsNone(name_family("ap7"))
        self.assertIsNone(name_family("42"))


if __name__ == "__main__":
    unittest.main()
