"""Feature-flag state changes. Each audits in the same transaction as the change it describes."""

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from apps.feature_flags import rules
from apps.feature_flags.models import Flag, FlagChangeRequest
from foundation import audit
from foundation.models import User
from foundation.workflow import transition

FLAG = "flag"
REQUEST = "flag_change_request"


def get_flag(session: Session, flag_id: int, *, for_update: bool = False) -> Flag:
    query = select(Flag).where(Flag.id == flag_id)
    if for_update:
        query = query.with_for_update()
    flag = session.scalar(query.execution_options(populate_existing=True))
    if flag is None:
        raise HTTPException(status_code=404, detail=f"Flag {flag_id} not found")
    return flag


def get_request(session: Session, request_id: int) -> FlagChangeRequest:
    req = session.get(FlagChangeRequest, request_id, populate_existing=True)
    if req is None:
        raise HTTPException(status_code=404, detail=f"Change request {request_id} not found")
    return req


def pending_request(session: Session, flag_id: int) -> FlagChangeRequest | None:
    return session.scalar(
        select(FlagChangeRequest).where(
            FlagChangeRequest.flag_id == flag_id, FlagChangeRequest.status == "pending"
        )
    )


def _label(flag: Flag) -> str:
    return f"{flag.environment} {flag.key}"


def toggle(session: Session, user: User, flag_id: int, enabled: bool) -> Flag:
    """dev/staging only. Conditional UPDATE: a stale or concurrent duplicate toggle gets 409."""
    flag = get_flag(session, flag_id)
    if rules.needs_approval(flag.environment):
        audit.record_denied(
            actor_id=user.id,
            permission="flags.toggle_nonprod",
            method="POST",
            path=f"/flags/{flag_id}/toggle",
            reason=f"{_label(flag)} is a prod flag: it changes only through an approved request",
        )
        raise HTTPException(
            status_code=403,
            detail="Prod flags change only through an approved change request",
        )
    changed = session.execute(
        update(Flag)
        .where(
            Flag.id == flag_id,
            Flag.environment.in_(rules.NONPROD_ENVIRONMENTS),
            Flag.enabled.is_not(enabled),
        )
        .values(enabled=enabled)
        .returning(Flag.id)
    ).first()
    if changed is None:
        raise HTTPException(
            status_code=409, detail=f"{_label(flag)} is already {rules.on_off(enabled)}"
        )
    audit.record(
        session,
        actor_id=user.id,
        entity=FLAG,
        entity_id=flag_id,
        action="toggle",
        before={"enabled": not enabled},
        after={"enabled": enabled, "key": flag.key, "environment": flag.environment},
    )
    session.commit()
    return get_flag(session, flag_id)


def request_change(session: Session, user: User, flag_id: int, enabled: bool) -> FlagChangeRequest:
    """prod only. Locks the flag row so two requests for one flag cannot both be opened."""
    flag = get_flag(session, flag_id, for_update=True)
    if not rules.needs_approval(flag.environment):
        raise HTTPException(
            status_code=409,
            detail=f"{_label(flag)} does not need approval: toggle it directly",
        )
    if flag.enabled == enabled:
        raise HTTPException(
            status_code=409, detail=f"{_label(flag)} is already {rules.on_off(enabled)}"
        )
    existing = pending_request(session, flag_id)
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"{_label(flag)} already has pending change request #{existing.id}",
        )
    req = FlagChangeRequest(
        flag_id=flag_id, desired_enabled=enabled, requested_by=user.id, status="pending"
    )
    session.add(req)
    session.flush()
    audit.record(
        session,
        actor_id=user.id,
        entity=REQUEST,
        entity_id=req.id,
        action="request",
        after={
            "status": "pending",
            "flag_id": flag_id,
            "key": flag.key,
            "environment": flag.environment,
            "desired_enabled": enabled,
        },
    )
    session.commit()
    return req


def approve(session: Session, user: User, request_id: int) -> FlagChangeRequest:
    """pending -> approved by someone other than the requester; only then does the flag change.

    The conditional UPDATE in transition() lets exactly one of two concurrent approvals win.
    """
    req = get_request(session, request_id)
    transition(
        session,
        FlagChangeRequest,
        request_id,
        rules.DECIDABLE_STATUSES,
        "approved",
        user.id,
        forbid_actor=req.requested_by,
        values={"reviewed_by": user.id},
    )
    flag = get_flag(session, req.flag_id, for_update=True)
    before = flag.enabled
    session.execute(update(Flag).where(Flag.id == flag.id).values(enabled=req.desired_enabled))
    audit.record(
        session,
        actor_id=user.id,
        entity=REQUEST,
        entity_id=request_id,
        action="approve",
        before={"status": "pending"},
        after={"status": "approved", "reviewed_by": user.id, "flag_id": flag.id},
    )
    audit.record(
        session,
        actor_id=user.id,
        entity=FLAG,
        entity_id=flag.id,
        action="apply_change",
        before={"enabled": before},
        after={
            "enabled": req.desired_enabled,
            "key": flag.key,
            "environment": flag.environment,
            "request_id": request_id,
            "requested_by": req.requested_by,
        },
    )
    session.commit()
    return get_request(session, request_id)


def reject(session: Session, user: User, request_id: int) -> FlagChangeRequest:
    req = get_request(session, request_id)
    transition(
        session,
        FlagChangeRequest,
        request_id,
        rules.DECIDABLE_STATUSES,
        "rejected",
        user.id,
        forbid_actor=req.requested_by,
        values={"reviewed_by": user.id},
    )
    audit.record(
        session,
        actor_id=user.id,
        entity=REQUEST,
        entity_id=request_id,
        action="reject",
        before={"status": "pending"},
        after={"status": "rejected", "reviewed_by": user.id, "flag_id": req.flag_id},
    )
    session.commit()
    return get_request(session, request_id)
