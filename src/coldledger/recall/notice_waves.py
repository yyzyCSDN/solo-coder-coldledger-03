from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import RLock


def _utc(at: datetime) -> datetime:
    return at.astimezone(timezone.utc)


@dataclass(frozen=True)
class NoticeWave:
    wave_id: str
    recall_reference: str
    channel: str
    sent_at: datetime
    recipients: frozenset[str]

    def validate(self) -> None:
        if not self.wave_id or not self.recall_reference:
            raise ValueError("wave id and recall reference required")
        if not self.recipients:
            raise ValueError("wave must address at least one recipient")


@dataclass(frozen=True)
class DeadlineRevision:
    wave_id: str
    revision: int
    deadline: datetime
    reason: str
    actor: str
    at: datetime


class DeadlineLog:
    """Append-only regulatory deadline history per notice wave.

    Every change is appended as a new revision; earlier revisions are never
    mutated or removed, so the deadline that was in force at any moment —
    and who decided it and why — stays provable after the fact.
    """

    def __init__(self):
        self._rows: list[DeadlineRevision] = []
        self._lock = RLock()

    def issue(self, wave: NoticeWave, deadline: datetime, actor: str, at: datetime, reason: str = "initial") -> DeadlineRevision:
        wave.validate()
        if not actor:
            raise ValueError("actor required")
        with self._lock:
            if self.history(wave.wave_id):
                raise ValueError("wave already issued")
            row = DeadlineRevision(wave.wave_id, 1, _utc(deadline), reason or "initial", actor, _utc(at))
            if row.deadline < _utc(wave.sent_at):
                raise ValueError("deadline before wave dispatch")
            self._rows.append(row)
            return row

    def revise(self, wave_id: str, deadline: datetime, actor: str, at: datetime, reason: str) -> DeadlineRevision:
        if not reason:
            raise ValueError("deadline change requires a reason")
        if not actor:
            raise ValueError("actor required")
        with self._lock:
            history = self.history(wave_id)
            if not history:
                raise KeyError(wave_id)
            previous = history[-1]
            at = _utc(at)
            if at < previous.at:
                raise ValueError("revision time moved backward")
            row = DeadlineRevision(wave_id, previous.revision + 1, _utc(deadline), reason, actor, at)
            self._rows.append(row)
            return row

    def history(self, wave_id: str) -> tuple[DeadlineRevision, ...]:
        return tuple(r for r in self._rows if r.wave_id == wave_id)

    def current(self, wave_id: str) -> DeadlineRevision:
        history = self.history(wave_id)
        if not history:
            raise KeyError(wave_id)
        return history[-1]

    def deadline_at(self, wave_id: str, moment: datetime) -> DeadlineRevision:
        """Revision in force at `moment`; reconstructs superseded decisions."""
        moment = _utc(moment)
        chosen = None
        for row in self.history(wave_id):
            if row.at <= moment:
                chosen = row
        if chosen is None:
            raise KeyError(wave_id)
        return chosen

    def revisions(self) -> tuple[DeadlineRevision, ...]:
        return tuple(self._rows)

    def verify(self) -> None:
        by_wave: dict[str, list[DeadlineRevision]] = {}
        for row in self._rows:
            by_wave.setdefault(row.wave_id, []).append(row)
        for rows in by_wave.values():
            for expected, row in enumerate(rows, start=1):
                if row.revision != expected:
                    raise ValueError("revision gap")
            for earlier, later in zip(rows, rows[1:]):
                if later.at < earlier.at:
                    raise ValueError("revision time moved backward")
                if not later.reason:
                    raise ValueError("revision without reason")
