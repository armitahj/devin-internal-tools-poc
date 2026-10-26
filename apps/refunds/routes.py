from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.refunds import rules, service
from apps.refunds.models import Payout, RefundRequest, Transaction
from foundation.connectors.payments import MODES, MockPaymentProvider, get_payment_provider
from foundation.db import get_session
from foundation.models import AuditLog, User
from foundation.policy import require
from foundation.ui import make_templates

router = APIRouter(prefix="/refunds")
templates = make_templates(Path(__file__).parent / "templates")
templates.env.filters["cents"] = lambda c: f"${c / 100:,.2f}" if c is not None else ""

SessionDep = Annotated[Session, Depends(get_session)]
ProviderDep = Annotated[MockPaymentProvider, Depends(get_payment_provider)]


def _detail(request: Request, session: Session, user: User, refund_id: int, status_code=200):
    refund = service.get_refund(session, refund_id)
    history = session.scalars(
        select(AuditLog)
        .where(AuditLog.entity == service.ENTITY, AuditLog.entity_id == str(refund_id))
        .order_by(AuditLog.id)
    ).all()
    return templates.TemplateResponse(
        request,
        "refunds/detail.html",
        {
            "user": user,
            "refund": refund,
            "transaction": session.get(Transaction, refund.transaction_id),
            "payout": session.scalar(select(Payout).where(Payout.refund_id == refund_id)),
            "history": history,
            "resumable": refund.status in rules.RESUMABLE_STATUSES,
        },
        status_code=status_code,
    )


@router.get("", response_class=HTMLResponse)
def list_refunds(
    request: Request,
    user: Annotated[User, Depends(require("refunds.view"))],
    session: SessionDep,
    provider: ProviderDep,
    status: str | None = None,
):
    query = select(RefundRequest).order_by(RefundRequest.id.desc())
    if status:
        query = query.where(RefundRequest.status == status)
    return templates.TemplateResponse(
        request,
        "refunds/list.html",
        {
            "user": user,
            "refunds": session.scalars(query).all(),
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
    transactions = session.scalars(select(Transaction).order_by(Transaction.id)).all()
    return templates.TemplateResponse(
        request, "refunds/new.html", {"user": user, "transactions": transactions}
    )


@router.post("", response_class=HTMLResponse)
def submit_refund(
    request: Request,
    user: Annotated[User, Depends(require("refunds.submit"))],
    session: SessionDep,
    transaction_id: Annotated[int, Form()],
    amount_cents: Annotated[int, Form(gt=0)],
    reason: Annotated[str, Form(min_length=1)],
):
    refund = service.submit(session, user, transaction_id, amount_cents, reason)
    return _detail(request, session, user, refund.id, status_code=201)


@router.post("/provider-mode")
def set_provider_mode(
    user: Annotated[User, Depends(require("refunds.set_provider_mode"))],
    session: SessionDep,
    provider: ProviderDep,
    mode: Annotated[str, Form()],
):
    service.set_provider_mode(session, user, provider, mode)
    return RedirectResponse("/refunds", status_code=303)


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
    return _detail(request, session, user, refund_id)


@router.post("/{refund_id}/reject", response_class=HTMLResponse)
def reject_refund(
    request: Request,
    refund_id: int,
    user: Annotated[User, Depends(require("refunds.reject"))],
    session: SessionDep,
):
    service.reject(session, refund_id, user)
    return _detail(request, session, user, refund_id)


@router.post("/{refund_id}/resume-payout", response_class=HTMLResponse)
def resume_payout(
    request: Request,
    refund_id: int,
    user: Annotated[User, Depends(require("refunds.resume_payout"))],
    session: SessionDep,
    provider: ProviderDep,
):
    service.resume_payout(session, refund_id, user, provider)
    return _detail(request, session, user, refund_id)
