"""升级扫描验收。

覆盖验收点：
- 基于注入时钟的升级按有效逾期触发（L1/L2/L3 与处罚）；
- 重启重跑幂等，不产生重复升级/处罚，不留半截停表；
- 暂停中整改结案后不再升级（结案冻结时钟）；
- 暂停不能删除已产生的升级（即使反事实下不该触发，记录也保留，
  只进入补偿建议流程，见 test_compensation.py）。
"""
from __future__ import annotations

from tests.conftest import apply_pause, approve, create_case, end_pause, sweep, trace


def test_escalation_ladder_by_effective_overdue(client, clock):
    case = create_case(client, sla_hours=1)
    cid = case["id"]

    clock.advance(hours=1)   # 有效逾期 0h → L1
    sweep(client)
    clock.advance(hours=25)  # 有效逾期 24h → L2 + 处罚 100_00
    sweep(client)
    clock.advance(hours=48)  # 有效逾期 72h → L3 + 处罚 300_00
    summary = sweep(client)

    assert summary["escalations_created"] == 1  # 本次仅 L3
    escs = client.get(f"/cases/{cid}/escalations").json()
    assert [e["level"] for e in escs] == [1, 2, 3]

    penalties = client.get(f"/cases/{cid}/penalties").json()
    assert [(p["amount_cents"], p["status"]) for p in penalties] == [
        (100_00, "OPEN"),
        (300_00, "OPEN"),
    ]

    body = client.get(f"/cases/{cid}").json()
    assert body["current_level"] == 3


def test_sweep_is_idempotent_across_restarts(client, clock):
    """重启重跑：多次扫描不双算，不产生重复升级或处罚。"""
    case = create_case(client, sla_hours=1)
    cid = case["id"]

    clock.advance(hours=25)
    first = sweep(client)
    assert first["escalations_created"] == 2
    assert first["penalties_created"] == 1

    # 模拟进程重启：同一时刻反复执行
    for _ in range(3):
        rerun = sweep(client)
        assert rerun["escalations_created"] == 0
        assert rerun["penalties_created"] == 0

    escs = client.get(f"/cases/{cid}/escalations").json()
    assert [e["level"] for e in escs] == [1, 2]
    penalties = client.get(f"/cases/{cid}/penalties").json()
    assert len(penalties) == 1

    # 继续前进后扫描：只补齐缺失级别，已有级别不重放
    clock.advance(hours=48)  # T0+73h → 有效逾期 72h
    sweep(client)
    escs = client.get(f"/cases/{cid}/escalations").json()
    assert [e["level"] for e in escs] == [1, 2, 3]


def test_close_during_pause_prevents_further_escalation(client, clock):
    """暂停中整改结案：即使墙上时间越过更高级别阈值，也不再升级。"""
    case = create_case(client, sla_hours=1)
    cid = case["id"]

    clock.advance(minutes=30)
    pr = apply_pause(client, cid, requested_start=clock.now().isoformat())
    approve(client, pr["id"])

    # T0+45m：暂停中结案（原始 45m、暂停 15m、有效 30m）
    clock.advance(minutes=15)
    client.post(f"/cases/{cid}/close")

    clock.advance(hours=24 * 30)  # 一个月后
    summary = sweep(client)
    assert summary["cases_scanned"] == 0
    assert summary["escalations_created"] == 0
    assert client.get(f"/cases/{cid}/escalations").json() == []

    body = client.get(f"/cases/{cid}").json()
    assert body["status"] == "CLOSED"
    assert body["current_level"] == 0

    tr = trace(client, cid)
    assert tr["reference_at"] == tr["closed_at"]  # 时钟冻结
    assert tr["paused_seconds"] == 15 * 60       # [+30m,+45m)
    assert tr["effective_elapsed_seconds"] == 30 * 60  # 45m 原始 − 15m 暂停


def test_closed_case_pause_bookkeeping_does_not_reopen_clock(client, clock):
    """结案后对既有暂停做结束/撤销只是账面收尾，不会重新走表。"""
    case = create_case(client, sla_hours=1)
    cid = case["id"]

    clock.advance(minutes=30)
    pr = apply_pause(client, cid, requested_start=clock.now().isoformat())
    approve(client, pr["id"])
    clock.advance(minutes=15)
    client.post(f"/cases/{cid}/close")

    clock.advance(hours=100)
    resp = client.post(f"/pause-requests/{pr['id']}/end", json={})
    assert resp.status_code == 200  # 允许账面收尾
    assert resp.json()["status"] == "ENDED"

    tr = trace(client, cid)
    assert tr["reference_at"] == tr["closed_at"]
    assert tr["paused_seconds"] == 15 * 60  # 截止到结案，后续 100h 不算


def test_existing_escalations_are_never_deleted(client, clock):
    """事后暂停不删除任何已产生升级（补偿流程另见 test_compensation.py）。"""
    case = create_case(client, sla_hours=1)
    cid = case["id"]

    clock.advance(hours=25)
    sweep(client)
    before = client.get(f"/cases/{cid}/escalations").json()
    assert [e["level"] for e in before] == [1, 2]

    # 迟到批准覆盖历史 → 只产生建议，不删除升级
    pr = apply_pause(client, cid, event_type="封路")
    client.post(
        f"/pause-requests/{pr['id']}/approve",
        json={
            "decided_by": "approver-li",
            "approved_start": "2026-09-01T09:00:00Z",  # T0+1h
            "approved_end": "2026-09-02T08:00:00Z",    # T0+24h
        },
    )

    after = client.get(f"/cases/{cid}/escalations").json()
    assert [e["id"] for e in after] == [e["id"] for e in before]
    assert [e["level"] for e in after] == [1, 2]
