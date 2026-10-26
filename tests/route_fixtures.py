"""Request fixtures for the policy matrix.

Register (method, path) -> callable(session) returning {"path_params": {...}, "data": {...}}
for any protected route that needs path parameters or a body that satisfies business rules,
so authorized roles get past validation and reach the permission check honestly.
"""

from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

RouteFixture = Callable[[Session], dict[str, Any]]

ROUTE_FIXTURES: dict[tuple[str, str], RouteFixture] = {}
