"""The mesh data model, the meshviewer.json reader and a compact storage format.

Only the fields Uplink needs are read. Contact details that community maps publish
(`owner`, `contact`) are never read, so they cannot reach logs, prompts or messages.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Iterator, Mapping

COMPACT_VERSION = 1
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_MAX_NAME = 64


class FeedFormatError(ValueError):
    """The document is not a meshviewer snapshot Uplink understands."""


@dataclass(frozen=True, slots=True)
class Node:
    id: str
    name: str
    online: bool
    last_seen: datetime | None
    is_gateway: bool = False
    clients: int = 0
    gateway: str | None = None
    next_hop: str | None = None
    booted_at: datetime | None = None
    firmware: str | None = None
    model: str | None = None


@dataclass(frozen=True, slots=True)
class Link:
    a: str
    b: str
    kind: str
    quality: float | None = None


@dataclass(frozen=True)
class Snapshot:
    """The whole network at one moment, as published by the community map."""

    taken_at: datetime
    nodes: Mapping[str, Node]
    links: tuple[Link, ...] = ()

    def is_reachable(self, node_id: str, stale_after: timedelta | None = None) -> bool:
        """True if the node is online and, when `stale_after` is given, was heard from recently.

        The map keeps a silent node "online" for about ten minutes. Checking how long ago it
        last answered lets Uplink notice a failure several minutes before the map does.
        """
        node = self.nodes.get(node_id)
        if node is None or not node.online:
            return False
        if stale_after is None or node.last_seen is None:
            return True
        return self.taken_at - node.last_seen <= stale_after

    def down_since(self, stale_after: timedelta | None = None) -> dict[str, datetime]:
        """Every node that is down right now, mapped to the moment it was last heard from."""
        return {
            node_id: node.last_seen
            for node_id, node in self.nodes.items()
            if node.last_seen is not None and not self.is_reachable(node_id, stale_after)
        }

    def reachable_count(self, stale_after: timedelta | None = None) -> int:
        return sum(1 for node_id in self.nodes if self.is_reachable(node_id, stale_after))

    def clients_online(self, stale_after: timedelta | None = None) -> int:
        return sum(n.clients for n in self.nodes.values() if self.is_reachable(n.id, stale_after))


def walk_upstream(nodes: Mapping[str, Node], start: str, max_hops: int = 32) -> Iterator[Node]:
    """Yield the nodes on `start`'s path to the internet, nearest first.

    The walk ends after a gateway, at a hop the map does not know, and on loops, which
    appear when routes change between two reports.
    """
    seen = {start}
    current = nodes.get(start)
    for _ in range(max_hops):
        if current is None or current.is_gateway:
            return
        hop_id = current.next_hop
        if not hop_id or hop_id in seen:
            return
        hop = nodes.get(hop_id)
        if hop is None:
            return
        seen.add(hop_id)
        yield hop
        current = hop


@dataclass(frozen=True, slots=True)
class DependencyPoint:
    node: Node
    nodes_behind: int
    clients_behind: int


def dependency_points(
    snapshot: Snapshot, stale_after: timedelta | None = None, limit: int = 5
) -> list[DependencyPoint]:
    """Reachable non-gateway nodes that other nodes depend on, busiest first."""
    behind: Counter[str] = Counter()
    clients: Counter[str] = Counter()
    for node in snapshot.nodes.values():
        if not snapshot.is_reachable(node.id, stale_after):
            continue
        for hop in walk_upstream(snapshot.nodes, node.id):
            if hop.is_gateway:
                break
            behind[hop.id] += 1
            clients[hop.id] += node.clients
    ranked = sorted(behind, key=lambda node_id: (-behind[node_id], -clients[node_id], node_id))
    return [DependencyPoint(snapshot.nodes[node_id], behind[node_id], clients[node_id]) for node_id in ranked[:limit]]


def parse_time(value: Any) -> datetime | None:
    """Parse the map's ISO timestamps ("2026-10-22T16:15:36+0200"); None if absent or invalid."""
    if not isinstance(value, str) or not value.strip():
        return None
    return _parse_time_text(value.strip())


@lru_cache(maxsize=32768)
def _parse_time_text(text: str) -> datetime | None:
    # Boot times repeat from poll to poll, so the cache answers most lookups.
    try:
        moment: datetime | None = datetime.fromisoformat(text)
    except ValueError:  # Python 3.10 reads only the isoformat() style; fall back for "+0200" and "Z"
        moment = None
        for pattern in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
            try:
                moment = datetime.strptime(text, pattern)
                break
            except ValueError:
                pass
    if moment is not None and moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def parse_meshviewer(document: Any) -> Snapshot:
    """Build a Snapshot from a meshviewer.json document (the format behind most Freifunk maps)."""
    if not isinstance(document, dict):
        raise FeedFormatError("expected a JSON object")
    taken_at = parse_time(document.get("timestamp"))
    raw_nodes = document.get("nodes")
    if taken_at is None or not isinstance(raw_nodes, list):
        raise FeedFormatError("no 'timestamp' or 'nodes' found; is this a meshviewer.json file?")
    nodes: dict[str, Node] = {}
    for raw in raw_nodes:
        node = _parse_node(raw)
        if node is not None:
            nodes[node.id] = node
    raw_links = document.get("links")
    links = tuple(
        link for link in map(_parse_link, raw_links if isinstance(raw_links, list) else ()) if link is not None
    )
    return Snapshot(taken_at=taken_at, nodes=nodes, links=links)


def _parse_node(raw: Any) -> Node | None:
    if not isinstance(raw, dict):
        return None
    node_id = _text(raw.get("node_id"))
    if not node_id:
        return None
    firmware = raw.get("firmware")
    return Node(
        id=node_id,
        name=(_text(raw.get("hostname")) or node_id)[:_MAX_NAME],
        online=raw.get("is_online") is True,
        last_seen=parse_time(raw.get("lastseen")),
        is_gateway=raw.get("is_gateway") is True,
        clients=_count(raw.get("clients")),
        gateway=_text(raw.get("gateway")),
        next_hop=_text(raw.get("gateway_nexthop")),
        booted_at=parse_time(raw.get("uptime")),
        firmware=_text(firmware.get("release")) if isinstance(firmware, dict) else None,
        model=_text(raw.get("model")),
    )


def _parse_link(raw: Any) -> Link | None:
    if not isinstance(raw, dict):
        return None
    a, b = _text(raw.get("source")), _text(raw.get("target"))
    if not a or not b or a == b:
        return None
    qualities = [
        float(value)
        for value in (raw.get("source_tq"), raw.get("target_tq"))
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]
    return Link(a=a, b=b, kind=_text(raw.get("type")) or "unknown", quality=min(qualities, default=None))


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = _CONTROL_CHARS.sub("", value).strip()
    return cleaned or None


def _count(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return 0


# Compact format: about a tenth of the size of meshviewer.json, enough to resume after a
# restart and to replay recordings. Positional rows keep it small; times are epoch seconds.


def to_compact(snapshot: Snapshot) -> dict[str, Any]:
    return {
        "v": COMPACT_VERSION,
        "t": snapshot.taken_at.isoformat(),
        "n": [
            [
                n.id,
                n.name,
                int(n.online),
                _epoch(n.last_seen),
                int(n.is_gateway),
                n.clients,
                n.gateway,
                n.next_hop,
                _epoch(n.booted_at),
                n.firmware,
                n.model,
            ]
            for n in snapshot.nodes.values()
        ],
        "l": [[link.a, link.b, link.kind, link.quality] for link in snapshot.links],
    }


def from_compact(document: Mapping[str, Any]) -> Snapshot:
    if document.get("v") != COMPACT_VERSION:
        raise FeedFormatError(f"unsupported compact snapshot version: {document.get('v')!r}")
    try:
        taken_at = datetime.fromisoformat(document["t"])
        tz = taken_at.tzinfo or timezone.utc
        nodes = {}
        for row in document["n"]:
            node_id, name, online, seen, gateway_flag, clients, gateway, hop, booted, firmware, model = row
            nodes[node_id] = Node(
                id=node_id,
                name=name,
                online=bool(online),
                last_seen=_from_epoch(seen, tz),
                is_gateway=bool(gateway_flag),
                clients=clients,
                gateway=gateway,
                next_hop=hop,
                booted_at=_from_epoch(booted, tz),
                firmware=firmware,
                model=model,
            )
        links = tuple(Link(a, b, kind, quality) for a, b, kind, quality in document.get("l", ()))
    except (KeyError, TypeError, ValueError) as exc:
        raise FeedFormatError(f"damaged compact snapshot: {exc}") from None
    return Snapshot(taken_at=taken_at, nodes=nodes, links=links)


def _epoch(moment: datetime | None) -> int | None:
    return None if moment is None else int(moment.timestamp())


def _from_epoch(seconds: int | None, tz: Any) -> datetime | None:
    return None if seconds is None else datetime.fromtimestamp(seconds, tz)
