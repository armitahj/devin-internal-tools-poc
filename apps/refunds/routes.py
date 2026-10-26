from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import and_, distinct, func, or_, select
from sqlalchemy.orm import Session

from apps.refunds import rules, service
from apps.refunds.models import Payout, RefundRequest, Transaction
from foundation.audit import DENIED
from foundation.connectors.payments import MODES, MockPaymentProvider, get_payment_provider
from foundation.db import get_session
from foundation.models import AuditLog, MockProviderLedger, User
from foundation.policy import can, require
from foundation.ui import make_templates

router = APIRouter(prefix="/refunds")
templates = make_templates(Path(__file__).parent / "templates")
templates.env.filters["cents"] = lambda c: f"${c / 100:,.2f}" if c is not None else ""

SessionDep = Annotated[Session, Depends(get_session)]
ProviderDep = Annotated[MockPaymentProvider, Depends(get_payment_provider)]

ATTENTION_AFTER = timedelta(minutes=1)


def _ago(value: datetime | None) -> str:
    if value is None:
        return ""
    seconds = int((datetime.now(UTC) - value).total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("min", 60)):
        if seconds >= size:
            return f"{seconds // size} {unit} ago"
    return "just now"


templates.env.filters["ago"] = _ago


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def needs_attention_clause(now: datetime):
    return or_(
        and_(RefundRequest.status == "approved", RefundRequest.updated_at < now - ATTENTION_AFTER),
        RefundRequest.status.in_(("payment_failed", "payment_unknown")),
    )


def needs_attention(refund: RefundRequest, now: datetime) -> bool:
    if refund.status == "approved":
        return refund.updated_at < now - ATTENTION_AFTER
    return refund.status in ("payment_failed", "payment_unknown")


def summary(session: Session, now: datetime) -> dict[str, int]:
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    count = select(func.count()).select_from(RefundRequest)
    return {
        "pending": session.scalar(count.where(RefundRequest.status == "submitted")),
        "approved_today": session.scalar(
            select(func.count(distinct(AuditLog.entity_id))).where(
                AuditLog.entity == service.ENTITY,
                AuditLog.action == "approve",
                AuditLog.at >= today,
            )
        ),
        "attention": session.scalar(count.where(needs_attention_clause(now))),
        "refunded_7d": session.scalar(
            select(func.coalesce(func.sum(RefundRequest.amount_cents), 0)).where(
                RefundRequest.status == "paid", RefundRequest.updated_at >= now - timedelta(days=7)
            )
        ),
    }


def available_actions(user: User, refund: RefundRequest) -> list[dict]:
    """Actions that apply to the refund's status, each marked allowed or not for this user."""
    own = user is not None and user.id == refund.requested_by
    actions = []
    if refund.status == "submitted":
        high_value = rules.requires_admin(refund.amount_cents)
        if not can(user, "refunds.approve"):
            approve_reason = "reviewers and admins only"
        elif own:
            approve_reason = "you requested this refund"
        elif high_value and not can(user, "refunds.approve_high_value"):
            approve_reason = "over $500: admin approval required"
        else:
            approve_reason = None
        actions.append({"verb": "approve", "label": "Approve", "reason": approve_reason})
        reject_reason = (
            "reviewers and admins only"
            if not can(user, "refunds.reject")
            else "you requested this refund"
            if own
            else None
        )
        actions.append({"verb": "reject", "label": "Reject", "reason": reject_reason})
    if refund.status in rules.RESUMABLE_STATUSES:
        actions.append(
            {
                "verb": "resume-payout",
                "label": "Resume payout",
                "reason": None
                if can(user, "refunds.resume_payout")
                else "reviewers and admins only",
            }
        )
    return actions


def _panel_context(session: Session, user: User, refund_id: int) -> dict:
    refund = service.get_refund(session, refund_id)
    payout = session.scalar(select(Payout).where(Payout.refund_id == refund_id))
    transaction = session.get(Transaction, refund.transaction_id)
    history = session.scalars(
        select(AuditLog)
        .where(
            or_(
                and_(AuditLog.entity == service.ENTITY, AuditLog.entity_id == str(refund_id)),
                and_(
                    AuditLog.action == DENIED,
                    AuditLog.after["path"].astext.like(f"/refunds/{refund_id}/%"),
                ),
            )
        )
        .order_by(AuditLog.id)
    ).all()
    ledger = 0
    if payout is not None:
        ledger = session.scalar(
            select(func.count())
            .select_from(MockProviderLedger)
            .where(MockProviderLedger.idempotency_key == payout.idempotency_key)
        )
    refunded = service.refunded_so_far(session, transaction.id)
    return {
        "user": user,
        "refund": refund,
        "transaction": transaction,
        "remaining": transaction.amount_cents - refunded,
        "payout": payout,
        "ledger_payments": ledger,
        "history": history,
        "actions": available_actions(user, refund),
        "high_value": rules.requires_admin(refund.amount_cents),
        "attention": needs_attention(refund, datetime.now(UTC)),
    }


def _detail(
    request: Request,
    session: Session,
    user: User,
    refund_id: int,
    status_code: int = 200,
    message: str | None = None,
    provider: MockPaymentProvider | None = None,
):
    context = _panel_context(session, user, refund_id) | {"message": message}
    if _is_htmx(request):
        return templates.TemplateResponse(request, "refunds/_panel.html", context)
    provider = provider or get_payment_provider()
    context |= {"provider_mode": provider.mode, "modes": MODES}
    return templates.TemplateResponse(
        request, "refunds/detail.html", context, status_code=status_code
    )


def _outcome_message(session: Session, refund_id: int, verb: str) -> str:
    status = service.get_refund(session, refund_id).status
    return {
        "paid": f"Refund {verb}; payout paid.",
        "payment_failed": f"Refund {verb}; the provider declined the payout (payment_failed).",
        "payment_unknown": (
            f"Refund {verb}; the provider did not answer, outcome unknown (payment_unknown). "
            "Resume the payout to confirm it: it reuses the same idempotency key."
        ),
        "rejected": "Refund rejected.",
    }.get(status, f"Refund {verb}.")


@router.get("", response_class=HTMLResponse)
def list_refunds(
    request: Request,
    user: Annotated[User, Depends(require("refunds.view"))],
    session: SessionDep,
    provider: ProviderDep,
    status: str | None = None,
):
    now = datetime.now(UTC)
    query = (
        select(RefundRequest, Transaction.customer_name)
        .join(Transaction, Transaction.id == RefundRequest.transaction_id)
        .order_by(RefundRequest.id.desc())
    )
    if status:
        query = query.where(RefundRequest.status == status)
    rows = [
        {
            "refund": refund,
            "customer": customer,
            "high_value": rules.requires_admin(refund.amount_cents),
            "attention": needs_attention(refund, now),
        }
        for refund, customer in session.execute(query)
    ]
    return templates.TemplateResponse(
        request,
        "refunds/list.html",
        {
            "user": user,
            "rows": rows,
            "summary": summary(session, now),
            "statuses": rules.REFUND_STATUSES,
            "selected_status": status,
            "provider_mode": provider.mode,
            "modes": MODES,
        },
    )


@router.get("/new", response_class=HTMLResponse)
def new_refund(
    request: Request,
    user: Annotated[User, Depends(require("refunds.submit"))],
    session: SessionDep,
):
    refunded = (
        select(
            RefundRequest.transaction_id,
            func.sum(RefundRequest.amount_cents).label("refunded"),
        )
        .where(RefundRequest.status != "rejected")
        .group_by(RefundRequest.transaction_id)
        .subquery()
    )
    transactions = [
        {"tx": tx, "remaining": tx.amount_cents - (done or 0)}
        for tx, done in session.execute(
            select(Transaction, refunded.c.refunded)
            .outerjoin(refunded, refunded.c.transaction_id == Transaction.id)
            .order_by(Transaction.id)
        )
    ]
    return templates.TemplateResponse(
        request, "refunds/new.html", {"user": user, "transactions": transactions}
    )


def _to_cents(amount: str | None, amount_cents: int | None) -> int:
    if amount_cents is not None:
        cents = amount_cents
    elif amount:
        try:
            value = Decimal(amount.strip().lstrip("$").replace(",", ""))
        except InvalidOperation:
            raise HTTPException(status_code=422, detail=f"{amount!r} is not an amount") from None
        if value != value.quantize(Decimal("0.01")):
            raise HTTPException(status_code=422, detail="Amounts have at most two decimals")
        cents = int(value * 100)
    else:
        raise HTTPException(status_code=422, detail="Enter an amount")
    if cents <= 0:
        raise HTTPException(status_code=422, detail="The amount must be greater than zero")
    return cents


@router.post("", response_class=HTMLResponse)
def submit_refund(
    request: Request,
    user: Annotated[User, Depends(require("refunds.submit"))],
    session: SessionDep,
    transaction_id: Annotated[int, Form()],
    reason: Annotated[str, Form(min_length=1)],
    amount_cents: Annotated[int | None, Form(gt=0)] = None,
    amount: Annotated[str | None, Form(description="Dollars, e.g. 120.00")] = None,
):
    refund = service.submit(session, user, transaction_id, _to_cents(amount, amount_cents), reason)
    if _is_htmx(request):
        return Response(status_code=200, headers={"HX-Redirect": f"/refunds/{refund.id}"})
    return _detail(request, session, user, refund.id, status_code=201)


@router.post("/provider-mode")
def set_provider_mode(
    request: Request,
    user: Annotated[User, Depends(require("refunds.set_provider_mode"))],
    session: SessionDep,
    provider: ProviderDep,
    mode: Annotated[str, Form()],
):
    service.set_provider_mode(session, user, provider, mode)
    if _is_htmx(request):
        return templates.TemplateResponse(
            request,
            "refunds/_provider_mode.html",
            {
                "user": user,
                "provider_mode": provider.mode,
                "modes": MODES,
                "message": f"Mock provider mode set to {provider.mode}.",
            },
        )
    return RedirectResponse(request.headers.get("referer") or "/refunds", status_code=303)


@router.get("/{refund_id}", response_class=HTMLResponse)
def refund_detail(
    request: Request,
    refund_id: int,
    user: Annotated[User, Depends(require("refunds.view"))],
    session: SessionDep,
):
    return _detail(request, session, user, refund_id)


@router.post("/{refund_id}/approve", response_class=HTMLResponse)
def approve_refund(
    request: Request,
    refund_id: int,
    user: Annotated[User, Depends(require("refunds.approve"))],
    session: SessionDep,
    provider: ProviderDep,
):
    service.approve(session, refund_id, user, provider)
    message = _outcome_message(session, refund_id, "approved")
    return _detail(request, session, user, refund_id, message=message, provider=provider)


@router.post("/{refund_id}/reject", response_class=HTMLResponse)
def reject_refund(
    request: Request,
    refund_id: int,
    user: Annotated[User, Depends(require("refunds.reject"))],
    session: SessionDep,
):
    service.reject(session, refund_id, user)
    return _detail(request, session, user, refund_id, message="Refund rejected.")


@router.post("/{refund_id}/resume-payout", response_class=HTMLResponse)
def resume_payout(
    request: Request,
    refund_id: int,
    user: Annotated[User, Depends(require("refunds.resume_payout"))],
    session: SessionDep,
    provider: ProviderDep,
):
    service.resume_payout(session, refund_id, user, provider)
    message = _outcome_message(session, refund_id, "payout resumed")
    return _detail(request, session, user, refund_id, message=message, provider=provider)
