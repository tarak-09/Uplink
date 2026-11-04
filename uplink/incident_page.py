"""A one-page picture of the outages in one snapshot.

`uplink analyze --html outages.html` writes it: the day's failures on a timeline, then one
picture per outage showing what stayed online, what went quiet and when, why Uplink grouped
the failures, and what to check first. The page is a single self-contained file (inline CSS
and SVG, no scripts, nothing loaded from elsewhere), so it opens offline, prints, and can be
attached to a message. Names go through the same Namer as the alerts, so ANONYMIZE applies.
"""

from __future__ import annotations

import html
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Iterator, Sequence
from urllib.parse import urlsplit

from . import templates
from .config import Settings
from .facts import Namer, namer_for, outage_facts
from .incidents import Incident
from .mesh import Snapshot

# Outage pictures: node boxes on a grid of columns and rows.
NODE_W, NODE_H = 164, 46
COLUMN, ROW = 180, 104
PAD_X, PAD_TOP, PAD_BOTTOM = 18, 30, 16
ROW_LIMIT = 8  # boxes per row; the rest are counted under the picture instead of drawn
DEPTH_LIMIT = 4  # rows of failed nodes
NAME_CHARS = 19

# The timeline: one dot per node that went quiet.
STRIP_W, STRIP_PAD = 880, 16
DOT_R, DOT_PITCH = 3.6, 9
STACK_LIMIT, LONE_LIMIT = 12, 5

KIND_NAMES = {
    "cascade": "Cascade",
    "site": "Site outage",
    "shared_neighbor": "Shared neighbour",
    "gateway": "Gateway outage",
    "single": "Single node",
}


def render_page(
    snapshot: Snapshot,
    settings: Settings,
    incidents: Sequence[Incident],
    notable: Sequence[Incident],
    hours: float,
) -> str:
    """The page for one snapshot. `notable` is numbered in the order given, as `analyze` prints it."""
    sections = [_summary(snapshot, incidents, notable, hours)]
    if notable:
        sections.append(KEY)
        sections += [_outage(number, incident, snapshot, settings) for number, incident in enumerate(notable, 1)]
    title = f"Uplink outage report: {settings.network_name}, {snapshot.taken_at:%d/%m/%Y %H:%M}"
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="color-scheme" content="light dark">\n'
        f"<title>{_e(title)}</title>\n<style>{STYLE}</style>\n</head>\n<body>\n"
        f'<div class="page">\n{_masthead(snapshot, settings)}\n<main>\n'
        + "\n".join(sections)
        + f"\n</main>\n{_footer(snapshot, settings)}\n</div>\n</body>\n</html>\n"
    )


# --- The summary and the timeline ---------------------------------------------------------------


def _summary(snapshot: Snapshot, incidents: Sequence[Incident], notable: Sequence[Incident], hours: float) -> str:
    went_quiet = sum(i.size for i in incidents)
    span = "in the last hour" if hours == 1 else f"in the last {hours:g} hours"
    if not went_quiet:
        return f'<section class="summary"><h1>No routers went quiet {span}.</h1></section>'
    alerts = len(notable)
    together = sum(i.size for i in notable)
    alone = went_quiet - together
    headline = f"{_plural(went_quiet, 'router')} went quiet {span}."
    if not alerts:
        lede = (
            "Each went quiet on its own, which usually means someone switched a router off, so Uplink sends no alert."
        )
    else:
        lede = (
            f"{together} of them failed together, in {_plural(alerts, 'outage')} with one likely cause each. "
            f"Uplink sends one alert per outage"
        )
        if alerts < went_quiet:
            fewer = round(100 * (1 - alerts / went_quiet))
            lede += f": {_plural(alerts, 'alert')} instead of {went_quiet}, {fewer}% fewer."
        else:
            lede += "."
        if alone:
            lede += f" The other {alone} went quiet alone, which usually means someone switched a router off."
    return (
        f'<section class="summary"><h1>{_e(headline)}</h1><p class="lede">{_e(lede)}</p>'
        f"{_timeline(snapshot, incidents, notable, hours)}</section>"
    )


def _timeline(snapshot: Snapshot, incidents: Sequence[Incident], notable: Sequence[Incident], hours: float) -> str:
    """Outages as stacks of dots above the line, lone nodes below it, along the hours looked at."""
    end = snapshot.taken_at
    start = end - timedelta(hours=hours)
    span = max((end - start).total_seconds(), 1.0)
    right = STRIP_W - STRIP_PAD

    def x_at(moment: datetime) -> float:
        share = min(max((moment - start).total_seconds() / span, 0.0), 1.0)
        return STRIP_PAD + share * (right - STRIP_PAD)

    numbers = {incident.id: number for number, incident in enumerate(notable, 1)}
    tallest = min(STACK_LIMIT, max((i.size for i in notable), default=0))
    axis = 34 + tallest * DOT_PITCH
    dots: list[str] = []
    last_x = float("-inf")
    for incident in sorted(notable, key=lambda i: i.started_at):
        x = min(max(x_at(incident.started_at), last_x + 16), right)  # keep close outages apart
        last_x = x
        shown = min(incident.size, STACK_LIMIT)
        dots += [
            f'<circle class="together" cx="{x:g}" cy="{axis - 8 - k * DOT_PITCH:g}" r="{DOT_R}"/>' for k in range(shown)
        ]
        top = axis - 8 - (shown - 1) * DOT_PITCH
        dots.append(f'<text class="stack-label" x="{x:g}" y="{top - 8:g}">{numbers[incident.id]}</text>')
        if incident.size > shown:
            dots.append(f'<text class="time" x="{x + 7:g}" y="{top + 4:g}">+{incident.size - shown}</text>')

    columns: dict[int, int] = defaultdict(int)
    deepest = 0
    for incident in sorted((i for i in incidents if i.id not in numbers), key=lambda i: i.started_at):
        column = round(x_at(incident.started_at) / DOT_PITCH)
        row = columns[column]
        columns[column] += 1
        if row < LONE_LIMIT:
            deepest = max(deepest, row + 1)
            cx = min(max(column * DOT_PITCH, STRIP_PAD), right)
            dots.append(f'<circle class="alone" cx="{cx:g}" cy="{axis + 9 + row * DOT_PITCH:g}" r="{DOT_R - 0.6:g}"/>')

    labels_y = axis + 9 + max(deepest, 1) * DOT_PITCH + 12
    marks = [f'<line class="axis" x1="{STRIP_PAD}" x2="{right}" y1="{axis:g}" y2="{axis:g}"/>']
    labels = [
        f'<text class="time" x="{STRIP_PAD}" y="{labels_y:g}">{_when(start, end)}</text>',
        f'<text class="time" x="{right}" y="{labels_y:g}" text-anchor="end">{_when(end, end)}</text>',
    ]
    for moment in _ticks(start, end):
        x = x_at(moment)
        marks.append(f'<line class="axis" x1="{x:g}" x2="{x:g}" y1="{axis - 4:g}" y2="{axis + 4:g}"/>')
        if min(x - STRIP_PAD, right - x) >= 60:
            labels.append(
                f'<text class="time" x="{x:g}" y="{labels_y:g}" text-anchor="middle">{_when(moment, end)}</text>'
            )

    together = sum(i.size for i in notable)
    alone = sum(i.size for i in incidents) - together
    described = (
        f"Timeline of the last {hours:g} hours: {_plural(together, 'node')} in {_plural(len(notable), 'outage')} "
        f"above the line, {alone} that went quiet alone below it."
    )
    height = labels_y + 6
    return (
        f'<figure class="timeline"><svg viewBox="0 0 {STRIP_W} {height:g}" role="img" aria-label="{_e(described)}">'
        + "".join(marks + dots + labels)
        + "</svg>"
        f'<figcaption class="legend"><span>{SWATCH_TOGETHER}went quiet together with others: one outage, one '
        f"alert, numbered as below</span><span>{SWATCH_ALONE}went quiet alone: no alert</span></figcaption></figure>"
    )


def _ticks(start: datetime, end: datetime) -> Iterator[datetime]:
    """Whole hours between start and end, at most about six of them."""
    hours = (end - start).total_seconds() / 3600
    step = next((s for s in (1, 2, 3, 6, 12, 24, 48, 96, 168) if hours / s <= 6), 336)
    moment = start.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    while moment.hour % min(step, 24):
        moment += timedelta(hours=1)
    while moment < end:
        yield moment
        moment += timedelta(hours=step)


# --- One outage ---------------------------------------------------------------------------------


def _outage(number: int, incident: Incident, snapshot: Snapshot, settings: Settings) -> str:
    facts = outage_facts(incident, snapshot, settings)
    headline = templates.render(facts, "english").splitlines()[0].strip("*").strip()
    meta = f"{KIND_NAMES.get(incident.kind, incident.kind)} from {_when(incident.started_at, snapshot.taken_at)}."
    if incident.size > 1:
        meta += f" {incident.size} nodes went quiet {_within(incident.outage.spread)}"
        people = facts.get("people_affected")
        meta += f", cutting off {_plural(people, 'person', 'people')}." if people else "."
    why = "".join(f"<li>{_e(line)}</li>" for line in facts.get("evidence", []))
    action = str(facts["check_first"])
    return (
        f'<article class="outage" id="outage-{number}">'
        f'<header class="outage-head"><span class="number" aria-hidden="true">{number}</span><div>'
        f'<h2><span class="sr">Outage {number}: </span>{_e(headline)}</h2><p class="meta">{_e(meta)}</p></div>'
        f"</header>{_picture(incident, snapshot, namer_for(incident, snapshot, settings))}"
        '<div class="explain">'
        + (f'<section class="why"><h3>Why Uplink grouped them</h3><ul>{why}</ul></section>' if why else "")
        + f'<section class="check-first"><h3>Check first</h3><p class="action">{_e(action[:1].upper() + action[1:])}'
        f'</p><p class="cause">Likely cause: {_e(facts["likely_cause"])}.</p></section></div></article>'
    )


@dataclass(frozen=True)
class _Tree:
    """The failed nodes as drawn: who reached the internet through whom, and what stayed online above."""

    roots: list[str]  # first row, grouped by the node above them that stayed online
    children: dict[str, list[str]]  # every drawn failed node -> its drawn children
    anchors: dict[str, str]  # drawn root -> the first node above it that stayed online
    hubs: frozenset[str]  # failed nodes that others reached the internet through
    hidden: int  # failed nodes left out to keep the picture readable


def _tree(incident: Incident) -> _Tree:
    members = incident.outage.members  # in the order they went quiet
    order = {node_id: i for i, node_id in enumerate(members)}
    parent: dict[str, str] = {}
    for item in incident.evidence:  # "behind": item.a reached the internet through item.b
        if item.kind != "behind" or item.a not in order or item.b not in order or item.a == item.b:
            continue
        current = parent.get(item.a)
        if current is None or (order[item.b], item.b) < (order[current], current):
            parent[item.a] = item.b
    for node_id in members:  # an incident merged across polls can, rarely, hold a loop: cut it
        hop, seen = parent.get(node_id), set()
        while hop is not None and hop != node_id and hop not in seen:
            seen.add(hop)
            hop = parent.get(hop)
        if hop == node_id:
            del parent[node_id]

    kids: dict[str, list[str]] = defaultdict(list)
    for node_id in members:
        if node_id in parent:
            kids[parent[node_id]].append(node_id)
    roots = [n for n in members if n not in parent]
    above = {r: a for r in roots if (a := incident.anchors.get(r)) and a not in incident.members}
    rank: dict[str, int] = {}
    for root in roots:
        if root in above:
            rank.setdefault(above[root], len(rank))
    roots.sort(key=lambda r: (rank[above[r]] if r in above else len(rank), order[r]))

    drawn: dict[str, list[str]] = {}
    level, depth = roots[:ROW_LIMIT], 1
    while level:
        below: list[str] = []
        for node_id in level:
            drawn[node_id] = []
            if depth < DEPTH_LIMIT:
                for child in kids.get(node_id, []):
                    if len(below) < ROW_LIMIT:
                        drawn[node_id].append(child)
                        below.append(child)
        level, depth = below, depth + 1
    first = roots[:ROW_LIMIT]
    return _Tree(
        roots=first,
        children=drawn,
        anchors={r: above[r] for r in first if r in above},
        hubs=frozenset(kids),
        hidden=len(members) - len(drawn),
    )


def _picture(incident: Incident, snapshot: Snapshot, name: Namer) -> str:
    tree = _tree(incident)
    slot: dict[str, float] = {}
    depth: dict[str, int] = {}
    slots = 0

    def place(node_id: str, level: int) -> None:  # leaves get a column each; parents sit over their children
        nonlocal slots
        depth[node_id] = level
        children = tree.children[node_id]
        for child in children:
            place(child, level + 1)
        if children:
            slot[node_id] = (slot[children[0]] + slot[children[-1]]) / 2
        else:
            slot[node_id] = slots + 0.5
            slots += 1

    for root in tree.roots:
        place(root, 0)
    groups: dict[str, list[float]] = defaultdict(list)
    for root, anchor in tree.anchors.items():
        groups[anchor].append(slot[root])
    anchor_slot = {anchor: sum(xs) / len(xs) for anchor, xs in groups.items()}

    first_row = 1 if anchor_slot else 0
    zone = incident.kind == "site"
    width = 2 * PAD_X + slots * COLUMN
    height = PAD_TOP + (first_row + max(depth.values())) * ROW + NODE_H + PAD_BOTTOM + (24 if zone else 0)

    def cx(position: float) -> float:
        return PAD_X + position * COLUMN

    def top(level: int) -> float:
        return PAD_TOP + level * ROW

    edges: list[str] = []
    cuts: list[str] = []
    boxes: list[str] = []
    marks: list[str] = []
    reference = snapshot.taken_at
    for node_id, level in depth.items():
        x, y = cx(slot[node_id]), top(first_row + level)
        dropped = incident.members[node_id].dropped_at
        after = round((dropped - incident.started_at).total_seconds())
        heard = f"{dropped.astimezone(reference.tzinfo):%H:%M:%S}"
        full = name(node_id)
        boxes.append(
            _box(
                x,
                y,
                state="down",
                text=full,
                label=f"offline {heard}" if after < 1 else f"offline {_after(after)}",
                tooltip=f"{full}: offline, last heard {heard}",
                hub=node_id in tree.hubs,
            )
        )
        edges += [_elbow(x, y + NODE_H, cx(slot[c]), top(first_row + level + 1)) for c in tree.children[node_id]]
    for root, anchor in tree.anchors.items():
        x, y = cx(slot[root]), top(first_row)
        edges.append(_elbow(cx(anchor_slot[anchor]), top(0) + NODE_H, x, y))
        cuts.append(_cut(x, y - (ROW - NODE_H) / 4))
    for anchor, position in anchor_slot.items():
        node = snapshot.nodes.get(anchor)
        full = name(anchor)
        label = "gateway, still online" if node is not None and node.is_gateway else "still online"
        boxes.append(_box(cx(position), top(0), state="up", text=full, label=label, tooltip=f"{full}: still online"))

    target = incident.anchor if incident.kind == "shared_neighbor" else (incident.roots or (None,))[0]
    if zone:
        left = min(cx(slot[n]) for n in depth) - NODE_W / 2 - 6
        right = max(cx(slot[n]) for n in depth) + NODE_W / 2 + 6
        upper, lower = top(first_row) - 6, top(first_row + max(depth.values())) + NODE_H + 6
        marks.append(
            f'<g class="check"><rect x="{left:g}" y="{upper:g}" width="{right - left:g}" height="{lower - upper:g}" '
            f'rx="16"/><text x="{left + 4:g}" y="{lower + 17:g}">check first: this location</text></g>'
        )
    elif target in anchor_slot:
        marks.append(_ring(cx(anchor_slot[target]), top(0)))
    elif target in slot:
        marks.append(_ring(cx(slot[target]), top(first_row + depth[target])))

    described = f"{incident.size} nodes went offline"
    if anchor_slot:
        described = f"{_join(name(a) for a in anchor_slot)} stayed online; {described}"
    hubs = [name(n) for n in depth if n in tree.hubs]
    if hubs:
        described += f"; the others reached the internet through {_join(hubs)}"
    caption = f"<figcaption>Showing {incident.size - tree.hidden} of {incident.size} nodes.</figcaption>"
    under, over = (marks, []) if zone else ([], marks)  # the location outline sits behind the boxes
    return (
        f'<figure class="diagram"><svg viewBox="0 0 {width:g} {height:g}" width="{width:g}" height="{height:g}" '
        f'role="img" aria-label="{_e(described)}.">'
        + "".join(edges + cuts + under + boxes + over)
        + "</svg>"
        + (caption if tree.hidden else "")
        + "</figure>"
    )


def _box(x: float, y: float, *, state: str, text: str, label: str, tooltip: str, hub: bool = False) -> str:
    left = x - NODE_W / 2
    # Long names keep their end, where the names of a set ("Dachantenne-03") differ.
    shown = text if len(text) <= NAME_CHARS else text[: NAME_CHARS - 8] + "…" + text[-7:]
    return (
        f'<g class="node {state}{" hub" if hub else ""}"><title>{_e(tooltip)}</title>'
        f'<rect x="{left:g}" y="{y:g}" width="{NODE_W}" height="{NODE_H}" rx="{NODE_H / 2:g}"/>'
        f'<circle cx="{left + 17:g}" cy="{y + NODE_H / 2:g}" r="4.5"/>'
        f'<text class="name" x="{left + 29:g}" y="{y + 20:g}">{_e(shown)}</text>'
        f'<text class="state" x="{left + 29:g}" y="{y + 36:g}">{_e(label)}</text></g>'
    )


def _elbow(x1: float, y1: float, x2: float, y2: float) -> str:
    middle = (y1 + y2) / 2
    return f'<path class="edge" d="M{x1:g} {y1:g}V{middle:g}H{x2:g}V{y2:g}"/>'


def _cut(x: float, y: float) -> str:
    """The connection that was lost: a gap in the line with a cross over it."""
    return f'<g class="cut"><circle cx="{x:g}" cy="{y:g}" r="7"/><path d="M{x - 4.5:g} {y - 4.5:g}l9 9m0-9l-9 9"/></g>'


def _ring(x: float, y: float) -> str:
    left = x - NODE_W / 2
    return (
        f'<g class="check"><rect x="{left - 6:g}" y="{y - 6:g}" width="{NODE_W + 12}" height="{NODE_H + 12}" '
        f'rx="{NODE_H / 2 + 6:g}"/><text x="{left + NODE_W:g}" y="{y - 12:g}" text-anchor="end">check first</text></g>'
    )


# --- Page furniture -----------------------------------------------------------------------------


def _masthead(snapshot: Snapshot, settings: Settings) -> str:
    return (
        f'<header class="masthead"><p class="brand">{LOGO}Uplink</p>'
        f'<p class="source"><strong>{_e(settings.network_name)}</strong><br>'
        f"{len(snapshot.nodes):,} nodes on the map at {snapshot.taken_at:%d/%m/%Y %H:%M} "
        f"({_zone(snapshot.taken_at)})</p></header>"
    )


def _footer(snapshot: Snapshot, settings: Settings) -> str:
    host = urlsplit(settings.feed_url).hostname or "the public map"
    notes = [
        f"Made by Uplink from the public data of the {settings.network_name} map ({host}). Times are in the "
        f"map's time zone ({_zone(snapshot.taken_at)}) and show when the map last heard from each node. Nodes "
        "report about once a minute, so a few seconds apart means the same moment."
    ]
    if settings.anonymize:
        notes.append("Node names are replaced with short ids; gateways keep their names.")
    return "<footer>" + "".join(f"<p>{_e(note)}</p>" for note in notes) + "</footer>"


def _e(text: object) -> str:
    return html.escape(str(text), quote=True)


def _plural(count: int, one: str, many: str | None = None) -> str:
    return f"{count} {one if count == 1 else (many or one + 's')}"


def _join(names: Iterable[str]) -> str:
    items = list(names)
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _when(moment: datetime, reference: datetime) -> str:
    """ "16:15" on the reference day, "22/10 16:15" otherwise, in the map's time zone."""
    local = moment.astimezone(reference.tzinfo)
    return f"{local:%H:%M}" if local.date() == reference.date() else f"{local:%d/%m %H:%M}"


def _within(spread: timedelta) -> str:
    seconds = round(spread.total_seconds())
    if seconds < 1:
        return "in the same second"
    if seconds < 120:
        return f"within {_plural(seconds, 'second')}"
    return f"within {round(seconds / 60)} minutes"


def _after(seconds: int) -> str:
    minutes, rest = divmod(seconds, 60)
    if not minutes:
        return f"+{rest} s"
    return f"+{minutes} min {rest} s" if rest else f"+{minutes} min"


def _zone(moment: datetime) -> str:
    offset = moment.utcoffset()
    if not offset:
        return "UTC"
    minutes = round(offset.total_seconds() / 60)
    hours, rest = divmod(abs(minutes), 60)
    return f"UTC{'+' if minutes > 0 else '-'}{hours:02d}:{rest:02d}"


LOGO = (
    '<svg class="logo" viewBox="0 0 24 24" aria-hidden="true"><path d="M6.5 18.5 17.5 5.5"/>'
    '<circle class="low" cx="6.5" cy="18.5" r="3.4"/><circle class="high" cx="17.5" cy="5.5" r="3.4"/></svg>'
)
SWATCH_TOGETHER = (
    '<svg class="swatch" viewBox="0 0 12 12" aria-hidden="true"><circle class="together" cx="6" cy="6" r="4"/></svg>'
)
SWATCH_ALONE = (
    '<svg class="swatch" viewBox="0 0 12 12" aria-hidden="true"><circle class="alone" cx="6" cy="6" r="3.4"/></svg>'
)


def _pill(state: str) -> str:
    return (
        f'<svg class="swatch wide" viewBox="0 0 30 16" aria-hidden="true"><g class="node {state}">'
        '<rect x="1.5" y="1.5" width="27" height="13" rx="6.5"/></g></svg>'
    )


KEY = (
    '<ul class="key" aria-label="How to read the pictures">'
    f"<li>{_pill('up')}still online</li>"
    f"<li>{_pill('down')}offline, with the time the map last heard from it</li>"
    f"<li>{_pill('down hub')}others reached the internet through it</li>"
    '<li><svg class="swatch" viewBox="0 0 16 16" aria-hidden="true"><path class="edge" d="M8 0V16"/>'
    '<g class="cut"><circle cx="8" cy="8" r="6"/><path d="M4.5 4.5l7 7m0-7l-7 7"/></g></svg>connection lost</li>'
    '<li><svg class="swatch wide" viewBox="0 0 30 16" aria-hidden="true"><g class="check">'
    '<rect x="1.5" y="1.5" width="27" height="13" rx="6.5"/></g></svg>check first</li></ul>'
)

STYLE = """
:root {
  color-scheme: light dark;
  --paper: #f3f5f2; --sheet: #ffffff; --ink: #15222a; --muted: #55656e; --rule: #d6ddd9;
  --line: #98a6ad; --grid: #e1e7e3;
  --down: #c0353f; --down-fill: #fbe6e6; --up: #1b7c51; --check: #9a6200;
  --din: "Bahnschrift", "DIN Alternate", "DIN Next", "D-DIN", "Barlow", system-ui, sans-serif;
  --text: system-ui, -apple-system, "Segoe UI", Roboto, "Noto Sans", sans-serif;
}
@media (prefers-color-scheme: dark) {
  :root {
    --paper: #0f181d; --sheet: #15222a; --ink: #e3ebed; --muted: #95a6ae; --rule: #273841;
    --line: #5d717c; --grid: #1c2b33;
    --down: #f0727b; --down-fill: #3b2026; --up: #55c892; --check: #f2b54e;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--paper); color: var(--ink); font: 16px/1.55 var(--text); }
.page { max-width: 960px; margin: 0 auto; padding: 0 20px 40px; }
.masthead { display: flex; justify-content: space-between; align-items: flex-end; gap: 16px;
  padding: 22px 0 14px; border-bottom: 1px solid var(--rule); }
.masthead p { margin: 0; }
.brand { display: flex; align-items: center; gap: 9px; font: 700 22px/1 var(--din); }
.logo { width: 24px; height: 24px; }
.logo path { stroke: var(--ink); stroke-width: 2; }
.logo .low { fill: var(--ink); } .logo .high { fill: var(--up); }
.source { text-align: right; color: var(--muted); font-size: 14px; line-height: 1.45; }
.source strong { color: var(--ink); font-weight: 600; }
.summary { padding: 40px 0 4px; }
h1 { font: 600 clamp(32px, 6vw, 52px)/1.06 var(--din); letter-spacing: -0.01em; margin: 0 0 16px; max-width: 22ch; text-wrap: balance; }
.lede { font-size: 18px; max-width: 62ch; margin: 0 0 28px; }
figure { margin: 0; }
.timeline > svg { display: block; width: 100%; height: auto; }
.legend, .key { display: flex; flex-wrap: wrap; gap: 6px 24px; color: var(--muted); font-size: 14px; }
.legend { margin-top: 10px; }
.legend span, .key li { display: inline-flex; align-items: center; gap: 8px; }
.key { list-style: none; margin: 36px 0 0; padding: 14px 0; border-block: 1px solid var(--rule); }
.swatch { width: 12px; height: 12px; flex: none; }
.swatch.wide { width: 30px; height: 16px; }
.key .swatch:not(.wide) { width: 16px; height: 16px; }
.outage { padding: 40px 0 36px; }
.outage + .outage { border-top: 1px solid var(--rule); }
.outage-head { display: grid; grid-template-columns: 2.2ch 1fr; gap: 14px; align-items: start; }
.number { font: 600 42px/0.95 var(--din); }
h2 { font: 600 25px/1.22 var(--din); margin: 0 0 6px; text-wrap: balance; }
.meta { margin: 0; color: var(--muted); }
.diagram { margin: 24px 0 22px; padding: 16px; overflow-x: auto; background-color: var(--sheet);
  background-image: radial-gradient(var(--grid) 1.2px, transparent 1.4px); background-size: 18px 18px;
  border: 1px solid var(--rule); border-radius: 10px; }
.diagram > svg { display: block; margin: 0 auto; max-width: 100%; height: auto; }
.diagram figcaption { margin-top: 8px; text-align: center; color: var(--muted); font-size: 14px; }
.explain { display: grid; grid-template-columns: 1.1fr 1fr; gap: 32px; }
.explain > :only-child { grid-column: 1 / -1; }
h3 { font: 600 16px/1.3 var(--din); margin: 0 0 8px; color: var(--muted); }
.why ul { margin: 0; padding-left: 18px; }
.why li + li { margin-top: 4px; }
.check-first { border-left: 3px solid var(--check); padding: 0 0 0 16px; }
.check-first h3 { color: var(--check); }
.action { font: 600 19px/1.35 var(--din); margin: 0 0 8px; }
.cause { margin: 0; color: var(--muted); }
footer { border-top: 1px solid var(--rule); padding-top: 16px; color: var(--muted); font-size: 14px; }
footer p { margin: 0 0 6px; max-width: 80ch; }
.sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; }
.edge { fill: none; stroke: var(--line); stroke-width: 1.6; }
.node rect { stroke-width: 1.6; }
.node.up rect { fill: var(--sheet); stroke: var(--up); }
.node.up circle { fill: var(--up); }
.node.down rect { fill: var(--down-fill); stroke: var(--down); }
.node.down circle { fill: var(--down); }
.node.hub rect { stroke-width: 3.2; }
.node text { font-family: var(--din); }
.node .name { font-size: 13.5px; font-weight: 600; fill: var(--ink); }
.node .state { font-size: 12px; }
.node.up .state { fill: var(--up); }
.node.down .state { fill: var(--down); }
.cut circle { fill: var(--sheet); }
.cut path { fill: none; stroke: var(--down); stroke-width: 2.4; stroke-linecap: round; }
.check rect { fill: none; stroke: var(--check); stroke-width: 2; stroke-dasharray: 6 4; }
.check text { font: 600 12px var(--din); fill: var(--check); }
.axis { stroke: var(--ink); stroke-width: 1.2; }
.together { fill: var(--down); }
.alone { fill: none; stroke: var(--muted); stroke-width: 1.3; }
.stack-label { font: 600 13px var(--din); fill: var(--ink); text-anchor: middle; }
.time { font: 12px var(--din); fill: var(--muted); }
@media (max-width: 680px) {
  .masthead { flex-direction: column; align-items: flex-start; }
  .source { text-align: left; }
  .explain { grid-template-columns: 1fr; gap: 20px; }
}
@media print {
  body { background: #fff; }
  .diagram { background-image: none; }
  .outage { break-inside: avoid; }
}
"""
