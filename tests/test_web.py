from sqlalchemy import func, select

from app.models import AuditEvent, Refund, RefundStatus as S, SimProviderRefund
from tests.conftest import csrf_of, login


def count_rows(sm):
    with sm() as s:
        return tuple(s.execute(select(func.count()).select_from(m)).scalar_one() for m in (Refund, AuditEvent, SimProviderRefund))


def request_via_ui(client, amount="10.00", txn="txn_1001"):
    page = client.get(f"/transactions/{txn}").text
    key = page.split('name="request_key" value="')[1].split('"')[0]
    return client.post(
        f"/transactions/{txn}/refunds",
        data={"amount": amount, "reason": "web test", "request_key": key, "csrf_token": csrf_of(page)},
        follow_redirects=False,
    )


def test_full_workflow_over_http(client, sm):
    login(client, "alice")
    r = request_via_ui(client, "25.00")
    assert r.status_code == 303
    rid = int(r.headers["location"].rsplit("/", 1)[1])

    login(client, "rita")
    page = client.get(f"/refunds/{rid}").text
    r = client.post(f"/refunds/{rid}/approve", data={"note": "ok", "csrf_token": csrf_of(page)}, follow_redirects=False)
    assert r.status_code == 303
    page = client.get(f"/refunds/{rid}").text
    assert "SUCCEEDED" in page and "provider_succeeded" in page and "alice" in page and "rita" in page


def test_agent_cannot_approve_over_http(client, sm):
    login(client, "alice")
    rid = int(request_via_ui(client).headers["location"].rsplit("/", 1)[1])
    login(client, "ben")
    page = client.get(f"/refunds/{rid}").text
    assert "Approve and execute" not in page
    r = client.post(f"/refunds/{rid}/approve", data={"csrf_token": csrf_of(page)})
    assert r.status_code == 403
    with sm() as s:
        assert s.get(Refund, rid).status == S.REQUESTED


def test_self_approval_blocked_over_http(client, sm):
    login(client, "sam")
    rid = int(request_via_ui(client).headers["location"].rsplit("/", 1)[1])
    page = client.get(f"/refunds/{rid}").text
    r = client.post(f"/refunds/{rid}/approve", data={"csrf_token": csrf_of(page)})
    assert r.status_code == 403
    with sm() as s:
        assert s.get(Refund, rid).status == S.REQUESTED


def test_over_refund_rejected_over_http(client):
    login(client, "alice")
    assert request_via_ui(client, "120.01").status_code == 422


def test_state_changes_require_csrf(client, sm):
    login(client, "alice")
    page = client.get("/transactions/txn_1001").text
    key = page.split('name="request_key" value="')[1].split('"')[0]
    for token in (None, "wrong"):
        data = {"amount": "1.00", "reason": "x", "request_key": key}
        if token:
            data["csrf_token"] = token
        assert client.post("/transactions/txn_1001/refunds", data=data).status_code == 403
    assert count_rows(sm) == (0, 0, 0)


def test_login_requires_csrf(client):
    r = client.post("/login", data={"user_id": "rita"}, follow_redirects=False)
    assert r.status_code == 403


def test_get_routes_are_read_only(client, sm):
    login(client, "alice")
    rid = int(request_via_ui(client).headers["location"].rsplit("/", 1)[1])
    login(client, "rita")
    before = count_rows(sm)
    for path in ["/", "/login", "/transactions", "/transactions/txn_1001", "/refunds", "/refunds?status=REQUESTED", f"/refunds/{rid}"]:
        assert client.get(path).status_code == 200, path
    # GET on action endpoints is not routed.
    for path in [f"/refunds/{rid}/approve", f"/refunds/{rid}/reject", f"/refunds/{rid}/reconcile", "/transactions/txn_1001/refunds"]:
        assert client.get(path).status_code == 405, path
    assert count_rows(sm) == before
    with sm() as s:
        assert s.get(Refund, rid).status == S.REQUESTED


def test_unauthenticated_redirects_to_login(client):
    r = client.get("/refunds", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
