"""Retrospective (追溯) output: the full auditable history of a case.

Assembles, for one case: the effective-time accounting as of now, raw and
unioned pause intervals, every pause application with its evidence, every
recorded escalation (with annotations), every penalty with its appended
correction chain, every compensation suggestion, and a merged timeline.
"""

from __future__ import annotations

from .clock import parse
from .compensation import list_suggestions
from .escalation import (
    case_row,
    case_status,
    escalation_view,
    penalty_view,
)
from .pauses import list_pauses


def case_trace(conn, clock, case_id: str) -> dict:
    case = case_row(conn, case_id)
    status = case_status(conn, clock, case_id)
    pauses = list_pauses(conn, case_id)
    escalations = [
        escalation_view(conn, r)
        for r in conn.execute(
            "SELECT * FROM escalations WHERE case_id = ? ORDER BY level", (case_id,)
        )
    ]
    penalties = [
        penalty_view(conn, r)
        for r in conn.execute(
            "SELECT * FROM penalties WHERE case_id = ? ORDER BY created_at", (case_id,)
        )
    ]
    suggestions = list_suggestions(conn, case_id)
    raw_intervals = [
        {"pause_id": p["id"], **p["interval"]} for p in pauses if p["interval"]
    ]
    return {
        "case": {
            "id": case["id"],
            "title": case["title"],
            "opened_at": case["opened_at"],
            "sla_seconds": case["sla_seconds"],
            "status": case["status"],
            "closed_reason": case["closed_reason"],
            "closed_at": case["closed_at"],
        },
        "as_of": status["as_of"],
        "accounting": {
            "wall_elapsed_seconds": status["wall_elapsed_seconds"],
            "paused_seconds": status["paused_seconds"],
            "effective_elapsed_seconds": status["effective_elapsed_seconds"],
            "effective_overdue_seconds": status["effective_overdue_seconds"],
            "remaining_seconds": status["remaining_seconds"],
        },
        "pause_intervals": {
            "raw": raw_intervals,
            "union": status["pause_intervals_union"],
        },
        "pauses": pauses,
        "escalations": escalations,
        "penalties": penalties,
        "compensation_suggestions": suggestions,
        "timeline": _timeline(case, pauses, escalations, penalties, suggestions),
    }


def _timeline(case, pauses, escalations, penalties, suggestions) -> list[dict]:
    entries = []

    def add(at, type_, ref, summary):
        if at:
            entries.append({"at": at, "type": type_, "ref": ref, "summary": summary})

    add(case["opened_at"], "CASE_OPENED", case["id"], f"case opened: {case['title']}")
    add(case["closed_at"], "CASE_CLOSED", case["id"],
        f"case closed ({case['closed_reason']})")
    for p in pauses:
        add(p["created_at"], "PAUSE_APPLIED", p["id"],
            f"pause applied, start {p['start_at']}, reason: {p['reason']}")
        add(p["approved_at"], "PAUSE_APPROVED", p["id"], "pause approved")
        add(p["ended_at"], "PAUSE_ENDED", p["id"], "pause ended")
        if p["state"] == "REJECTED":
            add(p["updated_at"], "PAUSE_REJECTED", p["id"], "pause rejected")
        if p["state"] == "REVOKED":
            add(p["updated_at"], "PAUSE_REVOKED", p["id"], "pause revoked")
    for e in escalations:
        add(e["triggered_at"], "ESCALATION_RECORDED", e["id"],
            f"level {e['level']} escalation recorded")
        for a in e["annotations"]:
            add(a["created_at"], "ESCALATION_ANNOTATED", a["id"], a["note"])
    for pen in penalties:
        add(pen["created_at"], "PENALTY_CREATED", pen["id"],
            f"penalty {pen['amount']} created")
        add(pen["locked_at"], "PENALTY_LOCKED", pen["id"], "penalty locked")
        for c in pen["corrections"]:
            add(c["created_at"], "PENALTY_CORRECTION_APPENDED", c["id"],
                f"{c['correction_type']} {c['amount_delta']}: {c['note']}")
    for s in suggestions:
        add(s["created_at"], "SUGGESTION_RAISED", s["id"],
            f"{s['kind']} on {s['target_type']} {s['target_id']}")
        if s["status"] != "PENDING_REVIEW":
            add(s["resolved_at"], f"SUGGESTION_{s['status']}", s["id"],
                f"suggestion {s['status'].lower()}")
    entries.sort(key=lambda e: (parse(e["at"]), e["type"]))
    return entries
