"""Seed 6 flags across dev, staging and prod plus one pending prod request (skipped if present)."""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.feature_flags.models import Flag, FlagChangeRequest
from apps.feature_flags.service import REQUEST
from foundation import audit

FLAGS = [
    ("new_checkout", "dev", True),
    ("new_checkout", "staging", True),
    ("new_checkout", "prod", False),
    ("instant_refunds", "dev", True),
    ("instant_refunds", "staging", False),
    ("dark_mode", "prod", True),
]


def seed(session: Session) -> bool:
    if session.scalar(select(func.count()).select_from(Flag)):
        return False
    flags = {}
    for key, environment, enabled in FLAGS:
        flag = Flag(key=key, environment=environment, enabled=enabled)
        session.add(flag)
        flags[key, environment] = flag
    session.flush()
    target = flags["new_checkout", "prod"]
    req = FlagChangeRequest(
        flag_id=target.id, desired_enabled=True, requested_by="dave", status="pending"
    )
    session.add(req)
    session.flush()
    audit.record(
        session,
        actor_id="dave",
        entity=REQUEST,
        entity_id=req.id,
        action="request",
        after={
            "status": "pending",
            "flag_id": target.id,
            "key": target.key,
            "environment": target.environment,
            "desired_enabled": True,
        },
    )
    return True
