"""Events that pauses bind to (rainstorm, road closure, ...)."""

from __future__ import annotations

from .clock import iso, parse
from .db import new_id, tx
from .errors import NotFound, Validation

EVENT_TYPES = {
    "RAINSTORM",
    "ROAD_CLOSURE",
    "POWER_OUTAGE",
    "GOVERNMENT_ORDER",
    "OTHER",
}
# Reserved for migration 002; not creatable through the API.
SYSTEM_EVENT_TYPES = {"LEGACY_MIGRATION"}


def create_event(conn, clock, event_type: str, description: str, occurred_at: str) -> dict:
    if not event_type or event_type not in EVENT_TYPES:
        raise Validation(
            "event_type must be one of " + ", ".join(sorted(EVENT_TYPES)),
            event_type=event_type,
        )
    if not description or not description.strip():
        raise Validation("description is required")
    try:
        occurred = parse(occurred_at)
    except Exception:
        raise Validation("occurred_at must be an ISO-8601 timestamp")
    event_id = new_id("evt")
    with tx(conn):
        conn.execute(
            "INSERT INTO events (id, event_type, description, occurred_at, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (event_id, event_type, description.strip(), iso(occurred), iso(clock.now())),
        )
    return get_event(conn, event_id)


def get_event(conn, event_id: str) -> dict:
    row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if row is None:
        raise NotFound(f"event {event_id} not found", event_id=event_id)
    return {
        "id": row["id"],
        "event_type": row["event_type"],
        "description": row["description"],
        "occurred_at": row["occurred_at"],
        "created_at": row["created_at"],
    }
