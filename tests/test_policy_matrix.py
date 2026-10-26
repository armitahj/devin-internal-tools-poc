import pytest

from apps.main import app
from foundation.policy import api_routes, is_public, load_policy, required_actions
from tests.conftest import ROLE_USERS, denied_entries
from tests.route_fixtures import ROUTE_FIXTURES

PROTECTED = [
    (method, route.path, required_actions(route))
    for route in api_routes(app)
    if not is_public(route.path)
    for method in sorted(route.methods - {"HEAD"})
]
ROLES = [*ROLE_USERS, None]
CASES = [(m, p, a, role) for m, p, a in PROTECTED for role in ROLES]


@pytest.mark.parametrize(
    ("method", "path", "actions", "role"),
    CASES,
    ids=[f"{m} {p} as {role or 'anonymous'}" for m, p, _, role in CASES],
)
def test_policy_matrix(client, db_session, method, path, actions, role):
    fixture = ROUTE_FIXTURES.get((method, path))
    if fixture is None and "{" in path:
        pytest.fail(f"Add a fixture for {method} {path} to tests/route_fixtures.py")
    spec = fixture(db_session) if fixture else {}
    if role:
        client.cookies.set("user_id", ROLE_USERS[role])

    response = client.request(
        method, path.format(**spec.get("path_params", {})), data=spec.get("data")
    )

    policy = load_policy()
    if role is not None and all(role in policy[a] for a in actions):
        assert response.status_code != 403, response.text
    else:
        assert response.status_code == 403
        entries = denied_entries(db_session)
        assert [e.entity_id for e in entries] == [actions[0]]
        assert entries[0].actor_id == (ROLE_USERS[role] if role else None)
        assert entries[0].after["path"] == path.format(**spec.get("path_params", {}))
