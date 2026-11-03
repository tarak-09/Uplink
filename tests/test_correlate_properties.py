"""Property tests: random meshes with planted failures, checked against invariants.

Set UPLINK_TEST_SEED to replay a failing run; every failure message prints its seed.
"""

import os
import random
import unittest
from datetime import timedelta

from uplink.correlate import Situation, correlate
from uplink.mesh import walk_upstream

from .support import MeshBuilder

CASES = 150
STALE = timedelta(seconds=180)


def random_mesh(rng: random.Random):
    """A random forest under a few gateways, with planted cascades and lone failures."""
    mesh = MeshBuilder()
    gateways = [f"gw{i}" for i in range(rng.randint(1, 3))]
    for gateway in gateways:
        mesh.gateway(gateway)
    ids: list[str] = []
    for i in range(rng.randint(5, 60)):
        node_id = f"n{i:02d}x"
        via = rng.choice(gateways + ids[-8:]) if ids else gateways[0]
        mesh.node(node_id, via=via, name=f"{node_id}.home", clients=rng.randint(0, 5))
        ids.append(node_id)
    mesh.tick()

    children: dict[str, list[str]] = {}
    for node_id in ids:
        children.setdefault(mesh.nodes[node_id]["gateway_nexthop"], []).append(node_id)

    def subtree(root: str) -> list[str]:
        out, stack = [], [root]
        while stack:
            current = stack.pop()
            out.append(current)
            stack.extend(children.get(current, []))
        return out

    planted: list[set[str]] = []
    taken: set[str] = set()
    clock = 30
    for root in rng.sample(ids, k=min(len(ids), rng.randint(1, 4))):
        members = [m for m in subtree(root) if m not in taken]
        if root not in members:
            continue
        # The root goes first; everything behind it follows within a few seconds.
        mesh.drop(root, ago=clock + 20)
        for member in members:
            if member != root:
                mesh.drop(member, ago=clock + 20 - rng.randint(0, 15))
        taken.update(members)
        planted.append(set(members))
        clock += 3600  # an hour between planted events keeps them apart
    return mesh, planted


class CorrelateProperties(unittest.TestCase):
    def test_invariants_hold_on_random_meshes(self):
        seed = int(os.environ.get("UPLINK_TEST_SEED") or random.SystemRandom().randrange(1 << 30))
        rng = random.Random(seed)
        for case in range(CASES):
            mesh, planted = random_mesh(rng)
            snapshot = mesh.snapshot()
            down = snapshot.down_since(STALE)
            situation = Situation(snapshot.nodes, down, snapshot.links)
            found = correlate(down, situation)
            where = f"(seed {seed}, case {case})"

            members = [m for o in found for m in o.members]
            self.assertEqual(sorted(members), sorted(down), f"every failure is in exactly one outage {where}")

            for outage in found:
                inside = set(outage.members)
                self.assertTrue(outage.roots, f"an outage has at least one root {where}")
                for item in outage.evidence:
                    self.assertTrue({item.a, item.b} <= inside, f"evidence stays inside its outage {where}")
                    if item.kind == "behind":
                        path = [hop.id for hop in walk_upstream(snapshot.nodes, item.a)]
                        self.assertIn(item.b, path, f"'behind' follows the real path {where}")
                        self.assertNotIn(item.a, outage.roots, f"a node behind another is not a root {where}")
                self.assertEqual(outage.started_at, min(down[m] for m in inside), where)

            for group in planted:
                owners = {o.members for o in found if group & set(o.members)}
                self.assertEqual(len(owners), 1, f"a planted cascade stays together {where}: {group}")
                owner = next(o for o in found if group & set(o.members))
                if len(group) > 1:
                    self.assertEqual(owner.kind, "cascade", f"a planted cascade is classified as one {where}")

            shuffled = list(down)
            rng.shuffle(shuffled)
            self.assertEqual(correlate(shuffled, situation), found, f"order does not matter {where}")


if __name__ == "__main__":
    unittest.main()
