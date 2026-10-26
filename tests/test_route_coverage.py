from fastapi import Depends, FastAPI

from apps.main import app
from foundation.policy import api_routes, is_public, load_policy, require, required_actions

SPEC_POLICY = {
    "refunds.view": {"agent", "reviewer", "admin"},
    "refunds.submit": {"agent"},
    "refunds.approve": {"reviewer", "admin"},
    "refunds.reject": {"reviewer", "admin"},
    "refunds.resume_payout": {"reviewer", "admin"},
    "refunds.approve_high_value": {"admin"},
    "refunds.set_provider_mode": {"admin"},
    "flags.view": {"engineer", "admin"},
    "flags.toggle_nonprod": {"engineer", "admin"},
    "flags.request_prod_change": {"engineer"},
    "flags.approve_prod_change": {"engineer", "admin"},
    "audit.view": {"reviewer", "admin", "engineer"},
}


def coverage_problems(application: FastAPI) -> list[str]:
    policy = load_policy()
    problems = []
    for route in api_routes(application):
        if is_public(route.path):
            continue
        actions = required_actions(route)
        if not actions:
            problems.append(f"{sorted(route.methods)} {route.path} has no require(...)")
        problems += [
            f"{route.path} requires {a!r}, which is not in policy.yaml"
            for a in actions
            if a not in policy
        ]
    return problems


def test_every_non_public_route_requires_a_known_permission():
    assert coverage_problems(app) == []


def test_app_has_protected_routes():
    assert any(not is_public(r.path) for r in api_routes(app))


def test_checker_flags_unprotected_and_unknown_routes():
    probe = FastAPI()

    @probe.get("/health")
    def health():
        return {}

    @probe.get("/open")
    def open_route():
        return {}

    @probe.get("/typo", dependencies=[Depends(require("audit.veiw"))])
    def typo():
        return {}

    @probe.get("/ok", dependencies=[Depends(require("audit.view"))])
    def ok():
        return {}

    problems = coverage_problems(probe)
    assert len(problems) == 2
    assert "/open has no require" in problems[0]
    assert "'audit.veiw'" in problems[1]


def test_policy_yaml_matches_spec():
    assert {k: set(v) for k, v in load_policy().items()} == SPEC_POLICY
