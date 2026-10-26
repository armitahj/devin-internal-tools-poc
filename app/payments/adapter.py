from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class ProviderResult:
    status: Literal["succeeded", "rejected"]
    provider_refund_id: str | None = None
    message: str | None = None


class ProviderTimeout(Exception):
    """The provider did not answer. The refund may or may not have been executed."""


class PaymentProvider(Protocol):
    def refund(self, *, charge_id: str, amount_cents: int, currency: str, idempotency_key: str) -> ProviderResult:
        """Submit (or replay) a refund. Same idempotency_key must never move money twice."""
        ...
