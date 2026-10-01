from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable
import hashlib
import json

from .acknowledgements import AckRegister
from .escalations import EscalationLog
from .notice_waves import DeadlineLog, NoticeWave
from .quantity_ledger import RecallLedger


@dataclass(frozen=True)
class ScopeLine:
    customer: str
    batch: str
    quantity: Decimal


@dataclass(frozen=True)
class ClosureReport:
    """Links scope, notifications, escalations, quantities and evidence into
    one digest-sealed answer to "why can this recall case be closed"."""
    recall_reference: str
    generated_at: datetime
    expected: tuple[tuple[str, str], ...]
    returned: tuple[tuple[str, str], ...]
    variance: tuple[tuple[str, str], ...]
    outstanding: tuple[tuple[str, tuple[str, ...]], ...]
    open_escalations: tuple[str, ...]
    deadline_revisions: tuple[tuple[str, int, str], ...]
    evidence: tuple[str, ...]
    blockers: tuple[str, ...]
    digest: str

    @property
    def closable(self) -> bool:
        return not self.blockers


def _digest(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _report_payload(report: ClosureReport) -> dict:
    return {
        "reference": report.recall_reference,
        "generated_at": report.generated_at.astimezone(timezone.utc).isoformat(),
        "expected": [list(r) for r in report.expected],
        "returned": [list(r) for r in report.returned],
        "variance": [list(r) for r in report.variance],
        "outstanding": [[w, list(m)] for w, m in report.outstanding],
        "open_escalations": list(report.open_escalations),
        "deadline_revisions": [list(r) for r in report.deadline_revisions],
        "evidence": list(report.evidence),
        "blockers": list(report.blockers),
    }


def evaluate_closure(reference: str,
                     scope: Iterable[ScopeLine],
                     ledger: RecallLedger,
                     waves: Iterable[NoticeWave],
                     deadlines: DeadlineLog,
                     acknowledgements: AckRegister,
                     escalations: EscalationLog,
                     evidence: Iterable[str],
                     now: datetime,
                     tolerance=Decimal("0")) -> ClosureReport:
    now = now.astimezone(timezone.utc)
    tolerance = Decimal(str(tolerance))
    if tolerance < 0:
        raise ValueError("negative tolerance")

    expected: dict[str, Decimal] = {}
    for line in scope:
        q = Decimal(str(line.quantity))
        if not line.customer:
            raise ValueError("scope customer required")
        if q <= 0:
            raise ValueError("scope quantity must be positive")
        expected[line.customer] = expected.get(line.customer, Decimal("0")) + q

    relevant = sorted(
        {w.wave_id: w for w in waves if w.recall_reference == reference}.values(),
        key=lambda w: w.wave_id,
    )

    customers = sorted(set(expected) | {r.account for r in ledger.entries()})
    returned = {c: ledger.balance(c) for c in customers}
    variance = {c: returned[c] - expected.get(c, Decimal("0")) for c in customers}

    blockers: list[str] = []
    outstanding: dict[str, tuple[str, ...]] = {}
    revision_rows: list[tuple[str, int, str]] = []
    for wave in relevant:
        history = deadlines.history(wave.wave_id)
        if not history:
            blockers.append(f"wave-without-deadline:{wave.wave_id}")
            revision_rows.append((wave.wave_id, 0, ""))
        else:
            current = history[-1]
            revision_rows.append((wave.wave_id, len(history), current.deadline.astimezone(timezone.utc).isoformat()))
        missing = tuple(sorted(acknowledgements.outstanding(wave)))
        outstanding[wave.wave_id] = missing
        for customer in missing:
            blockers.append(f"outstanding-acknowledgement:{wave.wave_id}:{customer}")

    open_escalations = tuple(sorted(e.escalation_id for e in escalations.entries(reference) if e.open))
    for escalation_id in open_escalations:
        blockers.append(f"open-escalation:{escalation_id}")

    for customer in customers:
        if customer not in expected:
            if returned[customer] != 0:
                blockers.append(f"unexpected-return:{customer}")
        elif abs(variance[customer]) > tolerance:
            blockers.append(f"quantity-variance:{customer}:{variance[customer]}")

    evidence = tuple(sorted(set(evidence)))
    if not evidence:
        blockers.append("missing-evidence")
    if not relevant:
        blockers.append("no-notice-waves")
    if not expected:
        blockers.append("empty-scope")

    payload = {
        "reference": reference,
        "generated_at": now.isoformat(),
        "expected": sorted((c, str(q)) for c, q in expected.items()),
        "returned": [(c, str(returned[c])) for c in customers],
        "variance": [(c, str(variance[c])) for c in customers],
        "outstanding": [[w, list(m)] for w, m in sorted(outstanding.items())],
        "open_escalations": list(open_escalations),
        "deadline_revisions": [list(r) for r in revision_rows],
        "evidence": list(evidence),
        "blockers": blockers,
    }
    return ClosureReport(
        recall_reference=reference,
        generated_at=now,
        expected=tuple((c, str(q)) for c, q in sorted(expected.items())),
        returned=tuple((c, str(returned[c])) for c in customers),
        variance=tuple((c, str(variance[c])) for c in customers),
        outstanding=tuple((w, outstanding[w]) for w in sorted(outstanding)),
        open_escalations=open_escalations,
        deadline_revisions=tuple(revision_rows),
        evidence=evidence,
        blockers=tuple(blockers),
        digest=_digest(payload),
    )


def verify(report: ClosureReport) -> bool:
    return report.digest == _digest(_report_payload(report))


def close(reference: str,
          scope: Iterable[ScopeLine],
          ledger: RecallLedger,
          waves: Iterable[NoticeWave],
          deadlines: DeadlineLog,
          acknowledgements: AckRegister,
          escalations: EscalationLog,
          evidence: Iterable[str],
          now: datetime,
          tolerance=Decimal("0")) -> ClosureReport:
    """Seal the case file; refuses while any blocker remains."""
    report = evaluate_closure(reference, scope, ledger, waves, deadlines,
                              acknowledgements, escalations, evidence, now, tolerance)
    if report.blockers:
        raise RuntimeError("recall case not closable: " + "; ".join(report.blockers))
    return report
