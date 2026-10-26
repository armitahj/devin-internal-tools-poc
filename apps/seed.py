"""Global seed command: python -m apps.seed (idempotent). Apps add their seed calls here."""

from apps.feature_flags.seed import seed as seed_flags
from apps.refunds.seed import seed as seed_refunds
from foundation.auth import seed_users
from foundation.db import SessionLocal


def main() -> None:
    with SessionLocal() as session, session.begin():
        seed_users(session)
        refunds_seeded = seed_refunds(session)
        flags_seeded = seed_flags(session)
    extra = [name for name, done in (("refunds", refunds_seeded), ("flags", flags_seeded)) if done]
    print(f"Seeded users{''.join(' and ' + name for name in extra)}.")


if __name__ == "__main__":
    main()
