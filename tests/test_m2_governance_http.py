"""M2 治理接口的 HTTP 端到端测试：角色越权、双人复核、接管并发裁决与回读。"""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.modules.m2_airports import governance, governance_store as store

OPERATOR = {"user": "u-op", "roles": ["OPERATOR"]}
REVIEWER = {"user": "u-rev", "roles": ["REVIEWER"]}
EMERGENCY = {"user": "u-em", "roles": ["EMERGENCY_OPERATOR"]}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("app.store.DATABASE_PATH", tmp_path / "http.sqlite3")
    governance.ensure_ready()
    return TestClient(app)


def takeovers_payload(actor=EMERGENCY, **overrides):
    payload = {
        "airport": "LHR",
        "cover_start": "2026-11-17",
        "cover_end": "2026-11-20",
        "emergency_timezone": "UTC",
        "expires_at": "2026-11-18T10:00:00+08:00",
        "reason": "时刻源错配应急纠正",
        "actor": actor,
    }
    payload.update(overrides)
    return payload


def test_forbidden_returns_403_with_code(client):
    response = client.post("/api/airports/takeovers", json=takeovers_payload(actor=OPERATOR))
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "FORBIDDEN_ROLE"


def test_full_review_workflow_over_http(client):
    # 申请
    response = client.post("/api/airports/change-requests", json={
        "airport": "lhr",
        "proposed_timezone": "Europe/Paris",
        "reason": "运行基地调整",
        "actor": OPERATOR,
    })
    assert response.status_code == 200
    request_id = response.json()["request_id"]

    # 申请人不能自审
    self_review = client.post("/api/airports/change-requests/review", json={
        "request_id": request_id, "decision": "APPROVED", "decision_reason": "self",
        "effective_start": "2026-12-01",
        "actor": {"user": "u-op", "roles": ["REVIEWER"]},
    })
    assert self_review.status_code == 403
    assert self_review.json()["detail"]["code"] == "REVIEWER_MUST_DIFFER"

    # 他人复核批准
    approved = client.post("/api/airports/change-requests/review", json={
        "request_id": request_id, "decision": "APPROVED", "decision_reason": "材料齐全",
        "effective_start": "2026-12-01", "actor": REVIEWER,
    })
    assert approved.status_code == 200 and approved.json()["status"] == "APPROVED"

    listed = client.get("/api/airports/change-requests", params={"airport": "LHR"})
    assert listed.status_code == 200 and listed.json()["items"][0]["request_id"] == request_id


def test_takeover_conflict_requires_adjudication_then_override(client):
    made = client.post("/api/airports/takeovers", json=takeovers_payload())
    assert made.status_code == 200 and made.json()["handoff_step"] == "COMPLETED"

    created = client.post("/api/airports/change-requests", json={
        "airport": "LHR", "proposed_timezone": "Europe/Paris",
        "reason": "基地调整", "actor": OPERATOR,
    }).json()

    # 无裁决 -> 409 明确冲突
    blocked = client.post("/api/airports/change-requests/review", json={
        "request_id": created["request_id"], "decision": "APPROVED",
        "decision_reason": "ok", "effective_start": "2026-11-18", "actor": REVIEWER,
    })
    assert blocked.status_code == 409 and blocked.json()["detail"]["code"] == "TAKEOVER_CONFLICT"

    # 显式裁决 OVERRIDE 后批准
    overridden = client.post("/api/airports/change-requests/review", json={
        "request_id": created["request_id"], "decision": "APPROVED",
        "decision_reason": "ok", "effective_start": "2026-11-18",
        "adjudication": "APPROVE_AS_LATEST_FORMAL", "actor": REVIEWER,
    })
    assert overridden.status_code == 200
    assert overridden.json()["adjudication"] == "APPROVE_AS_LATEST_FORMAL"

    readback = client.get("/api/airports/readback", params={"airport": "LHR", "at": "2026-11-18"})
    assert readback.status_code == 200
    body = readback.json()
    # 接管期内应急优先；正式登记已是新版本
    assert body["effective"]["source"] == "EMERGENCY_TAKEOVER"
    assert body["formal_source"]["timezone"] == "Europe/Paris"
    assert body["takeovers"][0]["handoff_step"] == "COMPLETED"


def test_takeover_idempotent_and_revoke_restores_formal(client):
    first = client.post("/api/airports/takeovers", json=takeovers_payload()).json()
    replay = client.post("/api/airports/takeovers", json=takeovers_payload()).json()
    assert replay["takeover_id"] == first["takeover_id"] and replay["idempotent_replay"] is True
    assert len(store.list_takeovers(airport="LHR")) == 1

    effective = client.post("/api/airports/effective", json={"airport": "LHR", "at": "2026-11-17"})
    assert effective.json()["timezone"] == "UTC"

    revoked = client.post("/api/airports/takeovers/revoke", json={
        "takeover_id": first["takeover_id"], "reason": "修复完成", "actor": EMERGENCY,
    })
    assert revoked.status_code == 200 and revoked.json()["status"] == "REVOKED"

    restored = client.post("/api/airports/effective", json={"airport": "LHR", "at": "2026-11-17"})
    assert restored.json()["source"] != "EMERGENCY_TAKEOVER"
    assert restored.json()["timezone"] == "Europe/London"


def test_handoff_failure_then_resume_over_http(tmp_path, monkeypatch):
    monkeypatch.setattr("app.store.DATABASE_PATH", tmp_path / "http2.sqlite3")
    governance.ensure_ready()
    # 关闭异常重抛，模拟真实进程在交接第一步后崩溃返回 500
    client = TestClient(app, raise_server_exceptions=False)
    payload = takeovers_payload(failure_point="AFTER_RECORDED")
    failed = client.post("/api/airports/takeovers", json=payload)
    assert failed.status_code == 500
    takeover_id = store.list_takeovers(airport="LHR")[0]["takeover_id"]
    assert store.get_handoff(takeover_id)["step"] == "RECORDED"

    # 不带故障点重跑，交接继续完成
    resume = client.post("/api/airports/takeovers", json=takeovers_payload())
    assert resume.status_code == 200
    assert resume.json()["resumed"] is True and resume.json()["handoff_step"] == "COMPLETED"
    assert len(store.list_takeovers(airport="LHR")) == 1


def test_bad_inputs_return_domain_codes(client):
    bad_zone = client.post("/api/airports/change-requests", json={
        "airport": "LHR", "proposed_timezone": "Not/AZone",
        "reason": "x", "actor": OPERATOR,
    })
    assert bad_zone.status_code == 422 and bad_zone.json()["detail"]["code"] == "BAD_TIMEZONE"

    unknown_request = client.post("/api/airports/change-requests/review", json={
        "request_id": "CR-NOPE", "decision": "REJECTED", "decision_reason": "x", "actor": REVIEWER,
    })
    assert unknown_request.status_code == 404
    assert unknown_request.json()["detail"]["code"] == "REQUEST_NOT_FOUND"
