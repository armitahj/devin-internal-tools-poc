"""Request fixtures for the policy matrix.

Register (method, path) -> callable(session) returning {"path_params": {...}, "data": {...}}
for any protected route that needs path parameters or a body that satisfies business rules,
so authorized roles get past validation and reach the permission check honestly.
"""

from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

RouteFixture = Callable[[Session], dict[str, Any]]

ROUTE_FIXTURES: dict[tuple[str, str], RouteFixture] = {}


def _refund(**kwargs: Any) -> RouteFixture:
    def fixture(session: Session) -> dict[str, Any]:
        from tests.refunds.factories import make_refund

        return {"path_params": {"refund_id": make_refund(**kwargs)}}

    return fixture


def _submit_refund(session: Session) -> dict[str, Any]:
    from tests.refunds.factories import make_transaction

    tx = make_transaction(10_000)
    return {"data": {"transaction_id": tx, "amount_cents": 1_000, "reason": "matrix"}}


ROUTE_FIXTURES.update(
    {
        ("GET", "/refunds/{refund_id}"): _refund(),
        ("POST", "/refunds"): _submit_refund,
        ("POST", "/refunds/{refund_id}/approve"): _refund(amount_cents=10_000),
        ("POST", "/refunds/{refund_id}/reject"): _refund(),
        ("POST", "/refunds/{refund_id}/resume-payout"): _refund(status="payment_failed"),
        ("POST", "/refunds/provider-mode"): lambda session: {"data": {"mode": "ok"}},
    }
)


def _flag(environment: str, enabled: bool = False, data: dict | None = None) -> RouteFixture:
    def fixture(session: Session) -> dict[str, Any]:
        from tests.feature_flags.factories import make_flag

        return {"path_params": {"flag_id": make_flag(environment, enabled)}, "data": data}

    return fixture


def _flag_request(session: Session) -> dict[str, Any]:
    from tests.feature_flags.factories import make_request

    # Requested by gwen so dave (engineer) and carol (admin) are both valid approvers.
    return {"path_params": {"request_id": make_request(requested_by="gwen")}}


ROUTE_FIXTURES.update(
    {
        ("POST", "/flags/{flag_id}/toggle"): _flag("dev", data={"enabled": "true"}),
        ("POST", "/flags/{flag_id}/request-change"): _flag("prod", data={"enabled": "true"}),
        ("POST", "/flags/requests/{request_id}/approve"): _flag_request,
        ("POST", "/flags/requests/{request_id}/reject"): _flag_request,
    }
)
