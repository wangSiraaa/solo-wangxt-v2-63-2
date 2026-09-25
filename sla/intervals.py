"""Time-interval service (pure functions, no I/O).

The escalation engine deducts the *union* of approved pause intervals from
the wall clock.  Working on the union -- rather than summing each pause --
is what makes overlapping pauses safe: two pauses covering the same hour
still only stop the clock once.

Intervals are half-open ``[start, end)`` of aware UTC datetimes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable, List, Tuple

Interval = Tuple[datetime, datetime]


def union(intervals: Iterable[Interval]) -> List[Interval]:
    """Merge overlapping or touching intervals into a sorted disjoint set."""
    sorted_iv = sorted((s, e) for s, e in intervals if e > s)
    merged: List[Interval] = []
    for start, end in sorted_iv:
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def clip(intervals: Iterable[Interval], lo: datetime, hi: datetime) -> List[Interval]:
    """Intersect every interval with the window ``[lo, hi)``."""
    out = []
    for start, end in intervals:
        s, e = max(start, lo), min(end, hi)
        if e > s:
            out.append((s, e))
    return out


def total_seconds(intervals: Iterable[Interval]) -> float:
    return sum((e - s).total_seconds() for s, e in intervals)


def covered_seconds(intervals: Iterable[Interval], lo: datetime, hi: datetime) -> float:
    """Seconds of ``[lo, hi)`` covered by the union of ``intervals``."""
    return total_seconds(union(clip(intervals, lo, hi)))
