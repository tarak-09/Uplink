"""Incidents: outages followed from poll to poll, from first sighting to recovery.

The correlator explains one moment. The tracker remembers: it merges failures that keep
arriving into the incident they belong to, notices when nodes come back, decides when an
incident deserves a message, and keeps the history that tells routine from unusual.

All times here are feed times (the map's own timestamps), so replays behave exactly like
live runs.
"""

from __future__ import annotations

from collections import ChainMap, deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Literal, Mapping

from .correlate import (
    DEFAULT_WINDOWS,
    Evidence,
    Outage,
    Situation,
    Windows,
    correlate,
    summarize,
)
from .mesh import Node, Snapshot, walk_upstream

NoticeKind = Literal["alert", "resolved"]
ChangeKind = Literal["opened", "grew", "merged", "resolved", "expired", "suppressed"]

MAX_SEND_ATTEMPTS = 3
ROUTINE_TOLERANCE = timedelta(minutes=45)
ROUTINE_DAYS = 7
ROUTINE_MIN_REPEATS = 2


@dataclass
class Member:
    id: str
    name: str
    dropped_at: datetime
    clients: int = 0
    firmware: str | None = None
    back_at: datetime | None = None
    firmware_after: str | None = None

    @property
    def is_back(self) -> bool:
        return self.back_at is not None


@dataclass
class Incident:
    id: str
    detected_at: datetime
    members: dict[str, Member]
    evidence: set[Evidence] = field(default_factory=set)
    anchors: dict[str, str] = field(default_factory=dict)
    gateway_ids: set[str] = field(default_factory=set)
    behind_before: set[str] = field(default_factory=set)  # nodes that routed through a root before it failed
    users_known: bool = True
    alerted_at: datetime | None = None
    alert_ref: str | None = None
    send_attempts: int = 0
    suppressed: str | None = None
    resolved_at: datetime | None = None
    closed_reason: str | None = None
    outage: Outage = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.refresh()

    def refresh(self) -> None:
        self.outage = summarize(
            self.members,
            self.evidence,
            dropped_at={m.id: m.dropped_at for m in self.members.values()},
            anchors=self.anchors,
            gateways=self.gateway_ids,
        )

    @property
    def kind(self) -> str:
        return self.outage.kind

    @property
    def roots(self) -> tuple[str, ...]:
        return self.outage.roots

    @property
    def anchor(self) -> str | None:
        return self.outage.anchor

    @property
    def started_at(self) -> datetime:
        return self.outage.started_at

    @property
    def size(self) -> int:
        return len(self.members)

    @property
    def users(self) -> int:
        return sum(m.clients for m in self.members.values())

    @property
    def is_open(self) -> bool:
        return self.closed_reason is None

    @classmethod
    def from_outage(cls, outage: Outage, nodes: Mapping[str, Node], *, detected_at: datetime) -> Incident:
        """An incident seen in a single snapshot, where client counts of dead nodes are unknown."""
        members = {
            node_id: Member(node_id, nodes[node_id].name, outage.dropped_at[node_id], firmware=nodes[node_id].firmware)
            for node_id in outage.members
        }
        return cls(
            id=_incident_id(outage),
            detected_at=detected_at,
            members=members,
            evidence=set(outage.evidence),
            anchors=dict(outage.anchors),
            gateway_ids={
                n for n in (*outage.members, *outage.anchors.values()) if nodes.get(n) and nodes[n].is_gateway
            },
            users_known=False,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "detected_at": _iso(self.detected_at),
            "members": [
                {
                    "id": m.id,
                    "name": m.name,
                    "dropped_at": _iso(m.dropped_at),
                    "clients": m.clients,
                    "firmware": m.firmware,
                    "back_at": _iso(m.back_at),
                    "firmware_after": m.firmware_after,
                }
                for m in self.members.values()
            ],
            "evidence": [[e.kind, e.a, e.b, e.detail] for e in sorted(self.evidence)],
            "anchors": self.anchors,
            "gateway_ids": sorted(self.gateway_ids),
            "behind_before": sorted(self.behind_before),
            "users_known": self.users_known,
            "alerted_at": _iso(self.alerted_at),
            "alert_ref": self.alert_ref,
            "send_attempts": self.send_attempts,
            "suppressed": self.suppressed,
            "resolved_at": _iso(self.resolved_at),
            "closed_reason": self.closed_reason,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Incident:
        members = {
            m["id"]: Member(
                id=m["id"],
                name=m["name"],
                dropped_at=_time(m["dropped_at"]),
                clients=m.get("clients", 0),
                firmware=m.get("firmware"),
                back_at=_maybe_time(m.get("back_at")),
                firmware_after=m.get("firmware_after"),
            )
            for m in data["members"]
        }
        return cls(
            id=data["id"],
            detected_at=_time(data["detected_at"]),
            members=members,
            evidence={Evidence(*row) for row in data.get("evidence", ())},
            anchors=dict(data.get("anchors", {})),
            gateway_ids=set(data.get("gateway_ids", ())),
            behind_before=set(data.get("behind_before", ())),
            users_known=data.get("users_known", True),
            alerted_at=_maybe_time(data.get("alerted_at")),
            alert_ref=data.get("alert_ref"),
            send_attempts=data.get("send_attempts", 0),
            suppressed=data.get("suppressed"),
            resolved_at=_maybe_time(data.get("resolved_at")),
            closed_reason=data.get("closed_reason"),
        )


@dataclass(frozen=True)
class Notice:
    kind: NoticeKind
    incident: Incident


@dataclass(frozen=True)
class Change:
    kind: ChangeKind
    incident: Incident
    detail: str = ""


@dataclass(frozen=True)
class Update:
    notices: list[Notice]
    changes: list[Change]


@dataclass(frozen=True)
class Policy:
    """When an incident deserves a message."""

    min_nodes: int = 2
    min_users: int = 15
    confirm: timedelta = timedelta(seconds=60)  # it must still be there one poll later
    alerts_per_hour: int = 6
    watch: frozenset[str] = frozenset()  # node ids or names (lower case) that always alert
    settle: timedelta = timedelta(minutes=5)  # stragglers' grace period once the roots are back
    lookback: timedelta = timedelta(hours=2)  # how old a failure can be and still join an incident
    expire_after: timedelta = timedelta(hours=48)


DEFAULT_POLICY = Policy()


class IncidentTracker:
    def __init__(
        self,
        policy: Policy = DEFAULT_POLICY,
        *,
        windows: Windows = DEFAULT_WINDOWS,
        stale_after: timedelta | None = timedelta(seconds=180),
    ) -> None:
        self.policy = policy
        self.windows = windows
        self.stale_after = stale_after
        self.open: dict[str, Incident] = {}
        self.closed: deque[Incident] = deque(maxlen=20)
        self.watching_since: datetime | None = None
        self._explained: dict[str, datetime] = {}  # node -> failure already covered by a closed incident
        self._history: dict[str, list[datetime]] = {}  # root node -> when its past outages started
        self._alert_times: deque[datetime] = deque()
        self._changes: list[Change] = []

    def start(self, at: datetime) -> None:
        """Begin watching. Failures from before this moment never open incidents."""
        self.watching_since = at

    def resume(self, at: datetime) -> None:
        """Continue after a gap in the data; failures from inside the gap are too old to report."""
        self.watching_since = at

    def update(self, before: Snapshot, now: Snapshot) -> Update:
        if self.watching_since is None:
            self.start(before.taken_at)
        self._changes = []
        down = now.down_since(self.stale_after)
        self._note_returns(now, down)
        situation = Situation(
            nodes=ChainMap(dict(now.nodes), dict(before.nodes)),
            down_since=down,
            links=before.links + now.links,  # a dead node's links vanish from the newer snapshot
        )
        for outage in correlate(self._candidates(before, now, down), situation, self.windows):
            self._absorb(outage, before, now, situation)
        notices = self._resolve(now)
        notices += self._due_alerts(now)
        self._expire(now)
        return Update(notices=notices, changes=self._changes)

    def mark_sent(self, notice: Notice, ref: str, at: datetime) -> None:
        if notice.kind == "alert":
            notice.incident.alerted_at = at
            notice.incident.alert_ref = ref
            self._alert_times.append(at)

    def mark_failed(self, notice: Notice) -> None:
        incident = notice.incident
        if notice.kind == "alert":
            incident.send_attempts += 1
            if incident.send_attempts >= MAX_SEND_ATTEMPTS:
                incident.suppressed = "send_failed"

    # -- one poll ---------------------------------------------------------------------------

    def _candidates(self, before: Snapshot, now: Snapshot, down: Mapping[str, datetime]) -> set[str]:
        """Failures worth explaining: new since watching began, or still part of an open incident.

        A silent node counts as down only after `stale_after`, so its last-heard time can
        predate the start of watching; seeing it go from reachable to down is enough.
        """
        assert self.watching_since is not None
        floor = max(self.watching_since, now.taken_at - self.policy.lookback) - self.windows.skew
        continuous = before.taken_at >= self.watching_since  # no gap between the two snapshots
        fresh = {
            node_id
            for node_id, dropped in down.items()
            if self._explained.get(node_id) != dropped
            and (dropped >= floor or (continuous and before.is_reachable(node_id, self.stale_after)))
        }
        owned = {m for incident in self.open.values() for m in incident.members if m in down}
        return fresh | owned

    def _note_returns(self, now: Snapshot, down: Mapping[str, datetime]) -> None:
        for incident in self.open.values():
            for member in incident.members.values():
                if member.id in down or member.id not in now.nodes:
                    member.back_at = None  # still down, or failed again after coming back
                elif member.back_at is None and now.is_reachable(member.id, self.stale_after):
                    member.back_at = now.taken_at
                    member.firmware_after = now.nodes[member.id].firmware

    def _absorb(self, outage: Outage, before: Snapshot, now: Snapshot, situation: Situation) -> None:
        ids = set(outage.members)
        touching = [incident for incident in self.open.values() if ids & incident.members.keys()]
        incident = self._merge(touching) if touching else None
        known = incident.members.keys() if incident else set()
        arrivals = {
            node_id: _member(node_id, before, now, situation.down_since[node_id]) for node_id in sorted(ids - known)
        }
        if incident is None:
            incident = Incident(id=self._new_id(outage), detected_at=now.taken_at, members=arrivals)
        elif not arrivals and set(outage.evidence) <= incident.evidence:
            return
        else:
            incident.members.update(arrivals)
        incident.evidence |= set(outage.evidence)
        incident.anchors.update(outage.anchors)
        incident.gateway_ids |= {
            n for n in (*ids, *outage.anchors.values()) if n in situation.nodes and situation.nodes[n].is_gateway
        }
        incident.refresh()
        incident.behind_before |= _routed_through(incident.roots, before) - incident.members.keys()
        if incident.id not in self.open:
            self.open[incident.id] = incident
            self._changes.append(Change("opened", incident))
        elif arrivals:
            self._changes.append(Change("grew", incident, f"+{len(arrivals)}"))

    def _merge(self, incidents: list[Incident]) -> Incident:
        """Fold incidents that turn out to be one; incidents grow and merge but never split."""
        survivor = min(incidents, key=lambda i: (i.alerted_at is None, i.detected_at, i.id))
        for other in incidents:
            if other is survivor:
                continue
            for node_id, member in other.members.items():
                survivor.members.setdefault(node_id, member)
            survivor.evidence |= other.evidence
            survivor.anchors.update(other.anchors)
            survivor.gateway_ids |= other.gateway_ids
            survivor.behind_before |= other.behind_before
            del self.open[other.id]
            other.closed_reason = f"merged into {survivor.id}"
            self._changes.append(Change("merged", other, survivor.id))
        survivor.refresh()
        return survivor

    def _resolve(self, now: Snapshot) -> list[Notice]:
        notices: list[Notice] = []
        for incident in list(self.open.values()):
            roots = [incident.members[r] for r in incident.roots]
            if not all(root.is_back for root in roots):
                continue
            everyone_back = all(m.is_back for m in incident.members.values())
            roots_back_for = now.taken_at - max(root.back_at for root in roots if root.back_at)
            if everyone_back or roots_back_for >= self.policy.settle:
                incident.resolved_at = now.taken_at
                self._close(incident, "resolved")
                if incident.alerted_at:
                    notices.append(Notice("resolved", incident))
        return notices

    def _due_alerts(self, now: Snapshot) -> list[Notice]:
        notices: list[Notice] = []
        for incident in sorted(self.open.values(), key=lambda i: (i.started_at, i.id)):
            if incident.alerted_at or incident.suppressed or not self._worth_alerting(incident):
                continue
            if now.taken_at - incident.detected_at < self.policy.confirm:
                continue
            if self._is_routine(incident):
                self._suppress(incident, "routine")
            elif self._sent_last_hour(now.taken_at) + len(notices) >= self.policy.alerts_per_hour:
                self._suppress(incident, "rate_limited")
            else:
                notices.append(Notice("alert", incident))
        return notices

    def _expire(self, now: Snapshot) -> None:
        for incident in list(self.open.values()):
            if now.taken_at - incident.started_at > self.policy.expire_after:
                self._close(incident, "expired")
        cutoff = now.taken_at - self.policy.lookback - timedelta(hours=1)
        self._explained = {n: t for n, t in self._explained.items() if t >= cutoff}

    # -- policy -----------------------------------------------------------------------------

    def _worth_alerting(self, incident: Incident) -> bool:
        if incident.kind == "gateway" or self._is_watched(incident):
            return True
        if incident.users_known and incident.users >= self.policy.min_users:
            return True
        return incident.kind != "single" and incident.size >= self.policy.min_nodes

    def _is_watched(self, incident: Incident) -> bool:
        watch = self.policy.watch
        return any(m.id.lower() in watch or m.name.lower() in watch for m in incident.members.values())

    def _is_routine(self, incident: Incident) -> bool:
        """Did the same node fail at about this time of day on several recent days?

        A café that switches its router off every night is not news.
        """
        start = incident.started_at
        for root in incident.roots:
            days: set[date] = set()
            for past in self._history.get(root, ()):
                past = past.astimezone(start.tzinfo)
                age_days = (start.date() - past.date()).days
                if 1 <= age_days <= ROUTINE_DAYS and _time_of_day_gap(past, start) <= ROUTINE_TOLERANCE:
                    days.add(past.date())
            if len(days) >= ROUTINE_MIN_REPEATS:
                return True
        return False

    def _sent_last_hour(self, at: datetime) -> int:
        while self._alert_times and at - self._alert_times[0] > timedelta(hours=1):
            self._alert_times.popleft()
        return len(self._alert_times)

    def _suppress(self, incident: Incident, reason: str) -> None:
        incident.suppressed = reason
        self._changes.append(Change("suppressed", incident, reason))

    def _close(self, incident: Incident, reason: Literal["resolved", "expired"]) -> None:
        incident.closed_reason = reason
        del self.open[incident.id]
        self.closed.append(incident)
        for member in incident.members.values():
            self._explained[member.id] = member.dropped_at
        self._remember_start(incident)
        self._changes.append(Change(reason, incident))

    def _remember_start(self, incident: Incident) -> None:
        horizon = incident.started_at - timedelta(days=ROUTINE_DAYS + 1)
        for root in incident.roots:
            starts = [s for s in self._history.get(root, ()) if s >= horizon]
            starts.append(incident.started_at)
            self._history[root] = starts[-50:]

    def _new_id(self, outage: Outage) -> str:
        base = _incident_id(outage)
        candidate, n = base, 1
        while candidate in self.open or any(i.id == candidate for i in self.closed):
            n += 1
            candidate = f"{base}-{n}"
        return candidate

    # -- persistence ------------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "watching_since": _iso(self.watching_since),
            "open": [i.to_dict() for i in self.open.values()],
            "closed": [i.to_dict() for i in self.closed],
            "explained": {n: t.isoformat() for n, t in self._explained.items()},
            "history": {n: [t.isoformat() for t in starts] for n, starts in self._history.items()},
            "alert_times": [t.isoformat() for t in self._alert_times],
        }

    def restore(self, data: Mapping[str, Any]) -> None:
        self.watching_since = _maybe_time(data.get("watching_since"))
        self.open = {i.id: i for i in map(Incident.from_dict, data.get("open", ()))}
        self.closed = deque(map(Incident.from_dict, data.get("closed", ())), maxlen=self.closed.maxlen)
        self._explained = {n: _time(t) for n, t in data.get("explained", {}).items()}
        self._history = {n: [_time(t) for t in starts] for n, starts in data.get("history", {}).items()}
        self._alert_times = deque(_time(t) for t in data.get("alert_times", ()))


def _member(node_id: str, before: Snapshot, now: Snapshot, dropped_at: datetime) -> Member:
    known = before.nodes.get(node_id) or now.nodes[node_id]  # `before` still has the pre-failure counts
    return Member(id=node_id, name=known.name, dropped_at=dropped_at, clients=known.clients, firmware=known.firmware)


def _routed_through(roots: Iterable[str], before: Snapshot) -> set[str]:
    """Nodes whose path to the internet passed through one of `roots` just before the failure."""
    targets = set(roots)
    return {
        node_id
        for node_id in before.nodes
        if node_id not in targets and any(hop.id in targets for hop in walk_upstream(before.nodes, node_id))
    }


def _incident_id(outage: Outage) -> str:
    return f"{outage.started_at:%Y%m%d-%H%M}-{outage.roots[0][-6:]}"


def _time_of_day_gap(a: datetime, b: datetime) -> timedelta:
    seconds_a = a.hour * 3600 + a.minute * 60 + a.second
    seconds_b = b.hour * 3600 + b.minute * 60 + b.second
    gap = abs(seconds_a - seconds_b)
    return timedelta(seconds=min(gap, 86400 - gap))


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.isoformat()


def _time(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _maybe_time(text: str | None) -> datetime | None:
    return None if text is None else datetime.fromisoformat(text)
