"""Committed feature-flag test data (the app reads it through its own sessions)."""

from apps.feature_flags.models import Flag, FlagChangeRequest
from foundation.db import SessionLocal


def make_flag(environment: str = "dev", enabled: bool = False, key: str | None = None) -> int:
    with SessionLocal() as session, session.begin():
        flag = Flag(key=key or f"flag_{environment}", environment=environment, enabled=enabled)
        session.add(flag)
        session.flush()
        return flag.id


def make_request(
    flag_id: int | None = None,
    desired_enabled: bool = True,
    requested_by: str = "gwen",
    status: str = "pending",
) -> int:
    if flag_id is None:
        flag_id = make_flag("prod", enabled=not desired_enabled)
    with SessionLocal() as session, session.begin():
        req = FlagChangeRequest(
            flag_id=flag_id,
            desired_enabled=desired_enabled,
            requested_by=requested_by,
            reviewed_by=None if status == "pending" else "carol",
            status=status,
        )
        session.add(req)
        session.flush()
        return req.id


def flag_enabled(flag_id: int) -> bool:
    with SessionLocal() as session:
        return session.get(Flag, flag_id).enabled


def request_state(request_id: int) -> tuple[str, str | None]:
    with SessionLocal() as session:
        req = session.get(FlagChangeRequest, request_id)
        return req.status, req.reviewed_by


def request_flag(request_id: int) -> int:
    with SessionLocal() as session:
        return session.get(FlagChangeRequest, request_id).flag_id
