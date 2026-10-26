from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import case, select
from sqlalchemy.orm import Session

from apps.feature_flags import rules, service
from apps.feature_flags.models import Flag, FlagChangeRequest
from foundation.db import get_session
from foundation.models import AuditLog, User
from foundation.policy import can, require
from foundation.ui import make_templates

router = APIRouter(prefix="/flags")
templates = make_templates(Path(__file__).parent / "templates")

SessionDep = Annotated[Session, Depends(get_session)]
EnvironmentForm = Annotated[str | None, Form()]


def _ago(value: datetime | None) -> str:
    if value is None:
        return ""
    seconds = int((datetime.now(UTC) - value).total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("min", 60)):
        if seconds >= size:
            return f"{seconds // size} {unit} ago"
    return "just now"


templates.env.filters["ago"] = _ago
templates.env.filters["on_off"] = rules.on_off


def _board_context(session: Session, user: User, environment: str | None) -> dict:
    env_order = case({env: i for i, env in enumerate(rules.ENVIRONMENTS)}, value=Flag.environment)
    query = select(Flag).order_by(env_order, Flag.key)
    if environment:
        query = query.where(Flag.environment == environment)
    flags = list(session.scalars(query))
    pending = list(
        session.execute(
            select(FlagChangeRequest, Flag)
            .join(Flag, Flag.id == FlagChangeRequest.flag_id)
            .where(FlagChangeRequest.status == "pending")
            .order_by(FlagChangeRequest.id)
        )
    )
    recent = session.scalars(
        select(AuditLog)
        .where(AuditLog.entity.in_((service.FLAG, service.REQUEST)))
        .order_by(AuditLog.id.desc())
        .limit(10)
    )
    return {
        "user": user,
        "environments": [e for e in rules.ENVIRONMENTS if not environment or e == environment],
        "selected_environment": environment,
        "flags": flags,
        "pending_by_flag": {req.flag_id: req for req, _ in pending},
        "pending": pending,
        "recent": list(recent),
        "summary": {
            "flags": len(flags) if not environment else session.query(Flag).count(),
            "prod_on": session.query(Flag).filter_by(environment="prod", enabled=True).count(),
            "pending": len(pending),
        },
        "can_toggle": can(user, "flags.toggle_nonprod"),
        "can_request": can(user, "flags.request_prod_change"),
        "can_decide": can(user, "flags.approve_prod_change"),
    }


def _respond(request: Request, session: Session, user: User, environment: str | None, message: str):
    if request.headers.get("hx-request") == "true":
        context = _board_context(session, user, environment) | {"message": message}
        return templates.TemplateResponse(request, "flags/_board.html", context)
    url = f"/flags?environment={environment}" if environment else "/flags"
    return RedirectResponse(url, status_code=303)


@router.get("", response_class=HTMLResponse)
def list_flags(
    request: Request,
    user: Annotated[User, Depends(require("flags.view"))],
    session: SessionDep,
    environment: str | None = None,
):
    context = _board_context(session, user, environment or None)
    return templates.TemplateResponse(
        request, "flags/index.html", context | {"all_environments": rules.ENVIRONMENTS}
    )


@router.post("/{flag_id}/toggle", response_class=HTMLResponse)
def toggle_flag(
    request: Request,
    flag_id: int,
    user: Annotated[User, Depends(require("flags.toggle_nonprod"))],
    session: SessionDep,
    enabled: Annotated[bool, Form()],
    environment: EnvironmentForm = None,
):
    flag = service.toggle(session, user, flag_id, enabled)
    message = f"{flag.environment} {flag.key} turned {rules.on_off(flag.enabled)}."
    return _respond(request, session, user, environment, message)


@router.post("/{flag_id}/request-change", response_class=HTMLResponse)
def request_flag_change(
    request: Request,
    flag_id: int,
    user: Annotated[User, Depends(require("flags.request_prod_change"))],
    session: SessionDep,
    enabled: Annotated[bool, Form()],
    environment: EnvironmentForm = None,
):
    req = service.request_change(session, user, flag_id, enabled)
    flag = service.get_flag(session, flag_id)
    message = (
        f"Change request #{req.id} opened: {flag.environment} {flag.key} → "
        f"{rules.on_off(enabled)}. Another engineer or an admin must approve it."
    )
    return _respond(request, session, user, environment, message)


@router.post("/requests/{request_id}/approve", response_class=HTMLResponse)
def approve_flag_change(
    request: Request,
    request_id: int,
    user: Annotated[User, Depends(require("flags.approve_prod_change"))],
    session: SessionDep,
    environment: EnvironmentForm = None,
):
    req = service.approve(session, user, request_id)
    flag = service.get_flag(session, req.flag_id)
    message = (
        f"Change request #{req.id} approved: {flag.environment} {flag.key} "
        f"is now {rules.on_off(flag.enabled)}."
    )
    return _respond(request, session, user, environment, message)


@router.post("/requests/{request_id}/reject", response_class=HTMLResponse)
def reject_flag_change(
    request: Request,
    request_id: int,
    user: Annotated[User, Depends(require("flags.approve_prod_change"))],
    session: SessionDep,
    environment: EnvironmentForm = None,
):
    req = service.reject(session, user, request_id)
    message = f"Change request #{req.id} rejected; the flag is unchanged."
    return _respond(request, session, user, environment, message)
