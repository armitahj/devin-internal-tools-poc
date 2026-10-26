import threading

import pytest
from sqlalchemy import func, select

from foundation.connectors.payments import (
    MockPaymentProvider,
    PaymentError,
    PaymentProvider,
    PaymentTimeout,
)
from foundation.db import SessionLocal
from foundation.models import MockProviderLedger


def ledger_rows(key: str | None = None) -> int:
    query = select(func.count()).select_from(MockProviderLedger)
    if key:
        query = query.where(MockProviderLedger.idempotency_key == key)
    with SessionLocal() as session:
        return session.scalar(query)


def test_mock_satisfies_protocol():
    provider: PaymentProvider = MockPaymentProvider()
    assert callable(provider.refund)


def test_ok_same_key_twice_records_once_and_returns_original():
    provider = MockPaymentProvider(mode="ok")
    first = provider.refund("refund-1", 1500)
    second = provider.refund("refund-1", 1500)
    assert second == first
    assert first.provider_ref.startswith("mock_")
    assert ledger_rows("refund-1") == 1


def test_fail_records_nothing():
    provider = MockPaymentProvider(mode="fail")
    with pytest.raises(PaymentError):
        provider.refund("refund-2", 1500)
    assert ledger_rows() == 0


def test_fail_mode_returns_original_for_recorded_key():
    provider = MockPaymentProvider(mode="ok")
    original = provider.refund("refund-3", 1500)
    provider.mode = "fail"
    assert provider.refund("refund-3", 1500) == original
    assert ledger_rows("refund-3") == 1


def test_timeout_after_success_records_then_raises_and_retry_returns_original():
    provider = MockPaymentProvider(mode="timeout_after_success")
    with pytest.raises(PaymentTimeout):
        provider.refund("refund-4", 1500)
    assert ledger_rows("refund-4") == 1

    provider.mode = "ok"
    result = provider.refund("refund-4", 1500)
    with SessionLocal() as session:
        stored = session.get(MockProviderLedger, "refund-4")
    assert result.provider_ref == stored.provider_ref
    assert ledger_rows("refund-4") == 1


def test_same_key_different_amount_is_rejected():
    provider = MockPaymentProvider(mode="ok")
    provider.refund("refund-5", 1500)
    with pytest.raises(PaymentError, match="different amount"):
        provider.refund("refund-5", 9999)
    assert ledger_rows("refund-5") == 1


def test_concurrent_calls_with_same_key_record_once():
    provider = MockPaymentProvider(mode="ok")
    barrier = threading.Barrier(5)
    results = []

    def pay() -> None:
        barrier.wait()
        results.append(provider.refund("refund-6", 1500))

    threads = [threading.Thread(target=pay) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 5 and len(set(results)) == 1
    assert ledger_rows("refund-6") == 1


def test_unknown_mode_rejected():
    with pytest.raises(ValueError):
        MockPaymentProvider(mode="maybe")  # type: ignore[arg-type]
