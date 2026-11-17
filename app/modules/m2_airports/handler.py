from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from app.web import page
from . import governance
from .service import demo

router = APIRouter()
ROWS = [{"key":"PVG","status":"Asia/Shanghai","value":"+08:00"},{"key":"JFK","status":"America/New_York","value":"DST"},{"key":"LHR","status":"Europe/London","value":"DST"}]


class ActorModel(BaseModel):
    user: str = Field(min_length=1)
    roles: list[str] = Field(default_factory=list)


class ChangeRequestModel(BaseModel):
    airport: str
    proposed_timezone: str
    reason: str
    actor: ActorModel


class ReviewModel(BaseModel):
    request_id: str
    decision: str
    decision_reason: str
    effective_start: str | None = None
    adjudication: str | None = None
    actor: ActorModel


class TakeoverModel(BaseModel):
    airport: str
    cover_start: str
    cover_end: str
    emergency_timezone: str
    expires_at: str
    reason: str
    takeover_id: str | None = None
    # 仅测试用：在指定交接步骤后注入故障，验证续跑
    failure_point: str | None = None
    actor: ActorModel


class RevokeModel(BaseModel):
    takeover_id: str
    reason: str | None = None
    actor: ActorModel


class EffectiveQueryModel(BaseModel):
    airport: str
    at: str
    actor: ActorModel | None = None


@router.get("/airports", response_class=HTMLResponse)
def view(request: Request):
    return page(request, "m2.html", "机场时区", "/api/airports", ROWS)


@router.post("/api/airports")
def execute():
    return demo()


@router.post("/api/airports/effective")
def effective_query(body: EffectiveQueryModel):
    return governance.effective_zone(body.airport, body.at)


@router.post("/api/airports/change-requests")
def submit_change_request(body: ChangeRequestModel):
    return governance.submit_change_request(body.model_dump())


@router.get("/api/airports/change-requests")
def list_change_requests(airport: str | None = None):
    return {"items": governance.list_change_requests(airport)}


@router.post("/api/airports/change-requests/review")
def review_change_request(body: ReviewModel):
    return governance.review_change_request(body.model_dump())


@router.post("/api/airports/takeovers")
def handoff_takeover(body: TakeoverModel):
    payload = body.model_dump(exclude={"failure_point"})
    return governance.handoff_takeover(payload, failure_point=body.failure_point)


@router.post("/api/airports/takeovers/revoke")
def revoke_takeover(body: RevokeModel):
    return governance.revoke_takeover(body.model_dump())


@router.post("/api/airports/takeovers/sweep-expired")
def sweep_expired():
    return governance.sweep_expired()


@router.get("/api/airports/readback")
def readback(airport: str, at: str):
    return governance.readback(airport, at)
