"""API 出入参模型（OpenAPI 文档来源）。"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from .models import (
    CaseStatus,
    PauseStatus,
    PenaltyStatus,
    SuggestionKind,
    SuggestionStatus,
)


# --------------------------------------------------------------------------- #
# 案件
# --------------------------------------------------------------------------- #
class CaseCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    sla_hours: float = Field(default=48.0, gt=0, description="SLA 时限（小时）")
    started_at: Optional[datetime] = Field(default=None, description="缺省为当前时钟")


class CaseOut(BaseModel):
    id: int
    title: str
    status: CaseStatus
    started_at: datetime
    sla_seconds: int
    due_at: datetime
    closed_at: Optional[datetime]
    effective_elapsed_seconds: float = Field(description="扣除已批准暂停并集后的有效时长")
    remaining_seconds: float = Field(description="扣除暂停后的剩余时限")
    is_overdue: bool
    current_level: int


# --------------------------------------------------------------------------- #
# 暂停申请
# --------------------------------------------------------------------------- #
class PauseApplyIn(BaseModel):
    event_type: str = Field(min_length=1, max_length=64, description="事件类型，如 暴雨/封路")
    reason: str = Field(min_length=1, description="暂停原因")
    evidence: list[str] = Field(min_length=1, description="证据链接/编号，至少一条")
    requested_start: Optional[datetime] = None
    requested_end: Optional[datetime] = None


class PauseApproveIn(BaseModel):
    decided_by: str = Field(min_length=1, max_length=64)
    approved_start: Optional[datetime] = Field(
        default=None, description="缺省取申请起点，再缺省取当前时钟；早于当前时钟即迟到批准"
    )
    approved_end: Optional[datetime] = None


class PauseRejectIn(BaseModel):
    decided_by: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1)


class PauseEndIn(BaseModel):
    ended_at: Optional[datetime] = Field(default=None, description="缺省为当前时钟")


class PauseCancelIn(BaseModel):
    reason: Optional[str] = None


class PauseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    case_id: int
    event_type: str
    reason: str
    evidence: list[str]
    requested_start: Optional[datetime]
    requested_end: Optional[datetime]
    status: PauseStatus
    approved_start: Optional[datetime]
    approved_end: Optional[datetime]
    decided_by: Optional[str]
    decision_reason: Optional[str]
    ended_at: Optional[datetime]
    legacy_ref: Optional[str]
    migration_note: Optional[str]
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------------- #
# 升级 / 处罚 / 补偿
# --------------------------------------------------------------------------- #
class EscalationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    case_id: int
    level: int
    triggered_at: datetime
    effective_overdue_seconds: float
    note: Optional[str]


class PenaltyCorrectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    penalty_id: int
    suggestion_id: int
    amount_delta_cents: int
    note: str
    created_at: datetime


class PenaltyOut(BaseModel):
    id: int
    case_id: int
    escalation_id: Optional[int]
    amount_cents: int = Field(description="原始金额，永不改写")
    effective_amount_cents: int = Field(description="原始金额 + 已确认更正之和")
    status: PenaltyStatus
    reason: str
    created_at: datetime
    locked_at: Optional[datetime]
    corrections: list[PenaltyCorrectionOut] = Field(
        default_factory=list, description="追加式更正链"
    )


class SuggestionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    case_id: int
    pause_request_id: int
    escalation_id: Optional[int]
    penalty_id: Optional[int]
    kind: SuggestionKind
    amount_cents: int
    reason: str
    status: SuggestionStatus
    created_at: datetime
    decided_at: Optional[datetime]
    decided_by: Optional[str]


class SuggestionConfirmIn(BaseModel):
    decided_by: str = Field(min_length=1, max_length=64)


class SuggestionDismissIn(BaseModel):
    decided_by: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1)


# --------------------------------------------------------------------------- #
# 追溯输出
# --------------------------------------------------------------------------- #
class IntervalOut(BaseModel):
    start: datetime
    end: datetime
    seconds: float


class CountedPauseOut(IntervalOut):
    request_id: int
    status: PauseStatus


class SlaTraceOut(BaseModel):
    """追溯输出：有效时长如何由暂停并集扣减而来，全程可审计。"""

    case_id: int
    case_status: CaseStatus
    now: datetime
    reference_at: datetime = Field(description="统计时点；结案后冻结在 closed_at")
    started_at: datetime
    closed_at: Optional[datetime]
    sla_seconds: int
    raw_elapsed_seconds: float
    paused_seconds: float = Field(description="已批准暂停区间并集总时长（重叠不双算）")
    effective_elapsed_seconds: float
    remaining_seconds: float
    overdue_seconds: float
    is_overdue: bool
    current_level: int
    counted_pause_intervals: list[CountedPauseOut]
    merged_pause_intervals: list[IntervalOut] = Field(description="并集合并后的区间")
    escalations: list[EscalationOut]


class SweepSummaryOut(BaseModel):
    ran_at: str
    cases_scanned: int
    escalations_created: int
    penalties_created: int
