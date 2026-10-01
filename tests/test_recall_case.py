import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from coldledger.recall.case import RecallCase, account_for
from coldledger.recall.closure import verify, verify_certificate

UTC = timezone.utc
T0 = datetime(2026, 1, 10, 8, 0, tzinfo=UTC)


def hours(n):
    return T0 + timedelta(hours=n)


class RecallCaseTests(unittest.TestCase):
    def setUp(self):
        self.case = RecallCase("RC-2026-001")
        c = self.case
        c.include("B1", "CUST-A", 100, "qa", "initial scope", T0)
        c.include("B1", "CUST-B", 50, "qa", "initial scope", T0)
        c.include("B2", "CUST-A", 40, "qa", "initial scope", T0)
        c.set_deadline("acknowledge", hours(24), "qa", "regulation 24h ack", T0)
        c.set_deadline("return", hours(24 * 14), "qa", "regulation 14d return", T0)
        c.notify("W1", "B1", "CUST-A", hours(1))
        c.notify("W1", "B1", "CUST-B", hours(1))
        c.notify("W1", "B2", "CUST-A", hours(1))

    def test_end_to_end_closure(self):
        c = self.case
        c.acknowledge("ACK-1", "W1", "B1", "CUST-A", 100, hours(2))
        c.acknowledge("ACK-2", "W1", "B1", "CUST-B", 50, hours(3))
        c.acknowledge("ACK-3", "W1", "B2", "CUST-A", 40, hours(4))
        c.book_return("B1", "CUST-A", 100, "RMA-1")
        c.book_return("B1", "CUST-B", 50, "RMA-2")
        c.book_return("B2", "CUST-A", 38, "RMA-3")
        c.book_return("B2", "CUST-A", 2, "RMA-4", reason="destroyed_on_site")
        report = c.closure_report(hours(20))
        self.assertTrue(report.ready, report.blockers)
        self.assertTrue(verify(report))
        cert = c.close("qa-manager", hours(21))
        self.assertTrue(verify_certificate(cert))
        # report generation is deterministic, so the certificate pins exactly
        # the report regenerated for the closing instant
        self.assertEqual(cert.report_digest, c.closure_report(hours(21)).digest)

    def test_duplicate_ack_not_counted(self):
        c = self.case
        ack, counted = c.acknowledge("ACK-1", "W1", "B1", "CUST-A", 100, hours(2))
        self.assertTrue(counted)
        replay, counted = c.acknowledge("ACK-1", "W1", "B1", "CUST-A", 100, hours(2))
        self.assertFalse(counted)
        self.assertEqual(replay, ack)
        dup, counted = c.acknowledge("ACK-1B", "W1", "B1", "CUST-A", 100, hours(3))
        self.assertFalse(counted)
        self.assertEqual(dup.duplicate_of, "ACK-1")
        self.assertEqual(c.acks.acknowledged_quantity(), Decimal("100"))
        self.assertEqual(len(c.acks.duplicates()), 1)
        with self.assertRaises(ValueError):
            c.acknowledge("ACK-1", "W1", "B1", "CUST-A", 5, hours(2))

    def test_deadline_extension_keeps_original_escalation(self):
        c = self.case
        first = c.check_escalations(hours(30))
        self.assertEqual(len(first), 3)
        self.assertTrue(all(e.level == 1 for e in first))
        pinned = first[0].deadline_sequence
        c.set_deadline("acknowledge", hours(72), "qa", "authority granted extension", hours(31))
        self.assertEqual(c.check_escalations(hours(40)), ())
        history = c.deadlines.history("acknowledge")
        self.assertEqual(len(history), 2)
        self.assertEqual(history[1].supersedes, pinned)
        esc = c.escalations.escalations(customer="CUST-A", batch="B1")
        self.assertEqual(len(esc), 1)
        self.assertEqual(esc[0].deadline_sequence, pinned)
        self.assertEqual(c.deadlines.get(pinned).due_at, hours(24))
        second = c.check_escalations(hours(80))
        self.assertEqual(len(second), 3)
        self.assertTrue(all(e.level == 2 and e.deadline_sequence == history[1].sequence for e in second))

    def test_closure_blocked_until_reconciled(self):
        c = self.case
        c.acknowledge("ACK-1", "W1", "B1", "CUST-A", 100, hours(2))
        report = c.closure_report(hours(10))
        self.assertFalse(report.ready)
        self.assertTrue(any(b.startswith("ack-outstanding:B1:CUST-B") for b in report.blockers))
        self.assertTrue(any(b.startswith("quantity-unreconciled:B1:CUST-A") for b in report.blockers))
        with self.assertRaises(RuntimeError):
            c.close("qa-manager", hours(10))

    def test_waiver_clears_ack_blocker_but_not_quantity(self):
        c = self.case
        c.waive("B1", "CUST-B", "qa", "customer out of business, stock seized", hours(5))
        c.acknowledge("ACK-1", "W1", "B1", "CUST-A", 100, hours(2))
        c.acknowledge("ACK-3", "W1", "B2", "CUST-A", 40, hours(2))
        c.book_return("B1", "CUST-A", 100, "RMA-1")
        c.book_return("B2", "CUST-A", 40, "RMA-3")
        report = c.closure_report(hours(10))
        self.assertFalse(report.ready)
        self.assertEqual(report.blockers, ("quantity-unreconciled:B1:CUST-B:50",))
        c.book_return("B1", "CUST-B", 50, "RMA-2", reason="seized_by_authority")
        self.assertTrue(c.closure_report(hours(11)).ready)

    def test_notice_requires_scope_and_is_idempotent(self):
        c = self.case
        with self.assertRaises(ValueError):
            c.notify("W1", "B9", "CUST-Z", hours(1))
        notice, created = c.notify("W2", "B1", "CUST-A", hours(6))
        self.assertTrue(created)
        again, created = c.notify("W2", "B1", "CUST-A", hours(6))
        self.assertFalse(created)
        self.assertEqual(again.reference, notice.reference)
        self.assertEqual(len(c.notices.wave("W2")), 1)

    def test_return_cannot_exceed_scope(self):
        c = self.case
        c.book_return("B1", "CUST-A", 60, "RMA-1")
        with self.assertRaises(ValueError):
            c.book_return("B1", "CUST-A", 41, "RMA-2")
        self.assertEqual(c.quantities.balance(account_for("B1", "CUST-A")), Decimal("60"))

    def test_scope_exclusion_is_append_only(self):
        c = self.case
        c.exclude("B2", "CUST-A", "qa", "batch B2 released from recall", hours(2))
        self.assertEqual(c.scope.quantity(), Decimal("150"))
        self.assertEqual(len(c.scope.revisions()), 4)
        self.assertEqual(len(c.scope.at(T0 + timedelta(minutes=30))), 3)
        with self.assertRaises(ValueError):
            c.include("B1", "CUST-A", 10, "qa", "duplicate", hours(3))


if __name__ == "__main__":
    unittest.main()
