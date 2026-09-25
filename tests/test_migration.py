"""Failure- and restart-safety (审批失败、重启重跑和旧事件迁移不留下半截停表).

* a failing approval rolls back completely -- no half stopwatch;
* re-running migrations, the engine and approval retries are idempotent,
  including across a genuine process restart (file-backed database);
* migration 002 converts legacy ad-hoc holds into proper, complete pause
  records, and re-running it converts nothing twice.
"""

import os
import tempfile
import unittest
from datetime import timedelta
from unittest import mock

from sla import cases, escalation, events, pauses
from sla.api import App
from sla.clock import FrozenClock, iso
from sla.db import connect
from sla.errors import Conflict
from sla.migrations import migrate

from tests.helpers import T0, ServiceTest


class ApprovalFailureTest(ServiceTest):
    def test_failed_approval_leaves_no_half_stopwatch(self):
        case_id = self.make_case()
        p = self.apply_pause(case_id)

        # Force a failure *inside* the approval transaction (after the state
        # transition and interval insert) and verify everything rolls back.
        with mock.patch("sla.pauses._scan_premature_records",
                        side_effect=RuntimeError("simulated crash")):
            with self.assertRaises(RuntimeError):
                pauses.approve_pause(self.conn, self.clock, p["id"])

        reloaded = pauses.get_pause(self.conn, p["id"])
        self.assertEqual(reloaded["state"], "PENDING")
        self.assertIsNone(reloaded["approved_at"])
        self.assertIsNone(reloaded["interval"])
        self.assertEqual(reloaded["version"], 0)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM pause_intervals").fetchone()["n"], 0)
        self.assert_pause_integrity()

        # The retry succeeds cleanly -- a failed attempt never poisons the pause.
        approved = pauses.approve_pause(self.conn, self.clock, p["id"])
        self.assertEqual(approved["state"], "APPROVED")
        self.assertIsNotNone(approved["interval"])
        self.assert_pause_integrity()

    def test_approval_refused_after_case_closed_leaves_no_trace(self):
        case_id = self.make_case()
        p = self.apply_pause(case_id)
        cases.rectify_case(self.conn, self.clock, case_id)
        with self.assertRaises(Conflict):
            pauses.approve_pause(self.conn, self.clock, p["id"])
        reloaded = pauses.get_pause(self.conn, p["id"])
        self.assertEqual(reloaded["state"], "PENDING")
        self.assertIsNone(reloaded["interval"])
        self.assert_pause_integrity()


class RestartRerunTest(unittest.TestCase):
    def test_restart_catchup_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "sla.db")
            clock = FrozenClock(T0)

            # First "process": migrate, create a case, escalate, pause.
            app1 = App(db_path, clock=clock)
            case_id = cases.create_case(app1.conn, clock, "fix it", 24 * 3600)["id"]
            clock.advance(hours=24)
            escalation.run_case(app1.conn, clock, case_id)
            event_id = events.create_event(
                app1.conn, clock, "RAINSTORM", "storm", iso(clock.now()))["id"]
            p = pauses.apply_pause(app1.conn, clock, case_id, event_id,
                                   "storm", ["photo://1"], iso(clock.now()))
            pauses.approve_pause(app1.conn, clock, p["id"])
            clock.advance(hours=3)
            pauses.end_pause(app1.conn, clock, p["id"])
            esc_before = escalation.list_escalations(app1.conn, case_id)
            app1.conn.close()

            # Second "process" on the same file: migrations are a no-op and
            # the catch-up engine run records nothing twice.
            app2 = App(db_path, clock=clock)
            self.assertEqual(migrate(app2.conn, clock), [])
            clock.advance(hours=100)
            escalation.run_all_open_cases(app2.conn, clock)
            escalation.run_all_open_cases(app2.conn, clock)  # run it twice
            esc_after = escalation.list_escalations(app2.conn, case_id)
            self.assertEqual([e["level"] for e in esc_after], [1, 2, 3])
            self.assertEqual(esc_after[0]["id"], esc_before[0]["id"])  # L1 kept, not duplicated
            escalation.run_all_open_cases(app2.conn, clock)  # and once more for luck
            self.assertEqual(len(escalation.list_escalations(app2.conn, case_id)), 3)
            self.assertEqual(pauses.integrity_violations(app2.conn), [])
            app2.conn.close()


class LegacyMigrationTest(unittest.TestCase):
    def _v1_database(self):
        """A database stopped at migration 001 with legacy rows."""
        conn = connect(":memory:")
        clock = FrozenClock(T0)
        migrate(conn, clock, upto="001_core")
        conn.execute(
            "INSERT INTO rectification_cases (id, title, opened_at, sla_seconds, status,"
            " legacy_hold_seconds) VALUES ('case_legacy', 'old case', ?, 86400, 'OPEN', 7200)",
            (iso(T0),),
        )
        conn.execute(
            "INSERT INTO rectification_cases (id, title, opened_at, sla_seconds, status)"
            " VALUES ('case_plain', 'new case', ?, 86400, 'OPEN')",
            (iso(T0),),
        )
        return conn, clock

    def test_legacy_holds_convert_to_complete_pause_records(self):
        conn, clock = self._v1_database()
        applied = migrate(conn, clock)
        self.assertEqual(applied, ["002_sla_pauses"])

        pause = conn.execute(
            "SELECT * FROM sla_pauses WHERE case_id = 'case_legacy'").fetchone()
        self.assertIsNotNone(pause)
        self.assertEqual(pause["state"], "ENDED")
        self.assertEqual(pause["start_at"], iso(T0))
        self.assertEqual(pause["ended_at"], iso(T0 + timedelta(hours=2)))
        event = conn.execute(
            "SELECT * FROM events WHERE id = ?", (pause["event_id"],)).fetchone()
        self.assertEqual(event["event_type"], "LEGACY_MIGRATION")
        interval = conn.execute(
            "SELECT * FROM pause_intervals WHERE pause_id = ?", (pause["id"],)).fetchone()
        self.assertEqual(interval["end_at"], pause["ended_at"])  # no open half
        migrated = conn.execute(
            "SELECT legacy_hold_migrated FROM rectification_cases WHERE id = 'case_legacy'"
        ).fetchone()["legacy_hold_migrated"]
        self.assertEqual(migrated, 1)

        # The converted hold actually deducts time: at +10h wall the legacy
        # case shows 8h effective, the untouched case 10h.
        clock.advance(hours=10)
        s_legacy = escalation.case_status(conn, clock, "case_legacy")
        s_plain = escalation.case_status(conn, clock, "case_plain")
        self.assertEqual(s_legacy["paused_seconds"], 7200)
        self.assertEqual(s_legacy["effective_elapsed_seconds"], 8 * 3600)
        self.assertEqual(s_plain["effective_elapsed_seconds"], 10 * 3600)
        self.assertEqual(pauses.integrity_violations(conn), [])

    def test_migration_rerun_converts_nothing_twice(self):
        conn, clock = self._v1_database()
        migrate(conn, clock)
        counts_before = {
            t: conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
            for t in ("sla_pauses", "pause_intervals", "events")
        }
        self.assertEqual(migrate(conn, clock), [])  # restart re-run
        for table, n in counts_before.items():
            self.assertEqual(
                conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"], n)
        self.assertEqual(pauses.integrity_violations(conn), [])


if __name__ == "__main__":
    unittest.main()
