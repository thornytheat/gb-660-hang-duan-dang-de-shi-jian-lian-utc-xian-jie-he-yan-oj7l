"""机场时区登记治理领域服务。

职责边界：
- 正式登记只能由“变更申请 + 他人复核批准”写入；应急接管不改动任何正式登记。
- 接管仅覆盖指定机场与日期半开区间，且有硬性到期时间。
- 交接分两步落库，失败后续跑；同一接管请求重复执行不新增记录。
"""
import hashlib
from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.errors import DomainError
from . import governance_store as store
from .contracts import normalize_airport, validate_interval

ROLE_OPERATOR = "OPERATOR"
ROLE_REVIEWER = "REVIEWER"
ROLE_EMERGENCY = "EMERGENCY_OPERATOR"
KNOWN_ROLES = {ROLE_OPERATOR, ROLE_REVIEWER, ROLE_EMERGENCY}

ADJUDICATION_BLOCK = "BLOCK_UNTIL_TAKEOVER_CLOSES"
ADJUDICATION_OVERRIDE = "APPROVE_AS_LATEST_FORMAL"


def _err(code: str, message: str, status: int = 422) -> DomainError:
    return DomainError(code, message, status)


def _now() -> datetime:
    return datetime.now().astimezone()


def _actor(actor: dict | None) -> tuple[str, set[str]]:
    if not actor or not actor.get("user"):
        raise _err("ACTOR_REQUIRED", "缺少操作人信息", 403)
    user = str(actor["user"]).strip()
    if not user:
        raise _err("ACTOR_REQUIRED", "操作人不能为空", 403)
    roles = set(actor.get("roles") or [])
    unknown = roles - KNOWN_ROLES
    if unknown:
        raise _err("UNKNOWN_ROLE", f"未知角色: {sorted(unknown)}", 403)
    return user, roles


def _require_role(actor: dict | None, role: str) -> tuple[str, set[str]]:
    user, roles = _actor(actor)
    if role not in roles:
        raise _err("FORBIDDEN_ROLE", f"该操作需要角色 {role}", 403)
    return user, roles


def _valid_timezone(zone_name: str) -> str:
    if not isinstance(zone_name, str) or not zone_name.strip():
        raise _err("BAD_TIMEZONE", "时区不能为空")
    zone_name = zone_name.strip()
    try:
        ZoneInfo(zone_name)
    except ZoneInfoNotFoundError:
        raise _err("BAD_TIMEZONE", f"非 IANA 时区: {zone_name}")
    return zone_name


def _valid_date(value: str, field: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError):
        raise _err("BAD_DATE", f"日期字段 {field} 必须是 ISO YYYY-MM-DD")


def _valid_iso_dt(value: str, field: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise _err("BAD_TIMESTAMP", f"时间字段 {field} 必须是 ISO 8601 带偏移时间")
    if parsed.tzinfo is None:
        raise _err("BAD_TIMESTAMP", f"时间字段 {field} 必须带时区偏移")
    return parsed.astimezone().isoformat(timespec="seconds")


def ensure_ready() -> None:
    store.ensure_ready()


# ---------- 有效登记读取：应急优先，正式兜底 ----------

def effective_zone(code: str, at: str, now_value: datetime | None = None) -> dict:
    """查询当前实际生效的时区：指定机场/日期存在有效接管时返回应急来源。

    接管到期或撤销后自动落回正式登记；正式登记已更新则取新版本，
    不会回退到接管发生前的旧值。
    """
    ensure_ready()
    airport = normalize_airport(code)
    target = _valid_date(at, "at")
    current = now_value or _now()
    takeover = store.active_takeover_for(airport, target, current)
    if takeover:
        return {
            "airport": airport,
            "at": target,
            "timezone": takeover["emergency_timezone"],
            "source": "EMERGENCY_TAKEOVER",
            "takeover_id": takeover["takeover_id"],
            "expires_at": takeover["expires_at"],
        }
    formal = formal_registry(airport, target)
    return {
        "airport": airport,
        "at": target,
        "timezone": formal["timezone"],
        "source": formal["source"],
        "version_start": formal["start"],
        "version_end": formal["end"],
        "request_id": formal.get("request_id"),
    }


def formal_registry(code: str, at: str) -> dict:
    """只读正式登记：DB 已登记机场读 DB，未登记机场回退内存基线。"""
    airport = normalize_airport(code)
    target = _valid_date(at, "at")
    stored = store.formal_version_from_store(airport, target)
    if stored is not None:
        return stored
    seed = store.seed_lookup_version(airport, target)
    return {
        "airport": airport,
        "start": seed["start"],
        "end": seed["end"],
        "timezone": seed["timezone"],
        "source": "BASELINE_MEMORY",
        "request_id": None,
    }


# ---------- 变更申请 ----------

def _request_id(airport: str, timezone: str, applicant: str, reason: str, created: str) -> str:
    digest = hashlib.sha256(
        f"{airport}|{timezone}|{applicant}|{reason}|{created}".encode("utf-8")
    ).hexdigest()[:12]
    return f"CR-{airport}-{digest}"


def submit_change_request(payload: dict) -> dict:
    ensure_ready()
    user, _ = _require_role(payload.get("actor"), ROLE_OPERATOR)
    airport = normalize_airport(payload["airport"])
    timezone = _valid_timezone(payload["proposed_timezone"])
    reason = str(payload.get("reason") or "").strip()
    if not reason:
        raise _err("REASON_REQUIRED", "变更原因不能为空")
    created = store.now_iso()
    request_id = _request_id(airport, timezone, user, reason, created)
    if store.get_request(request_id):
        raise _err("DUPLICATE_REQUEST", "相同内容的变更申请已存在", 409)
    store.insert_request(request_id, airport, timezone, reason, user)
    store.append_event(
        store.EVENT_REQUEST_SUBMITTED, request_id, user,
        {"airport": airport, "proposed_timezone": timezone},
    )
    return {"request_id": request_id, "status": store.STATUS_PENDING, "airport": airport,
            "proposed_timezone": timezone, "applicant": user}


# ---------- 双人复核 ----------

def review_change_request(payload: dict) -> dict:
    ensure_ready()
    reviewer, _ = _require_role(payload.get("actor"), ROLE_REVIEWER)
    request_id = str(payload.get("request_id") or "").strip()
    if not request_id:
        raise _err("REQUEST_ID_REQUIRED", "缺少 request_id")
    request = store.get_request(request_id)
    if request is None:
        raise _err("REQUEST_NOT_FOUND", f"变更申请不存在: {request_id}", 404)
    if request["status"] != store.STATUS_PENDING:
        raise _err("REQUEST_NOT_PENDING", f"申请已结案: {request['status']}", 409)
    # 申请人与复核人分离
    if request["applicant"] == reviewer:
        raise _err("REVIEWER_MUST_DIFFER", "复核人不能与申请人为同一人", 403)

    decision = str(payload.get("decision") or "").strip().upper()
    if decision not in (store.STATUS_APPROVED, store.STATUS_REJECTED):
        raise _err("BAD_DECISION", "decision 必须是 APPROVED 或 REJECTED")
    decision_reason = str(payload.get("decision_reason") or "").strip()
    if not decision_reason:
        raise _err("DECISION_REASON_REQUIRED", "复核意见不能为空")

    adjudication = None
    if decision == store.STATUS_APPROVED:
        airport = request["airport"]
        start = _valid_date(payload.get("effective_start") or _now().date().isoformat(), "effective_start")
        if not validate_interval(start, None):
            raise _err("BAD_INTERVAL", "生效日期非法")
        # 批准前先惰性清理过期接管，再判断并发
        current = _now()
        conflicts = [
            item for item in store.list_takeovers(airport=airport, include_closed=False)
            if item["cover_start"] <= start and start < item["cover_end"]
            and datetime.fromisoformat(item["expires_at"]) > current
        ]
        if conflicts:
            adjudication = str(payload.get("adjudication") or "").strip().upper()
            if adjudication not in (ADJUDICATION_BLOCK, ADJUDICATION_OVERRIDE):
                raise _err(
                    "TAKEOVER_CONFLICT",
                    f"机场 {airport} 自 {start} 起存在有效应急接管 {conflicts[0]['takeover_id']}，"
                    "必须显式裁决: BLOCK_UNTIL_TAKEOVER_CLOSES 或 APPROVE_AS_LATEST_FORMAL",
                    409,
                )
            if adjudication == ADJUDICATION_BLOCK:
                raise _err(
                    "APPROVAL_BLOCKED_BY_TAKEOVER",
                    f"裁决为搁置，待接管 {conflicts[0]['takeover_id']} 关闭后再批准",
                    409,
                )
            # OVERRIDE：正式申请照常写入最新正式登记；接管在其覆盖日期内仍优先，
            # 接管到期/撤销后查询直接落到这条新正式登记，而不是旧值。
        store.finalize_request(request_id, reviewer, store.STATUS_APPROVED, decision_reason, adjudication)
        store.add_formal_version(airport, start, request["proposed_timezone"], request_id)
        store.append_event(
            store.EVENT_REQUEST_REVIEWED, request_id, reviewer,
            {"decision": decision, "adjudication": adjudication, "effective_start": start},
        )
        return {"request_id": request_id, "status": store.STATUS_APPROVED, "reviewer": reviewer,
                "airport": airport, "effective_start": start,
                "timezone": request["proposed_timezone"], "adjudication": adjudication}

    store.finalize_request(request_id, reviewer, store.STATUS_REJECTED, decision_reason, None)
    store.append_event(
        store.EVENT_REQUEST_REVIEWED, request_id, reviewer, {"decision": decision},
    )
    return {"request_id": request_id, "status": store.STATUS_REJECTED, "reviewer": reviewer,
            "airport": request["airport"], "adjudication": None}


# ---------- 限时应急接管（两步交接，幂等续跑） ----------

def _takeover_id(airport: str, cover_start: str, cover_end: str, emergency_timezone: str, operator: str) -> str:
    digest = hashlib.sha256(
        f"{airport}|{cover_start}|{cover_end}|{emergency_timezone}|{operator}".encode("utf-8")
    ).hexdigest()[:12]
    return f"EM-{airport}-{digest}"


def _assert_same_payload(existing: dict, expected: dict) -> None:
    fields = ("airport", "cover_start", "cover_end", "emergency_timezone", "expires_at", "reason", "operator")
    mismatched = [field for field in fields if existing[field] != expected[field]]
    if mismatched:
        raise _err(
            "TAKEOVER_ID_CONFLICT",
            f"接管标识已存在但参数不一致: {mismatched}",
            409,
        )


def handoff_takeover(payload: dict, failure_point: str | None = None, now_value: datetime | None = None) -> dict:
    """登记应急接管并完成交接。

    交接两步：1) 落接管主记录并标记 RECORDED；2) 标记 COMPLETED。
    第一步完成后进程失败，重跑同一请求从第二步继续；两步均幂等，
    重复执行返回同一条接管，不新增记录。
    """
    ensure_ready()
    operator, _ = _require_role(payload.get("actor"), ROLE_EMERGENCY)
    airport = normalize_airport(payload["airport"])
    cover_start = _valid_date(payload["cover_start"], "cover_start")
    cover_end = _valid_date(payload["cover_end"], "cover_end")
    if not validate_interval(cover_start, cover_end):
        raise _err("BAD_INTERVAL", "覆盖日期区间必须满足 cover_start < cover_end（半开区间）")
    emergency_timezone = _valid_timezone(payload["emergency_timezone"])
    reason = str(payload.get("reason") or "").strip()
    if not reason:
        raise _err("REASON_REQUIRED", "接管原因不能为空")
    expires_raw = _valid_iso_dt(payload["expires_at"], "expires_at")
    current = now_value or _now()
    if datetime.fromisoformat(expires_raw) <= current:
        raise _err("TAKEOVER_ALREADY_EXPIRED", "接管到期时间必须晚于当前时间")

    takeover_id = str(payload.get("takeover_id") or "").strip() or _takeover_id(
        airport, cover_start, cover_end, emergency_timezone, operator
    )
    expected = {
        "takeover_id": takeover_id, "airport": airport, "cover_start": cover_start,
        "cover_end": cover_end, "emergency_timezone": emergency_timezone,
        "expires_at": expires_raw, "reason": reason, "operator": operator,
    }

    existing = store.get_takeover(takeover_id)
    progress = store.get_handoff(takeover_id)
    resumed = False
    if existing is not None:
        # 幂等续跑：同参重试，不新增接管记录
        _assert_same_payload(existing, expected)
        if existing["status"] != store.TAKEOVER_ACTIVE:
            raise _err("TAKEOVER_NOT_ACTIVE", f"接管已结案: {existing['status']}", 409)
        resumed = True
    else:
        if progress is not None:
            raise _err("HANDOFF_CORRUPT", "存在交接进度但缺少接管主记录")
        # 第一步事务：主记录 + RECORDED 进度
        store.insert_takeover(expected)
        store.upsert_handoff(takeover_id, store.PROGRESS_RECORDED)
        store.append_event(store.EVENT_TAKEOVER_RECORDED, takeover_id, operator,
                           {"airport": airport, "cover_start": cover_start, "cover_end": cover_end})
        if failure_point == "AFTER_RECORDED":
            raise RuntimeError("INJECTED_FAILURE_AFTER_RECORDED")

    if progress is None or progress["step"] != store.PROGRESS_COMPLETED:
        # 第二步事务：复核仍在期限内，标记 COMPLETED
        if datetime.fromisoformat(expires_raw) <= (now_value or _now()):
            raise _err("TAKEOVER_ALREADY_EXPIRED", "续跑时接管已到期，拒绝完成交接")
        store.upsert_handoff(takeover_id, store.PROGRESS_COMPLETED)
        store.append_event(store.EVENT_TAKEOVER_COMPLETED, takeover_id, operator,
                           {"emergency_timezone": emergency_timezone, "expires_at": expires_raw})
        if failure_point == "AFTER_COMPLETED":
            raise RuntimeError("INJECTED_FAILURE_AFTER_COMPLETED")

    final_record = store.get_takeover(takeover_id)
    handoff = store.get_handoff(takeover_id)
    return {
        "takeover_id": takeover_id,
        "status": final_record["status"],
        "handoff_step": handoff["step"],
        "resumed": resumed,
        "idempotent_replay": resumed and handoff["step"] == store.PROGRESS_COMPLETED,
        "airport": airport,
        "cover_start": cover_start,
        "cover_end": cover_end,
        "emergency_timezone": emergency_timezone,
        "expires_at": expires_raw,
    }


def revoke_takeover(payload: dict) -> dict:
    ensure_ready()
    actor, _ = _require_role(payload.get("actor"), ROLE_EMERGENCY)
    takeover_id = str(payload.get("takeover_id") or "").strip()
    if not takeover_id:
        raise _err("TAKEOVER_ID_REQUIRED", "缺少 takeover_id")
    record = store.get_takeover(takeover_id)
    if record is None:
        raise _err("TAKEOVER_NOT_FOUND", f"接管不存在: {takeover_id}", 404)
    if record["status"] != store.TAKEOVER_ACTIVE:
        raise _err("TAKEOVER_NOT_ACTIVE", f"接管已结案: {record['status']}", 409)
    store.mark_takeover_status(takeover_id, store.TAKEOVER_REVOKED, actor)
    store.append_event(store.EVENT_TAKEOVER_REVOKED, takeover_id, actor, {"reason": payload.get("reason")})
    return {"takeover_id": takeover_id, "status": store.TAKEOVER_REVOKED, "revoked_by": actor}


def sweep_expired(now_value: datetime | None = None) -> dict:
    """惰性/显式清理：把到期接管落库为 EXPIRED，正式登记始终未被改写。"""
    ensure_ready()
    current = now_value or _now()
    changed = []
    # 单遍扫描：按时间点判定，逐条借 active_takeover_for 完成惰性落库
    for record in store.list_takeovers(include_closed=False):
        if datetime.fromisoformat(record["expires_at"]) <= current:
            store.active_takeover_for(record["airport"], record["cover_start"], current)
            if store.get_takeover(record["takeover_id"])["status"] == store.TAKEOVER_EXPIRED:
                changed.append(record["takeover_id"])
    return {"expired": changed, "count": len(changed)}


# ---------- 回读 ----------

def readback(airport: str, at: str, now_value: datetime | None = None) -> dict:
    """回读正式来源、应急来源、期限与交接状态。"""
    ensure_ready()
    code = normalize_airport(airport)
    target = _valid_date(at, "at")
    current = now_value or _now()
    effective = effective_zone(code, target, current)
    formal = formal_registry(code, target)
    active = store.active_takeover_for(code, target, current)
    takeovers = []
    for record in store.list_takeovers(airport=code):
        progress = store.get_handoff(record["takeover_id"])
        takeovers.append({
            "takeover_id": record["takeover_id"],
            "status": record["status"],
            "cover_start": record["cover_start"],
            "cover_end": record["cover_end"],
            "emergency_timezone": record["emergency_timezone"],
            "expires_at": record["expires_at"],
            "handoff_step": progress["step"] if progress else None,
            "operator": record["operator"],
            "revoked_at": record["revoked_at"],
        })
    return {
        "airport": code,
        "at": target,
        "read_at": current.astimezone().isoformat(timespec="seconds"),
        "effective": effective,
        "formal_source": {
            "timezone": formal["timezone"],
            "start": formal["start"],
            "end": formal["end"],
            "source": formal["source"],
            "request_id": formal.get("request_id"),
        },
        "emergency_source": active,
        "takeovers": takeovers,
        "pending_requests": [
            item for item in store.list_requests(airport=code) if item["status"] == store.STATUS_PENDING
        ],
    }


def list_change_requests(airport: str | None = None) -> list[dict]:
    ensure_ready()
    return store.list_requests(normalize_airport(airport) if airport else None)
