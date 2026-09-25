"""Compensation suggestions for late-approved pauses.

A retroactively approved pause can make already-recorded escalations or
penalties premature.  Those records are immutable -- especially LOCKED
penalties -- so the system only ever *proposes* compensation:

    PENDING_REVIEW --confirm--> CONFIRMED   (appends a correction/annotation)
    PENDING_REVIEW --dismiss--> DISMISSED

Confirming appends to the chain; it never edits the original penalty or
escalation row.
"""

from __future__ import annotations

import json

from .clock import iso
from .db import new_id, tx
from .errors import Conflict, NotFound

CREDIT_CORRECTION_TYPE = "SLA_PAUSE_CREDIT"


def suggestion_view(row) -> dict:
    return {
        "id": row["id"],
        "case_id": row["case_id"],
        "pause_id": row["pause_id"],
        "target_type": row["target_type"],
        "target_id": row["target_id"],
        "kind": row["kind"],
        "detail": json.loads(row["detail"]),
        "status": row["status"],
        "created_at": row["created_at"],
        "resolved_at": row["resolved_at"],
    }


def list_suggestions(conn, case_id: str, status: str | None = None) -> list[dict]:
    if status:
        rows = conn.execute(
            "SELECT * FROM compensation_suggestions WHERE case_id = ? AND status = ?"
            " ORDER BY created_at, id",
            (case_id, status),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM compensation_suggestions WHERE case_id = ?"
            " ORDER BY created_at, id",
            (case_id,),
        ).fetchall()
    return [suggestion_view(r) for r in rows]


def _get(conn, suggestion_id: str):
    row = conn.execute(
        "SELECT * FROM compensation_suggestions WHERE id = ?", (suggestion_id,)
    ).fetchone()
    if row is None:
        raise NotFound(f"compensation suggestion {suggestion_id} not found",
                       suggestion_id=suggestion_id)
    return row


def confirm_suggestion(conn, clock, suggestion_id: str, note: str | None = None) -> dict:
    """Confirm a suggestion: append the correction, preserve the chain.

    * PENALTY target  -> append a ``penalty_corrections`` row crediting the
      penalty amount.  The penalty row itself (even if LOCKED) is untouched.
    * ESCALATION target -> append an ``escalation_annotations`` note.

    Idempotent on retry; a second credit for the same penalty is refused.
    """
    now = clock.now()
    with tx(conn):
        sug = _get(conn, suggestion_id)
        if sug["status"] == "CONFIRMED":
            return suggestion_view(sug)  # idempotent retry
        if sug["status"] != "PENDING_REVIEW":
            raise Conflict(
                f"suggestion is {sug['status']}; only PENDING_REVIEW can be confirmed",
                code="SUGGESTION_NOT_REVIEWABLE",
                status=sug["status"],
            )
        if sug["target_type"] == "PENALTY":
            penalty = conn.execute(
                "SELECT * FROM penalties WHERE id = ?", (sug["target_id"],)
            ).fetchone()
            existing = conn.execute(
                "SELECT COUNT(*) AS n FROM penalty_corrections"
                " WHERE penalty_id = ? AND correction_type = ?",
                (penalty["id"], CREDIT_CORRECTION_TYPE),
            ).fetchone()["n"]
            if existing:
                raise Conflict(
                    "penalty already has a confirmed SLA-pause credit",
                    code="ALREADY_CREDITED",
                    penalty_id=penalty["id"],
                )
            conn.execute(
                "INSERT INTO penalty_corrections"
                " (id, penalty_id, suggestion_id, correction_type, amount_delta, note, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    new_id("corr"),
                    penalty["id"],
                    suggestion_id,
                    CREDIT_CORRECTION_TYPE,
                    -penalty["amount"],
                    note or "SLA pause approved retroactively; penalty credited",
                    iso(now),
                ),
            )
        else:  # ESCALATION
            existing = conn.execute(
                "SELECT COUNT(*) AS n FROM escalation_annotations WHERE escalation_id = ?",
                (sug["target_id"],),
            ).fetchone()["n"]
            if existing:
                raise Conflict(
                    "escalation already carries a confirmed annotation",
                    code="ALREADY_ANNOTATED",
                    escalation_id=sug["target_id"],
                )
            conn.execute(
                "INSERT INTO escalation_annotations"
                " (id, escalation_id, suggestion_id, note, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    new_id("ann"),
                    sug["target_id"],
                    suggestion_id,
                    note or "SLA pause approved retroactively; escalation was premature",
                    iso(now),
                ),
            )
        conn.execute(
            "UPDATE compensation_suggestions SET status = 'CONFIRMED', resolved_at = ?"
            " WHERE id = ?",
            (iso(now), suggestion_id),
        )
    return suggestion_view(_get(conn, suggestion_id))


def dismiss_suggestion(conn, clock, suggestion_id: str, note: str | None = None) -> dict:
    """Dismiss a suggestion after review.  Nothing is appended."""
    now = clock.now()
    with tx(conn):
        sug = _get(conn, suggestion_id)
        if sug["status"] == "DISMISSED":
            return suggestion_view(sug)  # idempotent retry
        if sug["status"] != "PENDING_REVIEW":
            raise Conflict(
                f"suggestion is {sug['status']}; only PENDING_REVIEW can be dismissed",
                code="SUGGESTION_NOT_REVIEWABLE",
                status=sug["status"],
            )
        conn.execute(
            "UPDATE compensation_suggestions SET status = 'DISMISSED', resolved_at = ?"
            " WHERE id = ?",
            (iso(now), suggestion_id),
        )
    return suggestion_view(_get(conn, suggestion_id))
