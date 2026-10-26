"""Operator commands. Usage: python -m app.cli <command>"""

import argparse
import uuid
from datetime import timedelta

from sqlalchemy import func, select

from app.db import Base, make_engine, make_sessionmaker
from app.models import AuditEvent, Refund, SimProviderRefund
from app.payments.simulator import SimulatedProvider
from app.refunds.service import RefundService, SimulatedCrash, demo_crash_hook
from app.seed import seed_demo_history, seed_reference_data

RECOVERY_ACTOR = "system:recovery-cli"


def build(engine=None):
    engine = engine or make_engine()
    sm = make_sessionmaker(engine)
    return engine, sm, RefundService(sm, SimulatedProvider(sm), demo_crash_hook)


def reset_db(engine, sm, service):
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with sm.begin() as s:
        seed_reference_data(s)
    seed_demo_history(service)


def recover(service, stale_seconds: float):
    results = service.recover_stuck(RECOVERY_ACTOR, timedelta(seconds=stale_seconds))
    if not results:
        print("No PROCESSING refunds older than %ss." % stale_seconds)
    for rid, status in results:
        print(f"refund {rid}: {status}")
    return results


def demo_crash_recovery(sm, service):
    def show(label, rid):
        with sm() as s:
            r = s.get(Refund, rid)
            n = s.execute(
                select(func.count())
                .select_from(SimProviderRefund)
                .where(
                    SimProviderRefund.idempotency_key == r.provider_idempotency_key,
                    SimProviderRefund.status == "succeeded",
                )
            ).scalar_one()
            print(f"  {label:<38} status={r.status:<11} provider_refunds_for_key={n}")

    print("1. Agent 'ben' requests $0.97 against txn_1001; reviewer 'rita' approves.")
    r = service.request_refund("ben", "txn_1001", 97, "Crash recovery demo", str(uuid.uuid4()))
    service.approve("rita", r.id, "demo")
    show("after approval", r.id)
    print("2. Execute. Provider records success, then the app crashes before recording it.")
    try:
        service.execute("rita", r.id)
    except SimulatedCrash as e:
        print(f"  !! {e}")
    show("after crash", r.id)
    print("3. Operator runs recovery (reuses the original provider idempotency key).")
    recover(service, 0)
    show("after recovery", r.id)
    with sm() as s:
        for e in s.execute(select(AuditEvent).where(AuditEvent.refund_id == r.id).order_by(AuditEvent.id)).scalars():
            print(f"  audit {e.created_at:%H:%M:%S.%f} {e.actor:<20} {e.action:<26} {e.from_status} -> {e.to_status}")


def main():
    p = argparse.ArgumentParser(prog="python -m app.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("reset-db", help="drop, recreate and seed the database (synthetic data)")
    rp = sub.add_parser("recover", help="reconcile refunds stuck in PROCESSING")
    rp.add_argument("--stale-seconds", type=float, default=60.0)
    sub.add_parser("demo-crash-recovery", help="end-to-end crash-after-provider-success demo")
    args = p.parse_args()

    engine, sm, service = build()
    if args.cmd == "reset-db":
        reset_db(engine, sm, service)
        print("Database reset and seeded.")
    elif args.cmd == "recover":
        recover(service, args.stale_seconds)
    elif args.cmd == "demo-crash-recovery":
        demo_crash_recovery(sm, service)


if __name__ == "__main__":
    main()
