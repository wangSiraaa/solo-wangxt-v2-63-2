"""迁移 0001：初始化 schema + 回填旧系统暂停事件。

旧事件来源表 legacy_pause_events（旧系统导出，时间为 ISO8601 字符串）：

    legacy_id  TEXT PRIMARY KEY
    case_id    INTEGER
    event_type TEXT
    reason     TEXT
    start_at   TEXT NOT NULL   —— 暂停开始
    end_at     TEXT            —— NULL 表示旧系统里“仍在暂停、没有落账”

迁移规则（保证旧事件迁移不留下半截停表）
----------------------------------------
1. 区间完整（start/end 齐全且 end > start）→ 直接导入为 ENDED 申请，
   申请/审批/结束字段全部补齐，审计链完整；
2. 区间不完整（end 为 NULL）→ **不允许**导入成悬空的生效暂停，
   一律在迁移截止点 cutoff 落账为 ENDED，并写 migration_note；
3. 数据非法（end <= start、引用不存在的案件）→ 隔离到 migration_issues；
4. 可重跑：legacy_ref 唯一约束 + 导入前查重；重复执行不会产生双份暂停；
5. 导入后执行一次幂等升级扫描，使升级/处罚与新的暂停数据对齐。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.clock import ensure_utc
from app.models import Base, MigrationIssue, PauseRequest, PauseStatus, RectificationCase
from app.services import DEFAULT_POLICY, EscalationPolicy, record_migration_issue, run_escalation_sweep

LEGACY_TABLE = "legacy_pause_events"

LEGACY_DDL = f"""
CREATE TABLE IF NOT EXISTS {LEGACY_TABLE} (
    legacy_id  TEXT PRIMARY KEY,
    case_id    INTEGER NOT NULL,
    event_type TEXT,
    reason     TEXT,
    start_at   TEXT NOT NULL,
    end_at     TEXT
)
"""


@dataclass
class MigrationSummary:
    imported: int = 0
    closed_open_ended: int = 0
    skipped_existing: int = 0
    issues: int = 0
    sweep: dict | None = None
    issue_details: list[dict] = field(default_factory=list)


def _parse_iso(value: str) -> datetime:
    return ensure_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def ensure_legacy_table(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text(LEGACY_DDL))


def run_migration(
    engine: Engine,
    clock,
    legacy_cutoff: datetime | None = None,
    policy: EscalationPolicy = DEFAULT_POLICY,
) -> MigrationSummary:
    Base.metadata.create_all(engine)
    ensure_legacy_table(engine)

    cutoff = ensure_utc(legacy_cutoff) if legacy_cutoff else clock.now()
    summary = MigrationSummary()

    with engine.begin() as conn:
        rows = conn.execute(
            text(
                f"SELECT legacy_id, case_id, event_type, reason, start_at, end_at "
                f"FROM {LEGACY_TABLE} ORDER BY legacy_id"
            )
        ).all()

    factory = sessionmaker(bind=engine)
    with factory() as session:
        assert isinstance(session, Session)
        for legacy_id, case_id, event_type, reason, start_s, end_s in rows:
            legacy_ref = str(legacy_id)

            # 4. 重跑去重：已导入的申请或已登记的问题都不重复处理
            existing_pause = session.scalar(
                select(PauseRequest).where(PauseRequest.legacy_ref == legacy_ref).limit(1)
            )
            existing_issue = session.scalar(
                select(MigrationIssue).where(MigrationIssue.legacy_ref == legacy_ref).limit(1)
            )
            if existing_pause is not None or existing_issue is not None:
                summary.skipped_existing += 1
                continue

            case = session.get(RectificationCase, case_id)
            start = _parse_iso(start_s)
            end = _parse_iso(end_s) if end_s else None

            # 3. 非法数据隔离
            if case is None:
                issue = record_migration_issue(
                    session, legacy_ref, f"legacy event references missing case {case_id}", clock
                )
                summary.issues += 1
                summary.issue_details.append({"legacy_ref": legacy_ref, "issue_id": issue.id})
                continue
            if end is not None and end <= start:
                issue = record_migration_issue(
                    session, legacy_ref, "legacy event has end <= start; rejected", clock
                )
                summary.issues += 1
                summary.issue_details.append({"legacy_ref": legacy_ref, "issue_id": issue.id})
                continue

            note = None
            # 2. 旧系统悬空暂停：在截止点落账，绝不留下半截停表
            if end is None:
                if cutoff <= start:
                    issue = record_migration_issue(
                        session,
                        legacy_ref,
                        f"open legacy pause starts ({start.isoformat()}) at/after cutoff "
                        f"({cutoff.isoformat()}); rejected",
                        clock,
                    )
                    summary.issues += 1
                    summary.issue_details.append({"legacy_ref": legacy_ref, "issue_id": issue.id})
                    continue
                end = cutoff
                note = "legacy open pause closed at migration cutoff (no dangling pause allowed)"
                summary.closed_open_ended += 1

            now = clock.now()
            session.add(
                PauseRequest(
                    case_id=case.id,
                    event_type=event_type or "LEGACY",
                    reason=reason or "imported from legacy system",
                    evidence=[f"legacy://pause_events/{legacy_ref}"],
                    requested_start=start,
                    requested_end=end,
                    status=PauseStatus.ENDED,
                    approved_start=start,
                    approved_end=end,
                    ended_at=end,
                    decided_by="migration-0001",
                    decision_reason=note or "imported from legacy system",
                    legacy_ref=legacy_ref,
                    migration_note=note,
                    created_at=now,
                    updated_at=now,
                )
            )
            summary.imported += 1

        session.commit()

        # 5. 幂等扫描对齐升级/处罚（崩溃重跑安全）
        summary.sweep = run_escalation_sweep(session, clock, policy)
        session.commit()

    return summary
