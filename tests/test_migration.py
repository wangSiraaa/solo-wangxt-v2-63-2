"""旧事件迁移验收：完整区间导入、悬空区间在截止点落账、非法数据隔离、
重跑幂等——旧事件迁移不留下半截停表。
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import text

from app.models import MigrationIssue, PauseRequest, PauseStatus
from migrations.m0001_init_and_legacy import ensure_legacy_table, run_migration
from tests.conftest import T0, create_case, trace


def _insert_legacy_rows(engine, rows):
    ensure_legacy_table(engine)
    with engine.begin() as conn:
        for legacy_id, case_id, start, end, event_type, reason in rows:
            conn.execute(
                text(
                    "INSERT INTO legacy_pause_events "
                    "(legacy_id, case_id, event_type, reason, start_at, end_at) "
                    "VALUES (:id, :cid, :et, :r, :s, :e)"
                ),
                {
                    "id": legacy_id,
                    "cid": case_id,
                    "et": event_type,
                    "r": reason,
                    "s": start,
                    "e": end,
                },
            )


def test_legacy_migration_backfills_without_dangling_pauses(client, app, clock):
    case = create_case(client, sla_hours=48)
    cid = case["id"]
    engine = app.state.engine

    _insert_legacy_rows(
        engine,
        [
            # 完整区间：暴雨 +1h..+3h
            ("LEG-001", cid, "2026-09-01T09:00:00Z", "2026-09-01T11:00:00Z", "暴雨", "old-system export"),
            # 悬空区间：+5h 开始、无结束（旧系统停表未落账）
            ("LEG-002", cid, "2026-09-01T13:00:00Z", None, "封路", "still open in old system"),
            # 非法区间：end <= start
            ("LEG-003", cid, "2026-09-01T16:00:00Z", "2026-09-01T15:00:00Z", "暴雨", "bad row"),
            # 引用不存在的案件
            ("LEG-004", 999, "2026-09-01T09:00:00Z", "2026-09-01T10:00:00Z", "暴雨", "orphan"),
        ],
    )

    cutoff = T0 + timedelta(hours=10)
    summary = run_migration(engine, clock, legacy_cutoff=cutoff)

    assert summary.imported == 2
    assert summary.closed_open_ended == 1
    assert summary.issues == 2

    with app.state.session_factory() as session:
        imported = session.query(PauseRequest).order_by(PauseRequest.legacy_ref).all()
        assert [p.legacy_ref for p in imported] == ["LEG-001", "LEG-002"]
        for pr in imported:
            # 审计链完整，绝无悬空生效暂停
            assert pr.status is PauseStatus.ENDED
            assert pr.approved_start is not None
            assert pr.ended_at is not None
            assert pr.decided_by == "migration-0001"
            assert pr.evidence == [f"legacy://pause_events/{pr.legacy_ref}"]

        dangling = (
            session.query(PauseRequest)
            .filter(PauseRequest.status != PauseStatus.ENDED)
            .all()
        )
        assert dangling == []  # 不留下半截停表

        open_ended = session.query(PauseRequest).filter_by(legacy_ref="LEG-002").one()
        assert open_ended.ended_at == cutoff
        assert open_ended.migration_note is not None

        issues = session.query(MigrationIssue).order_by(MigrationIssue.legacy_ref).all()
        assert [i.legacy_ref for i in issues] == ["LEG-003", "LEG-004"]

    # 追溯（时钟推进到截止点）：[+1,+3)=2h 与 [+5,+10)=5h，合计 7h
    clock.set(cutoff)
    tr = trace(client, cid)
    assert tr["paused_seconds"] == 7 * 3600
    assert len(tr["merged_pause_intervals"]) == 2
    assert tr["effective_elapsed_seconds"] == 3 * 3600  # 10 − 7


def test_migration_is_rerunnable(client, app, clock):
    case = create_case(client, sla_hours=48)
    cid = case["id"]
    engine = app.state.engine

    _insert_legacy_rows(
        engine,
        [
            ("LEG-101", cid, "2026-09-01T09:00:00Z", "2026-09-01T11:00:00Z", "暴雨", "x"),
            ("LEG-102", cid, "2026-09-01T07:00:00Z", "2026-09-01T06:00:00Z", "暴雨", "bad"),
        ],
    )

    cutoff = T0 + timedelta(hours=10)
    first = run_migration(engine, clock, legacy_cutoff=cutoff)
    assert first.imported == 1
    assert first.issues == 1

    # 模拟失败后重跑：不产生重复暂停，也不重复登记问题
    second = run_migration(engine, clock, legacy_cutoff=cutoff)
    assert second.imported == 0
    assert second.closed_open_ended == 0
    assert second.issues == 0
    assert second.skipped_existing == 2

    with app.state.session_factory() as session:
        assert session.query(PauseRequest).filter_by(legacy_ref="LEG-101").count() == 1
        assert session.query(MigrationIssue).filter_by(legacy_ref="LEG-102").count() == 1

    # 暂停查询 API 能查到迁移导入的记录（状态 ENDED）
    rows = client.get("/pause-requests", params={"case_id": cid}).json()
    assert len(rows) == 1
    assert rows[0]["legacy_ref"] == "LEG-101"
    assert rows[0]["status"] == "ENDED"
