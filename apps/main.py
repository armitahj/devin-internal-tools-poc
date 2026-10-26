from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException

from apps.refunds import routes as refunds_routes
from foundation import routes as foundation_routes
from foundation.auth import user_from_cookie
from foundation.db import SessionLocal, get_session
from foundation.ui import STATIC_DIR, templates

app = FastAPI(title="Internal Tools Foundation", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.include_router(foundation_routes.router)
app.include_router(refunds_routes.router)


@app.exception_handler(StarletteHTTPException)
async def html_errors(request: Request, exc: StarletteHTTPException) -> Response:
    if "text/html" not in request.headers.get("accept", ""):
        return await http_exception_handler(request, exc)
    with SessionLocal() as session:
        user = user_from_cookie(request, session)
    return templates.TemplateResponse(
        request,
        "error.html",
        {"user": user, "status_code": exc.status_code, "detail": exc.detail},
        status_code=exc.status_code,
    )


@app.get("/health")
def health(session: Annotated[Session, Depends(get_session)]) -> JSONResponse:
    try:
        session.execute(text("SELECT 1"))
    except SQLAlchemyError:
        return JSONResponse({"status": "error", "database": "unavailable"}, status_code=503)
    return JSONResponse({"status": "ok", "database": "ok"})
