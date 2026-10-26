from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from foundation.audit import DENIED
from foundation.auth import USER_COOKIE
from foundation.db import get_session
from foundation.models import AuditLog, User
from foundation.policy import require
from foundation.ui import templates

router = APIRouter()


@router.post("/switch-user")
def switch_user(
    request: Request,
    user_id: Annotated[str, Form()],
    session: Annotated[Session, Depends(get_session)],
) -> RedirectResponse:
    if session.get(User, user_id) is None:
        raise HTTPException(status_code=400, detail=f"Unknown user {user_id!r}")
    referer = urlsplit(request.headers.get("referer", ""))
    target = (referer.path or "/") + (f"?{referer.query}" if referer.query else "")
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(USER_COOKIE, user_id, httponly=True, samesite="lax")
    return response


@router.get("/audit", response_class=HTMLResponse)
def audit_log(
    request: Request,
    user: Annotated[User, Depends(require("audit.view"))],
    session: Annotated[Session, Depends(get_session)],
    action: str | None = None,
) -> HTMLResponse:
    query = select(AuditLog).order_by(AuditLog.id.desc()).limit(200)
    if action:
        query = query.where(AuditLog.action == action)
    rows = [
        {
            "at": e.at.strftime("%Y-%m-%d %H:%M:%S"),
            "actor": e.actor_id or "-",
            "entity": f"{e.entity}:{e.entity_id}",
            "action": e.action,
            "entry": e,
            "flagged": e.action == DENIED,
        }
        for e in session.scalars(query)
    ]
    count = select(func.count()).select_from(AuditLog)
    return templates.TemplateResponse(
        request,
        "audit.html",
        {
            "user": user,
            "rows": rows,
            "actions": session.scalars(select(AuditLog.action).distinct().order_by("action")).all(),
            "selected_action": action,
            "total": session.scalar(count),
            "denied": session.scalar(count.where(AuditLog.action == DENIED)),
        },
    )
