"""Live-run numbers from the event log: what Uplink saw, explained and sent."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from statistics import median
from typing import Any, Iterable


def build_report(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    polls: Counter[str] = Counter()
    kinds: dict[str, str] = {}
    sizes: dict[str, int] = {}
    merged: set[str] = set()
    held_back: Counter[str] = Counter()
    writers: Counter[str] = Counter()
    replies: Counter[str] = Counter()
    first: datetime | None = None
    last: datetime | None = None
    network: dict[str, Any] = {"reachable": None, "nodes": None}
    alerts = recoveries = failures = 0
    detect_delays: list[int] = []
    alert_delays: list[int] = []
    durations: list[int] = []

    for event in events:
        name = event.get("event", "")
        if name == "poll":
            polls[event.get("status", "ok")] += 1
            moment = _time(event.get("taken_at"))
            first = first or moment
            last = moment or last
            network = {"reachable": event.get("reachable"), "nodes": event.get("nodes")}
        elif name == "poll_failed":
            polls["failed"] += 1
        elif name.startswith("incident_") and "id" in event:
            incident = event["id"]
            kinds[incident] = event.get("kind", kinds.get(incident, "?"))
            sizes[incident] = max(sizes.get(incident, 0), event.get("size", 0))
            if name == "incident_opened":
                detect_delays.append(event.get("delay_s", 0))
            elif name == "incident_resolved":
                durations.append(event.get("minutes", 0))
            elif name == "incident_suppressed":
                held_back[event.get("detail", "?")] += 1
            elif name == "incident_merged":
                merged.add(incident)
        elif name == "alert_sent" and event.get("notice") == "alert":
            alerts += 1
            writers[event.get("source", "?")] += 1
            alert_delays.append(event.get("delay_s", 0))
        elif name == "alert_sent" and event.get("notice") == "resolved":
            recoveries += 1
        elif name == "alert_failed":
            failures += 1
        elif name == "reply_sent":
            replies[event.get("kind", "?")] += 1

    total_polls = sum(polls.values())
    real = [i for i in sizes if i not in merged]
    nodes_failed = sum(sizes[i] for i in real)
    return {
        "watching_hours": round((last - first).total_seconds() / 3600, 1) if first and last else 0.0,
        "first_poll": first.isoformat() if first else None,
        "last_poll": last.isoformat() if last else None,
        "polls": dict(polls),
        "feed_availability_pct": round(100 * (total_polls - polls["failed"]) / total_polls, 1) if total_polls else None,
        "network": network,
        "incidents": {
            "total": len(real),
            "with_several_nodes": sum(1 for i in real if sizes[i] > 1),
            "largest": max((sizes[i] for i in real), default=0),
            "by_kind": dict(Counter(kinds[i] for i in real)),
            "resolved": len(durations),
            "median_minutes": _median(durations),
            "median_seconds_to_detect": _median(detect_delays),
        },
        "alerts": {
            "sent": alerts,
            "recoveries_sent": recoveries,
            "written_by": dict(writers),
            "median_seconds_from_failure": _median(alert_delays),
            "held_back": dict(held_back),
            "failed": failures,
        },
        "noise": {
            "nodes_failed": nodes_failed,
            "alerts_sent": alerts,
            "percent_fewer": percent_fewer(alerts, nodes_failed),
        },
        "replies": dict(replies),
    }


def format_report(report: dict[str, Any]) -> str:
    incidents, alerts, network, noise = report["incidents"], report["alerts"], report["network"], report["noise"]
    polls = sum(report["polls"].values())
    lines = [
        f"Watching          {report['watching_hours']} h, {polls} polls, feed available {report['feed_availability_pct']}%",
        f"Network now       {network['reachable']} of {network['nodes']} nodes reachable",
        f"Incidents         {incidents['total']} ({incidents['with_several_nodes']} involving several nodes, largest {incidents['largest']})",
        f"  by kind         {_pairs(incidents['by_kind'])}",
        f"  resolved        {incidents['resolved']}, median {incidents['median_minutes']} min",
        f"  noticed         median {incidents['median_seconds_to_detect']} s after the first node went quiet",
        f"Alerts            {alerts['sent']} sent (+{alerts['recoveries_sent']} recoveries), written by {_pairs(alerts['written_by'])}",
        f"  arrived         median {alerts['median_seconds_from_failure']} s after the failure began",
        f"  held back       {_pairs(alerts['held_back'])}",
        f"  failed          {alerts['failed']}",
        f"Noise: {_count(noise['nodes_failed'], 'node')} failed, {_count(noise['alerts_sent'], 'alert')} sent "
        f"({noise['percent_fewer']}% fewer)",
        f"Questions         {_pairs(report['replies'])}",
    ]
    return "\n".join(lines)


def percent_fewer(alerts: int, nodes: int) -> int:
    """How much smaller the alert count is than the number of nodes that failed, in whole percent.

    0 when no node failed, and never negative.
    """
    if nodes <= 0:
        return 0
    return max(0, round(100 * (nodes - alerts) / nodes))


def _count(number: int, word: str) -> str:
    return f"{number} {word}{'' if number == 1 else 's'}"


def _pairs(counts: dict[str, int]) -> str:
    return ", ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))) or "none"


def _median(values: list[int]) -> int | None:
    return round(median(values)) if values else None


def _time(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text) if text else None
