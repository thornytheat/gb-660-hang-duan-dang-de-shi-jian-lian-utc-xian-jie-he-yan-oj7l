"""机场时区治理的持久层：登记变更申请、复核记录、限时应急接管与交接进度。

所有写操作都在短事务内完成；交接被拆成两个独立事务，任一进程在中途
崩溃后，重跑同一请求可以依据 handoff 进度继续，不会新增接管记录。
"""
import sqlite3
from datetime import datetime
from app.store import connect
from .contracts import normalize_airport
from .registry import AIRPORTS, airport_version as formal_seed_version

STATUS_PENDING = "PENDING"
STATUS_APPROVED = "APPROVED"
STATUS_REJECTED = "REJECTED"

TAKEOVER_ACTIVE = "ACTIVE"
TAKEOVER_EXPIRED = "EXPIRED"
TAKEOVER_REVOKED = "REVOKED"

PROGRESS_RECORDED = "RECORDED"
PROGRESS_COMPLETED = "COMPLETED"

EVENT_REQUEST_SUBMITTED = "REQUEST_SUBMITTED"
EVENT_REQUEST_REVIEWED = "REQUEST_REVIEWED"
EVENT_TAKEOVER_RECORDED = "TAKEOVER_RECORDED"
EVENT_TAKEOVER_COMPLETED = "TAKEOVER_COMPLETED"
EVENT_TAKEOVER_EXPIRED = "TAKEOVER_EXPIRED"
EVENT_TAKEOVER_REVOKED = "TAKEOVER_REVOKED"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def init_governance_db() -> None:
    with connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS tz_change_requests (
                request_id        TEXT PRIMARY KEY,
                airport           TEXT NOT NULL,
                proposed_timezone TEXT NOT NULL,
                reason            TEXT NOT NULL,
                applicant         TEXT NOT NULL,
                reviewer          TEXT,
                status            TEXT NOT NULL,
                decision          TEXT,
                decision_reason   TEXT,
                adjudication      TEXT,
                created_at        TEXT NOT NULL,
                reviewed_at       TEXT
            );
            CREATE TABLE IF NOT EXISTS tz_formal_versions (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                airport   TEXT NOT NULL,
                start     TEXT NOT NULL,
                end_      TEXT,
                timezone  TEXT NOT NULL,
                source    TEXT NOT NULL,
                request_id TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(airport, start, timezone)
            );
            CREATE TABLE IF NOT EXISTS tz_takeovers (
                takeover_id  TEXT PRIMARY KEY,
                airport      TEXT NOT NULL,
                cover_start  TEXT NOT NULL,
                cover_end    TEXT NOT NULL,
                emergency_timezone TEXT NOT NULL,
                expires_at   TEXT NOT NULL,
                reason       TEXT NOT NULL,
                operator     TEXT NOT NULL,
                status       TEXT NOT NULL,
                created_at   TEXT NOT NULL,
                revoked_at   TEXT,
                revoke_actor TEXT
            );
            CREATE TABLE IF NOT EXISTS tz_handoff_progress (
                takeover_id TEXT PRIMARY KEY,
                step        TEXT NOT NULL,
                updated_at  TEXT NOT NULL,
                FOREIGN KEY(takeover_id) REFERENCES tz_takeovers(takeover_id)
            );
            CREATE TABLE IF NOT EXISTS tz_governance_events (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                entity_id  TEXT NOT NULL,
                actor      TEXT,
                payload    TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_tz_takeovers_airport ON tz_takeovers(airport, status);
            CREATE INDEX IF NOT EXISTS idx_tz_formal_airport ON tz_formal_versions(airport);
            CREATE INDEX IF NOT EXISTS idx_tz_requests_status ON tz_change_requests(status);
            """
        )


def seed_baseline() -> int:
    """把内存基线登记幂等写入正式版本表；已存在则跳过。返回新增条数。"""
    inserted = 0
    stamp = now_iso()
    with connect() as db:
        for code, versions in AIRPORTS.items():
            for start, end, zone, terminal in versions:
                cur = db.execute(
                    "INSERT OR IGNORE INTO tz_formal_versions(airport, start, end_, timezone, source, request_id, created_at)"
                    " VALUES (?, ?, ?, ?, 'BASELINE', NULL, ?)",
                    (code, start, end, zone, stamp),
                )
                inserted += cur.rowcount
    return inserted


def ensure_ready() -> None:
    init_governance_db()
    seed_baseline()


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


# ---------- 变更申请 ----------

def insert_request(request_id: str, airport: str, timezone: str, reason: str, applicant: str) -> None:
    with connect() as db:
        db.execute(
            "INSERT INTO tz_change_requests(request_id, airport, proposed_timezone, reason, applicant, status, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (request_id, airport, timezone, reason, applicant, STATUS_PENDING, now_iso()),
        )


def get_request(request_id: str) -> dict | None:
    with connect() as db:
        db.row_factory = sqlite3.Row
        return _row_to_dict(db.execute("SELECT * FROM tz_change_requests WHERE request_id = ?", (request_id,)).fetchone())


def list_requests(airport: str | None = None) -> list[dict]:
    with connect() as db:
        db.row_factory = sqlite3.Row
        if airport:
            rows = db.execute(
                "SELECT * FROM tz_change_requests WHERE airport = ? ORDER BY created_at, request_id", (airport,)
            ).fetchall()
        else:
            rows = db.execute("SELECT * FROM tz_change_requests ORDER BY created_at, request_id").fetchall()
        return [_row_to_dict(row) for row in rows]


def finalize_request(request_id: str, reviewer: str, status: str, decision_reason: str, adjudication: str | None) -> None:
    with connect() as db:
        db.execute(
            "UPDATE tz_change_requests SET reviewer = ?, status = ?, decision = ?, decision_reason = ?,"
            " adjudication = ?, reviewed_at = ? WHERE request_id = ? AND status = ?",
            (reviewer, status, status, decision_reason, adjudication, now_iso(), request_id, STATUS_PENDING),
        )


# ---------- 正式登记版本 ----------

def list_formal_versions(airport: str | None = None) -> list[dict]:
    with connect() as db:
        db.row_factory = sqlite3.Row
        if airport:
            rows = db.execute(
                "SELECT airport, start, end_ AS end, timezone, source, request_id, created_at"
                " FROM tz_formal_versions WHERE airport = ? ORDER BY start",
                (airport,),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT airport, start, end_ AS end, timezone, source, request_id, created_at"
                " FROM tz_formal_versions ORDER BY airport, start"
            ).fetchall()
        return [_row_to_dict(row) for row in rows]


def formal_versions_for(airport: str) -> list[dict]:
    return list_formal_versions(normalize_airport(airport))


def add_formal_version(airport: str, start: str, timezone: str, request_id: str) -> None:
    """批准新版本：关闭该机场仍开放的旧版本，再写入新版本，单事务提交。"""
    stamp = now_iso()
    with connect() as db:
        db.execute(
            "UPDATE tz_formal_versions SET end_ = ? WHERE airport = ? AND end_ IS NULL",
            (start, airport),
        )
        db.execute(
            "INSERT INTO tz_formal_versions(airport, start, end_, timezone, source, request_id, created_at)"
            " VALUES (?, ?, NULL, ?, 'APPROVED_REQUEST', ?, ?)",
            (airport, start, timezone, request_id, stamp),
        )


# ---------- 应急接管 ----------

def get_takeover(takeover_id: str) -> dict | None:
    with connect() as db:
        db.row_factory = sqlite3.Row
        return _row_to_dict(db.execute("SELECT * FROM tz_takeovers WHERE takeover_id = ?", (takeover_id,)).fetchone())


def list_takeovers(airport: str | None = None, include_closed: bool = True) -> list[dict]:
    with connect() as db:
        db.row_factory = sqlite3.Row
        sql = "SELECT * FROM tz_takeovers"
        params: list = []
        clauses = []
        if airport:
            clauses.append("airport = ?")
            params.append(airport)
        if not include_closed:
            clauses.append("status = ?")
            params.append(TAKEOVER_ACTIVE)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at, takeover_id"
        return [_row_to_dict(row) for row in db.execute(sql, params).fetchall()]


def insert_takeover(record: dict) -> None:
    with connect() as db:
        db.execute(
            "INSERT INTO tz_takeovers(takeover_id, airport, cover_start, cover_end, emergency_timezone, expires_at,"
            " reason, operator, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record["takeover_id"], record["airport"], record["cover_start"], record["cover_end"],
                record["emergency_timezone"], record["expires_at"], record["reason"], record["operator"],
                TAKEOVER_ACTIVE, now_iso(),
            ),
        )


def get_handoff(takeover_id: str) -> dict | None:
    with connect() as db:
        db.row_factory = sqlite3.Row
        return _row_to_dict(
            db.execute("SELECT * FROM tz_handoff_progress WHERE takeover_id = ?", (takeover_id,)).fetchone()
        )


def upsert_handoff(takeover_id: str, step: str) -> None:
    with connect() as db:
        db.execute(
            "INSERT INTO tz_handoff_progress(takeover_id, step, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(takeover_id) DO UPDATE SET step = excluded.step, updated_at = excluded.updated_at",
            (takeover_id, step, now_iso()),
        )


def mark_takeover_status(takeover_id: str, status: str, actor: str | None = None) -> None:
    with connect() as db:
        if status == TAKEOVER_REVOKED:
            db.execute(
                "UPDATE tz_takeovers SET status = ?, revoked_at = ?, revoke_actor = ?"
                " WHERE takeover_id = ? AND status = ?",
                (status, now_iso(), actor, takeover_id, TAKEOVER_ACTIVE),
            )
        else:
            db.execute(
                "UPDATE tz_takeovers SET status = ? WHERE takeover_id = ? AND status = ?",
                (status, takeover_id, TAKEOVER_ACTIVE),
            )


def active_takeover_for(airport: str, at: str, now_value: datetime) -> dict | None:
    """返回指定机场、日期上当前仍在期限内的接管；过期记录顺带落库为 EXPIRED。"""
    with connect() as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT * FROM tz_takeovers WHERE airport = ? AND status = ?"
            " AND cover_start <= ? AND ? < cover_end ORDER BY created_at, takeover_id",
            (airport, TAKEOVER_ACTIVE, at, at),
        ).fetchall()
        live = []
        for row in rows:
            if datetime.fromisoformat(row["expires_at"]) <= now_value:
                db.execute(
                    "UPDATE tz_takeovers SET status = ? WHERE takeover_id = ? AND status = ?",
                    (TAKEOVER_EXPIRED, row["takeover_id"], TAKEOVER_ACTIVE),
                )
                db.execute(
                    "INSERT INTO tz_governance_events(event_type, entity_id, actor, payload, created_at)"
                    " VALUES (?, ?, NULL, ?, ?)",
                    (EVENT_TAKEOVER_EXPIRED, row["takeover_id"], "{}", now_iso()),
                )
            else:
                live.append(_row_to_dict(row))
    if not live:
        return None
    if len(live) > 1:
        # 同一机场同一日期不允许出现两条并行接管，登记侧缺陷必须显式暴露
        raise RuntimeError("TAKEOVER_OVERLAP")
    return live[0]


def append_event(event_type: str, entity_id: str, actor: str | None, payload: dict) -> None:
    import json
    with connect() as db:
        db.execute(
            "INSERT INTO tz_governance_events(event_type, entity_id, actor, payload, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (event_type, entity_id, actor, json.dumps(payload, ensure_ascii=False, sort_keys=True), now_iso()),
        )


def list_events(entity_id: str | None = None) -> list[dict]:
    import json
    with connect() as db:
        db.row_factory = sqlite3.Row
        if entity_id:
            rows = db.execute(
                "SELECT * FROM tz_governance_events WHERE entity_id = ? ORDER BY id", (entity_id,)
            ).fetchall()
        else:
            rows = db.execute("SELECT * FROM tz_governance_events ORDER BY id").fetchall()
        result = []
        for row in rows:
            item = _row_to_dict(row)
            item["payload"] = json.loads(item["payload"])
            result.append(item)
        return result


def formal_version_from_store(code: str, at: str) -> dict | None:
    """优先读正式登记表；该机场没有登记行时返回 None，由调用方回退内存基线。"""
    versions = formal_versions_for(code)
    if not versions:
        return None
    matches = [
        item for item in versions
        if item["start"] <= at and (item["end"] is None or at < item["end"])
    ]
    if not matches:
        return None
    if len(matches) > 1:
        raise ValueError("TIMEZONE_VERSION_OVERLAP")
    return matches[0]


def seed_lookup_version(code: str, at: str) -> dict:
    return formal_seed_version(code, at)
