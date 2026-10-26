from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException

from apps.feature_flags import routes as flags_routes
from apps.refunds import routes as refunds_routes
from foundation import routes as foundation_routes
from foundation.auth import user_from_cookie
from foundation.db import SessionLocal, get_session
from foundation.policy import can
from foundation.ui import STATIC_DIR, templates

app = FastAPI(title="Internal Tools Foundation", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.include_router(foundation_routes.router)
app.include_router(refunds_routes.router)
app.include_router(flags_routes.router)

TITLES = {400: "Bad request", 403: "Not allowed", 404: "Not found", 409: "Conflict"}
TITLES[422] = "Check the form"
PERMISSION_DENIED = "Permission denied: "


def _readable(
    status_code: int, detail: object, user, stale_page_hint: bool = True
) -> tuple[str, str]:
    text_ = str(detail)
    if status_code == 403 and text_.startswith(PERMISSION_DENIED):
        action = text_.removeprefix(PERMISSION_DENIED)
        if user is None:
            text_ = "Choose a user in the header to continue."
        else:
            text_ = (
                f"{user.name} ({user.role}) is not permitted to do this ({action}). "
                "The attempt was recorded in the audit log."
            )
    elif status_code == 409 and stale_page_hint:
        text_ = f"{text_}. The page may be out of date: reload to see the current state."
    return TITLES.get(status_code, f"Error {status_code}"), text_


def _wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "") or _is_htmx(request)


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def _error_page(
    request: Request, status_code: int, detail: object, stale_page_hint: bool = True
) -> Response:
    with SessionLocal() as session:
        user = user_from_cookie(request, session)
    title, message = _readable(status_code, detail, user, stale_page_hint)
    if _is_htmx(request):
        # Render into the page's message area instead of replacing the action's target.
        return templates.TemplateResponse(
            request,
            "partials/message.html",
            {"title": title, "text": message},
            status_code=status_code,
            headers={"HX-Retarget": "#messages", "HX-Reswap": "innerHTML"},
        )
    return templates.TemplateResponse(
        request,
        "error.html",
        {"user": user, "status_code": status_code, "title": title, "detail": message},
        status_code=status_code,
    )


@app.exception_handler(StarletteHTTPException)
async def html_errors(request: Request, exc: StarletteHTTPException) -> Response:
    if not _wants_html(request):
        return await http_exception_handler(request, exc)
    return _error_page(request, exc.status_code, exc.detail, getattr(exc, "stale_page_hint", True))


@app.exception_handler(RequestValidationError)
async def html_validation_errors(request: Request, exc: RequestValidationError) -> Response:
    if not _wants_html(request):
        return await request_validation_exception_handler(request, exc)
    fields = "; ".join(f"{e['loc'][-1]}: {e['msg'].lower()}" for e in exc.errors())
    return _error_page(request, 422, fields)


@app.get("/", include_in_schema=False)
def index(request: Request) -> RedirectResponse:
    with SessionLocal() as session:
        user = user_from_cookie(request, session)
    home = "/flags" if can(user, "flags.view") and not can(user, "refunds.view") else "/refunds"
    return RedirectResponse(home, status_code=303)


@app.get("/health")
def health(session: Annotated[Session, Depends(get_session)]) -> JSONResponse:
    try:
        session.execute(text("SELECT 1"))
    except SQLAlchemyError:
        return JSONResponse({"status": "error", "database": "unavailable"}, status_code=503)
    return JSONResponse({"status": "ok", "database": "ok"})
