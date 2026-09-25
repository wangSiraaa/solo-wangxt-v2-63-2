"""Late approval vs. immutable history (锁定处罚上的迟到批准不被静默撤销).

A retroactively approved pause must never delete escalations or rewrite a
locked penalty.  It raises PENDING_REVIEW compensation suggestions; only a
human confirmation appends a correction, and the original penalty chain is
preserved.
"""

from sla import compensation, escalation, pauses

from tests.helpers import H, T0, ServiceTest


def at_hours(h):
    return (T0 + h * H).isoformat()


class LateApprovalTest(ServiceTest):
    def _build_locked_penalty_history(self):
        """Run a case to level 3 with no pauses and lock the penalty."""
        case_id = self.make_case(sla_hours=24)
        self.at(24)
        escalation.run_case(self.conn, self.clock, case_id)   # L1
        self.at(48)
        escalation.run_case(self.conn, self.clock, case_id)   # L2
        self.at(96)
        escalation.run_case(self.conn, self.clock, case_id)   # L3 -> penalty
        penalty = escalation.list_penalties(self.conn, case_id)[0]
        self.at(100)
        escalation.lock_penalty(self.conn, self.clock, penalty["id"])
        return case_id, penalty["id"]

    def test_late_approval_does_not_silently_revert_locked_penalty(self):
        case_id, _ = self._build_locked_penalty_history()
        before_penalty = escalation.list_penalties(self.conn, case_id)[0]
        before_escalations = escalation.list_escalations(self.conn, case_id)
        self.assertEqual([e["level"] for e in before_escalations], [1, 2, 3])
        self.assertEqual(before_penalty["status"], "LOCKED")

        # The storm started at +10h; the paperwork is approved only at +150h.
        self.at(150)
        p = self.apply_pause(case_id, start_at=at_hours(10))
        pauses.approve_pause(self.conn, self.clock, p["id"])

        # History is untouched: escalations and the locked penalty still stand.
        after_penalty = escalation.list_penalties(self.conn, case_id)[0]
        after_escalations = escalation.list_escalations(self.conn, case_id)
        self.assertEqual(after_penalty["status"], "LOCKED")
        self.assertEqual(after_penalty["amount"], before_penalty["amount"])
        self.assertEqual(after_penalty["locked_at"], before_penalty["locked_at"])
        self.assertEqual(after_penalty["corrections"], [])
        self.assertEqual([e["level"] for e in after_escalations], [1, 2, 3])
        self.assertTrue(all(e["annotations"] == [] for e in after_escalations))

        # Instead, reviewable suggestions were raised for every premature record.
        suggestions = compensation.list_suggestions(self.conn, case_id)
        self.assertEqual(
            sorted((s["target_type"], s["kind"]) for s in suggestions),
            [("ESCALATION", "PREMATURE_ESCALATION")] * 3
            + [("PENALTY", "PREMATURE_PENALTY")],
        )
        self.assertTrue(all(s["status"] == "PENDING_REVIEW" for s in suggestions))
        self.assertTrue(all(s["pause_id"] == p["id"] for s in suggestions))
        self.assert_pause_integrity()

    def test_confirmation_appends_correction_and_preserves_chain(self):
        case_id, _ = self._build_locked_penalty_history()
        self.at(150)
        p = self.apply_pause(case_id, start_at=at_hours(10))
        pauses.approve_pause(self.conn, self.clock, p["id"])
        suggestions = compensation.list_suggestions(self.conn, case_id)
        pen_sug = next(s for s in suggestions if s["target_type"] == "PENALTY")
        esc_sugs = [s for s in suggestions if s["target_type"] == "ESCALATION"]
        esc_sug, dis_sug = esc_sugs[0], esc_sugs[1]

        # Confirm the penalty suggestion: a credit is *appended*; the penalty
        # row itself is untouched (still LOCKED, same amount, same locked_at).
        confirmed = compensation.confirm_suggestion(self.conn, self.clock, pen_sug["id"])
        self.assertEqual(confirmed["status"], "CONFIRMED")
        penalty = escalation.list_penalties(self.conn, case_id)[0]
        self.assertEqual(penalty["status"], "LOCKED")
        self.assertEqual(penalty["amount"], 1000.0)
        self.assertEqual(len(penalty["corrections"]), 1)
        correction = penalty["corrections"][0]
        self.assertEqual(correction["correction_type"], "SLA_PAUSE_CREDIT")
        self.assertEqual(correction["amount_delta"], -1000.0)
        self.assertEqual(correction["suggestion_id"], pen_sug["id"])

        # Confirming again is an idempotent no-op, not a second credit.
        compensation.confirm_suggestion(self.conn, self.clock, pen_sug["id"])
        penalty = escalation.list_penalties(self.conn, case_id)[0]
        self.assertEqual(len(penalty["corrections"]), 1)

        # Confirm an escalation suggestion -> annotation appended, record kept.
        compensation.confirm_suggestion(self.conn, self.clock, esc_sug["id"])
        esc = [e for e in escalation.list_escalations(self.conn, case_id)
               if e["id"] == esc_sug["target_id"]][0]
        self.assertEqual(len(esc["annotations"]), 1)
        self.assertEqual(esc["annotations"][0]["suggestion_id"], esc_sug["id"])

        # Dismiss another one: nothing appended, escalation unchanged.
        dismissed = compensation.dismiss_suggestion(self.conn, self.clock, dis_sug["id"])
        self.assertEqual(dismissed["status"], "DISMISSED")
        esc2 = [e for e in escalation.list_escalations(self.conn, case_id)
                if e["id"] == dis_sug["target_id"]][0]
        self.assertEqual(esc2["annotations"], [])

        # Re-running the engine afterwards changes nothing.
        self.at(400)
        result = escalation.run_case(self.conn, self.clock, case_id)
        self.assertEqual(result["fired_levels"], [])
        self.assertEqual(len(escalation.list_escalations(self.conn, case_id)), 3)
        self.assertEqual(len(escalation.list_penalties(self.conn, case_id)), 1)
        self.assert_pause_integrity()

    def test_pause_shifts_future_escalations_and_compensates_only_premature_ones(self):
        """A retroactive pause compensates records it makes premature and
        shifts later escalations out -- those then fire correctly and need
        no compensation."""
        case_id = self.make_case(sla_hours=24)
        self.at(24)
        escalation.run_case(self.conn, self.clock, case_id)   # L1 at +24h

        # Storm [+20h, +40h), approved late at +30h.
        self.at(30)
        p = self.apply_pause(case_id, start_at=at_hours(20))
        pauses.approve_pause(self.conn, self.clock, p["id"])
        self.at(40)
        pauses.end_pause(self.conn, self.clock, p["id"])

        # Only L1 is premature (recomputed overdue at +24h is -4h); L2 has not
        # fired yet, so there is exactly one suggestion and no penalty record.
        suggestions = compensation.list_suggestions(self.conn, case_id)
        self.assertEqual(len(suggestions), 1)
        self.assertEqual(suggestions[0]["kind"], "PREMATURE_ESCALATION")
        self.assertEqual(escalation.list_penalties(self.conn, case_id), [])

        # The 20h pause shifts the L2 threshold from +48h to +68h.
        self.at(67)
        self.assertEqual(escalation.run_case(self.conn, self.clock, case_id)["fired_levels"], [])
        self.at(68)
        self.assertEqual(escalation.run_case(self.conn, self.clock, case_id)["fired_levels"], [2])
        # L2 fired at the correctly shifted time: still exactly one suggestion.
        self.assertEqual(len(compensation.list_suggestions(self.conn, case_id)), 1)
        self.assert_pause_integrity()


if __name__ == "__main__":
    import unittest
    unittest.main()
