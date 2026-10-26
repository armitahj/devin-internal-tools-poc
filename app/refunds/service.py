"""Refund workflow: state machine, invariants and audit trail.

Locking order (always): payment_transactions row, then refunds row. Every state change and its
audit event are written in the same database transaction. Provider calls happen outside any
database transaction.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import AuditEvent, PaymentTransaction, Refund, RefundStatus, User
from app.payments.adapter import PaymentProvider, ProviderResult, ProviderTimeout

S = RefundStatus


class RefundError(Exception):
    pass


class Forbidden(RefundError):
    pass


class NotFound(RefundError):
    pass


class Conflict(RefundError):
    pass


class InvalidRequest(RefundError):
    pass


class InsufficientBalance(RefundError):
    pass


class SimulatedCrash(Exception):
    """Fault injection: the application 'dies' after the provider call, before recording it."""


@dataclass
class Balance:
    captured_cents: int
    refunded_cents: int
    reserved_cents: int

    @property
    def available_cents(self) -> int:
        return self.captured_cents - self.refunded_cents - self.reserved_cents


def _audit(s: Session, refund: Refund, actor: str, action: str, from_status, to_status, **details):
    s.add(
        AuditEvent(
            refund_id=refund.id,
            transaction_id=refund.transaction_id,
            actor=actor,
            action=action,
            from_status=from_status,
            to_status=to_status,
            details=details,
        )
    )


def compute_balance(s: Session, txn: PaymentTransaction, exclude_refund_id: int | None = None) -> Balance:
    q = select(Refund.status, func.coalesce(func.sum(Refund.amount_cents), 0)).where(
        Refund.transaction_id == txn.id, Refund.status.in_(S.RESERVING)
    )
    if exclude_refund_id is not None:
        q = q.where(Refund.id != exclude_refund_id)
    sums = dict(s.execute(q.group_by(Refund.status)).all())
    refunded = sums.get(S.SUCCEEDED, 0)
    reserved = sums.get(S.APPROVED, 0) + sums.get(S.PROCESSING, 0)
    return Balance(txn.amount_cents, refunded, reserved)


def _lock_txn(s: Session, txn_id: str) -> PaymentTransaction:
    txn = s.execute(select(PaymentTransaction).where(PaymentTransaction.id == txn_id).with_for_update()).scalar_one_or_none()
    if txn is None:
        raise NotFound(f"transaction {txn_id} not found")
    return txn


def _lock_refund(s: Session, refund_id: int) -> tuple[PaymentTransaction, Refund]:
    txn_id = s.execute(select(Refund.transaction_id).where(Refund.id == refund_id)).scalar_one_or_none()
    if txn_id is None:
        raise NotFound(f"refund {refund_id} not found")
    txn = _lock_txn(s, txn_id)
    refund = s.execute(select(Refund).where(Refund.id == refund_id).with_for_update()).scalar_one()
    return txn, refund


def _get_user(s: Session, user_id: str) -> User:
    user = s.get(User, user_id)
    if user is None:
        raise Forbidden("unknown user")
    return user


class RefundService:
    def __init__(
        self,
        sessionmaker,
        provider: PaymentProvider,
        crash_after_provider: Callable[[Refund, ProviderResult], bool] | None = None,
    ):
        self.sessionmaker = sessionmaker
        self.provider = provider
        self.crash_after_provider = crash_after_provider

    # --- request / review -------------------------------------------------------------------

    def request_refund(self, actor_id: str, txn_id: str, amount_cents: int, reason: str, request_key: str) -> Refund:
        if amount_cents <= 0:
            raise InvalidRequest("amount must be positive")
        if not reason.strip():
            raise InvalidRequest("reason is required")
        if not request_key:
            raise InvalidRequest("missing request key")
        try:
            with self.sessionmaker.begin() as s:
                actor = _get_user(s, actor_id)
                if not actor.can_request:
                    raise Forbidden("user is not allowed to request refunds")
                existing = s.execute(select(Refund).where(Refund.request_key == request_key)).scalar_one_or_none()
                if existing is not None:
                    return self._replay(existing, actor_id, txn_id, amount_cents)
                txn = _lock_txn(s, txn_id)
                balance = compute_balance(s, txn)
                if amount_cents > balance.available_cents:
                    raise InsufficientBalance(f"amount exceeds available refundable balance ({balance.available_cents} cents)")
                refund = Refund(
                    transaction_id=txn.id,
                    amount_cents=amount_cents,
                    reason=reason.strip(),
                    status=S.REQUESTED,
                    request_key=request_key,
                    provider_idempotency_key="refund_" + uuid.uuid4().hex,
                    requested_by=actor_id,
                )
                s.add(refund)
                s.flush()
                _audit(s, refund, actor_id, "requested", None, S.REQUESTED, amount_cents=amount_cents, reason=refund.reason)
                return refund
        except IntegrityError:
            # Concurrent duplicate submission with the same request key.
            with self.sessionmaker() as s:
                existing = s.execute(select(Refund).where(Refund.request_key == request_key)).scalar_one_or_none()
                if existing is None:
                    raise
                return self._replay(existing, actor_id, txn_id, amount_cents)

    @staticmethod
    def _replay(existing: Refund, actor_id, txn_id, amount_cents) -> Refund:
        if (existing.requested_by, existing.transaction_id, existing.amount_cents) != (actor_id, txn_id, amount_cents):
            raise Conflict("request key reused with different parameters")
        return existing

    def _review(self, actor_id: str, refund_id: int, approve: bool, note: str | None) -> Refund:
        with self.sessionmaker.begin() as s:
            actor = _get_user(s, actor_id)
            if not actor.can_review:
                raise Forbidden("user is not allowed to review refunds")
            txn, refund = _lock_refund(s, refund_id)
            if refund.requested_by == actor_id:
                raise Forbidden("you cannot review your own refund request")
            if refund.status != S.REQUESTED:
                raise Conflict(f"refund is {refund.status}, not REQUESTED")
            if approve:
                balance = compute_balance(s, txn, exclude_refund_id=refund.id)
                if refund.amount_cents > balance.available_cents:
                    raise InsufficientBalance(f"amount exceeds available refundable balance ({balance.available_cents} cents)")
            new_status = S.APPROVED if approve else S.REJECTED
            refund.status = new_status
            refund.reviewed_by = actor_id
            refund.reviewed_at = datetime.now(timezone.utc)
            refund.review_note = (note or "").strip() or None
            _audit(s, refund, actor_id, "approved" if approve else "rejected", S.REQUESTED, new_status, note=refund.review_note)
            return refund

    def approve(self, actor_id: str, refund_id: int, note: str | None = None) -> Refund:
        return self._review(actor_id, refund_id, True, note)

    def reject(self, actor_id: str, refund_id: int, note: str | None = None) -> Refund:
        return self._review(actor_id, refund_id, False, note)

    # --- execution / recovery ----------------------------------------------------------------

    def execute(self, actor_id: str, refund_id: int) -> Refund:
        """Claim an APPROVED refund (APPROVED -> PROCESSING) and submit it to the provider."""
        with self.sessionmaker.begin() as s:
            txn, refund = _lock_refund(s, refund_id)
            if refund.status != S.APPROVED:
                raise Conflict(f"refund is {refund.status}, not APPROVED")
            refund.status = S.PROCESSING
            refund.attempts += 1
            refund.processing_started_at = datetime.now(timezone.utc)
            _audit(s, refund, actor_id, "execution_started", S.APPROVED, S.PROCESSING, attempt=refund.attempts)
            charge_id, attempt = txn.provider_charge_id, refund.attempts
        return self._drive(actor_id, refund, charge_id, txn.currency, attempt)

    def reconcile(self, actor_id: str, refund_id: int, stale_after: timedelta = timedelta(0)) -> Refund:
        """Re-drive a PROCESSING refund with its original provider idempotency key."""
        with self.sessionmaker.begin() as s:
            txn, refund = _lock_refund(s, refund_id)
            if refund.status != S.PROCESSING:
                raise Conflict(f"refund is {refund.status}, not PROCESSING")
            now = datetime.now(timezone.utc)
            if refund.processing_started_at and now - refund.processing_started_at < stale_after:
                raise Conflict("refund was claimed recently; not stale yet")
            refund.attempts += 1
            refund.processing_started_at = now
            _audit(s, refund, actor_id, "reconcile_started", S.PROCESSING, S.PROCESSING, attempt=refund.attempts)
            charge_id, attempt = txn.provider_charge_id, refund.attempts
        return self._drive(actor_id, refund, charge_id, txn.currency, attempt)

    def retry_as_reviewer(self, actor_id: str, refund_id: int) -> Refund:
        with self.sessionmaker() as s:
            if not _get_user(s, actor_id).can_review:
                raise Forbidden("only reviewers can retry or reconcile refunds")
        return self.reconcile(actor_id, refund_id)

    def _drive(self, actor_id: str, refund: Refund, charge_id: str, currency: str, attempt: int) -> Refund:
        try:
            result = self.provider.refund(
                charge_id=charge_id,
                amount_cents=refund.amount_cents,
                currency=currency,
                idempotency_key=refund.provider_idempotency_key,
            )
        except ProviderTimeout as e:
            return self._record_unknown(actor_id, refund.id, attempt, str(e))
        if self.crash_after_provider and self.crash_after_provider(refund, result):
            raise SimulatedCrash("simulated application crash after provider call, before recording outcome")
        return self._record_result(actor_id, refund.id, attempt, result)

    def _record_result(self, actor_id: str, refund_id: int, attempt: int, result: ProviderResult) -> Refund:
        with self.sessionmaker.begin() as s:
            _, refund = _lock_refund(s, refund_id)
            if refund.status != S.PROCESSING:
                # Another worker already recorded the outcome; nothing to do.
                return refund
            if result.status == "succeeded":
                refund.status = S.SUCCEEDED
                refund.provider_refund_id = result.provider_refund_id
                refund.last_error = None
                action = "provider_succeeded"
            else:
                refund.status = S.FAILED
                refund.last_error = result.message or "rejected by provider"
                action = "provider_rejected"
            refund.completed_at = datetime.now(timezone.utc)
            _audit(
                s,
                refund,
                actor_id,
                action,
                S.PROCESSING,
                refund.status,
                attempt=attempt,
                provider_refund_id=result.provider_refund_id,
                message=result.message,
            )
            return refund

    def _record_unknown(self, actor_id: str, refund_id: int, attempt: int, message: str) -> Refund:
        with self.sessionmaker.begin() as s:
            _, refund = _lock_refund(s, refund_id)
            if refund.status != S.PROCESSING:
                return refund
            refund.last_error = f"outcome unknown: {message}"
            _audit(s, refund, actor_id, "provider_outcome_unknown", S.PROCESSING, S.PROCESSING, attempt=attempt, message=message)
            return refund

    def recover_stuck(self, actor_id: str, stale_after: timedelta) -> list[tuple[int, str]]:
        """Reconcile every PROCESSING refund older than stale_after. Returns (id, final status)."""
        cutoff = datetime.now(timezone.utc) - stale_after
        with self.sessionmaker() as s:
            ids = (
                s.execute(
                    select(Refund.id).where(Refund.status == S.PROCESSING, Refund.processing_started_at <= cutoff).order_by(Refund.id)
                )
                .scalars()
                .all()
            )
        results = []
        for rid in ids:
            try:
                results.append((rid, self.reconcile(actor_id, rid, stale_after).status))
            except Conflict as e:
                results.append((rid, f"skipped: {e}"))
        return results

    # --- reads -------------------------------------------------------------------------------

    def balance(self, txn_id: str) -> Balance:
        with self.sessionmaker() as s:
            txn = s.get(PaymentTransaction, txn_id)
            if txn is None:
                raise NotFound(f"transaction {txn_id} not found")
            return compute_balance(s, txn)


def demo_crash_hook(refund: Refund, result: ProviderResult) -> bool:
    """Demo fault: amounts ending in ..97 crash after the first successful provider call."""
    return refund.amount_cents % 100 == 97 and result.status == "succeeded" and refund.attempts <= 1
