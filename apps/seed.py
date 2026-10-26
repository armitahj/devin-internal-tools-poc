"""Global seed command: python -m apps.seed (idempotent). Apps add their seed calls here."""

from apps.refunds.seed import seed as seed_refunds
from foundation.auth import seed_users
from foundation.db import SessionLocal


def main() -> None:
    with SessionLocal() as session, session.begin():
        seed_users(session)
        refunds_seeded = seed_refunds(session)
    print(f"Seeded users{' and refunds' if refunds_seeded else ''}.")


if __name__ == "__main__":
    main()
