from collections.abc import Callable, Iterator
from functools import cache
from pathlib import Path
from typing import Annotated

import yaml
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

from foundation import audit
from foundation.auth import current_user
from foundation.models import User

POLICY_PATH = Path(__file__).with_name("policy.yaml")

PUBLIC_PATHS = frozenset({"/", "/health", "/switch-user"})
PUBLIC_PATH_PREFIXES = ("/static",)


@cache
def load_policy() -> dict[str, frozenset[str]]:
    raw = yaml.safe_load(POLICY_PATH.read_text())
    return {action: frozenset(roles) for action, roles in raw.items()}


def can(user: User | None, action: str) -> bool:
    """Unknown actions are denied (fail closed)."""
    return user is not None and user.role in load_policy().get(action, frozenset())


def require(action: str) -> Callable[..., User]:
    """Dependency that enforces `action` and returns the current user.

    The returned callable carries `required_action` so tests can discover each route's action.
    """

    def dependency(request: Request, user: Annotated[User | None, Depends(current_user)]) -> User:
        if not can(user, action):
            audit.record_denied(
                actor_id=user.id if user else None,
                permission=action,
                method=request.method,
                path=request.url.path,
                reason="not signed in" if user is None else f"role {user.role} lacks {action}",
            )
            raise HTTPException(status_code=403, detail=f"Permission denied: {action}")
        return user

    dependency.required_action = action  # type: ignore[attr-defined]
    dependency.__name__ = f"require_{action.replace('.', '_')}"
    return dependency


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PATH_PREFIXES)


def _actions(dependant: Dependant) -> Iterator[str]:
    for dep in dependant.dependencies:
        action = getattr(dep.call, "required_action", None)
        if action is not None:
            yield action
        yield from _actions(dep)


def required_actions(route: APIRoute) -> list[str]:
    return list(dict.fromkeys(_actions(route.dependant)))


def api_routes(app: FastAPI) -> list[APIRoute]:
    return [r for r in app.routes if isinstance(r, APIRoute)]
