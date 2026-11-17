"""M2 机场时区治理：变更申请/双人复核/限时应急接管。

每个测试使用独立临时 SQLite 库，避免污染工作区 data/。
"""
import pytest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.errors import DomainError
from app.modules.m2_airports import governance, governance_store as store
from app.modules.m2_airports.registry import airport_version

OPERATOR = {"user": "u-op", "roles": ["OPERATOR"]}
REVIEWER_A = {"user": "u-rev-a", "roles": ["REVIEWER"]}
REVIEWER_B = {"user": "u-rev-b", "roles": ["REVIEWER"]}
EMERGENCY = {"user": "u-em", "roles": ["EMERGENCY_OPERATOR"]}
NOBODY = {"user": "u-nobody", "roles": []}
FIXED_NOW = datetime(2026, 11, 17, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.fixture()
def gov_db(tmp_path, monkeypatch):
    db_file = tmp_path / "gov.sqlite3"
    monkeypatch.setattr("app.store.DATABASE_PATH", db_file)
    governance.ensure_ready()
    return db_file


def clock(hours: float = 0) -> datetime:
    return FIXED_NOW + timedelta(hours=hours)


def expiry(hours: float = 24) -> str:
    return (FIXED_NOW + timedelta(hours=hours)).isoformat(timespec="seconds")


def request_payload(**overrides) -> dict:
    payload = {
        "airport": "JFK",
        "proposed_timezone": "America/Chicago",
        "reason": "航站楼整体搬迁至中部时区城市",
        "actor": OPERATOR,
    }
    payload.update(overrides)
    return payload


def takeover_payload(**overrides) -> dict:
    payload = {
        "airport": "JFK",
        "cover_start": "2026-11-17",
        "cover_end": "2026-11-19",
        "emergency_timezone": "UTC",
        "expires_at": expiry(24),
        "reason": "上游时刻源错配，查询立即纠正",
        "actor": EMERGENCY,
    }
    payload.update(overrides)
    return payload


# ---------- 权限：越权拒绝 ----------

def test_submit_requires_operator_role(gov_db):
    with pytest.raises(DomainError) as exc:
        governance.submit_change_request(request_payload(actor=NOBODY))
    assert exc.value.status == 403 and exc.value.code == "FORBIDDEN_ROLE"


def test_review_requires_reviewer_role(gov_db):
    created = governance.submit_change_request(request_payload())
    with pytest.raises(DomainError) as exc:
        governance.review_change_request({
            "request_id": created["request_id"], "decision": "APPROVED",
            "decision_reason": "ok", "actor": OPERATOR,
        })
    assert exc.value.status == 403 and exc.value.code == "FORBIDDEN_ROLE"


def test_takeover_requires_emergency_role(gov_db):
    with pytest.raises(DomainError) as exc:
        governance.handoff_takeover(takeover_payload(actor=OPERATOR), now_value=FIXED_NOW)
    assert exc.value.status == 403 and exc.value.code == "FORBIDDEN_ROLE"


def test_unknown_role_rejected(gov_db):
    with pytest.raises(DomainError) as exc:
        governance.submit_change_request(
            request_payload(actor={"user": "x", "roles": ["GOD"]})
        )
    assert exc.value.code == "UNKNOWN_ROLE"


# ---------- 申请人与复核人分离 ----------

def test_reviewer_must_differ_from_applicant(gov_db):
    created = governance.submit_change_request(
        request_payload(actor={"user": "same-person", "roles": ["OPERATOR", "REVIEWER"]})
    )
    with pytest.raises(DomainError) as exc:
        governance.review_change_request({
            "request_id": created["request_id"], "decision": "APPROVED",
            "decision_reason": "self approve",
            "actor": {"user": "same-person", "roles": ["REVIEWER"]},
        })
    assert exc.value.status == 403 and exc.value.code == "REVIEWER_MUST_DIFFER"


# ---------- 正式申请可继续审核，批准写入正式登记 ----------

def test_approved_request_updates_formal_registry(gov_db):
    created = governance.submit_change_request(request_payload())
    result = governance.review_change_request({
        "request_id": created["request_id"], "decision": "APPROVED",
        "decision_reason": "材料齐全", "effective_start": "2026-12-01",
        "actor": REVIEWER_A,
    })
    assert result["status"] == "APPROVED" and result["adjudication"] is None
    formal = governance.formal_registry("JFK", "2026-12-01")
    assert formal["timezone"] == "America/Chicago" and formal["source"] == "APPROVED_REQUEST"
    # 旧版本被半开关闭，历史日期仍可回读
    old = governance.formal_registry("JFK", "2026-11-30")
    assert old["timezone"] == "America/New_York" and old["end"] == "2026-12-01"
    # 纯正式查询函数不受影响
    assert airport_version("JFK", "2026-11-16")["timezone"] == "America/New_York"


def test_rejected_request_keeps_registry_unchanged(gov_db):
    created = governance.submit_change_request(request_payload())
    result = governance.review_change_request({
        "request_id": created["request_id"], "decision": "REJECTED",
        "decision_reason": "证据不足", "actor": REVIEWER_A,
    })
    assert result["status"] == "REJECTED"
    assert governance.formal_registry("JFK", "2026-12-01")["timezone"] == "America/New_York"
    with pytest.raises(DomainError) as exc:
        governance.review_change_request({
            "request_id": created["request_id"], "decision": "APPROVED",
            "decision_reason": "again", "actor": REVIEWER_B,
        })
    assert exc.value.code == "REQUEST_NOT_PENDING"


def test_bad_timezone_and_missing_reason_rejected(gov_db):
    with pytest.raises(DomainError) as exc:
        governance.submit_change_request(request_payload(proposed_timezone="Mars/Olympus"))
    assert exc.value.code == "BAD_TIMEZONE"
    with pytest.raises(DomainError) as exc:
        governance.submit_change_request(request_payload(reason="  "))
    assert exc.value.code == "REASON_REQUIRED"


# ---------- 限时应急接管：只覆盖指定机场和日期 ----------

def test_takeover_overrides_only_scoped_airport_and_dates(gov_db):
    result = governance.handoff_takeover(takeover_payload(), now_value=FIXED_NOW)
    assert result["status"] == "ACTIVE" and result["handoff_step"] == "COMPLETED"
    # 覆盖区间内查询被应急接管纠正（注入固定时钟保证确定性）
    effective = governance.effective_zone("JFK", "2026-11-17", FIXED_NOW)
    assert effective["source"] == "EMERGENCY_TAKEOVER" and effective["timezone"] == "UTC"
    # 半开区间右端不覆盖
    outside = governance.effective_zone("JFK", "2026-11-19", FIXED_NOW)
    assert outside["source"] != "EMERGENCY_TAKEOVER" and outside["timezone"] == "America/New_York"
    # 其他机场不受影响
    other = governance.effective_zone("LHR", "2026-11-17", FIXED_NOW)
    assert other["timezone"] == "Europe/London" and other["source"] != "EMERGENCY_TAKEOVER"
    # 正式登记从未被应急改写
    assert governance.formal_registry("JFK", "2026-11-17")["timezone"] == "America/New_York"


def test_takeover_rejects_bad_interval_and_past_expiry(gov_db):
    with pytest.raises(DomainError) as exc:
        governance.handoff_takeover(
            takeover_payload(cover_start="2026-11-20", cover_end="2026-11-19"), now_value=FIXED_NOW
        )
    assert exc.value.code == "BAD_INTERVAL"
    with pytest.raises(DomainError) as exc:
        governance.handoff_takeover(
            takeover_payload(expires_at=(FIXED_NOW - timedelta(hours=1)).isoformat()), now_value=FIXED_NOW
        )
    assert exc.value.code == "TAKEOVER_ALREADY_EXPIRED"


# ---------- 交接写入一半：保留进度，失败后续跑，不新增接管记录 ----------

def test_handoff_resumes_after_partial_failure(gov_db):
    payload = takeover_payload()
    with pytest.raises(RuntimeError, match="INJECTED_FAILURE_AFTER_RECORDED"):
        governance.handoff_takeover(payload, failure_point="AFTER_RECORDED", now_value=FIXED_NOW)
    # 主记录与第一步进度已保留
    partial_id = "EM-JFK-" + governance._takeover_id(
        "JFK", "2026-11-17", "2026-11-19", "UTC", "u-em"
    ).removeprefix("EM-JFK-")
    record = store.get_takeover(partial_id)
    assert record is not None and record["status"] == "ACTIVE"
    assert store.get_handoff(partial_id)["step"] == "RECORDED"
    assert store.list_takeovers(airport="JFK").__len__() == 1

    # 续跑同一请求：从第二步继续，不新增记录
    resumed = governance.handoff_takeover(payload, now_value=FIXED_NOW)
    assert resumed["resumed"] is True and resumed["handoff_step"] == "COMPLETED"
    assert len(store.list_takeovers(airport="JFK")) == 1
    assert store.get_handoff(partial_id)["step"] == "COMPLETED"

    # 再重复执行：幂等回放，仍然只有一条接管记录
    replay = governance.handoff_takeover(payload, now_value=FIXED_NOW)
    assert replay["idempotent_replay"] is True and replay["takeover_id"] == partial_id
    assert len(store.list_takeovers(airport="JFK")) == 1


def test_replay_with_different_payload_conflicts(gov_db):
    first = governance.handoff_takeover(takeover_payload(), now_value=FIXED_NOW)
    with pytest.raises(DomainError) as exc:
        governance.handoff_takeover(
            takeover_payload(emergency_timezone="Europe/London", takeover_id=first["takeover_id"]),
            now_value=FIXED_NOW,
        )
    assert exc.value.status == 409 and exc.value.code == "TAKEOVER_ID_CONFLICT"
    # 原接管参数保持不变，且仍只有一条记录
    assert store.get_takeover(first["takeover_id"])["emergency_timezone"] == "UTC"
    assert len(store.list_takeovers(airport="JFK")) == 1


# ---------- 批准遇到接管并发：必须明确裁决 ----------

def test_approval_under_takeover_requires_explicit_adjudication(gov_db):
    governance.handoff_takeover(takeover_payload(), now_value=FIXED_NOW)
    created = governance.submit_change_request(request_payload())
    with pytest.raises(DomainError) as exc:
        governance.review_change_request({
            "request_id": created["request_id"], "decision": "APPROVED",
            "decision_reason": "材料齐全", "effective_start": "2026-11-18",
            "actor": REVIEWER_A,
        })
    assert exc.value.status == 409 and exc.value.code == "TAKEOVER_CONFLICT"
    # 申请仍处于待审，正式流程没有被应急打断
    assert store.get_request(created["request_id"])["status"] == "PENDING"


def test_block_adjudication_keeps_request_pending(gov_db):
    governance.handoff_takeover(takeover_payload(), now_value=FIXED_NOW)
    created = governance.submit_change_request(request_payload())
    with pytest.raises(DomainError) as exc:
        governance.review_change_request({
            "request_id": created["request_id"], "decision": "APPROVED",
            "decision_reason": "先搁置", "effective_start": "2026-11-18",
            "adjudication": governance.ADJUDICATION_BLOCK, "actor": REVIEWER_A,
        })
    assert exc.value.code == "APPROVAL_BLOCKED_BY_TAKEOVER"
    assert store.get_request(created["request_id"])["status"] == "PENDING"


def test_override_approval_becomes_latest_formal(gov_db):
    governance.handoff_takeover(takeover_payload(), now_value=FIXED_NOW)
    created = governance.submit_change_request(request_payload())
    result = governance.review_change_request({
        "request_id": created["request_id"], "decision": "APPROVED",
        "decision_reason": "裁决写入最新正式登记", "effective_start": "2026-11-18",
        "adjudication": governance.ADJUDICATION_OVERRIDE, "actor": REVIEWER_A,
    })
    assert result["adjudication"] == governance.ADJUDICATION_OVERRIDE
    # 接管覆盖日期内应急仍优先
    assert governance.effective_zone("JFK", "2026-11-18", FIXED_NOW)["timezone"] == "UTC"
    # 正式登记已更新为新值
    assert governance.formal_registry("JFK", "2026-11-18")["timezone"] == "America/Chicago"


# ---------- 接管到期/撤销后恢复当前获批登记，不回退旧值 ----------

def test_expired_takeover_falls_back_to_latest_formal_not_old_value(gov_db):
    # 先批准一个新正式登记（America/Chicago），再制造接管
    created = governance.submit_change_request(request_payload())
    governance.review_change_request({
        "request_id": created["request_id"], "decision": "APPROVED",
        "decision_reason": "ok", "effective_start": "2026-11-10", "actor": REVIEWER_A,
    })
    governance.handoff_takeover(takeover_payload(), now_value=FIXED_NOW)
    assert governance.effective_zone("JFK", "2026-11-17", FIXED_NOW)["timezone"] == "UTC"
    # 到期后恢复的是当前获批登记（新值），不是接管前旧值 America/New_York
    after_expiry = clock(25)
    swept = governance.sweep_expired(after_expiry)
    assert swept["count"] == 1
    restored = governance.effective_zone("JFK", "2026-11-17", after_expiry)
    assert restored["source"] != "EMERGENCY_TAKEOVER"
    assert restored["timezone"] == "America/Chicago"


def test_revoked_takeover_restores_current_formal(gov_db):
    governance.handoff_takeover(takeover_payload(), now_value=FIXED_NOW)
    revoked = governance.revoke_takeover({"takeover_id": store.list_takeovers(airport="JFK")[0]["takeover_id"],
                                          "reason": "错配已修复", "actor": EMERGENCY})
    assert revoked["status"] == "REVOKED"
    restored = governance.effective_zone("JFK", "2026-11-17", FIXED_NOW)
    assert restored["source"] != "EMERGENCY_TAKEOVER"
    assert restored["timezone"] == "America/New_York"
    with pytest.raises(DomainError) as exc:
        governance.revoke_takeover({"takeover_id": revoked["takeover_id"], "actor": EMERGENCY})
    assert exc.value.code == "TAKEOVER_NOT_ACTIVE"


def test_takeover_lazy_expiry_on_read(gov_db):
    governance.handoff_takeover(takeover_payload(), now_value=FIXED_NOW)
    view = governance.readback("JFK", "2026-11-17", clock(25))
    assert view["emergency_source"] is None
    assert view["effective"]["timezone"] == "America/New_York"
    assert store.list_takeovers(airport="JFK")[0]["status"] == "EXPIRED"


# ---------- 回读：正式与应急来源、期限、交接状态 ----------

def test_readback_reports_sources_terms_and_handoff(gov_db):
    result = governance.handoff_takeover(takeover_payload(), now_value=FIXED_NOW)
    created = governance.submit_change_request(request_payload())
    view = governance.readback("JFK", "2026-11-17", FIXED_NOW)
    assert view["formal_source"]["timezone"] == "America/New_York"
    assert view["emergency_source"]["takeover_id"] == result["takeover_id"]
    assert datetime.fromisoformat(view["emergency_source"]["expires_at"]) == datetime.fromisoformat(expiry(24))
    takeover_view = view["takeovers"][0]
    assert takeover_view["handoff_step"] == "COMPLETED" and takeover_view["status"] == "ACTIVE"
    assert takeover_view["cover_start"] == "2026-11-17" and takeover_view["cover_end"] == "2026-11-19"
    assert view["pending_requests"][0]["request_id"] == created["request_id"]


def test_seed_baseline_is_idempotent(gov_db):
    before = len(store.list_formal_versions())
    assert store.seed_baseline() == 0
    assert len(store.list_formal_versions()) == before


def test_unknown_airport_still_rejected(gov_db):
    with pytest.raises(ValueError, match="UNKNOWN_AIRPORT"):
        governance.formal_registry("XXX", "2026-11-17")


# ---------- 接管即时作用于在用查询链路（M4 航链按航段日期取时区） ----------

def test_takeover_corrects_active_itinerary_query_and_restores(gov_db):
    from app.modules.m4_itinerary.builder import build_leg

    row = {
        "flight_no": "MU1", "origin": "JFK", "destination": "LHR",
        "departure_local": "2026-11-17T12:00", "arrival_local": "2026-11-18T00:30",
    }
    before = build_leg(row)
    governance.handoff_takeover(takeover_payload(), now_value=FIXED_NOW)
    during = build_leg(row)
    # 航段时刻输入未改，仅时区来源被应急接管纠正
    assert before["departure_utc"] != during["departure_utc"]
    assert during["departure_utc"] == "2026-11-17T12:00:00+00:00"

    takeover_id = store.list_takeovers(airport="JFK")[0]["takeover_id"]
    governance.revoke_takeover({"takeover_id": takeover_id, "actor": EMERGENCY})
    after = build_leg(row)
    assert after["departure_utc"] == before["departure_utc"]
