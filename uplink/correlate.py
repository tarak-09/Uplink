"""Group node failures that share a cause, using only what the public map shows.

One router going dark is usually someone unplugging it. Several going dark together
usually share a cause, and the map data says which one. Uplink looks for five kinds of
evidence between two failed nodes:

    behind         one reached the internet through the other, which failed first
    shared_uplink  both reached the internet through the same neighbour, which stayed up
    mesh_link      they were wired or radio-linked to each other
    co_booted      they last booted within the same minute, so they likely share power
    name_family    the operator named them as a set ("St.Georg3", "St.Georg5")

Every piece of evidence also requires the failures to line up in time; on its own, a shared
name or a shared neighbour proves nothing. Evidence joins failed nodes into groups, and
each group becomes one Outage whose kind says what most likely broke.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import pairwise
from typing import Collection, Iterable, Literal, Mapping, Sequence

from .mesh import Link, Node, walk_upstream

EvidenceKind = Literal["behind", "shared_uplink", "mesh_link", "co_booted", "name_family"]
OutageKind = Literal["gateway", "cascade", "shared_neighbor", "site", "single"]

_TRAILING_NUMBER = re.compile(r"[\W_]*\d+$")


@dataclass(frozen=True, slots=True)
class Windows:
    """How closely failures must line up in time before evidence counts."""

    chain: timedelta = timedelta(minutes=10)  # a node behind a failure may be noticed later...
    skew: timedelta = timedelta(seconds=60)  # ...or slightly earlier: reports are not in sync
    together: timedelta = timedelta(seconds=120)  # for every other kind of evidence
    boot: timedelta = timedelta(seconds=60)  # boot times this close suggest one power source


DEFAULT_WINDOWS = Windows()


@dataclass(frozen=True, slots=True, order=True)
class Evidence:
    """A reason to believe two failures share a cause. For `behind`, `a` sits behind `b`."""

    kind: str
    a: str
    b: str
    detail: str = ""


@dataclass(frozen=True)
class Situation:
    """The network at one moment: who is down since when, and the last known wiring."""

    nodes: Mapping[str, Node]
    down_since: Mapping[str, datetime]
    links: Sequence[Link] = ()


@dataclass(frozen=True)
class Outage:
    kind: OutageKind
    members: tuple[str, ...]  # ordered by the time each node went quiet
    roots: tuple[str, ...]  # members that are not behind another member
    anchor: str | None  # the node above the failure that stayed up, when there is one
    started_at: datetime
    last_drop_at: datetime
    evidence: tuple[Evidence, ...]
    dropped_at: Mapping[str, datetime] = field(default_factory=dict)  # member -> last heard from
    anchors: Mapping[str, str] = field(default_factory=dict)  # member -> first node above it still up

    @property
    def size(self) -> int:
        return len(self.members)

    @property
    def spread(self) -> timedelta:
        return self.last_drop_at - self.started_at


def name_family(name: str) -> str | None:
    """The shared part of names an operator gave as a set: "St.Georg8" -> "st.georg".

    A name without a trailing number is its own family, which lets a gateway's segments
    (all called "gw05.ext.ffmuc.net") group together. Very short families mean nothing.
    """
    family = _TRAILING_NUMBER.sub("", name.strip().lower())
    return family if len(family) >= 4 else None


def correlate(candidates: Iterable[str], situation: Situation, windows: Windows = DEFAULT_WINDOWS) -> list[Outage]:
    """Split the candidate failures into outages, oldest first."""
    down = situation.down_since
    pool = sorted({c for c in candidates if c in down and c in situation.nodes})
    if not pool:
        return []
    anchors: dict[str, str] = {}
    evidence = _behind(pool, situation, windows, anchors)
    evidence += _shared_uplink(pool, situation, windows, anchors)
    evidence += _mesh_links(pool, situation, windows)
    evidence += _co_booted(pool, situation, windows)
    evidence += _name_family(pool, situation, windows)
    gateways = {node_id for node_id, node in situation.nodes.items() if node.is_gateway}
    outages = [
        summarize(group, evidence, dropped_at=down, anchors=anchors, gateways=gateways)
        for group in _connected(pool, evidence)
    ]
    return sorted(outages, key=lambda o: (o.started_at, o.members[0]))


def summarize(
    members: Iterable[str],
    evidence: Iterable[Evidence],
    *,
    dropped_at: Mapping[str, datetime],
    anchors: Mapping[str, str],
    gateways: Collection[str],
) -> Outage:
    """Describe one group of failures: who failed first, what stayed up, and what kind of outage it is."""
    ordered = tuple(sorted(set(members), key=lambda n: (dropped_at[n], n)))
    if not ordered:
        raise ValueError("an outage needs at least one member")
    inside = set(ordered)
    relevant = tuple(sorted({e for e in evidence if e.a in inside and e.b in inside}))
    dependents = {e.a for e in relevant if e.kind == "behind"}
    roots = tuple(n for n in ordered if n not in dependents)
    root_anchors = {anchors.get(r) for r in roots}
    shared_anchor = next(iter(root_anchors)) if len(root_anchors) == 1 else None

    kind: OutageKind
    if any(n in gateways for n in ordered):
        kind, anchor = "gateway", None
    elif len(ordered) == 1:
        kind, anchor = "single", shared_anchor
    elif len(roots) == 1:
        kind, anchor = "cascade", shared_anchor
    elif shared_anchor and shared_anchor not in gateways:
        kind, anchor = "shared_neighbor", shared_anchor
    else:
        kind, anchor = "site", shared_anchor
    return Outage(
        kind=kind,
        members=ordered,
        roots=roots,
        anchor=anchor,
        started_at=dropped_at[ordered[0]],
        last_drop_at=max(dropped_at[n] for n in ordered),
        evidence=relevant,
        dropped_at={n: dropped_at[n] for n in ordered},
        anchors={n: anchors[n] for n in ordered if n in anchors},
    )


def _behind(pool: Sequence[str], situation: Situation, windows: Windows, anchors: dict[str, str]) -> list[Evidence]:
    """Walk each failed node's path upward until it reaches a node that was still up."""
    in_pool = set(pool)
    found: list[Evidence] = []
    for node_id in pool:
        dropped = situation.down_since[node_id]
        below = node_id
        for hop in walk_upstream(situation.nodes, node_id):
            hop_dropped = situation.down_since.get(hop.id)
            if hop_dropped is None:
                anchors[node_id] = hop.id  # still up: the failure ends here
                break
            lag = dropped - hop_dropped
            if lag < -windows.skew:
                anchors[node_id] = hop.id  # failed later, so it was up when this node dropped
                break
            if lag > windows.chain or hop.id not in in_pool:
                break  # gone long before: this path is stale map data, not a cause
            found.append(Evidence("behind", below, hop.id))
            below = hop.id
    return found


def _shared_uplink(
    pool: Sequence[str], situation: Situation, windows: Windows, anchors: Mapping[str, str]
) -> list[Evidence]:
    by_anchor: dict[str, list[str]] = defaultdict(list)
    for node_id in pool:
        anchor = anchors.get(node_id)
        # Nodes with their own VPN uplink share a gateway with hundreds of others; that is not evidence.
        if anchor and not situation.nodes[anchor].is_gateway:
            by_anchor[anchor].append(node_id)
    found: list[Evidence] = []
    for anchor, members in by_anchor.items():
        found += _close_in_time(members, situation.down_since, windows.together, "shared_uplink", anchor)
    return found


def _mesh_links(pool: Sequence[str], situation: Situation, windows: Windows) -> list[Evidence]:
    in_pool = set(pool)
    down = situation.down_since
    found: set[Evidence] = set()
    for link in situation.links:
        if link.kind == "vpn" or link.a not in in_pool or link.b not in in_pool:
            continue
        if abs(down[link.a] - down[link.b]) <= windows.together:
            a, b = sorted((link.a, link.b))
            found.add(Evidence("mesh_link", a, b, link.kind))
    return sorted(found)


def _co_booted(pool: Sequence[str], situation: Situation, windows: Windows) -> list[Evidence]:
    down = situation.down_since
    booted = sorted(
        (node.booted_at, node.id) for node in (situation.nodes[n] for n in pool) if node.booted_at is not None
    )
    found: list[Evidence] = []
    for i, (boot_a, a) in enumerate(booted):
        for boot_b, b in booted[i + 1 :]:
            if boot_b - boot_a > windows.boot:
                break
            if abs(down[a] - down[b]) <= windows.together:
                first, second = sorted((a, b))
                found.append(Evidence("co_booted", first, second, boot_a.isoformat(timespec="minutes")))
    return found


def _name_family(pool: Sequence[str], situation: Situation, windows: Windows) -> list[Evidence]:
    families: dict[str, list[str]] = defaultdict(list)
    for node_id in pool:
        family = name_family(situation.nodes[node_id].name)
        if family:
            families[family].append(node_id)
    found: list[Evidence] = []
    for family, members in families.items():
        found += _close_in_time(members, situation.down_since, windows.together, "name_family", family)
    return found


def _close_in_time(
    members: Sequence[str],
    down: Mapping[str, datetime],
    window: timedelta,
    kind: str,
    detail: str,
) -> list[Evidence]:
    """Link members that failed within `window` of the previous one (single-linkage in time)."""
    ordered = sorted(members, key=lambda n: (down[n], n))
    return [Evidence(kind, min(a, b), max(a, b), detail) for a, b in pairwise(ordered) if down[b] - down[a] <= window]


def _connected(pool: Sequence[str], evidence: Iterable[Evidence]) -> list[list[str]]:
    parent = {node_id: node_id for node_id in pool}

    def find(node_id: str) -> str:
        while parent[node_id] != node_id:
            parent[node_id] = parent[parent[node_id]]
            node_id = parent[node_id]
        return node_id

    for item in evidence:
        first, second = find(item.a), find(item.b)
        if first != second:
            parent[max(first, second)] = min(first, second)
    groups: dict[str, list[str]] = defaultdict(list)
    for node_id in pool:
        groups[find(node_id)].append(node_id)
    return list(groups.values())
