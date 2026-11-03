"""The watch loop: fetch, compare, explain, send, remember."""

from __future__ import annotations

import logging
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Literal, Protocol, TextIO

from .config import Settings
from .facts import clock, outage_facts, resolved_facts, stale_feed_facts
from .feed import Feed, FeedError
from .incidents import Change, IncidentTracker, Notice, Policy
from .mesh import Snapshot
from .store import Store
from .whatsapp import Messenger, WhatsAppError
from .writer import Writer

log = logging.getLogger(__name__)

MAX_GAP = timedelta(minutes=15)  # longer silences restart incident detection from scratch
STALE_FEED = timedelta(minutes=15)  # the map itself has stopped updating
GLITCH_MIN_NODES = 50
GLITCH_SHARE = 0.25  # a quarter of the network vanishing at once is a monitoring problem
GLITCH_CONFIRMATIONS = 3
SERVICE_WINDOW = timedelta(hours=24)

PollStatus = Literal["baseline", "ok", "gap", "unchanged", "glitch", "failed"]


class SendError(Exception):
    pass


class Sender(Protocol):
    @property
    def needs_open_window(self) -> bool:
        """True if delivery depends on WhatsApp's 24-hour customer service window."""

    def send(self, text: str) -> str:
        """Deliver a message and return a reference for the log."""


class WhatsAppSender:
    def __init__(self, client: Messenger, to: str, *, template: str = "", template_language: str = "en") -> None:
        self.client = client
        self.to = to
        self.template = template
        self.template_language = template_language

    @property
    def needs_open_window(self) -> bool:
        return not self.template

    def send(self, text: str) -> str:
        try:
            if self.template:
                return self.client.send_template(self.to, self.template, self.template_language, (text,))
            return self.client.send_text(self.to, text)
        except WhatsAppError as exc:
            raise SendError(f"{exc}" + (f" | {exc.hint}" if exc.hint else "")) from None


class ConsoleSender:
    """Prints messages instead of sending them (dry runs, replays)."""

    needs_open_window = False

    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = stream or sys.stdout
        self.sent: list[str] = []

    def send(self, text: str) -> str:
        self.sent.append(text)
        rule = "─" * 48
        print(f"{rule}\n{text}\n{rule}", file=self.stream, flush=True)
        return f"console-{len(self.sent)}"


@dataclass(frozen=True)
class PollResult:
    status: PollStatus
    sent: int = 0
    open_incidents: int = 0
    detail: str = ""


def policy_from(settings: Settings) -> Policy:
    return Policy(
        min_nodes=settings.alert_min_nodes,
        min_users=settings.alert_min_users,
        confirm=timedelta(seconds=settings.confirm_seconds),
        alerts_per_hour=settings.alerts_per_hour,
        watch=frozenset(settings.watch_nodes),
    )


class Monitor:
    def __init__(
        self,
        *,
        settings: Settings,
        feed: Feed,
        store: Store,
        writer: Writer,
        sender: Sender,
        tracker: IncidentTracker | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.feed = feed
        self.store = store
        self.writer = writer
        self.sender = sender
        self.stale_after = timedelta(seconds=settings.stale_after)
        self.tracker = tracker or IncidentTracker(policy_from(settings), stale_after=self.stale_after)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        state = store.load_state()
        if "tracker" in state:
            self.tracker.restore(state["tracker"])
        self.last: Snapshot | None = store.load_snapshot()
        self._fresh_at = self._clock()
        self._stale_notified = False
        self._glitches = 0

    def poll(self) -> PollResult:
        try:
            snapshot = self.feed.fetch()
        except FeedError as exc:
            self.store.log("poll_failed", error=str(exc))
            log.warning("feed unavailable: %s", exc)
            self._check_feed_health()
            return PollResult("failed", detail=str(exc))
        if snapshot is None or (self.last is not None and snapshot.taken_at <= self.last.taken_at):
            self._check_feed_health()
            return PollResult("unchanged", open_incidents=len(self.tracker.open))
        return self.observe(snapshot)

    def observe(self, snapshot: Snapshot) -> PollResult:
        """Process one new snapshot. Replays call this directly."""
        self._feed_is_fresh()
        before = self.last
        if before is None:
            self.tracker.start(snapshot.taken_at)
            self._accept(snapshot, "baseline")
            return PollResult("baseline")
        status: PollStatus = "ok"
        if snapshot.taken_at - before.taken_at > MAX_GAP:
            self.tracker.resume(snapshot.taken_at)
            status = "gap"
        vanished = self._mass_drop(before, snapshot)
        if vanished:
            return self._glitch(snapshot, vanished)
        self._glitches = 0
        update = self.tracker.update(before, snapshot)
        self._log_changes(update.changes)
        sent = sum(self._deliver(notice, snapshot) for notice in update.notices)
        self._accept(snapshot, status, sent)
        return PollResult(status, sent, len(self.tracker.open))

    def run(self, stop: threading.Event) -> None:
        interval = self.settings.poll_interval
        log.info("watching %s every %ss", self.settings.feed_url, interval)
        while not stop.is_set():
            started = time.monotonic()
            try:
                result = self.poll()
                log.info("poll %s: %s open, %s sent", result.status, result.open_incidents, result.sent)
            except Exception:  # a daemon must survive one bad snapshot; the traceback goes to the log
                log.exception("poll crashed")
            stop.wait(max(1.0, interval - (time.monotonic() - started)))

    # -- steps ------------------------------------------------------------------------------

    def _deliver(self, notice: Notice, snapshot: Snapshot) -> bool:
        incident = notice.incident
        if notice.kind == "alert":
            facts = outage_facts(incident, snapshot, self.settings)
        else:
            facts = resolved_facts(incident, snapshot, self.settings)
        draft = self.writer.write(facts)
        self._warn_if_window_closed()
        try:
            ref = self.sender.send(draft.text)
        except SendError as exc:
            self.tracker.mark_failed(notice)
            self.store.log("alert_failed", id=incident.id, notice=notice.kind, error=str(exc))
            log.error("could not send %s for %s: %s", notice.kind, incident.id, exc)
            return False
        self.tracker.mark_sent(notice, ref, snapshot.taken_at)
        self.store.log(
            "alert_sent",
            id=incident.id,
            notice=notice.kind,
            source=draft.source,
            note=draft.note,
            chars=len(draft.text),
            delay_s=int((snapshot.taken_at - incident.started_at).total_seconds()),
            ref=ref,
        )
        return True

    def _accept(self, snapshot: Snapshot, status: str, sent: int = 0) -> None:
        self.last = snapshot
        self.store.save_snapshot(snapshot)
        if self.settings.record_snapshots:
            self.store.record(snapshot)
        self.store.save_state({"tracker": self.tracker.to_dict(), "last_snapshot_at": snapshot.taken_at.isoformat()})
        self.store.log(
            "poll",
            status=status,
            taken_at=snapshot.taken_at.isoformat(),
            nodes=len(snapshot.nodes),
            reachable=snapshot.reachable_count(self.stale_after),
            open=len(self.tracker.open),
            sent=sent,
        )

    def _mass_drop(self, before: Snapshot, now: Snapshot) -> int:
        """How many nodes vanished at once, if that many points to a monitoring problem."""
        was_up = [n for n in before.nodes if before.is_reachable(n, self.stale_after)]
        vanished = sum(1 for n in was_up if not now.is_reachable(n, self.stale_after))
        threshold = max(GLITCH_MIN_NODES, GLITCH_SHARE * len(was_up))
        return vanished if vanished > threshold else 0

    def _glitch(self, snapshot: Snapshot, vanished: int) -> PollResult:
        """Hold the old baseline for a few polls; one broken snapshot should not page anyone."""
        self._glitches += 1
        self.store.log("glitch", vanished=vanished, count=self._glitches)
        if self._glitches < GLITCH_CONFIRMATIONS:
            return PollResult("glitch", open_incidents=len(self.tracker.open), detail=f"{vanished} nodes vanished")
        facts = {
            "event": "network_wide",
            "network": self.settings.network_name,
            "nodes_offline": vanished,
            "started": clock(snapshot.taken_at, snapshot.taken_at),
        }
        self._send_plain(facts, "network_wide")
        self._glitches = 0
        self.tracker.resume(snapshot.taken_at)
        self._accept(snapshot, "glitch")
        return PollResult("glitch", open_incidents=len(self.tracker.open), detail="accepted as the new baseline")

    def _check_feed_health(self) -> None:
        if self._stale_notified or self.last is None:
            return
        if self._clock() - self._fresh_at >= STALE_FEED:
            now = self._clock().astimezone(self.last.taken_at.tzinfo)
            self._send_plain(stale_feed_facts(self.last.taken_at, now, self.settings), "feed_stale")
            self._stale_notified = True

    def _feed_is_fresh(self) -> None:
        self._fresh_at = self._clock()
        if self._stale_notified:
            self.store.log("feed_recovered")
            self._stale_notified = False

    def _send_plain(self, facts: dict[str, Any], event: str) -> None:
        draft = self.writer.write(facts)
        try:
            ref = self.sender.send(draft.text)
        except SendError as exc:
            self.store.log("alert_failed", notice=event, error=str(exc))
            return
        self.store.log("alert_sent", notice=event, source=draft.source, chars=len(draft.text), ref=ref)

    def _log_changes(self, changes: list[Change]) -> None:
        for change in changes:
            incident = change.incident
            fields = {"id": incident.id, "kind": incident.kind, "size": incident.size}
            if change.kind == "opened":
                fields.update(
                    started_at=incident.started_at.isoformat(),
                    delay_s=int((incident.detected_at - incident.started_at).total_seconds()),
                    roots=list(incident.roots),
                )
            elif change.kind == "resolved" and incident.resolved_at:
                fields.update(
                    minutes=round((incident.resolved_at - incident.started_at).total_seconds() / 60),
                    back=sum(1 for m in incident.members.values() if m.is_back),
                    alerted=incident.alerted_at is not None,
                )
            elif change.detail:
                fields["detail"] = change.detail
            self.store.log(f"incident_{change.kind}", **fields)
            log.info("incident %s %s (%s, %s nodes)", incident.id, change.kind, incident.kind, incident.size)

    def _warn_if_window_closed(self) -> None:
        if not self.sender.needs_open_window:
            return
        last = self.store.read_json("inbox.json").get("last_inbound_at")
        if last is None or self._clock() - datetime.fromisoformat(last) > SERVICE_WINDOW:
            log.warning(
                "you have not messaged Uplink on WhatsApp in 24 hours, so WhatsApp may drop this alert. "
                "Send it any message to reopen the window, or set WHATSAPP_ALERT_TEMPLATE."
            )
