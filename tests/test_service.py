import threading
import time
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.models import AuditEvent, PaymentTransaction, Refund, RefundStatus as S
from app.refunds.service import (
    Conflict,
    Forbidden,
    InsufficientBalance,
    RefundService,
    SimulatedCrash,
    demo_crash_hook,
)
from tests.conftest import provider_success_count, run_concurrently

TXN = "txn_1001"  # captured 120.00


def req(svc, amount, actor="alice", txn=TXN):
    return svc.request_refund(actor, txn, amount, "test", str(uuid.uuid4()))


def status_of(sm, rid):
    with sm() as s:
        return s.get(Refund, rid).status


def audit_of(sm, rid):
    with sm() as s:
        return s.execute(select(AuditEvent).where(AuditEvent.refund_id == rid).order_by(AuditEvent.id)).scalars().all()


# --- authorization -----------------------------------------------------------------------------


def test_agent_cannot_approve(svc, sm):
    r = req(svc, 1000)
    with pytest.raises(Forbidden):
        svc.approve("ben", r.id)
    assert status_of(sm, r.id) == S.REQUESTED


def test_reviewer_without_request_permission_cannot_request(svc):
    with pytest.raises(Forbidden):
        req(svc, 1000, actor="rita")


def test_self_approval_blocked_for_dual_role_user(svc, sm):
    r = req(svc, 1000, actor="sam")
    with pytest.raises(Forbidden):
        svc.approve("sam", r.id)
    with pytest.raises(Forbidden):
        svc.reject("sam", r.id)
    assert status_of(sm, r.id) == S.REQUESTED
    assert svc.approve("rita", r.id).status == S.APPROVED


def test_database_constraint_blocks_self_review(svc, sm):
    r = req(svc, 1000, actor="sam")
    with pytest.raises(IntegrityError):
        with sm.begin() as s:
            s.execute(text("UPDATE refunds SET reviewed_by = 'sam', status = 'APPROVED' WHERE id = :id"), {"id": r.id})


def test_only_reviewers_can_retry(svc, sm):
    r = req(svc, 1099)
    svc.approve("rita", r.id)
    svc.execute("rita", r.id)
    with pytest.raises(Forbidden):
        svc.retry_as_reviewer("alice", r.id)


# --- balance -----------------------------------------------------------------------------------


def test_request_over_captured_amount_rejected(svc):
    with pytest.raises(InsufficientBalance):
        req(svc, 12001)


def test_pending_requests_do_not_reserve_but_approval_enforces(svc, sm):
    a, b = req(svc, 7000), req(svc, 7000)  # both allowed: REQUESTED does not reserve
    svc.approve("rita", a.id)
    with pytest.raises(InsufficientBalance):
        svc.approve("rita", b.id)
    assert status_of(sm, b.id) == S.REQUESTED
    assert svc.balance(TXN).available_cents == 5000


def test_cumulative_partial_refunds_cannot_exceed_balance(svc):
    for amount in (5000, 5000):
        r = req(svc, amount)
        svc.approve("rita", r.id)
        svc.execute("rita", r.id)
    assert svc.balance(TXN).available_cents == 2000
    with pytest.raises(InsufficientBalance):
        req(svc, 2001)
    r = req(svc, 2000)
    # Approval excludes the refund being approved from the reserved sum, so the exact
    # remaining balance can be approved.
    assert svc.approve("rita", r.id).status == S.APPROVED
    assert svc.balance(TXN).available_cents == 0


# --- concurrency (independent sessions/connections per thread) ---------------------------------


def test_concurrent_approvals_cannot_exceed_balance(svc, sm, provider):
    a, b = req(svc, 7000, "alice"), req(svc, 7000, "ben")
    results = run_concurrently(lambda: svc.approve("rita", a.id), lambda: svc.approve("sam", b.id))
    ok = [r for r in results if r[0] == "ok"]
    errs = [r[1] for r in results if r[0] == "err"]
    assert len(ok) == 1 and len(errs) == 1 and isinstance(errs[0], InsufficientBalance)
    assert svc.balance(TXN).reserved_cents == 7000


def test_concurrent_approve_and_execute_same_refund_refunds_once(svc, sm):
    r = req(svc, 3000)

    def approve_and_execute(reviewer):
        svc.approve(reviewer, r.id)
        return svc.execute(reviewer, r.id)

    results = run_concurrently(*[lambda rv=rv: approve_and_execute(rv) for rv in ["rita", "sam"] * 4])
    assert sum(1 for res in results if res[0] == "ok") == 1
    assert all(isinstance(res[1], Conflict) for res in results if res[0] == "err")
    assert status_of(sm, r.id) == S.SUCCEEDED
    assert provider_success_count(sm) == 1
    assert [e.action for e in audit_of(sm, r.id)].count("approved") == 1


def test_concurrent_execute_retries_refund_once(svc, sm):
    r = req(svc, 3000)
    svc.approve("rita", r.id)
    results = run_concurrently(*[lambda: svc.execute("rita", r.id) for _ in range(6)])
    assert sum(1 for res in results if res[0] == "ok") == 1
    assert provider_success_count(sm) == 1
    with pytest.raises(Conflict):
        svc.execute("rita", r.id)
    assert provider_success_count(sm) == 1


def test_approval_waits_for_transaction_row_lock(svc, sm):
    """Proves the approval path actually takes the per-transaction lock first."""
    r = req(svc, 1000)
    done = threading.Event()
    with sm.begin() as s:
        s.execute(select(PaymentTransaction).where(PaymentTransaction.id == TXN).with_for_update())
        t = threading.Thread(target=lambda: (svc.approve("rita", r.id), done.set()))
        t.start()
        time.sleep(0.5)
        assert not done.is_set(), "approval should block while the transaction row is locked"
    t.join(10)
    assert done.is_set()


def test_duplicate_request_submission_creates_one_refund(svc, sm):
    key = str(uuid.uuid4())
    results = run_concurrently(*[lambda: svc.request_refund("alice", TXN, 1000, "dup", key) for _ in range(5)])
    ids = {res[1].id for res in results if res[0] == "ok"}
    assert len(ids) == 1
    with sm() as s:
        assert len(s.execute(select(Refund)).scalars().all()) == 1


# --- provider outcomes and recovery ------------------------------------------------------------


def test_definitive_rejection_fails_and_releases_balance(svc, sm):
    r = req(svc, 1013)
    svc.approve("rita", r.id)
    out = svc.execute("rita", r.id)
    assert out.status == S.FAILED and out.provider_refund_id is None
    assert svc.balance(TXN).available_cents == 12000
    assert provider_success_count(sm) == 0
    assert "provider_rejected" in [e.action for e in audit_of(sm, r.id)]


def test_timeout_keeps_balance_reserved_then_retry_succeeds(svc, sm):
    r = req(svc, 11099)
    other = req(svc, 1000)  # requested while balance is free; REQUESTED does not reserve
    svc.approve("rita", r.id)
    out = svc.execute("rita", r.id)
    assert out.status == S.PROCESSING and "outcome unknown" in out.last_error
    assert svc.balance(TXN).reserved_cents == 11099
    with pytest.raises(InsufficientBalance):
        svc.approve("rita", other.id)
    out = svc.retry_as_reviewer("rita", r.id)
    assert out.status == S.SUCCEEDED and out.attempts == 2
    assert provider_success_count(sm) == 1


def test_lost_response_timeout_reconciles_without_duplicate(svc, sm):
    r = req(svc, 1098)
    svc.approve("rita", r.id)
    assert svc.execute("rita", r.id).status == S.PROCESSING
    assert provider_success_count(sm) == 1  # provider did move (simulated) money
    assert svc.reconcile("rita", r.id).status == S.SUCCEEDED
    assert provider_success_count(sm) == 1


def test_crash_after_provider_success_is_recovered_by_cli(sm, provider):
    from app.cli import RECOVERY_ACTOR, recover

    svc = RefundService(sm, provider, demo_crash_hook)
    r = req(svc, 1097)
    svc.approve("rita", r.id)
    with pytest.raises(SimulatedCrash):
        svc.execute("rita", r.id)
    assert status_of(sm, r.id) == S.PROCESSING
    assert provider_success_count(sm) == 1
    assert svc.balance(TXN).reserved_cents == 1097

    assert recover(svc, stale_seconds=0) == [(r.id, S.SUCCEEDED)]
    assert status_of(sm, r.id) == S.SUCCEEDED
    assert provider_success_count(sm) == 1
    assert svc.balance(TXN).refunded_cents == 1097
    events = audit_of(sm, r.id)
    assert events[-1].actor == RECOVERY_ACTOR and events[-1].action == "provider_succeeded"


def test_recovery_skips_recent_processing_and_concurrent_reconcile_is_safe(sm, provider):
    svc = RefundService(sm, provider, demo_crash_hook)
    r = req(svc, 1097)
    svc.approve("rita", r.id)
    with pytest.raises(SimulatedCrash):
        svc.execute("rita", r.id)
    assert svc.recover_stuck("system:recovery-cli", timedelta(minutes=5)) == []
    results = run_concurrently(*[lambda: svc.reconcile("rita", r.id) for _ in range(4)])
    # Racers either finish (idempotent replay) or find it no longer PROCESSING.
    assert any(res[0] == "ok" for res in results)
    assert all(res[0] == "ok" or isinstance(res[1], Conflict) for res in results)
    assert status_of(sm, r.id) == S.SUCCEEDED
    assert provider_success_count(sm) == 1
    assert [e.action for e in audit_of(sm, r.id)].count("provider_succeeded") == 1


# --- audit -------------------------------------------------------------------------------------


def test_audit_records_actor_action_and_time_for_each_step(svc, sm):
    r = req(svc, 1500)
    svc.approve("rita", r.id, "ok")
    svc.execute("rita", r.id)
    events = audit_of(sm, r.id)
    assert [(e.actor, e.action, e.from_status, e.to_status) for e in events] == [
        ("alice", "requested", None, S.REQUESTED),
        ("rita", "approved", S.REQUESTED, S.APPROVED),
        ("rita", "execution_started", S.APPROVED, S.PROCESSING),
        ("rita", "provider_succeeded", S.PROCESSING, S.SUCCEEDED),
    ]
    times = [e.created_at for e in events]
    assert all(times) and times == sorted(times)


def test_rejection_is_audited_and_terminal(svc, sm):
    r = req(svc, 1500)
    svc.reject("rita", r.id, "not eligible")
    with pytest.raises(Conflict):
        svc.approve("sam", r.id)
    assert [(e.actor, e.action) for e in audit_of(sm, r.id)][-1] == ("rita", "rejected")
