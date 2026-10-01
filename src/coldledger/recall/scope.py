from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from threading import RLock

from ..core.clock import utc


@dataclass(frozen=True)
class ScopeItem:
    batch: str
    customer: str
    quantity: Decimal


@dataclass(frozen=True)
class ScopeRevision:
    sequence: int
    recall: str
    action: str
    item: ScopeItem
    actor: str
    reason: str
    recorded_at: datetime


class ScopeBook:
    """Append-only recall scope.

    Excluding a batch/customer pair posts a new revision; the original
    inclusion is never deleted, so the scope in force at any past moment
    stays reconstructable via :meth:`at`.
    """

    def __init__(self, recall: str):
        if not recall:
            raise ValueError("recall reference required")
        self._recall = recall
        self._rows: list[ScopeRevision] = []
        self._lock = RLock()

    @property
    def recall(self) -> str:
        return self._recall

    def _append(self, action: str, item: ScopeItem, actor: str, reason: str, recorded_at: datetime) -> ScopeRevision:
        if not actor or not reason:
            raise ValueError("actor and reason required")
        recorded_at = utc(recorded_at)
        if self._rows and recorded_at < self._rows[-1].recorded_at:
            raise ValueError("event time moved backward")
        row = ScopeRevision(len(self._rows) + 1, self._recall, action, item, actor, reason, recorded_at)
        self._rows.append(row)
        return row

    def include(self, batch: str, customer: str, quantity, actor: str, reason: str, recorded_at: datetime) -> ScopeRevision:
        if not batch or not customer:
            raise ValueError("batch and customer required")
        q = Decimal(str(quantity))
        if q <= 0:
            raise ValueError("scope quantity must be positive")
        with self._lock:
            if any(i.batch == batch and i.customer == customer for i in self._current().values()):
                raise ValueError("scope item already included")
            return self._append("include", ScopeItem(batch, customer, q), actor, reason, recorded_at)

    def exclude(self, batch: str, customer: str, actor: str, reason: str, recorded_at: datetime) -> ScopeRevision:
        with self._lock:
            current = self._current()
            key = (batch, customer)
            if key not in current:
                raise KeyError(f"scope item not included: {batch}/{customer}")
            return self._append("exclude", current[key], actor, reason, recorded_at)

    def _current(self) -> dict[tuple[str, str], ScopeItem]:
        out: dict[tuple[str, str], ScopeItem] = {}
        for row in self._rows:
            key = (row.item.batch, row.item.customer)
            if row.action == "include":
                out[key] = row.item
            else:
                out.pop(key, None)
        return out

    def items(self) -> tuple[ScopeItem, ...]:
        with self._lock:
            return tuple(sorted(self._current().values(), key=lambda i: (i.batch, i.customer)))

    def at(self, when: datetime) -> tuple[ScopeItem, ...]:
        when = utc(when)
        with self._lock:
            out: dict[tuple[str, str], ScopeItem] = {}
            for row in self._rows:
                if row.recorded_at > when:
                    break
                key = (row.item.batch, row.item.customer)
                if row.action == "include":
                    out[key] = row.item
                else:
                    out.pop(key, None)
            return tuple(sorted(out.values(), key=lambda i: (i.batch, i.customer)))

    def revisions(self) -> tuple[ScopeRevision, ...]:
        with self._lock:
            return tuple(self._rows)

    def quantity(self, batch: str | None = None) -> Decimal:
        return sum((i.quantity for i in self.items() if batch is None or i.batch == batch), Decimal("0"))
