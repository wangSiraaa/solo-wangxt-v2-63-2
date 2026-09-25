"""OpenAPI 3.0 document built from the API route registry.

``sla.api.ROUTES`` is the single source of truth: the same metadata that
dispatches requests produces this specification, so the two cannot drift.
``scripts/export_openapi.py`` writes the document to ``openapi.json`` and a
test asserts the served document matches the exported file.
"""

from __future__ import annotations

from . import __version__

_DT = {"type": "string", "format": "date-time"}

COMPONENTS = {
    "Error": {
        "type": "object",
        "required": ["error"],
        "properties": {
            "error": {
                "type": "object",
                "required": ["code", "message"],
                "properties": {
                    "code": {"type": "string", "example": "CONFLICT"},
                    "message": {"type": "string"},
                    "details": {"type": "object"},
                },
            }
        },
    },
    "Event": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "event_type": {"type": "string", "enum": [
                "RAINSTORM", "ROAD_CLOSURE", "POWER_OUTAGE",
                "GOVERNMENT_ORDER", "OTHER", "LEGACY_MIGRATION"]},
            "description": {"type": "string"},
            "occurred_at": _DT,
            "created_at": _DT,
        },
    },
    "EventCreate": {
        "type": "object",
        "required": ["event_type", "description", "occurred_at"],
        "properties": {
            "event_type": {"type": "string", "enum": [
                "RAINSTORM", "ROAD_CLOSURE", "POWER_OUTAGE", "GOVERNMENT_ORDER", "OTHER"]},
            "description": {"type": "string"},
            "occurred_at": _DT,
        },
    },
    "Case": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "title": {"type": "string"},
            "opened_at": _DT,
            "sla_seconds": {"type": "integer"},
            "status": {"type": "string", "enum": ["OPEN", "CLOSED"]},
            "closed_reason": {"type": ["string", "null"], "enum": ["RECTIFIED", "CANCELLED", None]},
            "closed_at": {"type": ["string", "null"], "format": "date-time"},
        },
    },
    "CaseCreate": {
        "type": "object",
        "required": ["title", "sla_seconds"],
        "properties": {
            "title": {"type": "string"},
            "sla_seconds": {"type": "integer", "minimum": 1},
            "opened_at": {**_DT, "description": "defaults to the injected clock's now"},
        },
    },
    "CaseList": {
        "type": "object",
        "properties": {"cases": {"type": "array", "items": {"$ref": "#/components/schemas/Case"}}},
    },
    "CaseStatus": {
        "type": "object",
        "description": "Stopwatch view. remaining_seconds resumes decreasing only "
                       "while no approved pause covers the current time.",
        "properties": {
            "case_id": {"type": "string"},
            "case_status": {"type": "string"},
            "as_of": _DT,
            "sla_seconds": {"type": "integer"},
            "wall_elapsed_seconds": {"type": "number"},
            "paused_seconds": {"type": "number",
                               "description": "union of approved pause intervals"},
            "effective_elapsed_seconds": {"type": "number"},
            "effective_overdue_seconds": {"type": "number"},
            "remaining_seconds": {"type": "number"},
            "current_level": {"type": "integer"},
            "recorded_levels": {"type": "array", "items": {"type": "integer"}},
            "pause_intervals_union": {
                "type": "array",
                "items": {"type": "object", "properties": {"start_at": _DT, "end_at": _DT}},
            },
        },
    },
    "Pause": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "case_id": {"type": "string"},
            "event_id": {"type": "string"},
            "reason": {"type": "string"},
            "evidence": {"type": "array", "items": {"type": "string"}},
            "state": {"type": "string",
                      "enum": ["PENDING", "APPROVED", "REJECTED", "ENDED", "REVOKED"]},
            "start_at": _DT,
            "approved_at": {"type": ["string", "null"], "format": "date-time"},
            "ended_at": {"type": ["string", "null"], "format": "date-time"},
            "decision_note": {"type": ["string", "null"]},
            "created_at": _DT,
            "updated_at": _DT,
            "version": {"type": "integer"},
            "interval": {
                "type": ["object", "null"],
                "description": "present iff the pause was approved; end_at null = running",
                "properties": {"start_at": _DT, "end_at": {"type": ["string", "null"], "format": "date-time"}},
            },
        },
    },
    "PauseApply": {
        "type": "object",
        "required": ["event_id", "reason", "evidence", "start_at"],
        "properties": {
            "event_id": {"type": "string"},
            "reason": {"type": "string"},
            "evidence": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "start_at": {**_DT, "description": "evidence-backed start; may be retroactive"},
        },
    },
    "PauseList": {
        "type": "object",
        "properties": {"pauses": {"type": "array", "items": {"$ref": "#/components/schemas/Pause"}}},
    },
    "DecisionNote": {
        "type": "object",
        "properties": {"note": {"type": ["string", "null"]}},
    },
    "Escalation": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "case_id": {"type": "string"},
            "level": {"type": "integer"},
            "triggered_at": _DT,
            "effective_overdue_seconds": {"type": "number"},
            "created_at": _DT,
            "annotations": {"type": "array", "items": {"type": "object"}},
        },
    },
    "EscalationList": {
        "type": "object",
        "properties": {"escalations": {"type": "array",
                                       "items": {"$ref": "#/components/schemas/Escalation"}}},
    },
    "Penalty": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "case_id": {"type": "string"},
            "escalation_id": {"type": "string"},
            "amount": {"type": "number"},
            "status": {"type": "string", "enum": ["OPEN", "LOCKED"]},
            "locked_at": {"type": ["string", "null"], "format": "date-time"},
            "created_at": _DT,
            "corrections": {
                "type": "array",
                "description": "append-only correction chain; the penalty row itself "
                               "is never mutated",
                "items": {"type": "object"},
            },
        },
    },
    "PenaltyList": {
        "type": "object",
        "properties": {"penalties": {"type": "array",
                                     "items": {"$ref": "#/components/schemas/Penalty"}}},
    },
    "Suggestion": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "case_id": {"type": "string"},
            "pause_id": {"type": "string"},
            "target_type": {"type": "string", "enum": ["ESCALATION", "PENALTY"]},
            "target_id": {"type": "string"},
            "kind": {"type": "string", "enum": ["PREMATURE_ESCALATION", "PREMATURE_PENALTY"]},
            "detail": {"type": "object"},
            "status": {"type": "string",
                       "enum": ["PENDING_REVIEW", "CONFIRMED", "DISMISSED"]},
            "created_at": _DT,
            "resolved_at": {"type": ["string", "null"], "format": "date-time"},
        },
    },
    "SuggestionList": {
        "type": "object",
        "properties": {"suggestions": {"type": "array",
                                       "items": {"$ref": "#/components/schemas/Suggestion"}}},
    },
    "EngineRunResult": {
        "type": "object",
        "properties": {
            "case_id": {"type": "string"},
            "as_of": _DT,
            "skipped": {"type": ["string", "null"]},
            "effective_overdue_seconds": {"type": "number"},
            "fired_levels": {"type": "array", "items": {"type": "integer"}},
        },
    },
    "EngineRunAllResult": {
        "type": "object",
        "properties": {"results": {"type": "array",
                                   "items": {"$ref": "#/components/schemas/EngineRunResult"}}},
    },
    "Trace": {
        "type": "object",
        "description": "Retrospective audit output: accounting, raw and unioned pause "
                       "intervals, pauses, escalations, penalties with correction chain, "
                       "compensation suggestions and a merged timeline.",
        "properties": {
            "case": {"type": "object"},
            "as_of": _DT,
            "accounting": {"type": "object"},
            "pause_intervals": {"type": "object",
                                "properties": {"raw": {"type": "array", "items": {"type": "object"}},
                                               "union": {"type": "array", "items": {"type": "object"}}}},
            "pauses": {"type": "array", "items": {"$ref": "#/components/schemas/Pause"}},
            "escalations": {"type": "array", "items": {"$ref": "#/components/schemas/Escalation"}},
            "penalties": {"type": "array", "items": {"$ref": "#/components/schemas/Penalty"}},
            "compensation_suggestions": {"type": "array",
                                         "items": {"$ref": "#/components/schemas/Suggestion"}},
            "timeline": {"type": "array", "items": {"type": "object"}},
        },
    },
    "OpenAPI": {"type": "object"},
}


def build_openapi() -> dict:
    from .api import ROUTES  # local import: api imports this module lazily

    paths: dict = {}
    for r in ROUTES:
        entry = paths.setdefault(r["path"], {})
        op = {
            "summary": r["summary"],
            "description": r["description"],
            "tags": r["tags"],
            "responses": {
                str(r["status"]): {
                    "description": "success",
                    **(_json_content(r["response"]) if r["response"] else {}),
                },
                "400": _error_response("validation error"),
                "404": _error_response("not found"),
                "409": _error_response("state conflict"),
            },
        }
        params = [
            {
                "name": name, "in": "path", "required": True,
                "schema": {"type": "string"},
            }
            for name in _path_params(r["path"])
        ]
        params += [
            {"name": q, "in": "query", "required": False, "schema": {"type": "string"}}
            for q in r["query"]
        ]
        if params:
            op["parameters"] = params
        if r["request"]:
            op["requestBody"] = {
                "required": True,
                "content": {"application/json": {
                    "schema": {"$ref": f"#/components/schemas/{r['request']}"}}},
            }
        entry[r["method"].lower()] = op
    return {
        "openapi": "3.0.3",
        "info": {
            "title": "Rectification SLA Pause Service",
            "version": __version__,
            "description": (
                "Auditable SLA pauses (停表) for overdue rectification escalation. "
                "Pauses bind event+reason+evidence, flow through "
                "PENDING/APPROVED/ENDED/REJECTED/REVOKED, and the escalation engine "
                "deducts the union of approved intervals. Late approvals never rewrite "
                "history: they raise reviewable compensation suggestions whose "
                "confirmation appends corrections while preserving the penalty chain."
            ),
        },
        "paths": paths,
        "components": {"schemas": COMPONENTS},
    }


def _path_params(path: str) -> list[str]:
    return [seg[1:-1] for seg in path.split("/") if seg.startswith("{")]


def _json_content(schema_name: str) -> dict:
    return {"content": {"application/json": {
        "schema": {"$ref": f"#/components/schemas/{schema_name}"}}}}


def _error_response(description: str) -> dict:
    return {"description": description,
            "content": {"application/json": {
                "schema": {"$ref": "#/components/schemas/Error"}}}}
