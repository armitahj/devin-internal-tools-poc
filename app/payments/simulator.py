"""Simulated payment provider. No real money moves.

Its state lives in its own table and is committed in its own transaction, independent of the
application's transactions, so it behaves like an external system.

Deterministic fault injection by the amount's cents value:
  ..13 -> definitive rejection (every call)
  ..99 -> first call per idempotency key times out before processing; retry succeeds
  ..98 -> first call per idempotency key processes the refund, then the response is lost (timeout)
"""

import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.models import SimProviderRefund
from app.payments.adapter import ProviderResult, ProviderTimeout


class SimulatedProvider:
    def __init__(self, sessionmaker):
        self.sessionmaker = sessionmaker

    def refund(self, *, charge_id, amount_cents, currency, idempotency_key) -> ProviderResult:
        timeout = False
        with self.sessionmaker.begin() as s:
            # Upsert locks the per-key row, serializing concurrent calls with the same key.
            calls = s.execute(
                insert(SimProviderRefund)
                .values(idempotency_key=idempotency_key, charge_id=charge_id, amount_cents=amount_cents, calls=1)
                .on_conflict_do_update(
                    index_elements=["idempotency_key"],
                    set_={"calls": SimProviderRefund.calls + 1},
                )
                .returning(SimProviderRefund.calls)
            ).scalar_one()
            rec = s.execute(select(SimProviderRefund).where(SimProviderRefund.idempotency_key == idempotency_key)).scalar_one()
            if rec.charge_id != charge_id or rec.amount_cents != amount_cents:
                return ProviderResult("rejected", message="idempotency key reused with different parameters")

            if rec.status is not None:
                result = ProviderResult(rec.status, rec.provider_refund_id, "idempotent replay")
            else:
                cents = amount_cents % 100
                if cents == 13:
                    rec.status = "rejected"
                    result = ProviderResult("rejected", message="card issuer declined refund")
                elif cents == 99 and calls == 1:
                    timeout = True
                    result = None
                else:
                    rec.status = "succeeded"
                    rec.provider_refund_id = "sim_re_" + uuid.uuid4().hex[:12]
                    result = ProviderResult("succeeded", rec.provider_refund_id)
                    if cents == 98 and calls == 1:
                        timeout = True
        if timeout:
            raise ProviderTimeout("simulated provider timeout")
        return result
