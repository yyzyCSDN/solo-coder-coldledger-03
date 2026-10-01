from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from threading import RLock

from ..core.clock import utc
from .deadlines import DeadlineRevision

TRIGGERS = ("acknowledge", "return")
LEVELS = {1: "reminder", 2: "formal_notice", 3: "regulatory_report"}
MAX_LEVEL = max(LEVELS)


@dataclass(frozen=True)
class Escalation:
    sequence: int
    recall: str
    customer: str
    batch: str
    trigger: str
    level: int
    deadline_sequence: int
    raised_at: datetime
    actor: str
    reason: str

    @property
    def label(self) -> str:
        return LEVELS.get(self.level, f"level_{self.level}")


class EscalationLog:
    """Append-only escalation record.

    Every escalation pins the deadline revision it was decided under, so a
    later deadline extension cannot erase the original decision. Re-raising
    the same level under the same deadline revision is idempotent; re-raising
    it under a newer revision (for example after an extension lapses) adds a
    new evidence row.
    """

    def __init__(self, recall: str):
        if not recall:
            raise ValueError("recall reference required")
        self._recall = recall
        self._rows: list[Escalation] = []
        self._lock = RLock()

    def raise_level(self, customer: str, batch: str, trigger: str, level: int,
                    deadline: DeadlineRevision, raised_at: datetime, actor: str,
                    reason: str) -> tuple[Escalation, bool]:
        if trigger not in TRIGGERS:
            raise ValueError("unknown trigger")
        if not 1 <= level <= MAX_LEVEL:
            raise ValueError("level outside supported ladder")
        if not customer or not batch or not actor or not reason:
            raise ValueError("customer, batch, actor and reason required")
        if deadline.recall != self._recall:
            raise ValueError("deadline belongs to another recall")
        raised_at = utc(raised_at)
        with self._lock:
            for row in self._rows:
                if (row.customer, row.batch, row.trigger, row.level, row.deadline_sequence) == (customer, batch, trigger, level, deadline.sequence):
                    return row, False
            current = self.level(customer, batch, trigger)
            if level > current + 1:
                raise ValueError(f"levels must rise one at a time (current {current})")
            if level == current:
                pinned = max(
                    r.deadline_sequence for r in self._rows
                    if r.customer == customer and r.batch == batch and r.trigger == trigger and r.level == level
                )
                if deadline.sequence <= pinned:
                    raise ValueError("level already raised under this or a newer deadline")
            row = Escalation(len(self._rows) + 1, self._recall, customer, batch, trigger, level,
                             deadline.sequence, raised_at, actor, reason)
            self._rows.append(row)
            return row, True

    def level(self, customer: str, batch: str, trigger: str) -> int:
        with self._lock:
            return max(
                (r.level for r in self._rows
                 if r.customer == customer and r.batch == batch and r.trigger == trigger),
                default=0,
            )

    def escalations(self, customer: str | None = None, batch: str | None = None) -> tuple[Escalation, ...]:
        with self._lock:
            return tuple(
                r for r in self._rows
                if (customer is None or r.customer == customer)
                and (batch is None or r.batch == batch)
            )
