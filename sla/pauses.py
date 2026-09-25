"""SLA pause (停表) lifecycle.

State machine::

    PENDING --approve--> APPROVED --end--> ENDED
    PENDING --reject-->  REJECTED
    PENDING --revoke-->  REVOKED          (applicant withdraws)

Invariants (checked by :func:`integrity_violations`):

* an interval row exists **iff** the pause was approved (APPROVED or ENDED);
* APPROVED pauses have an open interval (``end_at IS NULL``), ENDED pauses a
  closed one; ``end_at >= start_at`` always;
* every mutation happens in one transaction, so a failed approval or a
  crashed restart can never leave half a stopwatch behind.

Retroactive ``start_at`` is allowed (the storm started before the paperwork).
When such a "late approval" reaches history -- escalations or penalties that
fired while the pause should have been running -- those records are **not**
deleted or rewritten.  Instead the approval transaction raises
``compensation_suggestions`` (PENDING_REVIEW); confirming one appends a
correction/annotation while preserving the original chain.
"""

from __future__ import annotations

import json

from .clock import iso, parse
from .db import new_id, tx
from .errors import Conflict, NotFound, Validation
from .escalation import LEVELS, effective_clock

STATES = ("PENDING", "APPROVED", "REJECTED", "ENDED", "REVOKED")


# --------------------------------------------------------------------------- views

def pause_view(conn, row) -> dict:
    interval = conn.execute(
        "SELECT start_at, end_at FROM pause_intervals WHERE pause_id = ?", (row["id"],)
    ).fetchone()
    return {
        "id": row["id"],
        "case_id": row["case_id"],
        "event_id": row["event_id"],
        "reason": row["reason"],
        "evidence": json.loads(row["evidence"]),
        "state": row["state"],
        "start_at": row["start_at"],
        "approved_at": row["approved_at"],
        "ended_at": row["ended_at"],
        "decision_note": row["decision_note"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "version": row["version"],
        "interval": dict(interval) if interval else None,
    }


def get_pause(conn, pause_id: str) -> dict:
    row = conn.execute("SELECT * FROM sla_pauses WHERE id = ?", (pause_id,)).fetchone()
    if row is None:
        raise NotFound(f"pause {pause_id} not found", pause_id=pause_id)
    return pause_view(conn, row)


def list_pauses(conn, case_id: str, state: str | None = None) -> list[dict]:
    if state is not None and state not in STATES:
        raise Validation("unknown pause state", state=state)
    if state:
        rows = conn.execute(
            "SELECT * FROM sla_pauses WHERE case_id = ? AND state = ? ORDER BY created_at, id",
            (case_id, state),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM sla_pauses WHERE case_id = ? ORDER BY created_at, id",
            (case_id,),
        ).fetchall()
    return [pause_view(conn, r) for r in rows]


# --------------------------------------------------------------------------- apply

def apply_pause(conn, clock, case_id: str, event_id: str, reason: str,
                evidence, start_at: str) -> dict:
    """File a pause application bound to an event, a reason and evidence."""
    now = clock.now()
    case = conn.execute(
        "SELECT * FROM rectification_cases WHERE id = ?", (case_id,)
    ).fetchone()
    if case is None:
        raise NotFound(f"case {case_id} not found", case_id=case_id)
    if case["status"] != "OPEN":
        raise Conflict(
            "case is closed; the SLA clock cannot be paused or restarted after closure",
            code="CASE_CLOSED",
        )
    event = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if event is None:
        raise NotFound(f"event {event_id} not found", event_id=event_id)
    if not reason or not reason.strip():
        raise Validation("reason is required")
    if not isinstance(evidence, list) or not evidence or not all(
        isinstance(e, str) and e.strip() for e in evidence
    ):
        raise Validation("evidence must be a non-empty list of references")
    try:
        start = parse(start_at)
    except Exception:
        raise Validation("start_at must be an ISO-8601 timestamp")
    if start > now:
        raise Validation("start_at cannot be in the future")
    if start < parse(case["opened_at"]):
        raise Validation("start_at cannot precede the case opening")

    pause_id = new_id("pause")
    with tx(conn):
        conn.execute(
            "INSERT INTO sla_pauses (id, case_id, event_id, reason, evidence, state,"
            " start_at, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, 'PENDING', ?, ?, ?)",
            (
                pause_id,
                case_id,
                event_id,
                reason.strip(),
                json.dumps([e.strip() for e in evidence]),
                iso(start),
                iso(now),
                iso(now),
            ),
        )
    return get_pause(conn, pause_id)


# --------------------------------------------------------------------------- approve

def approve_pause(conn, clock, pause_id: str, note: str | None = None) -> dict:
    """Approve an application: the pause becomes effective immediately.

    One transaction: state transition + interval creation + late-approval
    compensation scan.  Any failure rolls all three back, so a failed
    approval never leaves half a stopwatch.  Retrying an already-approved
    pause is an idempotent no-op.
    """
    now = clock.now()
    with tx(conn):
        row = conn.execute("SELECT * FROM sla_pauses WHERE id = ?", (pause_id,)).fetchone()
        if row is None:
            raise NotFound(f"pause {pause_id} not found", pause_id=pause_id)
        if row["state"] == "APPROVED":
            return pause_view(conn, row)  # idempotent retry of the same request
        if row["state"] != "PENDING":
            raise Conflict(
                f"pause is {row['state']}; only PENDING pauses can be approved",
                code="PAUSE_NOT_PENDING",
                state=row["state"],
            )
        case = conn.execute(
            "SELECT * FROM rectification_cases WHERE id = ?", (row["case_id"],)
        ).fetchone()
        if case["status"] != "OPEN":
            raise Conflict(
                "case is closed; a pause cannot be approved after closure",
                code="CASE_CLOSED",
            )
        cur = conn.execute(
            "UPDATE sla_pauses SET state = 'APPROVED', approved_at = ?, decision_note = ?,"
            " updated_at = ?, version = version + 1 WHERE id = ? AND state = 'PENDING'",
            (iso(now), note, iso(now), pause_id),
        )
        if cur.rowcount != 1:
            raise Conflict("pause changed concurrently; retry", code="CONCURRENT_CHANGE")
        conn.execute(
            "INSERT INTO pause_intervals (pause_id, case_id, start_at, end_at)"
            " VALUES (?, ?, ?, NULL)",
            (pause_id, row["case_id"], row["start_at"]),
        )
        _scan_premature_records(conn, case, pause_id, now)
    return get_pause(conn, pause_id)


def _scan_premature_records(conn, case, pause_id: str, now) -> None:
    """Late-approval hook: flag records that the new pause makes premature.

    Runs inside the approval transaction.  Never mutates the escalation or
    penalty itself -- only raises PENDING_REVIEW compensation suggestions.
    ``UNIQUE(pause_id, target_type, target_id)`` keeps re-scans idempotent.
    """
    escalations = conn.execute(
        "SELECT * FROM escalations WHERE case_id = ? ORDER BY level", (case["id"],)
    ).fetchall()
    for esc in escalations:
        threshold = LEVELS[esc["level"]]
        # Recompute, with the pause set as now known, whether the level had
        # really been reached at the moment the escalation fired.
        overdue_then = effective_clock(conn, case, parse(esc["triggered_at"]))[
            "effective_overdue_seconds"
        ]
        if overdue_then >= threshold:
            continue  # record stands even with the pauses applied
        detail = {
            "pause_id": pause_id,
            "recorded_triggered_at": esc["triggered_at"],
            "level_threshold_seconds": threshold,
            "recomputed_overdue_seconds": overdue_then,
            "explanation": (
                "retroactively approved pause means this record fired before the "
                "effective overdue time reached the level threshold"
            ),
        }
        conn.execute(
            "INSERT OR IGNORE INTO compensation_suggestions"
            " (id, case_id, pause_id, target_type, target_id, kind, detail, status, created_at)"
            " VALUES (?, ?, ?, 'ESCALATION', ?, 'PREMATURE_ESCALATION', ?, 'PENDING_REVIEW', ?)",
            (new_id("sug"), case["id"], pause_id, esc["id"], json.dumps(detail), iso(now)),
        )
        penalty = conn.execute(
            "SELECT * FROM penalties WHERE escalation_id = ?", (esc["id"],)
        ).fetchone()
        if penalty is not None:
            conn.execute(
                "INSERT OR IGNORE INTO compensation_suggestions"
                " (id, case_id, pause_id, target_type, target_id, kind, detail, status, created_at)"
                " VALUES (?, ?, ?, 'PENALTY', ?, 'PREMATURE_PENALTY', ?, 'PENDING_REVIEW', ?)",
                (
                    new_id("sug"),
                    case["id"],
                    pause_id,
                    penalty["id"],
                    json.dumps({**detail, "penalty_status": penalty["status"]}),
                    iso(now),
                ),
            )


# --------------------------------------------------------------------------- reject / revoke / end

def _transition(conn, clock, pause_id: str, allowed_from: str, to: str,
                note: str | None, verb: str) -> dict:
    now = clock.now()
    with tx(conn):
        row = conn.execute("SELECT * FROM sla_pauses WHERE id = ?", (pause_id,)).fetchone()
        if row is None:
            raise NotFound(f"pause {pause_id} not found", pause_id=pause_id)
        if row["state"] == to:
            return pause_view(conn, row)  # idempotent retry
        if row["state"] != allowed_from:
            raise Conflict(
                f"pause is {row['state']}; only {allowed_from} pauses can be {verb}",
                code="PAUSE_INVALID_STATE",
                state=row["state"],
            )
        conn.execute(
            "UPDATE sla_pauses SET state = ?, decision_note = ?, updated_at = ?,"
            " version = version + 1 WHERE id = ?",
            (to, note, iso(now), pause_id),
        )
    return get_pause(conn, pause_id)


def reject_pause(conn, clock, pause_id: str, note: str | None = None) -> dict:
    return _transition(conn, clock, pause_id, "PENDING", "REJECTED", note, "rejected")


def revoke_pause(conn, clock, pause_id: str, note: str | None = None) -> dict:
    return _transition(conn, clock, pause_id, "PENDING", "REVOKED", note, "revoked")


def end_pause(conn, clock, pause_id: str) -> dict:
    """Stop a running pause.  Idempotent: re-ending returns the same record
    with the original ``ended_at`` -- a duplicate end request never extends
    or double-counts the pause."""
    now = clock.now()
    with tx(conn):
        row = conn.execute("SELECT * FROM sla_pauses WHERE id = ?", (pause_id,)).fetchone()
        if row is None:
            raise NotFound(f"pause {pause_id} not found", pause_id=pause_id)
        if row["state"] == "ENDED":
            return pause_view(conn, row)  # duplicate end: no-op, no double count
        if row["state"] != "APPROVED":
            raise Conflict(
                f"pause is {row['state']}; only APPROVED pauses can be ended",
                code="PAUSE_NOT_ACTIVE",
                state=row["state"],
            )
        conn.execute(
            "UPDATE sla_pauses SET state = 'ENDED', ended_at = ?, updated_at = ?,"
            " version = version + 1 WHERE id = ?",
            (iso(now), iso(now), pause_id),
        )
        conn.execute(
            "UPDATE pause_intervals SET end_at = ? WHERE pause_id = ?",
            (iso(now), pause_id),
        )
    return get_pause(conn, pause_id)


def end_active_pauses(conn, case_id: str, now) -> None:
    """End every running pause of a case (used when the case is closed).

    Must be called inside the caller's transaction so case closure and pause
    ending commit -- or roll back -- together.
    """
    rows = conn.execute(
        "SELECT id FROM sla_pauses WHERE case_id = ? AND state = 'APPROVED'", (case_id,)
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE sla_pauses SET state = 'ENDED', ended_at = ?, updated_at = ?,"
            " version = version + 1 WHERE id = ?",
            (iso(now), iso(now), row["id"]),
        )
        conn.execute(
            "UPDATE pause_intervals SET end_at = ? WHERE pause_id = ?",
            (iso(now), row["id"]),
        )


# --------------------------------------------------------------------------- integrity

def integrity_violations(conn) -> list[str]:
    """Detect half-finished stopwatches.  An empty list means the ledger is
    consistent; used by tests and operable as a runtime health probe."""
    violations = []
    rows = conn.execute(
        "SELECT p.id, p.state, p.start_at, p.ended_at, i.start_at AS i_start, i.end_at AS i_end"
        " FROM sla_pauses p LEFT JOIN pause_intervals i ON i.pause_id = p.id"
    ).fetchall()
    for r in rows:
        has_interval = r["i_start"] is not None
        if r["state"] in ("APPROVED", "ENDED") and not has_interval:
            violations.append(f"{r['id']}: {r['state']} pause without interval row")
        if r["state"] in ("PENDING", "REJECTED", "REVOKED") and has_interval:
            violations.append(f"{r['id']}: {r['state']} pause must not have an interval")
        if r["state"] == "APPROVED" and has_interval and r["i_end"] is not None:
            violations.append(f"{r['id']}: APPROVED pause has a closed interval")
        if r["state"] == "ENDED":
            if r["ended_at"] is None or (has_interval and r["i_end"] is None):
                violations.append(f"{r['id']}: ENDED pause has an open interval")
        if has_interval and parse(r["i_start"]) > parse(r["i_end"] or r["i_start"]):
            violations.append(f"{r['id']}: interval end precedes start")
    return violations
