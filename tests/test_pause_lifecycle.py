"""Pause lifecycle: state machine, validation, idempotent transitions."""

from sla import cases, pauses
from sla.errors import Conflict, NotFound, Validation

from tests.helpers import H, T0, ServiceTest


class ApplyValidationTest(ServiceTest):
    def test_application_binds_event_reason_and_evidence(self):
        case_id = self.make_case()
        event_id = self.make_event()
        p = self.apply_pause(case_id, event_id)
        self.assertEqual(p["state"], "PENDING")
        self.assertEqual(p["event_id"], event_id)
        self.assertEqual(p["reason"], "storm")
        self.assertEqual(p["evidence"], ["photo://site/1", "meteo://alert/7"])
        self.assertIsNone(p["interval"])  # no stopwatch before approval
        self.assert_pause_integrity()

    def test_event_must_exist(self):
        case_id = self.make_case()
        with self.assertRaises(NotFound):
            self.apply_pause(case_id, event_id="evt_missing")

    def test_reason_and_evidence_are_required(self):
        case_id = self.make_case()
        with self.assertRaises(Validation):
            self.apply_pause(case_id, reason="")
        with self.assertRaises(Validation):
            self.apply_pause(case_id, evidence=[])
        with self.assertRaises(Validation):
            self.apply_pause(case_id, evidence=["  "])

    def test_start_at_cannot_be_future_or_before_case_opening(self):
        case_id = self.make_case()
        with self.assertRaises(Validation):
            self.apply_pause(case_id, start_at=(self.clock.now() + H).isoformat())
        with self.assertRaises(Validation):
            self.apply_pause(case_id, start_at=(T0 - H).isoformat())

    def test_apply_on_closed_case_is_rejected(self):
        case_id = self.make_case()
        cases.rectify_case(self.conn, self.clock, case_id)
        with self.assertRaises(Conflict) as ctx:
            self.apply_pause(case_id)
        self.assertEqual(ctx.exception.code, "CASE_CLOSED")


class StateMachineTest(ServiceTest):
    def test_full_happy_path(self):
        case_id = self.make_case()
        p = self.apply_pause(case_id)
        p = pauses.approve_pause(self.conn, self.clock, p["id"], note="verified")
        self.assertEqual(p["state"], "APPROVED")
        self.assertIsNotNone(p["approved_at"])
        self.assertIsNotNone(p["interval"])
        self.assertIsNone(p["interval"]["end_at"])
        self.clock.advance(hours=5)
        p = pauses.end_pause(self.conn, self.clock, p["id"])
        self.assertEqual(p["state"], "ENDED")
        self.assertEqual(p["interval"]["end_at"], p["ended_at"])
        self.assert_pause_integrity()

    def test_reject_and_revoke_from_pending(self):
        case_id = self.make_case()
        p1 = pauses.reject_pause(self.conn, self.clock, self.apply_pause(case_id)["id"])
        p2 = pauses.revoke_pause(self.conn, self.clock, self.apply_pause(case_id)["id"])
        self.assertEqual(p1["state"], "REJECTED")
        self.assertEqual(p2["state"], "REVOKED")
        self.assert_pause_integrity()

    def test_terminal_states_reject_further_transitions(self):
        case_id = self.make_case()
        rejected = pauses.reject_pause(self.conn, self.clock, self.apply_pause(case_id)["id"])
        revoked = pauses.revoke_pause(self.conn, self.clock, self.apply_pause(case_id)["id"])
        ended = self.apply_pause(case_id)
        pauses.approve_pause(self.conn, self.clock, ended["id"])
        ended = pauses.end_pause(self.conn, self.clock, ended["id"])
        # Cross-state transitions are rejected...
        with self.assertRaises(Conflict):
            pauses.approve_pause(self.conn, self.clock, rejected["id"])
        with self.assertRaises(Conflict):
            pauses.end_pause(self.conn, self.clock, rejected["id"])
        with self.assertRaises(Conflict):
            pauses.revoke_pause(self.conn, self.clock, rejected["id"])
        with self.assertRaises(Conflict):
            pauses.approve_pause(self.conn, self.clock, revoked["id"])
        with self.assertRaises(Conflict):
            pauses.end_pause(self.conn, self.clock, revoked["id"])
        with self.assertRaises(Conflict):
            pauses.reject_pause(self.conn, self.clock, revoked["id"])
        with self.assertRaises(Conflict):
            pauses.approve_pause(self.conn, self.clock, ended["id"])
        with self.assertRaises(Conflict):
            pauses.reject_pause(self.conn, self.clock, ended["id"])
        with self.assertRaises(Conflict):
            pauses.revoke_pause(self.conn, self.clock, ended["id"])
        # ...while repeating the transition that produced the state is the
        # idempotent retry path (duplicate requests must not fail).
        self.assertEqual(pauses.reject_pause(self.conn, self.clock, rejected["id"])["state"],
                         "REJECTED")
        self.assertEqual(pauses.revoke_pause(self.conn, self.clock, revoked["id"])["state"],
                         "REVOKED")
        again = pauses.end_pause(self.conn, self.clock, ended["id"])
        self.assertEqual(again["ended_at"], ended["ended_at"])
        self.assert_pause_integrity()

    def test_end_requires_active_pause(self):
        case_id = self.make_case()
        p = self.apply_pause(case_id)
        with self.assertRaises(Conflict) as ctx:
            pauses.end_pause(self.conn, self.clock, p["id"])
        self.assertEqual(ctx.exception.code, "PAUSE_NOT_ACTIVE")

    def test_idempotent_retries_return_same_record(self):
        case_id = self.make_case()
        p = self.apply_pause(case_id)
        a1 = pauses.approve_pause(self.conn, self.clock, p["id"])
        a2 = pauses.approve_pause(self.conn, self.clock, p["id"])  # retry
        self.assertEqual(a1, a2)
        e1 = pauses.end_pause(self.conn, self.clock, p["id"])
        e2 = pauses.end_pause(self.conn, self.clock, p["id"])  # retry
        self.assertEqual(e1["ended_at"], e2["ended_at"])
        n = self.conn.execute(
            "SELECT COUNT(*) AS n FROM pause_intervals WHERE pause_id = ?", (p["id"],)
        ).fetchone()["n"]
        self.assertEqual(n, 1)  # retried approval created exactly one stopwatch
        r1 = pauses.reject_pause(self.conn, self.clock, self.apply_pause(case_id)["id"])
        r2 = pauses.reject_pause(self.conn, self.clock, r1["id"])
        self.assertEqual(r1, r2)
        self.assert_pause_integrity()

    def test_unknown_pause_404s(self):
        with self.assertRaises(NotFound):
            pauses.get_pause(self.conn, "pause_missing")


if __name__ == "__main__":
    import unittest
    unittest.main()
