import os

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DEFAULT_DATABASE_URL = "postgresql+psycopg://refunds:refunds@localhost:5433/refunds"


class Base(DeclarativeBase):
    pass


def make_engine(url: str | None = None):
    return create_engine(url or os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL), pool_size=10, max_overflow=20)


def make_sessionmaker(engine):
    return sessionmaker(engine, expire_on_commit=False)
