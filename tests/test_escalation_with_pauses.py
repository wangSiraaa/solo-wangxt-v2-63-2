"""Escalation engine with pauses.

Covers the acceptance criteria:
* remaining time resumes correctly after approval (批准后剩余时长正确续算);
* overlapping pauses and duplicate end requests never double-count
  (重叠暂停和重复结束请求不双算);
* a case rectified during a pause never escalates again
  (暂停中整改后不再升级).
"""

from sla import cases, escalation, pauses
from sla.errors import Conflict

from tests.helpers import H, T0, ServiceTest


class RemainingTimeResumesTest(ServiceTest):
    """批准后剩余时长正确续算: the stopwatch stops while an approved pause
    runs and resumes afterwards, with retroactive effect to start_at."""

    def test_remaining_time_resumes_correctly_after_approval(self):
        case_id = self.make_case(sla_hours=72)

        self.at(10)
        p = self.apply_pause(case_id, start_at=self.clock.now().isoformat())
        self.at(12)  # approval lands 2h after the pause started
        pauses.approve_pause(self.conn, self.clock, p["id"])

        # While the pause runs the effective clock is frozen: remaining time
        # at +20h equals remaining time at +30h.
        self.at(20)
        s20 = escalation.case_status(self.conn, self.clock, case_id)
        self.at(30)
        s30 = escalation.case_status(self.conn, self.clock, case_id)
        self.assertEqual(s20["effective_elapsed_seconds"], 10 * 3600)
        self.assertEqual(s20["remaining_seconds"], 62 * 3600)
        self.assertEqual(s30["remaining_seconds"], s20["remaining_seconds"])

        pauses.end_pause(self.conn, self.clock, p["id"])  # pause [10h, 30h) = 20h

        # At +80h: 80h wall - 20h paused = 60h effective -> 12h remaining.
        self.at(80)
        status = escalation.case_status(self.conn, self.clock, case_id)
        self.assertEqual(status["paused_seconds"], 20 * 3600)
        self.assertEqual(status["effective_elapsed_seconds"], 60 * 3600)
        self.assertEqual(status["remaining_seconds"], 12 * 3600)
        self.assertEqual(status["current_level"], 0)

        result = escalation.run_case(self.conn, self.clock, case_id)
        self.assertEqual(result["fired_levels"], [])  # 60h < 72h SLA

        # Effective deadline is wall +92h (72h SLA + 20h paused).
        self.at(91)
        self.assertEqual(escalation.run_case(self.conn, self.clock, case_id)["fired_levels"], [])
        self.at(92)
        self.assertEqual(escalation.run_case(self.conn, self.clock, case_id)["fired_levels"], [1])
        self.at(92 + 24)
        self.assertEqual(escalation.run_case(self.conn, self.clock, case_id)["fired_levels"], [2])
        self.assert_pause_integrity()

    def test_engine_does_not_escalate_while_paused(self):
        case_id = self.make_case(sla_hours=10)
        self.at(2)
        p = self.apply_pause(case_id)
        pauses.approve_pause(self.conn, self.clock, p["id"])
        self.at(100)  # wall clock far beyond the SLA, but the clock is stopped
        result = escalation.run_case(self.conn, self.clock, case_id)
        self.assertEqual(result["fired_levels"], [])
        status = escalation.case_status(self.conn, self.clock, case_id)
        self.assertEqual(status["effective_elapsed_seconds"], 2 * 3600)
        # End the pause: the clock resumes and the SLA is breached 8h later.
        pauses.end_pause(self.conn, self.clock, p["id"])
        self.at(100 + 8)
        self.assertEqual(escalation.run_case(self.conn, self.clock, case_id)["fired_levels"], [1])
        self.assert_pause_integrity()


class OverlapAndDuplicateEndTest(ServiceTest):
    """重叠暂停和重复结束请求不双算."""

    def test_overlapping_pauses_deduct_the_union_not_the_sum(self):
        case_id = self.make_case(sla_hours=72)
        self.at(10)
        a = self.apply_pause(case_id)
        pauses.approve_pause(self.conn, self.clock, a["id"])
        self.at(20)
        b = self.apply_pause(case_id)
        pauses.approve_pause(self.conn, self.clock, b["id"])
        self.at(25)
        c = self.apply_pause(case_id)  # fully contained in A
        pauses.approve_pause(self.conn, self.clock, c["id"])
        self.at(30)
        pauses.end_pause(self.conn, self.clock, c["id"])  # C = [25,30)
        self.at(40)
        pauses.end_pause(self.conn, self.clock, a["id"])  # A = [10,40)
        self.at(50)
        pauses.end_pause(self.conn, self.clock, b["id"])  # B = [20,50)

        self.at(60)
        status = escalation.case_status(self.conn, self.clock, case_id)
        # Sum of individual pauses would be 30+30+5 = 65h; the union is 40h.
        self.assertEqual(status["paused_seconds"], 40 * 3600)
        self.assertEqual(status["effective_elapsed_seconds"], 20 * 3600)
        self.assertEqual(status["pause_intervals_union"],
                         [{"start_at": (T0 + 10 * H).isoformat().replace("+00:00", "Z"),
                           "end_at": (T0 + 50 * H).isoformat().replace("+00:00", "Z")}])
        self.assert_pause_integrity()

    def test_duplicate_end_request_does_not_extend_the_pause(self):
        case_id = self.make_case(sla_hours=72)
        self.at(10)
        p = self.apply_pause(case_id)
        pauses.approve_pause(self.conn, self.clock, p["id"])
        self.at(30)
        first = pauses.end_pause(self.conn, self.clock, p["id"])
        self.at(45)  # a late duplicate end request must not move ended_at
        second = pauses.end_pause(self.conn, self.clock, p["id"])
        self.assertEqual(first["ended_at"], second["ended_at"])
        status = escalation.case_status(self.conn, self.clock, case_id)
        self.assertEqual(status["paused_seconds"], 20 * 3600)  # [10,30), not [10,45)
        self.assert_pause_integrity()


class RectifiedDuringPauseTest(ServiceTest):
    """暂停中整改后不再升级: closure during a pause freezes everything."""

    def test_rectified_during_pause_never_escalates_again(self):
        case_id = self.make_case(sla_hours=24)
        self.at(2)
        p = self.apply_pause(case_id)
        pauses.approve_pause(self.conn, self.clock, p["id"])
        pending = self.apply_pause(case_id)  # still awaiting approval

        self.at(5)
        cases.rectify_case(self.conn, self.clock, case_id)

        # The running pause was ended by the closure, in the same transaction.
        p = pauses.get_pause(self.conn, p["id"])
        self.assertEqual(p["state"], "ENDED")
        self.assertEqual(p["ended_at"], (T0 + 5 * H).isoformat().replace("+00:00", "Z"))

        self.at(500)
        result = escalation.run_case(self.conn, self.clock, case_id)
        self.assertEqual(result["skipped"], "CLOSED")
        self.assertEqual(result["fired_levels"], [])
        self.assertEqual(escalation.list_escalations(self.conn, case_id), [])

        # The clock cannot be restarted: no new application, no late approval.
        with self.assertRaises(Conflict) as ctx:
            self.apply_pause(case_id)
        self.assertEqual(ctx.exception.code, "CASE_CLOSED")
        with self.assertRaises(Conflict) as ctx:
            pauses.approve_pause(self.conn, self.clock, pending["id"])
        self.assertEqual(ctx.exception.code, "CASE_CLOSED")

        # Accounting is frozen at closure time.
        status = escalation.case_status(self.conn, self.clock, case_id)
        self.assertEqual(status["as_of"], (T0 + 5 * H).isoformat().replace("+00:00", "Z"))
        self.assertEqual(status["effective_elapsed_seconds"], 2 * 3600)
        self.assert_pause_integrity()


if __name__ == "__main__":
    import unittest
    unittest.main()
