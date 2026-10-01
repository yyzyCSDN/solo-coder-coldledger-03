from __future__ import annotations
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from threading import RLock

from .notice_waves import NoticeWave

DEFAULT_STEPS = (timedelta(hours=24), timedelta(hours=72))


def _utc(at: datetime) -> datetime:
    return at.astimezone(timezone.utc)


@dataclass(frozen=True)
class Escalation:
    escalation_id: str
    recall_reference: str
    wave_id: str
    customer: str
    level: int
    reason: str
    actor: str
    at: datetime
    resolved_at: datetime | None = None

    @property
    def open(self) -> bool:
        return self.resolved_at is None


def ladder_level(overdue: timedelta, steps=DEFAULT_STEPS) -> int:
    level = 1
    for step in steps:
        if overdue >= step:
            level += 1
    return level


def pending_escalations(wave: NoticeWave, deadline: datetime, acknowledged, now: datetime, steps=DEFAULT_STEPS) -> tuple[tuple[str, int], ...]:
    """(customer, level) pairs past the current deadline, sorted by customer."""
    deadline = _utc(deadline)
    now = _utc(now)
    if now <= deadline:
        return ()
    level = ladder_level(now - deadline, steps)
    missing = sorted(set(wave.recipients) - set(acknowledged))
    return tuple((customer, level) for customer in missing)


class EscalationLog:
    """Append-only escalation register; resolution is recorded, never erased."""

    def __init__(self):
        self._rows: list[Escalation] = []
        self._ids: set[str] = set()
        self._resolved: dict[str, datetime] = {}
        self._lock = RLock()

    def raise_escalation(self, escalation_id: str, reference: str, wave_id: str, customer: str,
                         level: int, reason: str, actor: str, at: datetime) -> Escalation:
        if not escalation_id or not reference or not reason or not actor:
            raise ValueError("id, reference, reason and actor required")
        if level < 1:
            raise ValueError("level must be >= 1")
        with self._lock:
            if escalation_id in self._ids:
                raise ValueError("duplicate escalation id")
            row = Escalation(escalation_id, reference, wave_id, customer, level, reason, actor, _utc(at))
            self._rows.append(row)
            self._ids.add(escalation_id)
            return row

    def resolve(self, escalation_id: str, at: datetime) -> Escalation:
        with self._lock:
            if escalation_id not in self._ids:
                raise KeyError(escalation_id)
            if escalation_id in self._resolved:
                raise ValueError("escalation already resolved")
            at = _utc(at)
            row = next(r for r in self._rows if r.escalation_id == escalation_id)
            if at < row.at:
                raise ValueError("resolution before escalation")
            self._resolved[escalation_id] = at
            return replace(row, resolved_at=at)

    def entries(self, reference: str | None = None) -> tuple[Escalation, ...]:
        with self._lock:
            rows = [r for r in self._rows if reference is None or r.recall_reference == reference]
            return tuple(replace(r, resolved_at=self._resolved.get(r.escalation_id)) for r in rows)

    def open_escalations(self, reference: str | None = None) -> tuple[Escalation, ...]:
        return tuple(r for r in self.entries(reference) if r.open)
