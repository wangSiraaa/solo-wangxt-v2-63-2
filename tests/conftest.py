from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.clock import ManualClock
from app.main import create_app

T0 = datetime(2026, 9, 1, 8, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(T0)


@pytest.fixture
def app(clock):
    return create_app(db_url="sqlite://", clock=clock)


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


# --------------------------------------------------------------------------- #
# 测试辅助
# --------------------------------------------------------------------------- #
def create_case(client, sla_hours: float = 48.0, title: str = "现场整改单") -> dict:
    resp = client.post("/cases", json={"title": title, "sla_hours": sla_hours})
    assert resp.status_code == 201, resp.text
    return resp.json()


def apply_pause(client, case_id: int, **overrides) -> dict:
    payload = {
        "event_type": "暴雨",
        "reason": "道路积水，现场暂时无法进场整改",
        "evidence": ["evidence://photo/20260901-rain-1"],
    }
    payload.update(overrides)
    resp = client.post(f"/cases/{case_id}/pause-requests", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def approve(client, pause_id: int, **overrides) -> dict:
    payload = {"decided_by": "approver-li"}
    payload.update(overrides)
    resp = client.post(f"/pause-requests/{pause_id}/approve", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


def reject(client, pause_id: int, reason: str = "证据不足") -> dict:
    resp = client.post(
        f"/pause-requests/{pause_id}/reject",
        json={"decided_by": "approver-li", "reason": reason},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def end_pause(client, pause_id: int, **payload) -> tuple[int, dict]:
    resp = client.post(f"/pause-requests/{pause_id}/end", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.status_code, resp.json()


def sweep(client) -> dict:
    resp = client.post("/jobs/escalation-sweep")
    assert resp.status_code == 200, resp.text
    return resp.json()


def trace(client, case_id: int) -> dict:
    resp = client.get(f"/cases/{case_id}/sla-trace")
    assert resp.status_code == 200, resp.text
    return resp.json()
