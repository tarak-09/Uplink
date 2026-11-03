"""Uplink on a real Freifunk München snapshot (23 Oct 2026, 04:22).

The fixture is the real data trimmed to the nodes that matter, with contact details and
locations removed and router names replaced while keeping their families. Set
UPLINK_FULL_SNAPSHOT to a complete meshviewer.json to run the same checks on all of it.
"""

import os
import unittest
from datetime import timedelta
from pathlib import Path

from uplink.correlate import Situation, correlate
from uplink.feed import read_snapshot

from .support import FIXTURE

STALE = timedelta(seconds=180)


def outages_in(path: Path):
    snapshot = read_snapshot(path)
    down = snapshot.down_since(STALE)
    recent = [n for n, t in down.items() if snapshot.taken_at - t <= timedelta(hours=24)]
    return snapshot, correlate(recent, Situation(snapshot.nodes, down, snapshot.links))


class MunichSnapshotTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snapshot, cls.outages = outages_in(FIXTURE)
        cls.by_root = {o.roots[0]: o for o in cls.outages}

    def test_the_rooftop_cascade(self):
        """An outdoor antenna went down at 16:15 and six antennas behind it followed within 15 s."""
        cascade = self.by_root["f4f26dd477b2"]
        self.assertEqual(cascade.kind, "cascade")
        self.assertEqual(
            set(cascade.members),
            {
                "f4f26dd477b2",
                "18a6f7caa24a",
                "704f57ad4674",
                "18a6f7cf1730",
                "18a6f7cf19d6",
                "18a6f7caa170",
                "18a6f7cabe5c",
            },
        )
        self.assertEqual(cascade.spread, timedelta(seconds=15))
        self.assertEqual(f"{cascade.started_at:%H:%M}", "16:15")
        # Its own uplink neighbour failed two hours later, so at 16:15 it was still up.
        self.assertEqual(cascade.anchor, "cc32e54e7b3e")
        self.assertEqual(self.snapshot.nodes[cascade.roots[0]].model, "TP-Link CPE210 v1")

    def test_six_access_points_across_three_gateways(self):
        site = next(o for o in self.outages if o.kind == "site" and o.size == 6)
        self.assertEqual(f"{site.started_at:%H:%M}", "22:03")
        self.assertEqual(site.spread, timedelta(seconds=7))
        self.assertEqual({e.kind for e in site.evidence}, {"name_family"})
        self.assertEqual(len({site.anchors[r] for r in site.roots}), 3)

    def test_three_access_points_that_share_power(self):
        site = next(o for o in self.outages if o.kind == "site" and o.size == 3)
        self.assertEqual(f"{site.started_at:%H:%M}", "23:26")
        self.assertIn("co_booted", {e.kind for e in site.evidence})

    def test_two_small_cascades(self):
        small = sorted(f"{o.started_at:%H:%M}" for o in self.outages if o.kind == "cascade" and o.size == 2)
        self.assertEqual(small, ["17:59", "21:24"])

    def test_two_nodes_going_quiet_before_the_map_noticed(self):
        quiet = next(o for o in self.outages if o.kind == "shared_neighbor")
        self.assertEqual(quiet.size, 2)
        self.assertTrue(all(self.snapshot.nodes[m].online for m in quiet.members))

    def test_everything_else_is_a_lone_failure(self):
        self.assertEqual(len(self.outages), 63)
        self.assertEqual(sum(1 for o in self.outages if o.size == 1), 57)

    def test_the_fixture_carries_no_contact_details(self):
        text = FIXTURE.read_text(encoding="utf-8")
        for field in ('"owner"', '"contact"', '"location"', '"addresses"'):
            self.assertNotIn(field, text)


@unittest.skipUnless(os.environ.get("UPLINK_FULL_SNAPSHOT"), "set UPLINK_FULL_SNAPSHOT to a full meshviewer.json")
class FullSnapshotTest(unittest.TestCase):
    def test_full_snapshot_gives_the_same_outages(self):
        _, full = outages_in(Path(os.environ["UPLINK_FULL_SNAPSHOT"]))
        _, trimmed = outages_in(FIXTURE)
        self.assertEqual([(o.kind, o.members) for o in full], [(o.kind, o.members) for o in trimmed])


if __name__ == "__main__":
    unittest.main()
