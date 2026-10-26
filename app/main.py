import hmac
import os
import re
import secrets
import uuid
from decimal import Decimal
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from starlette.middleware.sessions import SessionMiddleware

from app.db import make_engine, make_sessionmaker
from app.models import AuditEvent, PaymentTransaction, Refund, RefundStatus, User
from app.payments.simulator import SimulatedProvider
from app.refunds.service import (
    Conflict,
    Forbidden,
    InsufficientBalance,
    InvalidRequest,
    NotFound,
    RefundError,
    RefundService,
    SimulatedCrash,
    compute_balance,
    demo_crash_hook,
)

ERROR_STATUS = {Forbidden: 403, NotFound: 404, Conflict: 409, InsufficientBalance: 422, InvalidRequest: 400}
AMOUNT_RE = re.compile(r"^\d+(\.\d{1,2})?$")


class LoginRequired(Exception):
    pass


def format_money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def parse_amount(text: str) -> int:
    text = text.strip().lstrip("$")
    if not AMOUNT_RE.match(text):
        raise InvalidRequest("amount must be a number with at most two decimals, e.g. 12.34")
    return int(Decimal(text) * 100)


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


async def verify_csrf(request: Request) -> None:
    form = await request.form()
    sent = str(form.get("csrf_token", ""))
    expected = request.session.get("csrf")
    if not expected or not hmac.compare_digest(sent, expected):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid")


def flash(request: Request, message: str, kind: str = "info") -> None:
    request.session.setdefault("flash", []).append([kind, message])


def create_app(sessionmaker=None, provider=None, crash_hook=demo_crash_hook) -> FastAPI:
    if sessionmaker is None:
        sessionmaker = make_sessionmaker(make_engine())
    service = RefundService(sessionmaker, provider or SimulatedProvider(sessionmaker), crash_hook)

    app = FastAPI(title="Refunds workflow prototype (simulated)")
    app.add_middleware(
        SessionMiddleware,
        secret_key=os.environ.get("SESSION_SECRET", "dev-only-insecure-secret"),
        session_cookie="refunds_session",
        same_site="lax",
    )
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
    templates.env.filters["money"] = format_money

    def current_user(request: Request) -> User:
        uid = request.session.get("user_id")
        if uid:
            with sessionmaker() as s:
                user = s.get(User, uid)
            if user:
                return user
        raise LoginRequired()

    def render(request: Request, name: str, status_code: int = 200, **ctx):
        uid = request.session.get("user_id")
        if "user" not in ctx and uid:
            with sessionmaker() as s:
                ctx["user"] = s.get(User, uid)
        ctx.setdefault("user", None)
        ctx["csrf_token"] = csrf_token(request)
        ctx["flashes"] = request.session.pop("flash", [])
        ctx["S"] = RefundStatus
        return templates.TemplateResponse(request, name, ctx, status_code=status_code)

    def redirect(url: str):
        return RedirectResponse(url, status_code=303)

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        return redirect("/login")

    @app.exception_handler(RefundError)
    async def _refund_error(request: Request, exc: RefundError):
        return render(request, "error.html", ERROR_STATUS.get(type(exc), 400), message=str(exc))

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException):
        return render(request, "error.html", exc.status_code, message=str(exc.detail))

    # --- simulated authentication --------------------------------------------------------------

    @app.get("/login")
    def login_page(request: Request):
        with sessionmaker() as s:
            users = s.execute(select(User).order_by(User.id)).scalars().all()
        return render(request, "login.html", users=users)

    @app.post("/login", dependencies=[Depends(verify_csrf)])
    def login(request: Request, user_id: str = Form(...)):
        with sessionmaker() as s:
            if s.get(User, user_id) is None:
                raise Forbidden("unknown demo user")
        csrf = request.session.get("csrf")
        request.session.clear()
        request.session["user_id"] = user_id
        request.session["csrf"] = csrf
        return redirect("/transactions")

    @app.post("/logout", dependencies=[Depends(verify_csrf)])
    def logout(request: Request):
        request.session.clear()
        return redirect("/login")

    # --- read-only pages -----------------------------------------------------------------------

    @app.get("/")
    def index(user: User = Depends(current_user)):
        return redirect("/refunds?status=REQUESTED" if user.can_review else "/transactions")

    @app.get("/transactions")
    def transactions(request: Request, user: User = Depends(current_user)):
        with sessionmaker() as s:
            txns = s.execute(select(PaymentTransaction).order_by(PaymentTransaction.id)).scalars().all()
            rows = [(t, compute_balance(s, t)) for t in txns]
        return render(request, "transactions.html", user=user, rows=rows)

    @app.get("/transactions/{txn_id}")
    def transaction(request: Request, txn_id: str, user: User = Depends(current_user)):
        with sessionmaker() as s:
            txn = s.get(PaymentTransaction, txn_id)
            if txn is None:
                raise NotFound("transaction not found")
            balance = compute_balance(s, txn)
            refunds = s.execute(select(Refund).where(Refund.transaction_id == txn_id).order_by(Refund.id)).scalars().all()
        return render(
            request,
            "transaction.html",
            user=user,
            txn=txn,
            balance=balance,
            refunds=refunds,
            request_key=str(uuid.uuid4()),
        )

    @app.get("/refunds")
    def refunds(request: Request, status: str | None = None, user: User = Depends(current_user)):
        with sessionmaker() as s:
            q = select(Refund).order_by(Refund.id.desc())
            if status:
                q = q.where(Refund.status == status)
            items = s.execute(q).scalars().all()
        return render(request, "refunds.html", user=user, refunds=items, status=status)

    @app.get("/refunds/{refund_id}")
    def refund(request: Request, refund_id: int, user: User = Depends(current_user)):
        with sessionmaker() as s:
            r = s.get(Refund, refund_id)
            if r is None:
                raise NotFound("refund not found")
            txn = s.get(PaymentTransaction, r.transaction_id)
            balance = compute_balance(s, txn)
            events = s.execute(select(AuditEvent).where(AuditEvent.refund_id == refund_id).order_by(AuditEvent.id)).scalars().all()
        can_review = user.can_review and r.requested_by != user.id and r.status == RefundStatus.REQUESTED
        can_retry = user.can_review and r.status == RefundStatus.PROCESSING
        return render(
            request,
            "refund.html",
            user=user,
            r=r,
            txn=txn,
            balance=balance,
            events=events,
            can_review=can_review,
            can_retry=can_retry,
        )

    # --- state-changing actions (POST + CSRF only) ---------------------------------------------

    @app.post("/transactions/{txn_id}/refunds", dependencies=[Depends(verify_csrf)])
    def request_refund(
        request: Request,
        txn_id: str,
        amount: str = Form(...),
        reason: str = Form(""),
        request_key: str = Form(...),
        user: User = Depends(current_user),
    ):
        r = service.request_refund(user.id, txn_id, parse_amount(amount), reason, request_key)
        flash(request, f"Refund #{r.id} requested. A different user with reviewer permissions must approve it.")
        return redirect(f"/refunds/{r.id}")

    @app.post("/refunds/{refund_id}/approve", dependencies=[Depends(verify_csrf)])
    def approve(request: Request, refund_id: int, note: str = Form(""), user: User = Depends(current_user)):
        service.approve(user.id, refund_id, note)
        try:
            r = service.execute(user.id, refund_id)
            if r.status == RefundStatus.SUCCEEDED:
                flash(request, "Approved and refunded by the simulated provider.", "success")
            elif r.status == RefundStatus.FAILED:
                flash(request, f"Approved, but the provider rejected the refund: {r.last_error}", "error")
            else:
                flash(
                    request,
                    f"Approved, but the provider outcome is not confirmed ({r.last_error}). Balance stays reserved; retry/reconcile below.",
                    "warning",
                )
        except SimulatedCrash as e:
            flash(request, f"{e}. Refund is stuck in PROCESSING; run `python -m app.cli recover`.", "error")
        return redirect(f"/refunds/{refund_id}")

    @app.post("/refunds/{refund_id}/reject", dependencies=[Depends(verify_csrf)])
    def reject(request: Request, refund_id: int, note: str = Form(""), user: User = Depends(current_user)):
        service.reject(user.id, refund_id, note)
        flash(request, "Refund rejected.")
        return redirect(f"/refunds/{refund_id}")

    @app.post("/refunds/{refund_id}/reconcile", dependencies=[Depends(verify_csrf)])
    def reconcile(request: Request, refund_id: int, user: User = Depends(current_user)):
        r = service.retry_as_reviewer(user.id, refund_id)
        flash(request, f"Reconciled with provider using the original idempotency key: {r.status}.")
        return redirect(f"/refunds/{refund_id}")

    app.state.service = service
    return app


app = create_app() if os.environ.get("REFUNDS_APP_AUTOCREATE", "1") == "1" else None
