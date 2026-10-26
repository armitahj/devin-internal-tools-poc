"""Mock authentication. The only place that reads the user cookie; swap for OIDC/SSO here."""

from typing import Annotated, NamedTuple

from fastapi import Depends, Request
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from foundation.db import get_session
from foundation.models import User

USER_COOKIE = "user_id"


class SeedUser(NamedTuple):
    id: str
    name: str
    role: str


SEED_USERS = (
    SeedUser("alice", "Alice", "agent"),
    SeedUser("erin", "Erin", "agent"),
    SeedUser("bob", "Bob", "reviewer"),
    SeedUser("frank", "Frank", "reviewer"),
    SeedUser("carol", "Carol", "admin"),
    SeedUser("dave", "Dave", "engineer"),
    SeedUser("gwen", "Gwen", "engineer"),
)


def seed_users(session: Session) -> None:
    stmt = insert(User).values([u._asdict() for u in SEED_USERS])
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=[User.id], set_={"name": stmt.excluded.name, "role": stmt.excluded.role}
        )
    )


def user_from_cookie(request: Request, session: Session) -> User | None:
    user_id = request.cookies.get(USER_COOKIE)
    return session.get(User, user_id) if user_id else None


def current_user(
    request: Request, session: Annotated[Session, Depends(get_session)]
) -> User | None:
    return user_from_cookie(request, session)
