import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from coldledger.recall.acknowledgements import Acknowledgement, AckRegister
from coldledger.recall.closure import ScopeLine, close, evaluate_closure, verify
from coldledger.recall.escalations import EscalationLog, pending_escalations
from coldledger.recall.notice_waves import DeadlineLog, NoticeWave
from coldledger.recall.quantity_ledger import RecallLedger

UTC = timezone.utc
T0 = datetime(2026, 9, 1, 8, tzinfo=UTC)


def wave(wave_id="W1", recipients=("C1", "C2", "C3")):
    return NoticeWave(wave_id, "RC-001", "email", T0, frozenset(recipients))


def ack(wave_id, customer, reference, at):
    return Acknowledgement(wave_id, customer, reference, at)


class DeadlineLogTests(unittest.TestCase):
    def test_revision_preserves_original_decision(self):
        log = DeadlineLog()
        w = wave()
        first = log.issue(w, T0 + timedelta(days=7), "qa", T0)
        second = log.revise("W1", T0 + timedelta(days=14), "qa", T0 + timedelta(days=2), "regulator granted extension")
        self.assertEqual((first.revision, second.revision), (1, 2))
        self.assertEqual(log.current("W1").deadline, T0 + timedelta(days=14))
        self.assertEqual(len(log.history("W1")), 2)
        # the original decision is still reconstructable
        self.assertEqual(log.deadline_at("W1", T0 + timedelta(days=1)).deadline, T0 + timedelta(days=7))
        log.verify()

    def test_revision_requires_reason_and_monotonic_time(self):
        log = DeadlineLog()
        log.issue(wave(), T0 + timedelta(days=7), "qa", T0)
        with self.assertRaises(ValueError):
            log.revise("W1", T0 + timedelta(days=9), "qa", T0 + timedelta(days=1), "")
        with self.assertRaises(ValueError):
            log.revise("W1", T0 + timedelta(days=9), "qa", T0 - timedelta(seconds=1), "backdated")
        with self.assertRaises(KeyError):
            log.revise("W-UNKNOWN", T0, "qa", T0, "reason")

    def test_issue_rejects_reissue_and_early_deadline(self):
        log = DeadlineLog()
        log.issue(wave(), T0 + timedelta(days=7), "qa", T0)
        with self.assertRaises(ValueError):
            log.issue(wave(), T0 + timedelta(days=8), "qa", T0)
        with self.assertRaises(ValueError):
            log.issue(wave("W2"), T0 - timedelta(seconds=1), "qa", T0)


class AckRegisterTests(unittest.TestCase):
    def test_duplicate_receipts_count_once(self):
        register = AckRegister()
        w = wave()
        self.assertTrue(register.record(ack("W1", "C1", "ref-1", T0 + timedelta(hours=1))))
        self.assertFalse(register.record(ack("W1", "C1", "ref-2", T0 + timedelta(hours=2))))
        self.assertFalse(register.record(ack("W1", "C1", "ref-1", T0 + timedelta(hours=3))))
        self.assertEqual(register.acknowledged("W1"), frozenset({"C1"}))
        self.assertAlmostEqual(register.coverage(w), 1 / 3)
        self.assertEqual(register.outstanding(w), frozenset({"C2", "C3"}))
        self.assertEqual(len(register.duplicates()), 2)

    def test_reference_conflict_across_customers_rejected(self):
        register = AckRegister()
        register.record(ack("W1", "C1", "ref-1", T0))
        with self.assertRaises(ValueError):
            register.record(ack("W1", "C2", "ref-1", T0))


class EscalationTests(unittest.TestCase):
    def test_pending_escalations_follow_current_deadline(self):
        w = wave()
        deadline = T0 + timedelta(days=7)
        self.assertEqual(pending_escalations(w, deadline, {"C1"}, deadline), ())
        due = pending_escalations(w, deadline, {"C1"}, deadline + timedelta(hours=2))
        self.assertEqual(due, (("C2", 1), ("C3", 1)))
        due = pending_escalations(w, deadline, {"C1"}, deadline + timedelta(hours=25))
        self.assertEqual(due, (("C2", 2), ("C3", 2)))

    def test_raise_and_resolve(self):
        log = EscalationLog()
        log.raise_escalation("E1", "RC-001", "W1", "C2", 1, "no response past deadline", "qa", T0)
        self.assertEqual([e.escalation_id for e in log.open_escalations("RC-001")], ["E1"])
        resolved = log.resolve("E1", T0 + timedelta(hours=5))
        self.assertFalse(resolved.open)
        self.assertEqual(log.open_escalations("RC-001"), ())
        with self.assertRaises(ValueError):
            log.resolve("E1", T0 + timedelta(hours=6))
        with self.assertRaises(ValueError):
            log.raise_escalation("E1", "RC-001", "W1", "C2", 2, "again", "qa", T0)


class ClosureTests(unittest.TestCase):
    def setUp(self):
        self.w = wave("W1", ("C1", "C2"))
        self.scope = [ScopeLine("C1", "B1", Decimal("100")), ScopeLine("C2", "B1", Decimal("50"))]
        self.deadlines = DeadlineLog()
        self.deadlines.issue(self.w, T0 + timedelta(days=7), "qa", T0)
        self.acks = AckRegister()
        self.ledger = RecallLedger()
        self.escalations = EscalationLog()
        self.now = T0 + timedelta(days=9)

    def evaluate(self, evidence=("CERT-DESTRUCTION-1",)):
        return evaluate_closure("RC-001", self.scope, self.ledger, [self.w], self.deadlines,
                                self.acks, self.escalations, evidence, self.now)

    def test_blocked_report_names_every_gap(self):
        self.acks.record(ack("W1", "C1", "ref-1", T0 + timedelta(days=1)))
        self.ledger.post("C1", "ret-1", Decimal("100"), "returned")
        self.escalations.raise_escalation("E1", "RC-001", "W1", "C2", 1, "silent", "qa", T0 + timedelta(days=8))
        report = self.evaluate(evidence=())
        self.assertFalse(report.closable)
        self.assertIn("outstanding-acknowledgement:W1:C2", report.blockers)
        self.assertIn("open-escalation:E1", report.blockers)
        self.assertIn("quantity-variance:C2:-50", report.blockers)
        self.assertIn("missing-evidence", report.blockers)
        with self.assertRaises(RuntimeError):
            close("RC-001", self.scope, self.ledger, [self.w], self.deadlines,
                  self.acks, self.escalations, (), self.now)

    def test_unexpected_return_blocks(self):
        self.ledger.post("C9", "ret-x", Decimal("5"), "not in scope")
        report = self.evaluate()
        self.assertIn("unexpected-return:C9", report.blockers)

    def test_close_seals_digest_linking_all_inputs(self):
        self.acks.record(ack("W1", "C1", "ref-1", T0 + timedelta(days=1)))
        self.acks.record(ack("W1", "C2", "ref-2", T0 + timedelta(days=2)))
        self.acks.record(ack("W1", "C2", "ref-2b", T0 + timedelta(days=3)))  # duplicate, not counted
        self.ledger.post("C1", "ret-1", Decimal("100"), "returned")
        self.ledger.post("C2", "ret-2", Decimal("50"), "returned")
        self.deadlines.revise("W1", T0 + timedelta(days=10), "qa", T0 + timedelta(days=2), "extension granted")
        self.escalations.raise_escalation("E1", "RC-001", "W1", "C2", 1, "late", "qa", T0 + timedelta(days=8))
        self.escalations.resolve("E1", T0 + timedelta(days=8, hours=2))
        report = self.evaluate()
        self.assertTrue(report.closable)
        self.assertTrue(verify(report))
        # deadline history is bound into the sealed evidence
        self.assertIn(("W1", 2, (T0 + timedelta(days=10)).isoformat()), report.deadline_revisions)
        sealed = close("RC-001", self.scope, self.ledger, [self.w], self.deadlines,
                       self.acks, self.escalations, ("CERT-DESTRUCTION-1",), self.now)
        self.assertEqual(sealed.digest, report.digest)
        self.assertFalse(verify(replace(report, evidence=("FORGED",))))


if __name__ == "__main__":
    unittest.main()
