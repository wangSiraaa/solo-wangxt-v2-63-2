"""HTTP API：申请 / 审批 / 结束 / 查询 / 追溯 / 任务。"""
from __future__ import annotations

from datetime import timedelta
from typing import Iterator, Optional

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import services
from .clock import Clock, ensure_utc
from .models import (
    CaseStatus,
    CompensationSuggestion,
    Escalation,
    PauseRequest,
    PauseStatus,
    Penalty,
    RectificationCase,
    SuggestionStatus,
)
from .schemas import (
    CaseCreate,
    CaseOut,
    CountedPauseOut,
    EscalationOut,
    IntervalOut,
    PauseApplyIn,
    PauseApproveIn,
    PauseCancelIn,
    PauseEndIn,
    PauseOut,
    PauseRejectIn,
    PenaltyCorrectionOut,
    PenaltyOut,
    SlaTraceOut,
    SuggestionConfirmIn,
    SuggestionDismissIn,
    SuggestionOut,
    SweepSummaryOut,
)

router = APIRouter()


# --------------------------------------------------------------------------- #
# 依赖注入：会话 / 时钟 / 策略
# --------------------------------------------------------------------------- #
def get_session(request: Request) -> Iterator[Session]:
    factory = request.app.state.session_factory
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()  # 失败整体回滚，不留半截停表
        raise
    finally:
        session.close()


def get_clock(request: Request) -> Clock:
    return request.app.state.clock


def get_policy(request: Request) -> services.EscalationPolicy:
    return request.app.state.policy


def _get_case(session: Session, case_id: int) -> RectificationCase:
    case = session.get(RectificationCase, case_id)
    if case is None:
        raise services.DomainError(404, f"case {case_id} not found")
    return case


def _get_pause(session: Session, pause_id: int) -> PauseRequest:
    pr = session.get(PauseRequest, pause_id)
    if pr is None:
        raise services.DomainError(404, f"pause request {pause_id} not found")
    return pr


def _get_penalty(session: Session, penalty_id: int) -> Penalty:
    penalty = session.get(Penalty, penalty_id)
    if penalty is None:
        raise services.DomainError(404, f"penalty {penalty_id} not found")
    return penalty


def _get_suggestion(session: Session, suggestion_id: int) -> CompensationSuggestion:
    suggestion = session.get(CompensationSuggestion, suggestion_id)
    if suggestion is None:
        raise services.DomainError(404, f"suggestion {suggestion_id} not found")
    return suggestion


# --------------------------------------------------------------------------- #
# 展示组装
# --------------------------------------------------------------------------- #
def _case_out(session: Session, case: RectificationCase, clock, policy) -> CaseOut:
    snap = services.compute_sla(session, case, clock.now(), policy)
    return CaseOut(
        id=case.id,
        title=case.title,
        status=case.status,
        started_at=case.started_at,
        sla_seconds=case.sla_seconds,
        due_at=case.due_at,
        closed_at=case.closed_at,
        effective_elapsed_seconds=snap.effective_elapsed_seconds,
        remaining_seconds=snap.remaining_seconds,
        is_overdue=snap.is_overdue,
        current_level=snap.current_level,
    )


def _penalty_out(penalty: Penalty) -> PenaltyOut:
    corrections = sorted(penalty.corrections, key=lambda c: c.id)
    effective = penalty.amount_cents + sum(c.amount_delta_cents for c in corrections)
    return PenaltyOut(
        id=penalty.id,
        case_id=penalty.case_id,
        escalation_id=penalty.escalation_id,
        amount_cents=penalty.amount_cents,
        effective_amount_cents=effective,
        status=penalty.status,
        reason=penalty.reason,
        created_at=penalty.created_at,
        locked_at=penalty.locked_at,
        corrections=[PenaltyCorrectionOut.model_validate(c) for c in corrections],
    )


# --------------------------------------------------------------------------- #
# 案件
# --------------------------------------------------------------------------- #
@router.post("/cases", response_model=CaseOut, status_code=201, tags=["cases"])
def create_case(
    payload: CaseCreate,
    session: Session = Depends(get_session),
    clock=Depends(get_clock),
    policy=Depends(get_policy),
):
    now = clock.now()
    started = ensure_utc(payload.started_at) if payload.started_at else now
    sla_seconds = int(payload.sla_hours * 3600)
    case = RectificationCase(
        title=payload.title,
        status=CaseStatus.OPEN,
        started_at=started,
        sla_seconds=sla_seconds,
        due_at=started + timedelta(seconds=sla_seconds),
        created_at=now,
        updated_at=now,
    )
    session.add(case)
    session.flush()
    return _case_out(session, case, clock, policy)


@router.get("/cases/{case_id}", response_model=CaseOut, tags=["cases"])
def get_case(
    case_id: int,
    session: Session = Depends(get_session),
    clock=Depends(get_clock),
    policy=Depends(get_policy),
):
    return _case_out(session, _get_case(session, case_id), clock, policy)


@router.post("/cases/{case_id}/close", response_model=CaseOut, tags=["cases"])
def close_case(
    case_id: int,
    session: Session = Depends(get_session),
    clock=Depends(get_clock),
    policy=Depends(get_policy),
):
    case = _get_case(session, case_id)
    services.close_case(session, case, clock)
    return _case_out(session, case, clock, policy)


# --------------------------------------------------------------------------- #
# 暂停申请：申请 / 审批 / 结束 / 撤销 / 查询
# --------------------------------------------------------------------------- #
@router.post(
    "/cases/{case_id}/pause-requests",
    response_model=PauseOut,
    status_code=201,
    tags=["pauses"],
)
def apply_pause(
    case_id: int,
    payload: PauseApplyIn,
    session: Session = Depends(get_session),
    clock=Depends(get_clock),
):
    case = _get_case(session, case_id)
    return services.apply_pause_request(
        session,
        case,
        event_type=payload.event_type,
        reason=payload.reason,
        evidence=payload.evidence,
        requested_start=ensure_utc(payload.requested_start) if payload.requested_start else None,
        requested_end=ensure_utc(payload.requested_end) if payload.requested_end else None,
        clock=clock,
    )


@router.get("/cases/{case_id}/pause-requests", response_model=list[PauseOut], tags=["pauses"])
def list_case_pauses(case_id: int, session: Session = Depends(get_session)):
    _get_case(session, case_id)
    return session.scalars(
        select(PauseRequest).where(PauseRequest.case_id == case_id).order_by(PauseRequest.id)
    ).all()


@router.get("/pause-requests", response_model=list[PauseOut], tags=["pauses"])
def list_pauses(
    case_id: Optional[int] = Query(default=None),
    status: Optional[PauseStatus] = Query(default=None),
    session: Session = Depends(get_session),
):
    stmt = select(PauseRequest).order_by(PauseRequest.id)
    if case_id is not None:
        stmt = stmt.where(PauseRequest.case_id == case_id)
    if status is not None:
        stmt = stmt.where(PauseRequest.status == status)
    return session.scalars(stmt).all()


@router.get("/pause-requests/{pause_id}", response_model=PauseOut, tags=["pauses"])
def get_pause(pause_id: int, session: Session = Depends(get_session)):
    return _get_pause(session, pause_id)


@router.post("/pause-requests/{pause_id}/approve", response_model=PauseOut, tags=["pauses"])
def approve_pause(
    pause_id: int,
    payload: PauseApproveIn,
    session: Session = Depends(get_session),
    clock=Depends(get_clock),
    policy=Depends(get_policy),
):
    pr = _get_pause(session, pause_id)
    return services.approve_pause_request(
        session,
        pr,
        approved_start=ensure_utc(payload.approved_start) if payload.approved_start else None,
        approved_end=ensure_utc(payload.approved_end) if payload.approved_end else None,
        decided_by=payload.decided_by,
        clock=clock,
        policy=policy,
    )


@router.post("/pause-requests/{pause_id}/reject", response_model=PauseOut, tags=["pauses"])
def reject_pause(
    pause_id: int,
    payload: PauseRejectIn,
    session: Session = Depends(get_session),
    clock=Depends(get_clock),
):
    pr = _get_pause(session, pause_id)
    return services.reject_pause_request(
        session, pr, decided_by=payload.decided_by, reason=payload.reason, clock=clock
    )


@router.post("/pause-requests/{pause_id}/end", response_model=PauseOut, tags=["pauses"])
def end_pause(
    pause_id: int,
    payload: PauseEndIn,
    session: Session = Depends(get_session),
    clock=Depends(get_clock),
):
    pr = _get_pause(session, pause_id)
    pr, _ = services.end_pause_request(
        session,
        pr,
        ended_at=ensure_utc(payload.ended_at) if payload.ended_at else None,
        clock=clock,
    )
    return pr


@router.post("/pause-requests/{pause_id}/cancel", response_model=PauseOut, tags=["pauses"])
def cancel_pause(
    pause_id: int,
    payload: PauseCancelIn,
    session: Session = Depends(get_session),
    clock=Depends(get_clock),
):
    pr = _get_pause(session, pause_id)
    return services.cancel_pause_request(session, pr, reason=payload.reason, clock=clock)


# --------------------------------------------------------------------------- #
# 升级 / 处罚 / 追溯
# --------------------------------------------------------------------------- #
@router.get("/cases/{case_id}/escalations", response_model=list[EscalationOut], tags=["escalations"])
def list_escalations(case_id: int, session: Session = Depends(get_session)):
    _get_case(session, case_id)
    return session.scalars(
        select(Escalation).where(Escalation.case_id == case_id).order_by(Escalation.level)
    ).all()


@router.get("/cases/{case_id}/penalties", response_model=list[PenaltyOut], tags=["penalties"])
def list_case_penalties(case_id: int, session: Session = Depends(get_session)):
    _get_case(session, case_id)
    penalties = session.scalars(
        select(Penalty).where(Penalty.case_id == case_id).order_by(Penalty.id)
    ).all()
    return [_penalty_out(p) for p in penalties]


@router.get("/penalties/{penalty_id}", response_model=PenaltyOut, tags=["penalties"])
def get_penalty(penalty_id: int, session: Session = Depends(get_session)):
    return _penalty_out(_get_penalty(session, penalty_id))


@router.post("/penalties/{penalty_id}/lock", response_model=PenaltyOut, tags=["penalties"])
def lock_penalty(
    penalty_id: int,
    session: Session = Depends(get_session),
    clock=Depends(get_clock),
):
    penalty = _get_penalty(session, penalty_id)
    services.lock_penalty(session, penalty, clock)
    return _penalty_out(penalty)


@router.get("/cases/{case_id}/sla-trace", response_model=SlaTraceOut, tags=["trace"])
def sla_trace(
    case_id: int,
    session: Session = Depends(get_session),
    clock=Depends(get_clock),
    policy=Depends(get_policy),
):
    case = _get_case(session, case_id)
    snap = services.compute_sla(session, case, clock.now(), policy)
    escalations = session.scalars(
        select(Escalation).where(Escalation.case_id == case.id).order_by(Escalation.level)
    ).all()
    return SlaTraceOut(
        case_id=case.id,
        case_status=case.status,
        now=snap.now,
        reference_at=snap.reference_at,
        started_at=snap.started_at,
        closed_at=snap.closed_at,
        sla_seconds=snap.sla_seconds,
        raw_elapsed_seconds=snap.raw_elapsed_seconds,
        paused_seconds=snap.paused_seconds,
        effective_elapsed_seconds=snap.effective_elapsed_seconds,
        remaining_seconds=snap.remaining_seconds,
        overdue_seconds=snap.overdue_seconds,
        is_overdue=snap.is_overdue,
        current_level=snap.current_level,
        counted_pause_intervals=[
            CountedPauseOut(
                request_id=pr.id,
                status=pr.status,
                start=iv.start,
                end=iv.end,
                seconds=iv.seconds,
            )
            for pr, iv in snap.counted
        ],
        merged_pause_intervals=[
            IntervalOut(start=iv.start, end=iv.end, seconds=iv.seconds) for iv in snap.merged
        ],
        escalations=[EscalationOut.model_validate(e) for e in escalations],
    )


# --------------------------------------------------------------------------- #
# 补偿建议
# --------------------------------------------------------------------------- #
@router.get("/compensation-suggestions", response_model=list[SuggestionOut], tags=["compensation"])
def list_suggestions(
    case_id: Optional[int] = Query(default=None),
    status: Optional[SuggestionStatus] = Query(default=None),
    session: Session = Depends(get_session),
):
    stmt = select(CompensationSuggestion).order_by(CompensationSuggestion.id)
    if case_id is not None:
        stmt = stmt.where(CompensationSuggestion.case_id == case_id)
    if status is not None:
        stmt = stmt.where(CompensationSuggestion.status == status)
    return session.scalars(stmt).all()


@router.post(
    "/compensation-suggestions/{suggestion_id}/confirm",
    response_model=SuggestionOut,
    tags=["compensation"],
)
def confirm_suggestion(
    suggestion_id: int,
    payload: SuggestionConfirmIn,
    session: Session = Depends(get_session),
    clock=Depends(get_clock),
):
    suggestion = _get_suggestion(session, suggestion_id)
    return services.confirm_suggestion(
        session, suggestion, decided_by=payload.decided_by, clock=clock
    )


@router.post(
    "/compensation-suggestions/{suggestion_id}/dismiss",
    response_model=SuggestionOut,
    tags=["compensation"],
)
def dismiss_suggestion(
    suggestion_id: int,
    payload: SuggestionDismissIn,
    session: Session = Depends(get_session),
    clock=Depends(get_clock),
):
    suggestion = _get_suggestion(session, suggestion_id)
    return services.dismiss_suggestion(
        session, suggestion, decided_by=payload.decided_by, reason=payload.reason, clock=clock
    )


# --------------------------------------------------------------------------- #
# 任务 / 健康检查
# --------------------------------------------------------------------------- #
@router.post("/jobs/escalation-sweep", response_model=SweepSummaryOut, tags=["jobs"])
def escalation_sweep(
    session: Session = Depends(get_session),
    clock=Depends(get_clock),
    policy=Depends(get_policy),
):
    return services.run_escalation_sweep(session, clock, policy)


@router.get("/healthz", tags=["meta"])
def healthz():
    return {"status": "ok"}
