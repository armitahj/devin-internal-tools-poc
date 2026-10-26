"""Refund state changes. Every committed state change writes exactly one audit row."""

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.refunds import rules
from apps.refunds.models import Payout, RefundRequest, Transaction
from foundation import audit
from foundation.connectors.payments import (
    MODES,
    MockPaymentProvider,
    PaymentError,
    PaymentProvider,
    PaymentTimeout,
)
from foundation.models import User
from foundation.policy import can
from foundation.workflow import transition


class BalanceExceeded(HTTPException):
    """R3 rule violation: a 409, but not a stale-page race, so no "reload" hint."""

    stale_page_hint = False


ENTITY = "refund"


def get_refund(session: Session, refund_id: int, *, for_update: bool = False) -> RefundRequest:
    query = select(RefundRequest).where(RefundRequest.id == refund_id)
    if for_update:
        query = query.with_for_update()
    refund = session.scalar(query.execution_options(populate_existing=True))
    if refund is None:
        raise HTTPException(status_code=404, detail=f"Refund {refund_id} not found")
    return refund


def refunded_so_far(session: Session, transaction_id: int) -> int:
    """Sum of all non-rejected refunds against a transaction."""
    return session.scalar(
        select(func.coalesce(func.sum(RefundRequest.amount_cents), 0)).where(
            RefundRequest.transaction_id == transaction_id, RefundRequest.status != "rejected"
        )
    )


def submit(
    session: Session, user: User, transaction_id: int, amount_cents: int, reason: str
) -> RefundRequest:
    """R1 (enforced by refunds.submit) and R3: lock the transaction row, check, insert."""
    tx = session.scalar(
        select(Transaction).where(Transaction.id == transaction_id).with_for_update()
    )
    if tx is None:
        raise HTTPException(status_code=404, detail=f"Transaction {transaction_id} not found")
    already = refunded_so_far(session, transaction_id)
    if rules.exceeds_balance(tx.amount_cents, already, amount_cents):
        remaining = rules.dollars(tx.amount_cents - already)
        raise BalanceExceeded(
            status_code=409, detail=f"Refund exceeds remaining balance of {remaining}"
        )
    refund = RefundRequest(
        transaction_id=transaction_id,
        amount_cents=amount_cents,
        reason=reason,
        status="submitted",
        requested_by=user.id,
    )
    session.add(refund)
    session.flush()
    audit.record(
        session,
        actor_id=user.id,
        entity=ENTITY,
        entity_id=refund.id,
        action="submit",
        after={"status": "submitted", "amount_cents": amount_cents, "transaction_id": tx.id},
    )
    session.commit()
    return refund


def commit_approval(session: Session, refund_id: int, user: User) -> str:
    """One approval step; returns the refund's new status.

    Refunds of $1,000 or less: submitted -> approved (R6 step 1, transaction A: conditional
    approve + pending payout row + audit, committed). Over $1,000 (R8): the first approval moves
    submitted -> partially_approved with no payout; a second, different approver moves
    partially_approved -> approved and runs transaction A.

    R2 via forbid_actor on every step, R4 via refunds.approve_high_value (at least one admin
    among the approvers), R5 via the conditional UPDATE from the status this approval was
    decided on. first_approved_by is written only on the way into partially_approved, so it
    cannot change between the checks below and the UPDATE that requires that status.
    """
    refund = get_refund(session, refund_id)
    if refund.status not in rules.REVIEWABLE_STATUSES:
        raise HTTPException(status_code=409, detail=f"Refund {refund_id} is {refund.status}")
    from_status, to_status = rules.approval_step(refund.amount_cents, refund.status)
    first_approver = refund.first_approved_by if from_status == "partially_approved" else None
    path = f"/refunds/{refund_id}/approve"

    if first_approver == user.id:
        audit.record_denied(
            actor_id=user.id,
            permission="refunds.approve",
            method="POST",
            path=path,
            reason=f"{user.id} already approved refund {refund_id}; a different approver is needed",
        )
        raise HTTPException(
            status_code=403,
            detail="You already approved this refund; a second, different approver is needed",
        )
    first_is_admin = None
    if first_approver is not None:
        first_is_admin = can(session.get(User, first_approver), "refunds.approve_high_value")
    if not rules.admin_approval_satisfied(
        refund.amount_cents, can(user, "refunds.approve_high_value"), first_is_admin
    ):
        audit.record_denied(
            actor_id=user.id,
            permission="refunds.approve_high_value",
            method="POST",
            path=path,
            reason=f"refund {refund_id} is over ${rules.HIGH_VALUE_THRESHOLD_CENTS // 100}",
        )
        raise HTTPException(status_code=403, detail="Refunds over $500 need an admin")

    if to_status == "partially_approved":
        transition(
            session,
            RefundRequest,
            refund_id,
            [from_status],
            to_status,
            user.id,
            forbid_actor=refund.requested_by,
            values={"first_approved_by": user.id},
        )
        audit.record(
            session,
            actor_id=user.id,
            entity=ENTITY,
            entity_id=refund_id,
            action="partially_approve",
            before={"status": from_status},
            after={"status": to_status, "first_approved_by": user.id},
        )
        session.commit()
        return to_status

    transition(
        session,
        RefundRequest,
        refund_id,
        [from_status],
        "approved",
        user.id,
        forbid_actor=refund.requested_by,
        values={"reviewed_by": user.id},
    )
    key = rules.idempotency_key(refund_id)
    session.add(Payout(refund_id=refund_id, idempotency_key=key, status="pending", attempts=0))
    before = {"status": from_status}
    after = {"status": "approved", "reviewed_by": user.id, "idempotency_key": key}
    if first_approver is not None:
        before["first_approved_by"] = first_approver
        after["approvers"] = [first_approver, user.id]
    audit.record(
        session,
        actor_id=user.id,
        entity=ENTITY,
        entity_id=refund_id,
        action="approve",
        before=before,
        after=after,
    )
    session.commit()
    return "approved"


def approve(session: Session, refund_id: int, user: User, provider: PaymentProvider) -> None:
    if commit_approval(session, refund_id, user) == "approved":
        execute_payout(session, refund_id, provider, user.id)


def reject(session: Session, refund_id: int, user: User) -> None:
    """Rejects a refund awaiting review, including one with a first approval (R8)."""
    refund = get_refund(session, refund_id)
    if refund.status not in rules.REVIEWABLE_STATUSES:
        raise HTTPException(status_code=409, detail=f"Refund {refund_id} is {refund.status}")
    from_status = refund.status
    transition(
        session,
        RefundRequest,
        refund_id,
        [from_status],
        "rejected",
        user.id,
        forbid_actor=refund.requested_by,
        values={"reviewed_by": user.id},
    )
    audit.record(
        session,
        actor_id=user.id,
        entity=ENTITY,
        entity_id=refund_id,
        action="reject",
        before={"status": from_status},
        after={"status": "rejected", "reviewed_by": user.id},
    )
    session.commit()


def resume_payout(session: Session, refund_id: int, user: User, provider: PaymentProvider) -> None:
    """R7: re-run the provider call + result recording with the same idempotency key."""
    refund = get_refund(session, refund_id)
    if refund.status not in rules.RESUMABLE_STATUSES:
        raise HTTPException(
            status_code=409, detail=f"Refund {refund_id} is {refund.status}; nothing to resume"
        )
    execute_payout(session, refund_id, provider, user.id)


def execute_payout(
    session: Session, refund_id: int, provider: PaymentProvider, triggered_by: str
) -> None:
    """R6 steps 2-3: provider call outside any DB transaction, then record the result."""
    payout = session.scalar(select(Payout).where(Payout.refund_id == refund_id))
    if payout is None:
        raise HTTPException(status_code=409, detail=f"Refund {refund_id} has no payout")
    key, amount = payout.idempotency_key, get_refund(session, refund_id).amount_cents
    session.commit()  # end the read transaction: the provider call must not hold one open

    provider_ref = error = None
    try:
        provider_ref = provider.refund(key, amount).provider_ref
        outcome = "paid"
    except PaymentTimeout as exc:
        outcome, error = "payment_unknown", str(exc)
    except PaymentError as exc:
        outcome, error = "payment_failed", str(exc)

    record_payout_result(session, refund_id, outcome, triggered_by, provider_ref, error)


def record_payout_result(
    session: Session,
    refund_id: int,
    outcome: str,
    triggered_by: str,
    provider_ref: str | None = None,
    error: str | None = None,
) -> None:
    """R6 transaction B. Locks the refund so concurrent results are recorded one at a time.

    Provider outcomes are system transitions (SPEC state machine); the user who approved or
    resumed is kept as triggered_by.
    """
    actor_id = rules.SYSTEM_ACTOR
    refund = get_refund(session, refund_id, for_update=True)
    if refund.status == "paid":
        session.rollback()  # another attempt already recorded the (idempotent) success
        return
    if refund.status not in rules.PAYOUT_OUTCOME_FROM[outcome] and refund.status != outcome:
        session.rollback()
        raise HTTPException(
            status_code=409, detail=f"Refund {refund_id} is {refund.status}; cannot be {outcome}"
        )
    # Re-read under lock: the session may still hold the payout loaded before the provider call.
    payout = session.scalar(
        select(Payout)
        .where(Payout.refund_id == refund_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    before = {"status": refund.status, "payout_status": payout.status, "attempts": payout.attempts}
    if refund.status != outcome:
        transition(session, RefundRequest, refund_id, [refund.status], outcome, actor_id)
    payout_status = rules.PAYOUT_STATUS_FOR_OUTCOME[outcome]
    transition(
        session,
        Payout,
        payout.id,
        rules.UNRESOLVED_PAYOUT_STATUSES,
        payout_status,
        actor_id,
        values={
            "attempts": Payout.attempts + 1,
            "last_error": error,
            "provider_ref": provider_ref or payout.provider_ref,
        },
    )
    audit.record(
        session,
        actor_id=actor_id,
        entity=ENTITY,
        entity_id=refund_id,
        action=f"payout_{payout_status}",
        before=before,
        after={
            "status": outcome,
            "payout_status": payout_status,
            "attempts": before["attempts"] + 1,
            "provider_ref": provider_ref,
            "error": error,
            "triggered_by": triggered_by,
        },
    )
    session.commit()


def set_provider_mode(
    session: Session, user: User, provider: MockPaymentProvider, mode: str
) -> None:
    if mode not in MODES:
        raise HTTPException(status_code=422, detail=f"Unknown mode {mode!r}")
    before = provider.mode
    audit.record(
        session,
        actor_id=user.id,
        entity="mock_payment_provider",
        entity_id="mode",
        action="set_mode",
        before={"mode": before},
        after={"mode": mode},
    )
    session.commit()
    provider.mode = mode  # type: ignore[assignment]
