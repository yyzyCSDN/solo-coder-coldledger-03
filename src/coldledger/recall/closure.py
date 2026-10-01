from __future__ import annotations
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Iterable

from ..core.clock import utc
from .acknowledgments import Ack
from .deadlines import DeadlineRevision
from .escalation import Escalation


@dataclass(frozen=True)
class PairStatus:
    batch: str
    customer: str
    in_scope: Decimal
    notified: Decimal
    acknowledged: bool
    waived: bool
    returned: Decimal
    variance: Decimal
    escalation_level: int


@dataclass(frozen=True)
class ClosureReport:
    recall: str
    generated_at: datetime
    pairs: tuple[PairStatus, ...]
    duplicate_acks: tuple[Ack, ...]
    escalations: tuple[Escalation, ...]
    deadline_history: tuple[DeadlineRevision, ...]
    blockers: tuple[str, ...]
    ready: bool
    digest: str


@dataclass(frozen=True)
class ClosureCertificate:
    recall: str
    closed_at: datetime
    actor: str
    report_digest: str
    digest: str


def _canonical(payload) -> str:
    def default(o):
        if isinstance(o, Decimal):
            return str(o)
        if isinstance(o, datetime):
            return o.isoformat()
        if hasattr(o, "__dataclass_fields__"):
            return {k: getattr(o, k) for k in o.__dataclass_fields__}
        raise TypeError(type(o).__name__)
    return json.dumps(payload, default=default, sort_keys=True, separators=(",", ":"))


def _payload(recall, generated_at, pairs, duplicate_acks, escalations, deadline_history, blockers) -> dict:
    return {
        "recall": recall,
        "generated_at": generated_at,
        "pairs": tuple(pairs),
        "duplicate_acks": tuple(duplicate_acks),
        "escalations": tuple(escalations),
        "deadline_history": tuple(deadline_history),
        "blockers": tuple(blockers),
    }


def evaluate(recall: str, pairs: Iterable[PairStatus], duplicate_acks: Iterable[Ack],
             escalations: Iterable[Escalation], deadline_history: Iterable[DeadlineRevision],
             generated_at: datetime) -> ClosureReport:
    pairs = tuple(pairs)
    blockers: list[str] = []
    for p in pairs:
        tag = f"{p.batch}:{p.customer}"
        if p.in_scope > 0 and p.notified == 0:
            blockers.append(f"notice-missing:{tag}")
        if p.notified > 0 and not p.acknowledged and not p.waived:
            blockers.append(f"ack-outstanding:{tag}")
        if p.variance != 0:
            blockers.append(f"quantity-unreconciled:{tag}:{p.variance}")
    blockers = sorted(blockers)
    generated_at = utc(generated_at)
    digest = hashlib.sha256(
        _canonical(_payload(recall, generated_at, pairs, duplicate_acks, escalations, deadline_history, blockers)).encode()
    ).hexdigest()
    return ClosureReport(recall, generated_at, pairs, tuple(duplicate_acks), tuple(escalations),
                         tuple(deadline_history), tuple(blockers), not blockers, digest)


def verify(report: ClosureReport) -> bool:
    digest = hashlib.sha256(
        _canonical(_payload(report.recall, report.generated_at, report.pairs, report.duplicate_acks,
                            report.escalations, report.deadline_history, report.blockers)).encode()
    ).hexdigest()
    return digest == report.digest


def issue_certificate(report: ClosureReport, actor: str, closed_at: datetime) -> ClosureCertificate:
    if not report.ready:
        raise RuntimeError(f"closure blocked: {', '.join(report.blockers)}")
    if not verify(report):
        raise ValueError("report digest mismatch")
    if not actor:
        raise ValueError("actor required")
    closed_at = utc(closed_at)
    raw = json.dumps([report.recall, closed_at.isoformat(), actor, report.digest], separators=(",", ":"))
    digest = hashlib.sha256(raw.encode()).hexdigest()
    return ClosureCertificate(report.recall, closed_at, actor, report.digest, digest)


def verify_certificate(cert: ClosureCertificate) -> bool:
    raw = json.dumps([cert.recall, cert.closed_at.isoformat(), cert.actor, cert.report_digest], separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest() == cert.digest
