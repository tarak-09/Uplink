"""Build the trimmed, pseudonymized Munich fixture that the tests use.

    python3 scripts/make_fixture.py FULL_MESHVIEWER_JSON tests/fixtures/munich-2026-10-23.json

Keeps every node that went quiet in the 24 hours before the snapshot, every node on the
paths above them, the gateways, and a fixed sample of healthy nodes. Drops owner, contact,
address and location fields. Renames routers while keeping name families intact
("St.Georg8" becomes "site-ab8"), so name-based evidence still works without publishing
anyone's router name. Gateways keep their public names.
"""

from __future__ import annotations

import json
import random
import re
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from uplink.correlate import name_family
from uplink.mesh import parse_meshviewer, walk_upstream

KEEP_FIELDS = (
    "node_id",
    "hostname",
    "is_online",
    "is_gateway",
    "lastseen",
    "firstseen",
    "uptime",
    "clients",
    "clients_wifi24",
    "clients_wifi5",
    "clients_other",
    "gateway",
    "gateway_nexthop",
    "model",
    "firmware",
    "autoupdater",
    "domain",
)
HEALTHY_SAMPLE = 300
TRAILING_NUMBER = re.compile(r"([\W_]*\d+)$")


def main(source: Path, target: Path) -> None:
    text = source.read_text(encoding="utf-8").strip()
    if text.startswith("```"):  # a file pasted from a chat
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    document = json.loads(text)
    snapshot = parse_meshviewer(document)

    down = snapshot.down_since(timedelta(seconds=180))
    keep = {n for n, t in down.items() if snapshot.taken_at - t <= timedelta(hours=24)}
    for node_id in list(keep):
        keep.update(hop.id for hop in walk_upstream(snapshot.nodes, node_id))
    keep.update(n.gateway for n in snapshot.nodes.values() if n.id in keep and n.gateway in snapshot.nodes)
    healthy = sorted(set(snapshot.nodes) - keep)
    keep.update(random.Random(2026).sample(healthy, min(HEALTHY_SAMPLE, len(healthy))))

    names = pseudonyms(snapshot.nodes[n] for n in sorted(keep))
    nodes = []
    for raw in document["nodes"]:
        if raw.get("node_id") not in keep:
            continue
        node = {field: raw[field] for field in KEEP_FIELDS if field in raw}
        node["hostname"] = names[node["node_id"]]
        if isinstance(node.get("firmware"), dict):
            node["firmware"] = {k: v for k, v in node["firmware"].items() if k in ("base", "release")}
        nodes.append(node)
    links = [link for link in document.get("links", ()) if link.get("source") in keep and link.get("target") in keep]
    links = [{k: v for k, v in link.items() if k not in ("source_addr", "target_addr")} for link in links]

    target.parent.mkdir(parents=True, exist_ok=True)
    output = {"timestamp": document["timestamp"], "nodes": nodes, "links": links}
    target.write_text(json.dumps(output, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"wrote {target}: {len(nodes)} nodes, {len(links)} links, {target.stat().st_size // 1024} KB")


def pseudonyms(nodes) -> dict[str, str]:
    """Stable fake names that keep families: same family in, same family out."""
    tags: dict[str, str] = {}
    names: dict[str, str] = {}
    for node in nodes:
        if node.is_gateway:
            names[node.id] = node.name
            continue
        family = name_family(node.name)
        if family is None:
            names[node.id] = f"solo-{letters(len(names))}"
            continue
        tag = tags.setdefault(family, f"site-{letters(len(tags))}")
        match = TRAILING_NUMBER.search(node.name)
        names[node.id] = tag + (match.group(1) if match else "")
    return names


def letters(index: int) -> str:
    out = ""
    index += 26 * 27  # always three letters: "aba", "abb", ...
    while index:
        index, rest = divmod(index, 26)
        out = chr(ord("a") + rest) + out
    return out


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(Path(sys.argv[1]), Path(sys.argv[2]))
