"""时间区间服务：合并、裁剪、求并集时长。

升级计算的核心不变量：
- 多个已批准暂停区间取**并集**后再扣减，重叠部分绝不重复延长；
- 所有区间在扣除前都裁剪到案件的有效统计窗口 [started_at, reference_at]。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional


@dataclass(frozen=True)
class Interval:
    """半开区间 [start, end)，end 必须严格大于 start。"""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError(f"empty interval: {self.start!r}..{self.end!r}")

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()


def merge_union(intervals: Iterable[Interval]) -> list[Interval]:
    """合并重叠或首尾相接的区间，返回互不相交、按起点排序的并集。"""
    merged: list[Interval] = []
    for iv in sorted(intervals, key=lambda i: (i.start, i.end)):
        if merged and iv.start <= merged[-1].end:
            last = merged[-1]
            if iv.end > last.end:
                merged[-1] = Interval(last.start, iv.end)
        else:
            merged.append(iv)
    return merged


def clip(interval: Interval, lo: datetime, hi: datetime) -> Optional[Interval]:
    """把区间裁剪到 [lo, hi] 内；裁剪后为空则返回 None。"""
    start, end = max(interval.start, lo), min(interval.end, hi)
    if end <= start:
        return None
    return Interval(start, end)


def total_seconds(intervals: Iterable[Interval]) -> float:
    return sum(iv.seconds for iv in intervals)
