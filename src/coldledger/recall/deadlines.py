from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from threading import RLock

from ..core.clock import utc


@dataclass(frozen=True)
class DeadlineRevision:
    sequence: int
    recall: str
    kind: str
    due_at: datetime
    actor: str
    reason: str
    recorded_at: datetime
    supersedes: int | None


class DeadlineBook:
    """Append-only regulatory deadline registry.

    Moving a deadline posts a new revision that supersedes the previous one;
    the superseded revision is never removed, so a decision taken under it
    (for example an escalation) remains explainable after the change.
    """

    def __init__(self, recall: str):
        if not recall:
            raise ValueError("recall reference required")
        self._recall = recall
        self._rows: list[DeadlineRevision] = []
        self._lock = RLock()

    def post(self, kind: str, due_at: datetime, actor: str, reason: str, recorded_at: datetime) -> DeadlineRevision:
        if not kind or not actor or not reason:
            raise ValueError("kind, actor and reason required")
        due_at = utc(due_at)
        recorded_at = utc(recorded_at)
        with self._lock:
            if self._rows and recorded_at < self._rows[-1].recorded_at:
                raise ValueError("event time moved backward")
            previous = self._current_unlocked(kind)
            row = DeadlineRevision(
                len(self._rows) + 1, self._recall, kind, due_at, actor, reason, recorded_at,
                previous.sequence if previous else None,
            )
            self._rows.append(row)
            return row

    def _current_unlocked(self, kind: str) -> DeadlineRevision | None:
        for row in reversed(self._rows):
            if row.kind == kind:
                return row
        return None

    def current(self, kind: str) -> DeadlineRevision | None:
        with self._lock:
            return self._current_unlocked(kind)

    def get(self, sequence: int) -> DeadlineRevision:
        with self._lock:
            for row in self._rows:
                if row.sequence == sequence:
                    return row
        raise KeyError(sequence)

    def at(self, kind: str, when: datetime) -> DeadlineRevision | None:
        when = utc(when)
        with self._lock:
            found = None
            for row in self._rows:
                if row.kind == kind and row.recorded_at <= when:
                    found = row
            return found

    def history(self, kind: str | None = None) -> tuple[DeadlineRevision, ...]:
        with self._lock:
            rows = self._rows if kind is None else [r for r in self._rows if r.kind == kind]
            return tuple(rows)

    def overdue(self, kind: str, when: datetime) -> bool:
        current = self.current(kind)
        return current is not None and utc(when) > current.due_at
