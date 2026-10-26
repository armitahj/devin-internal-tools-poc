import uuid

from sqlalchemy.orm import Session

from app.models import PaymentTransaction, User

USERS = [
    User(id="alice", name="Alice (support agent)", can_request=True, can_review=False),
    User(id="ben", name="Ben (support agent)", can_request=True, can_review=False),
    User(id="rita", name="Rita (refund reviewer)", can_request=False, can_review=True),
    User(id="sam", name="Sam (supervisor: agent + reviewer)", can_request=True, can_review=True),
]

TRANSACTIONS = [
    ("txn_1001", "Acme Corp", "Annual plan", 12000),
    ("txn_1002", "Jordan Lee", "Monthly plan", 4550),
    ("txn_1003", "Globex Ltd", "Enterprise seats", 30000),
    ("txn_1004", "Priya Shah", "Add-on bundle", 8913),
    ("txn_1005", "Initech", "Usage overage", 15099),
    ("txn_1006", "Chen Wu", "Hardware deposit", 25098),
    ("txn_1007", "Umbrella LLC", "Consulting hours", 50097),
    ("txn_1008", "Maria Garcia", "Monthly plan", 4550),
]


def seed_reference_data(s: Session) -> None:
    for u in USERS:
        s.merge(User(id=u.id, name=u.name, can_request=u.can_request, can_review=u.can_review))
    for tid, customer, desc, cents in TRANSACTIONS:
        s.merge(
            PaymentTransaction(
                id=tid,
                customer_name=customer,
                description=desc,
                amount_cents=cents,
                currency="USD",
                provider_charge_id="sim_ch_" + tid.split("_")[1],
            )
        )


def seed_demo_history(service) -> None:
    """One completed partial refund so the UI starts with some history."""
    r = service.request_refund("alice", "txn_1003", 10000, "Two unused seats", str(uuid.uuid4()))
    service.approve("rita", r.id, "Confirmed seats were never activated")
    service.execute("rita", r.id)
