"""R6 and R7: payouts, provider failures, crash recovery and resume."""

from sqlalchemy import create_engine, text

from apps.main import app
from apps.refunds import service
from foundation.connectors.payments import MockPaymentProvider, get_payment_provider
from foundation.db import SessionLocal
from foundation.models import User
from tests.refunds.factories import (
    audit_actions,
    ledger_rows,
    make_refund,
    refund_state,
)
from tests.refunds.helpers import client_as, run_concurrently


def set_mode(mode: str) -> None:
    response = client_as("carol").post("/refunds/provider-mode", data={"mode": mode})
    assert response.status_code == 303
    assert get_payment_provider().mode == mode


def approve(refund_id: int, user: str = "bob"):
    return client_as(user).post(f"/refunds/{refund_id}/approve")


def resume(refund_id: int, user: str = "bob"):
    return client_as(user).post(f"/refunds/{refund_id}/resume-payout")


def test_approve_ok_pays_once(database):
    refund_id = make_refund(10_000)
    assert approve(refund_id).status_code == 200
    status, payout = refund_state(refund_id)
    assert status == "paid"
    assert payout.status == "succeeded"
    assert payout.idempotency_key == f"refund-{refund_id}"
    assert payout.provider_ref and payout.attempts == 1 and payout.last_error is None
    assert ledger_rows(refund_id) == 1


def test_provider_failure_is_payment_failed_then_resume_pays(database):
    refund_id = make_refund(10_000)
    set_mode("fail")
    assert approve(refund_id).status_code == 200
    status, payout = refund_state(refund_id)
    assert (status, payout.status, payout.attempts) == ("payment_failed", "failed", 1)
    assert payout.last_error
    assert ledger_rows(refund_id) == 0

    assert resume(refund_id).status_code == 200  # still failing: stays payment_failed
    status, payout = refund_state(refund_id)
    assert (status, payout.status, payout.attempts) == ("payment_failed", "failed", 2)

    set_mode("ok")
    assert resume(refund_id).status_code == 200
    status, payout = refund_state(refund_id)
    assert (status, payout.status, payout.attempts) == ("paid", "succeeded", 3)
    assert payout.idempotency_key == f"refund-{refund_id}"
    assert ledger_rows(refund_id) == 1


def test_timeout_after_success_is_unknown_then_resume_pays_once(database):
    refund_id = make_refund(10_000)
    set_mode("timeout_after_success")
    assert approve(refund_id).status_code == 200
    status, payout = refund_state(refund_id)
    assert status == "payment_unknown"  # never payment_failed
    assert payout.status == "unknown"
    assert ledger_rows(refund_id) == 1  # the provider did pay

    set_mode("ok")
    assert resume(refund_id).status_code == 200
    status, payout = refund_state(refund_id)
    assert (status, payout.status) == ("paid", "succeeded")
    assert payout.idempotency_key == f"refund-{refund_id}"
    assert ledger_rows(refund_id) == 1
    assert audit_actions(refund_id) == ["approve", "payout_unknown", "payout_succeeded"]


def test_unknown_resumed_in_fail_mode_confirms_original_payment(database):
    refund_id = make_refund(10_000)
    set_mode("timeout_after_success")
    approve(refund_id)
    set_mode("fail")
    assert resume(refund_id).status_code == 200
    assert refund_state(refund_id)[0] == "paid"
    assert ledger_rows(refund_id) == 1


def test_crash_after_approval_commit_then_resume_pays_once(database):
    refund_id = make_refund(10_000)
    with SessionLocal() as session:
        # Step 1 only: the process dies before the provider call.
        service.commit_approval(session, refund_id, session.get(User, "bob"))
    status, payout = refund_state(refund_id)
    assert (status, payout.status, payout.attempts) == ("approved", "pending", 0)
    assert ledger_rows(refund_id) == 0

    assert resume(refund_id, "frank").status_code == 200
    status, payout = refund_state(refund_id)
    assert (status, payout.status) == ("paid", "succeeded")
    assert ledger_rows(refund_id) == 1


def test_two_concurrent_resumes_pay_once(database):
    refund_id = make_refund(10_000, status="payment_unknown")
    results = run_concurrently(lambda: resume(refund_id, "bob"), lambda: resume(refund_id, "carol"))
    assert [r.status_code for r in results] == [200, 200]
    status, payout = refund_state(refund_id)
    assert (status, payout.status, payout.attempts) == ("paid", "succeeded", 1)
    assert ledger_rows(refund_id) == 1
    assert audit_actions(refund_id) == ["payout_succeeded"]


def test_resume_reuses_key_across_all_resumable_states(database):
    for status in ("approved", "payment_failed", "payment_unknown"):
        refund_id = make_refund(10_000, status=status)
        assert resume(refund_id).status_code == 200
        _, payout = refund_state(refund_id)
        assert payout.idempotency_key == f"refund-{refund_id}"
        assert refund_state(refund_id)[0] == "paid"
        assert ledger_rows(refund_id) == 1


def test_provider_called_outside_any_db_transaction(database):
    """While the provider runs, no app connection may be idle in a transaction (R6)."""
    from foundation.db import engine

    observed: list[int] = []
    probe = create_engine(engine.url)

    class SpyProvider(MockPaymentProvider):
        def refund(self, idempotency_key, amount_cents):
            with probe.connect() as conn:
                observed.append(
                    conn.scalar(
                        text(
                            "SELECT count(*) FROM pg_stat_activity WHERE datname = "
                            "current_database() AND state LIKE 'idle in transaction%' "
                            "AND pid <> pg_backend_pid()"
                        )
                    )
                )
            return super().refund(idempotency_key, amount_cents)

    spy = SpyProvider()
    app.dependency_overrides[get_payment_provider] = lambda: spy
    try:
        approved = make_refund(10_000)
        assert approve(approved).status_code == 200
        failed = make_refund(10_000, status="payment_failed")
        assert resume(failed).status_code == 200
    finally:
        app.dependency_overrides.pop(get_payment_provider)
        probe.dispose()
    assert observed == [0, 0]


def test_set_provider_mode_rejects_unknown_mode(database):
    response = client_as("carol").post("/refunds/provider-mode", data={"mode": "explode"})
    assert response.status_code == 422
    assert get_payment_provider().mode == "ok"
    assert audit_actions(entity="mock_payment_provider") == []
