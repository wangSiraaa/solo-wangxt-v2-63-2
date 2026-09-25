"""迟到批准与补偿建议验收。

覆盖验收点：锁定处罚上的迟到批准不被静默撤销——
原处罚（金额/状态）与已产生升级保持不变，只生成待复核补偿建议；
确认后以追加更正形式冲抵，原处罚链完整保留。
"""
from __future__ import annotations

from tests.conftest import apply_pause, create_case, sweep


def test_late_approval_on_locked_penalty_creates_suggestion_not_reversal(client, clock):
    case = create_case(client, sla_hours=1)
    cid = case["id"]

    # 有效逾期 24h → L1 + L2，L2 带来 ¥100 处罚
    clock.advance(hours=25)
    sweep(client)
    penalties = client.get(f"/cases/{cid}/penalties").json()
    assert len(penalties) == 1
    penalty_id = penalties[0]["id"]

    # 处罚锁定
    resp = client.post(f"/penalties/{penalty_id}/lock")
    assert resp.status_code == 200
    assert resp.json()["status"] == "LOCKED"

    # 迟到批准：[T0+2h, T0+20h)，在锁定之后才审批
    pr = apply_pause(client, cid, event_type="封路", reason="事故封路，审批材料补齐延迟")
    resp = client.post(
        f"/pause-requests/{pr['id']}/approve",
        json={
            "decided_by": "approver-li",
            "approved_start": "2026-09-01T10:00:00Z",  # T0+2h
            "approved_end": "2026-09-02T04:00:00Z",    # T0+20h
        },
    )
    assert resp.status_code == 200

    # 生成一条待复核建议（L2 反事实逾期仅 6h < 24h；L1 仍会触发，不出建议）
    suggestions = client.get(
        "/compensation-suggestions", params={"case_id": cid}
    ).json()
    assert len(suggestions) == 1
    s = suggestions[0]
    assert s["status"] == "PENDING_REVIEW"
    assert s["kind"] == "PENALTY_CREDIT"
    assert s["amount_cents"] == 100_00
    assert s["penalty_id"] == penalty_id

    # 已锁定处罚未被静默撤销：金额、状态、更正链都不变
    penalty = client.get(f"/penalties/{penalty_id}").json()
    assert penalty["amount_cents"] == 100_00
    assert penalty["effective_amount_cents"] == 100_00
    assert penalty["status"] == "LOCKED"
    assert penalty["corrections"] == []

    # 已产生升级一条不少
    escs = client.get(f"/cases/{cid}/escalations").json()
    assert [e["level"] for e in escs] == [1, 2]

    # 确认建议：只追加更正，原金额与状态保持
    resp = client.post(f"/compensation-suggestions/{s['id']}/confirm", json={"decided_by": "reviewer-wang"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "CONFIRMED"

    penalty = client.get(f"/penalties/{penalty_id}").json()
    assert penalty["amount_cents"] == 100_00       # 原值保留
    assert penalty["status"] == "LOCKED"
    assert len(penalty["corrections"]) == 1        # 更正链追加
    correction = penalty["corrections"][0]
    assert correction["amount_delta_cents"] == -100_00
    assert correction["suggestion_id"] == s["id"]
    assert penalty["effective_amount_cents"] == 0  # 冲抵后净额为 0

    # 升级记录依旧保留
    assert [e["level"] for e in client.get(f"/cases/{cid}/escalations").json()] == [1, 2]

    # 再次确认 → 409（不能重复冲抵）
    resp = client.post(f"/compensation-suggestions/{s['id']}/confirm", json={"decided_by": "x"})
    assert resp.status_code == 409

    # 后续扫描：L2 已存在，有效逾期即便再次达标也不会重放/双算
    clock.advance(hours=25)
    summary = sweep(client)
    assert summary["escalations_created"] == 0
    assert len(client.get(f"/cases/{cid}/penalties").json()) == 1


def test_dismissed_suggestion_changes_nothing(client, clock):
    case = create_case(client, sla_hours=1)
    cid = case["id"]

    clock.advance(hours=25)
    sweep(client)
    penalty_id = client.get(f"/cases/{cid}/penalties").json()[0]["id"]
    client.post(f"/penalties/{penalty_id}/lock")

    pr = apply_pause(client, cid, event_type="封路")
    client.post(
        f"/pause-requests/{pr['id']}/approve",
        json={
            "decided_by": "approver-li",
            "approved_start": "2026-09-01T10:00:00Z",
            "approved_end": "2026-09-02T04:00:00Z",
        },
    )
    s = client.get("/compensation-suggestions", params={"case_id": cid}).json()[0]

    resp = client.post(
        f"/compensation-suggestions/{s['id']}/dismiss",
        json={"decided_by": "reviewer-wang", "reason": "证据不支持封路时段"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "DISMISSED"

    penalty = client.get(f"/penalties/{penalty_id}").json()
    assert penalty["amount_cents"] == 100_00
    assert penalty["status"] == "LOCKED"
    assert penalty["corrections"] == []
    assert penalty["effective_amount_cents"] == 100_00


def test_two_late_approvals_same_escalation_do_not_stack_credits(client, clock):
    """两条迟到暂停覆盖同一升级：只允许一条活/已确认建议，防止超额冲抵。"""
    case = create_case(client, sla_hours=1)
    cid = case["id"]

    clock.advance(hours=25)
    sweep(client)
    penalty_id = client.get(f"/cases/{cid}/penalties").json()[0]["id"]
    client.post(f"/penalties/{penalty_id}/lock")

    first = apply_pause(client, cid, event_type="封路")
    client.post(
        f"/pause-requests/{first['id']}/approve",
        json={
            "decided_by": "li",
            "approved_start": "2026-09-01T10:00:00Z",
            "approved_end": "2026-09-02T04:00:00Z",
        },
    )
    second = apply_pause(client, cid, event_type="暴雨")
    client.post(
        f"/pause-requests/{second['id']}/approve",
        json={
            "decided_by": "li",
            "approved_start": "2026-09-01T11:00:00Z",
            "approved_end": "2026-09-02T03:00:00Z",
        },
    )

    suggestions = client.get(
        "/compensation-suggestions", params={"case_id": cid}
    ).json()
    assert len(suggestions) == 1
    assert suggestions[0]["pause_request_id"] == first["id"]
    assert suggestions[0]["amount_cents"] == 100_00


def test_late_approval_against_warning_escalation_creates_review_only(client, clock):
    """迟到暂停若只使 L1 警告“不该触发”：保留升级，只出复核型建议，确认无更正。"""
    case = create_case(client, sla_hours=1)
    cid = case["id"]

    clock.advance(hours=2)  # 逾期 1h → 仅 L1
    sweep(client)

    pr = apply_pause(client, cid, event_type="暴雨")
    client.post(
        f"/pause-requests/{pr['id']}/approve",
        json={
            "decided_by": "approver-li",
            "approved_start": "2026-09-01T08:30:00Z",  # T0+0.5h
            "approved_end": "2026-09-01T11:00:00Z",    # T0+3h
        },
    )

    suggestions = client.get("/compensation-suggestions", params={"case_id": cid}).json()
    assert len(suggestions) == 1
    s = suggestions[0]
    assert s["kind"] == "ESCALATION_REVIEW"
    assert s["penalty_id"] is None
    assert s["amount_cents"] == 0

    client.post(f"/compensation-suggestions/{s['id']}/confirm", json={"decided_by": "r"})

    # L1 记录保留
    assert [e["level"] for e in client.get(f"/cases/{cid}/escalations").json()] == [1]
    assert client.get(f"/cases/{cid}/penalties").json() == []
