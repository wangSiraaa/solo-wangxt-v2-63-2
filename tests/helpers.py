"""Shared fixtures: an in-memory app on a frozen clock."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from sla import cases, events, pauses
from sla.api import App
from sla.clock import FrozenClock

T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
H = timedelta(hours=1)


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.clock = FrozenClock(T0)
        self.app = App(":memory:", clock=self.clock)
        self.conn = self.app.conn

    # -- factories ---------------------------------------------------------
    def make_case(self, sla_hours=72, title="fix the drainage"):
        case = cases.create_case(self.conn, self.clock, title, sla_hours * 3600)
        return case["id"]

    def make_event(self, event_type="RAINSTORM", occurred_at=None):
        return events.create_event(
            self.conn, self.clock, event_type, "recorded by field team",
            occurred_at or self.clock.now().isoformat(),
        )["id"]

    def apply_pause(self, case_id, event_id=None, start_at=None, reason="storm",
                    evidence=("photo://site/1", "meteo://alert/7")):
        return pauses.apply_pause(
            self.conn, self.clock, case_id, event_id or self.make_event(),
            reason, list(evidence), start_at or self.clock.now().isoformat(),
        )

    # -- invariant ---------------------------------------------------------
    def assert_pause_integrity(self):
        violations = pauses.integrity_violations(self.conn)
        self.assertEqual(violations, [], "half-finished stopwatch detected")

    def at(self, hours):
        """Advance the frozen clock to ``hours`` after T0."""
        self.clock.set(T0 + hours * H)
