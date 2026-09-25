"""OpenAPI 规范验收：在线规范与仓库内快照一致，覆盖全部约定端点。"""
from __future__ import annotations

import json
from pathlib import Path

REQUIRED_PATHS = {
    "/cases",
    "/cases/{case_id}",
    "/cases/{case_id}/close",
    "/cases/{case_id}/pause-requests",
    "/pause-requests",
    "/pause-requests/{pause_id}",
    "/pause-requests/{pause_id}/approve",
    "/pause-requests/{pause_id}/reject",
    "/pause-requests/{pause_id}/end",
    "/pause-requests/{pause_id}/cancel",
    "/cases/{case_id}/escalations",
    "/cases/{case_id}/penalties",
    "/penalties/{penalty_id}/lock",
    "/cases/{case_id}/sla-trace",
    "/compensation-suggestions",
    "/compensation-suggestions/{suggestion_id}/confirm",
    "/compensation-suggestions/{suggestion_id}/dismiss",
    "/jobs/escalation-sweep",
}


def test_live_openapi_documents_all_contracts(client):
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    spec = resp.json()
    assert spec["info"]["title"] == "SLA Pause & Escalation Service"
    assert REQUIRED_PATHS <= set(spec["paths"])

    # 关键操作方法齐全
    assert "post" in spec["paths"]["/pause-requests/{pause_id}/approve"]
    assert "post" in spec["paths"]["/pause-requests/{pause_id}/end"]
    assert "get" in spec["paths"]["/cases/{case_id}/sla-trace"]
    assert "post" in spec["paths"]["/jobs/escalation-sweep"]


def test_openapi_snapshot_matches_live_spec(client):
    snapshot_path = Path(__file__).resolve().parent.parent / "openapi.json"
    assert snapshot_path.exists(), "run: python scripts/export_openapi.py"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    live = client.get("/openapi.json").json()
    assert set(snapshot["paths"]) == set(live["paths"])
    assert snapshot["info"]["version"] == live["info"]["version"]
