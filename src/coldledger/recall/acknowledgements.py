from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from threading import RLock

from .notice_waves import NoticeWave


@dataclass(frozen=True)
class Acknowledgement:
    wave_id: str
    customer: str
    reference: str
    received_at: datetime


class AckRegister:
    """Customer acknowledgements with idempotent counting.

    The first acknowledgement per (wave, customer) counts. Later repeats —
    a customer replying twice, or the same reference resubmitted — are kept
    visible for audit but never counted again.
    """

    def __init__(self):
        self._counted: dict[tuple[str, str], Acknowledgement] = {}
        self._duplicates: list[Acknowledgement] = []
        self._refs: dict[str, tuple[str, str]] = {}
        self._lock = RLock()

    def record(self, ack: Acknowledgement) -> bool:
        """Returns True when the acknowledgement counted, False for a repeat."""
        if not ack.wave_id or not ack.customer or not ack.reference:
            raise ValueError("wave, customer and reference required")
        key = (ack.wave_id, ack.customer)
        with self._lock:
            holder = self._refs.get(ack.reference)
            if holder is not None and holder != key:
                raise ValueError("acknowledgement reference conflict")
            if holder is not None or key in self._counted:
                self._duplicates.append(ack)
                return False
            self._counted[key] = ack
            self._refs[ack.reference] = key
            return True

    def acknowledged(self, wave_id: str) -> frozenset[str]:
        with self._lock:
            return frozenset(customer for (wave, customer) in self._counted if wave == wave_id)

    def outstanding(self, wave: NoticeWave) -> frozenset[str]:
        return frozenset(wave.recipients) - self.acknowledged(wave.wave_id)

    def coverage(self, wave: NoticeWave) -> float:
        if not wave.recipients:
            return 1.0
        return len(self.acknowledged(wave.wave_id) & set(wave.recipients)) / len(wave.recipients)

    def duplicates(self) -> tuple[Acknowledgement, ...]:
        with self._lock:
            return tuple(self._duplicates)

    def entries(self) -> tuple[Acknowledgement, ...]:
        with self._lock:
            return tuple(self._counted.values())
