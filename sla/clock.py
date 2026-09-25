"""Clock abstraction.

All time-dependent logic (escalation engine, pause lifecycle, migrations)
takes time from an injected clock so behaviour is deterministic under test
and auditable in production.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def utc(dt: datetime) -> datetime:
    """Normalise a datetime to aware UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    """Serialise a datetime as ISO-8601 UTC (``...Z``)."""
    return utc(dt).isoformat().replace("+00:00", "Z")


def parse(value) -> datetime:
    """Parse an ISO-8601 string (or pass through a datetime) to aware UTC."""
    if isinstance(value, datetime):
        return utc(value)
    return utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))


class Clock:
    """Interface: anything with ``now() -> datetime`` (aware, UTC)."""

    def now(self) -> datetime:  # pragma: no cover - interface
        raise NotImplementedError


class SystemClock(Clock):
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FrozenClock(Clock):
    """Manually advanced clock for tests and replay."""

    def __init__(self, start: datetime):
        self._t = utc(start)

    def now(self) -> datetime:
        return self._t

    def set(self, t: datetime) -> datetime:
        self._t = utc(t)
        return self._t

    def advance(self, **kwargs) -> datetime:
        self._t = self._t + timedelta(**kwargs)
        return self._t
