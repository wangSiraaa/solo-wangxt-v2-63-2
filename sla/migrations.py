"""Versioned, transactional, restart-safe migrations.

The runner records applied versions in ``schema_migrations`` and executes
each pending migration inside a single transaction, so a crash mid-migration
cannot leave half-applied DDL or half-converted rows.  Re-running the runner
(on every service start) is a no-op once everything is applied.

History
-------
001_core
    The pre-existing ("legacy") escalation system: cases, escalations,
    penalties.  Cases could carry an ad-hoc ``legacy_hold_seconds`` -- a bare
    number with no event, reason, evidence or audit trail.

002_sla_pauses
    Adds the auditable pause ledger (events, sla_pauses, pause_intervals,
    compensation_suggestions, penalty_corrections, escalation_annotations)
    and, in the *same* transaction, converts every legacy hold into a proper
    ENDED pause record anchored at the case opening (the legacy data carries
    no timing information, so anchoring at ``opened_at`` is the documented
    convention).  The conversion is guarded by ``legacy_hold_migrated`` so
    re-running can never duplicate a converted stopwatch.
"""

from __future__ import annotations

from datetime import timedelta

from .clock import iso, parse
from .db import new_id, tx

MIGRATION_001 = """
CREATE TABLE rectification_cases (
    id                    TEXT PRIMARY KEY,
    title                 TEXT NOT NULL,
    opened_at             TEXT NOT NULL,
    sla_seconds           INTEGER NOT NULL,
    status                TEXT NOT NULL DEFAULT 'OPEN'
                          CHECK (status IN ('OPEN', 'CLOSED')),
    closed_reason         TEXT CHECK (closed_reason IN ('RECTIFIED', 'CANCELLED')),
    closed_at             TEXT,
    legacy_hold_seconds   INTEGER NOT NULL DEFAULT 0,
    legacy_hold_migrated  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE escalations (
    id                          TEXT PRIMARY KEY,
    case_id                     TEXT NOT NULL REFERENCES rectification_cases(id),
    level                       INTEGER NOT NULL,
    triggered_at                TEXT NOT NULL,
    effective_overdue_seconds   REAL NOT NULL,
    created_at                  TEXT NOT NULL,
    UNIQUE (case_id, level)
);

CREATE TABLE penalties (
    id              TEXT PRIMARY KEY,
    case_id         TEXT NOT NULL REFERENCES rectification_cases(id),
    escalation_id   TEXT NOT NULL REFERENCES escalations(id),
    amount          REAL NOT NULL,
    status          TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'LOCKED')),
    locked_at       TEXT,
    created_at      TEXT NOT NULL,
    UNIQUE (case_id, escalation_id)
);
"""

MIGRATION_002_DDL = """
CREATE TABLE events (
    id           TEXT PRIMARY KEY,
    event_type   TEXT NOT NULL,
    description  TEXT NOT NULL,
    occurred_at  TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE sla_pauses (
    id             TEXT PRIMARY KEY,
    case_id        TEXT NOT NULL REFERENCES rectification_cases(id),
    event_id       TEXT NOT NULL REFERENCES events(id),
    reason         TEXT NOT NULL,
    evidence       TEXT NOT NULL,               -- JSON array of evidence refs
    state          TEXT NOT NULL
                   CHECK (state IN ('PENDING', 'APPROVED', 'REJECTED', 'ENDED', 'REVOKED')),
    start_at       TEXT NOT NULL,               -- evidence-backed start, may be retroactive
    approved_at    TEXT,
    ended_at       TEXT,
    decision_note  TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    version        INTEGER NOT NULL DEFAULT 0   -- optimistic concurrency guard
);
CREATE INDEX idx_sla_pauses_case ON sla_pauses(case_id);

-- Exactly one interval row exists per pause that was ever approved; it is
-- inserted in the same transaction as the PENDING -> APPROVED transition and
-- closed in the same transaction as APPROVED -> ENDED.  end_at NULL means the
-- stopwatch is currently stopped.
CREATE TABLE pause_intervals (
    pause_id   TEXT PRIMARY KEY REFERENCES sla_pauses(id),
    case_id    TEXT NOT NULL,
    start_at   TEXT NOT NULL,
    end_at     TEXT
);
CREATE INDEX idx_pause_intervals_case ON pause_intervals(case_id);

CREATE TABLE compensation_suggestions (
    id           TEXT PRIMARY KEY,
    case_id      TEXT NOT NULL,
    pause_id     TEXT NOT NULL REFERENCES sla_pauses(id),
    target_type  TEXT NOT NULL CHECK (target_type IN ('ESCALATION', 'PENALTY')),
    target_id    TEXT NOT NULL,
    kind         TEXT NOT NULL,
    detail       TEXT NOT NULL,                 -- JSON: why the record is premature
    status       TEXT NOT NULL DEFAULT 'PENDING_REVIEW'
                 CHECK (status IN ('PENDING_REVIEW', 'CONFIRMED', 'DISMISSED')),
    created_at   TEXT NOT NULL,
    resolved_at  TEXT,
    UNIQUE (pause_id, target_type, target_id)   -- re-approval / re-scan is idempotent
);

-- Append-only corrections.  The referenced penalty row is never mutated, so
-- the original penalty chain (penalty -> corrections) stays intact even when
-- the penalty is LOCKED.
CREATE TABLE penalty_corrections (
    id               TEXT PRIMARY KEY,
    penalty_id       TEXT NOT NULL REFERENCES penalties(id),
    suggestion_id    TEXT NOT NULL UNIQUE REFERENCES compensation_suggestions(id),
    correction_type  TEXT NOT NULL,
    amount_delta     REAL NOT NULL,
    note             TEXT NOT NULL,
    created_at       TEXT NOT NULL
);

CREATE TABLE escalation_annotations (
    id              TEXT PRIMARY KEY,
    escalation_id   TEXT NOT NULL REFERENCES escalations(id),
    suggestion_id   TEXT NOT NULL UNIQUE REFERENCES compensation_suggestions(id),
    note            TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
"""


def _migrate_002(conn, now) -> None:
    """DDL + legacy-hold conversion, atomically."""
    conn.executescript(MIGRATION_002_DDL)
    rows = conn.execute(
        "SELECT * FROM rectification_cases "
        "WHERE legacy_hold_seconds > 0 AND legacy_hold_migrated = 0"
    ).fetchall()
    for case in rows:
        opened = parse(case["opened_at"])
        hold = int(case["legacy_hold_seconds"])
        event_id = new_id("evt")
        conn.execute(
            "INSERT INTO events (id, event_type, description, occurred_at, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                event_id,
                "LEGACY_MIGRATION",
                "Ad-hoc hold imported from the pre-pause-ledger system",
                case["opened_at"],
                iso(now),
            ),
        )
        pause_id = new_id("pause")
        conn.execute(
            "INSERT INTO sla_pauses (id, case_id, event_id, reason, evidence, state,"
            " start_at, approved_at, ended_at, decision_note, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, 'ENDED', ?, ?, ?, ?, ?, ?)",
            (
                pause_id,
                case["id"],
                event_id,
                "Legacy ad-hoc hold converted by migration 002",
                '["legacy:legacy_hold_seconds"]',
                iso(opened),
                iso(opened),
                iso(opened + timedelta(seconds=hold)),
                "converted from legacy_hold_seconds",
                iso(now),
                iso(now),
            ),
        )
        conn.execute(
            "INSERT INTO pause_intervals (pause_id, case_id, start_at, end_at)"
            " VALUES (?, ?, ?, ?)",
            (pause_id, case["id"], iso(opened), iso(opened + timedelta(seconds=hold))),
        )
        conn.execute(
            "UPDATE rectification_cases SET legacy_hold_migrated = 1 WHERE id = ?",
            (case["id"],),
        )


# (version, migration).  A migration is either a SQL script string or a
# callable ``fn(conn, now)``; both run inside one transaction.
MIGRATIONS = [
    ("001_core", MIGRATION_001),
    ("002_sla_pauses", _migrate_002),
]


def migrate(conn, clock, upto: str | None = None) -> list[str]:
    """Apply pending migrations (optionally only up to ``upto`` inclusive).

    Returns the versions applied during this call.  Safe to call on every
    startup: already-applied versions are skipped.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    applied = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}
    done = []
    for version, migration in MIGRATIONS:
        if upto is not None and version > upto:
            break
        if version in applied:
            continue
        with tx(conn):
            if isinstance(migration, str):
                conn.executescript(migration)
            else:
                migration(conn, clock.now())
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, iso(clock.now())),
            )
        done.append(version)
    return done
