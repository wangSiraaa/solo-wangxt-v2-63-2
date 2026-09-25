"""时间区间服务单元测试：并集合并、裁剪、重叠不重复计算。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.intervals import Interval, clip, merge_union, total_seconds

T = datetime(2026, 9, 1, tzinfo=timezone.utc)


def iv(start_h: float, end_h: float) -> Interval:
    return Interval(T + timedelta(hours=start_h), T + timedelta(hours=end_h))


def test_merge_disjoint():
    merged = merge_union([iv(0, 1), iv(2, 3)])
    assert merged == [iv(0, 1), iv(2, 3)]


def test_merge_overlapping_union():
    # [0,2) ∪ [1,3) = [0,3) —— 重叠只算 3h 而不是 4h
    merged = merge_union([iv(0, 2), iv(1, 3)])
    assert merged == [iv(0, 3)]
    assert total_seconds(merged) == 3 * 3600


def test_merge_contained_and_touching():
    # 被包含区间不产生延长；首尾相接也合并
    merged = merge_union([iv(0, 5), iv(1, 2), iv(5, 7)])
    assert merged == [iv(0, 7)]
    assert total_seconds(merged) == 7 * 3600


def test_clip_drops_empty():
    assert clip(iv(0, 10), T + timedelta(hours=2), T + timedelta(hours=8)) == iv(2, 8)
    assert clip(iv(0, 10), T + timedelta(hours=10), T + timedelta(hours=20)) is None
    assert clip(iv(0, 10), T - timedelta(hours=5), T + timedelta(hours=3)) == iv(0, 3)


def test_empty_interval_rejected():
    with pytest.raises(ValueError):
        Interval(T, T)
