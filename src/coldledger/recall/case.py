from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from threading import RLock

from ..core.clock import utc
from .acknowledgments import Ack, AckLedger
from .closure import (
    ClosureCertificate,
    ClosureReport,
    PairStatus,
    evaluate,
    issue_certificate,
)
from .deadlines import DeadlineBook, DeadlineRevision
from .escalation import MAX_LEVEL, Escalation, EscalationLog
from .notification_waves import Notice, NotificationLog
from .quantity_ledger import Entry, RecallLedger
from .scope import ScopeBook, ScopeItem, ScopeRevision

ACK_DEADLINE = "acknowledge"


@dataclass(frozen=True)
class Waiver:
    recall: str
    batch: str
    customer: str
    actor: str
    reason: str
    granted_at: datetime


def account_for(batch: str, customer: str) -> str:
    return f"{batch}::{customer}"


class RecallCase:
    """One recall case end to end.

    Wires scope, regulatory deadlines, notification waves, customer
    acknowledgments, escalations and returned quantities into a single chain
    that closure evidence is computed from. Workflow state transitions
    (draft/approved/issued/...) live in ``campaign_state``; this class tracks
    the facts those transitions are judged on.
    """

    def __init__(self, reference: str):
        if not reference:
            raise ValueError("recall reference required")
        self.reference = reference
        self.scope = ScopeBook(reference)
        self.deadlines = DeadlineBook(reference)
        self.notices = NotificationLog(reference)
        self.acks = AckLedger(reference)
        self.escalations = EscalationLog(reference)
        self.quantities = RecallLedger()
        self._waivers: list[Waiver] = []
        self._lock = RLock()

    # -- scope -------------------------------------------------------------
    def include(self, batch: str, customer: str, quantity, actor: str, reason: str, at: datetime) -> ScopeRevision:
        return self.scope.include(batch, customer, quantity, actor, reason, at)

    def exclude(self, batch: str, customer: str, actor: str, reason: str, at: datetime) -> ScopeRevision:
        return self.scope.exclude(batch, customer, actor, reason, at)

    # -- regulatory deadlines ----------------------------------------------
    def set_deadline(self, kind: str, due_at: datetime, actor: str, reason: str, at: datetime) -> DeadlineRevision:
        return self.deadlines.post(kind, due_at, actor, reason, at)

    # -- notification waves --------------------------------------------------
    def notify(self, wave: str, batch: str, customer: str, sent_at: datetime,
               channel: str = "mail", quantity=None, reference: str | None = None) -> tuple[Notice, bool]:
        item = self._scope_item(batch, customer)
        if item is None:
            raise ValueError(f"not in recall scope: {batch}/{customer}")
        q = item.quantity if quantity is None else Decimal(str(quantity))
        if q > item.quantity:
            raise ValueError("notice quantity exceeds scope")
        ref = reference or f"{self.reference}:{wave}:{batch}:{customer}"
        return self.notices.send(ref, wave, batch, customer, q, sent_at, channel)

    # -- customer acknowledgments -------------------------------------------
    def acknowledge(self, reference: str, wave: str, batch: str, customer: str, quantity,
                    received_at: datetime) -> tuple[Ack, bool]:
        if not any(n.wave == wave and n.batch == batch and n.customer == customer
                   for n in self.notices.notices()):
            raise ValueError("acknowledgment without matching notice")
        return self.acks.record(reference, wave, batch, customer, quantity, received_at)

    # -- returned quantities --------------------------------------------------
    def book_return(self, batch: str, customer: str, quantity, reference: str,
                    reason: str = "returned") -> Entry:
        item = self._scope_item(batch, customer)
        if item is None:
            raise ValueError(f"not in recall scope: {batch}/{customer}")
        q = Decimal(str(quantity))
        with self._lock:
            account = account_for(batch, customer)
            if self.quantities.balance(account) + q > item.quantity:
                raise ValueError("returned quantity exceeds scope")
            return self.quantities.post(account, reference, q, reason)

    # -- waivers --------------------------------------------------------------
    def waive(self, batch: str, customer: str, actor: str, reason: str, at: datetime) -> Waiver:
        if not actor or not reason:
            raise ValueError("actor and reason required")
        with self._lock:
            if any(w.batch == batch and w.customer == customer for w in self._waivers):
                raise ValueError("waiver already granted")
            row = Waiver(self.reference, batch, customer, actor, reason, utc(at))
            self._waivers.append(row)
            return row

    def waivers(self) -> tuple[Waiver, ...]:
        with self._lock:
            return tuple(self._waivers)

    # -- escalation -------------------------------------------------------------
    def check_escalations(self, now: datetime, actor: str = "system",
                          reason: str = "acknowledgment overdue") -> tuple[Escalation, ...]:
        now = utc(now)
        deadline = self.deadlines.current(ACK_DEADLINE)
        if deadline is None:
            return ()
        acked = self.acks.acknowledged_keys()
        waived = {(w.batch, w.customer) for w in self.waivers()}
        raised: list[Escalation] = []
        for notice in self.notices.notices():
            if (notice.wave, notice.batch, notice.customer) in acked:
                continue
            if (notice.batch, notice.customer) in waived:
                continue
            if now <= deadline.due_at:
                continue
            current = self.escalations.level(notice.customer, notice.batch, "acknowledge")
            row, created = self.escalations.raise_level(
                notice.customer, notice.batch, "acknowledge", min(current + 1, MAX_LEVEL),
                deadline, now, actor, reason,
            )
            if created:
                raised.append(row)
        return tuple(raised)

    # -- closure ----------------------------------------------------------------
    def pair_status(self) -> tuple[PairStatus, ...]:
        acked_pairs = {(batch, customer) for _, batch, customer in self.acks.acknowledged_keys()}
        waived = {(w.batch, w.customer) for w in self.waivers()}
        keys = {(i.batch, i.customer) for i in self.scope.items()}
        keys |= {(n.batch, n.customer) for n in self.notices.notices()}
        rows = []
        for batch, customer in sorted(keys):
            item = self._scope_item(batch, customer)
            in_scope = item.quantity if item else Decimal("0")
            returned = self.quantities.balance(account_for(batch, customer))
            rows.append(PairStatus(
                batch=batch,
                customer=customer,
                in_scope=in_scope,
                notified=self.notices.notified_quantity(batch=batch, customer=customer),
                acknowledged=(batch, customer) in acked_pairs,
                waived=(batch, customer) in waived,
                returned=returned,
                variance=in_scope - returned,
                escalation_level=self.escalations.level(customer, batch, "acknowledge"),
            ))
        return tuple(rows)

    def closure_report(self, generated_at: datetime) -> ClosureReport:
        return evaluate(
            self.reference,
            self.pair_status(),
            self.acks.duplicates(),
            self.escalations.escalations(),
            self.deadlines.history(),
            generated_at,
        )

    def close(self, actor: str, at: datetime) -> ClosureCertificate:
        return issue_certificate(self.closure_report(at), actor, at)

    def _scope_item(self, batch: str, customer: str) -> ScopeItem | None:
        for item in self.scope.items():
            if item.batch == batch and item.customer == customer:
                return item
        return None
