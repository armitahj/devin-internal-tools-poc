import os
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://postgres:postgres@localhost:5433/foundation_test",
)
# foundation.db reads DATABASE_URL at import time, so set it before the app is imported.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL

ROLE_USERS = {"agent": "alice", "reviewer": "bob", "admin": "carol", "engineer": "dave"}


def _ensure_database(url: str) -> None:
    target = make_url(url)
    admin = create_engine(target.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": target.database}
        ).scalar()
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{target.database}"'))
    admin.dispose()


@pytest.fixture(scope="session", autouse=True)
def database() -> None:
    _ensure_database(TEST_DATABASE_URL)
    config = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    config.attributes["configure_logger"] = False
    command.upgrade(config, "head")

    from foundation.auth import seed_users
    from foundation.db import SessionLocal

    with SessionLocal() as session, session.begin():
        seed_users(session)


@pytest.fixture(autouse=True)
def clean_tables(database: None) -> None:
    from foundation.db import engine

    # TRUNCATE is not blocked by the audit_log UPDATE/DELETE trigger.
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE audit_log, mock_provider_ledger RESTART IDENTITY"))


@pytest.fixture()
def db_session():
    from foundation.db import SessionLocal

    with SessionLocal() as session:
        yield session


@pytest.fixture()
def client() -> Iterator[TestClient]:
    from apps.main import app

    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client


def denied_entries(session, permission: str | None = None):
    from sqlalchemy import select

    from foundation.models import AuditLog

    query = select(AuditLog).where(AuditLog.action == "denied")
    if permission:
        query = query.where(AuditLog.entity_id == permission)
    return session.scalars(query).all()


@pytest.fixture()
def ticket(database: None):
    """A scratch_tickets row in status 'submitted' requested by alice; table dropped after."""
    from foundation.db import SessionLocal, engine
    from tests.scratch import ScratchBase, Ticket

    ScratchBase.metadata.create_all(engine)
    with SessionLocal() as session, session.begin():
        row = Ticket(status="submitted", requested_by="alice")
        session.add(row)
        session.flush()
        ticket_id = row.id
    yield ticket_id
    ScratchBase.metadata.drop_all(engine)
