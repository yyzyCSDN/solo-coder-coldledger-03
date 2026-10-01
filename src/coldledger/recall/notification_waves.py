from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from threading import RLock

from ..core.clock import utc


@dataclass(frozen=True)
class Notice:
    reference: str
    recall: str
    wave: str
    batch: str
    customer: str
    quantity: Decimal
    channel: str
    sent_at: datetime


class NotificationLog:
    """Recall notices grouped into waves.

    Sending is idempotent per reference: a retried send with the same
    reference and payload returns the original notice instead of notifying
    the customer twice; the same reference with a different payload is a
    conflict and rejected.
    """

    def __init__(self, recall: str):
        if not recall:
            raise ValueError("recall reference required")
        self._recall = recall
        self._rows: list[Notice] = []
        self._by_ref: dict[str, Notice] = {}
        self._lock = RLock()

    def send(self, reference: str, wave: str, batch: str, customer: str, quantity,
             sent_at: datetime, channel: str = "mail") -> tuple[Notice, bool]:
        if not reference or not wave or not batch or not customer:
            raise ValueError("reference, wave, batch and customer required")
        q = Decimal(str(quantity))
        if q <= 0:
            raise ValueError("notice quantity must be positive")
        sent_at = utc(sent_at)
        with self._lock:
            existing = self._by_ref.get(reference)
            if existing is not None:
                if (existing.wave, existing.batch, existing.customer, existing.quantity, existing.channel) != (wave, batch, customer, q, channel):
                    raise ValueError("conflicting notice reference")
                return existing, False
            row = Notice(reference, self._recall, wave, batch, customer, q, channel, sent_at)
            self._rows.append(row)
            self._by_ref[reference] = row
            return row, True

    def wave(self, name: str) -> tuple[Notice, ...]:
        with self._lock:
            return tuple(r for r in self._rows if r.wave == name)

    def waves(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted({r.wave for r in self._rows}))

    def notices(self, customer: str | None = None, batch: str | None = None) -> tuple[Notice, ...]:
        with self._lock:
            return tuple(
                r for r in self._rows
                if (customer is None or r.customer == customer)
                and (batch is None or r.batch == batch)
            )

    def notified_quantity(self, batch: str | None = None, customer: str | None = None) -> Decimal:
        return sum((r.quantity for r in self.notices(customer=customer, batch=batch)), Decimal("0"))
