"""核心领域服务：SLA 计算、升级扫描、暂停状态机、补偿建议。

关键不变量
----------
1. 有效时长 = 原始时长 − 已批准暂停区间在统计窗口内的**并集**；重叠不重复扣。
2. 升级扫描基于注入时钟，逐案件事务化、可重跑；Escalation/Penalty 永不删除。
3. 结案后时钟冻结在 closed_at；禁止新建暂停，扫描不再产生升级。
4. 迟到批准（approved_start < now）不回改历史，只产生 PENDING_REVIEW 建议；
   建议确认后以 PenaltyCorrection 追加更正，原处罚链完整保留。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .clock import ensure_utc
from .intervals import Interval, clip, merge_union, total_seconds
from .models import (
    CaseStatus,
    CompensationSuggestion,
    Escalation,
    MigrationIssue,
    PauseRequest,
    PauseStatus,
    Penalty,
    PenaltyCorrection,
    PenaltyStatus,
    RectificationCase,
    SuggestionKind,
    SuggestionStatus,
)

# 批准后参与计时的状态。
# CANCELLED 仅在“先批准后撤销”时参与计时（此时 ended_at 已写入），
# 未经批准即撤销的申请 approved_start 为空，天然被排除。
_COUNTED_STATUSES = (PauseStatus.APPROVED, PauseStatus.ENDED, PauseStatus.CANCELLED)
_TERMINAL_STATUSES = (PauseStatus.REJECTED, PauseStatus.ENDED, PauseStatus.CANCELLED)


class DomainError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


# --------------------------------------------------------------------------- #
# 升级策略
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class EscalationPolicy:
    """(级别, 触发阈值=有效逾期秒数, 处罚金额/分，None 表示无处罚)。"""

    tiers: tuple[tuple[int, float, Optional[int]], ...] = (
        (1, 0.0, None),                    # 逾期即 L1 警告
        (2, 24 * 3600.0, 100_00),          # 有效逾期 24h → L2 + ¥100
        (3, 72 * 3600.0, 300_00),          # 有效逾期 72h → L3 + ¥300
    )

    def threshold_for(self, level: int) -> float:
        for lv, threshold, _ in self.tiers:
            if lv == level:
                return threshold
        raise KeyError(level)

    def penalty_for(self, level: int) -> Optional[int]:
        for lv, _, amount in self.tiers:
            if lv == level:
                return amount
        raise KeyError(level)


DEFAULT_POLICY = EscalationPolicy()


# --------------------------------------------------------------------------- #
# SLA 计算（时间区间服务在领域内的应用）
# --------------------------------------------------------------------------- #
def request_interval(pr: PauseRequest, reference_at: datetime) -> Optional[Interval]:
    """申请在 reference_at 时点实际生效的暂停区间（尚未裁剪到案件窗口）。"""
    if pr.approved_start is None or pr.status not in _COUNTED_STATUSES:
        return None
    end = pr.ended_at or pr.approved_end or reference_at
    if pr.approved_end is not None:
        end = min(end, pr.approved_end)
    end = min(end, reference_at)
    if end <= pr.approved_start:
        return None
    return Interval(pr.approved_start, end)


@dataclass
class SlaSnapshot:
    case_id: int
    now: datetime
    reference_at: datetime
    started_at: datetime
    closed_at: Optional[datetime]
    sla_seconds: int
    raw_elapsed_seconds: float
    paused_seconds: float
    effective_elapsed_seconds: float
    remaining_seconds: float
    overdue_delta_seconds: float  # effective − sla，可能为负；阈值判定必须用它
    overdue_seconds: float        # 逾期秒数，未逾期为 0（展示用）
    is_overdue: bool
    current_level: int
    counted: list[tuple[PauseRequest, Interval]] = field(default_factory=list)
    merged: list[Interval] = field(default_factory=list)


def compute_sla(
    session: Session, case: RectificationCase, now: datetime, policy: EscalationPolicy
) -> SlaSnapshot:
    """以注入时钟 now 计算案件有效时长。结案案件冻结在 closed_at。"""
    now = ensure_utc(now)
    reference_at = min(now, case.closed_at) if case.closed_at else now

    raw = max((reference_at - case.started_at).total_seconds(), 0.0)

    counted: list[tuple[PauseRequest, Interval]] = []
    requests = session.scalars(
        select(PauseRequest).where(PauseRequest.case_id == case.id)
    ).all()
    for pr in requests:
        interval = request_interval(pr, reference_at)
        if interval is None:
            continue
        clipped = clip(interval, case.started_at, reference_at)
        if clipped is not None:
            counted.append((pr, clipped))

    merged = merge_union(iv for _, iv in counted)  # 并集 —— 重叠绝不双算
    paused = total_seconds(merged)
    effective = max(raw - paused, 0.0)

    remaining = max(case.sla_seconds - effective, 0.0)
    overdue_delta = effective - case.sla_seconds
    overdue = max(overdue_delta, 0.0)
    level = 0
    for lv, threshold, _ in policy.tiers:
        if overdue_delta >= threshold:
            level = max(level, lv)

    return SlaSnapshot(
        case_id=case.id,
        now=now,
        reference_at=reference_at,
        started_at=case.started_at,
        closed_at=case.closed_at,
        sla_seconds=case.sla_seconds,
        raw_elapsed_seconds=raw,
        paused_seconds=paused,
        effective_elapsed_seconds=effective,
        remaining_seconds=remaining,
        overdue_delta_seconds=overdue_delta,
        overdue_seconds=overdue,
        is_overdue=overdue_delta >= 0,
        current_level=level,
        counted=counted,
        merged=merged,
    )


# --------------------------------------------------------------------------- #
# 升级扫描（可重启重跑，不留下半截停表）
# --------------------------------------------------------------------------- #
def run_escalation_sweep(
    session: Session, clock, policy: EscalationPolicy = DEFAULT_POLICY
) -> dict:
    now = clock.now()
    summary = {
        "ran_at": now.isoformat(),
        "cases_scanned": 0,
        "escalations_created": 0,
        "penalties_created": 0,
    }
    cases = session.scalars(
        select(RectificationCase).where(RectificationCase.status == CaseStatus.OPEN)
    ).all()

    for case in cases:
        summary["cases_scanned"] += 1
        snapshot = compute_sla(session, case, now, policy)
        for level, threshold, _ in policy.tiers:
            if snapshot.overdue_delta_seconds >= threshold:
                _, esc_created, penalty_created = _ensure_escalation(
                    session, case, level, now, snapshot.overdue_seconds, policy
                )
                summary["escalations_created"] += int(esc_created)
                summary["penalties_created"] += int(penalty_created)
    return summary


def _ensure_escalation(
    session: Session,
    case: RectificationCase,
    level: int,
    now: datetime,
    overdue_seconds: float,
    policy: EscalationPolicy,
) -> tuple[Escalation, bool, bool]:
    """存在则返回，不存在则在保存点内原子写入；唯一约束兜底并发/重跑。"""
    existing = session.scalar(
        select(Escalation).where(Escalation.case_id == case.id, Escalation.level == level)
    )
    if existing is not None:
        return existing, False, False

    try:
        with session.begin_nested():
            escalation = Escalation(
                case_id=case.id,
                level=level,
                triggered_at=now,
                effective_overdue_seconds=overdue_seconds,
                note=f"auto-escalation L{level} at effective overdue {int(overdue_seconds)}s",
            )
            session.add(escalation)
            session.flush()

            penalty_created = False
            amount = policy.penalty_for(level)
            if amount is not None:
                session.add(
                    Penalty(
                        case_id=case.id,
                        escalation_id=escalation.id,
                        amount_cents=amount,
                        status=PenaltyStatus.OPEN,
                        reason=f"automatic penalty for escalation L{level}",
                        created_at=now,
                    )
                )
                session.flush()
                penalty_created = True
        return escalation, True, penalty_created
    except IntegrityError:
        # 并发扫描或崩溃重放：另一事务已写入，读取已有记录，不重复升级。
        existing = session.scalar(
            select(Escalation).where(Escalation.case_id == case.id, Escalation.level == level)
        )
        assert existing is not None
        return existing, False, False


# --------------------------------------------------------------------------- #
# 暂停申请状态机
# --------------------------------------------------------------------------- #
def apply_pause_request(
    session: Session,
    case: RectificationCase,
    *,
    event_type: str,
    reason: str,
    evidence: list[str],
    requested_start: Optional[datetime],
    requested_end: Optional[datetime],
    clock,
    legacy_ref: Optional[str] = None,
) -> PauseRequest:
    if case.status == CaseStatus.CLOSED:
        # 结案后不得重新启动：新申请直接拒绝。
        raise DomainError(409, "case is closed; pause requests cannot be started after closure")
    if requested_start is not None and requested_end is not None and requested_end <= requested_start:
        raise DomainError(422, "requested_end must be after requested_start")

    now = clock.now()
    pr = PauseRequest(
        case_id=case.id,
        event_type=event_type,
        reason=reason,
        evidence=evidence,
        requested_start=requested_start,
        requested_end=requested_end,
        status=PauseStatus.PENDING,
        legacy_ref=legacy_ref,
        created_at=now,
        updated_at=now,
    )
    session.add(pr)
    session.flush()
    return pr


def approve_pause_request(
    session: Session,
    pr: PauseRequest,
    *,
    approved_start: Optional[datetime],
    approved_end: Optional[datetime],
    decided_by: str,
    clock,
    policy: EscalationPolicy = DEFAULT_POLICY,
) -> PauseRequest:
    if pr.status != PauseStatus.PENDING:
        # 审批失败：除状态机合法迁移外什么都不改动，不会留下半截停表。
        raise DomainError(409, f"cannot approve request in status {pr.status.value}")

    now = clock.now()
    start = approved_start or pr.requested_start or now
    end = approved_end if approved_end is not None else pr.requested_end
    if end is not None and end <= start:
        raise DomainError(422, "approved_end must be after approved_start")

    pr.status = PauseStatus.APPROVED
    pr.approved_start = start
    pr.approved_end = end
    pr.decided_by = decided_by
    pr.updated_at = now
    session.flush()

    if start < now:
        # 迟到批准：可能影响历史。只生成待复核建议，不删除升级、不改写处罚。
        _generate_compensation_suggestions(session, pr, clock, policy)
    return pr


def reject_pause_request(
    session: Session, pr: PauseRequest, *, decided_by: str, reason: str, clock
) -> PauseRequest:
    if pr.status != PauseStatus.PENDING:
        raise DomainError(409, f"cannot reject request in status {pr.status.value}")
    now = clock.now()
    pr.status = PauseStatus.REJECTED
    pr.decided_by = decided_by
    pr.decision_reason = reason
    pr.updated_at = now
    session.flush()
    return pr


def end_pause_request(
    session: Session, pr: PauseRequest, *, ended_at: Optional[datetime], clock
) -> tuple[PauseRequest, bool]:
    """结束暂停。返回 (申请, 本次是否真的执行了结束)。

    对已结束的申请幂等返回：重复结束请求不会改写 ended_at，也不会双算时长。
    """
    if pr.status == PauseStatus.ENDED:
        return pr, False
    if pr.status != PauseStatus.APPROVED:
        raise DomainError(409, f"cannot end request in status {pr.status.value}")

    now = clock.now()
    end = ended_at or now
    if pr.approved_start is not None and end < pr.approved_start:
        raise DomainError(422, "ended_at cannot be before approved_start")

    pr.status = PauseStatus.ENDED
    pr.ended_at = end
    pr.updated_at = now
    session.flush()
    return pr, True


def cancel_pause_request(
    session: Session, pr: PauseRequest, *, reason: Optional[str], clock
) -> PauseRequest:
    """撤销：申请阶段撤销则从未生效；生效后撤销，已走表的过去不回改，
    区间计算到撤销时刻为止（ended_at 落账）。"""
    now = clock.now()
    if pr.status == PauseStatus.PENDING:
        pr.status = PauseStatus.CANCELLED
    elif pr.status == PauseStatus.APPROVED:
        pr.status = PauseStatus.CANCELLED
        pr.ended_at = pr.ended_at or now
    else:
        raise DomainError(409, f"cannot cancel request in terminal status {pr.status.value}")

    if reason:
        pr.decision_reason = reason
    pr.updated_at = now
    session.flush()
    return pr


def close_case(session: Session, case: RectificationCase, clock) -> tuple[RectificationCase, bool]:
    """结案：时钟永久冻结。重复结案幂等。"""
    if case.status == CaseStatus.CLOSED:
        return case, False
    now = clock.now()
    case.status = CaseStatus.CLOSED
    case.closed_at = now
    case.updated_at = now
    session.flush()
    return case, True


def lock_penalty(session: Session, penalty: Penalty, clock) -> tuple[Penalty, bool]:
    if penalty.status == PenaltyStatus.LOCKED:
        return penalty, False
    penalty.status = PenaltyStatus.LOCKED
    penalty.locked_at = clock.now()
    session.flush()
    return penalty, True


# --------------------------------------------------------------------------- #
# 迟到批准 → 补偿建议（历史不可变，只追加）
# --------------------------------------------------------------------------- #
def _generate_compensation_suggestions(
    session: Session, pr: PauseRequest, clock, policy: EscalationPolicy
) -> list[CompensationSuggestion]:
    """对每个“若当时已含本次暂停便不会触发”的升级生成建议。

    反事实计算：在升级触发时点用当前全部已批准暂停（含本次迟到批准）
    重新计算有效逾期；低于该级别阈值即说明历史结果被改变。
    """
    now = clock.now()
    case = session.get(RectificationCase, pr.case_id)
    assert case is not None and pr.approved_start is not None

    created: list[CompensationSuggestion] = []
    escalations = session.scalars(
        select(Escalation)
        .where(Escalation.case_id == case.id, Escalation.triggered_at > pr.approved_start)
        .order_by(Escalation.level)
    ).all()

    for escalation in escalations:
        counterfactual = compute_sla(session, case, escalation.triggered_at, policy)
        threshold = policy.threshold_for(escalation.level)
        if counterfactual.overdue_delta_seconds >= threshold:
            continue  # 即使扣除本次暂停，该升级仍会触发，无需补偿

        # 同一暂停对同一升级只出一条建议（审批只发生一次，这里再防重）。
        duplicate = session.scalar(
            select(CompensationSuggestion).where(
                CompensationSuggestion.pause_request_id == pr.id,
                CompensationSuggestion.escalation_id == escalation.id,
            )
        )
        if duplicate is not None:
            continue

        # 同一升级已有待复核或已确认的建议（可能来自另一条迟到暂停）时不再重复出建议，
        # 避免多条全冲抵建议被确认后超额补偿；驳回的建议允许再次提议。
        live_suggestion = session.scalar(
            select(CompensationSuggestion).where(
                CompensationSuggestion.escalation_id == escalation.id,
                CompensationSuggestion.status.in_(
                    [SuggestionStatus.PENDING_REVIEW, SuggestionStatus.CONFIRMED]
                ),
            )
        )
        if live_suggestion is not None:
            continue

        penalty = session.scalar(
            select(Penalty).where(Penalty.escalation_id == escalation.id)
        )
        if penalty is not None:
            locked_note = "LOCKED" if penalty.status == PenaltyStatus.LOCKED else "open"
            suggestion = CompensationSuggestion(
                case_id=case.id,
                pause_request_id=pr.id,
                escalation_id=escalation.id,
                penalty_id=penalty.id,
                kind=SuggestionKind.PENALTY_CREDIT,
                amount_cents=penalty.amount_cents,
                reason=(
                    f"late-approved pause request #{pr.id} ({pr.event_type}) covers this "
                    f"escalation window; penalty #{penalty.id} ({locked_note}, "
                    f"{penalty.amount_cents} cents) would not have been issued. "
                    f"Pending review: credit instead of silent reversal."
                ),
                created_at=now,
            )
        else:
            suggestion = CompensationSuggestion(
                case_id=case.id,
                pause_request_id=pr.id,
                escalation_id=escalation.id,
                kind=SuggestionKind.ESCALATION_REVIEW,
                amount_cents=0,
                reason=(
                    f"late-approved pause request #{pr.id} ({pr.event_type}) covers this "
                    f"escalation window; escalation L{escalation.level} would not have "
                    f"fired on time. Escalation record is retained; review only."
                ),
                created_at=now,
            )
        session.add(suggestion)
        session.flush()
        created.append(suggestion)
    return created


def confirm_suggestion(
    session: Session, suggestion: CompensationSuggestion, *, decided_by: str, clock
) -> CompensationSuggestion:
    """确认补偿建议：追加更正，绝不就地改写原处罚。"""
    if suggestion.status != SuggestionStatus.PENDING_REVIEW:
        raise DomainError(409, f"suggestion already {suggestion.status.value}")

    now = clock.now()
    suggestion.status = SuggestionStatus.CONFIRMED
    suggestion.decided_by = decided_by
    suggestion.decided_at = now

    if (
        suggestion.kind == SuggestionKind.PENALTY_CREDIT
        and suggestion.penalty_id is not None
        and suggestion.amount_cents > 0
    ):
        correction = PenaltyCorrection(
            penalty_id=suggestion.penalty_id,
            suggestion_id=suggestion.id,
            amount_delta_cents=-suggestion.amount_cents,
            note=(
                f"credit for late-approved pause request #{suggestion.pause_request_id}; "
                f"original penalty chain retained"
            ),
            created_at=now,
        )
        session.add(correction)
    session.flush()
    return suggestion


def dismiss_suggestion(
    session: Session, suggestion: CompensationSuggestion, *, decided_by: str, reason: str, clock
) -> CompensationSuggestion:
    if suggestion.status != SuggestionStatus.PENDING_REVIEW:
        raise DomainError(409, f"suggestion already {suggestion.status.value}")
    suggestion.status = SuggestionStatus.DISMISSED
    suggestion.decided_by = decided_by
    suggestion.decided_at = clock.now()
    suggestion.reason = suggestion.reason + f"\n[dismissed] {reason}"
    session.flush()
    return suggestion


# --------------------------------------------------------------------------- #
# 迁移辅助：旧事件落账，不留半截停表
# --------------------------------------------------------------------------- #
def record_migration_issue(
    session: Session, legacy_ref: str, message: str, clock
) -> MigrationIssue:
    issue = MigrationIssue(legacy_ref=legacy_ref, message=message, created_at=clock.now())
    session.add(issue)
    session.flush()
    return issue
