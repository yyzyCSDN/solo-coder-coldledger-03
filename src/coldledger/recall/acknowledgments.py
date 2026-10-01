from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from threading import RLock

from ..core.clock import utc


@dataclass(frozen=True)
class Ack:
    reference: str
    recall: str
    wave: str
    batch: str
    customer: str
    quantity: Decimal
    received_at: datetime
    duplicate_of: str | None


class AckLedger:
    """Customer acknowledgments with two-level de-duplication.

    * Same reference and payload: an idempotent replay; the original row is
      returned and nothing is added.
    * Same (wave, batch, customer) under a new reference: a re-transmission.
      It is kept as evidence with ``duplicate_of`` pointing at the first ack
      but is never counted, so duplicate receipts cannot inflate the numbers.
    """

    def __init__(self, recall: str):
        if not recall:
            raise ValueError("recall reference required")
        self._recall = recall
        self._rows: list[Ack] = []
        self._by_ref: dict[str, Ack] = {}
        self._first: dict[tuple[str, str, str], Ack] = {}
        self._lock = RLock()

    def record(self, reference: str, wave: str, batch: str, customer: str, quantity,
               received_at: datetime) -> tuple[Ack, bool]:
        if not reference or not wave or not batch or not customer:
            raise ValueError("reference, wave, batch and customer required")
        q = Decimal(str(quantity))
        if q < 0:
            raise ValueError("negative acknowledged quantity")
        received_at = utc(received_at)
        key = (wave, batch, customer)
        with self._lock:
            existing = self._by_ref.get(reference)
            if existing is not None:
                if (existing.wave, existing.batch, existing.customer, existing.quantity) != (wave, batch, customer, q):
                    raise ValueError("conflicting acknowledgment reference")
                return existing, False
            first = self._first.get(key)
            row = Ack(reference, self._recall, wave, batch, customer, q, received_at,
                      first.reference if first else None)
            self._rows.append(row)
            self._by_ref[reference] = row
            if first is None:
                self._first[key] = row
            return row, first is None

    def counted(self, customer: str | None = None, batch: str | None = None) -> tuple[Ack, ...]:
        with self._lock:
            return tuple(
                r for r in self._rows if r.duplicate_of is None
                and (customer is None or r.customer == customer)
                and (batch is None or r.batch == batch)
            )

    def duplicates(self) -> tuple[Ack, ...]:
        with self._lock:
            return tuple(r for r in self._rows if r.duplicate_of is not None)

    def acknowledged(self, wave: str, batch: str, customer: str) -> bool:
        with self._lock:
            return (wave, batch, customer) in self._first

    def acknowledged_keys(self) -> frozenset[tuple[str, str, str]]:
        with self._lock:
            return frozenset(self._first)

    def acknowledged_quantity(self, batch: str | None = None, customer: str | None = None) -> Decimal:
        return sum((r.quantity for r in self.counted(customer=customer, batch=batch)), Decimal("0"))
