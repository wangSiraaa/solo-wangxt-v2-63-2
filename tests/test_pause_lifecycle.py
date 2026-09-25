"""暂停申请状态机 + SLA 续算验收。

覆盖验收点：
- 批准后剩余时长正确续算；
- 重叠暂停不双算，重复结束请求不双算；
- 拒绝/撤销不产生扣减；
- 审批失败不留半截停表；
- 结案后不得重新启动。
"""
from __future__ import annotations

from tests.conftest import apply_pause, approve, create_case, end_pause, reject, sweep, trace


def test_approved_pause_then_remaining_resumes_correctly(client, clock):
    """批准后剩余时长正确续算：48h SLA，[+10h,+20h) 暂停，
    到 +30h 时有效时长应为 20h、剩余 28h。"""
    case = create_case(client, sla_hours=48)
    cid = case["id"]

    clock.advance(hours=10)
    pr = apply_pause(client, cid, requested_start=clock.now().isoformat())
    approve(client, pr["id"])  # 生效起点 = 申请起点 = T0+10h

    clock.advance(hours=10)  # T0+20h：暂停中
    end_pause(client, pr["id"])

    clock.advance(hours=10)  # T0+30h
    body = client.get(f"/cases/{cid}").json()
    assert body["effective_elapsed_seconds"] == 20 * 3600
    assert body["remaining_seconds"] == 28 * 3600
    assert body["is_overdue"] is False

    tr = trace(client, cid)
    assert tr["paused_seconds"] == 10 * 3600
    assert tr["raw_elapsed_seconds"] == 30 * 3600
    assert len(tr["merged_pause_intervals"]) == 1


def test_overlapping_pauses_not_double_counted(client, clock):
    """两条重叠暂停 [+10,+20) 与 [+15,+25)：并集 15h，不是 20h。"""
    case = create_case(client, sla_hours=48)
    cid = case["id"]

    clock.advance(hours=10)
    a = apply_pause(client, cid, requested_start=clock.now().isoformat())
    approve(client, a["id"])

    clock.advance(hours=5)  # T0+15h
    b = apply_pause(
        client, cid, event_type="封路", reason="道路封闭", requested_start=clock.now().isoformat()
    )
    approve(client, b["id"])

    clock.advance(hours=5)  # T0+20h
    end_pause(client, a["id"])

    clock.advance(hours=5)  # T0+25h
    end_pause(client, b["id"])

    tr = trace(client, cid)
    assert tr["paused_seconds"] == 15 * 3600
    assert tr["effective_elapsed_seconds"] == 10 * 3600  # 25 - 15
    assert len(tr["merged_pause_intervals"]) == 1  # 并集合并成单条
    assert tr["merged_pause_intervals"][0]["seconds"] == 15 * 3600
    assert len(tr["counted_pause_intervals"]) == 2


def test_duplicate_end_request_is_idempotent(client, clock):
    """重复结束请求：ended_at 不变、扣减不双算。"""
    case = create_case(client, sla_hours=48)
    cid = case["id"]

    clock.advance(hours=2)
    pr = apply_pause(client, cid, requested_start=clock.now().isoformat())
    approve(client, pr["id"])

    clock.advance(hours=3)
    _, first = end_pause(client, pr["id"])
    ended_at = first["ended_at"]

    clock.advance(hours=4)
    _, second = end_pause(client, pr["id"])  # 重复请求
    assert second["ended_at"] == ended_at
    assert second["status"] == "ENDED"

    tr = trace(client, cid)
    assert tr["paused_seconds"] == 3 * 3600  # 仍是第一次落账的 3h，不是 7h


def test_rejected_pause_never_counts(client, clock):
    case = create_case(client, sla_hours=48)
    cid = case["id"]

    clock.advance(hours=1)
    pr = apply_pause(client, cid, requested_start=clock.now().isoformat())
    reject(client, pr["id"], reason="证据与事件无关")

    clock.advance(hours=10)
    body = client.get(f"/cases/{cid}").json()
    assert body["effective_elapsed_seconds"] == 11 * 3600
    assert trace(client, cid)["paused_seconds"] == 0


def test_cancel_pending_never_counts_and_cancel_approved_freezes_at_cancel(client, clock):
    case = create_case(client, sla_hours=48)
    cid = case["id"]

    # PENDING 撤销：从未生效
    clock.advance(hours=1)
    pending = apply_pause(client, cid, requested_start=clock.now().isoformat())
    resp = client.post(f"/pause-requests/{pending['id']}/cancel", json={"reason": "雨停了"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "CANCELLED"
    assert trace(client, cid)["paused_seconds"] == 0

    # APPROVED 撤销：已走表的 3h 保留，之后不再扣减
    clock.advance(hours=2)
    approved = apply_pause(client, cid, requested_start=clock.now().isoformat())
    approve(client, approved["id"])
    clock.advance(hours=3)
    resp = client.post(f"/pause-requests/{approved['id']}/cancel", json={"reason": "提前恢复"})
    assert resp.status_code == 200
    cancelled_at = resp.json()["ended_at"]
    assert cancelled_at is not None

    clock.advance(hours=5)
    tr = trace(client, cid)
    assert tr["paused_seconds"] == 3 * 3600  # 仅撤销前的 3h


def test_approval_failure_leaves_no_half_pause(client, clock):
    """审批失败（状态非法）不改变任何状态，不留半截停表。"""
    case = create_case(client, sla_hours=48)
    cid = case["id"]

    clock.advance(hours=1)
    pr = apply_pause(client, cid, requested_start=clock.now().isoformat())
    approve(client, pr["id"])

    clock.advance(hours=2)  # 暂停已生效 2h

    # 对已批准申请重复批准 → 409
    resp = client.post(
        f"/pause-requests/{pr['id']}/approve", json={"decided_by": "approver-li"}
    )
    assert resp.status_code == 409

    # 对已批准申请拒绝 → 409
    resp = client.post(
        f"/pause-requests/{pr['id']}/reject",
        json={"decided_by": "approver-li", "reason": "x"},
    )
    assert resp.status_code == 409

    tr = trace(client, cid)
    assert len(tr["counted_pause_intervals"]) == 1  # 仍是最初那一段（2h）
    assert tr["paused_seconds"] == 2 * 3600

    # 对已批准申请撤销后再结束 → 409
    client.post(f"/pause-requests/{pr['id']}/cancel", json={"reason": "x"})
    resp = client.post(f"/pause-requests/{pr['id']}/end", json={})
    assert resp.status_code == 409

    # 终态申请不再参与未来走表
    clock.advance(hours=10)
    tr2 = trace(client, cid)
    assert tr2["paused_seconds"] == tr["paused_seconds"]


def test_no_new_pause_after_case_closed(client, clock):
    """整改结案后不得重新启动：禁止新申请、禁止结案后审批新暂停。"""
    case = create_case(client, sla_hours=48)
    cid = case["id"]

    clock.advance(hours=1)
    resp = client.post(f"/cases/{cid}/close")
    assert resp.status_code == 200

    resp = client.post(
        f"/cases/{cid}/pause-requests",
        json={
            "event_type": "暴雨",
            "reason": "r",
            "evidence": ["e://1"],
            "requested_start": clock.now().isoformat(),
        },
    )
    assert resp.status_code == 409

    # 重复结案幂等
    assert client.post(f"/cases/{cid}/close").status_code == 200


def test_validation_errors_do_not_create_requests(client, clock):
    case = create_case(client, sla_hours=48)
    cid = case["id"]

    # 缺证据 → 422，不产生申请
    resp = client.post(
        f"/cases/{cid}/pause-requests",
        json={"event_type": "暴雨", "reason": "r", "evidence": []},
    )
    assert resp.status_code == 422

    # 结束早于开始 → 422
    resp = client.post(
        f"/cases/{cid}/pause-requests",
        json={
            "event_type": "暴雨",
            "reason": "r",
            "evidence": ["e://1"],
            "requested_start": "2026-09-02T00:00:00Z",
            "requested_end": "2026-09-01T00:00:00Z",
        },
    )
    assert resp.status_code == 422
    assert client.get(f"/cases/{cid}/pause-requests").json() == []


def test_approve_invalid_window_leaves_request_pending(client, clock):
    """审批参数非法（结束早于开始）→ 422，申请保持 PENDING，不留半截停表。"""
    case = create_case(client, sla_hours=48)
    cid = case["id"]

    clock.advance(hours=1)
    pr = apply_pause(client, cid, requested_start=clock.now().isoformat())

    resp = client.post(
        f"/pause-requests/{pr['id']}/approve",
        json={
            "decided_by": "approver-li",
            "approved_start": clock.now().isoformat(),
            "approved_end": "2026-09-01T08:00:00Z",
        },
    )
    assert resp.status_code == 422

    body = client.get(f"/pause-requests/{pr['id']}").json()
    assert body["status"] == "PENDING"
    assert body["approved_start"] is None
    assert trace(client, cid)["paused_seconds"] == 0


def test_pause_query_filters(client, clock):
    """查询 API：按案件、状态过滤。"""
    case = create_case(client, sla_hours=48)
    cid = case["id"]

    clock.advance(hours=1)
    approved = apply_pause(client, cid, requested_start=clock.now().isoformat())
    approve(client, approved["id"])
    rejected = apply_pause(client, cid, requested_start=clock.now().isoformat())
    reject(client, rejected["id"])

    rows = client.get("/pause-requests", params={"case_id": cid, "status": "APPROVED"}).json()
    assert [r["id"] for r in rows] == [approved["id"]]

    rows = client.get("/pause-requests", params={"case_id": cid, "status": "REJECTED"}).json()
    assert [r["id"] for r in rows] == [rejected["id"]]

    rows = client.get(f"/cases/{cid}/pause-requests").json()
    assert len(rows) == 2


def test_sweep_during_open_pause_stalls_escalation_until_pause_ends(client, clock):
    """暂停批准后，升级触发时点随有效时长顺延（剩余时长正确续算的端到端验证）。"""
    case = create_case(client, sla_hours=1)
    cid = case["id"]

    clock.advance(minutes=30)
    pr = apply_pause(client, cid, requested_start=clock.now().isoformat())
    approve(client, pr["id"])

    # T0+60m：原始时长到点，但有效时长仅 30m → 不升级；此刻结束暂停
    clock.advance(minutes=30)
    summary = sweep(client)
    assert summary["escalations_created"] == 0
    end_pause(client, pr["id"])  # 暂停区间 [T0+30m, T0+60m)

    # T0+90m：有效时长 = 90m − 30m = 60m → L1 触发（墙上时间顺延 30m）
    clock.advance(minutes=30)
    summary = sweep(client)
    assert summary["escalations_created"] == 1
    escs = client.get(f"/cases/{cid}/escalations").json()
    assert [e["level"] for e in escs] == [1]
