import threading
import time

import pytest
from fastapi import HTTPException

from foundation.db import SessionLocal
from foundation.workflow import transition
from tests.conftest import denied_entries
from tests.scratch import Ticket


def test_transition_updates_status_and_values(ticket):
    with SessionLocal() as session:
        transition(
            session, Ticket, ticket, ["submitted"], "approved", "bob", values={"reviewed_by": "bob"}
        )
        session.commit()
    with SessionLocal() as session:
        row = session.get(Ticket, ticket)
        assert (row.status, row.reviewed_by) == ("approved", "bob")


def test_transition_from_wrong_state_is_409(ticket):
    with SessionLocal() as session, pytest.raises(HTTPException) as exc:
        transition(session, Ticket, ticket, ["approved"], "paid", "bob")
    assert exc.value.status_code == 409


def test_missing_row_is_409(ticket):
    with SessionLocal() as session, pytest.raises(HTTPException) as exc:
        transition(session, Ticket, ticket + 999, ["submitted"], "approved", "bob")
    assert exc.value.status_code == 409


def test_forbidden_actor_is_403_and_recorded(ticket, db_session):
    with SessionLocal() as session, pytest.raises(HTTPException) as exc:
        transition(
            session, Ticket, ticket, ["submitted"], "approved", "alice", forbid_actor="alice"
        )
    assert exc.value.status_code == 403
    with SessionLocal() as session:
        assert session.get(Ticket, ticket).status == "submitted"
    [entry] = denied_entries(db_session)
    assert (entry.actor_id, entry.entity_id) == ("alice", "scratch_tickets.approved")


def test_concurrent_transitions_exactly_one_wins(ticket):
    barrier = threading.Barrier(2)
    outcomes: dict[str, int] = {}

    def approve(actor: str) -> None:
        with SessionLocal() as session:
            barrier.wait()
            try:
                transition(
                    session,
                    Ticket,
                    ticket,
                    ["submitted"],
                    "approved",
                    actor,
                    values={"reviewed_by": actor},
                )
                time.sleep(0.3)  # hold the row lock so the other UPDATE has to wait on it
                session.commit()
                outcomes[actor] = 200
            except HTTPException as exc:
                session.rollback()
                outcomes[actor] = exc.status_code

    threads = [threading.Thread(target=approve, args=(a,)) for a in ("bob", "frank")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(outcomes.values()) == [200, 409]
    winner = next(a for a, code in outcomes.items() if code == 200)
    with SessionLocal() as session:
        row = session.get(Ticket, ticket)
        assert (row.status, row.reviewed_by) == ("approved", winner)
