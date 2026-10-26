from typing import Any

from sqlalchemy.orm import Session

from foundation.db import SessionLocal
from foundation.models import AuditLog

DENIED = "denied"


def record(
    session: Session,
    *,
    actor_id: str | None,
    entity: str,
    entity_id: object,
    action: str,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> AuditLog:
    """Add an audit row to the caller's session. Never commits: the row commits or rolls
    back together with the state change it describes."""
    entry = AuditLog(
        actor_id=actor_id,
        entity=entity,
        entity_id=str(entity_id),
        action=action,
        before=before,
        after=after,
    )
    session.add(entry)
    return entry


def record_denied(
    *,
    actor_id: str | None,
    permission: str,
    method: str | None = None,
    path: str | None = None,
    reason: str | None = None,
) -> None:
    """Record a denial in its own session and commit immediately, so it survives the
    rollback of the rejected request's main transaction."""
    details = {"method": method, "path": path, "reason": reason}
    with SessionLocal() as session, session.begin():
        record(
            session,
            actor_id=actor_id,
            entity="permission",
            entity_id=permission,
            action=DENIED,
            after={k: v for k, v in details.items() if v is not None} or None,
        )
