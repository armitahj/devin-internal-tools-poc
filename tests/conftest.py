import os
import re
import threading

os.environ.setdefault("REFUNDS_APP_AUTOCREATE", "0")

import psycopg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db import Base, make_engine, make_sessionmaker
from app.models import SimProviderRefund
from app.payments.simulator import SimulatedProvider
from app.refunds.service import RefundService
from app.seed import seed_reference_data

ADMIN_DSN = os.environ.get("TEST_ADMIN_DSN", "postgresql://refunds:refunds@localhost:5433/refunds")
TEST_DB = "refunds_test"
TEST_URL = os.environ.get("TEST_DATABASE_URL", f"postgresql+psycopg://refunds:refunds@localhost:5433/{TEST_DB}")


@pytest.fixture(scope="session")
def engine():
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB,)).fetchone():
            conn.execute(f"CREATE DATABASE {TEST_DB}")
    eng = make_engine(TEST_URL)
    yield eng
    eng.dispose()


@pytest.fixture
def sm(engine):
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    sm = make_sessionmaker(engine)
    with sm.begin() as s:
        seed_reference_data(s)
    return sm


@pytest.fixture
def provider(sm):
    return SimulatedProvider(sm)


@pytest.fixture
def svc(sm, provider):
    return RefundService(sm, provider)


@pytest.fixture
def client(sm, provider):
    from app.main import create_app

    return TestClient(create_app(sm, provider))


def provider_success_count(sm, key=None):
    with sm() as s:
        q = select(func.count()).select_from(SimProviderRefund).where(SimProviderRefund.status == "succeeded")
        if key:
            q = q.where(SimProviderRefund.idempotency_key == key)
        return s.execute(q).scalar_one()


def run_concurrently(*fns):
    """Start all callables at the same instant on separate threads.

    Each service call opens its own Session, so every thread uses an independent DB connection.
    """
    barrier = threading.Barrier(len(fns))
    results = [None] * len(fns)

    def wrap(i, fn):
        barrier.wait()
        try:
            results[i] = ("ok", fn())
        except Exception as e:  # noqa: BLE001
            results[i] = ("err", e)

    threads = [threading.Thread(target=wrap, args=(i, f)) for i, f in enumerate(fns)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return results


CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


def csrf_of(html: str) -> str:
    return CSRF_RE.search(html).group(1)


def login(client, user_id):
    token = csrf_of(client.get("/login").text)
    r = client.post("/login", data={"user_id": user_id, "csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    return token
