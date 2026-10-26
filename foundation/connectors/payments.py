import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from foundation.db import SessionLocal
from foundation.models import MockProviderLedger


class PaymentError(Exception):
    """Definite failure: no money moved."""


class PaymentTimeout(Exception):
    """Outcome unknown: the payment may or may not have happened. Never treat as failure."""


@dataclass(frozen=True)
class PaymentResult:
    idempotency_key: str
    amount_cents: int
    provider_ref: str


class PaymentProvider(Protocol):
    def refund(self, idempotency_key: str, amount_cents: int) -> PaymentResult: ...


Mode = Literal["ok", "fail", "timeout_after_success"]
MODES: tuple[Mode, ...] = ("ok", "fail", "timeout_after_success")


class MockPaymentProvider:
    """Idempotent mock provider backed by mock_provider_ledger.

    The ledger is written in the provider's own session and committed independently of any
    app transaction. A key that is already in the ledger returns the original result in
    ok and fail modes and never inserts a second row. For new keys:
      ok                    -> record payment, return result
      fail                  -> record nothing, raise PaymentError
      timeout_after_success -> record payment, then raise PaymentTimeout
    timeout_after_success also raises PaymentTimeout for keys that are already recorded.
    """

    def __init__(
        self, session_factory: Callable[[], Session] = SessionLocal, mode: Mode = "ok"
    ) -> None:
        self._session_factory = session_factory
        self.mode = mode

    @property
    def mode(self) -> Mode:
        return self._mode

    @mode.setter
    def mode(self, value: Mode) -> None:
        if value not in MODES:
            raise ValueError(f"Unknown mode {value!r}; expected one of {MODES}")
        self._mode = value

    def refund(self, idempotency_key: str, amount_cents: int) -> PaymentResult:
        mode = self.mode
        with self._session_factory() as session, session.begin():
            entry = self._get(session, idempotency_key)
            if entry is None:
                if mode == "fail":
                    raise PaymentError(f"Provider declined refund {idempotency_key}")
                session.execute(
                    insert(MockProviderLedger)
                    .values(
                        idempotency_key=idempotency_key,
                        amount_cents=amount_cents,
                        provider_ref=f"mock_{uuid.uuid4().hex[:16]}",
                    )
                    .on_conflict_do_nothing(index_elements=[MockProviderLedger.idempotency_key])
                )
                entry = self._get(session, idempotency_key)
            assert entry is not None
            if entry.amount_cents != amount_cents:
                raise PaymentError(
                    f"Idempotency key {idempotency_key} reused with a different amount"
                )
            result = PaymentResult(entry.idempotency_key, entry.amount_cents, entry.provider_ref)
        if mode == "timeout_after_success":
            raise PaymentTimeout(f"No response for refund {idempotency_key}")
        return result

    @staticmethod
    def _get(session: Session, key: str) -> MockProviderLedger | None:
        return session.scalar(
            select(MockProviderLedger)
            .where(MockProviderLedger.idempotency_key == key)
            .execution_options(populate_existing=True)
        )


_provider = MockPaymentProvider()


def get_payment_provider() -> MockPaymentProvider:
    return _provider
