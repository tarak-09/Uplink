import json
import unittest
from datetime import datetime, timedelta, timezone

from uplink.mesh import (
    FeedFormatError,
    Node,
    dependency_points,
    from_compact,
    parse_meshviewer,
    parse_time,
    to_compact,
    walk_upstream,
)

from .support import START, MeshBuilder, stamp


def small_mesh() -> MeshBuilder:
    return (
        MeshBuilder()
        .gateway("gw")
        .node("a", via="gw", clients=4)
        .node("b", via="a", clients=2)
        .node("c", via="b", clients=1)
        .node("d", via="gw")
        .link("a", "b")
        .link("b", "c", kind="other", quality=0.4)
        .link("a", "gw", kind="vpn")
    )


class ParsingTest(unittest.TestCase):
    def test_reads_nodes_links_and_times(self):
        snapshot = small_mesh().snapshot()
        self.assertEqual(snapshot.taken_at, START)
        self.assertEqual(set(snapshot.nodes), {"gw", "a", "b", "c", "d"})
        b = snapshot.nodes["b"]
        self.assertEqual((b.next_hop, b.gateway, b.clients, b.firmware), ("a", "gw", 2, "v2025.12.3"))
        self.assertTrue(snapshot.nodes["gw"].is_gateway)
        self.assertEqual(b.last_seen, START - timedelta(seconds=20))
        self.assertEqual(len(snapshot.links), 3)
        self.assertEqual(snapshot.links[1].quality, 0.4)

    def test_contact_details_are_never_read(self):
        document = small_mesh().document()
        self.assertIn("owner", document["nodes"][0])
        snapshot = parse_meshviewer(document)
        self.assertNotIn("private@example.org", repr(snapshot))
        self.assertNotIn("private@example.org", json.dumps(to_compact(snapshot)))

    def test_skips_entries_it_cannot_use(self):
        snapshot = parse_meshviewer(
            {
                "timestamp": stamp(START),
                "nodes": [
                    "not a node",
                    {"hostname": "no id"},
                    {
                        "node_id": "x",
                        "is_online": "yes",
                        "clients": True,
                        "lastseen": "yesterday",
                        "hostname": "x\x00\x1b",
                    },
                    {"node_id": "y", "clients": -3, "firmware": "not a dict"},
                    {"node_id": "z", "clients": "7"},
                ],
                "links": [
                    {"source": "x"},
                    {"source": "x", "target": "x"},
                    {"source": "x", "target": "y", "source_tq": True},
                ],
            }
        )
        self.assertEqual(set(snapshot.nodes), {"x", "y", "z"})
        x = snapshot.nodes["x"]
        self.assertEqual((x.online, x.clients, x.last_seen, x.name), (False, 0, None, "x"))
        self.assertEqual((snapshot.nodes["y"].clients, snapshot.nodes["y"].firmware), (0, None))
        self.assertEqual(snapshot.nodes["z"].clients, 7)
        self.assertEqual(len(snapshot.links), 1)
        self.assertIsNone(snapshot.links[0].quality)

    def test_rejects_documents_that_are_not_snapshots(self):
        for document in (
            [],
            {"nodes": []},
            {"timestamp": "2026-11-02T12:00:00+0100"},
            {"timestamp": "soon", "nodes": []},
        ):
            with self.subTest(document=document), self.assertRaises(FeedFormatError):
                parse_meshviewer(document)

    def test_parse_time_accepts_the_formats_maps_use(self):
        expected = datetime(2026, 10, 22, 16, 15, 36, tzinfo=timezone(timedelta(hours=2)))
        self.assertEqual(parse_time("2026-10-22T16:15:36+0200"), expected)
        self.assertEqual(parse_time("2026-10-22T16:15:36+02:00"), expected)
        self.assertEqual(parse_time("2026-10-22T14:15:36Z"), expected)
        self.assertEqual(parse_time("2026-10-22T14:15:36"), expected)
        self.assertEqual(parse_time("2026-10-22T16:15:36.250+0200"), expected + timedelta(milliseconds=250))
        for bad in (None, "", "22.10.2026", 1729606536):
            self.assertIsNone(parse_time(bad))


class SnapshotTest(unittest.TestCase):
    def test_a_silent_node_counts_as_down_before_the_map_notices(self):
        mesh = small_mesh().tick(60).silence("c", ago=200).drop("d")
        snapshot = mesh.snapshot()
        self.assertTrue(snapshot.nodes["c"].online)
        self.assertTrue(snapshot.is_reachable("c"))
        self.assertFalse(snapshot.is_reachable("c", timedelta(seconds=180)))
        self.assertFalse(snapshot.is_reachable("missing"))
        down = snapshot.down_since(timedelta(seconds=180))
        self.assertEqual(set(down), {"c", "d"})
        self.assertEqual(down["c"], mesh.now - timedelta(seconds=200))
        self.assertEqual(snapshot.reachable_count(timedelta(seconds=180)), 3)
        self.assertEqual(snapshot.clients_online(timedelta(seconds=180)), 6)

    def test_compact_round_trip_keeps_everything(self):
        snapshot = small_mesh().tick().drop("c").snapshot()
        again = from_compact(json.loads(json.dumps(to_compact(snapshot))))
        self.assertEqual(again.taken_at, snapshot.taken_at)
        self.assertEqual(again.nodes, snapshot.nodes)
        self.assertEqual(again.links, snapshot.links)

    def test_compact_round_trip_keeps_the_maps_zero_time(self):
        # The live map writes Go's zero time for a boot time it does not know. On Windows its epoch
        # seconds could not be turned back into a date, so `uplink run` crashed loading the snapshot.
        document = small_mesh().document()
        document["nodes"][-1]["uptime"] = "0001-01-01T00:00:00Z"
        snapshot = parse_meshviewer(document)
        again = from_compact(json.loads(json.dumps(to_compact(snapshot))))
        zero = datetime(1, 1, 1, tzinfo=timezone.utc)
        self.assertIn(zero, [node.booted_at for node in snapshot.nodes.values()])
        self.assertEqual(again.nodes, snapshot.nodes)

    def test_compact_times_beyond_the_calendar_count_as_unknown(self):
        row = ["a", "a.home", 1, 10**12, 0, 0, "gw", "gw", -(10**12), "v1", "m"]
        snapshot = from_compact({"v": 1, "t": "2026-11-02T12:00:00+01:00", "n": [row]})
        self.assertEqual((snapshot.nodes["a"].last_seen, snapshot.nodes["a"].booted_at), (None, None))

    def test_compact_rejects_unknown_versions_and_damage(self):
        with self.assertRaises(FeedFormatError):
            from_compact({"v": 99})
        with self.assertRaises(FeedFormatError):
            from_compact({"v": 1, "t": "2026-11-02T12:00:00+01:00", "n": [["too", "short"]]})


class TopologyTest(unittest.TestCase):
    def test_walk_upstream_goes_to_the_gateway(self):
        nodes = small_mesh().snapshot().nodes
        self.assertEqual([n.id for n in walk_upstream(nodes, "c")], ["b", "a", "gw"])
        self.assertEqual(list(walk_upstream(nodes, "gw")), [])

    def test_walk_upstream_survives_loops_and_unknown_hops(self):
        def node(node_id, hop):
            return Node(node_id, node_id, True, START, next_hop=hop)

        looped = {"x": node("x", "y"), "y": node("y", "z"), "z": node("z", "x")}
        self.assertEqual([n.id for n in walk_upstream(looped, "x")], ["y", "z"])
        dangling = {"x": node("x", "nowhere")}
        self.assertEqual(list(walk_upstream(dangling, "x")), [])
        self.assertEqual(list(walk_upstream({"x": node("x", "x")}, "x")), [])

    def test_dependency_points_rank_by_what_sits_behind(self):
        points = dependency_points(small_mesh().snapshot())
        self.assertEqual([(p.node.id, p.nodes_behind, p.clients_behind) for p in points], [("a", 2, 3), ("b", 1, 1)])

    def test_dependency_points_ignore_unreachable_nodes(self):
        snapshot = small_mesh().tick().drop("c").snapshot()
        self.assertEqual([(p.node.id, p.nodes_behind) for p in dependency_points(snapshot)], [("a", 1)])


if __name__ == "__main__":
    unittest.main()
