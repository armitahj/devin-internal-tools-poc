def test_switch_user_sets_cookie_and_redirects_back(client):
    response = client.post(
        "/switch-user", data={"user_id": "bob"}, headers={"referer": "http://x/audit?action=denied"}
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/audit?action=denied"
    assert client.cookies.get("user_id") == "bob"


def test_switch_to_unknown_user_is_400(client):
    assert client.post("/switch-user", data={"user_id": "mallory"}).status_code == 400


def test_audit_page_renders_base_layout_partials_and_switcher(client):
    client.cookies.set("user_id", "alice")
    client.get("/audit")  # denied -> creates an audit entry to display
    client.cookies.set("user_id", "carol")
    response = client.get("/audit")
    assert response.status_code == 200
    html = response.text
    assert 'action="/switch-user"' in html
    assert '<option value="carol" selected>Carol (admin)</option>' in html
    assert html.count("<option value=") >= 7
    assert '<a href="/audit">Audit log</a>' in html
    assert "summary-card alert" in html
    assert 'class="flagged"' in html
    assert "permission:audit.view" in html


def test_nav_hides_links_the_user_cannot_use(client):
    client.cookies.set("user_id", "alice")
    response = client.get("/audit", headers={"accept": "text/html"})
    assert response.status_code == 403
    assert "Alice (agent) is not permitted to do this (audit.view)" in response.text
    assert '<a href="/audit">' not in response.text


def test_static_is_public(client):
    assert client.get("/static/htmx.min.js").status_code == 200
