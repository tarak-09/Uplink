"""Everything Uplink keeps on disk, in one folder.

    state.json           the monitor's state (incidents, history), rewritten after each poll
    inbox.json           when the volunteer last wrote to Uplink (written by the webhook)
    snapshot.json.gz     the last snapshot in compact form, so a restart can keep diffing
    events.jsonl         an append-only log; `uplink report` turns it into numbers
    recordings/<day>/    one compact snapshot per poll, for `uplink replay` (optional)

Each JSON file has a single writer process, and every write is atomic. Message text is
never logged, only its length.
"""

from __future__ import annotations

import gzip
import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .mesh import Snapshot, from_compact, to_compact


class StoreError(Exception):
    pass


class Store:
    def __init__(self, root: Path, clock: Callable[[], datetime] | None = None) -> None:
        self.root = root
        self.snapshot_path = root / "snapshot.json.gz"
        self.events_path = root / "events.jsonl"
        self.recordings_dir = root / "recordings"
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()

    def read_json(self, name: str) -> dict[str, Any]:
        path = self.root / name
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise StoreError(f"{path} is unreadable ({exc}); move it away to start fresh") from None
        return data if isinstance(data, dict) else {}

    def write_json(self, name: str, data: dict[str, Any]) -> None:
        self._write_atomic(self.root / name, json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8"))

    def load_state(self) -> dict[str, Any]:
        return self.read_json("state.json")

    def save_state(self, state: dict[str, Any]) -> None:
        self.write_json("state.json", state)

    def load_snapshot(self) -> Snapshot | None:
        try:
            data = self.snapshot_path.read_bytes()
        except FileNotFoundError:
            return None
        try:
            return from_compact(json.loads(gzip.decompress(data)))
        except (OSError, EOFError, ValueError) as exc:
            raise StoreError(f"{self.snapshot_path} is damaged ({exc}); delete it to start fresh") from None

    def save_snapshot(self, snapshot: Snapshot) -> None:
        self._write_atomic(self.snapshot_path, _pack(snapshot))

    def record(self, snapshot: Snapshot) -> Path:
        moment = snapshot.taken_at
        path = self.recordings_dir / f"{moment:%Y-%m-%d}" / f"{moment:%H%M%S}.json.gz"
        self._write_atomic(path, _pack(snapshot))
        return path

    def log(self, event: str, **fields: Any) -> None:
        record = {"at": self._clock().isoformat(timespec="seconds"), "event": event, **fields}
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            with self.events_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def events(self) -> Iterator[dict[str, Any]]:
        """Read the event log, skipping a line cut short by a crash."""
        try:
            handle = self.events_path.open(encoding="utf-8")
        except FileNotFoundError:
            return
        with handle:
            for line in handle:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue

    def _write_atomic(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(temp, path)
        except BaseException:
            Path(temp).unlink(missing_ok=True)
            raise


def _pack(snapshot: Snapshot) -> bytes:
    text = json.dumps(to_compact(snapshot), ensure_ascii=False, separators=(",", ":"))
    return gzip.compress(text.encode("utf-8"), mtime=0)
