"""Twelve simulated hours of a busy mesh, with restarts, checked for consistency at the end.

Set UPLINK_TEST_SEED to replay a run; failures print the seed.
"""

import io
import json
import os
import random
import tempfile
import time
import unittest
from collections import Counter
from pathlib import Path

from uplink.feed import FileFeed
from uplink.monitor import ConsoleSender, Monitor
from uplink.store import Store
from uplink.writer import Writer

from .support import MeshBuilder, make_settings

HOURS = 12
RESTART_EVERY = 150


class SoakTest(unittest.TestCase):
    def test_twelve_hours_with_restarts(self):
        seed = int(os.environ.get("UPLINK_TEST_SEED") or random.SystemRandom().randrange(1 << 30))
        rng = random.Random(seed)
        where = f"(seed {seed})"

        mesh = MeshBuilder()
        for g in range(3):
            mesh.gateway(f"gw{g}")
        ids = []
        for i in range(150):
            node_id = f"n{i:03d}x"
            via = rng.choice([f"gw{rng.randrange(3)}"] + ids[-10:]) if ids else "gw0"
            name = f"Kiosk{i}" if i % 17 == 0 else None
            mesh.node(node_id, via=via, name=name, clients=rng.randint(0, 6))
            ids.append(node_id)

        def subtree(root):
            out, stack = [], [root]
            while stack:
                current = stack.pop()
                out.append(current)
                stack.extend(n for n in ids if mesh.nodes[n]["gateway_nexthop"] == current)
            return out

        with tempfile.TemporaryDirectory() as folder:
            settings = make_settings(Path(folder), language=rng.choice(["english", "telugu", "hindi"]))
            store = Store(Path(folder))
            sender = ConsoleSender(io.StringIO())

            def new_monitor():
                writer = Writer(None, language=settings.language, network=settings.network_name)
                return Monitor(
                    settings=settings, feed=FileFeed(Path(folder, "unused")), store=store, writer=writer, sender=sender
                )

            monitor = new_monitor()
            started = time.monotonic()
            for minute in range(HOURS * 60):
                down = [n for n in ids if not mesh.nodes[n]["is_online"]]
                up = [n for n in ids if mesh.nodes[n]["is_online"]]
                roll = rng.random()
                if roll < 0.03 and up:
                    victims = [n for n in subtree(rng.choice(up)) if mesh.nodes[n]["is_online"]][:20]
                    for victim in victims:
                        mesh.drop(victim, ago=rng.randint(20, 45))
                elif roll < 0.06 and up:
                    mesh.drop(rng.choice(up), ago=rng.randint(20, 45))
                elif roll < 0.08 and up:
                    mesh.silence(rng.choice(up), ago=rng.randint(200, 300))
                elif roll < 0.16 and down:
                    back = rng.sample(down, k=min(len(down), rng.randint(1, 8)))
                    mesh.restore(*back, firmware=rng.choice([None, None, "v2026.1.0"]))
                monitor.observe(mesh.snapshot())
                mesh.tick()
                if minute and minute % RESTART_EVERY == 0:
                    monitor = new_monitor()
            elapsed = time.monotonic() - started

            events = [
                json.loads(line) for line in Path(folder, "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            alerts = Counter(e["id"] for e in events if e["event"] == "alert_sent" and e.get("notice") == "alert")
            resolved = [e["id"] for e in events if e["event"] == "alert_sent" and e.get("notice") == "resolved"]
            statuses = Counter(e["status"] for e in events if e["event"] == "poll")

            self.assertTrue(alerts, f"a busy mesh produces alerts {where}")
            self.assertEqual(max(alerts.values()), 1, f"no incident is alerted twice {where}")
            for incident_id in resolved:
                self.assertIn(incident_id, alerts, f"recoveries follow alerts {where}")
            self.assertEqual(sum(statuses.values()), HOURS * 60, f"every snapshot was processed {where}")
            self.assertLessEqual(set(statuses), {"baseline", "ok"}, f"no gaps or glitches in a steady run {where}")
            self.assertEqual(len(sender.sent), len([e for e in events if e["event"] == "alert_sent"]), where)

            final = new_monitor()
            for incident in final.tracker.open.values():
                roots_down = [r for r in incident.roots if not incident.members[r].is_back]
                stragglers = [m for m in incident.members.values() if not m.is_back]
                self.assertTrue(roots_down or stragglers, f"open incidents still have something down {where}")
            self.assertLess(elapsed, 60, f"twelve hours of polls take seconds, not minutes {where}")


if __name__ == "__main__":
    unittest.main()
