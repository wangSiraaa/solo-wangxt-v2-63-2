"""可注入时钟：所有与时间相关的计算都通过 Clock 获取当前时间。

生产环境使用 SystemClock；测试与批量重放使用 ManualClock，
保证升级计算、暂停扣减、迟到批准判定完全可复现。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol


def ensure_utc(value: datetime) -> datetime:
    """把外部传入的时间统一为 UTC  aware datetime（naive 视为 UTC）。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class ManualClock:
    """确定性时钟，用于测试与迁移重放。"""

    def __init__(self, start: datetime):
        self._now = ensure_utc(start)

    def now(self) -> datetime:
        return self._now

    def set(self, value: datetime) -> None:
        self._now = ensure_utc(value)

    def advance(self, **kwargs) -> datetime:
        self._now = self._now + timedelta(**kwargs)
        return self._now
