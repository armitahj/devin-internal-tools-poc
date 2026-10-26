import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from foundation import audit
from foundation.db import SessionLocal
from foundation.models import AuditLog
from foundation.workflow import transition
from tests.conftest import denied_entries
from tests.scratch import Ticket


def count_audit(session) -> int:
    return session.scalar(text("SELECT count(*) FROM audit_log"))


def test_record_does_not_commit(db_session):
    audit.record(db_session, actor_id="bob", entity="thing", entity_id=1, action="touched")
    db_session.flush()
    with SessionLocal() as other:
        assert count_audit(other) == 0
    db_session.rollback()
    assert count_audit(db_session) == 0


def test_state_change_and_audit_roll_back_together(ticket):
    with pytest.raises(RuntimeError), SessionLocal() as session:
        transition(session, Ticket, ticket, ["submitted"], "approved", actor="bob")
        audit.record(
            session,
            actor_id="bob",
            entity="ticket",
            entity_id=ticket,
            action="approve",
            before={"status": "submitted"},
            after={"status": "approved"},
        )
        session.flush()
        raise RuntimeError("crash before commit")

    with SessionLocal() as session:
        assert session.get(Ticket, ticket).status == "submitted"
        assert count_audit(session) == 0


def test_state_change_and_audit_commit_together(ticket):
    with SessionLocal() as session:
        transition(session, Ticket, ticket, ["submitted"], "approved", actor="bob")
        audit.record(session, actor_id="bob", entity="ticket", entity_id=ticket, action="approve")
        session.commit()

    with SessionLocal() as session:
        assert session.get(Ticket, ticket).status == "approved"
        assert session.scalars(select(AuditLog.action)).all() == ["approve"]


def test_denied_audit_survives_rollback_of_main_transaction(ticket):
    with SessionLocal() as session:
        transition(session, Ticket, ticket, ["submitted"], "approved", actor="bob")
        audit.record(session, actor_id="bob", entity="ticket", entity_id=ticket, action="approve")
        audit.record_denied(actor_id="bob", permission="tickets.approve", reason="test")
        session.rollback()

    with SessionLocal() as session:
        assert session.get(Ticket, ticket).status == "submitted"
        [entry] = session.scalars(select(AuditLog)).all()
        assert (entry.action, entry.entity_id, entry.actor_id) == (
            "denied",
            "tickets.approve",
            "bob",
        )


def test_http_denial_is_recorded(client, db_session):
    client.cookies.set("user_id", "alice")
    assert client.get("/audit").status_code == 403
    [entry] = denied_entries(db_session, "audit.view")
    assert entry.actor_id == "alice"
    assert entry.after == {
        "method": "GET",
        "path": "/audit",
        "reason": "role agent lacks audit.view",
    }


@pytest.mark.parametrize(
    "statement", ["UPDATE audit_log SET action = 'x'", "DELETE FROM audit_log"]
)
def test_audit_log_is_append_only(db_session, statement):
    audit.record_denied(actor_id="bob", permission="audit.view")
    with pytest.raises(DBAPIError, match="append-only"):
        db_session.execute(text(statement))
    db_session.rollback()
    assert count_audit(db_session) == 1
