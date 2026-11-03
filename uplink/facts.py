"""What a message may say: names, times and numbers, settled before any model sees them.

Facts are plain JSON-ready dicts. The model only rephrases them, and the writer rejects any
draft that mentions a number not found here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping

from .config import Settings
from .incidents import Incident
from .mesh import Node, Snapshot, walk_upstream
from .templates import duration

HEARD_AFTER = timedelta(seconds=60)


@dataclass(frozen=True)
class Namer:
    """Display names. With anonymize on, people's routers become "node 4f2a9c"; gateways keep their names."""

    nodes: Mapping[str, Node]
    anonymize: bool = False
    remembered: Mapping[str, str] = field(default_factory=dict)

    def __call__(self, node_id: str) -> str:
        node = self.nodes.get(node_id)
        if node is not None and node.is_gateway:
            return node.name
        if self.anonymize:
            return f"node {node_id[-6:]}"
        return self.remembered.get(node_id) or (node.name if node else f"node {node_id[-6:]}")


def namer_for(incident: Incident | None, snapshot: Snapshot, settings: Settings) -> Namer:
    remembered = {m.id: m.name for m in incident.members.values()} if incident else {}
    return Namer(snapshot.nodes, settings.anonymize, remembered)


def clock(moment: datetime, reference: datetime, label: str = "") -> str:
    """ "16:15" on the reference day, "22/10 16:15" otherwise, in the network's own time zone.

    Messages pass the TIME_ZONE_LABEL as `label` ("16:15 Munich time"), so readers elsewhere know whose clock it is.
    """
    local = moment.astimezone(reference.tzinfo)
    shown = f"{local:%H:%M}" if local.date() == reference.date() else f"{local:%d/%m %H:%M}"
    return f"{shown} {label}" if label else shown


def minutes_between(start: datetime, end: datetime) -> int:
    return max(0, round((end - start).total_seconds() / 60))


def outage_facts(incident: Incident, snapshot: Snapshot, settings: Settings) -> dict[str, Any]:
    name = namer_for(incident, snapshot, settings)
    roots = [name(r) for r in incident.roots]
    anchor = name(incident.anchor) if incident.anchor else None
    via_gateway = incident.anchor in incident.gateway_ids
    facts: dict[str, Any] = {
        "event": "outage",
        "network": settings.network_name,
        "kind": incident.kind,
        "started": clock(incident.started_at, snapshot.taken_at, settings.time_zone_label),
        "nodes_offline": incident.size,
        "root": roots[0],
    }
    if len(roots) > 1:
        facts["first_to_fail"] = roots[:4]
    if incident.kind == "cascade":
        facts["nodes_behind_root"] = incident.size - 1
    if anchor:
        facts["internet_via_gateway" if via_gateway else "still_online_above"] = anchor
    if incident.users_known:
        facts["people_affected"] = incident.users
    if incident.size > 1:
        facts["failed_within_seconds"] = int(incident.outage.spread.total_seconds())
    rerouted = rerouted_count(incident, snapshot, timedelta(seconds=settings.stale_after))
    if rerouted:
        facts["nodes_that_found_another_path"] = rerouted
    evidence = evidence_lines(incident, name, snapshot, settings.anonymize, settings.time_zone_label)
    if evidence:
        facts["evidence"] = evidence
    facts["likely_cause"], facts["check_first"] = cause_and_check(
        incident.kind, roots[0], anchor, via_gateway=via_gateway
    )
    facts["affected_nodes"] = [name(m) for m in incident.outage.members[:8]]
    return facts


def resolved_facts(incident: Incident, snapshot: Snapshot, settings: Settings) -> dict[str, Any]:
    name = namer_for(incident, snapshot, settings)
    ended = incident.resolved_at or snapshot.taken_at
    minutes = minutes_between(incident.started_at, ended)
    back = [m for m in incident.members.values() if m.is_back]
    still_down = [name(m.id) for m in incident.members.values() if not m.is_back]
    root = incident.members[incident.roots[0]]
    facts: dict[str, Any] = {
        "event": "resolved",
        "network": settings.network_name,
        "kind": incident.kind,
        "root": name(root.id),
        "started": clock(incident.started_at, snapshot.taken_at, settings.time_zone_label),
        "ended": clock(ended, snapshot.taken_at, settings.time_zone_label),
        "duration": duration(minutes, "english"),
        "duration_minutes": minutes,
        "nodes_back": len(back),
        "nodes_total": incident.size,
    }
    if still_down:
        facts["still_down"] = still_down[:6]
    if root.firmware and root.firmware_after and root.firmware != root.firmware_after:
        facts["came_back_on_new_firmware"] = root.firmware_after
    return facts


def stale_feed_facts(since: datetime, now: datetime, settings: Settings) -> dict[str, Any]:
    return {
        "event": "feed_stale",
        "network": settings.network_name,
        "since": clock(since, now, settings.time_zone_label),
    }


def network_facts(
    snapshot: Snapshot,
    open_incidents: Iterable[Incident],
    recent_incidents: Iterable[Incident],
    settings: Settings,
    question: str = "",
) -> dict[str, Any]:
    stale = timedelta(seconds=settings.stale_after)
    ongoing = list(open_incidents)
    notable = [i for i in ongoing if i.size > 1 or i.kind == "gateway" or i.alerted_at]
    finished = [i for i in recent_incidents if i.size > 1 or i.alerted_at]
    facts: dict[str, Any] = {
        "network": settings.network_name,
        "as_of": clock(snapshot.taken_at, snapshot.taken_at, settings.time_zone_label),
        "nodes_online": snapshot.reachable_count(stale),
        "nodes_total": len(snapshot.nodes),
        "people_online": snapshot.clients_online(stale),
        "open_outages": [_brief(i, snapshot, settings) for i in notable],
        "lone_nodes_offline": len(ongoing) - len(notable),
        "recent_outages": [_brief(i, snapshot, settings) for i in finished[-5:]],
    }
    asked = [_node_brief(n, snapshot, settings) for n in find_nodes(question, snapshot)]
    if asked:
        facts["nodes_asked_about"] = asked
    return facts


def find_nodes(question: str, snapshot: Snapshot, limit: int = 5) -> list[Node]:
    """Nodes the question names: by full name, by id, or by a numbered part ("is Daniel5 up?")."""
    text = question.lower()
    tokens = {token.strip(".,;:!?()\"'") for token in text.split()}
    found = []
    for node in snapshot.nodes.values():
        name = node.name.lower()
        numbered = {w for w in re.split(r"[\s/_-]+", name) if any(c.isdigit() for c in w)}
        if (len(name) >= 4 and name in text) or node.id in tokens or tokens & numbered:
            found.append(node)
    return sorted(found, key=lambda n: (not n.online, n.name))[:limit]


def rerouted_count(incident: Incident, snapshot: Snapshot, stale_after: timedelta) -> int:
    """Nodes that used to route through the failed node and have been heard from since it failed."""
    heard_after = incident.started_at + HEARD_AFTER
    return sum(
        1
        for node_id in incident.behind_before - incident.members.keys()
        if snapshot.is_reachable(node_id, stale_after)
        and (seen := snapshot.nodes[node_id].last_seen) is not None
        and seen > heard_after
    )


def evidence_lines(
    incident: Incident, name: Namer, snapshot: Snapshot, anonymize: bool, time_zone_label: str = ""
) -> list[str]:
    """Plain-English reasons, built from the evidence that grouped these failures."""
    evidence = incident.evidence
    kinds = {e.kind for e in evidence}
    lines: list[str] = []
    if incident.kind == "cascade" and "behind" in kinds:
        root = name(incident.roots[0])
        behind = incident.size - 1
        lines.append(
            f"the other node reaches the internet only through {root}"
            if behind == 1
            else f"the other {behind} reach the internet only through {root}"
        )
    if "shared_uplink" in kinds and incident.anchor and incident.kind != "cascade":
        lines.append(f"they all connect through {name(incident.anchor)}, which stayed online")
    families = sorted({e.detail for e in evidence if e.kind == "name_family"})
    if families:
        shown = "" if anonymize else " (" + ", ".join(f + "…" for f in families[:2]) + ")"
        lines.append(f"their names form one set{shown}")
    co_booted = [e for e in evidence if e.kind == "co_booted"]
    if co_booted:
        when = clock(datetime.fromisoformat(min(e.detail for e in co_booted)), snapshot.taken_at, time_zone_label)
        together = {n for e in co_booted for n in (e.a, e.b)}
        if len(together) == incident.size:
            lines.append(f"they last booted within the same minute ({when}), so they likely share a power supply")
        elif incident.kind != "cascade":
            lines.append(
                f"{len(together)} of them last booted within the same minute ({when}), so they likely share power"
            )
    if "mesh_link" in kinds:
        lines.append("they are linked to each other directly")
    if incident.kind == "site":
        gateways = {incident.anchors[r] for r in incident.roots if r in incident.anchors}
        if len(gateways) >= 2:
            lines.append(f"they used {len(gateways)} different gateways, so the gateways are not the problem")
    return lines[:4]


def cause_and_check(kind: str, root: str, anchor: str | None, *, via_gateway: bool = False) -> tuple[str, str]:
    """The most likely cause and the first thing to check, for each kind of outage."""
    if kind == "gateway":
        return f"the gateway {root} stopped answering", "the gateway server and its VPN service"
    if kind == "cascade":
        then = ""
        if anchor:
            then = ", then its internet line" if via_gateway else f", then its mesh link to {anchor}"
        return (
            f"{root} failed (power, cabling or its own uplink) and the nodes behind it had no other path",
            f"power and cabling at {root}{then}",
        )
    if kind == "shared_neighbor":
        return (
            f"the links into {anchor} failed while {anchor} itself stayed online (radio, interference or a moved antenna)",
            f"the radio and antenna at {anchor}",
        )
    if kind == "site":
        return "a power or internet cut at their location", "power and the internet line at that location"
    return f"power or internet at {root}", f"power at {root}"


def _brief(incident: Incident, snapshot: Snapshot, settings: Settings) -> dict[str, Any]:
    name = namer_for(incident, snapshot, settings)
    via_gateway = incident.anchor in incident.gateway_ids
    brief: dict[str, Any] = {
        "kind": incident.kind,
        "started": clock(incident.started_at, snapshot.taken_at, settings.time_zone_label),
        "nodes": incident.size,
        "root": name(incident.roots[0]),
    }
    anchor = name(incident.anchor) if incident.anchor else None
    if anchor:
        brief["internet_via_gateway" if via_gateway else "still_online_above"] = anchor
    if incident.users_known:
        brief["people_affected"] = incident.users
    if incident.resolved_at:
        brief["ended"] = clock(incident.resolved_at, snapshot.taken_at, settings.time_zone_label)
        brief["duration"] = duration(minutes_between(incident.started_at, incident.resolved_at), "english")
    else:
        brief["likely_cause"] = cause_and_check(incident.kind, brief["root"], anchor, via_gateway=via_gateway)[0]
    return brief


def _node_brief(node: Node, snapshot: Snapshot, settings: Settings) -> dict[str, Any]:
    name = Namer(snapshot.nodes, settings.anonymize)
    reachable = snapshot.is_reachable(node.id, timedelta(seconds=settings.stale_after))
    brief: dict[str, Any] = {"name": name(node.id), "online": reachable}
    if node.last_seen:
        brief["last_heard"] = clock(node.last_seen, snapshot.taken_at, settings.time_zone_label)
    if reachable:
        brief["people_connected"] = node.clients
    path = [name(hop.id) for hop in walk_upstream(snapshot.nodes, node.id)][:4]
    if path:
        brief["path_to_internet"] = path
    return brief
