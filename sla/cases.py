"""Rectification cases."""

from __future__ import annotations

from .clock import iso
from .db import new_id, tx
from .errors import NotFound, Validation
from . import pauses


def create_case(conn, clock, title: str, sla_seconds: int, opened_at: str | None = None) -> dict:
    if not title or not title.strip():
        raise Validation("title is required")
    if not isinstance(sla_seconds, int) or sla_seconds <= 0:
        raise Validation("sla_seconds must be a positive integer")
    opened = iso(clock.now()) if opened_at is None else opened_at
    case_id = new_id("case")
    with tx(conn):
        conn.execute(
            "INSERT INTO rectification_cases (id, title, opened_at, sla_seconds, status)"
            " VALUES (?, ?, ?, ?, 'OPEN')",
            (case_id, title.strip(), opened, sla_seconds),
        )
    return get_case(conn, case_id)


def get_case(conn, case_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM rectification_cases WHERE id = ?", (case_id,)
    ).fetchone()
    if row is None:
        raise NotFound(f"case {case_id} not found", case_id=case_id)
    return case_view(row)


def list_cases(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM rectification_cases ORDER BY opened_at, id"
    ).fetchall()
    return [case_view(r) for r in rows]


def case_view(row) -> dict:
    return {
        "id": row["id"],
        "title": row["title"],
        "opened_at": row["opened_at"],
        "sla_seconds": row["sla_seconds"],
        "status": row["status"],
        "closed_reason": row["closed_reason"],
        "closed_at": row["closed_at"],
    }


def rectify_case(conn, clock, case_id: str) -> dict:
    """Close a case as rectified.

    Any still-running pause is ended in the same transaction (a closed case
    must not keep a dangling stopped clock), and from this point on the
    escalation engine skips the case and no new pause can be applied for or
    approved -- the clock is never restarted after closure.
    """
    now = clock.now()
    with tx(conn):
        row = conn.execute(
            "SELECT * FROM rectification_cases WHERE id = ?", (case_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"case {case_id} not found", case_id=case_id)
        if row["status"] == "CLOSED":
            return case_view(row)  # idempotent retry
        conn.execute(
            "UPDATE rectification_cases"
            " SET status = 'CLOSED', closed_reason = 'RECTIFIED', closed_at = ?"
            " WHERE id = ?",
            (iso(now), case_id),
        )
        pauses.end_active_pauses(conn, case_id, now)
    return get_case(conn, case_id)
