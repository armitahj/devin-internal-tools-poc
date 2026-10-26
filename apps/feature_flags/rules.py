"""Feature-flag rules (SPEC "Feature flags domain"). Allowed values for every status column."""

ENVIRONMENTS = ("dev", "staging", "prod")
# Engineers and admins toggle these directly (audited).
NONPROD_ENVIRONMENTS = ("dev", "staging")
# Changes here go through a change request approved by a different engineer or an admin.
PROD_ENVIRONMENT = "prod"

REQUEST_STATUSES = ("pending", "approved", "rejected")
# Only pending requests can be decided; approved and rejected are final.
DECIDABLE_STATUSES = ("pending",)


def needs_approval(environment: str) -> bool:
    return environment == PROD_ENVIRONMENT


def on_off(enabled: bool) -> str:
    return "on" if enabled else "off"


def check_in_sql(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"
