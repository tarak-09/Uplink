import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from uplink.facts import outage_facts, resolved_facts
from uplink.incidents import IncidentTracker, Policy, Update

from .support import MeshBuilder, make_settings

STALE = timedelta(seconds=180)


class Run:
    """Feeds the mesh, as it is at each moment, to a tracker; delivers notices unless told not to."""

    def __init__(self, mesh: MeshBuilder, tracker: IncidentTracker | None = None, **policy) -> None:
        self.mesh = mesh
        self.tracker = tracker or IncidentTracker(Policy(**policy), stale_after=STALE)
        self.previous = mesh.snapshot()
        if self.tracker.watching_since is None:
            self.tracker.start(self.previous.taken_at)
        self.sent = []

    def poll(self, *, deliver: bool = True) -> Update:
        now = self.mesh.snapshot()
        update = self.tracker.update(self.previous, now)
        self.previous = now
        if deliver:
            for notice in update.notices:
                self.tracker.mark_sent(notice, f"ref-{len(self.sent)}", now.taken_at)
                self.sent.append(notice)
        return update

    def only_incident(self):
        incidents = list(self.tracker.open.values())
        assert len(incidents) == 1, incidents
        return incidents[0]


def chain_mesh() -> MeshBuilder:
    return (
        MeshBuilder()
        .gateway("gw")
        .node("a", via="gw", clients=5)
        .node("b", via="a", clients=3)
        .node("c", via="b", clients=2)
        .node("d", via="gw", clients=1)
    )


class LifecycleTest(unittest.TestCase):
    def test_a_cascade_is_confirmed_alerted_and_resolved(self):
        run = Run(chain_mesh())
        run.mesh.tick().drop("a", ago=40).drop("b", ago=35).drop("c", ago=30)
        first = run.poll()
        self.assertEqual([c.kind for c in first.changes], ["opened"])
        self.assertEqual(first.notices, [], "one poll is not enough to alert")
        incident = run.only_incident()
        self.assertEqual((incident.kind, incident.roots, incident.size, incident.users), ("cascade", ("a",), 3, 10))

        run.mesh.tick()
        second = run.poll()
        self.assertEqual([(n.kind, n.incident.id) for n in second.notices], [("alert", incident.id)])
        self.assertEqual(incident.alert_ref, "ref-0")

        run.mesh.tick().restore("a", "b", "c")
        third = run.poll()
        self.assertEqual([n.kind for n in third.notices], ["resolved"])
        self.assertEqual(run.tracker.open, {})
        self.assertEqual(incident.closed_reason, "resolved")
        self.assertEqual(incident.resolved_at, run.previous.taken_at)

    def test_a_silent_node_is_noticed_before_the_map_marks_it_offline(self):
        run = Run(chain_mesh())
        run.mesh.tick().silence("a", "b", "c", ago=200)
        run.poll()
        incident = run.only_incident()
        self.assertEqual(incident.kind, "cascade")
        self.assertTrue(all(run.previous.nodes[m].online for m in incident.members))

    def test_a_brief_flap_never_alerts(self):
        run = Run(chain_mesh())
        run.mesh.tick().drop("a", ago=40).drop("b", ago=35)
        run.poll()
        run.mesh.tick().restore("a", "b")
        update = run.poll()
        self.assertEqual(update.notices, [])
        self.assertEqual([c.kind for c in update.changes], ["resolved"])
        self.assertEqual(run.sent, [])

    def test_late_arrivals_join_the_incident(self):
        run = Run(chain_mesh())
        run.mesh.tick().drop("a", ago=40)
        run.poll()
        run.mesh.tick().drop("b", "c", ago=30)
        update = run.poll()
        incident = run.only_incident()
        self.assertEqual(incident.size, 3)
        self.assertEqual(incident.kind, "cascade")
        self.assertIn("grew", [c.kind for c in update.changes])
        self.assertEqual([n.incident.size for n in update.notices], [3])

    def test_incidents_merge_when_new_evidence_links_them(self):
        mesh = MeshBuilder().gateway("gw")
        booted = mesh.now - timedelta(days=1)
        mesh.node("x", via="gw", name="Kiosk1").node("y", via="gw", booted=booted).node(
            "z", via="gw", name="Kiosk2", booted=booted + timedelta(seconds=20)
        )
        run = Run(mesh)
        mesh.tick().drop("x", ago=30).drop("y", ago=20)
        run.poll()
        self.assertEqual(len(run.tracker.open), 2)
        mesh.tick().drop("z", ago=50)
        update = run.poll()
        incident = run.only_incident()
        self.assertEqual((incident.kind, incident.size), ("site", 3))
        self.assertIn("merged", [c.kind for c in update.changes])

    def test_stragglers_get_a_grace_period(self):
        run = Run(chain_mesh())
        run.mesh.tick().drop("a", ago=40).drop("b", ago=35).drop("c", ago=30)
        run.poll()
        run.mesh.tick()
        run.poll()
        run.mesh.tick().restore("a", "b")
        self.assertEqual(run.poll().notices, [], "the root is back, but c might still follow")
        for _ in range(4):
            run.mesh.tick()
            self.assertEqual(run.poll().notices, [])
        run.mesh.tick()
        resolved = run.poll().notices
        self.assertEqual([n.kind for n in resolved], ["resolved"])
        facts = resolved_facts(resolved[0].incident, run.previous, make_settings(Path(".")))
        self.assertEqual((facts["nodes_back"], facts["nodes_total"], facts["still_down"]), (2, 3, ["c.home"]))

        run.mesh.tick()
        self.assertEqual(run.poll().changes, [], "a straggler of a closed incident does not reopen it")

    def test_failures_from_before_watching_are_ignored(self):
        mesh = chain_mesh().tick().drop("d", ago=600)
        run = Run(mesh)
        mesh.tick()
        run.poll()
        self.assertEqual(run.tracker.open, {})


class PolicyTest(unittest.TestCase):
    def alert_for_d(self, **policy):
        run = Run(chain_mesh(), **policy)
        run.mesh.tick().drop("d", ago=30)
        run.poll()
        run.mesh.tick()
        return run.poll()

    def test_a_lone_node_does_not_alert(self):
        self.assertEqual(self.alert_for_d().notices, [])

    def test_a_watched_node_alerts_alone(self):
        self.assertEqual(len(self.alert_for_d(watch=frozenset({"d"})).notices), 1)
        self.assertEqual(len(self.alert_for_d(watch=frozenset({"d.home"})).notices), 1)

    def test_a_lone_node_with_many_people_alerts(self):
        self.assertEqual(len(self.alert_for_d(min_users=1).notices), 1)

    def test_a_routine_outage_is_held_back(self):
        run = Run(chain_mesh(), watch=frozenset({"d"}))
        start = run.mesh.now + timedelta(seconds=30)
        history = {"d": [(start - timedelta(days=days, minutes=10)).isoformat() for days in (1, 2)]}
        run.tracker.restore({**run.tracker.to_dict(), "history": history})
        run.mesh.tick().drop("d", ago=30)
        run.poll()
        run.mesh.tick()
        update = run.poll()
        self.assertEqual(update.notices, [])
        self.assertEqual([(c.kind, c.detail) for c in update.changes], [("suppressed", "routine")])

    def test_once_is_not_a_routine(self):
        run = Run(chain_mesh(), watch=frozenset({"d"}))
        start = run.mesh.now + timedelta(seconds=30)
        run.tracker.restore({**run.tracker.to_dict(), "history": {"d": [(start - timedelta(days=1)).isoformat()]}})
        run.mesh.tick().drop("d", ago=30)
        run.poll()
        run.mesh.tick()
        self.assertEqual(len(run.poll().notices), 1)

    def test_the_hourly_limit_holds_back_extra_alerts(self):
        mesh = chain_mesh().node("p", via="gw").node("q", via="p")
        run = Run(mesh, alerts_per_hour=1)
        mesh.tick().drop("a", "b", ago=40).drop("p", "q", ago=35)
        run.poll()
        mesh.tick()
        update = run.poll()
        self.assertEqual(len(update.notices), 1)
        self.assertEqual([(c.kind, c.detail) for c in update.changes], [("suppressed", "rate_limited")])

    def test_sending_is_retried_then_given_up(self):
        run = Run(chain_mesh())
        run.mesh.tick().drop("a", "b", ago=40)
        run.poll(deliver=False)
        for _ in range(3):
            run.mesh.tick()
            notices = run.poll(deliver=False).notices
            self.assertEqual(len(notices), 1)
            run.tracker.mark_failed(notices[0])
        run.mesh.tick()
        self.assertEqual(run.poll(deliver=False).notices, [])
        self.assertEqual(run.only_incident().suppressed, "send_failed")

    def test_old_incidents_expire(self):
        run = Run(chain_mesh(), expire_after=timedelta(minutes=10))
        run.mesh.tick().drop("a", "b", ago=40)
        run.poll()
        for _ in range(10):
            run.mesh.tick()
            update = run.poll()
        self.assertEqual(run.tracker.open, {})
        self.assertIn("expired", [c.kind for c in update.changes])


class DetailsTest(unittest.TestCase):
    def test_state_survives_a_restart(self):
        run = Run(chain_mesh())
        run.mesh.tick().drop("a", "b", "c", ago=40)
        run.poll()
        saved = json.loads(json.dumps(run.tracker.to_dict()))

        restored = IncidentTracker(Policy(), stale_after=STALE)
        restored.restore(saved)
        resumed = Run(run.mesh, tracker=restored)
        resumed.previous = run.previous
        run.mesh.tick()
        self.assertEqual([n.kind for n in resumed.poll().notices], ["alert"])
        run.mesh.tick().restore("a", "b", "c")
        self.assertEqual([n.kind for n in resumed.poll().notices], ["resolved"])

    def test_new_firmware_on_return_reads_as_an_update(self):
        run = Run(chain_mesh())
        run.mesh.tick().drop("a", "b", ago=40)
        run.poll()
        run.mesh.tick()
        run.poll()
        run.mesh.tick().restore("a", "b", firmware="v2026.1.0")
        notice = run.poll().notices[0]
        with tempfile.TemporaryDirectory() as folder:
            facts = resolved_facts(notice.incident, run.previous, make_settings(Path(folder)))
        self.assertEqual(facts["came_back_on_new_firmware"], "v2026.1.0")
        self.assertEqual(facts["duration_minutes"], 3)

    def test_nodes_that_found_another_path_are_counted(self):
        mesh = MeshBuilder().gateway("gw").node("a", via="gw", clients=2).node("b", via="a").node("e", via="a")
        run = Run(mesh)
        mesh.tick().drop("a", "b", ago=40).reroute("e", "gw")
        run.poll()
        mesh.tick()
        notice = run.poll().notices[0]
        facts = outage_facts(notice.incident, run.previous, make_settings(Path(".")))
        self.assertEqual(facts["nodes_that_found_another_path"], 1)
        self.assertEqual(facts["people_affected"], 2)


if __name__ == "__main__":
    unittest.main()
