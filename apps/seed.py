"""Global seed command: python -m apps.seed (idempotent). Apps add their seed calls here."""

from foundation.auth import seed_users
from foundation.db import SessionLocal


def main() -> None:
    with SessionLocal() as session, session.begin():
        seed_users(session)
    print("Seeded users.")


if __name__ == "__main__":
    main()
