"""持久化模型。

设计约束：
- 升级记录（Escalation）与处罚（Penalty）一旦写入**永不删除、永不改写**；
  历史更正只能通过 PenaltyCorrection 追加，保留完整处罚链；
- 暂停申请（PauseRequest）全程留痕：申请 → 批准生效 / 拒绝 → 结束 / 撤销；
- 所有时间统一 UTC。
"""
from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import JSON, Enum as SAEnum, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import DateTime, TypeDecorator


class UtcDateTime(TypeDecorator):
    """可安全穿越 SQLite 的 UTC aware datetime。"""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    pass


class CaseStatus(str, enum.Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"  # 结案：时钟永久停止，不得重新启动


class PauseStatus(str, enum.Enum):
    PENDING = "PENDING"      # 申请
    APPROVED = "APPROVED"    # 批准生效
    REJECTED = "REJECTED"    # 拒绝（终态）
    ENDED = "ENDED"          # 结束（终态）
    CANCELLED = "CANCELLED"  # 撤销（终态）


class PenaltyStatus(str, enum.Enum):
    OPEN = "OPEN"
    LOCKED = "LOCKED"  # 锁定后任何暂停都不得改写，只能追加更正


class SuggestionStatus(str, enum.Enum):
    PENDING_REVIEW = "PENDING_REVIEW"  # 待复核
    CONFIRMED = "CONFIRMED"            # 已确认（已追加更正）
    DISMISSED = "DISMISSED"            # 已驳回


class SuggestionKind(str, enum.Enum):
    PENALTY_CREDIT = "PENALTY_CREDIT"        # 建议冲抵处罚金额
    ESCALATION_REVIEW = "ESCALATION_REVIEW"  # 建议复核升级记录（仅标注，不删除）


class RectificationCase(Base):
    __tablename__ = "rectification_cases"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    status: Mapped[CaseStatus] = mapped_column(
        SAEnum(CaseStatus, native_enum=False, length=16), default=CaseStatus.OPEN
    )
    started_at: Mapped[datetime] = mapped_column(UtcDateTime)
    sla_seconds: Mapped[int] = mapped_column(Integer)
    due_at: Mapped[datetime] = mapped_column(UtcDateTime)  # started_at + sla，仅作展示
    closed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime)

    pauses: Mapped[list["PauseRequest"]] = relationship(back_populates="case")
    escalations: Mapped[list["Escalation"]] = relationship(back_populates="case")
    penalties: Mapped[list["Penalty"]] = relationship(back_populates="case")


class PauseRequest(Base):
    """SLA 暂停申请：绑定事件、原因与证据，全程状态留痕。"""

    __tablename__ = "pause_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("rectification_cases.id"), index=True)

    event_type: Mapped[str] = mapped_column(String(64))   # 事件类型：暴雨 / 封路 / ...
    reason: Mapped[str] = mapped_column(Text)             # 原因说明
    evidence: Mapped[list] = mapped_column(JSON, default=list)  # 证据（链接/编号列表）

    requested_start: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    requested_end: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    status: Mapped[PauseStatus] = mapped_column(
        SAEnum(PauseStatus, native_enum=False, length=16), default=PauseStatus.PENDING, index=True
    )

    # 审批结果：实际生效区间以 approved_* 为准（可能与申请窗口不同，如迟到批准）
    approved_start: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    approved_end: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    ended_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    # 迁移溯源：保证旧事件重复导入不产生半截停表
    legacy_ref: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)
    migration_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime)

    case: Mapped[RectificationCase] = relationship(back_populates="pauses")


class Escalation(Base):
    """升级记录：不可变事实。(case_id, level) 唯一，保证重跑不重复。"""

    __tablename__ = "escalations"
    __table_args__ = (UniqueConstraint("case_id", "level", name="uq_escalation_case_level"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("rectification_cases.id"), index=True)
    level: Mapped[int] = mapped_column(Integer)
    triggered_at: Mapped[datetime] = mapped_column(UtcDateTime)
    effective_overdue_seconds: Mapped[float] = mapped_column()  # 触发时的有效逾期时长
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    case: Mapped[RectificationCase] = relationship(back_populates="escalations")
    penalty: Mapped["Penalty | None"] = relationship(back_populates="escalation", uselist=False)


class Penalty(Base):
    """处罚：金额与状态永不就地修改；更正通过 corrections 链追加。"""

    __tablename__ = "penalties"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("rectification_cases.id"), index=True)
    escalation_id: Mapped[int | None] = mapped_column(
        ForeignKey("escalations.id"), unique=True, nullable=True
    )
    amount_cents: Mapped[int] = mapped_column(Integer)
    status: Mapped[PenaltyStatus] = mapped_column(
        SAEnum(PenaltyStatus, native_enum=False, length=16), default=PenaltyStatus.OPEN
    )
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    locked_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    case: Mapped[RectificationCase] = relationship(back_populates="penalties")
    escalation: Mapped["Escalation | None"] = relationship(back_populates="penalty")
    corrections: Mapped[list["PenaltyCorrection"]] = relationship(back_populates="penalty")


class PenaltyCorrection(Base):
    """处罚更正（追加式）：确认补偿建议后写入，原处罚记录保持不变。"""

    __tablename__ = "penalty_corrections"

    id: Mapped[int] = mapped_column(primary_key=True)
    penalty_id: Mapped[int] = mapped_column(ForeignKey("penalties.id"), index=True)
    suggestion_id: Mapped[int] = mapped_column(ForeignKey("compensation_suggestions.id"))
    amount_delta_cents: Mapped[int] = mapped_column(Integer)  # 通常为负（冲抵）
    note: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)

    penalty: Mapped[Penalty] = relationship(back_populates="corrections")


class CompensationSuggestion(Base):
    """补偿建议：迟到批准影响历史时的唯一出口。

    状态机：PENDING_REVIEW → CONFIRMED（追加更正） / DISMISSED（驳回）。
    绝不静默撤销已锁定处罚或删除已产生升级。
    """

    __tablename__ = "compensation_suggestions"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("rectification_cases.id"), index=True)
    pause_request_id: Mapped[int] = mapped_column(ForeignKey("pause_requests.id"), index=True)
    escalation_id: Mapped[int | None] = mapped_column(ForeignKey("escalations.id"), nullable=True)
    penalty_id: Mapped[int | None] = mapped_column(ForeignKey("penalties.id"), nullable=True)

    kind: Mapped[SuggestionKind] = mapped_column(SAEnum(SuggestionKind, native_enum=False, length=32))
    amount_cents: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str] = mapped_column(Text)

    status: Mapped[SuggestionStatus] = mapped_column(
        SAEnum(SuggestionStatus, native_enum=False, length=16),
        default=SuggestionStatus.PENDING_REVIEW,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    decided_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


class MigrationIssue(Base):
    """迁移隔离区：无法导入的旧事件在此留痕，绝不静默丢弃。"""

    __tablename__ = "migration_issues"

    id: Mapped[int] = mapped_column(primary_key=True)
    legacy_ref: Mapped[str] = mapped_column(String(128), index=True)
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
