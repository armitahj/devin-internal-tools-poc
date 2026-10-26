from collections.abc import Iterable
from typing import Any

from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.orm import Session

from foundation import audit


def transition(
    session: Session,
    model: type,
    id: object,
    from_states: Iterable[str],
    to_state: str,
    actor: str,
    forbid_actor: str | None = None,
    values: dict[str, Any] | None = None,
) -> None:
    """Move one row from any of `from_states` to `to_state` with a single conditional UPDATE.

    Concurrent callers race on the row lock; the loser matches 0 rows and gets 409.
    `forbid_actor` (e.g. the requester) may not perform the transition: 403.
    Does not commit and does not write audit; the caller records audit in the same session.
    """
    from_states = list(from_states)
    if forbid_actor is not None and actor == forbid_actor:
        audit.record_denied(
            actor_id=actor,
            permission=f"{model.__tablename__}.{to_state}",
            reason=f"{actor} may not act on their own {model.__tablename__} {id}",
        )
        raise HTTPException(status_code=403, detail="You cannot act on your own request")

    stmt = (
        update(model)
        .where(model.id == id, model.status.in_(from_states))
        .values(status=to_state, **(values or {}))
        .returning(model.id)
        .execution_options(synchronize_session="fetch")
    )
    if session.execute(stmt).first() is None:
        raise HTTPException(
            status_code=409,
            detail=f"{model.__tablename__} {id} is not in {from_states}; cannot move to {to_state}",
        )
