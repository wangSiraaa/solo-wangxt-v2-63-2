"""Escalation engine.

Escalation levels are driven by *effective* overdue time: wall-clock elapsed
minus the union of approved pause intervals.  All time comes from an injected
clock.  The engine is idempotent -- ``UNIQUE(case_id, level)`` plus
``INSERT OR IGNORE`` means restart catch-up runs never duplicate records, and
a partially failed run self-heals on the next pass.

The engine never updates or deletes existing rows.  In particular, approving
a pause afterwards cannot remove escalations or penalties that already fired
(see :mod:`sla.pauses` for the compensation-suggestion flow instead).
"""

from __future__ import annotations

from datetime import datetime

from .clock import iso, parse
from .db import new_id, tx
from .errors import NotFound
from .intervals import Interval, covered_seconds, union

# level -> effective overdue seconds at which the level fires.
LEVELS = {1: 0, 2: 24 * 3600, 3: 72 * 3600}
PENALTY_LEVEL = 3
PENALTY_AMOUNT = 1000.0


def case_row(conn, case_id: str):
    row = conn.execute(
        "SELECT * FROM rectification_cases WHERE id = ?", (case_id,)
    ).fetchone()
    if row is None:
        raise NotFound(f"case {case_id} not found", case_id=case_id)
    return row


def approved_intervals(conn, case_id: str, cap: datetime) -> list[Interval]:
    """Approved pause intervals for the case, as ``[start, end)`` capped at ``cap``.

    Running intervals (``end_at IS NULL``) are treated as ending at ``cap`` --
    the clock is stopped *up to* the moment we evaluate.
    """
    rows = conn.execute(
        "SELECT start_at, end_at FROM pause_intervals WHERE case_id = ?", (case_id,)
    ).fetchall()
    out: list[Interval] = []
    for r in rows:
        start = parse(r["start_at"])
        end = parse(r["end_at"]) if r["end_at"] else cap
        end = min(end, cap)
        if end > start:
            out.append((start, end))
    return out


def effective_clock(conn, case, at: datetime) -> dict:
    """Effective elapsed/overdue accounting for ``case`` as of ``at``."""
    opened = parse(case["opened_at"])
    pauses = approved_intervals(conn, case["id"], at)
    wall = max(0.0, (at - opened).total_seconds())
    paused = covered_seconds(pauses, opened, at)
    effective = max(0.0, wall - paused)
    overdue = effective - case["sla_seconds"]
    return {
        "wall_elapsed_seconds": wall,
        "paused_seconds": paused,
        "effective_elapsed_seconds": effective,
        "effective_overdue_seconds": overdue,
        "pause_intervals_union": [
            {"start_at": iso(s), "end_at": iso(e)} for s, e in union(pauses)
        ],
    }


def level_for(overdue_seconds: float) -> int:
    level = 0
    for lv, threshold in sorted(LEVELS.items()):
        if overdue_seconds >= threshold:
            level = lv
    return level


def run_case(conn, clock, case_id: str) -> dict:
    """Advance escalation for one case to the injected clock's ``now``.

    Closed cases are skipped: once rectification is closed the clock is never
    restarted, so no escalation can fire afterwards.
    """
    now = clock.now()
    case = case_row(conn, case_id)
    if case["status"] != "OPEN":
        return {"case_id": case_id, "as_of": iso(now), "skipped": case["status"],
                "fired_levels": []}

    clock_view = effective_clock(conn, case, now)
    overdue = clock_view["effective_overdue_seconds"]
    fired: list[int] = []
    with tx(conn):
        for level, threshold in sorted(LEVELS.items()):
            if overdue < threshold:
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO escalations"
                " (id, case_id, level, triggered_at, effective_overdue_seconds, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (new_id("esc"), case_id, level, iso(now), overdue, iso(now)),
            )
            if cur.rowcount:
                fired.append(level)
            if level == PENALTY_LEVEL:
                esc = conn.execute(
                    "SELECT id FROM escalations WHERE case_id = ? AND level = ?",
                    (case_id, PENALTY_LEVEL),
                ).fetchone()
                conn.execute(
                    "INSERT OR IGNORE INTO penalties"
                    " (id, case_id, escalation_id, amount, status, created_at)"
                    " VALUES (?, ?, ?, ?, 'OPEN', ?)",
                    (new_id("pen"), case_id, esc["id"], PENALTY_AMOUNT, iso(now)),
                )
    return {
        "case_id": case_id,
        "as_of": iso(now),
        "skipped": None,
        "effective_overdue_seconds": overdue,
        "fired_levels": fired,
    }


def run_all_open_cases(conn, clock) -> dict:
    """Restart catch-up: advance every open case.  Idempotent by construction."""
    ids = [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM rectification_cases WHERE status = 'OPEN' ORDER BY opened_at"
        )
    ]
    return {"results": [run_case(conn, clock, cid) for cid in ids]}


def case_status(conn, clock, case_id: str) -> dict:
    """Live 'stopwatch' view: remaining time resumes correctly after pauses.

    For a closed case the accounting is frozen at ``closed_at`` -- the clock
    is never restarted after closure.
    """
    case = case_row(conn, case_id)
    as_of = parse(case["closed_at"]) if case["status"] == "CLOSED" else clock.now()
    view = effective_clock(conn, case, as_of)
    overdue = view["effective_overdue_seconds"]
    recorded = [
        r["level"]
        for r in conn.execute(
            "SELECT level FROM escalations WHERE case_id = ? ORDER BY level", (case_id,)
        )
    ]
    return {
        "case_id": case_id,
        "case_status": case["status"],
        "as_of": iso(as_of),
        "sla_seconds": case["sla_seconds"],
        "remaining_seconds": max(0.0, case["sla_seconds"] - view["effective_elapsed_seconds"]),
        "current_level": level_for(overdue),
        "recorded_levels": recorded,
        **view,
    }


def list_escalations(conn, case_id: str) -> list[dict]:
    case_row(conn, case_id)
    rows = conn.execute(
        "SELECT * FROM escalations WHERE case_id = ? ORDER BY level", (case_id,)
    ).fetchall()
    return [escalation_view(conn, r) for r in rows]


def list_penalties(conn, case_id: str) -> list[dict]:
    case_row(conn, case_id)
    rows = conn.execute(
        "SELECT * FROM penalties WHERE case_id = ? ORDER BY created_at", (case_id,)
    ).fetchall()
    return [penalty_view(conn, r) for r in rows]


def escalation_view(conn, row) -> dict:
    annotations = conn.execute(
        "SELECT * FROM escalation_annotations WHERE escalation_id = ? ORDER BY created_at",
        (row["id"],),
    ).fetchall()
    return {
        "id": row["id"],
        "case_id": row["case_id"],
        "level": row["level"],
        "triggered_at": row["triggered_at"],
        "effective_overdue_seconds": row["effective_overdue_seconds"],
        "created_at": row["created_at"],
        "annotations": [
            {
                "id": a["id"],
                "suggestion_id": a["suggestion_id"],
                "note": a["note"],
                "created_at": a["created_at"],
            }
            for a in annotations
        ],
    }


def penalty_view(conn, row) -> dict:
    corrections = conn.execute(
        "SELECT * FROM penalty_corrections WHERE penalty_id = ? ORDER BY created_at",
        (row["id"],),
    ).fetchall()
    return {
        "id": row["id"],
        "case_id": row["case_id"],
        "escalation_id": row["escalation_id"],
        "amount": row["amount"],
        "status": row["status"],
        "locked_at": row["locked_at"],
        "created_at": row["created_at"],
        "corrections": [
            {
                "id": c["id"],
                "suggestion_id": c["suggestion_id"],
                "correction_type": c["correction_type"],
                "amount_delta": c["amount_delta"],
                "note": c["note"],
                "created_at": c["created_at"],
            }
            for c in corrections
        ],
    }


def lock_penalty(conn, clock, penalty_id: str) -> dict:
    """Finalise a penalty.  Locked penalties can never be rewritten -- a late
    pause approval can only append a correction via a confirmed suggestion."""
    now = clock.now()
    with tx(conn):
        row = conn.execute(
            "SELECT * FROM penalties WHERE id = ?", (penalty_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"penalty {penalty_id} not found", penalty_id=penalty_id)
        if row["status"] == "LOCKED":
            return penalty_view(conn, row)  # idempotent retry
        conn.execute(
            "UPDATE penalties SET status = 'LOCKED', locked_at = ? WHERE id = ?",
            (iso(now), penalty_id),
        )
    row = conn.execute("SELECT * FROM penalties WHERE id = ?", (penalty_id,)).fetchone()
    return penalty_view(conn, row)
